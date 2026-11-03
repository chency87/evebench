"""Run unmodified XData on one query pair and confirm its kills on PostgreSQL.

XData is a mutation-killing test-data generator. The runner writes the pair in
XData's benchmark layout (the left query as the original and the right query as
its mutant, plus the reverse direction when bidirectional), points XData at
its own scratch directories by placing a generated ``util/XData.properties``
ahead of ``xdata.jar`` on the classpath, and runs ``test.RegressionTests``.

XData counts any SQL error while comparing a query and its mutant as a kill, so
each reported kill is replayed on the generated datasets before it is returned
as a counterexample.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.errors
from sqlalchemy.engine import make_url

DIRECTIONS = {1: "left_to_right", 2: "right_to_left"}
WITNESS_ROWS = 20
XDATA_LOG_CHARS = 4000
_RESULT_LINE = re.compile(r"^(\d+)\|.*state='(\w+)'\s*$")
_CREATE_TABLE = re.compile(r"(?i)^\s*create\s+table\s+")

# Settings other than paths and credentials match the XData.properties
# bundled in xdata.jar.
_FIXED_PROPERTIES = {
    "tempDatabaseType": "PostgreSQL",
    "smtargs": "-smt2",
    "queries": "queries",
    "dataset": "datasets",
    "mutants": "mutants",
    "sample": "sample",
    "logLevel": "INFO",
    "tempJoins": "true",
    "cntFlag": "true",
    "existsUnrollFlag": "true",
    "sampleDataJson": "false",
    "isEnumInt": "false",
    "enumArrayIndex": "enum_int",
    "enumIndexVar": "v",
    "outerSQ": "true",
    "regressDS0": "false",
    "primarykey": "1",
    "printSQL": "false",
}


class _Timeout(Exception):
    pass


def main() -> None:
    payload = json.load(sys.stdin)
    deadline = time.monotonic() + payload["deadline"] - time.time()
    try:
        output = _check(payload, deadline)
    except _Timeout:
        output = {"state": "timeout", "reason": "XData exceeded its deadline"}
    except Exception as exc:
        output = {"state": "error", "reason": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(output, separators=(",", ":"), default=str))


def _check(payload: dict[str, Any], deadline: float) -> dict[str, Any]:
    database = _database(payload["connection_string"])
    schema = [line for line in payload["schema"].splitlines() if line.strip()]
    sql1, sql2 = payload["sql1"], payload["sql2"]
    pairs = {1: (sql1, sql2)}
    if payload["bidirectional"]:
        pairs[2] = (sql2, sql1)

    rejected = _validate(database, schema, sql1, sql2, deadline)
    if rejected is not None:
        return rejected

    with tempfile.TemporaryDirectory(prefix="evebench-xdata-") as directory:
        work = Path(directory)
        _write_inputs(work, payload, database, pairs)
        returncode, lock_wait, elapsed = _run_xdata(payload, work, deadline)
        log = (work / "xdata.log").read_text(errors="replace")
        results = work / "output" / "case-mutant-results.out"
        found = _read_states(results)
        if not found:
            return {
                "state": "error",
                "reason": (
                    f"XData exited with code {returncode} without results: "
                    f"{_failure(log) or log[-1000:]}"
                ),
                "xdata_log": log[-XDATA_LOG_CHARS:],
            }
        # XData writes no result line when its basic dataset raises.
        states = {query_id: found.get(query_id, "Unsupported") for query_id in pairs}
        dataset_dirs = {
            query_id: work / "home" / "temp_smt" / str(query_id) for query_id in pairs
        }
        output: dict[str, Any] = {
            "xdata_states": {
                DIRECTIONS[query_id]: state for query_id, state in states.items()
            },
            "dataset_count": sum(
                len(list(path.glob("DS*.sql"))) for path in dataset_dirs.values()
            ),
            "lock_wait_s": round(lock_wait, 3),
            "xdata_time_s": round(elapsed, 3),
        }

        unconfirmed = []
        for query_id, state in states.items():
            if state != "Killed":
                continue
            witness = _confirm_kill(
                database, schema, dataset_dirs[query_id], sql1, sql2, deadline
            )
            if witness is not None:
                return {
                    **output,
                    "state": "not_equivalent",
                    "counterexample": {"direction": DIRECTIONS[query_id], **witness},
                }
            unconfirmed.append(DIRECTIONS[query_id])

    if all(state == "Unsupported" for state in states.values()):
        reason = _failure(log)
        return {
            **output,
            "state": "unsupported",
            "reason": "XData could not generate datasets"
            + (f": {reason}" if reason else ""),
            "xdata_log": log[-XDATA_LOG_CHARS:],
        }
    reason = "No XData dataset distinguished the queries"
    if unconfirmed:
        reason += (
            "; XData reported a kill that did not reproduce on its datasets "
            f"({', '.join(unconfirmed)})"
        )
    return {**output, "state": "unknown", "reason": reason}


def _database(connection_string: str) -> dict[str, Any]:
    url = make_url(connection_string)
    return {
        "host": url.host or "127.0.0.1",
        "port": url.port or 5432,
        "dbname": url.database,
        "user": url.username,
        "password": url.password or "",
    }


@contextlib.contextmanager
def _session(
    database: dict[str, Any], schema: list[str], deadline: float
) -> Iterator[Any]:
    """Open a connection holding the schema as temporary tables, as XData does."""
    remaining_ms = int((deadline - time.monotonic()) * 1000)
    if remaining_ms <= 0:
        raise _Timeout
    connection = psycopg2.connect(
        **database,
        connect_timeout=10,
        options=f"-c statement_timeout={remaining_ms}",
    )
    try:
        connection.autocommit = True
        cursor = connection.cursor()
        for statement in schema:
            cursor.execute(_CREATE_TABLE.sub("CREATE TEMPORARY TABLE ", statement))
        yield cursor
    finally:
        connection.close()


def _validate(
    database: dict[str, Any],
    schema: list[str],
    sql1: str,
    sql2: str,
    deadline: float,
) -> dict[str, Any] | None:
    try:
        with _session(database, schema, deadline) as cursor:
            for side, sql in (("left", sql1), ("right", sql2)):
                try:
                    cursor.execute("EXPLAIN " + sql)
                except psycopg2.Error as exc:
                    return {
                        "state": "invalid_input",
                        "reason": f"The {side} query failed on PostgreSQL: {_pg_error(exc)}",
                    }
    except psycopg2.errors.QueryCanceled as exc:
        raise _Timeout from exc
    except psycopg2.OperationalError:
        raise
    except psycopg2.Error as exc:
        # XData loads the schema the same way and reports such pairs as
        # Unsupported, e.g. for schema-qualified tables.
        return {
            "state": "unsupported",
            "reason": (
                "XData could not load the schema as PostgreSQL temporary tables: "
                f"{_pg_error(exc)}"
            ),
        }
    return None


def _write_inputs(
    work: Path,
    payload: dict[str, Any],
    database: dict[str, Any],
    pairs: dict[int, tuple[str, str]],
) -> None:
    home = work / "home"
    base = work / "input"
    for path in (
        work / "conf" / "util",
        home,
        work / "output",
        base / "datasets",
        base / "queries",
        base / "mutants",
    ):
        path.mkdir(parents=True, exist_ok=True)

    (base / "datasets" / "case.sql").write_text(payload["schema"] + "\n")
    (base / "queries" / "case.txt").write_text(
        "".join(f"{query_id}|evebench|{query}\n" for query_id, (query, _) in pairs.items())
    )
    (base / "mutants" / "case.txt").write_text(
        "".join(f"{query_id}|evebench|{mutant}\n" for query_id, (_, mutant) in pairs.items())
    )

    properties = {
        **_FIXED_PROPERTIES,
        "databaseName": database["dbname"],
        "existingDatabaseUser": database["user"],
        "existingDatabaseUserPasswd": database["password"],
        "testDatabaseUser": database["user"],
        "testDatabaseUserPasswd": database["password"],
        "databaseIP": database["host"],
        "databasePort": str(database["port"]),
        "homeDir": str(home),
        "testDir": str(home),
        "printDir": str(home),
        "logFile": str(home / "XData.log"),
        "base": str(base),
        "smtsolver": str(Path(payload["root"]) / "xdata" / "z3" / "z3"),
    }
    (work / "conf" / "util" / "XData.properties").write_text(
        "".join(f"{key}={_property(value)}\n" for key, value in properties.items()),
        encoding="ascii",
    )


def _run_xdata(
    payload: dict[str, Any], work: Path, deadline: float
) -> tuple[int, float, float]:
    xdata = Path(payload["root"]).resolve() / "xdata"
    jars = xdata / "jars"
    z3 = str(xdata / "z3")
    # xdata/jars ships jsqlparser 0.9.7 and 4.3; XData's parser needs 0.9.7
    # to win, so it precedes the wildcard.
    classpath = os.pathsep.join(
        [
            str(work / "conf"),
            str(jars / "xdata.jar"),
            str(jars / "jsqlparser-0.9.7-SNAPSHOT.jar"),
            str(jars / "*"),
        ]
    )
    environment = os.environ.copy()
    library_path = environment.get("LD_LIBRARY_PATH")
    environment["LD_LIBRARY_PATH"] = (
        z3 if not library_path else z3 + os.pathsep + library_path
    )
    command = [
        payload["java"],
        f"-Djava.library.path={z3}",
        "-cp",
        classpath,
        "test.RegressionTests",
        str(work / "input"),
        "datasets",
        "",
        "queries",
        "mutants",
        "",
        str(work / "output"),
    ]

    with open(payload["lock_path"], "a") as lock:
        waiting = time.monotonic()
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise _Timeout from None
                time.sleep(0.05)
        started = time.monotonic()
        with open(work / "xdata.log", "w") as log:
            process = subprocess.Popen(
                command,
                cwd=work / "home",
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                returncode = process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                # XData starts Z3 through bash, so stop the whole process group.
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise _Timeout from None
        return returncode, started - waiting, time.monotonic() - started


def _read_states(path: Path) -> dict[int, str]:
    if not path.is_file():
        return {}
    states = {}
    for line in path.read_text(errors="replace").splitlines():
        match = _RESULT_LINE.match(line)
        if match:
            states[int(match[1])] = match[2]
    return states


def _confirm_kill(
    database: dict[str, Any],
    schema: list[str],
    dataset_dir: Path,
    sql1: str,
    sql2: str,
    deadline: float,
) -> dict[str, Any] | None:
    """Replay XData's datasets and return the first one that separates the queries."""
    datasets = sorted(
        dataset_dir.glob("DS*.sql"),
        key=lambda path: int(path.stem[2:]) if path.stem[2:].isdigit() else -1,
    )
    difference = f"(({sql1}) EXCEPT ALL ({sql2})) UNION (({sql2}) EXCEPT ALL ({sql1}))"
    for path in datasets:
        statements = [
            line
            for line in path.read_text(errors="replace").splitlines()
            if line.strip() and not line.startswith("--")
        ]
        try:
            with _session(database, schema, deadline) as cursor:
                for statement in statements:
                    try:
                        cursor.execute(statement)
                    except psycopg2.Error:
                        # XData skips generated rows that violate constraints.
                        pass
                try:
                    cursor.execute(difference)
                    if cursor.fetchone() is None:
                        continue
                    reason = None
                except psycopg2.Error as exc:
                    # Incompatible result shapes are a difference only when both
                    # queries run on this dataset.
                    reason = f"Results are not comparable: {_pg_error(exc)}"
                witness: dict[str, Any] = {
                    "dataset": path.stem,
                    "statements": statements,
                }
                for key, sql in (("left_rows", sql1), ("right_rows", sql2)):
                    try:
                        cursor.execute(sql)
                    except psycopg2.Error:
                        witness = {}
                        break
                    witness[key] = [list(row) for row in cursor.fetchmany(WITNESS_ROWS)]
                if not witness:
                    continue
                if reason is not None:
                    witness["reason"] = reason
                return witness
        except psycopg2.errors.QueryCanceled as exc:
            raise _Timeout from exc
    return None


def _failure(log: str) -> str | None:
    for line in log.splitlines():
        line = line.strip()
        if ("Exception" in line or "Error" in line) and not line.startswith("at "):
            return line[:500]
    return None


def _pg_error(exc: psycopg2.Error) -> str:
    return (exc.pgerror or str(exc)).strip().splitlines()[0]


def _property(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")
    )
    return "".join(
        char if ord(char) < 128 else f"\\u{ord(char):04x}" for char in escaped
    )


if __name__ == "__main__":
    main()
