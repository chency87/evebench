"""Schema and case conversion for sqlsolver."""

from __future__ import annotations

from dataclasses import replace

from sqlglot import exp

from evebench.adapters.common.schema import SchemaCatalog, parse_schema
from evebench.adapters.core import normalize_adapter_input, reject_unknown_options
from evebench.domain.models import CheckOptions, CheckerDescriptor, EquivalenceCase

from .queries import compile_query


def prepare_case(
    case: EquivalenceCase, options: CheckOptions, descriptor: CheckerDescriptor
) -> EquivalenceCase:
    normalized = normalize_adapter_input(case, options, descriptor)
    reject_unknown_options(
        options.for_backend(descriptor.checker.checker_id),
        allowed=set(),
        display_name=descriptor.display_name,
    )
    schema = compile_schema(normalized.schema_ddl, normalized.dialect)

    return replace(
        normalized,
        schema_ddl=schema,
        query_left=compile_query(normalized.query_left, normalized.dialect),
        query_right=compile_query(normalized.query_right, normalized.dialect),
    )


def compile_schema(schema_ddl: str, dialect: str) -> str:
    """Render table definitions for SQLSolver's MySQL schema parser.

    PostgreSQL's ``ALTER TABLE ONLY`` is not MySQL syntax. Fold additions into
    their preceding CREATE TABLE and retain actions that cannot be folded.
    SQLSolver's schema parser also requires a length on VARCHAR, so represent
    unbounded PostgreSQL character varying as TEXT.
    """
    catalog = parse_schema(schema_ddl, dialect)
    rendered: list[exp.Expression] = []
    tables: dict[str, exp.Create] = {}
    primary_keys = _primary_keys(catalog)
    for statement in catalog.statements:
        if statement in catalog.tables:
            table = statement.copy()
            tables[table.this.this.name.lower()] = table
            rendered.append(table)
        elif isinstance(statement, exp.AlterTable):
            table = tables.get(statement.this.name.lower())
            remaining = []
            for action in statement.args.get("actions") or []:
                if table is not None and isinstance(action, exp.ColumnDef):
                    table.this.append("expressions", action.copy())
                elif table is not None and isinstance(action, exp.AddConstraint):
                    for constraint in action.expressions:
                        table.this.append("expressions", constraint.copy())
                else:
                    remaining.append(action.copy())
            if remaining:
                alter = statement.copy()
                alter.set("actions", remaining)
                alter.set("only", False)
                rendered.append(alter)
        else:
            rendered.append(statement.copy())
    for table in tables.values():
        _normalize_table_constraints(table, primary_keys)
    for statement in rendered:
        for data_type in statement.find_all(exp.DataType):
            if data_type.this == exp.DataType.Type.VARCHAR and not data_type.expressions:
                data_type.set("this", exp.DataType.Type.TEXT)
    return ";\n".join(statement.sql(dialect="mysql") for statement in rendered) + ";"


def _normalize_table_constraints(
    table: exp.Create,
    primary_keys: dict[str, tuple[str, ...]],
) -> None:
    """Use constraint forms accepted by SQLSolver's MySQL schema parser."""
    added_foreign_keys: list[exp.ForeignKey] = []
    for column in table.this.expressions:
        if not isinstance(column, exp.ColumnDef):
            continue
        kept: list[exp.ColumnConstraint] = []
        for constraint in column.args.get("constraints") or []:
            kind = constraint.args.get("kind")
            if isinstance(kind, exp.Reference):
                reference = kind.copy()
                if isinstance(reference.this, exp.Table):
                    target_table = reference.this.name.upper()
                    target_columns = primary_keys.get(target_table, ())
                    if target_columns:
                        reference.set(
                            "this",
                            exp.Schema(
                                this=reference.this.copy(),
                                expressions=[
                                    exp.to_identifier(target_column)
                                    for target_column in target_columns
                                ],
                            ),
                        )
                added_foreign_keys.append(
                    exp.ForeignKey(
                        expressions=[column.this.copy()],
                        reference=reference,
                    )
                )
                continue
            normalized = constraint.copy()
            # SQLSolver rejects names attached to inline column constraints,
            # such as `id INT CONSTRAINT id_pk PRIMARY KEY`.
            normalized.set("this", None)
            kept.append(normalized)
        column.set("constraints", kept)

    for foreign_key in added_foreign_keys:
        if foreign_key not in table.this.expressions:
            table.this.append("expressions", foreign_key)


def _primary_keys(catalog: SchemaCatalog) -> dict[str, tuple[str, ...]]:
    keys: dict[str, tuple[str, ...]] = {}
    for table in catalog.tables:
        table_name = table.this.this.name.upper()
        for item in table.this.expressions:
            if isinstance(item, exp.ColumnDef) and any(
                isinstance(
                    constraint.args.get("kind"),
                    exp.PrimaryKeyColumnConstraint,
                )
                for constraint in item.args.get("constraints") or []
            ):
                keys[table_name] = (item.name.upper(),)
            elif isinstance(item, exp.PrimaryKey):
                keys[table_name] = tuple(
                    identifier.name.upper() for identifier in item.expressions
                )
    for alter in catalog.alters:
        for primary_key in alter.find_all(exp.PrimaryKey):
            keys[alter.this.name.upper()] = tuple(
                identifier.name.upper() for identifier in primary_key.expressions
            )
    return keys
