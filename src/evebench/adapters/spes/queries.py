"""Render source queries for spes's SQL parser."""

import sqlglot


def compile_query(sql: str, dialect: str) -> str:
    read = {"postgresql": "postgres", "mariadb": "mysql"}.get(dialect, dialect)
    return ";\n".join(sqlglot.transpile(sql, read=read, write="mysql"))
