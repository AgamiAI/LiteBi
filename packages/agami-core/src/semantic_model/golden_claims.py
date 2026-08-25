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
from typing import Any, Optional, Sequence

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
_UNREADABLE_UNKNOWN = "the statement could not be read"
_UNREADABLE_NOT_ONE_SELECT = (
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
        return ClaimSet(unreadable=why or _UNREADABLE_UNKNOWN)
    if not isinstance(tree, exp.Select):
        return ClaimSet(unreadable=_UNREADABLE_NOT_ONE_SELECT)

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
        date_window=_resolve_date_window(conjuncts, aliases),
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


# ---------------------------------------------------------------------------
# The temporal fold
#
# Three spellings of the same interval reach this module — a half-open comparison chain, a year
# pulled out with EXTRACT, and a BETWEEN — and a golden item that failed one of them against
# another would be failing on spelling rather than on meaning. Everything else resolves to nothing,
# and "nothing" is a real answer here: this is one of the two claims allowed to gate, so the cost
# of guessing an interval the statement did not write is a correct statement failed.
# ---------------------------------------------------------------------------

# Which bound each comparison puts on the column standing on its LEFT, and whether that bound
# includes the value. Written out rather than derived, because the mirrored form (`'2025-01-01' <=
# d`) is handled by swapping the side rather than by a second table.
_COMPARISON_BOUNDS: dict[type, tuple[str, bool]] = {
    exp.GTE: ("start", True),
    exp.GT: ("start", False),
    exp.LTE: ("end", True),
    exp.LT: ("end", False),
}


def _resolve_date_window(
    conjuncts: list["exp.Expression"], aliases: dict[str, str]
) -> Optional[DateWindow]:
    """Fold this statement's filtering conjuncts into the one interval they constrain, or None.

    None is returned wherever the fold would be a claim this module cannot stand behind: no
    temporal predicate at all, two columns carrying one (picking either would make the answer
    depend on the order the conjuncts were written in), a conjunct over the temporal column that
    did not reduce, or two bounds on the same side that disagree.

    A PARTIAL reduction is discarded whole, mirroring `runtime._reduced_on`. Half of a constraint
    reported as the whole of one is not a weaker fact than no fact — it is a false one, and it is
    the shape that fails a statement which filters correctly.
    """
    bounds: dict[str, list[tuple[str, str, bool]]] = {}
    written_as: dict[str, str] = {}
    unreduced: set[str] = set()
    for conjunct in conjuncts:
        found = _temporal_bounds(conjunct)
        if found is None:
            unreduced |= rt._predicate_columns(conjunct)
            continue
        column, pieces = found
        key = column.name.lower()
        bounds.setdefault(key, []).extend(pieces)
        written_as.setdefault(key, _expression_key(column, aliases))

    if len(bounds) != 1:
        return None
    key, pieces = next(iter(bounds.items()))
    if key in unreduced:
        return None

    edges: dict[str, tuple[Optional[str], bool]] = {"start": (None, False), "end": (None, False)}
    for side, value, inclusive in pieces:
        settled = edges[side]
        if settled[0] is not None and settled != (value, inclusive):
            return None
        edges[side] = (value, inclusive)
    return DateWindow(
        column=written_as[key],
        start=edges["start"][0],
        start_inclusive=edges["start"][1],
        end=edges["end"][0],
        end_inclusive=edges["end"][1],
    )


def _temporal_bounds(
    node: "exp.Expression",
) -> "tuple[exp.Column, list[tuple[str, str, bool]]] | None":
    """One conjunct as (the column it constrains, the bounds it puts on it), or None if this module
    does not model the shape it was written in."""
    if isinstance(node, exp.Between):
        column = node.this
        low = _date_literal(node.args.get("low"))
        high = _date_literal(node.args.get("high"))
        if isinstance(column, exp.Column) and low is not None and high is not None:
            # BETWEEN is inclusive at BOTH ends, and the upper one stays where it was written —
            # shifting it to the next day is only sound on a DATE column, and no column type
            # reaches this module.
            return column, [("start", low, True), ("end", high, True)]
        return None
    if isinstance(node, exp.EQ):
        extracted = _extracted_year(node)
        if extracted is None:
            return None
        column, year = extracted
        # A calendar year IS a half-open interval, so this folds to exactly the chain form and the
        # two spellings compare equal.
        return column, [("start", f"{year}-01-01", True), ("end", f"{year + 1}-01-01", False)]

    bound = _COMPARISON_BOUNDS.get(type(node))
    if bound is None:
        return None
    side, inclusive = bound
    if isinstance(node.this, exp.Column):
        column, value = node.this, _date_literal(node.expression)
    elif isinstance(node.expression, exp.Column):
        # `'2025-01-01' <= d` is `d >= '2025-01-01'` written the other way round, so the bound it
        # puts on the column is the mirror of the operator rather than the operator itself.
        column, value = node.expression, _date_literal(node.this)
        side = "end" if side == "start" else "start"
    else:
        return None
    if value is None:
        return None
    return column, [(side, value, inclusive)]


def _date_literal(node: "exp.Expression | None") -> Optional[str]:
    """The ISO date a node spells, as written — None when it spells anything else.

    A typed literal (`DATE '2025-01-01'`) parses as a cast over the string, so the cast is unwrapped
    and the two spellings resolve to one bound. Nothing is reformatted: the bound a report names has
    to be the bound the statement wrote.
    """
    if isinstance(node, exp.Cast):
        node = node.this
    if isinstance(node, exp.Literal) and node.is_string and _ISO_DATE.match(node.this):
        return node.this
    return None


def _extracted_year(node: "exp.EQ") -> "tuple[exp.Column, int] | None":
    """`EXTRACT(YEAR FROM col) = 2025` as (col, 2025), from either operand order.

    Only YEAR, and only against an integer literal. A quarter or a month extracted the same way is
    a real interval too, but it is one this module has no test corpus for, and an interval derived
    from a rule nobody has exercised is the kind that gates a correct statement.
    """
    for extract, other in ((node.this, node.expression), (node.expression, node.this)):
        if not isinstance(extract, exp.Extract):
            continue
        # The unit is a bare keyword rather than an identifier, so the tree-wide case fold does not
        # reach it and it is compared case-insensitively here.
        if (extract.this.name or "").upper() != "YEAR":
            continue
        column, value = extract.expression, other
        if not (isinstance(column, exp.Column) and isinstance(value, exp.Literal)):
            continue
        if not value.is_string:
            return column, int(value.this)
    return None


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


# ---------------------------------------------------------------------------
# The comparison, and the two gates
# ---------------------------------------------------------------------------

# The two gate reasons, value-free so a caller may print either one verbatim beside a failing item.
# The offending column is a FIELD rather than part of the sentence, so a renderer decides how to
# show it and the sentence itself never carries anything the caller's statement wrote.
_MUST_FILTER_REASON = (
    "the dataset requires this column to be filtered, and the generated statement constrains it in "
    "none of the predicates it writes"
)
_DATE_WINDOW_REASON = "the two statements resolve their date filters to different intervals"


@dataclass
class Claim:
    """One of the seven claims, and whether the two statements agree on it.

    `generated` and `golden` are the claim's own value on each side, in the JSON-able form
    `ClaimSet.as_dict` renders it — identifiers, bounds and counts, never a statement.
    """

    name: str
    status: str  # AGREES | DIFFERS | UNKNOWN
    generated: Any
    golden: Any

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "generated": self.generated,
            "golden": self.golden,
        }


