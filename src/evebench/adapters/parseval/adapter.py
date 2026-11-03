from __future__ import annotations

from .config import ParsevalConfig

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime

from parseval import (
    GenerationConfig,
    RunResult,
    Verdict as ParsevalVerdict,
)

from evebench.adapters.core import (
    AdapterInputError,
    AdapterLifecycleError,
    duration_ms,
    normalize_adapter_input,
    reject_unknown_options,
)
from evebench.adapters.parseval.connection import (
    redact_connection,
    validate_connection_string,
)
from evebench.adapters.parseval.disprove import disprove
from evebench.adapters.parseval.schema import compile_schema
from evebench.domain.models import (
    JSONValue,
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
class ParsevalInput:
    query_left: str
    query_right: str
    schema_ddl: str
    connection_string: str
    dialect: str
    semantics: str
    generation_config: GenerationConfig
    timeout_seconds: int


@dataclass(frozen=True, slots=True)
class ParsevalOutput:
    native: RunResult
    connection_string: str


GENERATION_OPTIONS = {
    "timeout_ms",
    "time_limit_s",
    "min_string_length",
    "speculate",
    "seed",
}


def parseval_descriptor(config: ParsevalConfig) -> CheckerDescriptor:
    return CheckerDescriptor(
        checker=CheckerRef("parseval", config.backend_version, config.adapter_version),
        display_name="ParSEval",
        supported_dialects=("sqlite", "mysql", "postgres", "postgresql"),
        supported_semantics=("bag", "set"),
        execution_mode="process_pool",
    )


class ParsevalAdapter:
    """Complete ParSEval adapter. Instances live only in pool workers."""

    def __init__(self, config: ParsevalConfig) -> None:
        self.descriptor = parseval_descriptor(config)

    def preprocess(
        self,
        case: EquivalenceCase,
        options: CheckOptions,
    ) -> ParsevalInput:
        normalized = normalize_adapter_input(case, options, self.descriptor)
        backend_options = dict(options.for_backend("parseval"))
        connection_string = backend_options.pop("connection_string", None)
        reject_unknown_options(
            backend_options,
            allowed=GENERATION_OPTIONS,
            display_name="ParSEval",
        )
        if not isinstance(connection_string, str) or not connection_string.strip():
            raise AdapterInputError(
                ExecutionStatus.INVALID_INPUT,
                "missing_connection_string",
                "ParSEval requires backend_options.parseval.connection_string",
            )
        connection_string = connection_string.strip()
        connection_error = validate_connection_string(
            connection_string,
            normalized.dialect,
        )
        if connection_error is not None:
            raise AdapterInputError(
                ExecutionStatus.INVALID_INPUT,
                "invalid_connection_string",
                connection_error,
            )
        if options.seed is not None:
            backend_options["seed"] = options.seed
        timeout_seconds = max(1, math.ceil(options.timeout_ms / 1000))
        backend_options.setdefault("time_limit_s", timeout_seconds)
        boolean_fields = {"speculate"}
        integer_fields = {"timeout_ms", "min_string_length", "seed"}
        for name in boolean_fields & backend_options.keys():
            if not isinstance(backend_options[name], bool):
                raise AdapterInputError(
                    ExecutionStatus.INVALID_INPUT,
                    "invalid_backend_options",
                    f"ParSEval {name} must be a boolean",
                )
        for name in integer_fields & backend_options.keys():
            value = backend_options[name]
            if isinstance(value, bool) or not isinstance(value, int):
                raise AdapterInputError(
                    ExecutionStatus.INVALID_INPUT,
                    "invalid_backend_options",
                    f"ParSEval {name} must be an integer",
                )
        time_limit_s = backend_options.get("time_limit_s")
        if (
            time_limit_s is not None
            and (
                isinstance(time_limit_s, bool)
                or not isinstance(time_limit_s, (int, float))
            )
        ):
            raise AdapterInputError(
                ExecutionStatus.INVALID_INPUT,
                "invalid_backend_options",
                "ParSEval time_limit_s must be a number or null",
            )
        try:
            generation_config = GenerationConfig(**backend_options)
        except (TypeError, ValueError) as exc:
            raise AdapterInputError(
                ExecutionStatus.INVALID_INPUT,
                "invalid_backend_options",
                str(exc),
            ) from exc
        dialect = "postgres" if normalized.dialect == "postgresql" else normalized.dialect
        return ParsevalInput(
            query_left=normalized.query_left,
            query_right=normalized.query_right,
            schema_ddl=compile_schema(normalized.schema_ddl, dialect),
            connection_string=connection_string,
            dialect=dialect,
            semantics=normalized.semantics,
            generation_config=generation_config,
            timeout_seconds=timeout_seconds,
        )

    def run(
        self,
        prepared: ParsevalInput,
    ) -> ParsevalOutput:
        try:
            native_result = disprove(
                prepared.query_left,
                prepared.query_right,
                prepared.schema_ddl,
                prepared.connection_string,
                prepared.dialect,
                config=prepared.generation_config,
                timeout_s=prepared.timeout_seconds,
                set_semantics=prepared.semantics == "set",
            )
        except Exception as exc:
            raise AdapterLifecycleError(
                ExecutionStatus.FAILED,
                "backend_error",
                redact_connection(
                    f"{type(exc).__name__}: {exc}",
                    prepared.connection_string,
                ),
            ) from exc
        return ParsevalOutput(
            native=native_result,
            connection_string=prepared.connection_string,
        )

    def postprocess(
        self,
        output: ParsevalOutput,
        *,
        started_at: datetime | None = None,
    ) -> CheckResult:
        started = started_at or datetime.now(UTC)
        result = output.native
        connection_string = output.connection_string
        reason = (
            redact_connection(result.reason, connection_string)
            if result.reason is not None
            else None
        )
        finished = datetime.now(UTC)
        duration = duration_ms(started, finished)
        counterexample = _serialize_instance(result.counterexample)
        native = {
            "verdict": result.verdict.value,
            "reason": result.reason,
            "counterexample": counterexample,
            "results": json_safe(result.results),
        }
        raw = redact_connection(
            json.dumps(native, default=str, separators=(",", ":")),
            connection_string,
        )
        native_verdict = result.verdict
        if native_verdict is ParsevalVerdict.SYNTAX_ERROR:
            raise AdapterLifecycleError(
                ExecutionStatus.INVALID_INPUT,
                "syntax_error",
                reason or "ParSEval returned a syntax error",
                raw_output=raw,
            )
        if native_verdict is ParsevalVerdict.TIMEOUT:
            raise AdapterLifecycleError(
                ExecutionStatus.TIMEOUT,
                "backend_timeout",
                reason or "ParSEval timed out",
                raw_output=raw,
            )

        verdict = {
            ParsevalVerdict.EQ: Verdict.EQUIVALENT,
            ParsevalVerdict.NEQ: Verdict.NOT_EQUIVALENT,
            ParsevalVerdict.UNKNOWN: Verdict.UNKNOWN,
        }.get(native_verdict)
        if verdict is None:
            raise AdapterLifecycleError(
                ExecutionStatus.FAILED,
                "unparseable_output",
                f"Unknown ParSEval verdict: {native_verdict.value!r}",
                raw_output=raw,
            )

        witness = None
        if verdict == Verdict.NOT_EQUIVALENT and result.results is not None:
            rows_left, rows_right = result.results
            witness = {
                "database": counterexample,
                "query_left_rows": json_safe(rows_left),
                "query_right_rows": json_safe(rows_right),
            }
        return CheckResult(
            checker=self.descriptor.checker,
            execution_status=ExecutionStatus.COMPLETED,
            verdict=verdict,
            duration_ms=duration,
            started_at=started,
            finished_at=finished,
            message=reason,
            witness=witness,
            metrics={
                "counterexample_rows": (
                    result.counterexample.row_count
                    if result.counterexample is not None
                    else None
                ),
            },
            raw_output=raw,
        )


def _serialize_instance(instance) -> dict[str, JSONValue] | None:
    if instance is None:
        return None
    return {
        table.name.qualified_name: json_safe(instance.rows(table.relation))
        for table in instance.catalog.tables()
        if instance.rows(table.relation)
    }
