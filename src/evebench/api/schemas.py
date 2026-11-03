from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from evebench.domain.models import (
    CheckOptions,
    CheckResult,
    CheckerDescriptor,
    EquivalenceCase,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EquivalenceCaseRequest(StrictModel):
    case_id: str | None = Field(default=None, max_length=200)
    query_left: str = Field(min_length=1, max_length=200_000)
    query_right: str = Field(min_length=1, max_length=200_000)
    schema_ddl: str = Field(min_length=1, max_length=500_000)
    dialect: str = Field(min_length=1, max_length=50)
    semantics: Literal["bag", "set"] = "bag"
    parameters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("dialect")
    @classmethod
    def normalize_dialect(cls, value: str) -> str:
        return value.strip().lower()

    def to_domain(self) -> EquivalenceCase:
        return EquivalenceCase(
            case_id=self.case_id,
            query_left=self.query_left,
            query_right=self.query_right,
            schema_ddl=self.schema_ddl,
            dialect=self.dialect,
            semantics=self.semantics,
            parameters=self.parameters,
        )


class CheckOptionsRequest(StrictModel):
    timeout_ms: int = Field(default=30_000, ge=100, le=300_000)
    memory_limit_mb: int | None = Field(default=None, ge=64, le=65_536)
    seed: int | None = None
    backend_options: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("memory_limit_mb")
    @classmethod
    def reject_unavailable_memory_limit(cls, value: int | None) -> int | None:
        if value is not None:
            raise ValueError(
                "memory_limit_mb requires the future container executor and is not available"
            )
        return value

    def to_domain(self) -> CheckOptions:
        return CheckOptions(
            timeout_ms=self.timeout_ms,
            memory_limit_mb=self.memory_limit_mb,
            seed=self.seed,
            backend_options=self.backend_options,
        )


class CheckRequest(StrictModel):
    case: EquivalenceCaseRequest
    checker_ids: list[str] | None = Field(default=None, max_length=32)
    options: CheckOptionsRequest = Field(default_factory=CheckOptionsRequest)

    @field_validator("checker_ids")
    @classmethod
    def validate_checker_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and not value:
            raise ValueError("checker_ids must contain at least one checker")
        return value


class CheckerRefResponse(StrictModel):
    checker_id: str
    backend_version: str
    adapter_version: str


class CheckerDescriptorResponse(StrictModel):
    checker: CheckerRefResponse
    display_name: str
    supported_dialects: list[str]
    supported_semantics: list[str]
    execution_mode: str
    available: bool
    unavailable_reason: str | None

    @classmethod
    def from_domain(cls, descriptor: CheckerDescriptor) -> CheckerDescriptorResponse:
        return cls(
            checker=CheckerRefResponse(
                checker_id=descriptor.checker.checker_id,
                backend_version=descriptor.checker.backend_version,
                adapter_version=descriptor.checker.adapter_version,
            ),
            display_name=descriptor.display_name,
            supported_dialects=list(descriptor.supported_dialects),
            supported_semantics=list(descriptor.supported_semantics),
            execution_mode=descriptor.execution_mode,
            available=descriptor.available,
            unavailable_reason=descriptor.unavailable_reason,
        )


class CheckResultResponse(StrictModel):
    checker: CheckerRefResponse
    execution_status: str
    verdict: str | None
    duration_ms: int
    started_at: datetime
    finished_at: datetime
    reason_code: str | None
    message: str | None
    witness: dict[str, Any] | None
    metrics: dict[str, int | float | str | bool | None]
    raw_output: str | None

    @classmethod
    def from_domain(cls, result: CheckResult) -> CheckResultResponse:
        return cls(
            checker=CheckerRefResponse(
                checker_id=result.checker.checker_id,
                backend_version=result.checker.backend_version,
                adapter_version=result.checker.adapter_version,
            ),
            execution_status=result.execution_status.value,
            verdict=result.verdict.value if result.verdict else None,
            duration_ms=result.duration_ms,
            started_at=result.started_at,
            finished_at=result.finished_at,
            reason_code=result.reason_code,
            message=result.message,
            witness=result.witness,
            metrics=result.metrics,
            raw_output=result.raw_output,
        )


class CheckResponse(StrictModel):
    results: list[CheckResultResponse]
