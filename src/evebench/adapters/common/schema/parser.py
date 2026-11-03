from __future__ import annotations

import re
from copy import deepcopy
from functools import lru_cache

import sqlglot
from sqlglot import exp

from .models import SchemaCatalog


def parse_schema(ddl: str, dialect: str) -> SchemaCatalog:
    return deepcopy(_parse_schema(ddl, dialect))


@lru_cache(maxsize=128)
def _parse_schema(ddl: str, dialect: str) -> SchemaCatalog:
    read = {"postgresql": "postgres", "mariadb": "mysql"}.get(dialect, dialect)
    statements = tuple(
        statement for statement in sqlglot.parse(ddl, read=read) if statement is not None
    )
    # SQLGlot attaches comments to nearby expressions. Drop them from the
    # catalog so disabled DDL never appears in emitted backend schemas.
    for statement in statements:
        for expression in statement.walk():
            expression.comments = None
    tables: list[exp.Create] = []
    alters: list[exp.AlterTable] = []
    functions: list[exp.Expression] = []
    other_statements: list[exp.Expression] = []
    for statement in statements:
        if (
            isinstance(statement, exp.Create)
            and statement.args.get("kind") == "TABLE"
            and isinstance(statement.this, exp.Schema)
        ):
            tables.append(statement)
            continue
        if isinstance(statement, exp.AlterTable):
            alters.append(statement)
        elif (
            isinstance(statement, exp.Create)
            and statement.args.get("kind") == "FUNCTION"
        ) or (
            isinstance(statement, exp.Command)
            and re.match(
                r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\b",
                statement.sql(),
                re.I,
            )
        ):
            functions.append(statement)
        else:
            other_statements.append(statement)
    return SchemaCatalog(
        dialect,
        statements,
        tuple(tables),
        tuple(alters),
        tuple(functions),
        tuple(other_statements),
    )
