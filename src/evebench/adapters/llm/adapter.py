from __future__ import annotations

from .config import LLMConfig

import json
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import sqlglot
from sqlglot import expressions

from evebench.adapters.core import (
    AdapterInputError,
    AdapterLifecycleError,
    duration_ms,
    normalize_adapter_input,
    reject_unknown_options,
)
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


_REQUIRED_RESPONSE_FIELDS = (
    "explanation",
    "counterexample_dsp",
    "counterexample",
)


@dataclass(frozen=True, slots=True)
class LLMInput:
    url: str
    headers: dict[str, str]
    payload: dict[str, Any]
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class LLMOutput:
    response: dict[str, Any]
    content: str


def llm_descriptor(config: LLMConfig) -> CheckerDescriptor:
    return CheckerDescriptor(
        checker=CheckerRef("llm", config.model, config.adapter_version),
        display_name=f"LLM ({config.model})",
        supported_dialects=("sqlite", "mysql", "postgresql"),
        supported_semantics=("bag", "set"),
        execution_mode="process_pool",
        available=bool(config.model.strip() and config.base_url.strip()),
        unavailable_reason="LLM model and base URL must be configured",
    )


class LLMAdapter:
    """Counterexample checker backed by an OpenAI-compatible HTTP endpoint."""

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self.descriptor = llm_descriptor(config)

    def preprocess(
        self,
        case: EquivalenceCase,
        options: CheckOptions,
    ) -> LLMInput:
        normalized = normalize_adapter_input(case, options, self.descriptor)
        backend_options = dict(options.for_backend("llm"))
        reject_unknown_options(
            backend_options,
            allowed={
                "max_completion_tokens",
                "prompt",
                "reasoning_effort",
                "temperature",
            },
            display_name="LLM",
        )

        prompt_template = backend_options.pop("prompt", self._config.prompt_template)
        if not isinstance(prompt_template, str) or not prompt_template.strip():
            raise AdapterInputError(
                ExecutionStatus.INVALID_INPUT,
                "invalid_backend_options",
                "LLM prompt must be a non-empty string",
            )
        prompt = _render_prompt(
            prompt_template,
            q1=normalized.query_left,
            q2=normalized.query_right,
            schema=normalized.schema_ddl,
        )

        reasoning_effort = backend_options.pop(
            "reasoning_effort", self._config.reasoning_effort
        )
        if reasoning_effort is not None and (
            not isinstance(reasoning_effort, str) or not reasoning_effort.strip()
        ):
            raise AdapterInputError(
                ExecutionStatus.INVALID_INPUT,
                "invalid_backend_options",
                "LLM reasoning_effort must be a non-empty string or null",
            )

        max_tokens = backend_options.pop(
            "max_completion_tokens", self._config.max_completion_tokens
        )
        if (
            isinstance(max_tokens, bool)
            or not isinstance(max_tokens, int)
            or max_tokens < 1
        ):
            raise AdapterInputError(
                ExecutionStatus.INVALID_INPUT,
                "invalid_backend_options",
                "LLM max_completion_tokens must be a positive integer",
            )

        temperature = backend_options.pop("temperature", self._config.temperature)
        if temperature is not None and (
            isinstance(temperature, bool)
            or not isinstance(temperature, (int, float))
            or not 0 <= temperature <= 2
        ):
            raise AdapterInputError(
                ExecutionStatus.INVALID_INPUT,
                "invalid_backend_options",
                "LLM temperature must be a number between 0 and 2",
            )

        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        }
        if reasoning_effort is None:
            # Older and third-party Chat Completions servers commonly expose
            # max_tokens for ordinary generation.
            payload["max_tokens"] = max_tokens
            if temperature is not None:
                payload["temperature"] = temperature
        else:
            # OpenAI reasoning models account for visible and reasoning tokens
            # together and use max_completion_tokens.
            payload["max_completion_tokens"] = max_tokens
            payload["reasoning_effort"] = reasoning_effort.strip()

        headers = {"Content-Type": "application/json"}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"
        return LLMInput(
            url=f"{self._config.base_url.rstrip('/')}/chat/completions",
            headers=headers,
            payload=payload,
            timeout_seconds=options.timeout_ms / 1_000,
        )

    def run(self, prepared: LLMInput) -> LLMOutput:
        request = urllib.request.Request(
            prepared.url,
            data=json.dumps(prepared.payload).encode("utf-8"),
            headers=prepared.headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=prepared.timeout_seconds
            ) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise AdapterLifecycleError(
                ExecutionStatus.FAILED,
                "llm_http_error",
                f"LLM endpoint returned HTTP {exc.code}",
                raw_output=raw,
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise AdapterLifecycleError(
                ExecutionStatus.TIMEOUT,
                "backend_timeout",
                "LLM endpoint exceeded its request deadline",
            ) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise AdapterLifecycleError(
                    ExecutionStatus.TIMEOUT,
                    "backend_timeout",
                    "LLM endpoint exceeded its request deadline",
                ) from exc
            raise AdapterLifecycleError(
                ExecutionStatus.FAILED,
                "llm_connection_error",
                f"Could not reach LLM endpoint: {exc.reason}",
            ) from exc

        try:
            response_data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AdapterLifecycleError(
                ExecutionStatus.FAILED,
                "unparseable_output",
                "LLM endpoint did not return JSON",
                raw_output=raw,
            ) from exc
        if not isinstance(response_data, dict):
            raise AdapterLifecycleError(
                ExecutionStatus.FAILED,
                "unparseable_output",
                "LLM endpoint response must be a JSON object",
                raw_output=raw,
            )
        return LLMOutput(
            response=response_data,
            content=extract_completion_text(response_data),
        )

    def postprocess(
        self,
        output: LLMOutput,
        *,
        started_at: datetime | None = None,
    ) -> CheckResult:
        started = started_at or datetime.now(UTC)
        finished = datetime.now(UTC)
        raw = json.dumps(output.response, separators=(",", ":"), default=str)
        try:
            counterexample = parse_counterexample_response(output.content)
        except ValueError as exc:
            raise AdapterLifecycleError(
                ExecutionStatus.FAILED,
                "unparseable_output",
                str(exc),
                raw_output=raw,
            ) from exc

        usage = output.response.get("usage")
        metrics = (
            {
                str(key): json_safe(value)
                for key, value in usage.items()
                if value is None or isinstance(value, (bool, int, float, str))
            }
            if isinstance(usage, dict)
            else {}
        )
        return CheckResult(
            checker=self.descriptor.checker,
            execution_status=ExecutionStatus.COMPLETED,
            verdict=Verdict.NOT_EQUIVALENT,
            duration_ms=duration_ms(started, finished),
            started_at=started,
            finished_at=finished,
            message=counterexample["explanation"],
            witness=counterexample,
            metrics=metrics,
            raw_output=raw,
        )


def extract_completion_text(response: dict[str, Any]) -> str:
    """Extract final text from Chat Completions and Responses-like payloads."""
    output_text = response.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0]
        if isinstance(choice, dict):
            message = choice.get("message")
            if isinstance(message, dict):
                text = _text_from_content(message.get("content"))
                if text:
                    return text
                # Some reasoning-compatible providers place the complete
                # answer here when content is absent.
                for field in ("reasoning_content", "reasoning"):
                    reasoning = message.get(field)
                    if isinstance(reasoning, str) and reasoning.strip():
                        return reasoning
            text = choice.get("text")
            if isinstance(text, str) and text.strip():
                return text

    output = response.get("output")
    if isinstance(output, list):
        parts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            text = _text_from_content(item.get("content"))
            if text:
                parts.append(text)
        if parts:
            return "\n".join(parts)

    raise AdapterLifecycleError(
        ExecutionStatus.FAILED,
        "unparseable_output",
        "LLM response did not contain completion text",
        raw_output=json.dumps(response, separators=(",", ":"), default=str),
    )


