"""Schema and case conversion for XData."""

from __future__ import annotations

from dataclasses import replace

from sqlalchemy.engine import make_url

from evebench.adapters.common.schema import parse_schema
from evebench.adapters.core import (
    AdapterInputError,
    normalize_adapter_input,
    reject_unknown_options,
)
from evebench.domain.models import (
    CheckOptions,
    CheckerDescriptor,
    EquivalenceCase,
    ExecutionStatus,
)

from .queries import compile_query


def prepare_case(
    case: EquivalenceCase,
    options: CheckOptions,
    descriptor: CheckerDescriptor,
    *,
    default_connection_string: str | None = None,
) -> tuple[EquivalenceCase, str, bool]:
    normalized = normalize_adapter_input(case, options, descriptor)
    backend_options = options.for_backend(descriptor.checker.checker_id)
    reject_unknown_options(
        backend_options,
        allowed={"connection_string", "bidirectional"},
        display_name=descriptor.display_name,
    )
    connection_string = backend_options.get(
        "connection_string", default_connection_string
    )
    if not isinstance(connection_string, str) or not connection_string.strip():
        raise AdapterInputError(
            ExecutionStatus.INVALID_INPUT,
            "missing_connection_string",
            "XData requires backend_options.xdata.connection_string "
            "or EVEBENCH_XDATA_CONNECTION_STRING",
        )
    connection_string = connection_string.strip()
    try:
        backend = make_url(connection_string).get_backend_name()
    except Exception:
        backend = None
    if backend != "postgresql":
        raise AdapterInputError(
            ExecutionStatus.INVALID_INPUT,
            "invalid_connection_string",
            "XData connection_string must be a PostgreSQL SQLAlchemy URL",
        )
    bidirectional = backend_options.get("bidirectional", True)
    if not isinstance(bidirectional, bool):
        raise AdapterInputError(
            ExecutionStatus.INVALID_INPUT,
            "invalid_backend_options",
            "XData bidirectional must be a boolean",
        )
    try:
        query_left = compile_query(normalized.query_left, normalized.dialect)
        query_right = compile_query(normalized.query_right, normalized.dialect)
    except ValueError as exc:
        raise AdapterInputError(
            ExecutionStatus.UNSUPPORTED,
            "unsupported_query",
            str(exc),
        ) from exc
    return (
        replace(
            normalized,
            schema_ddl=compile_schema(normalized.schema_ddl, normalized.dialect),
            query_left=query_left,
            query_right=query_right,
        ),
        connection_string,
        bidirectional,
    )


def compile_schema(schema_ddl: str, dialect: str) -> str:
    """Render one comment-free PostgreSQL statement per line.

    XData's DDL splitter mishandles lines that start with a block comment, and
    it loads each CREATE TABLE as a PostgreSQL temporary table.
    """
    catalog = parse_schema(schema_ddl, dialect)
    return "\n".join(
        statement.sql(dialect="postgres", comments=False) + ";"
        for statement in catalog.statements
    )
