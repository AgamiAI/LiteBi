"""Read a statement into structured claims, and say where two of them disagree.

A golden item that fails on its numbers says the two statements returned different rows. It cannot
say *why*, and "why" is the whole value of the failure: a window off by a quarter, a required filter
left out and a genuinely different question all look identical from a row count. This module is the
sentence after that one. It reads each statement into seven claims about what the statement asks
for, compares them claim by claim, and hands the caller a structured diff.

**It is a describer with two gates, and the split is the design.** Five of the seven claims are
REPORTED — a difference in them is a fact for a person to read, not a verdict — and exactly two are
allowed to decide anything:

* a column the dataset requires filtered that the statement constrains NOWHERE, and
* a date window that resolves, on both sides, to a different interval.

Those two were selected because neither can false-positive. A column is either mentioned in one of
the statement's own predicates or it is not, and that scan errs toward "filtered": it reads WHERE,
every join's ON (outer joins included), HAVING, QUALIFY and an aggregate's own FILTER, so a
predicate written anywhere at all disarms the gate. A window gates only when BOTH statements resolve
to one; a spelling the resolver does not model reads `unknown` and gates nothing, because failing a
correct statement on this module's own incompleteness is the one outcome a gate must never have.

Three things this module deliberately does NOT do:

* **It does not score.** There is one deterministic scorer for the eval mode, and a second one
  living here would be a second answer to the same question. This emits claims; the scorer folds
  them in.
* **It does not parse SQL itself.** Every parse goes through `runtime._parse_reporting`, the one
  helper that reads a statement in the engine's own grammar and raises rather than silently
  truncating the tree, and every normalization reuses runtime's own helpers. A second parser would
  be a second reading of the same statement, and the two would drift.
* **It does not normalize an inclusive date upper bound to the next day.** That transform is only
  sound on a `DATE` column, and nothing this module is handed carries a column type. So
  `BETWEEN '2025-01-01' AND '2025-12-31'` resolves to an upper bound of `2025-12-31` INCLUSIVE and
  the half-open year to `2026-01-01` EXCLUSIVE, and the two are reported as the different intervals
  they are on a timestamp column.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from sqlglot import expressions as exp

# Imported unguarded, unlike `runtime`, and the difference is deliberate: `runtime` is on the
# import path of the stdlib-only local serving harness and must survive sqlglot's absence, while
# nothing reaches this module except an eval run that has already parsed two statements.
from . import runtime as rt

AGREES = "agrees"
DIFFERS = "differs"
UNKNOWN = "unknown"

# Exactly seven, and the tuple is the contract: an eighth claim is a change to what a golden item
# is allowed to assert about a statement, not an implementation detail of this module. The order is
# the order a diff renders in.
CLAIM_NAMES = (
    "tables",
    "filter_predicates",
    "date_window",
    "group_keys",
    "join_keys",
    "ordering",
    "limit",
)

# Why a statement's claims were not read. Sentences rather than codes because they are printed
# beside a failing item, and value-free because the statement that produced them is the caller's.
UNREADABLE_UNKNOWN = "the statement could not be read"
UNREADABLE_NOT_ONE_SELECT = (
    "the statement is not a single SELECT, so its claims belong to its arms rather than to it"
)

# An aggregate's own row filter (`SUM(x) FILTER (WHERE …)`) — resolved by name because the package
# pins only `sqlglot>=20`, and a class this build does not declare is a shape it cannot parse.
_FILTER_NODES = rt._exp_nodes("Filter")

# A literal this module is willing to read as a date bound: an ISO calendar date, optionally
# carrying a time. Deliberately narrow — an engine-specific date expression is a spelling the
# resolver does not model, and reading one it half-understands is how a partial interval gets
# reported as a whole one.
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T].*)?$")


@dataclass(frozen=True)
class DateWindow:
    """The interval a statement's temporal predicates resolve to, over one column.

    `column` rides along so a report can name what moved, and is NOT part of agreement — see
    `_windows_agree`. Both bounds are the date AS WRITTEN, never shifted.
    """

    column: str
    start: Optional[str]  # None for an open lower bound
    start_inclusive: bool
    end: Optional[str]  # None for an open upper bound
    end_inclusive: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "column": self.column,
            "start": self.start,
            "start_inclusive": self.start_inclusive,
            "end": self.end,
            "end_inclusive": self.end_inclusive,
        }


@dataclass
class ClaimSet:
    """What one statement asks for, in the seven terms two statements are compared in.

    Every field defaults to its own empty value so that `ClaimSet(unreadable=…)` is the whole of
    the unreadable case; `read_claims` is the only constructor, and it always fills all of them.
    """

    tables: frozenset[str] = frozenset()  # bare, case-folded
    filter_predicates: frozenset[str] = frozenset()  # normalized keys, not the statement's text
    # Every column any predicate the statement writes mentions — the `must_filter` gate's input,
    # and a different question from the one above: *is this column constrained anywhere* rather
    # than *do these two statements constrain the same way*. Derived on the same walk, because the
    # parse-exactly-once discipline is why `_parse_reporting` exists.
    filtered_columns: frozenset[str] = frozenset()
    date_window: Optional[DateWindow] = None
    group_keys: tuple[str, ...] = ()
    join_keys: frozenset[frozenset[tuple[str, str]]] = frozenset()
    ordering: tuple[tuple[str, str], ...] = ()  # (column, "asc" | "desc"), in the written order
    limit: Optional[int] = None
    # A sentence when the statement could not be read, None when it was — so that an empty claim is
    # never asked to mean both "the statement constrains nothing" and "we could not tell".
    unreadable: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "tables": sorted(self.tables),
            "filter_predicates": sorted(self.filter_predicates),
            "filtered_columns": sorted(self.filtered_columns),
            "date_window": self.date_window.as_dict() if self.date_window else None,
            "group_keys": list(self.group_keys),
            "join_keys": _join_keys_as_list(self.join_keys),
            "ordering": [list(pair) for pair in self.ordering],
            "limit": self.limit,
            "unreadable": self.unreadable,
        }


def _join_keys_as_list(keys: "frozenset[frozenset[tuple[str, str]]]") -> list[list[list[str]]]:
    """The join-keys claim as nested sorted lists, so `json.dumps` accepts it and two runs over the
    same statement render it identically."""
    return sorted(sorted([table, column] for table, column in pair) for pair in keys)


def read_claims(sql: str, *, dialect: str) -> ClaimSet:
    """Read one statement into its seven claims. Never raises: an input this module cannot read
    comes back as a `ClaimSet` whose `unreadable` says so."""
    tree, why = rt._parse_reporting(sql, dialect=dialect)
    if tree is None:
        # `_parse_reporting` reports None for both a parse failure and a build without sqlglot;
        # only the first carries a sentence, and the caller is owed one either way.
        return ClaimSet(unreadable=why or UNREADABLE_UNKNOWN)
    if not isinstance(tree, exp.Select):
        return ClaimSet(unreadable=UNREADABLE_NOT_ONE_SELECT)

    # Folded ONCE, into a copy, and every claim below is derived from that copy. Unquoted
    # identifiers fold case in SQL, so `O.REGION` and `o.region` are one column; a quoted one does
    # not, and neither does a string literal — `status != 'Test'` and `status != 'test'` select
    # different rows, and a normalizer that flattened both would report two statements as agreeing
    # on a filter that keeps different rows.
    select = rt._fold_unquoted_identifiers(tree)
    # This SELECT's OWN sources, and not the subtree-wide map: the latter is last-wins across CTE
    # bodies and nested subqueries, so a qualifier could resolve to a table this query never read.
    aliases = rt._own_alias_map(select)
    conjuncts = rt._filtering_conjuncts(select)

    return ClaimSet(
        tables=frozenset(rt._tkey(ref.bare) for ref in rt._table_references(select)),
        filter_predicates=frozenset(_expression_key(node, aliases) for node in conjuncts),
        filtered_columns=_constrained_columns(select),
        date_window=None,
        group_keys=_group_keys(select, aliases),
        join_keys=_join_keys(select, aliases),
        ordering=_ordering(select, aliases),
        limit=_limit(select),
    )


def _expression_key(node: "exp.Expression", aliases: dict[str, str]) -> str:
    """One expression reduced to the key two statements compare it by.

    Qualifiers are resolved to the table they name, so an alias rewrite changes no key — that, and
    the case fold already applied to the tree, are the whole of what makes a re-spelling of the same
    question produce the same claims. The result is regenerated from the tree rather than sliced out
    of the caller's SQL, and bounded by `_echo_expr`, because a claim rides on tool output that the
    calling model reads as server-authored.
    """
    return rt._echo_expr(_resolve_qualifiers(node, aliases).sql())


def _resolve_qualifiers(node: "exp.Expression", aliases: dict[str, str]) -> "exp.Expression":
    """A COPY of `node` with every column qualifier rewritten to the bare table it names."""
    resolved = node.copy()
    for column in resolved.find_all(exp.Column):
        qualifier = column.table
        if not qualifier:
            continue
        resolved_table = rt._tkey(rt._bare(aliases.get(qualifier, qualifier)))
        column.set("table", exp.to_identifier(resolved_table))
        # The schema and catalog parts go with it: `sales.orders.region` and `orders.region` name
        # one column, and `_bare` has already stripped the schema off the resolved table name, so
        # leaving them on would make the two spellings two different keys.
        column.set("db", None)
        column.set("catalog", None)
    return resolved


def _constrained_columns(select: "exp.Select") -> frozenset[str]:
    """Every column any predicate ANYWHERE in the statement mentions, bare and case-folded.

    Whole-statement rather than per-scope, and every predicate rather than only the filtering ones,
    because this feeds a gate that fires on an ABSENCE. Erring toward "this column is constrained"
    is the direction that cannot produce a false gate; erring the other way would fail a statement
    that filters correctly in a clause this walk declined to look at.
    """
    columns: set[str] = set()
    for scope in select.find_all(exp.Select):
        for predicate in rt._mentioned_predicates(scope):
            columns |= rt._predicate_columns(predicate)
    # An aggregate's own row filter is not a clause of any SELECT, so `_mentioned_predicates` does
    # not reach it — and a statement that moved its WHERE into `SUM(x) FILTER (WHERE …)` has still
    # plainly constrained the column.
    for aggregate_filter in select.find_all(*_FILTER_NODES):
        where = aggregate_filter.expression
        if where is not None:
            columns |= rt._predicate_columns(where)
    return frozenset(columns)


def _group_keys(select: "exp.Select", aliases: dict[str, str]) -> tuple[str, ...]:
    """The GROUP BY keys, SORTED — a grouping is a set, and reordering it returns the same rows, so
    comparing it as written would report a difference that changes no answer."""
    group = select.args.get("group")
    if group is None:
        return ()
    return tuple(sorted(_expression_key(node, aliases) for node in group.expressions))


def _ordering(select: "exp.Select", aliases: dict[str, str]) -> tuple[tuple[str, str], ...]:
    """The ORDER BY terms in the order they were WRITTEN, which is the opposite decision from the
    grouping above and for the opposite reason: two statements sorting by the same two columns in
    opposite order hand back their rows in a different sequence."""
    order = select.args.get("order")
    if order is None:
        return ()
    return tuple(
        (_expression_key(term.this, aliases), "desc" if term.args.get("desc") else "asc")
        for term in order.expressions
    )


def _limit(select: "exp.Select") -> Optional[int]:
    """The row limit, when the statement writes one as a plain integer. A computed or parameterized
    limit is not a number this module can compare, so it reads as no limit rather than as a wrong
    one."""
    limit = select.args.get("limit")
    if limit is None:
        return None
    value = limit.expression
    if isinstance(value, exp.Literal) and not value.is_string:
        return int(value.this)
    return None


def _join_keys(
    select: "exp.Select", aliases: dict[str, str]
) -> "frozenset[frozenset[tuple[str, str]]]":
    """The column pairs this SELECT's joins join on, already order-insensitive.

    Every join's ON, outer ones included: which columns two tables are matched on is the same fact
    whether or not the join keeps unmatched rows, and the join's *kind* is not one of the seven
    claims.
    """
    pairs: set[frozenset[tuple[str, str]]] = set()
    for join in select.args.get("joins") or []:
        on = join.args.get("on")
        if on is not None:
            pairs |= rt._predicate_pairs(on, aliases)
    return frozenset(pairs)


__all__ = [
    "AGREES",
    "CLAIM_NAMES",
    "DIFFERS",
    "UNKNOWN",
    "ClaimSet",
    "DateWindow",
    "read_claims",
]
