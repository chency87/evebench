from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Mapping, Protocol, TypeVar, runtime_checkable

from evebench.domain.models import (
    CheckOptions,
    CheckResult,
    CheckerDescriptor,
    EquivalenceCase,
    ExecutionStatus,
    Verdict,
)


class AdapterInputError(ValueError):
    def __init__(
        self,
        status: ExecutionStatus,
        reason_code: str,
        message: str,
    ) -> None:
        self.status = status
        self.reason_code = reason_code
        super().__init__(message)


class AdapterLifecycleError(RuntimeError):
    """A backend lifecycle failure that can be returned as a normal result."""

    def __init__(
        self,
        status: ExecutionStatus,
        reason_code: str,
        message: str,
        *,
        raw_output: str | None = None,
    ) -> None:
        self.status = status
        self.reason_code = reason_code
        self.raw_output = raw_output
        super().__init__(message)


PreparedInput = TypeVar("PreparedInput")
NativeOutput = TypeVar("NativeOutput")


@runtime_checkable
class BackendAdapter(Protocol[PreparedInput, NativeOutput]):
    """Synchronous worker-side adapter lifecycle."""

    @property
    def descriptor(self) -> CheckerDescriptor: ...

    def preprocess(
        self,
        case: EquivalenceCase,
        options: CheckOptions,
    ) -> PreparedInput: ...

    def run(self, prepared: PreparedInput) -> NativeOutput: ...

    def postprocess(
        self,
        native: NativeOutput,
        *,
        started_at: datetime | None = None,
    ) -> CheckResult: ...


def execute_lifecycle(
    adapter: BackendAdapter[PreparedInput, NativeOutput],
    case: EquivalenceCase,
    options: CheckOptions,
) -> CheckResult:
    """Execute preprocess, run, and postprocess as one adapter attempt."""
    started = datetime.now(UTC)
    phase = "preprocess"
    try:
        prepared = adapter.preprocess(case, options)
        phase = "run"
        native = adapter.run(prepared)
        phase = "postprocess"
        return adapter.postprocess(native, started_at=started)
    except AdapterInputError as exc:
        return error_result(
            adapter.descriptor,
            started,
            exc.status,
            reason_code=exc.reason_code,
            message=str(exc),
        )
    except AdapterLifecycleError as exc:
        return error_result(
            adapter.descriptor,
            started,
            exc.status,
            reason_code=exc.reason_code,
            message=str(exc),
            raw_output=exc.raw_output,
        )
    except subprocess.TimeoutExpired:
        return error_result(
            adapter.descriptor,
            started,
            ExecutionStatus.TIMEOUT,
            reason_code="backend_timeout",
            message=(
                f"{adapter.descriptor.display_name} exceeded its backend deadline"
            ),
        )
    except Exception as exc:
        reason_code = {
            "preprocess": "preprocess_error",
            "run": "backend_error",
            "postprocess": "postprocess_error",
        }[phase]
        return error_result(
            adapter.descriptor,
            started,
            ExecutionStatus.FAILED,
            reason_code=reason_code,
            message=f"{type(exc).__name__}: {exc}",
        )


def normalize_adapter_input(
    case: EquivalenceCase,
    options: CheckOptions,
    descriptor: CheckerDescriptor,
) -> EquivalenceCase:
    """Normalize validated inputs and check backend capabilities."""
    if not descriptor.available:
        raise AdapterInputError(
            ExecutionStatus.UNSUPPORTED,
            "backend_unavailable",
            descriptor.unavailable_reason
            or f"{descriptor.display_name} is unavailable",
        )
    dialect = case.dialect.strip().lower()
    semantics = case.semantics.strip().lower()
    if semantics not in descriptor.supported_semantics:
        raise AdapterInputError(
            ExecutionStatus.UNSUPPORTED,
            "unsupported_semantics",
            f"{descriptor.display_name} does not support semantics {case.semantics!r}",
        )
    query_left = case.query_left.strip()
    query_right = case.query_right.strip()
    schema_ddl = case.schema_ddl.strip()
    if case.parameters:
        raise AdapterInputError(
            ExecutionStatus.UNSUPPORTED,
            "unsupported_parameters",
            f"{descriptor.display_name} does not support bound query parameters",
        )
    if options.memory_limit_mb is not None:
        raise AdapterInputError(
            ExecutionStatus.UNSUPPORTED,
            "unsupported_memory_limit",
            "memory_limit_mb is not enforced by the current executor",
        )
    return replace(
        case,
        query_left=query_left,
        query_right=query_right,
        schema_ddl=schema_ddl,
        dialect=dialect,
        semantics=semantics,
    )


def reject_unknown_options(
    options: Mapping[str, object],
    *,
    allowed: set[str],
    display_name: str,
) -> None:
    unknown = sorted(set(options) - allowed)
    if unknown:
        raise AdapterInputError(
            ExecutionStatus.INVALID_INPUT,
            "invalid_backend_options",
            f"Unknown {display_name} options: {', '.join(unknown)}",
        )


def error_result(
    descriptor: CheckerDescriptor,
    started_at: datetime,
    status: ExecutionStatus,
    *,
    reason_code: str,
    message: str,
    raw_output: str | None = None,
    finished_at: datetime | None = None,
) -> CheckResult:
    finished_at = finished_at or datetime.now(UTC)
    return CheckResult(
        checker=descriptor.checker,
        execution_status=status,
        verdict=Verdict.UNSUPPORTED if status == ExecutionStatus.UNSUPPORTED else None,
        duration_ms=duration_ms(started_at, finished_at),
        started_at=started_at,
        finished_at=finished_at,
        reason_code=reason_code,
        message=message,
        raw_output=raw_output,
    )


def duration_ms(started_at: datetime, finished_at: datetime) -> int:
    return max(0, int((finished_at - started_at).total_seconds() * 1000))


def parse_last_json_object(raw: str) -> dict[str, Any]:
    """Parse a backend's final non-empty stdout line as a JSON object."""
    line = next(
        (line for line in reversed(raw.splitlines()) if line.strip()),
        None,
    )
    if line is None:
        raise ValueError("empty stdout")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value
