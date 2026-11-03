from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

JSONValue = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]


class ExecutionStatus(StrEnum):
    COMPLETED = "completed"
    TIMEOUT = "timeout"
    UNSUPPORTED = "unsupported"
    INVALID_INPUT = "invalid_input"
    FAILED = "failed"


class Verdict(StrEnum):
    EQUIVALENT = "equivalent"
    NOT_EQUIVALENT = "not_equivalent"
    UNKNOWN = "unknown"
    UNSUPPORTED = "unsupported"
    TIMEOUT = "timeout"


@dataclass(frozen=True, slots=True)
class EquivalenceCase:
    query_left: str
    query_right: str
    schema_ddl: str
    dialect: str
    semantics: str
    case_id: str | None = None
    parameters: dict[str, JSONValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CheckOptions:
    timeout_ms: int = 30_000
    memory_limit_mb: int | None = None
    seed: int | None = None
    backend_options: dict[str, dict[str, JSONValue]] = field(default_factory=dict)

    def for_backend(self, checker_id: str) -> dict[str, JSONValue]:
        return self.backend_options.get(checker_id, {})


@dataclass(frozen=True, slots=True)
class CheckerRef:
    checker_id: str
    backend_version: str
    adapter_version: str


@dataclass(frozen=True, slots=True)
class CheckerDescriptor:
    checker: CheckerRef
    display_name: str
    supported_dialects: tuple[str, ...]
    supported_semantics: tuple[str, ...]
    execution_mode: str
    available: bool = True
    unavailable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class CheckResult:
    checker: CheckerRef
    execution_status: ExecutionStatus
    verdict: Verdict | None
    duration_ms: int
    started_at: datetime
    finished_at: datetime
    reason_code: str | None = None
    message: str | None = None
    witness: dict[str, JSONValue] | None = None
    metrics: dict[str, int | float | str | bool | None] = field(default_factory=dict)
    raw_output: str | None = None


def json_safe(value: Any) -> JSONValue:
    """Convert backend values into the deliberately small JSON domain."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    return str(value)
