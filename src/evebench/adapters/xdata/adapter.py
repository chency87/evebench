from __future__ import annotations

from .config import XDataConfig

import json
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from evebench.adapters.core import (
    AdapterLifecycleError,
    duration_ms,
    parse_last_json_object,
)
from .schema import prepare_case
from evebench.domain.models import (
    CheckOptions,
    CheckResult,
    CheckerDescriptor,
    CheckerRef,
    EquivalenceCase,
    ExecutionStatus,
    Verdict,
    json_safe,
)


@dataclass(frozen=True, slots=True)
class XDataInput:
    query_left: str
    query_right: str
    schema_ddl: str
    connection_string: str
    bidirectional: bool
    timeout_seconds: float


def xdata_descriptor(config: XDataConfig) -> CheckerDescriptor:
    xdata_root = Path(config.root_path) / "xdata"
    missing = next(
        (
            path
            for path in (xdata_root / "jars" / "xdata.jar", xdata_root / "z3" / "z3")
            if not path.is_file()
        ),
        None,
    )
    return CheckerDescriptor(
        checker=CheckerRef("xdata", config.backend_version, config.adapter_version),
        display_name="XData",
        supported_dialects=("postgresql", "mysql", "sqlite"),
        supported_semantics=("bag",),
        execution_mode="process_pool",
        available=missing is None,
        unavailable_reason=None if missing is None else f"XData file not found: {missing}",
    )


class XDataAdapter:
    """Isolated subprocess adapter for XData's mutation-killing data generator."""

    def __init__(self, config: XDataConfig) -> None:
        self._config = config
        self.descriptor = xdata_descriptor(config)

    def preprocess(
        self,
        case: EquivalenceCase,
        options: CheckOptions,
    ) -> XDataInput:
        normalized, connection_string, bidirectional = prepare_case(
            case,
            options,
            self.descriptor,
            default_connection_string=self._config.default_connection_string,
        )
        return XDataInput(
            query_left=normalized.query_left,
            query_right=normalized.query_right,
            schema_ddl=normalized.schema_ddl,
            connection_string=connection_string,
            bidirectional=bidirectional,
            timeout_seconds=max(0.001, options.timeout_ms * 0.95 / 1000),
        )

    def run(self, prepared: XDataInput) -> subprocess.CompletedProcess[str]:
        payload = {
            "root": self._config.root_path,
            "java": self._config.java_command,
            "lock_path": self._config.lock_path,
            "connection_string": prepared.connection_string,
            "sql1": prepared.query_left,
            "sql2": prepared.query_right,
            "schema": prepared.schema_ddl,
            "bidirectional": prepared.bidirectional,
            # An absolute deadline inside the subprocess timeout leaves the
            # runner time to stop XData's JVM and remove its scratch files.
            "deadline": time.time() + prepared.timeout_seconds * 0.9,
        }
        return subprocess.run(
            [
                self._config.python_command,
                "-m",
                "evebench.adapters.xdata.runner",
            ],
            input=json.dumps(payload, separators=(",", ":")),
            capture_output=True,
            text=True,
            timeout=prepared.timeout_seconds,
            check=False,
        )

    def postprocess(
        self,
        completed: subprocess.CompletedProcess[str],
        *,
        started_at: datetime | None = None,
    ) -> CheckResult:
        started = started_at or datetime.now(UTC)
        raw = completed.stdout.strip()
        if completed.returncode != 0:
            raise AdapterLifecycleError(
                ExecutionStatus.FAILED,
                "backend_error",
                (
                    completed.stderr.strip()
                    or f"XData runner exited with code {completed.returncode}"
                ),
                raw_output=raw or None,
            )

        try:
            native = parse_last_json_object(raw)
        except ValueError as exc:
            raise AdapterLifecycleError(
                ExecutionStatus.FAILED,
                "unparseable_output",
                f"Invalid XData output: {exc}",
                raw_output=raw or None,
            ) from exc
        state = native.get("state")
        status = {
            "not_equivalent": ExecutionStatus.COMPLETED,
            "unknown": ExecutionStatus.COMPLETED,
            "invalid_input": ExecutionStatus.INVALID_INPUT,
            "unsupported": ExecutionStatus.UNSUPPORTED,
            "timeout": ExecutionStatus.TIMEOUT,
            "error": ExecutionStatus.FAILED,
        }.get(state)
        if status is None:
            raise AdapterLifecycleError(
                ExecutionStatus.FAILED,
                "unparseable_output",
                f"Unknown XData state: {state!r}",
                raw_output=raw,
            )
        verdict = {
            "not_equivalent": Verdict.NOT_EQUIVALENT,
            # XData only generates test data, so surviving every dataset is
            # never a proof of equivalence.
            "unknown": Verdict.UNKNOWN,
            "unsupported": Verdict.UNSUPPORTED,
        }.get(state)

        finished = datetime.now(UTC)
        reason_codes = {
            "unknown": "no_counterexample_found",
            "invalid_input": "query_error",
            "unsupported": "unsupported_feature",
            "timeout": "backend_timeout",
            "error": "backend_error",
        }
        return CheckResult(
            checker=self.descriptor.checker,
            execution_status=status,
            verdict=verdict,
            duration_ms=duration_ms(started, finished),
            started_at=started,
            finished_at=finished,
            reason_code=reason_codes.get(state),
            message=(str(native["reason"]) if native.get("reason") else None),
            witness=(
                json_safe(native["counterexample"])
                if state == "not_equivalent"
                and isinstance(native.get("counterexample"), dict)
                else None
            ),
            metrics={
                **{
                    f"xdata_{direction}": str(xdata_state)
                    for direction, xdata_state in (
                        native.get("xdata_states") or {}
                    ).items()
                },
                "dataset_count": native.get("dataset_count"),
                "lock_wait_s": native.get("lock_wait_s"),
                "xdata_time_s": native.get("xdata_time_s"),
            },
            raw_output=raw,
        )
