from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from evebench.adapters.core import error_result, execute_lifecycle
from evebench.domain.models import (
    CheckOptions,
    CheckResult,
    CheckerDescriptor,
    EquivalenceCase,
    ExecutionStatus,
)
from evebench.execution import (
    BackendProcessExpired,
    BackendProcessPool,
    BackendTaskTimeout,
)


def execute_adapter(
    adapter_id: str,
    adapter_config: Any,
    case: EquivalenceCase,
    options: CheckOptions,
) -> CheckResult:
    """Worker entry point: construct and run the complete selected adapter."""
    if adapter_id == "parseval":
        from evebench.adapters.parseval import ParsevalAdapter

        adapter = ParsevalAdapter(adapter_config)
    elif adapter_id == "llm":
        from evebench.adapters.llm import LLMAdapter

        adapter = LLMAdapter(adapter_config)
    elif adapter_id == "sqlsolver":
        from evebench.adapters.sqlsolver import SQLSolverAdapter

        adapter = SQLSolverAdapter(adapter_config)
    elif adapter_id == "spes":
        from evebench.adapters.spes import SPESAdapter

        adapter = SPESAdapter(adapter_config)
    elif adapter_id == "qed":
        from evebench.adapters.qed import QEDAdapter

        adapter = QEDAdapter(adapter_config)
    elif adapter_id == "verieql":
        from evebench.adapters.verieql import VeriEQLAdapter

        adapter = VeriEQLAdapter(adapter_config)
    elif adapter_id == "xdata":
        from evebench.adapters.xdata import XDataAdapter

        adapter = XDataAdapter(adapter_config)
    else:
        raise ValueError(f"unknown worker adapter: {adapter_id}")
    return execute_lifecycle(adapter, case, options)


@dataclass(frozen=True, slots=True)
class PooledChecker:
    """Lightweight API-process proxy for a worker-side backend adapter."""

    _descriptor: CheckerDescriptor
    _adapter_config: Any
    _pool: BackendProcessPool

    @property
    def descriptor(self) -> CheckerDescriptor:
        return self._descriptor

    async def check(
        self,
        case: EquivalenceCase,
        options: CheckOptions,
    ) -> CheckResult:
        started = datetime.now(UTC)
        try:
            return await self._pool.run(
                execute_adapter,
                args=(
                    self.descriptor.checker.checker_id,
                    self._adapter_config,
                    case,
                    options,
                ),
                timeout_ms=options.timeout_ms,
            )
        except BackendTaskTimeout:
            return error_result(
                self.descriptor,
                started,
                ExecutionStatus.TIMEOUT,
                reason_code="worker_timeout",
                message=(
                    f"{self.descriptor.display_name} exceeded the "
                    "hard wall-clock deadline"
                ),
            )
        except BackendProcessExpired as exc:
            return error_result(
                self.descriptor,
                started,
                ExecutionStatus.FAILED,
                reason_code="worker_expired",
                message=str(exc),
            )