def parse_counterexample_response(content: str) -> dict[str, str]:
    candidate = content.strip()
    fenced = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```", candidate, flags=re.DOTALL | re.IGNORECASE
    )
    if fenced:
        candidate = fenced.group(1)
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError("LLM completion is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("LLM completion must be a JSON object")
    if set(value) != set(_REQUIRED_RESPONSE_FIELDS):
        raise ValueError(
            "LLM completion must contain exactly: "
            + ", ".join(_REQUIRED_RESPONSE_FIELDS)
        )
    if not all(isinstance(value[field], str) for field in _REQUIRED_RESPONSE_FIELDS):
        raise ValueError("LLM completion fields must all be strings")
    if not value["explanation"].strip() or not value["counterexample_dsp"].strip():
        raise ValueError("LLM explanation and counterexample_dsp must be non-empty")
    _validate_insert_only(value["counterexample"])
    return {field: value[field] for field in _REQUIRED_RESPONSE_FIELDS}


def _render_prompt(template: str, *, q1: str, q2: str, schema: str) -> str:
    required = ("{q1}", "{q2}", "{schema}")
    missing = [placeholder for placeholder in required if placeholder not in template]
    if missing:
        raise AdapterInputError(
            ExecutionStatus.INVALID_INPUT,
            "invalid_backend_options",
            "LLM prompt is missing placeholder(s): " + ", ".join(missing),
        )
    return template.replace("{q1}", q1).replace("{q2}", q2).replace("{schema}", schema)


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts).strip()


def _validate_insert_only(sql: str) -> None:
    if not sql.strip():
        return
    try:
        statements = sqlglot.parse(sql)
    except sqlglot.errors.ParseError as exc:
        raise ValueError("LLM counterexample is not executable SQL") from exc
    if not statements or any(
        not isinstance(statement, expressions.Insert) for statement in statements
    ):
        raise ValueError("LLM counterexample may contain only INSERT statements")
