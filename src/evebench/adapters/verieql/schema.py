from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from evebench.adapters.common.schema import SchemaCatalog, parse_schema
from sqlglot import exp


@dataclass(frozen=True, slots=True)
class VeriEQLSchema:
    tables: dict[str, dict[str, str]]
    constraints: list[dict[str, Any]]


def parse_verieql_ddl(schema_ddl: str, dialect: str) -> VeriEQLSchema:
    """Encode table DDL, including separate ADD CONSTRAINT primary/foreign keys.

    Unencodable but parseable DDL is passed to VeriEQL for an unsupported verdict.
    """
    return compile_verieql_schema(parse_schema(schema_ddl, dialect))


def compile_verieql_schema(catalog: SchemaCatalog) -> VeriEQLSchema:
    """Convert an ordered source schema catalog to VeriEQL's native input."""
    tables: dict[str, dict[str, str]] = {}
    constraints: list[dict[str, Any]] = []
    primary_keys = _primary_keys(catalog)

    for statement in catalog.statements:
        if isinstance(statement, exp.AlterTable):
            table_name = _table_name(statement.this)
            actions = statement.args.get("actions") or []
            if table_name not in tables:
                raise ValueError(f"ALTER TABLE refers to unknown table: {table_name}")
            if not actions or not all(isinstance(action, exp.AddConstraint) for action in actions):
                constraints.append(_unsupported(statement))
                continue
            for action in actions:
                for item in action.expressions:
                    constraints.extend(
                        _encode_table_constraint(table_name, item, primary_keys)
                    )
            continue
        if not (
            isinstance(statement, exp.Create)
            and statement.args.get("kind") == "TABLE"
            and isinstance(statement.this, exp.Schema)
        ):
            constraints.append(_unsupported(statement))
            continue
        schema = statement.this

        table_name = _table_name(schema.this)
        columns: dict[str, str] = {}
        for item in schema.expressions:
            if isinstance(item, exp.ColumnDef):
                column_name = item.name.upper()
                columns[column_name] = _column_type(item)
                constraints.extend(
                    _column_constraints(
                        table_name,
                        column_name,
                        item,
                        primary_keys,
                    )
                )
            elif isinstance(item, (exp.PrimaryKey, exp.ForeignKey, exp.Constraint)):
                constraints.extend(
                    _encode_table_constraint(table_name, item, primary_keys)
                )
            elif isinstance(item, exp.UniqueColumnConstraint):
                constraints.extend(_unique_constraint(table_name, item))
            elif isinstance(item, exp.CheckColumnConstraint):
                constraints.append(_check_constraint(table_name, item))
            else:
                constraints.append(_unsupported(item))
        tables[table_name] = columns

    # SQL dumps commonly repeat constraints (for example, two identical inline
    # REFERENCES clauses). Reasserting the same Z3 fact is unnecessary.
    unique_constraints: list[dict[str, Any]] = []
    for constraint in constraints:
        if constraint not in unique_constraints:
            unique_constraints.append(constraint)

    return VeriEQLSchema(tables=tables, constraints=unique_constraints)


def _encode_table_constraint(
    table_name: str,
    item: exp.Expression,
    primary_keys: dict[str, tuple[str, ...]],
) -> list[dict[str, Any]]:
    if isinstance(item, exp.Constraint):
        result: list[dict[str, Any]] = []
        for constraint in item.expressions:
            if isinstance(constraint, exp.PrimaryKey):
                result.append(_primary_constraint(table_name, constraint.expressions))
            elif isinstance(constraint, exp.UniqueColumnConstraint):
                result.extend(_unique_constraint(table_name, constraint))
            elif isinstance(constraint, exp.ForeignKey):
                result.extend(
                    _table_constraints(table_name, constraint, primary_keys)
                )
            elif isinstance(constraint, exp.CheckColumnConstraint):
                result.append(_check_constraint(table_name, constraint))
            else:
                return [_unsupported(item)]
        return result
    if isinstance(item, exp.PrimaryKey):
        return [_primary_constraint(table_name, item.expressions)]
    if isinstance(item, exp.UniqueColumnConstraint):
        return _unique_constraint(table_name, item)
    if isinstance(item, exp.CheckColumnConstraint):
        return [_check_constraint(table_name, item)]
    return _table_constraints(table_name, item, primary_keys)


