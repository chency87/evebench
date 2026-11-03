from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from evebench.adapters.registry import CheckerRegistry
from evebench.domain.models import (
    CheckOptions,
    CheckResult,
    EquivalenceCase,
    ExecutionStatus,
)
from evebench.domain.ports import EquivalenceChecker
from evebench.observability import get_logger, log_context

logger = get_logger(__name__)


class UnknownCheckerError(ValueError):
    def __init__(self, checker_ids: list[str]) -> None:
        self.checker_ids = checker_ids
        super().__init__(f"unknown checker(s): {', '.join(checker_ids)}")


class CheckService:
    def __init__(
        self,
        registry: CheckerRegistry,
    ) -> None:
        self.registry = registry

    async def check(
        self,
        case: EquivalenceCase,
        options: CheckOptions,
        checker_ids: list[str] | None = None,
    ) -> list[CheckResult]:
        selected_ids = checker_ids or [
            checker.descriptor.checker.checker_id
            for checker in self.registry.all()
            if checker.descriptor.available
        ]
        selected_ids = list(dict.fromkeys(selected_ids))
        unknown = [checker_id for checker_id in selected_ids if checker_id not in self.registry.ids()]
        if unknown:
            logger.warning(
                "unknown checker requested",
                extra={
                    "event": "check.rejected",
                    "unknown_checker_ids": unknown,
                },
            )
            raise UnknownCheckerError(unknown)
        checkers = [self.registry.get(checker_id) for checker_id in selected_ids]
        logger.debug(
            "check started",
            extra={
                "event": "check.started",
                "checker_ids": selected_ids,
                "dialect": case.dialect,
                "semantics": case.semantics,
                "timeout_ms": options.timeout_ms,
            },
        )
        return list(
            await asyncio.gather(
                *(self._run_one(checker, case, options) for checker in checkers)
            )
        )

    async def _run_one(
        self,
        checker: EquivalenceChecker,
        case: EquivalenceCase,
        options: CheckOptions,
    ) -> CheckResult:
        checker_id = checker.descriptor.checker.checker_id
        with log_context(case_id=case.case_id, checker_id=checker_id):
            started = datetime.now(UTC)
            try:
                result = await checker.check(case, options)
            except Exception as exc:
                finished = datetime.now(UTC)
                logger.exception(
                    "checker raised an unhandled exception",
                    extra={"event": "checker.exception"},
                )
                result = CheckResult(
                    checker=checker.descriptor.checker,
                    execution_status=ExecutionStatus.FAILED,
                    verdict=None,
                    duration_ms=max(
                        0, int((finished - started).total_seconds() * 1000)
                    ),
                    started_at=started,
                    finished_at=finished,
                    reason_code="adapter_error",
                    message=f"{type(exc).__name__}: {exc}",
                )
            log = (
                logger.warning
                if result.execution_status
                in {ExecutionStatus.FAILED, ExecutionStatus.TIMEOUT}
                else logger.debug
            )
            log(
                "checker completed",
                extra={
                    "event": "checker.completed",
                    "execution_status": result.execution_status.value,
                    "verdict": result.verdict.value if result.verdict else None,
                    "duration_ms": result.duration_ms,
                    "reason_code": result.reason_code,
                },
            )
            return result
