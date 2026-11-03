from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import time
from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_LOGGER_NAME = "evebench"
_CONTEXT_FIELDS = ("request_id", "run_id", "case_id", "checker_id")
_context: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "evebench_log_context",
    default={},
)
_STANDARD_RECORD_FIELDS = frozenset(
    logging.makeLogRecord({}).__dict__
) | frozenset({"message", "asctime"})
_SENSITIVE_KEY_PARTS = (
    "connection_string",
    "password",
    "secret",
    "token",
    "query_left",
    "query_right",
    "schema_ddl",
    "raw_output",
    "witness",
)


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    file_path: Path | None = Path("logs/evebench.jsonl")
    file_level: str = "DEBUG"
    console_level: str = "INFO"
    console_enabled: bool = True
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 5

    @classmethod
    def from_env(cls) -> LoggingConfig:
        raw_path = os.environ.get("EVEBENCH_LOG_FILE", "logs/evebench.jsonl")
        return cls(
            file_path=Path(raw_path) if raw_path.strip() else None,
            file_level=os.environ.get("EVEBENCH_LOG_LEVEL", "DEBUG").upper(),
            console_level=os.environ.get(
                "EVEBENCH_CONSOLE_LOG_LEVEL", "INFO"
            ).upper(),
            console_enabled=_environment_bool(
                "EVEBENCH_LOG_CONSOLE",
                default=True,
            ),
            max_bytes=_environment_int(
                "EVEBENCH_LOG_MAX_BYTES",
                default=10 * 1024 * 1024,
                minimum=1,
            ),
            backup_count=_environment_int(
                "EVEBENCH_LOG_BACKUP_COUNT",
                default=5,
                minimum=0,
            ),
        )


def configure_logging(config: LoggingConfig | None = None) -> logging.Logger:
    """Configure EveBench's isolated logger without altering library loggers."""
    selected = config or LoggingConfig.from_env()
    file_level = _logging_level(selected.file_level)
    console_level = _logging_level(selected.console_level)
    logger = logging.getLogger(_LOGGER_NAME)
    logger.propagate = False

    for handler in tuple(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    levels: list[int] = []
    context_filter = _ContextFilter()
    if selected.console_enabled:
        console = logging.StreamHandler()
        console.setLevel(console_level)
        console.addFilter(context_filter)
        console.setFormatter(_ConsoleFormatter())
        logger.addHandler(console)
        levels.append(console_level)

    if selected.file_path is not None:
        selected.file_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            selected.file_path,
            maxBytes=selected.max_bytes,
            backupCount=selected.backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(file_level)
        file_handler.addFilter(context_filter)
        file_handler.setFormatter(_JSONFormatter())
        logger.addHandler(file_handler)
        levels.append(file_level)

    logger.setLevel(min(levels, default=logging.CRITICAL + 1))
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    if not name:
        return logging.getLogger(_LOGGER_NAME)
    if name == _LOGGER_NAME or name.startswith(f"{_LOGGER_NAME}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{_LOGGER_NAME}.{name}")


@contextlib.contextmanager
def log_context(**values: object) -> Generator[None, None, None]:
    """Attach safe correlation values to all logs in the current async context."""
    updated = dict(_context.get())
    updated.update(
        {
            key: _safe_context_value(value)
            for key, value in values.items()
            if key in _CONTEXT_FIELDS and value is not None
        }
    )
    token = _context.set(updated)
    try:
        yield
    finally:
        _context.reset(token)


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        values = _context.get()
        for field in _CONTEXT_FIELDS:
            if field in values:
                setattr(record, field, values[field])
            elif not hasattr(record, field):
                setattr(record, field, None)
        return True


class _ConsoleFormatter(logging.Formatter):
    converter = time.gmtime

    def format(self, record: logging.LogRecord) -> str:
        timestamp = self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ")
        context = " ".join(
            f"{field}={getattr(record, field)}"
            for field in _CONTEXT_FIELDS
            if getattr(record, field, None)
        )
        suffix = f" [{context}]" if context else ""
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        return f"{timestamp} {record.levelname:<8} {record.name}: {message}{suffix}"


class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "process_id": record.process,
            "thread_name": record.threadName,
        }
        for field in _CONTEXT_FIELDS:
            value = getattr(record, field, None)
            if value:
                payload[field] = value
        for key, value in record.__dict__.items():
            if (
                key not in _STANDARD_RECORD_FIELDS
                and key not in _CONTEXT_FIELDS
                and not key.startswith("_")
            ):
                payload[key] = (
                    "[REDACTED]" if _is_sensitive_key(key) else _json_safe(value)
                )
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower()
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _safe_context_value(value: object) -> str:
    raw = str(value)
    return "".join(
        character if character.isprintable() else "?"
        for character in raw[:200]
    )


def _json_safe(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]" if _is_sensitive_key(str(key)) else _json_safe(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _logging_level(value: str) -> int:
    level = logging.getLevelNamesMapping().get(value.upper())
    if level is None:
        raise ValueError(f"invalid logging level: {value}")
    return level


def _environment_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _environment_int(name: str, *, default: int, minimum: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value
