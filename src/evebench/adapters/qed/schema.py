"""Schema and case conversion for qed."""

from __future__ import annotations

from dataclasses import replace

from sqlglot import exp

from evebench.adapters.common.schema import parse_schema
from evebench.adapters.core import normalize_adapter_input
from evebench.domain.models import CheckOptions, CheckerDescriptor, EquivalenceCase

from .queries import compile_query


def prepare_case(
    case: EquivalenceCase, options: CheckOptions, descriptor: CheckerDescriptor
) -> EquivalenceCase:
    normalized = normalize_adapter_input(case, options, descriptor)
    strip_foreign_keys = options.for_backend(descriptor.checker.checker_id).get(
        "strip_foreign_keys", False
    )
    if not isinstance(strip_foreign_keys, bool):
        raise ValueError("QED strip_foreign_keys must be a boolean")
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
    """Adapt supported table declarations and forward the rest to QED.

    QED interprets non-CREATE statements as queries, so supported ALTER TABLE
    constraints are folded into CREATE TABLE. FK removal is opt-in.
    """
    catalog = parse_schema(schema_ddl, dialect)
    tables: dict[str, exp.Create] = {}
    for create in catalog.tables:
        table = create.copy()
        table.this.set("expressions", [
            column for column in table.this.expressions
            if not (strip_foreign_keys and _foreign_key(column))
        ])
        added_keys = []
        for column in table.this.expressions:
            if isinstance(column, exp.ColumnDef):
                _normalize_column(column)
                kept = []
                for constraint in column.args.get("constraints") or []:
                    kind = constraint.args.get("kind")
                    if _foreign_key(constraint) and strip_foreign_keys:
                        continue
                    if _foreign_key(constraint):
                        added_keys.append(exp.ForeignKey(
                            expressions=[column.this.copy()],
                            reference=kind.copy(),
                        ))
                        continue
                    if isinstance(kind, exp.PrimaryKeyColumnConstraint):
                        added_keys.append(exp.PrimaryKey(expressions=[column.this.copy()]))
                    elif isinstance(kind, exp.UniqueColumnConstraint):
                        added_keys.append(exp.UniqueColumnConstraint(
                            this=exp.Schema(expressions=[column.this.copy()])
                        ))
                    else:
                        kept.append(constraint)
                column.set("constraints", kept)
        for key in added_keys:
            table.this.append("expressions", key)
        tables[table.this.this.name.lower()] = table

    passthrough: dict[int, exp.Expression] = {}
    for statement in catalog.statements:
        if not isinstance(statement, exp.AlterTable):
            continue
        table = tables.get(statement.this.name.lower())
        if table is None:
            passthrough[id(statement)] = statement
            continue
        unsupported = []
        for action in statement.args.get("actions") or []:
            if isinstance(action, exp.Anonymous) and action.name.upper() == "UNIQUE":
                table.this.append("expressions", exp.UniqueColumnConstraint(
                    this=exp.Schema(expressions=[column.copy() for column in action.expressions])
                ))
                continue
            if not isinstance(action, exp.AddConstraint):
                unsupported.append(action)
                continue
            for constraint in action.expressions:
                if not (strip_foreign_keys and _foreign_key(constraint)):
                    table.this.append("expressions", constraint.copy())
        if unsupported:
            remainder = statement.copy()
            remainder.set("actions", [action.copy() for action in unsupported])
            passthrough[id(statement)] = remainder

    for table in tables.values():
        _quote_identifiers(table)

    rendered = []
    for statement in catalog.statements:
        if isinstance(statement, exp.Create) and statement in catalog.tables:
            rendered.append(tables[statement.this.this.name.lower()].sql())
        elif isinstance(statement, exp.AlterTable):
            if id(statement) in passthrough:
                rendered.append(passthrough[id(statement)].sql())
        else:
            rendered.append(statement.sql())
    return ";\n".join(rendered) + ";"


def _normalize_column(column: exp.ColumnDef) -> None:
    """Render a column in the subset accepted by QED's Calcite DDL parser."""
    data_type = column.args.get("kind")
    if isinstance(data_type, exp.DataType):
        replacement = {
            exp.DataType.Type.TEXT: "VARCHAR",
            exp.DataType.Type.DATETIME: "TIMESTAMP",
            exp.DataType.Type.DATETIME64: "TIMESTAMP",
            exp.DataType.Type.TIMESTAMPLTZ: "TIMESTAMP",
            exp.DataType.Type.TIMESTAMPTZ: "TIMESTAMP",
            exp.DataType.Type.TIMESTAMP_MS: "TIMESTAMP",
            exp.DataType.Type.TIMESTAMP_NS: "TIMESTAMP",
            exp.DataType.Type.TIMESTAMP_S: "TIMESTAMP",
            exp.DataType.Type.TIMETZ: "TIME",
            exp.DataType.Type.SERIAL: "INT",
            exp.DataType.Type.SMALLSERIAL: "SMALLINT",
            exp.DataType.Type.BIGSERIAL: "BIGINT",
        }.get(data_type.this)
        if replacement is not None:
            column.set("kind", exp.DataType.build(replacement))

    column.set(
        "constraints",
        [
            constraint
            for constraint in column.args.get("constraints") or []
            if not isinstance(
                constraint.args.get("kind"),
                (
                    exp.AutoIncrementColumnConstraint,
                    exp.DefaultColumnConstraint,
                    exp.GeneratedAsIdentityColumnConstraint,
                    exp.OnUpdateColumnConstraint,
                ),
            )
        ],
    )


def _quote_identifiers(expression: exp.Expression) -> None:
    for identifier in expression.find_all(exp.Identifier):
        identifier.set("quoted", True)


def _foreign_key(expression: exp.Expression) -> bool:
    if isinstance(expression, (exp.ForeignKey, exp.Reference)):
        return True
    if isinstance(expression, (exp.Constraint, exp.ColumnConstraint)):
        return any(_foreign_key(child) for child in expression.iter_expressions())
    return False
