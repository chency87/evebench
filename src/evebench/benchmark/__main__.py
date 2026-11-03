from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import replace
from pathlib import Path

from evebench.api.app import default_registry
from evebench.application import CheckService
from evebench.execution import BackendProcessPool
from evebench.observability import LoggingConfig, configure_logging, get_logger

from .config import BenchmarkConfig
from .runner import BenchmarkRunner
from .store import ResultStore

logger = get_logger(__name__)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run an EveBench CSV benchmark")
    parser.add_argument(
        "dataset",
        nargs="?",
        help="CSV dataset for an ad-hoc run",
    )
    parser.add_argument(
        "--config",
        help="benchmark TOML profile",
    )
    parser.add_argument("--database")
    parser.add_argument("--name")
    parser.add_argument("--timeout-ms", type=int)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--semantics", choices=("bag", "set"))
    parser.add_argument("--max-workers", type=int)
    parser.add_argument("--max-tasks-per-worker", type=int)
    parser.add_argument("--progress-every", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--run-id", type=int, help="resume an existing run")
    parser.add_argument(
        "--log-file",
        help="JSONL log path (empty EVEBENCH_LOG_FILE disables file logging)",
    )
    parser.add_argument(
        "--console-log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
    )
    parser.add_argument(
        "--checker",
        action="append",
        dest="checkers",
        help="override profile checker IDs; repeat to select multiple",
    )
    args = parser.parse_args(argv)
    if args.config and args.dataset:
        parser.error("dataset and --config cannot be used together")
    try:
        if args.config:
            benchmark_config = BenchmarkConfig.load(args.config)
        elif args.dataset:
            benchmark_config = BenchmarkConfig.ad_hoc(args.dataset)
        else:
            parser.error("provide a dataset CSV or --config")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    selected_checkers = (
        tuple(args.checkers)
        if args.checkers is not None
        else benchmark_config.checker_ids
    )
    database_path = (
        Path(args.database).resolve()
        if args.database
        else benchmark_config.database_path
    )
    timeout_ms = (
        args.timeout_ms if args.timeout_ms is not None else benchmark_config.timeout_ms
    )
    seed = args.seed if args.seed is not None else benchmark_config.seed
    semantics = args.semantics or benchmark_config.semantics
    max_workers = (
        args.max_workers
        if args.max_workers is not None
        else int(
            os.environ.get(
                "EVEBENCH_MAX_WORKERS",
                benchmark_config.max_workers,
            )
        )
    )
    max_tasks = (
        args.max_tasks_per_worker
        if args.max_tasks_per_worker is not None
        else int(
            os.environ.get(
                "EVEBENCH_MAX_TASKS_PER_WORKER",
                benchmark_config.max_tasks_per_worker,
            )
        )
    )
    progress_every = (
        args.progress_every
        if args.progress_every is not None
        else benchmark_config.progress_every
    )
    if max_workers < 1:
        parser.error("max-workers must be positive")
    if max_tasks < 0:
        parser.error("max-tasks-per-worker must be non-negative")
    if progress_every < 1:
        parser.error("progress-every must be positive")

    logging_config = LoggingConfig.from_env()
    if args.log_file is not None:
        logging_config = replace(
            logging_config,
            file_path=Path(args.log_file),
        )
    if args.console_log_level is not None:
        logging_config = replace(
            logging_config,
            console_level=args.console_log_level,
        )
    configure_logging(logging_config)

    pool = BackendProcessPool(
        max_workers=max_workers,
        max_tasks=max_tasks,
    )
    store = ResultStore(database_path)
    runner = BenchmarkRunner(CheckService(default_registry(pool)), store)

    def progress(position: int, total: int, run_id: int) -> None:
        if position == 1 or position == total or position % progress_every == 0:
            logger.info(
                "benchmark progress",
                extra={
                    "event": "benchmark.progress",
                    "run_id": run_id,
                    "completed_cases": position,
                    "total_cases": total,
                },
            )

    try:
        run_id = asyncio.run(
            runner.run_csv(
                benchmark_config.dataset_path,
                dataset_name=args.name or benchmark_config.name,
                checker_ids=list(selected_checkers) or None,
                timeout_ms=timeout_ms,
                semantics=semantics,
                seed=seed,
                strip_shared_order_by_limit=benchmark_config.strip_shared_order_by_limit,
                backend_options={
                    checker_id: options
                    for checker_id, options in (
                        benchmark_config.backend_options().items()
                    )
                    if checker_id in selected_checkers
                },
                execution_options={
                    "config_version": benchmark_config.version,
                    "config_path": (
                        str(benchmark_config.source_path)
                        if benchmark_config.source_path
                        else None
                    ),
                    "max_workers": max_workers,
                    "max_tasks_per_worker": max_tasks,
                    "progress_every": progress_every,
                },
                limit=args.limit,
                run_id=args.run_id,
                progress=progress,
            )
        )
    finally:
        pool.stop()

    print()
    print("backend       evaluated      eq     neq  unknown  unsupported  timeout")
    for row in store.summaries(run_id=run_id):
        print(
            f"{row.checker_id:<13} {row.evaluated:>5}  "
            f"{row.eq_ratio:>6.1%} {row.neq_ratio:>7.1%} "
            f"{row.unknown_ratio:>8.1%} {row.unsupported_ratio:>12.1%} "
            f"{row.timeout_ratio:>8.1%}"
        )
    print(f"\nSQLite results: {store.path.resolve()} (run {run_id})")


if __name__ == "__main__":
    main()