@dataclass
class GateVerdict:
    """One of the two differences a golden item is allowed to FAIL on, rather than merely report."""

    kind: str  # "must_filter" | "date_window"
    column: Optional[str]  # the required column filtered nowhere; None for a window verdict
    reason: str  # value-free, safe to print beside a failure

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "column": self.column, "reason": self.reason}


@dataclass
class ClaimDiff:
    """What two statements say about each other: seven claims, and whatever gated."""

    claims: list[Claim]  # exactly seven, in CLAIM_NAMES order
    gates: list[GateVerdict]  # empty when nothing gates

    @property
    def gated(self) -> bool:
        return bool(self.gates)

    def as_dict(self) -> dict[str, Any]:
        return {
            "claims": [claim.as_dict() for claim in self.claims],
            "gates": [gate.as_dict() for gate in self.gates],
            "gated": self.gated,
        }


def compare_statements(
    generated_sql: str,
    golden_sql: str,
    *,
    must_filter: Sequence[str] = (),
    dialect: str,
) -> ClaimDiff:
    """Read both statements and compare them — the whole module in one call."""
    return diff_claims(
        read_claims(generated_sql, dialect=dialect),
        read_claims(golden_sql, dialect=dialect),
        must_filter=must_filter,
    )


def diff_claims(
    generated: ClaimSet, golden: ClaimSet, *, must_filter: Sequence[str] = ()
) -> ClaimDiff:
    """Compare two already-read claim sets.

    A statement that could not be read makes every claim `unknown` rather than `differs`: `differs`
    is a definite comparison, and there is nothing on one side to have compared.
    """
    unreadable = generated.unreadable is not None or golden.unreadable is not None
    generated_values, golden_values = generated.as_dict(), golden.as_dict()
    window = _window_status(generated.date_window, golden.date_window)

    claims: list[Claim] = []
    for name in CLAIM_NAMES:
        if unreadable:
            claims.append(Claim(name=name, status=UNKNOWN, generated=None, golden=None))
            continue
        # Six of the seven are decided on the RENDERED value, which `as_dict` has already sorted —
        # so every claim that is a set underneath compares order-insensitively for free, and the
        # value a reader is shown is the same value the status was decided from. The window is the
        # exception, because its own rule ignores one of its fields.
        status = (
            window
            if name == "date_window"
            else (AGREES if generated_values[name] == golden_values[name] else DIFFERS)
        )
        claims.append(
            Claim(
                name=name,
                status=status,
                generated=generated_values[name],
                golden=golden_values[name],
            )
        )
    return ClaimDiff(claims=claims, gates=_gates(generated, golden, must_filter))


