"""EveBench's query-result comparison policy.

A difference is reported only when it holds for every execution the engine
may choose; uncertainty is never evidence.  Rows compare positionally, with
float cells equal within a relative tolerance.  Results compare as bags (or
sets), except that when both queries have a top-level ``ORDER BY`` and each
query's row order is fully determined on the database, the results compare as
ordered lists.  Timeouts and backend errors are never evidence: whether a
query fails can depend on its plan.

Whether a query's output is determined is checked by running it twice more
with its output columns appended to the top-level ``ORDER BY`` as tie-breakers,
once ascending and once descending.  Distinct rows that tie on the query's own
sort keys come back in opposite orders, so the two runs agree only if no such
ties exist.  A query whose rows are selected by ``LIMIT``, ``OFFSET``, ``FETCH``
or ``DISTINCT ON`` can also return a different bag when ties exist; a database
on which either query's output depends on tie-breaking is evidence of a
difference only through differing row counts.  Under a collation that equates
distinct strings (MySQL's defaults, or one declared in the query or schema),
tied text rows are treated as undetermined.

Only the top level of each query is examined.  Nondeterminism inside a query
is out of scope: ``LIMIT`` in a subquery or union arm, window functions over
ties, ``GROUP_CONCAT``/``STRING_AGG`` order, bare columns in SQLite
aggregates, volatile functions such as ``random()``, and float sums whose
result depends on summation order beyond the tolerance.  The validator, and the
benchmark runner when configured, strip a shared top-level ``ORDER BY`` and
``LIMIT`` before comparing (``strip_shared_order_by_limit``).

The tie-broken variants are sqlglot renderings of the query and serve only as
probes; the evidence is always the original query's rows.  A determined
outcome requires both variants to return exactly what the original did, so a
rendering that changed the query leaves the outcome undetermined.
"""

from __future__ import annotations

import unicodedata
from collections import deque
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Any, Callable

import sqlglot
from sqlglot import exp

# Floats compare like math.isclose: generous enough for roundoff from summing
# in a different order, at any magnitude.
RELATIVE_TOLERANCE = Decimal("1e-9")
ABSOLUTE_TOLERANCE = Decimal("1e-10")
_SQLGLOT_DIALECTS = {"postgresql": "postgres", "mariadb": "mysql"}


@dataclass(frozen=True, slots=True)
class QueryOutcome:
    """One query's result on one database, with how far it is determined.

    ``rows`` is the original query's list of rows, ``None`` for a timeout, or
    a string for a backend error.
    """

    rows: Any
    ordered: bool = False
    list_determined: bool = False
    bag_determined: bool = False


def observe(
    execute: Callable[[str], Any],
    sql: str,
    dialect: str,
    schema: str = "",
) -> QueryOutcome:
    """Run ``sql`` and, when needed, its tie-broken variants.

    ``execute`` returns rows, ``None`` for a timeout, or an error string, or
    raises.  Exceptions from the original query propagate; a failed variant
    only leaves the outcome undetermined.  ``schema`` is the DDL the query runs
    against, used only to notice declared collations.
    """
    rows = execute(sql)
    if not isinstance(rows, list):
        return QueryOutcome(rows)
    shape = query_shape(sql, dialect)
    if shape is None:
        # Without a parse, LIMIT and DISTINCT ON cannot be ruled out.
        return QueryOutcome(rows)
    ordered, selects_rows = shape
    if not ordered and not selects_rows:
        return QueryOutcome(rows, bag_determined=True)
    if not rows:
        return QueryOutcome(rows, ordered, list_determined=True, bag_determined=True)

    variants = [
        tiebreak_query(sql, dialect, len(rows[0]), descending=descending)
        for descending in (False, True)
    ]
    try:
        ascending, descending = (
            execute(variant) if variant is not None else None
            for variant in variants
        )
    except Exception:
        ascending = descending = None
    if not isinstance(ascending, list) or not isinstance(descending, list):
        return QueryOutcome(rows, ordered, bag_determined=not selects_rows)

    # The variants only probe whether ties matter; the evidence is always the
    # original's rows.  If the order (or row selection) is determined, the
    # original must have returned exactly what both variants return, so any
    # mismatch, such as from a rendering that changed the query, leaves the
    # outcome undetermined.
    bag_determined = not selects_rows or (
        _bags_equal(ascending, descending) and _bags_equal(ascending, rows)
    )
    list_determined = _lists_equal(ascending, descending) and _lists_equal(ascending, rows)
    if _collation_may_tie(sql, dialect, schema):
        # Strings equal under a case/accent/pad-insensitive collation tie in both
        # runs, so the runs agreeing does not show the order is determined.  A
        # twin cut off by LIMIT is not even visible, so selected text rows are
        # never determined under such a collation.
        if _has_collation_twins(rows):
            list_determined = False
        if selects_rows and any(isinstance(value, str) for row in rows for value in row):
            bag_determined = False
    return QueryOutcome(
        rows,
        ordered,
        list_determined=list_determined,
        bag_determined=bag_determined,
    )


