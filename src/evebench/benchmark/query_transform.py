"""Optional SQL transformations applied to benchmark query pairs."""

from __future__ import annotations

from collections import Counter

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import build_scope
from sqlglot.tokens import TokenType

_ROW_SELECTION = {TokenType.LIMIT, TokenType.OFFSET, TokenType.FETCH}


def _identifier_key(identifier: exp.Identifier) -> tuple[bool, str]:
    quoted = bool(identifier.args.get("quoted"))
    name = identifier.name if quoted else identifier.name.casefold()
    return quoted, name


def _table_key(table: exp.Table) -> tuple[tuple[bool, str], ...]:
    return tuple(_identifier_key(part) for part in table.parts)


def _canonical_order(query: exp.Expression, order: exp.Order) -> exp.Order:
    """Normalize an order for comparison, resolving unambiguous table aliases."""
    scope = build_scope(query)
    source_counts: Counter[tuple[tuple[bool, str], ...]] = Counter()
    aliases: dict[tuple[bool, str], tuple[tuple[bool, str], ...]] = {}

    if scope is not None:
        for _, (_, source) in scope.selected_sources.items():
            if not isinstance(source, exp.Table):
                continue
            source_key = _table_key(source)
            source_counts[source_key] += 1
            alias = source.args.get("alias")
            alias_identifier = alias.this if alias is not None else source.this
            if isinstance(alias_identifier, exp.Identifier):
                aliases[_identifier_key(alias_identifier)] = source_key

    canonical = order.copy()
    for identifier in canonical.find_all(exp.Identifier):
        if not identifier.args.get("quoted"):
            identifier.set("this", identifier.name.casefold())

    for column in canonical.find_all(exp.Column):
        qualifier = column.args.get("table")
        if not isinstance(qualifier, exp.Identifier):
            continue
        source_key = aliases.get(_identifier_key(qualifier))
        if source_key is None or source_counts[source_key] != 1:
            continue
        column.set("catalog", None)
        column.set("db", None)
        column.set("table", exp.Identifier(this=repr(source_key), quoted=True))

    return canonical


def strip_shared_order_by_limit(
    query_left: str, query_right: str, dialect: str
) -> tuple[str, str]:
    """Remove matching top-level orders and shared limits and offsets.

    The clauses are cut from the original text, which is otherwise kept
    verbatim; if either cut does not parse back as intended, both queries are
    returned unchanged.
    """
    read_dialect = {"postgresql": "postgres", "mariadb": "mysql"}.get(
        dialect, dialect
    )

    try:
        left = sqlglot.parse_one(query_left, read=read_dialect)
        right = sqlglot.parse_one(query_right, read=read_dialect)
    except sqlglot.errors.ParseError:
        return query_left, query_right

    left_order, right_order = left.args.get("order"), right.args.get("order")
    left_limit, right_limit = left.args.get("limit"), right.args.get("limit")
    left_offset, right_offset = left.args.get("offset"), right.args.get("offset")

    orders_match = (
        left_order is not None
        and right_order is not None
        and _canonical_order(left, left_order)
        == _canonical_order(right, right_order)
    )
    limits_match = (
        left_limit is not None
        and right_limit is not None
        and left_limit == right_limit
        and left_offset == right_offset
    )

    if limits_match:
        left.set("limit", None)
        right.set("limit", None)
        left.set("offset", None)
        right.set("offset", None)

    order_has_unremoved_row_selection = any(
        clause is not None
        for clause in (
            left.args.get("limit"),
            right.args.get("limit"),
            left.args.get("offset"),
            right.args.get("offset"),
        )
    )
    if orders_match and not order_has_unremoved_row_selection:
        left.set("order", None)
        right.set("order", None)

    remove_order = orders_match and not order_has_unremoved_row_selection
    if not limits_match and not remove_order:
        return query_left, query_right
    stripped = tuple(
        _cut_clauses(sql, read_dialect, expected, remove_order, limits_match)
        for sql, expected in ((query_left, left), (query_right, right))
    )
    if None in stripped:
        return query_left, query_right
    return stripped


def _cut_clauses(
    sql: str,
    read_dialect: str,
    expected: exp.Expression,
    remove_order: bool,
    remove_limit: bool,
) -> str | None:
    """Cut the top-level ORDER BY and/or LIMIT/OFFSET out of the original text.

    The rest of the text is kept verbatim.  Returns ``None`` unless the result
    parses to ``expected``, the original tree without those clauses.
    """
    try:
        tokens = sqlglot.Dialect.get_or_raise(read_dialect).tokenize(sql)
    except sqlglot.errors.SqlglotError:
        return None
    depth = 0
    order_start = selection_start = None
    for token in tokens:
        if token.token_type == TokenType.L_PAREN:
            depth += 1
        elif token.token_type == TokenType.R_PAREN:
            depth -= 1
        elif depth == 0:
            if token.token_type == TokenType.ORDER_BY:
                order_start, selection_start = token.start, None
            elif token.token_type in _ROW_SELECTION and selection_start is None:
                selection_start = token.start
    last = len(tokens) - 1
    while last >= 0 and tokens[last].token_type == TokenType.SEMICOLON:
        last -= 1
    if last < 0:
        return None
    end = tokens[last].end + 1

    if (remove_order and order_start is None) or (remove_limit and selection_start is None):
        return None
    cut_start = order_start if remove_order else selection_start
    cut_end = end if remove_limit or selection_start is None else selection_start
    head, tail = sql[:cut_start].rstrip(), sql[cut_end:].lstrip()
    separator = " " if tail and not tail.startswith(";") else ""
    sql = head + separator + tail

    try:
        if sqlglot.parse_one(sql, read=read_dialect) != expected:
            return None
    except sqlglot.errors.SqlglotError:
        return None
    return sql
