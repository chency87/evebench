from __future__ import annotations

from sqlalchemy.engine import make_url


def validate_connection_string(
    connection_string: str,
    dialect: str,
) -> str | None:
    try:
        url = make_url(connection_string)
        backend = url.get_backend_name()
    except Exception:
        return "ParSEval connection_string is not a valid SQLAlchemy URL"

    expected_backend = {"postgres": "postgresql", "mariadb": "mysql"}.get(
        dialect.lower(), dialect.lower()
    )
    if backend != expected_backend:
        return f"Connection backend {backend!r} does not match dialect {dialect!r}"
    return None


def redact_connection(text_value: str, connection_string: str) -> str:
    try:
        redacted = make_url(connection_string).render_as_string(hide_password=True)
    except Exception:
        redacted = "[redacted connection string]"
    return text_value.replace(connection_string, redacted)