def comparison_mode(
    left: QueryOutcome,
    right: QueryOutcome,
    *,
    set_semantics: bool = False,
) -> str:
    """How two successful outcomes compare: list, bag, set, or undetermined."""
    if not (left.bag_determined and right.bag_determined):
        return "undetermined"
    if set_semantics:
        return "set"
    if (
        left.ordered
        and right.ordered
        and left.list_determined
        and right.list_determined
    ):
        return "list"
    return "bag"


def outcomes_differ(
    left: QueryOutcome,
    right: QueryOutcome,
    *,
    set_semantics: bool = False,
) -> bool:
    """Whether two observed query outcomes establish a difference."""
    if not isinstance(left.rows, list) or not isinstance(right.rows, list):
        return results_differ(left.rows, right.rows)
    mode = comparison_mode(left, right, set_semantics=set_semantics)
    if mode == "list":
        return not _lists_equal(left.rows, right.rows)
    if mode == "set":
        return not _sets_equal(left.rows, right.rows)
    if mode == "bag":
        return not _bags_equal(left.rows, right.rows)
    # Tie-breaking changes which rows LIMIT, OFFSET or DISTINCT ON select, never
    # how many, so differing row counts remain evidence.
    return not set_semantics and len(left.rows) != len(right.rows)


def query_shape(sql: str, dialect: str) -> tuple[bool, bool] | None:
    """Return (top-level ORDER BY, rows selected by LIMIT/OFFSET/FETCH/DISTINCT ON)."""
    tree = _parse_query(sql, dialect)
    if tree is None:
        return None
    distinct = tree.args.get("distinct")
    selects_rows = (
        any(tree.args.get(key) is not None for key in ("limit", "offset"))
        or (isinstance(distinct, exp.Distinct) and distinct.args.get("on") is not None)
    )
    return tree.args.get("order") is not None, selects_rows


def tiebreak_query(
    sql: str,
    dialect: str,
    width: int,
    *,
    descending: bool,
) -> str | None:
    """Return ``sql`` with output ordinals 1..width appended to its top-level ORDER BY.

    An ORDER BY is added when absent.  The query is re-rendered by sqlglot, so
    :func:`observe` checks that the result returns what the original does.
    Returns ``None`` when the query does not parse or uses FETCH ... WITH TIES,
    which returns every row tied with the last one: tie-breakers would shrink
    that group.
    """
    tree = _parse_query(sql, dialect)
    limit = tree.args.get("limit") if tree is not None else None
    if tree is None or (isinstance(limit, exp.Fetch) and limit.args.get("with_ties")):
        return None
    read = _SQLGLOT_DIALECTS.get(dialect, dialect)
    # Parsed rather than built, so the ordinals keep the dialect's default NULL
    # placement, which ASC and DESC reverse, and render without NULLS FIRST/LAST.
    direction = " DESC" if descending else ""
    ordinals = sqlglot.parse_one(
        "SELECT 1 ORDER BY " + ", ".join(f"{index}{direction}" for index in range(1, width + 1)),
        read=read,
    ).args["order"].expressions
    order = tree.args.get("order") or exp.Order(expressions=[])
    for ordinal in ordinals:
        order.append("expressions", ordinal)
    tree.set("order", order)
    return tree.sql(dialect=read)


def _parse_query(sql: str, dialect: str) -> exp.Query | None:
    """Parse one query, unwrapping parentheses around the whole of it."""
    try:
        statements = [
            statement
            for statement in sqlglot.parse(sql, read=_SQLGLOT_DIALECTS.get(dialect, dialect))
            if statement is not None
        ]
    except sqlglot.errors.SqlglotError:
        return None
    if len(statements) != 1:
        return None
    tree = statements[0]
    # A whole query in parentheses keeps its ORDER BY and LIMIT on the inner query.
    while isinstance(tree, exp.Subquery) and isinstance(tree.this, exp.Query):
        tree = tree.this
    return tree if isinstance(tree, exp.Query) else None


def results_differ(
    first: Any,
    second: Any,
    *,
    set_semantics: bool = False,
) -> bool:
    """Whether two backend query results differ, ignoring row order.

    ``None`` means a timed-out query and a string a backend error; neither is
    evidence of a difference.  Whether a query fails can depend on its plan,
    such as an overflow that depends on summation order or a guard the planner
    evaluates after the expression it guards.
    """
    if not isinstance(first, list) or not isinstance(second, list):
        return False

    if set_semantics:
        return not _sets_equal(first, second)
    return not _bags_equal(first, second)


def rows_equal(first: Any, second: Any) -> bool:
    """Compare rows positionally, tolerating floating-point roundoff."""
    if not isinstance(first, (tuple, list)) or not isinstance(second, (tuple, list)):
        return values_equal(first, second)
    return len(first) == len(second) and all(
        values_equal(left, right) for left, right in zip(first, second, strict=True)
    )


