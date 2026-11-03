from __future__ import annotations

from .config import QEDConfig

import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from evebench.adapters.core import AdapterLifecycleError, duration_ms
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
class QEDInput:
    sql: str
    timeout_seconds: float


def qed_descriptor(config: QEDConfig) -> CheckerDescriptor:
    reason = None
    if not Path(config.parser_jar_path).is_file():
        reason = f"QED parser JAR not found: {config.parser_jar_path}"
    else:
        for command in (config.prover_path, config.java_command, "z3", "cvc5"):
            if shutil.which(command) is None:
                reason = f"QED executable not found: {command}"
                break
    return CheckerDescriptor(
        checker=CheckerRef("qed", config.backend_version, config.adapter_version),
        display_name="QED",
        supported_dialects=("postgresql", "mysql", "sqlite"),
        supported_semantics=("bag",),
        execution_mode="process_pool",
        available=reason is None,
        unavailable_reason=reason,
    )


class QEDAdapter:
    """Run the QED SQL parser followed by the equivalence prover."""

    def __init__(self, config: QEDConfig) -> None:
        self._config = config
        self.descriptor = qed_descriptor(config)

    def preprocess(self, case: EquivalenceCase, options: CheckOptions) -> QEDInput:
        normalized = prepare_case(case, options, self.descriptor)
        return QEDInput(
            sql=(
                "\n".join(
                    text.rstrip().rstrip(";") + "\n;"
                    for text in (
                        normalized.schema_ddl,
                        normalized.query_left,
                        normalized.query_right,
                    )
                )
                + "\n"
            ),
            timeout_seconds=max(0.001, options.timeout_ms * 0.95 / 1000),
        )

    def run(self, prepared: QEDInput) -> dict:
        deadline = time.monotonic() + prepared.timeout_seconds
        output = {}

        def run_stage(stage: str, command: list[str]) -> None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, prepared.timeout_seconds)
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=remaining,
                check=False,
            )
            output[stage] = {
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "returncode": completed.returncode,
            }
            if completed.returncode != 0:
                raise AdapterLifecycleError(
                    ExecutionStatus.FAILED,
                    f"{stage}_error",
                    completed.stderr.strip()
                    or f"QED {stage} exited with code {completed.returncode}",
                    raw_output=json.dumps(output),
                )

        with tempfile.TemporaryDirectory(prefix="evebench-qed-") as directory:
            sql_path = Path(directory) / "case.sql"
            sql_path.write_text(prepared.sql, encoding="utf-8")
            run_stage(
                "parser",
                [
                    self._config.java_command,
                    "--enable-preview",
                    "--add-opens=java.base/java.lang.reflect=ALL-UNNAMED",
                    "-jar",
                    self._config.parser_jar_path,
                    str(sql_path),
                ],
            )
            # The parser catches exceptions and may exit zero without an output.
            parsed_path = sql_path.with_suffix(".json")
            if not parsed_path.is_file():
                raise AdapterLifecycleError(
                    ExecutionStatus.FAILED,
                    "parser_error",
                    output["parser"]["stderr"].strip()
                    or "QED parser did not produce parsed expressions",
                    raw_output=json.dumps(output),
                )
            run_stage("prover", [self._config.prover_path, str(parsed_path)])
            try:
                output["result"] = json.loads(
                    sql_path.with_suffix(".result").read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                raise AdapterLifecycleError(
                    ExecutionStatus.FAILED,
                    "unparseable_output",
                    f"Invalid QED result: {exc}",
                    raw_output=json.dumps(output),
                ) from exc
        return output

    def postprocess(
        self,
        native: dict,
        *,
        started_at: datetime | None = None,
    ) -> CheckResult:
        started = started_at or datetime.now(UTC)
        raw = json.dumps(native)
        stats = native.get("result")
        if not isinstance(stats, dict) or any(
            type(stats.get(key)) is not bool for key in ("provable", "panicked")
        ):
            raise AdapterLifecycleError(
                ExecutionStatus.FAILED,
                "unparseable_output",
                "QED result must contain boolean provable and panicked fields",
                raw_output=raw,
            )
        if stats["panicked"]:
            raise AdapterLifecycleError(
                ExecutionStatus.FAILED,
                "backend_error",
                "QED prover panicked or could not parse its input",
                raw_output=raw,
            )
        verdict = Verdict.EQUIVALENT if stats["provable"] else Verdict.UNKNOWN
        finished = datetime.now(UTC)
        return CheckResult(
            checker=self.descriptor.checker,
            execution_status=ExecutionStatus.COMPLETED,
            verdict=verdict,
            duration_ms=duration_ms(started, finished),
            started_at=started,
            finished_at=finished,
            reason_code="backend_unknown" if verdict == Verdict.UNKNOWN else None,
            message=(
                "QED could not prove equivalence"
                if verdict == Verdict.UNKNOWN
                else None
            ),
            raw_output=raw,
        )
