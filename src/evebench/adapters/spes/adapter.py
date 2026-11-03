from __future__ import annotations

from .config import SPESConfig

import os
import subprocess
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
)


@dataclass(frozen=True, slots=True)
class SPESInput:
    query_left: str
    query_right: str
    schema_ddl: str
    timeout_seconds: float


def spes_descriptor(config: SPESConfig) -> CheckerDescriptor:
    jar_path = Path(config.jar_path)
    available = jar_path.is_file()
    return CheckerDescriptor(
        checker=CheckerRef("spes", config.backend_version, config.adapter_version),
        display_name="SPES",
        supported_dialects=("postgresql", "mysql", "sqlite"),
        supported_semantics=("bag",),
        execution_mode="process_pool",
        available=available,
        unavailable_reason=None if available else f"JAR not found: {jar_path}",
    )


class SPESAdapter:
    """SPES adapter backed by its executable fat JAR."""

    def __init__(self, config: SPESConfig) -> None:
        self._config = config
        self.descriptor = spes_descriptor(config)

    def preprocess(
        self,
        case: EquivalenceCase,
        options: CheckOptions,
    ) -> SPESInput:
        normalized = prepare_case(
            case,
            options,
            self.descriptor,
        )
        return SPESInput(
            query_left=normalized.query_left,
            query_right=normalized.query_right,
            schema_ddl=normalized.schema_ddl,
            timeout_seconds=max(0.001, options.timeout_ms * 0.95 / 1000),
        )

    def run(self, prepared: SPESInput) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        current_library_path = environment.get("LD_LIBRARY_PATH")
        environment["LD_LIBRARY_PATH"] = (
            self._config.library_path
            if not current_library_path
            else self._config.library_path + os.pathsep + current_library_path
        )
        command = [
            self._config.java_command,
            f"-Djava.library.path={self._config.library_path}",
            "-jar",
            self._config.jar_path,
            "-sql1",
            prepared.query_left,
            "-sql2",
            prepared.query_right,
            "-schema",
            prepared.schema_ddl,
        ]
        return subprocess.run(
            command,
            cwd=self._config.working_directory,
            env=environment,
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
            message = (
                completed.stderr.strip()
                or f"SPES exited with code {completed.returncode}"
            )
            raise AdapterLifecycleError(
                ExecutionStatus.FAILED,
                "backend_error",
                message,
                raw_output=raw or None,
            )

        try:
            native = parse_last_json_object(raw)
        except ValueError as exc:
            raise AdapterLifecycleError(
                ExecutionStatus.FAILED,
                "unparseable_output",
                f"Invalid SPES output: {exc}",
                raw_output=raw or None,
            ) from exc

        decision = native.get("decision")
        verdict = {
            "true": Verdict.EQUIVALENT,
            "false": Verdict.UNKNOWN,
            "unknown": Verdict.UNSUPPORTED,
        }.get(decision)
        if verdict is None:
            raise AdapterLifecycleError(
                ExecutionStatus.FAILED,
                "unparseable_output",
                f"Unknown SPES decision: {decision!r}",
                raw_output=raw,
            )

        finished = datetime.now(UTC)
        return CheckResult(
            checker=self.descriptor.checker,
            execution_status=(
                ExecutionStatus.UNSUPPORTED
                if verdict == Verdict.UNSUPPORTED
                else ExecutionStatus.COMPLETED
            ),
            verdict=verdict,
            duration_ms=duration_ms(started, finished),
            started_at=started,
            finished_at=finished,
            reason_code=(
                "equivalence_not_proven" if decision == "false"
                else "backend_unsupported" if decision == "unknown"
                else None
            ),
            message=(
                str(native.get("reason"))
                if verdict == Verdict.UNSUPPORTED and native.get("reason")
                else None
            ),
            raw_output=raw,
        )