def values_equal(first: Any, second: Any) -> bool:
    """Compare cells, using tolerance only when at least one value is a float.

    Array and JSON cells compare element by element under the same rules.
    """
    # bool is a subclass of int, but SQL boolean and numeric results are not
    # interchangeable merely because Python considers True == 1.
    if isinstance(first, bool) or isinstance(second, bool):
        return type(first) is type(second) and first == second
    if isinstance(first, (list, tuple)) and isinstance(second, (list, tuple)):
        return rows_equal(first, second)
    if isinstance(first, dict) and isinstance(second, dict):
        return first.keys() == second.keys() and all(
            values_equal(first[key], second[key]) for key in first
        )

    if isinstance(first, float) or isinstance(second, float):
        if not _is_number(first) or not _is_number(second):
            return False
        return _floats_close(first, second)
    return first == second


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def _floats_close(first: int | float | Decimal, second: int | float | Decimal) -> bool:
    """Equal within a relative tolerance, or an absolute one near zero."""
    left, right = Decimal(str(first)), Decimal(str(second))
    if left.is_nan() or right.is_nan():
        return left.is_nan() and right.is_nan()
    if left.is_infinite() or right.is_infinite():
        return left == right

    with localcontext() as context:
        context.prec = 64
        limit = max(ABSOLUTE_TOLERANCE, RELATIVE_TOLERANCE * max(abs(left), abs(right)))
        return abs(left - right) <= limit


def _collation_may_tie(sql: str, dialect: str, schema: str) -> bool:
    """Whether distinct strings may compare equal when this query orders rows.

    MySQL's default collations ignore case, accents and trailing spaces; other
    engines compare text with deterministic collations unless the query or the
    schema declares one (such as SQLite NOCASE or PostgreSQL citext).
    """
    if _SQLGLOT_DIALECTS.get(dialect, dialect) == "mysql":
        return True
    text = f"{sql}\n{schema}".lower()
    return "collate" in text or "citext" in text


def _fold(value: Any) -> Any:
    if isinstance(value, str):
        decomposed = unicodedata.normalize("NFKD", value)
        stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
        return stripped.casefold().rstrip(" ")
    if isinstance(value, (list, tuple)):
        return tuple(_fold(item) for item in value)
    return value


def _has_collation_twins(rows: list[Any]) -> bool:
    """Whether two distinct rows become equal once their strings are folded."""
    seen: dict[str, Any] = {}
    for row in rows:
        first = seen.setdefault(repr(_fold(row)), row)
        if first is not row and not rows_equal(first, row):
            return True
    return False


def _lists_equal(first: list[Any], second: list[Any]) -> bool:
    return len(first) == len(second) and all(
        rows_equal(left, right) for left, right in zip(first, second)
    )


def _bags_equal(first: list[Any], second: list[Any]) -> bool:
    if len(first) != len(second):
        return False

    # A one-to-one match preserves duplicate multiplicity without requiring
    # backend values to be hashable or sortable.
    adjacency = [
        [index for index, candidate in enumerate(second) if rows_equal(row, candidate)]
        for row in first
    ]
    if any(not candidates for candidates in adjacency):
        return False

    matched_left = [-1] * len(first)
    matched_right = [-1] * len(second)
    for start in range(len(first)):
        pending = deque([start])
        seen_left = {start}
        parent_right = [-1] * len(second)
        free_right = -1

        while pending and free_right == -1:
            left_index = pending.popleft()
            for right_index in adjacency[left_index]:
                if parent_right[right_index] != -1:
                    continue
                parent_right[right_index] = left_index
                owner = matched_right[right_index]
                if owner == -1:
                    free_right = right_index
                    break
                if owner not in seen_left:
                    seen_left.add(owner)
                    pending.append(owner)

        if free_right == -1:
            return False

        # Flip the alternating path ending at the newly reached free row.
        right_index = free_right
        while right_index != -1:
            left_index = parent_right[right_index]
            previous_right = matched_left[left_index]
            matched_left[left_index] = right_index
            matched_right[right_index] = left_index
            right_index = previous_right

    return True


def _sets_equal(first: list[Any], second: list[Any]) -> bool:
    # Under tolerant equality, set equality means bidirectional coverage.  Row
    # multiplicity is deliberately irrelevant.
    first_is_covered = all(
        any(rows_equal(row, candidate) for candidate in second) for row in first
    )
    return first_is_covered and all(
        any(rows_equal(row, candidate) for candidate in first)
        for row in second
    )


__all__ = [
    "ABSOLUTE_TOLERANCE",
    "RELATIVE_TOLERANCE",
    "QueryOutcome",
    "comparison_mode",
    "observe",
    "outcomes_differ",
    "query_shape",
    "results_differ",
    "rows_equal",
    "tiebreak_query",
    "values_equal",
]