def _column_constraints(
    table_name: str,
    column_name: str,
    column: exp.ColumnDef,
    primary_keys: dict[str, tuple[str, ...]],
) -> list[dict[str, Any]]:
    constraints: list[dict[str, Any]] = []
    for constraint in column.args.get("constraints") or []:
        kind = constraint.args.get("kind")
        if isinstance(kind, exp.NotNullColumnConstraint) and not kind.args.get(
            "allow_null"
        ):
            constraints.append({"not_null": _attribute(table_name, column_name)})
        elif isinstance(kind, exp.NotNullColumnConstraint):
            # An explicit NULL declaration permits nulls; it is not NOT NULL.
            continue
        elif isinstance(kind, exp.PrimaryKeyColumnConstraint):
            constraints.append(_primary_constraint(table_name, [column]))
        elif isinstance(kind, exp.UniqueColumnConstraint):
            constraints.append(_unique_columns(table_name, [column]))
        elif isinstance(kind, exp.CheckColumnConstraint):
            constraints.append(_check_constraint(table_name, kind))
        elif isinstance(kind, exp.Reference):
            constraints.extend(
                _foreign_constraints(
                    table_name,
                    [column_name],
                    kind,
                    primary_keys,
                )
            )
        elif isinstance(
            kind,
            (exp.DefaultColumnConstraint, exp.AutoIncrementColumnConstraint),
        ):
            # Defaults and generated values govern future INSERT operations;
            # they do not restrict the static database instances compared by
            # VeriEQL.
            continue
        else:
            constraints.append(_unsupported(constraint))
    return constraints


def _table_constraints(
    table_name: str,
    expression: exp.Expression,
    primary_keys: dict[str, tuple[str, ...]],
) -> list[dict[str, Any]]:
    foreign_keys: list[exp.ForeignKey] = []
    if isinstance(expression, exp.ForeignKey):
        foreign_keys.append(expression)
    elif isinstance(expression, exp.Constraint):
        foreign_keys.extend(expression.find_all(exp.ForeignKey))

    if not foreign_keys:
        return [_unsupported(expression)]
    constraints: list[dict[str, Any]] = []
    for foreign_key in foreign_keys:
        reference = foreign_key.args.get("reference")
        if isinstance(reference, exp.Reference):
            constraints.extend(
                _foreign_constraints(
                    table_name,
                    [identifier.name for identifier in foreign_key.expressions],
                    reference,
                    primary_keys,
                )
            )
        else:
            constraints.append(_unsupported(foreign_key))
    return constraints


def _foreign_constraints(
    source_table: str,
    source_columns: list[str],
    reference: exp.Reference,
    primary_keys: dict[str, tuple[str, ...]],
) -> list[dict[str, Any]]:
    target = reference.this
    if isinstance(target, exp.Schema):
        target_table = _table_name(target.this)
        target_columns = [identifier.name for identifier in target.expressions]
    elif isinstance(target, exp.Table):
        target_table = _table_name(target)
        target_columns = list(primary_keys.get(target_table, ()))
        if not target_columns:
            return [_unsupported(reference)]
    else:
        return [_unsupported(reference)]
    if len(source_columns) != len(target_columns):
        return [_unsupported(reference)]
    if len(source_columns) != 1:
        return [_unsupported(reference)]
    return [
        {
            "foreign": [
                _attribute(source_table, source_column),
                _attribute(target_table, target_column),
            ]
        }
        for source_column, target_column in zip(source_columns, target_columns)
    ]


