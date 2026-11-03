from __future__ import annotations

from .config import VeriEQLConfig

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evebench.adapters.core import (
    AdapterInputError,
    AdapterLifecycleError,
    duration_ms,
    normalize_adapter_input,
    parse_last_json_object,
    reject_unknown_options,
)
from evebench.adapters.verieql.schema import parse_verieql_ddl
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
class VeriEQLInput:
    query_left: str
    query_right: str
    dialect: str
    schema: dict[str, dict[str, str]]
    constraints: list[dict[str, Any]]
    bound_size: int
    timeout_seconds: float


def verieql_descriptor(config: VeriEQLConfig) -> CheckerDescriptor:
    root = Path(config.root_path)
    available = (root / "environment.py").is_file()
    return CheckerDescriptor(
        checker=CheckerRef("verieql", config.backend_version, config.adapter_version),
        display_name="VeriEQL",
        supported_dialects=(
            "mysql",
            "mariadb",
            "postgresql",
            "oracle",
        ),
        supported_semantics=("bag",),
        execution_mode="process_pool",
        available=available,
        unavailable_reason=(None if available else f"VeriEQL source not found: {root}"),
    )


class VeriEQLAdapter:
    """Isolated subprocess adapter for VeriEQL's bounded verifier."""

    def __init__(self, config: VeriEQLConfig) -> None:
        self._config = config
        self.descriptor = verieql_descriptor(config)

    def preprocess(
        self,
        case: EquivalenceCase,
        options: CheckOptions,
    ) -> VeriEQLInput:
        normalized = normalize_adapter_input(case, options, self.descriptor)
        backend_options = options.for_backend("verieql")
        reject_unknown_options(
            backend_options,
            allowed={"bound_size"},
            display_name="VeriEQL",
        )
        bound_size = backend_options.get(
            "bound_size",
            self._config.default_bound_size,
        )
        if (
            isinstance(bound_size, bool)
            or not isinstance(bound_size, int)
            or not 1 <= bound_size <= 100
        ):
            raise AdapterInputError(
                ExecutionStatus.INVALID_INPUT,
                "invalid_bound_size",
                "VeriEQL bound_size must be an integer from 1 to 100",
            )
        try:
            parsed_schema = parse_verieql_ddl(
                normalized.schema_ddl,
                normalized.dialect,
            )
        except AdapterInputError:
            raise
        except ValueError as exc:
            raise AdapterInputError(
                ExecutionStatus.INVALID_INPUT,
                "invalid_schema",
                str(exc),
            ) from exc
        return VeriEQLInput(
            query_left=normalized.query_left,
            query_right=normalized.query_right,
            dialect=normalized.dialect,
            schema=parsed_schema.tables,
            constraints=parsed_schema.constraints,
            bound_size=bound_size,
            timeout_seconds=max(0.001, options.timeout_ms * 0.95 / 1000),
        )

    def run(self, prepared: VeriEQLInput) -> subprocess.CompletedProcess[str]:
        payload = {
            "root": self._config.root_path,
            "z3_library_path": (
                self._config.z3_library_path
                or str(Path(self._config.root_path).parent / "SQLSolver" / "lib")
            ),
            "sql1": prepared.query_left,
            "sql2": prepared.query_right,
            "schema": prepared.schema,
            "constraints": prepared.constraints,
            "bound_size": prepared.bound_size,
            "dialect": prepared.dialect,
        }
        environment = os.environ.copy()
        python_path = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            self._config.root_path
            if not python_path
            else self._config.root_path + os.pathsep + python_path
        )
        return subprocess.run(
            [
                self._config.python_command,
                "-m",
                "evebench.adapters.verieql.runner",
            ],
            input=json.dumps(payload, separators=(",", ":")),
            cwd=self._config.root_path,
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
            raise AdapterLifecycleError(
                ExecutionStatus.FAILED,
                "backend_error",
                (
                    completed.stderr.strip()
                    or f"VeriEQL exited with code {completed.returncode}"
                ),
                raw_output=raw or None,
            )

        try:
            native = parse_last_json_object(raw)
        except ValueError as exc:
            raise AdapterLifecycleError(
                ExecutionStatus.FAILED,
                "unparseable_output",
                f"Invalid VeriEQL output: {exc}",
                raw_output=raw or None,
            ) from exc
        state = native.get("state")
        status = {
            "equivalent": ExecutionStatus.COMPLETED,
            "not_equivalent": ExecutionStatus.COMPLETED,
            "unknown": ExecutionStatus.COMPLETED,
            "syntax_error": ExecutionStatus.INVALID_INPUT,
            "unsupported": ExecutionStatus.UNSUPPORTED,
            "error": ExecutionStatus.FAILED,
        }.get(state)
        verdict = {
            "equivalent": Verdict.EQUIVALENT,
            "not_equivalent": Verdict.NOT_EQUIVALENT,
            "unknown": Verdict.UNKNOWN,
            "unsupported": Verdict.UNSUPPORTED,
        }.get(state)
        if status is None:
            raise AdapterLifecycleError(
                ExecutionStatus.FAILED,
                "unparseable_output",
                f"Unknown VeriEQL state: {state!r}",
                raw_output=raw,
            )

        finished = datetime.now(UTC)
        reason_codes = {
            "unknown": "backend_unknown",
            "syntax_error": "syntax_error",
            "unsupported": "unsupported_feature",
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
                "bound_size": native.get("bound_size"),
                "traversing_time_s": native.get("traversing_time_s"),
                "solving_time_s": native.get("solving_time_s"),
            },
            raw_output=raw,
        )