def _window_status(generated: Optional[DateWindow], golden: Optional[DateWindow]) -> str:
    """`unknown` unless BOTH statements resolved a window.

    One side unresolved is not evidence of disagreement — it is this module declining to model a
    spelling — and reporting it as `differs` would hand the gate below a difference that the
    statements may not have.
    """
    if generated is None or golden is None:
        return UNKNOWN
    return AGREES if _windows_agree(generated, golden) else DIFFERS


def _windows_agree(generated: DateWindow, golden: DateWindow) -> bool:
    """Two windows agree iff their four BOUND fields are equal — the column is not compared.

    Two statements over one table may qualify the same column differently, or one may qualify it
    and the other not, and a gate that read those as two different windows would fail a correct
    rewrite on a qualifier. Which column each side constrains still rides on the claim, so a report
    can say so; it just does not decide.
    """
    return (
        generated.start,
        generated.start_inclusive,
        generated.end,
        generated.end_inclusive,
    ) == (golden.start, golden.start_inclusive, golden.end, golden.end_inclusive)


def _gates(generated: ClaimSet, golden: ClaimSet, must_filter: Sequence[str]) -> list[GateVerdict]:
    """The two differences that may fail an item, and nothing else.

    The required-column gate reads only the GENERATED statement, because `must_filter` is the
    dataset's requirement rather than a property of the golden statement — but it stays silent when
    that statement could not be read, since "constrains it nowhere" is a claim about a statement
    nobody managed to read.
    """
    verdicts: list[GateVerdict] = []
    if generated.unreadable is None:
        verdicts.extend(
            GateVerdict(kind="must_filter", column=column, reason=_MUST_FILTER_REASON)
            for column in must_filter
            if rt._tkey(rt._bare(column)) not in generated.filtered_columns
        )
    if _window_status(generated.date_window, golden.date_window) == DIFFERS:
        verdicts.append(GateVerdict(kind="date_window", column=None, reason=_DATE_WINDOW_REASON))
    return verdicts


__all__ = [
    "AGREES",
    "CLAIM_NAMES",
    "DIFFERS",
    "UNKNOWN",
    "Claim",
    "ClaimDiff",
    "ClaimSet",
    "DateWindow",
    "GateVerdict",
    "compare_statements",
    "diff_claims",
    "read_claims",
]
