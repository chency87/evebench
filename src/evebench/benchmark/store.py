from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator, Iterable
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median

from evebench.domain.models import CheckResult, EquivalenceCase, ExecutionStatus


class _Median:
    def __init__(self) -> None:
        self.values: list[int] = []

    def step(self, value: int | None) -> None:
        if value is not None:
            self.values.append(value)

    def finalize(self) -> float | None:
        return float(median(self.values)) if self.values else None


@dataclass(frozen=True, slots=True)
class BenchmarkSummary:
    run_id: int
    dataset: str
    checker_id: str
    display_name: str
    evaluated: int
    total_cases: int
    eq_count: int
    neq_count: int
    unknown_count: int
    unsupported_count: int
    timeout_count: int
    eq_ratio: float
    neq_ratio: float
    unknown_ratio: float
    unsupported_ratio: float
    timeout_ratio: float
    median_duration_ms: float | None
    labeled_count: int
    correct_count: int
    accuracy: float | None
    run_status: str
    started_at: str
    finished_at: str | None


class ResultStore:
    """Small SQLite repository for benchmark runs and backend results."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection, None, None]:
        with closing(sqlite3.connect(self.path, timeout=30)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.create_aggregate("median", 1, _Median)
            with connection:
                yield connection

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS benchmark_runs (
                    id INTEGER PRIMARY KEY,
                    dataset TEXT NOT NULL,
                    dataset_path TEXT NOT NULL,
                    dataset_sha256 TEXT NOT NULL,
                    total_cases INTEGER NOT NULL,
                    timeout_ms INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('running', 'completed', 'interrupted', 'failed')
                    ),
                    configuration_json TEXT NOT NULL DEFAULT '{}',
                    started_at TEXT NOT NULL,
                    finished_at TEXT
                );

                CREATE TABLE IF NOT EXISTS benchmark_cases (
                    run_id INTEGER NOT NULL REFERENCES benchmark_runs(id)
                        ON DELETE CASCADE,
                    case_id TEXT NOT NULL,
                    dataset_row INTEGER NOT NULL,
                    db_id TEXT,
                    ground_truth TEXT,
                    query_left TEXT NOT NULL,
                    query_right TEXT NOT NULL,
                    schema_ddl TEXT NOT NULL,
                    dialect TEXT NOT NULL,
                    PRIMARY KEY (run_id, case_id)
                );

                CREATE TABLE IF NOT EXISTS evaluation_results (
                    run_id INTEGER NOT NULL,
                    case_id TEXT NOT NULL,
                    checker_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    backend_version TEXT NOT NULL,
                    adapter_version TEXT NOT NULL,
                    result_category TEXT NOT NULL CHECK (
                        result_category IN ('eq', 'neq', 'unknown', 'unsupported', 'timeout')
                    ),
                    execution_status TEXT NOT NULL,
                    verdict TEXT,
                    duration_ms INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    reason_code TEXT,
                    message TEXT,
                    witness_json TEXT,
                    metrics_json TEXT NOT NULL,
                    raw_output TEXT,
                    PRIMARY KEY (run_id, case_id, checker_id),
                    FOREIGN KEY (run_id, case_id)
                        REFERENCES benchmark_cases(run_id, case_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS evaluation_results_summary
                    ON evaluation_results(run_id, checker_id, result_category);
                """
            )
    def create_run(
        self,
        *,
        dataset: str,
        dataset_path: str,
        dataset_sha256: str,
        total_cases: int,
        timeout_ms: int,
        configuration: dict[str, object],
        started_at: str,
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO benchmark_runs (
                    dataset, dataset_path, dataset_sha256, total_cases,
                    timeout_ms, status, configuration_json, started_at
                ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?)
                """,
                (
                    dataset,
                    dataset_path,
                    dataset_sha256,
                    total_cases,
                    timeout_ms,
                    json.dumps(configuration, sort_keys=True),
                    started_at,
                ),
            )
            run_id = int(cursor.lastrowid)
        return run_id

    def finish_run(self, run_id: int, *, status: str, finished_at: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE benchmark_runs
                SET status = ?, finished_at = ?
                WHERE id = ?
                """,
                (status, finished_at, run_id),
            )
    def validate_run(
        self,
        run_id: int,
        *,
        dataset_sha256: str,
        total_cases: int,
        timeout_ms: int,
        configuration: dict[str, object],
    ) -> None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT dataset_sha256, total_cases, timeout_ms,
                       configuration_json
                FROM benchmark_runs WHERE id = ?
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"benchmark run does not exist: {run_id}")
        mismatches = []
        if row["dataset_sha256"] != dataset_sha256:
            mismatches.append("dataset contents")
        if row["total_cases"] != total_cases:
            mismatches.append("case limit")
        if row["timeout_ms"] != timeout_ms:
            mismatches.append("timeout")
        if row["configuration_json"] != json.dumps(configuration, sort_keys=True):
            mismatches.append("benchmark options")
        if mismatches:
            raise ValueError(
                f"cannot resume run {run_id} with a different " + ", ".join(mismatches)
            )

    def completed_checker_ids(self, run_id: int, case_id: str) -> set[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT checker_id FROM evaluation_results
                WHERE run_id = ? AND case_id = ?
                """,
                (run_id, case_id),
            )
            return {row["checker_id"] for row in rows}

    def save_results(
        self,
        *,
        run_id: int,
        case: EquivalenceCase,
        dataset_row: int,
        db_id: str | None,
        ground_truth: str | None,
        results: Iterable[tuple[str, CheckResult]],
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO benchmark_cases (
                    run_id, case_id, dataset_row, db_id, ground_truth,
                    query_left, query_right, schema_ddl, dialect
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, case_id) DO NOTHING
                """,
                (
                    run_id,
                    case.case_id,
                    dataset_row,
                    db_id,
                    ground_truth,
                    case.query_left,
                    case.query_right,
                    case.schema_ddl,
                    case.dialect,
                ),
            )
            connection.executemany(
                """
                INSERT INTO evaluation_results (
                        run_id, case_id, checker_id, display_name,
                        backend_version, adapter_version, result_category,
                        execution_status, verdict, duration_ms, started_at,
                        finished_at, reason_code, message, witness_json,
                        metrics_json, raw_output
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, case_id, checker_id) DO UPDATE SET
                        display_name = excluded.display_name,
                        backend_version = excluded.backend_version,
                        adapter_version = excluded.adapter_version,
                        result_category = excluded.result_category,
                        execution_status = excluded.execution_status,
                        verdict = excluded.verdict,
                        duration_ms = excluded.duration_ms,
                        started_at = excluded.started_at,
                        finished_at = excluded.finished_at,
                        reason_code = excluded.reason_code,
                        message = excluded.message,
                        witness_json = excluded.witness_json,
                        metrics_json = excluded.metrics_json,
                        raw_output = excluded.raw_output
                """,
                (
                    (
                        run_id,
                        case.case_id,
                        result.checker.checker_id,
                        display_name,
                        result.checker.backend_version,
                        result.checker.adapter_version,
                        result_category(result),
                        result.execution_status.value,
                        result.verdict.value if result.verdict else None,
                        result.duration_ms,
                        result.started_at.isoformat(),
                        result.finished_at.isoformat(),
                        result.reason_code,
                        result.message,
                        (
                            json.dumps(result.witness, sort_keys=True)
                            if result.witness is not None
                            else None
                        ),
                        json.dumps(result.metrics, sort_keys=True),
                        result.raw_output,
                    )
                    for display_name, result in results
                ),
            )

    def summaries(
        self,
        *,
        dataset: str | None = None,
        run_id: int | None = None,
    ) -> list[BenchmarkSummary]:
        where = []
        parameters: list[object] = []
        if dataset is not None:
            where.append("r.dataset = ?")
            parameters.append(dataset)
        if run_id is not None:
            where.append("r.id = ?")
            parameters.append(run_id)
        else:
            where.append(
                """r.id = (
                    SELECT MAX(latest.id) FROM benchmark_runs AS latest
                    WHERE latest.dataset = r.dataset
                )"""
            )
        predicate = f"WHERE {' AND '.join(where)}"
        query = f"""
            SELECT
                r.id AS run_id, r.dataset, e.checker_id,
                MAX(e.display_name) AS display_name,
                COUNT(*) AS evaluated, r.total_cases,
                SUM(e.result_category = 'eq') AS eq_count,
                SUM(e.result_category = 'neq') AS neq_count,
                SUM(e.result_category = 'unknown') AS unknown_count,
                SUM(e.result_category = 'unsupported') AS unsupported_count,
                SUM(e.result_category = 'timeout') AS timeout_count,
                ROUND(1.0 * SUM(e.result_category = 'eq') / COUNT(*), 6)
                    AS eq_ratio,
                ROUND(1.0 * SUM(e.result_category = 'neq') / COUNT(*), 6)
                    AS neq_ratio,
                ROUND(1.0 * SUM(e.result_category = 'unknown') / COUNT(*), 6)
                    AS unknown_ratio,
                ROUND(1.0 * SUM(e.result_category = 'unsupported') / COUNT(*), 6)
                    AS unsupported_ratio,
                ROUND(1.0 * SUM(e.result_category = 'timeout') / COUNT(*), 6)
                    AS timeout_ratio,
                median(CASE WHEN json_extract(e.metrics_json, '$.timing_available') = 0
                    THEN NULL ELSE e.duration_ms END) AS median_duration_ms,
                SUM(CASE WHEN c.ground_truth IN ('EQ', 'NEQ') THEN 1 ELSE 0 END) AS labeled_count,
                SUM(CASE WHEN e.result_category = LOWER(c.ground_truth) THEN 1 ELSE 0 END) AS correct_count,
                ROUND(1.0 * SUM(CASE WHEN e.result_category = LOWER(c.ground_truth) THEN 1 ELSE 0 END)
                    / NULLIF(SUM(CASE WHEN c.ground_truth IN ('EQ', 'NEQ') THEN 1 ELSE 0 END), 0), 6) AS accuracy,
                r.status AS run_status,
                r.started_at, r.finished_at
            FROM benchmark_runs AS r
            JOIN evaluation_results AS e ON e.run_id = r.id
            JOIN benchmark_cases AS c ON c.run_id = e.run_id AND c.case_id = e.case_id
            {predicate}
            GROUP BY r.id, e.checker_id
            ORDER BY r.dataset, e.checker_id
        """
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [BenchmarkSummary(**dict(row)) for row in rows]

    def summary_dicts(
        self,
        *,
        dataset: str | None = None,
        run_id: int | None = None,
    ) -> list[dict[str, object]]:
        return [asdict(item) for item in self.summaries(dataset=dataset, run_id=run_id)]

    def unsupported_reasons(self, *, run_id: int) -> list[dict[str, object]]:
        """Count unsupported results by checker and recorded reason for one run."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT checker_id, MAX(display_name) AS display_name,
                       COALESCE(NULLIF(reason_code, ''), 'unspecified') AS reason_code,
                       COUNT(*) AS count
                FROM evaluation_results
                WHERE run_id = ? AND result_category = 'unsupported'
                GROUP BY checker_id, COALESCE(NULLIF(reason_code, ''), 'unspecified')
                ORDER BY checker_id, count DESC, reason_code
                """,
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]


def result_category(result: CheckResult) -> str:
    if result.execution_status == ExecutionStatus.TIMEOUT:
        return "timeout"
    if result.execution_status == ExecutionStatus.UNSUPPORTED:
        return "unsupported"
    if result.verdict is None:
        return "unknown"
    return {
        "equivalent": "eq",
        "not_equivalent": "neq",
        "unknown": "unknown",
        "unsupported": "unsupported",
        "timeout": "timeout",
    }[result.verdict.value]