def _primary_keys(catalog: SchemaCatalog) -> dict[str, tuple[str, ...]]:
    """Collect primary keys before resolving implicit REFERENCES targets."""
    keys: dict[str, tuple[str, ...]] = {}
    for statement in catalog.statements:
        if (
            isinstance(statement, exp.Create)
            and statement.args.get("kind") == "TABLE"
            and isinstance(statement.this, exp.Schema)
        ):
            table_name = _table_name(statement.this.this)
            for item in statement.this.expressions:
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
                elif isinstance(item, exp.Constraint):
                    primary_key = next(
                        (
                            constraint
                            for constraint in item.expressions
                            if isinstance(constraint, exp.PrimaryKey)
                        ),
                        None,
                    )
                    if primary_key is not None:
                        keys[table_name] = tuple(
                            identifier.name.upper()
                            for identifier in primary_key.expressions
                        )
        elif isinstance(statement, exp.AlterTable):
            table_name = _table_name(statement.this)
            for primary_key in statement.find_all(exp.PrimaryKey):
                keys[table_name] = tuple(
                    identifier.name.upper() for identifier in primary_key.expressions
                )
    return keys


def _primary_constraint(
    table_name: str,
    columns: list[exp.Expression],
) -> dict[str, list[dict[str, str]]]:
    return {
        "primary": [_attribute(table_name, column.name) for column in columns]
    }


def _unique_constraint(
    table_name: str,
    constraint: exp.UniqueColumnConstraint,
) -> list[dict[str, Any]]:
    columns = constraint.this
    if not isinstance(columns, exp.Schema) or not columns.expressions:
        return [_unsupported(constraint)]
    return [_unique_columns(table_name, columns.expressions)]


def _unique_columns(
    table_name: str,
    columns: list[exp.Expression],
) -> dict[str, list[dict[str, str]]]:
    return {"unique": [_attribute(table_name, column.name) for column in columns]}


def _check_constraint(table_name: str, constraint: exp.CheckColumnConstraint) -> dict[str, Any]:
    return _check_expression(table_name, constraint.this)


def _check_expression(table_name: str, expression: exp.Expression) -> Any:
    """Translate DDL expressions to VeriEQL's native constraint vocabulary."""
    if isinstance(expression, exp.Paren):
        return _check_expression(table_name, expression.this)
    if isinstance(expression, exp.Column):
        return _attribute(expression.table or table_name, expression.name)
    if isinstance(expression, exp.Literal):
        if expression.is_string:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", expression.this):
                return {"date": expression.this}
            return {"literal": expression.this.upper()}
        return int(expression.this) if expression.is_int else float(expression.this)
    if isinstance(expression, exp.Neg) and isinstance(expression.this, exp.Literal):
        return -_check_expression(table_name, expression.this)
    if isinstance(expression, exp.Not) and isinstance(expression.this, exp.Is):
        predicate = expression.this
        if isinstance(predicate.expression, exp.Null):
            return {"not_null": _check_expression(table_name, predicate.this)}
    if isinstance(expression, exp.Binary):
        return {
            expression.key: [
                _check_expression(table_name, expression.this),
                _check_expression(table_name, expression.expression),
            ]
        }
    if isinstance(expression, exp.Between):
        return {
            "between": [
                _check_expression(table_name, expression.this),
                _check_expression(table_name, expression.args["low"]),
                _check_expression(table_name, expression.args["high"]),
            ]
        }
    if isinstance(expression, exp.In) and not expression.args.get("query"):
        return {
            "in": [
                _check_expression(table_name, expression.this),
                [_check_expression(table_name, item) for item in expression.expressions],
            ]
        }
    return _unsupported(expression)


def _column_type(column: exp.ColumnDef) -> str:
    kind = column.args.get("kind")
    if not isinstance(kind, exp.DataType):
        raise ValueError("Column has no declared type")
    if kind.this == exp.DataType.Type.TEXT:
        return "VARCHAR"
    return kind.sql(dialect="mysql")


def _table_name(table: exp.Expression) -> str:
    if isinstance(table, exp.Table) and table.name:
        return table.name.upper()
    raise ValueError("CREATE TABLE is missing a table name")


def _attribute(table: str, column: str) -> dict[str, str]:
    return {"value": f"{table.upper()}__{column.upper()}"}


def _unsupported(expression: exp.Expression) -> dict[str, str]:
    # VeriEQL rejects unknown constraint operators in add_constraints().
    # This keeps the decision in the backend without silently dropping DDL.
    return {"unsupported_schema": expression.sql()}
