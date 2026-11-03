from __future__ import annotations

from .config import SQLSolverConfig

import ctypes
import math
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from evebench.adapters.core import (
    AdapterLifecycleError,
    duration_ms,
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
class SQLSolverInput:
    query_left: str
    query_right: str
    schema_ddl: str
    timeout_seconds: int


def sqlsolver_descriptor(config: SQLSolverConfig) -> CheckerDescriptor:
    jar_path = Path(config.jar_path)
    available = jar_path.is_file()
    return CheckerDescriptor(
        checker=CheckerRef("sqlsolver", config.backend_version, config.adapter_version),
        display_name="SQLSolver",
        supported_dialects=("postgresql", "mysql", "sqlite"),
        supported_semantics=("bag",),
        execution_mode="process_pool",
        available=available,
        unavailable_reason=None if available else f"JAR not found: {jar_path}",
    )


class SQLSolverAdapter:
    """Complete embedded-JVM adapter. Instances live only in pool workers."""

    def __init__(self, config: SQLSolverConfig) -> None:
        self._config = config
        self.descriptor = sqlsolver_descriptor(config)

    def preprocess(
        self,
        case: EquivalenceCase,
        options: CheckOptions,
    ) -> SQLSolverInput:
        normalized = prepare_case(
            case,
            options,
            self.descriptor,
        )
        return SQLSolverInput(
            query_left=normalized.query_left,
            query_right=normalized.query_right,
            schema_ddl=normalized.schema_ddl,
            timeout_seconds=max(1, math.ceil(options.timeout_ms / 1000)),
        )

    def run(self, prepared: SQLSolverInput) -> str:
        import jpype

        if not jpype.isJVMStarted():
            library_path = Path(self._config.library_path)
            ctypes.CDLL(
                str(library_path / "libz3.so"),
                mode=ctypes.RTLD_GLOBAL,
            )
            ctypes.CDLL(
                str(library_path / "libz3java.so"),
                mode=ctypes.RTLD_GLOBAL,
            )
            jpype.startJVM(
                jpype.getDefaultJVMPath(),
                f"-Djava.library.path={self._config.library_path}",
                classpath=[self._config.jar_path],
                convertStrings=True,
            )

        verification = jpype.JClass("sqlsolver.api.entry.Verification")
        array_list = jpype.JClass("java.util.ArrayList")
        left = array_list()
        right = array_list()
        left.add(prepared.query_left)
        right.add(prepared.query_right)

        previous_directory = os.getcwd()
        try:
            os.chdir(self._config.working_directory)
            results = verification.verify(
                left,
                right,
                prepared.schema_ddl,
                jpype.JLong(prepared.timeout_seconds),
            )
        finally:
            os.chdir(previous_directory)
        result = results.get(0)
        return "UNKNOWN" if result is None else str(result)

    def postprocess(
        self,
        native: str,
        *,
        started_at: datetime | None = None,
    ) -> CheckResult:
        started = started_at or datetime.now(UTC)
        finished = datetime.now(UTC)
        duration = duration_ms(started, finished)
        verdict = {
            "EQ": Verdict.EQUIVALENT,
            # SQLSolver can report NEQ when its proof fails without producing a
            # counterexample; reason_code marks the verdict as unconfirmed.
            "NEQ": Verdict.NOT_EQUIVALENT,
            "UNKNOWN": Verdict.UNKNOWN,
            "TIMEOUT": None,
        }.get(native)
        if native not in {"EQ", "NEQ", "UNKNOWN", "TIMEOUT"}:
            raise AdapterLifecycleError(
                ExecutionStatus.FAILED,
                "unparseable_output",
                f"Unknown SQLSolver result: {native!r}",
                raw_output=native,
            )
        return CheckResult(
            checker=self.descriptor.checker,
            execution_status=(
                ExecutionStatus.TIMEOUT
                if native == "TIMEOUT"
                else ExecutionStatus.COMPLETED
            ),
            verdict=verdict,
            duration_ms=duration,
            started_at=started,
            finished_at=finished,
            reason_code=(
                "backend_timeout" if native == "TIMEOUT"
                else "inequivalence_not_confirmed" if native == "NEQ"
                else None
            ),
            raw_output=native,
        )
