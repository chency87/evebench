"""Render source queries for qed's SQL parser."""

import sqlglot
from sqlglot import exp


def compile_query(sql: str, dialect: str) -> str:
    read = {"postgresql": "postgres", "mariadb": "mysql"}.get(dialect, dialect)
    statements = [statement for statement in sqlglot.parse(sql, read=read) if statement]
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise ValueError("QED requires exactly one SELECT query per side")
    for identifier in statements[0].find_all(exp.Identifier):
        identifier.set("quoted", True)
    # QED configures Calcite for double-quoted identifiers. The generic SQLGlot
    # renderer preserves that form, while the MySQL renderer emits backticks
    # that QED's parser rejects.
    return statements[0].sql()
