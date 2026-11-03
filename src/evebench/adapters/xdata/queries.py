"""Render source queries for XData's line-oriented input files."""

import sqlglot
from sqlglot import exp


def compile_query(sql: str, dialect: str) -> str:
    read = {"postgresql": "postgres", "mariadb": "mysql"}.get(dialect, dialect)
    statements = [statement for statement in sqlglot.parse(sql, read=read) if statement]
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise ValueError("XData requires exactly one SELECT query per side")
    # XData reads one `id|type|query` entry per line and executes the queries
    # on PostgreSQL, so comments are dropped and the query is kept on one line.
    rendered = statements[0].sql(dialect="postgres", comments=False)
    if "\n" in rendered or "\r" in rendered:
        raise ValueError("XData cannot read queries containing line breaks")
    return rendered
