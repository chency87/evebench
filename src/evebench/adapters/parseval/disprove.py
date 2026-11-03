"""EveBench's ParSEval orchestration and result-comparison policy.

ParSEval supplies the query-driven data generator and scratch database
lifecycle.  EveBench owns comparison (:mod:`evebench.adapters.common.comparison`)
because its cases explicitly select bag or set semantics, ordered results
need deterministic tie handling, and backend floating-point results need
tolerant rather than bit-exact comparison.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

import sqlglot
from parseval import (
    Catalog,
    DBManager,
    GenerationConfig,
    RunResult,
    Verdict,
    generate,
)
from parseval.db_manager import Connect
from parseval.errors import SubEqError
from parseval.instance import ExecutionError, Instance
from sqlalchemy.exc import DBAPIError

from evebench.adapters.common.comparison import (
    ABSOLUTE_TOLERANCE,
    RELATIVE_TOLERANCE,
    observe,
    outcomes_differ,
    results_differ,
    rows_equal,
    values_equal,
)


def disprove(
    query_left: str,
    query_right: str,
    schema: str,
    connection_string: str,
    dialect: str,
    *,
    config: GenerationConfig | None = None,
    timeout_s: float = 15,
    set_semantics: bool = False,
) -> RunResult:
    """Look for a generated database that distinguishes two queries.

    This follows ParSEval 1.0.1's public ``disprove`` contract, but delegates
    result equality to :func:`outcomes_differ`.  In particular, row order
    matters only when both queries have a top-level ``ORDER BY`` and both
    orders are determined on the generated database.
    """
    queries = (query_left, query_right)
    try:
        trees = [
            [tree for tree in sqlglot.parse(sql, read=dialect) if tree is not None]
            for sql in queries
        ]
    except sqlglot.errors.SqlglotError as error:
        # As in ParSEval, let the backend decide whether unparsed SQL is valid.
        trees, unparsed = None, error

    if trees is not None and any(len(statements) != 1 for statements in trees):
        return RunResult(Verdict.SYNTAX_ERROR, "a query must be one statement")

    generation_config = replace(
        config or GenerationConfig(),
        set_semantics=set_semantics,
    )
    database = DBManager(connection_string, dialect)
    catalog = Catalog.from_ddl(schema, dialect=dialect)
    counterexample = None
    results: tuple[Any, Any] = (None, None)

    def check(instance: Instance) -> bool:
        """Run both queries on one instance and retain the first mismatch."""
        nonlocal counterexample, results
        with database.scratch() as connection:
            connection.create_tables(schema)
            connection.load(instance)
            outcomes = tuple(
                observe(
                    lambda sql: _run(connection, sql, timeout_s),
                    query,
                    dialect,
                    schema,
                )
                for query in queries
            )
        results = tuple(outcome.rows for outcome in outcomes)
        if not outcomes_differ(*outcomes, set_semantics=set_semantics):
            return False
        counterexample = instance
        return True

    # Generation never returns the empty instance.  It also lets the backend
    # perform the authoritative initial syntax check.
    check(Instance(catalog))
    for result in results:
        if isinstance(result, str):
            return RunResult(Verdict.SYNTAX_ERROR, result)
    if counterexample is not None:
        return RunResult(
            Verdict.NEQ,
            "the empty database tells them apart",
            counterexample,
            results,
        )
    if trees is None:
        return RunResult(Verdict.UNKNOWN, f"the queries do not parse: {unparsed}")

    doubts: list[tuple[Verdict, str]] = []
    for index, sql in enumerate(queries, 1):
        try:
            generated = generate(
                sql,
                catalog,
                config=generation_config,
                on_instance=check,
            )
        except (SubEqError, ExecutionError) as error:
            doubts.append((Verdict.UNKNOWN, f"q{index}: {error}"))
            continue
        if generated.instance is None:
            if generated.timed_out:
                doubts.append(
                    (Verdict.TIMEOUT, f"q{index}: generation ran out of time")
                )
            else:
                reason = generated.unsupported or "no database generated"
                doubts.append((Verdict.UNKNOWN, f"q{index}: {reason}"))
            continue
        if counterexample is not None:
            return RunResult(
                Verdict.NEQ,
                f"q{index}'s database tells them apart",
                counterexample,
                results,
            )
        if None in results:
            doubts.append(
                (Verdict.TIMEOUT, f"a query ran out of time on q{index}'s database")
            )
        elif generated.timed_out:
            doubts.append(
                (Verdict.TIMEOUT, f"q{index}: generation ran out of time")
            )
        elif not any(isinstance(result, list) and result for result in results):
            doubts.append(
                (Verdict.UNKNOWN, f"q{index}'s database gives neither query rows")
            )

    if doubts:
        verdict = (
            Verdict.TIMEOUT
            if any(verdict is Verdict.TIMEOUT for verdict, _ in doubts)
            else Verdict.UNKNOWN
        )
        return RunResult(
            verdict,
            "; ".join(reason for _, reason in doubts),
        )
    return RunResult(Verdict.EQ)


def _run(connection: Connect, sql: str, timeout_s: float) -> Any:
    """Run a query using ParSEval's timeout/error result convention."""
    started = time.monotonic()
    try:
        return connection.execute(sql, timeout=timeout_s)
    except DBAPIError as error:
        if time.monotonic() - started >= timeout_s:
            return None
        return str(error.orig)


__all__ = [
    "ABSOLUTE_TOLERANCE",
    "RELATIVE_TOLERANCE",
    "disprove",
    "results_differ",
    "rows_equal",
    "values_equal",
]
