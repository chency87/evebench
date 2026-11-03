from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from evebench.application import CheckService
from evebench.domain.models import (
    JSONValue,
    CheckOptions,
    EquivalenceCase,
)
from evebench.observability import get_logger, log_context

from .loader import read_dataset
from .query_transform import strip_shared_order_by_limit as strip_shared_clauses
from .store import ResultStore

ProgressCallback = Callable[[int, int, int], None]
logger = get_logger(__name__)


class BenchmarkRunner:
    def __init__(self, service: CheckService, store: ResultStore) -> None:
        self.service = service
        self.store = store

    async def run_csv(
        self,
        dataset_path: str | Path,
        *,
        dataset_name: str | None = None,
        checker_ids: list[str] | None = None,
        timeout_ms: int = 30_000,
        semantics: str = "bag",
        seed: int | None = 1,
        strip_shared_order_by_limit: bool = False,
        backend_options: dict[str, dict[str, JSONValue]] | None = None,
        execution_options: dict[str, JSONValue] | None = None,
        limit: int | None = None,
        run_id: int | None = None,
        progress: ProgressCallback | None = None,
    ) -> int:
        path = Path(dataset_path).resolve()
        rows = read_dataset(path)
        if limit is not None:
            if limit < 1:
                raise ValueError("limit must be positive")
            rows = rows[:limit]
        selected = checker_ids or [
            checker.descriptor.checker.checker_id
            for checker in self.service.registry.all()
            if checker.descriptor.available
        ]
        selected = list(dict.fromkeys(selected))
        if semantics not in {"bag", "set"}:
            raise ValueError("semantics must be 'bag' or 'set'")
        configured_options = backend_options or {}

        configuration: dict[str, object] = {
            "checker_ids": selected,
            "semantics": semantics,
            "seed": seed,
            "strip_shared_order_by_limit": strip_shared_order_by_limit,
            "backend_options": configured_options,
            "execution_options": execution_options or {},
        }

        # Resume skips saved case IDs; reject edited datasets with the same IDs.
        dataset_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        if run_id is None:
            run_id = self.store.create_run(
                dataset=dataset_name or path.stem,
                dataset_path=str(path),
                dataset_sha256=dataset_sha256,
                total_cases=len(rows),
                timeout_ms=timeout_ms,
                configuration=configuration,
                started_at=datetime.now(UTC).isoformat(),
            )
            logger.info(
                "benchmark run created",
                extra={
                    "event": "benchmark.created",
                    "run_id": run_id,
                    "dataset": dataset_name or path.stem,
                    "dataset_path": str(path),
                    "total_cases": len(rows),
                    "checker_ids": selected,
                    "timeout_ms": timeout_ms,
                },
            )
        else:
            self.store.validate_run(
                run_id,
                dataset_sha256=dataset_sha256,
                total_cases=len(rows),
                timeout_ms=timeout_ms,
                configuration=configuration,
            )
            logger.info(
                "benchmark run resumed",
                extra={
                    "event": "benchmark.resumed",
                    "run_id": run_id,
                    "total_cases": len(rows),
                    "checker_ids": selected,
                },
            )

        terminal_status = "completed"
        with log_context(run_id=run_id):
            try:
                for position, row in enumerate(rows, start=1):
                    case_id = f"{row['dbid']}:{row['index']}"
                    with log_context(case_id=case_id):
                        completed = self.store.completed_checker_ids(
                            run_id, case_id
                        )
                        pending = [
                            item for item in selected if item not in completed
                        ]
                        if pending:
                            query_left, query_right = row["q1"], row["q2"]
                            if strip_shared_order_by_limit:
                                query_left, query_right = strip_shared_clauses(
                                    query_left, query_right, row["dialect"]
                                )
                            base_case = EquivalenceCase(
                                query_left=query_left,
                                query_right=query_right,
                                schema_ddl=row["schema_ddl"],
                                dialect=row["dialect"],
                                semantics=semantics,
                                case_id=case_id,
                            )
                            checked = await self.service.check(
                                base_case,
                                CheckOptions(
                                    timeout_ms=timeout_ms,
                                    seed=seed,
                                    backend_options=configured_options,
                                ),
                                pending,
                            )
                            results = [
                                (
                                    self.service.registry.get(
                                        checker_id
                                    ).descriptor.display_name,
                                    result,
                                )
                                for checker_id, result in zip(
                                    pending, checked, strict=True
                                )
                            ]
                            self.store.save_results(
                                run_id=run_id,
                                case=base_case,
                                dataset_row=position,
                                db_id=row.get("dbid"),
                                ground_truth=row.get("ground_truth"),
                                results=results,
                            )
                            logger.debug(
                                "benchmark case persisted",
                                extra={
                                    "event": "benchmark.case_persisted",
                                    "dataset_row": position,
                                    "result_count": len(results),
                                },
                            )
                        if progress:
                            progress(position, len(rows), run_id)
            except (KeyboardInterrupt, asyncio.CancelledError):
                terminal_status = "interrupted"
                logger.warning(
                    "benchmark run interrupted",
                    extra={"event": "benchmark.interrupted"},
                )
                raise
            except Exception:
                terminal_status = "failed"
                logger.exception(
                    "benchmark run failed",
                    extra={"event": "benchmark.failed"},
                )
                raise
            finally:
                self.store.finish_run(
                    run_id,
                    status=terminal_status,
                    finished_at=datetime.now(UTC).isoformat(),
                )
                logger.info(
                    "benchmark run finished",
                    extra={
                        "event": "benchmark.finished",
                        "status": terminal_status,
                    },
                )
        return run_id
