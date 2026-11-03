"""Schema and case conversion for spes."""

from __future__ import annotations

from dataclasses import replace

from sqlglot import exp

from evebench.adapters.common.schema import parse_schema
from evebench.adapters.core import normalize_adapter_input, reject_unknown_options
from evebench.domain.models import CheckOptions, CheckerDescriptor, EquivalenceCase

from .queries import compile_query


def prepare_case(
    case: EquivalenceCase, options: CheckOptions, descriptor: CheckerDescriptor
) -> EquivalenceCase:
    normalized = normalize_adapter_input(case, options, descriptor)
    backend_options = options.for_backend(descriptor.checker.checker_id)
    reject_unknown_options(
        backend_options,
        allowed={"strip_foreign_keys"},
        display_name=descriptor.display_name,
    )
    strip_foreign_keys = backend_options.get("strip_foreign_keys", False)
    if not isinstance(strip_foreign_keys, bool):
        raise ValueError("SPES strip_foreign_keys must be a boolean")
    schema = compile_schema(
        normalized.schema_ddl, normalized.dialect,
        strip_foreign_keys=strip_foreign_keys,
    )

    return replace(
        normalized,
        schema_ddl=schema,
        query_left=compile_query(normalized.query_left, normalized.dialect),
        query_right=compile_query(normalized.query_right, normalized.dialect),
    )


def compile_schema(
    schema_ddl: str, dialect: str, *, strip_foreign_keys: bool = False
) -> str:
    """Fold table additions into CREATE TABLE for SPES's minimal schema loader.

    The loader reads CREATE TABLE definitions and ignores standalone ALTER TABLE
    statements. Keep actions that cannot be folded in the emitted DDL.
    """
    catalog = parse_schema(schema_ddl, dialect)
    rendered: list[exp.Expression] = []
    tables: dict[str, exp.Create] = {}
    for statement in catalog.statements:
        if statement in catalog.tables:
            table = statement.copy()
            if strip_foreign_keys:
                table.this.set("expressions", [
                    item for item in table.this.expressions
                    if not _is_foreign_key(item)
                ])
                for column in table.this.expressions:
                    if isinstance(column, exp.ColumnDef):
                        column.set("constraints", [
                            constraint for constraint in column.args.get("constraints") or []
                            if not _is_foreign_key(constraint)
                        ])
            tables[table.this.this.name.lower()] = table
            rendered.append(table)
        elif isinstance(statement, exp.AlterTable):
            table = tables.get(statement.this.name.lower())
            remaining = []
            for action in statement.args.get("actions") or []:
                if table is not None and isinstance(action, exp.ColumnDef):
                    column = action.copy()
                    if strip_foreign_keys:
                        column.set("constraints", [
                            constraint for constraint in column.args.get("constraints") or []
                            if not _is_foreign_key(constraint)
                        ])
                    table.this.append("expressions", column)
                elif isinstance(action, exp.AddConstraint):
                    constraints = [
                        constraint.copy() for constraint in action.expressions
                        if not (strip_foreign_keys and _is_foreign_key(constraint))
                    ]
                    if table is not None:
                        for constraint in constraints:
                            table.this.append("expressions", constraint)
                    elif constraints:
                        remaining.append(exp.AddConstraint(expressions=constraints))
                else:
                    remaining.append(action.copy())
            if remaining:
                alter = statement.copy()
                alter.set("actions", remaining)
                alter.set("only", False)
                rendered.append(alter)
        else:
            rendered.append(statement.copy())
    return ";\n".join(statement.sql(dialect="mysql") for statement in rendered) + ";"


def _is_foreign_key(expression: exp.Expression) -> bool:
    if isinstance(expression, (exp.ForeignKey, exp.Reference)):
        return True
    if isinstance(expression, (exp.Constraint, exp.ColumnConstraint)):
        return any(_is_foreign_key(child) for child in expression.iter_expressions())
    return False
