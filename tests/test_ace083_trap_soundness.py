"""ACE-083 — the shapes a soundness fix may never report clean, pinned before any fix lands.

ACE-083 makes the multiplication report say what a join actually multiplies. Three of its four
corrections are LOOSENINGS: an aggregate a duplication cannot move stops being called a trap, a
many-side column that is not on the value path stops attributing the fan, and a reference the
statement cannot see stops entering the alias map. Each one narrows what gets reported, and the
whole existing battery guards the other direction — every shipped assertion says "this IS reported"
and not one of them says "this is still not reported clean".

That asymmetry is the hazard. A loosening that goes one step too far turns a genuinely inflated
number into `not_multiplied`, which is a positive claim on the receipt that the number is sound.
Nothing on `main` fails when that happens. So the reverse corpus lands FIRST, in its own file,
before a line of production code moves.

The reverse corpus passes on the base implementation. That is the point: those are not the tests for
the new behaviour, they are the tests for the behaviour that must survive it. The batteries below it
are the per-slice tests of what each correction actually changed, and they land beside the corpus
rather than in files of their own so that a loosening and the pins guarding it are read together.

Slice 2 (S1) is here: an aggregate a duplication cannot move keeps the multiplication and loses the
word trap, and the marker sentence that reported the detector's old shortcoming is deleted.

Slice 3 (S2) is here: the fan is attributed by where a column sits on the aggregate's VALUE path
rather than by its presence anywhere inside the aggregate.

Slices 4 and 5 (S3) are here, and they are ONE commit rather than two. The scope filter and the
grain plumbing were planned as separate slices; they cannot be, and the corpus is what proved it.
The filter stops a reference written inside a CTE body from entering the outer query's map, which
is correct and is half of S3 — but `CTE_LAUNDERED_FAN` reported `multiplied` at HEAD only BECAUSE
of that leak, and with the leak gone and no resolution for what `oi` stands for, a genuinely
inflated statement read `not_multiplied`. The intermediate state fails the corpus's own criterion,
so it is not a legal place to stop.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import get_args

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

from sqlglot import exp, parse_one  # noqa: E402

# `rt` is imported FROM the fixture's module rather than beside it, and that is load-bearing.
# `test_semantic_model_runtime` puts `plugins/agami/scripts` on `sys.path`, while the ACE-060 and
# ACE-099 batteries put `packages/agami-core/src` there. Importing `semantic_model.runtime` down a
# different path than the fixture came down yields a second module object with its own
# `MULTIPLIED` / `NOT_MULTIPLIED` string objects and its own detector — so an assertion here could
# compare a status produced by one copy against a constant defined by the other. One import.
from test_semantic_model_runtime import _sales_org, m, rt  # noqa: E402

# Where the subprocess probe below finds the SAME source `rt` was imported from, derived from the
# module object rather than rebuilt from the repo root. The header above explains why two copies of
# `semantic_model` reached down two `sys.path` entries is a real hazard in this file; a determinism
# test that compared a child process running one copy against a parent running the other would be
# measuring the wrong difference.
PKG_SRC = Path(rt.__file__).resolve().parents[1]

# The one-to-many the whole corpus is built on: `orders` is the ONE side, `order_items` the MANY,
# and the model declares that edge. Written once so "the same fan join in every member" is a fact
# of the code rather than of the typing.
FAN_JOIN = "FROM orders JOIN order_items ON order_items.order_id = orders.id"

# 1. The plain fan. The shape every other member is a disguise of.
PLAIN_FAN = f"SELECT SUM(orders.total_amount) {FAN_JOIN}"

# 2. The measure wrapped in arithmetic. The value path is still a one-side column; scaling every
#    duplicated row scales the inflated total.
SCALED_FAN = f"SELECT SUM(orders.total_amount * 1.1) {FAN_JOIN}"

# 3. The many-side column is the CASE predicate, not the value. A value-path rule that attributes
#    the fan to `order_items` here would clear a total that is still summed once per item.
CONDITIONED_FAN = (
    f"SELECT SUM(CASE WHEN order_items.quantity > 0 THEN orders.total_amount END) {FAN_JOIN}"
)

# 4. The many-side column is the ordering arm. Same trap as 3, on the branch that reads an
#    `exp.Order` rather than an `exp.Case`, and the concatenation genuinely repeats each status.
ORDERED_FAN = f"SELECT STRING_AGG(orders.status, ',' ORDER BY order_items.id) {FAN_JOIN}"

# 5. The many side behind a derived table. `d` is an `exp.Subquery` and binds no table name of its
#    own, so a scope filter that drops what it cannot bind drops `order_items` — and the fan with
#    it. This is the member that measures the false clean the filter itself creates.
DERIVED_TABLE_FAN = (
    "SELECT SUM(orders.total_amount) FROM orders "
    "JOIN (SELECT order_id FROM order_items) d ON d.order_id = orders.id"
)

# 6. The many side behind a CTE that changes nothing. `oi` is one grain-preserving hop from
#    `order_items`, so the join still multiplies `orders` exactly as member 1 does.
CTE_LAUNDERED_FAN = (
    "WITH oi AS (SELECT * FROM order_items) "
    "SELECT SUM(orders.total_amount) FROM orders JOIN oi ON oi.order_id = orders.id"
)

# 7. The join is inside the CTE body, so the outer statement joins nothing and the rows behind
#    `SUM(j.total_amount)` were multiplied where the walk does not look. Nothing about this one is
#    determinable, which is a different answer from clean and must stay a different answer.
JOINED_CTE_BODY = (
    "WITH j AS (SELECT o.id AS id, o.total_amount AS total_amount FROM orders o "
    "JOIN order_items i ON i.order_id = o.id) SELECT SUM(j.total_amount) FROM j"
)

# 8. The rows come from a source with no name at all. A `VALUES` list in a comma join multiplies
#    every order by the number of tuples in it, and there is no alias to hang the fact on. Added
#    when the scope filter's own alias guard was measured: the guard has to drop the wrapper of a
#    parenthesized table, and dropping this one with it would have been a false clean.
UNALIASED_VALUES_FAN = "SELECT SUM(orders.total_amount) FROM orders, (VALUES (1), (2))"

# The corpus, named so the later slices can re-run it unchanged rather than restating it. Labels
# are what a failure prints, so they name the disguise and not the SQL.
INFLATED_SHAPES = [
    ("plain fan", PLAIN_FAN),
    ("measure scaled by a literal", SCALED_FAN),
    ("many side in the CASE predicate", CONDITIONED_FAN),
    ("many side in the ORDER BY arm", ORDERED_FAN),
    ("many side behind a derived table", DERIVED_TABLE_FAN),
    ("many side behind a CTE", CTE_LAUNDERED_FAN),
    ("join inside the CTE body", JOINED_CTE_BODY),
    ("rows from an unaliased VALUES list", UNALIASED_VALUES_FAN),
]

# A conditional count and a conditional sum over the many side. Both are honestly clean: one row
# per order item is exactly what they count, so the join multiplies nothing they read.
CONDITIONAL_COUNT = f"SELECT COUNT(CASE WHEN order_items.quantity > 0 THEN 1 END) {FAN_JOIN}"
CONDITIONAL_SUM = (
    f"SELECT SUM(CASE WHEN order_items.quantity > 0 THEN 1 ELSE 0 END) {FAN_JOIN}"
)

FAN_EDGE = "orders (1) <- order_items (N)"

# --- S2: the value path ----------------------------------------------------
#
# The statement S2 is about. Its value is a product computed once per `order_items` row, so the
# duplication the join performs is the grain the value was already at.
VALUE_AT_MANY_GRAIN = f"SELECT SUM(order_items.quantity * orders.total_amount) {FAN_JOIN}"

# The same one-side measure with the many-side column moved OUT of the aggregate entirely, into a
# `FILTER (WHERE …)` clause. Structurally different from every other member here — see the test.
FILTERED_FAN = (
    f"SELECT SUM(orders.total_amount) FILTER (WHERE order_items.quantity > 0) {FAN_JOIN}"
)

# One statement carrying all three statuses and both risk labels, for the cross-process pin. Four
# aggregates over one fan: a value already at the many side's grain, a one-side measure the join
# inflates, a one-side measure the duplication cannot move, and one that names no column at all.
EVERY_STATUS_SQL = (
    "SELECT SUM(i.quantity * o.total_amount), SUM(o.total_amount), MIN(o.total_amount), COUNT(*) "
    "FROM orders o JOIN order_items i ON i.order_id = o.id"
)

# Every branch of `_value_columns`, exercised on the function directly. `expected` is a LIST and not
# a set: the walk's order decides the order of nothing the receipt renders today, but `sources` is
# built from it and a list says what was measured rather than what happened to be enough.
VALUE_COLUMN_CASES = [
    ("a bare column", "orders.total_amount", ["orders.total_amount"]),
    ("arithmetic, the generic branch",
     "SUM(order_items.quantity * orders.total_amount)",
     ["order_items.quantity", "orders.total_amount"]),
    ("a searched CASE, whose WHEN is a predicate",
     "SUM(CASE WHEN order_items.quantity > 0 THEN orders.total_amount END)",
     ["orders.total_amount"]),
    ("a searched CASE with an ELSE, which is a value",
     "SUM(CASE WHEN order_items.quantity > 0 THEN orders.total_amount ELSE orders.revenue END)",
     ["orders.total_amount", "orders.revenue"]),
    ("a simple CASE, whose operand is a predicate input",
     "SUM(CASE orders.status WHEN 'x' THEN order_items.quantity END)",
     ["order_items.quantity"]),
    ("a CASE inside a CASE branch",
     "SUM(CASE WHEN order_items.quantity > 0 "
     "THEN CASE WHEN orders.flag THEN orders.total_amount END END)",
     ["orders.total_amount"]),
    ("an ordering arm, which is neither predicate nor value",
     "STRING_AGG(orders.status, ',' ORDER BY order_items.id)",
     ["orders.status"]),
    ("DISTINCT, which the generic branch reaches with no case of its own",
     "SUM(DISTINCT orders.total_amount)",
     ["orders.total_amount"]),
    ("no column at all", "COUNT(*)", []),
]

# --- S1: the aggregates a duplication cannot move --------------------------
#
# Six spellings of one property. Duplicating a row does not move a minimum, a maximum, an aggregate
# over the distinct values it was handed, or a fold over booleans. All six sit on the ONE side of
# the same fan as `PLAIN_FAN`, so the multiplication behind them is identical and the only thing
# that differs is whether the number moved with it.
DISTINCT_COUNT_OVER_FAN = f"SELECT COUNT(DISTINCT orders.id) {FAN_JOIN}"
MIN_OVER_FAN = f"SELECT MIN(orders.total_amount) {FAN_JOIN}"
MAX_OVER_FAN = f"SELECT MAX(orders.total_amount) {FAN_JOIN}"
# The spelling the predicate is most easily got wrong on: sqlglot parses this to
# `exp.Sum(this=exp.Distinct(...))` and leaves `args["distinct"]` at `None`, so a check written
# against the arg alone sees no DISTINCT here at all.
DISTINCT_SUM_OVER_FAN = f"SELECT SUM(DISTINCT orders.total_amount) {FAN_JOIN}"
# The boolean folds, which are `exp.LogicalOr` / `exp.LogicalAnd` whichever dialect wrote them and
# whatever it called them. They are echoed on the receipt as `LOGICAL_OR` / `LOGICAL_AND`.
BOOL_OR_OVER_FAN = f"SELECT BOOL_OR(orders.total_amount > 0) {FAN_JOIN}"
BOOL_AND_OVER_FAN = f"SELECT BOOL_AND(orders.total_amount > 0) {FAN_JOIN}"

# And four over the same fan that a duplication does move. `PLAIN_FAN` is the SUM.
AVG_OVER_FAN = f"SELECT AVG(orders.total_amount) {FAN_JOIN}"
COUNT_COLUMN_OVER_FAN = f"SELECT COUNT(orders.id) {FAN_JOIN}"
# Echoed as the sqlglot-normalized `GROUP_CONCAT(...)`, not as the `STRING_AGG` that was written.
STRING_AGG_OVER_FAN = f"SELECT STRING_AGG(orders.status, ',') {FAN_JOIN}"

# `COUNT(*)` names no column, so ACE-060's rule resolves it to no table and it settles as
# `undetermined` before any fan is considered.
COUNT_STAR_OVER_FAN = f"SELECT COUNT(*) {FAN_JOIN}"

# --- the marker, after the fan-immune clause is deleted --------------------
#
# Four statements, one per state `_aggregates_marker` can be in that this spec could have disturbed,
# each carrying an aggregate a duplication cannot move so that the deleted clause would fire if it
# were still there. The fifth clause (the cap's "further aggregate(s) are not listed") is pinned by
# `test_ace060_trap_free_aggregates.py::test_the_cap_counts_aggregates_and_says_so`.
MARKER_NULL = "SELECT MIN(orders.total_amount) FROM orders"
MARKER_NESTED_SCOPE = (
    "WITH x AS (SELECT SUM(orders.total_amount) AS t FROM orders) SELECT MAX(x.t) FROM x"
)
MARKER_FILTER_OR_SORT = (
    "SELECT orders.id, MAX(orders.total_amount) FROM orders GROUP BY orders.id "
    "HAVING SUM(orders.total_amount) > 1"
)
MARKER_UNRESOLVED = (
    "SELECT COUNT(*), MIN(orders.total_amount) FROM customers "
    "JOIN orders ON orders.customer_id = customers.id"
)

# The sentence this slice deleted, quoted by the fragment that identifies it rather than in full, so
# that a reworded survivor cannot accidentally re-satisfy the absence assertions below.
DELETED_MARKER_PHRASE = "MIN, MAX and COUNT(DISTINCT)"


def _reports(sql: str) -> list["rt.AggregateReport"]:
    """Every aggregate report for `sql` against the sales model, with the analysis proved to run.

    `unchecked` is asserted here rather than in each test because an empty aggregate list is also
    what an unparseable statement returns, and a corpus that silently stopped parsing would pass
    every "is not reported clean" assertion below by computing nothing at all.
    """
    pf = rt.pre_flight_check(sql, _sales_org())
    assert pf.unchecked is None, pf.unchecked
    assert pf.aggregates, sql
    return pf.aggregates


@pytest.mark.parametrize("label,sql", INFLATED_SHAPES)
def test_no_known_inflated_shape_is_ever_reported_clean(label, sql):
    """A17 — for every shape a join is known to inflate, no aggregate says `not_multiplied`.

    The assertion is a PROPERTY over the whole corpus, not a per-case expected value, and that
    choice is the test. Three of ACE-083's four corrections are loosenings; each one is correct in
    a direction nothing had asserted, and the failure mode they share is the same one: a shape that
    used to report `multiplied` quietly starts reporting `not_multiplied`. Pinning each member's
    exact status would freeze answers this spec is deliberately changing (a member may legitimately
    move between `multiplied` and `undetermined` as the scope and grain work lands) and would still
    not say the one thing that must hold across all seven.

    `multiplied` and `undetermined` are both acceptable answers here. What is not acceptable is the
    positive claim that a number a join inflated is sound: `undetermined` declines to answer, and a
    reader can act on that, while `not_multiplied` is a receipt asserting something false.
    """
    statuses = [(a.aggregate, a.status) for a in _reports(sql)]
    assert all(s != rt.NOT_MULTIPLIED for _, s in statuses), (label, statuses)


@pytest.mark.parametrize("label,sql", [
    ("the CASE predicate", CONDITIONED_FAN),
    ("the ORDER BY arm", ORDERED_FAN),
])
def test_a_many_side_column_off_the_value_path_still_reports_the_multiplication(label, sql):
    """A8, A9 — a many-side column that is not the value does not clear the aggregate.

    These are the regression the value-path rule must not cause. That rule attributes the fan by
    the columns the aggregate's VALUE is computed from, so that
    `SUM(order_items.quantity * orders.total_amount)` — already at the many side's grain — stops
    being called a fan trap. Both statements here put a many-side column inside the aggregate
    without putting it on the value path: one is a filter on which rows contribute, the other is
    the order the values are concatenated in. In both the summed and concatenated value is a
    one-side column, once per order item, and the join multiplies it.

    Asserted on `status` and `joins` rather than on the finding's `risk`, because the risk label is
    exactly what a later slice splits: an aggregate a duplication cannot move keeps the
    multiplication and loses the word trap. What may never move is that the report says the rows
    were multiplied, and names the edge that did it.
    """
    reports = _reports(sql)
    assert [a.status for a in reports] == [rt.MULTIPLIED], (label, reports)
    assert reports[0].joins == [FAN_EDGE], (label, reports[0].joins)


@pytest.mark.parametrize("label,sql", [
    ("COUNT over a conditional literal", CONDITIONAL_COUNT),
    ("SUM over a conditional literal", CONDITIONAL_SUM),
])
def test_a_conditional_count_over_the_many_side_still_says_it_is_clean(label, sql):
    """These two are honestly clean, and a value-path rule applied too widely would lose that.

    Each counts order items, one row per item, which is precisely the grain the join produces. No
    fan fires because the only table either reads is the many side, and `not_multiplied` is the
    true answer.

    They are pinned because they are the measured cost of one tempting simplification. The value
    path also decides `sources`; if it were made to decide `resolved` as well, both of these would
    flip to `undetermined`, because their value path is a bare literal and "no columns on the value
    path" is indistinguishable from "no columns at all" to a `bool(cols)` test. That degradation
    breaks no existing assertion and would surface only as two receipts that stopped answering a
    question they used to answer correctly. This is what catches it.

    Unreachable before this spec: `_sales_org` declared no tables, so ACE-060's `visible` set was
    empty and no aggregate on this model could report `not_multiplied` at all.
    """
    reports = _reports(sql)
    assert [a.status for a in reports] == [rt.NOT_MULTIPLIED], (label, reports)
    assert [f.risk for f in reports[0].findings] == [], (label, reports[0].findings)


def _every_risk(sql: str) -> list[str]:
    """Every risk label the pre-flight produced for `sql`, from BOTH channels it reports on.

    `PreFlightResult.findings` is the flat list the CLI reads and `.aggregates[].findings` is the
    per-aggregate roster the receipt reads. They are meant to be projections of one analysis, so a
    test that read only one of them would pass while the other still said `fan_trap` at whichever
    surface it did not check.
    """
    pf = rt.pre_flight_check(sql, _sales_org())
    assert pf.unchecked is None, pf.unchecked
    return [f.risk for f in pf.findings] + [f.risk for a in pf.aggregates for f in a.findings]


# --- S1: the fan is reported, and it is not called a trap -------------------


@pytest.mark.parametrize("label,sql", [
    ("COUNT(DISTINCT)", DISTINCT_COUNT_OVER_FAN),
    ("MIN", MIN_OVER_FAN),
    ("MAX", MAX_OVER_FAN),
    ("SUM(DISTINCT)", DISTINCT_SUM_OVER_FAN),
    ("BOOL_OR", BOOL_OR_OVER_FAN),
    ("BOOL_AND", BOOL_AND_OVER_FAN),
])
def test_an_aggregate_a_duplication_cannot_move_reports_the_fan_without_calling_it_a_trap(
    label, sql,
):
    """A1-A5. The multiplication survives; the word `trap` does not.

    The rows behind a `MAX` over the one side of a fan really are duplicated, so the report keeps
    saying `multiplied` and keeps naming the edge that did it. Suppressing the finding instead would
    make the item say `not_multiplied`, which is the positive claim that the rows were not
    duplicated, and they were. What was false is only the label: `fan_trap` names a defect, and
    there is no defect in a number a duplication cannot move. So `fan_out_invariant` is a second
    derivable property of the same aggregate reported alongside the first fact, not a reason to drop
    it — which is why it is a member of `_MULTIPLYING_RISKS` and `status` is untouched.

    `joins` is asserted because it is what makes the finding actionable at all: a reader told the
    rows were multiplied and not told by what has been given a fact they cannot check. It comes off
    the same shared `triggering_joins` the trap branch builds, so the two labels describe one edge.

    The `BOOL_OR` / `BOOL_AND` cases are the `exp.LogicalOr` / `exp.LogicalAnd` arms of the
    predicate, and `SUM(DISTINCT)` is the arm that only `isinstance(agg.this, exp.Distinct)`
    reaches. All three are TYPE tests: ACE-079 reads each statement in its engine's own dialect, so
    the written function name is whatever that dialect spells the fold, and a name allowlist would
    be wrong the first time the two differ.
    """
    reports = _reports(sql)
    assert [a.status for a in reports] == [rt.MULTIPLIED], (label, reports)
    assert reports[0].joins == [FAN_EDGE], (label, reports[0].joins)
    assert [f.risk for f in reports[0].findings] == ["fan_out_invariant"], (label, reports)
    assert "fan_trap" not in _every_risk(sql), (label, _every_risk(sql))


@pytest.mark.parametrize("label,sql", [
    ("SUM", PLAIN_FAN),
    ("AVG", AVG_OVER_FAN),
    ("COUNT of a column", COUNT_COLUMN_OVER_FAN),
    ("STRING_AGG", STRING_AGG_OVER_FAN),
])
def test_an_aggregate_a_duplication_moves_carries_no_invariance(label, sql):
    """A6. The other half of the split, and the half that must not move at all.

    Each of these four returns a different number when its rows are duplicated: the sum and the
    average shift, the count counts line items instead of orders, and the concatenation repeats
    every status once per item. Whatever widened the invariance predicate, it may not reach them —
    an aggregate wrongly labelled `fan_out_invariant` tells a reader the number is the same either
    way, which is the one false statement this split makes possible and the reason the negative
    assertion is here rather than left implicit in the positive one above.
    """
    reports = _reports(sql)
    assert [a.status for a in reports] == [rt.MULTIPLIED], (label, reports)
    assert [f.risk for f in reports[0].findings] == ["fan_trap"], (label, reports)
    assert "fan_out_invariant" not in _every_risk(sql), (label, _every_risk(sql))


def test_count_star_over_a_fan_stays_undetermined_and_claims_no_invariance():
    """A6. `COUNT(*)` names no column, so there is nothing to be invariant about.

    ACE-060's rule is that an aggregate whose reads the analysis could not attribute to a table says
    it could not tell, rather than claiming to be clean. `COUNT(*)` is that case exactly, and it is
    reached before any fan is considered — no source table means no measure table, so the fan branch
    never looks at it and the invariance predicate is never asked.

    It is pinned because `COUNT(*)` parses to `exp.Count(this=exp.Star())`, one node type away from
    the `exp.Count(this=exp.Distinct())` the predicate does accept. A widening that stopped
    distinguishing them would attach `fan_out_invariant` here, and because that risk is a member of
    `_MULTIPLYING_RISKS` the status would flip from `undetermined` to `multiplied` — turning "we
    could not tell" into an assertion, over a join that genuinely does multiply what it counts.
    """
    reports = _reports(COUNT_STAR_OVER_FAN)
    assert [(a.aggregate, a.status) for a in reports] == [("COUNT(*)", rt.UNDETERMINED)], reports
    assert reports[0].joins == [], reports[0].joins
    assert _every_risk(COUNT_STAR_OVER_FAN) == [], _every_risk(COUNT_STAR_OVER_FAN)


# --- A21: what the marker says once the detector no longer has that gap -----


@pytest.mark.parametrize("label,sql,phrase", [
    ("nothing left unsaid", MARKER_NULL, None),
    ("an aggregate in a CTE body", MARKER_NESTED_SCOPE, "CTE or a subquery"),
    ("an aggregate in HAVING", MARKER_FILTER_OR_SORT, "HAVING or ORDER BY"),
    ("an aggregate that resolved to no table", MARKER_UNRESOLVED, "could not be resolved"),
])
def test_the_marker_stops_calling_an_invariant_aggregate_a_fan_out_risk(label, sql, phrase):
    """A21. The deleted clause is gone; the four it stood among are unchanged.

    It said "MIN, MAX and COUNT(DISTINCT) are counted as fan-out risks although a fan-out cannot
    change what they return" — a true statement about a shortcoming in the DETECTOR, which is why
    ACE-060 put it on the marker rather than on an item. This slice removed the shortcoming: those
    aggregates now carry `fan_out_invariant` on the item itself, where the reader is already
    looking. Left in place the sentence would report a gap the analysis no longer has, and by the
    four-state contract that costs the section its null marker, which is the only way it can make
    the positive claim "established, here it is".

    Nothing else moves, and the other clauses are pinned here rather than assumed because they were
    deleted from a shared tuple: each of the three remaining conditional clauses is still true of
    the statements it fires on. An aggregate inside a CTE or a subquery is still not reported, one
    in `HAVING` or `ORDER BY` is still not reported, and one that resolved to no table still says
    so. Every statement here carries a `MIN` or a `MAX` in its output list, so the deleted clause
    would fire on all four if it survived — including the null case, which is the state it made
    unreachable for every statement containing one.
    """
    marker = rt.assemble_receipt(_sales_org(), sql)["aggregates"]["undetermined"]
    assert (marker is None) == (phrase is None), (label, marker)
    assert phrase is None or phrase in marker, (label, marker)
    assert DELETED_MARKER_PHRASE not in (marker or ""), (label, marker)


# --- A22: a fact about correctness is still not a refusal -------------------


def test_no_correctness_finding_can_become_a_refusal():
    """A22. `fan_out_invariant` is not a refusal reason, and the type is what makes it so.

    Asserted against `guardrail.RefusalReason` itself rather than by searching the tree for the
    string, because the question is not whether some path happens to refuse today. It is whether one
    could: the reason vocabulary is a closed three-member `Literal`, so a correctness finding has no
    member to become, and adding a fourth means editing that one line in a diff a reviewer reads.
    ACE-094 made a multiplication a fact rather than a refusal, and this slice adds a member to the
    fact vocabulary — the assertion is that the new member landed on the side of that line it was
    meant to.

    `rt.guardrail` and not a fresh import: it is the module object `runtime` itself resolved, so
    this cannot pass against a second copy of `guardrail` reached down a different `sys.path` entry
    than the detector came down.

    The disjointness check covers `_MULTIPLYING_RISKS` whole rather than the new member alone, so
    the next risk added to it is held to the same rule without anyone remembering to come back here.
    """
    reasons = get_args(rt.guardrail.RefusalReason)
    assert reasons == ("unsafe", "out_of_scope", "undetermined"), reasons
    assert set(rt._MULTIPLYING_RISKS).isdisjoint(reasons), rt._MULTIPLYING_RISKS
    assert "fan_out_invariant" in rt._MULTIPLYING_RISKS, rt._MULTIPLYING_RISKS
    # And end to end: the statement that produces it produces a finding, not a refusal.
    pf = rt.pre_flight_check(MIN_OVER_FAN, _sales_org())
    assert {f.risk for f in pf.findings} == {"fan_out_invariant"}, pf.findings
    assert pf.unchecked is None, pf.unchecked


# --- S2: the fan is attributed by position on the value path ----------------


def test_a_value_at_many_side_grain_is_not_multiplied_by_the_fan():
    """A7. The join duplicates the rows, and the value was already one per duplicated row.

    `SUM(order_items.quantity * orders.total_amount)` computes one product per `order_items` row.
    `orders.total_amount` appears in it as a scalar co-factor: the join hands the expression the
    same amount once per item, and multiplying each item's quantity by it and summing is the same
    number the statement asked for. The duplication IS the grain the value is defined at, so there
    is nothing for the fan to inflate.

    The old rule attributed the fan by SYNTACTIC presence — every table with a column anywhere
    inside the aggregate — so this reported `fan_trap` on the strength of `orders` being named. That
    is a false statement on the receipt about a correct statement, and the cost of it is the one
    that matters: a reader who is told a sound number was inflated learns to discount the report.

    `joins` is asserted empty as well as the status, because the two are separable. A report that
    said `not_multiplied` while still naming the edge would be internally contradictory, and it is
    the second half of that pair that a rule attributing by presence would leave behind.
    """
    reports = _reports(VALUE_AT_MANY_GRAIN)
    assert [a.status for a in reports] == [rt.NOT_MULTIPLIED], reports
    assert reports[0].joins == [], reports[0].joins
    assert [f.risk for f in reports[0].findings] == [], reports[0].findings


def test_a_filter_clause_predicate_is_outside_the_aggregate_the_analysis_reads():
    """`_value_columns` has no `exp.Filter` branch because the parse makes one unnecessary.

    `SUM(orders.total_amount) FILTER (WHERE order_items.quantity > 0)` parses to
    `Filter(this=Sum(...), expression=Where(...))` — the `Filter` is the PARENT of the aggregate, so
    `order_items.quantity` is not in the `Sum` subtree at all. `_select_aggregates` collects
    `exp.AggFunc` nodes, so the aggregate that reaches `_value_columns` is the bare `Sum` and the
    predicate is structurally invisible to it. A branch for `exp.Filter` would be dead code.

    That is a fact about sqlglot's tree and not about this module, which is exactly why it is pinned
    here rather than trusted. If a future sqlglot re-parented the predicate under the aggregate, or
    if this layer started reading `exp.Filter` as the aggregate node, the predicate's many-side
    column would silently join the value path and clear a genuinely inflated total. The measured
    behaviour is asserted through the analysis so that either change fails: the summed value is a
    one-side amount, once per surviving order item, and the join multiplies it.
    """
    agg = parse_one(FILTERED_FAN).find(exp.AggFunc)
    assert isinstance(agg, exp.Sum), agg
    assert isinstance(agg.parent, exp.Filter), agg.parent
    assert [c.sql() for c in agg.find_all(exp.Column)] == ["orders.total_amount"], agg

    reports = _reports(FILTERED_FAN)
    assert [a.status for a in reports] == [rt.MULTIPLIED], reports
    assert reports[0].joins == [FAN_EDGE], reports[0].joins


@pytest.mark.parametrize("label,expression,expected", VALUE_COLUMN_CASES)
def test_the_value_path_holds_only_the_columns_the_result_is_built_from(label, expression, expected):
    """A7. Every branch of `_value_columns`, asserted on the function rather than through a receipt.

    The analysis reaches most of these branches, but it reaches them in combination and reports one
    status per statement, so a branch that dropped a column the value does use and a branch that
    kept one it does not can cancel out in the answer. Called directly, each shape says which
    columns it thinks the value is built from, and a wrong one is wrong visibly.

    The two guard branches are the reason this is a unit test at all. A simple `CASE`'s operand
    (`CASE orders.status WHEN 'x' THEN …`) is an input to the branch CHOICE, not to the result, so
    it belongs with the `WHEN` predicates and not with the `THEN` values — and it lives under
    `Case.this`, which the generic branch would have taken. The nested case proves the recursion
    applies the same rule at depth rather than only at the top. `DISTINCT` is the opposite check:
    it carries a genuine value column under `args["expressions"]`, so the generic branch must reach
    it and no case may intercept it.
    """
    node = parse_one(f"SELECT {expression} FROM orders").expressions[0]
    assert [c.sql() for c in rt._value_columns(node)] == expected, label


def test_the_value_path_of_something_that_is_not_an_expression_is_empty():
    """The guard that lets the generic branch iterate `node.args` without inspecting what it holds.

    Argument values are not all nodes: `Count(big_int=True)` and `Ordered(nulls_first=True)` carry
    bools, and an absent optional argument is `None`. The alternative to this guard is a type check
    at every call site inside the walk, which is the same test written four times.
    """
    assert rt._value_columns(None) == []
    assert rt._value_columns(True) == []


# --- A24: the same report in every process ---------------------------------

_PROBE = """
import json, sys
sys.path.insert(0, sys.argv[1])
from semantic_model import models as m
from semantic_model import runtime as rt
org = m.Datasource.model_validate_json(sys.argv[3])
print(json.dumps([a.as_dict() for a in rt.pre_flight_check(sys.argv[2], org).aggregates]))
"""


def test_the_aggregate_report_is_the_same_in_every_process():
    """A24 / REQ-022: the receipt is "the same for the same SQL and model version".

    That is a claim about PROCESSES, so nothing inside one can check it. The hazards it names are
    invisible under a fixed seed: `sources` is a `frozenset` and `many_tables` is a `set`, the fan
    branch iterates a sorted copy of one of them and intersects the other, and `_shared_dimension`
    picks its dimension by walking a set. Any answer derived from one of those is stable within an
    interpreter and free to differ in the next. Four seeds, four processes, one answer.

    The whole serialized item list is compared rather than the statuses alone, and it is NOT sorted:
    the aggregates are reported in the order the statement wrote them, so a re-ordered list is as
    much a difference as a flipped status and sorting here would hide exactly one of the two modes
    this exists to catch.

    `EVERY_STATUS_SQL` carries all three statuses and both risk labels over a single fan, so one
    comparison covers the value-path attribution that produced `not_multiplied`, the fan that
    produced `multiplied`, and the unresolved read that produced `undetermined`.

    The model crosses the process boundary as JSON rather than as a path, because this fixture is
    built in Python and has no profile on disk; `model_validate_json` of `model_dump_json` is the
    same object by construction, so the child analyses the model the parent declared.
    """
    payload = _sales_org().model_dump_json()
    seen = set()
    for seed in ("0", "1", "42", "12345"):
        proc = subprocess.run(
            [sys.executable, "-c", _PROBE, str(PKG_SRC), EVERY_STATUS_SQL, payload],
            capture_output=True, text=True, env={**os.environ, "PYTHONHASHSEED": seed},
        )
        assert proc.returncode == 0, proc.stderr
        seen.add(proc.stdout.strip())

    assert len(seen) == 1, f"the aggregate report differed across hash seeds: {seen}"
    # And it is the answer this process gets, so four children agreeing on something wrong would
    # still fail rather than agree quietly.
    assert json.loads(seen.pop()) == [a.as_dict() for a in _reports(EVERY_STATUS_SQL)]


# --- S3: what a SELECT can see, and what a CTE reference stands for ---------
#
# The two statements S3 names. Both were wrong at HEAD and wrong in opposite directions, which is
# why one fix could not have been enough on its own.
#
# The first names a join only the CTE BODY takes. `oi` groups `order_items` to one row per order,
# so the outer join is one-to-one and nothing is multiplied — but `order_items` leaked out of the
# body into the outer scope map and the report claimed a fan over a table the statement never
# joined to.
GRAIN_CHANGING_CTE = (
    "WITH oi AS (SELECT order_id, SUM(quantity) q FROM order_items GROUP BY order_id) "
    "SELECT SUM(orders.total_amount) FROM orders JOIN oi ON oi.order_id = orders.id"
)
# The second MISSES the join it does take. `o` hands back the rows of `orders` unchanged and the
# outer query joins that to `order_items`, which is the plain fan wearing one disguise — but `o`
# resolved to the string `'o'`, a name the model never declared, so nothing was found at all.
GRAIN_PRESERVING_CTE = (
    "WITH o AS (SELECT * FROM orders) SELECT SUM(o.total_amount) FROM o "
    "JOIN order_items ON order_items.order_id = o.id"
)
# The same laundering one hop further out. Two grain-preserving CTEs in a chain are still the same
# rows, so the resolution has to be transitive or it answers correctly only at depth one.
TRANSITIVE_CTE = (
    "WITH a AS (SELECT * FROM order_items), b AS (SELECT * FROM a) "
    "SELECT SUM(orders.total_amount) FROM orders JOIN b ON b.order_id = orders.id"
)
# Grouped FINER than the join key: one row per (order, product), joined on the order alone. The
# CTE is genuinely the many side of its own edge and the fan is real.
CTE_GRAIN_BELOW_JOIN_KEY = (
    "WITH oi AS (SELECT order_id, product_id, SUM(quantity) q FROM order_items "
    "GROUP BY order_id, product_id) "
    "SELECT SUM(orders.total_amount) FROM orders JOIN oi ON oi.order_id = orders.id"
)
# `_cte_names` and `_model_table_index` fold case; `_alias_map` preserves it. Every comparison the
# resolution makes therefore has to be explicit about which side it is on, and this is the shape
# that catches one that is not.
CASE_FOLDED_CTE = (
    "WITH OI AS (SELECT * FROM order_items) "
    "SELECT SUM(orders.total_amount) FROM orders JOIN OI ON OI.order_id = orders.id"
)

# The derived-table shape, with its aggregate's column UNQUALIFIED. Two independent mechanisms
# would each have to fail for this to read clean: without the derived binding the outer scope is
# the single table `orders`, so `total_amount` resolves by being the only candidate.
DERIVED_TABLE_UNQUALIFIED = (
    "SELECT SUM(total_amount) FROM orders "
    "JOIN (SELECT order_id FROM order_items) d ON d.order_id = orders.id"
)

# A parenthesized named table. `Subquery(this=Table)` binds no name of its own — the `exp.Table`
# arm already bound `orders` — so this reads one declared table and must stay clean.
PARENTHESIZED_TABLE = "SELECT SUM(orders.total_amount) FROM (orders)"

# The two shapes `check_scopable` refuses at the guarded chokepoint and `sm prepare` does not.
VALUES_SOURCE = (
    "SELECT SUM(orders.total_amount) FROM orders "
    "JOIN (VALUES (1), (2)) AS v(order_id) ON v.order_id = orders.id"
)
LATERAL_SOURCE = "SELECT SUM(o.total_amount) FROM orders o, LATERAL (SELECT 1) l"

# Every way `_grain_preserving_source` and `_cte_edge` can decline, one row per guard. Each body
# below differs from a grain-preserving one in exactly one respect, so a guard that stopped firing
# would show up as one row rather than as a general collapse.
UNREADABLE_CTE_BODIES = [
    ("the body is a set operation, not a SELECT",
     "WITH u AS (SELECT order_id FROM order_items UNION SELECT id FROM orders) "
     "SELECT SUM(orders.total_amount) FROM orders JOIN u ON u.order_id = orders.id"),
    ("the body is DISTINCT, which collapses rows",
     "WITH d AS (SELECT DISTINCT order_id FROM order_items) "
     "SELECT SUM(orders.total_amount) FROM orders JOIN d ON d.order_id = orders.id"),
    ("the body aggregates without grouping",
     "WITH s AS (SELECT SUM(quantity) AS order_id FROM order_items) "
     "SELECT SUM(orders.total_amount) FROM orders JOIN s ON s.order_id = orders.id"),
    ("the body takes a join of its own",
     "WITH j AS (SELECT o.id AS id FROM orders o JOIN order_items i ON i.order_id = o.id) "
     "SELECT SUM(orders.total_amount) FROM orders JOIN j ON j.id = orders.id"),
    ("the body has no FROM at all",
     "WITH n AS (SELECT 1 AS order_id) "
     "SELECT SUM(orders.total_amount) FROM orders JOIN n ON n.order_id = orders.id"),
    ("the body reads more than one table",
     "WITH t AS (SELECT order_id FROM order_items WHERE order_id IN (SELECT id FROM orders)) "
     "SELECT SUM(orders.total_amount) FROM orders JOIN t ON t.order_id = orders.id"),
    ("the body reads a table the model does not declare",
     "WITH x AS (SELECT * FROM shipments) "
     "SELECT SUM(orders.total_amount) FROM orders JOIN x ON x.order_id = orders.id"),
    ("two CTEs read each other, so the walk would not terminate",
     "WITH a AS (SELECT * FROM b), b AS (SELECT * FROM a) "
     "SELECT SUM(orders.total_amount) FROM orders JOIN a ON a.order_id = orders.id"),
    ("the join key is composite",
     "WITH oi AS (SELECT order_id, product_id, SUM(quantity) q FROM order_items "
     "GROUP BY order_id) SELECT SUM(orders.total_amount) FROM orders "
     "JOIN oi ON oi.order_id = orders.id AND oi.product_id = orders.id"),
    ("there is no join key, because it is a comma join",
     "WITH oi AS (SELECT order_id, SUM(quantity) q FROM order_items GROUP BY order_id) "
     "SELECT SUM(orders.total_amount) FROM orders, oi"),
    ("the join predicate is an inequality, not an equality",
     "WITH oi AS (SELECT order_id, SUM(quantity) q FROM order_items GROUP BY order_id) "
     "SELECT SUM(orders.total_amount) FROM orders JOIN oi ON oi.order_id > orders.id"),
    ("the far side of the join key is a literal, not a column",
     "WITH oi AS (SELECT order_id, SUM(quantity) q FROM order_items GROUP BY order_id) "
     "SELECT SUM(orders.total_amount) FROM orders JOIN oi ON oi.order_id = 1"),
    ("the far side resolves to nothing in scope",
     "WITH oi AS (SELECT order_id, SUM(quantity) q FROM order_items GROUP BY order_id) "
     "SELECT SUM(orders.total_amount) FROM orders JOIN oi ON oi.order_id = zzz.id"),
    ("the grain is an expression with no column name to compare",
     "WITH oi AS (SELECT SUM(quantity) q FROM order_items GROUP BY order_id + 1) "
     "SELECT SUM(orders.total_amount) FROM orders JOIN oi ON oi.q = orders.id"),
]


def _parse(sql: str):
    """One parse, so every assertion on the resolver reads the tree the analysis reads."""
    return parse_one(sql)


def _no_grain_org() -> "m.Datasource":
    """`orders` with `grain: []` — the one thing `_sales_org` cannot express and must not learn.

    Every `_sales_org` table declares a non-empty grain, which is what makes its assertions mean
    what they say; weakening one of them to reach this branch would silently change what several
    other tests are testing. So the empty-grain case gets its own two-table model, cut down to the
    single edge the assertion is about.
    """
    tables = [
        m.Table(name="orders", schema="public", storage_connection="c", grain=[],
                description="orders",
                columns=[m.Column(name="id", type="integer"),
                         m.Column(name="total_amount", type="decimal")]),
        m.Table(name="order_items", schema="public", storage_connection="c",
                grain=["order_id", "product_id"], description="order items",
                columns=[m.Column(name="order_id", type="integer"),
                         m.Column(name="product_id", type="integer"),
                         m.Column(name="quantity", type="integer")]),
    ]
    rels = [m.Relationship(from_table="order_items", to_table="orders", from_column="order_id",
                           to_column="id", relationship="many_to_one")]
    return m.Datasource(datasource="Shop",
                        subject_areas=[m.SubjectArea(name="sales", tables_defined=tables,
                                                     relationships=rels)])


def _averageable_org() -> "m.Datasource":
    """One table with one `averageable` column — the smallest model `bad_aggregation` fires on.

    `_sales_org` deliberately leaves every `Column.aggregation` at `unknown` so that the exact
    risk-list assertions elsewhere stay about the fan/chasm detector. This model exists only to
    hold `_check_aggregation_semantics` still while the scope map underneath it changes.
    """
    t = m.Table(name="facts", schema="public", storage_connection="c", grain=["id"],
                description="facts",
                columns=[m.Column(name="id", type="integer", aggregation="dimension"),
                         m.Column(name="unit_price", type="decimal", aggregation="averageable")])
    return m.Datasource(datasource="F",
                        subject_areas=[m.SubjectArea(name="a", description="d",
                                                     tables_defined=[t])])


def test_a_grain_changing_cte_is_not_the_table_it_reads():
    """A10. The join a CTE BODY takes is not a join the statement takes.

    `oi` groups `order_items` to one row per order. The outer statement joins `orders` to that, one
    order to one row, and `SUM(orders.total_amount)` is exactly the sum of the orders. Nothing is
    multiplied and the receipt may say so.

    What it said before was `multiplied`, naming `orders (1) <- order_items (N)` — an edge that
    appears nowhere in the outer query. `order_items` is read once, inside the CTE body, under a
    `GROUP BY` that is the whole point of writing the CTE. Reporting it is not an over-cautious
    answer, it is a false one: it names a join the reader can look for and not find, and it does so
    on a statement that is correct.

    Both halves are asserted because they fail independently. Abstaining would fix the naming and
    lose the answer; resolving the CTE without checking its grain would keep the answer and keep
    naming the wrong edge. `joins` is asserted over EVERY item rather than the first, since the
    criterion is that no item names `order_items` — a second aggregate would be a second chance to
    get it wrong.
    """
    reports = _reports(GRAIN_CHANGING_CTE)
    assert [a.status for a in reports] == [rt.NOT_MULTIPLIED], reports
    assert [j for a in reports for j in a.joins] == [], reports
    assert not any("order_items" in j for a in reports for j in a.joins), reports
    assert [f.risk for a in reports for f in a.findings] == [], reports


def test_a_grain_preserving_cte_carries_the_join_it_launders():
    """A11. A CTE that hands back a table's rows unchanged IS that table, for this question.

    `WITH o AS (SELECT * FROM orders)` produces exactly the rows of `orders`, so joining `o` to
    `order_items` multiplies `SUM(o.total_amount)` precisely as joining `orders` would. The
    statement is the plain fan with one indirection, and the fan is real.

    It reported `undetermined` before, and that was not a cautious answer either — it was the
    detector failing to resolve `o` to anything and then having nothing to say. The edge is asserted
    by name: `order_items` and not `o`, because a reader who is told their number was multiplied
    and handed back the alias they invented has been told something they already knew.
    """
    reports = _reports(GRAIN_PRESERVING_CTE)
    assert [a.status for a in reports] == [rt.MULTIPLIED], reports
    assert reports[0].joins == [FAN_EDGE], reports[0].joins
    assert [f.risk for f in reports[0].findings] == ["fan_trap"], reports[0].findings


def test_a_grain_preserving_cte_resolves_through_another_one():
    """A13. Two grain-preserving hops are still the same rows, so the resolution is transitive.

    `b` reads `a` and `a` reads `order_items`, neither changing a thing. A resolver that stopped at
    the first hop would find `b` bound to `a`, which is not a declared table either, and fall back
    to the same `undetermined` the single-hop case used to give — correct-looking on the corpus,
    which only asks that nothing read clean, and wrong on the one assertion that matters here.

    The cycle guard makes this safe rather than unbounded: `seen` is what stops `a` reading `b`
    reading `a` from recursing forever, and it is exercised by its own row in the guard table below.
    """
    reports = _reports(TRANSITIVE_CTE)
    assert [a.status for a in reports] == [rt.MULTIPLIED], reports
    assert reports[0].joins == [FAN_EDGE], reports[0].joins


def test_a_cte_grain_that_does_not_cover_the_join_key_still_fans():
    """A14. Grouped finer than the join key, the CTE is the many side of its own edge.

    `oi` is one row per (order, product) and the join uses `order_id` alone, so an order with three
    products meets three rows and `SUM(orders.total_amount)` triples. That is a fan, and it is a fan
    over a source the model never declared — which is exactly why the analysis has to DERIVE the
    edge rather than abstain. Abstaining would report `undetermined` on a statement whose answer is
    known and wrong.

    The derived edge is asserted, not just the status. `many_to_one` from the CTE to `orders` is the
    claim the report rests on, and `infer_cardinality` read it off the two grains — the CTE's
    `(order_id, product_id)` against the join key `order_id`. A status assertion alone would pass
    just as well if the fan came from somewhere else entirely.

    The edge names `oi` because that is the only name this source has. It is the one place a join
    on the receipt names an alias, and it is honest: there is no table to name.
    """
    tree = _parse(CTE_GRAIN_BELOW_JOIN_KEY)
    scope, derived = rt._resolve_cte_scope(
        tree, rt._alias_map(tree, in_scope_only=True), rt._model_table_index(_sales_org()))
    assert scope == {"orders": "orders", "oi": "oi"}, scope
    assert [(r.from_table, r.to_table, r.from_column, r.to_column, r.relationship)
            for r in derived] == [("oi", "orders", "order_id", "id", "many_to_one")], derived

    reports = _reports(CTE_GRAIN_BELOW_JOIN_KEY)
    assert [a.status for a in reports] == [rt.MULTIPLIED], reports
    assert reports[0].joins == ["orders (1) <- oi (N)"], reports[0].joins


def test_a_cte_grain_that_covers_the_join_key_is_not_multiplied():
    """A15. `not_multiplied` BECAUSE the grain covers the join key, not because we declined.

    `oi` is one row per order and the join is on `order_id`, so each order meets exactly one row.
    The derived edge is `one_to_one`, and that is the assertion: `undetermined` and `not_multiplied`
    are different answers, and so are two routes to `not_multiplied`. A resolver that bound every
    grain-changing CTE to nothing would also produce no fan here, and the item would then say
    `undetermined` — so the status alone cannot tell the working rule from the abstaining one.

    Same SQL as A10, read one layer down. A10 asserts what the receipt says; this asserts why.
    """
    tree = _parse(GRAIN_CHANGING_CTE)
    scope, derived = rt._resolve_cte_scope(
        tree, rt._alias_map(tree, in_scope_only=True), rt._model_table_index(_sales_org()))
    assert scope == {"orders": "orders", "oi": "oi"}, scope
    assert [(r.from_table, r.to_table, r.from_column, r.to_column, r.relationship)
            for r in derived] == [("oi", "orders", "order_id", "id", "one_to_one")], derived


@pytest.mark.parametrize("label,on,expected", [
    ("the CTE is written on the left", "oi.order_id = orders.id", "one_to_one"),
    ("the CTE is written on the right", "orders.id = oi.order_id", "one_to_one"),
])
def test_the_join_key_is_read_in_either_orientation(label, on, expected):
    """Which side of the equality the CTE sits on is the author's typing, not a fact about the join.

    `a = b` and `b = a` are the same predicate, and sqlglot keeps the two apart in the tree, so the
    orientation has to be resolved rather than assumed. Getting it wrong is not a crash: the pair
    would simply never be found, `_cte_edge` would see no join key, and the statement would report
    `undetermined` — a receipt that declines to answer half the statements it can answer, on a
    difference nobody writing SQL considers a difference.

    The derived edge is compared rather than the status, because both orientations of THIS statement
    happen to end at `not_multiplied` — one by deriving a `one_to_one` and one by abstaining — and
    only the edge tells those apart.
    """
    sql = ("WITH oi AS (SELECT order_id, SUM(quantity) q FROM order_items GROUP BY order_id) "
           f"SELECT SUM(orders.total_amount) FROM orders JOIN oi ON {on}")
    tree = _parse(sql)
    _scope, derived = rt._resolve_cte_scope(
        tree, rt._alias_map(tree, in_scope_only=True), rt._model_table_index(_sales_org()))
    assert [(r.from_table, r.to_table, r.from_column, r.to_column, r.relationship)
            for r in derived] == [("oi", "orders", "order_id", "id", expected)], label
    assert [a.status for a in _reports(sql)] == [rt.NOT_MULTIPLIED], label


@pytest.mark.parametrize("label,sql", UNREADABLE_CTE_BODIES)
def test_a_cte_body_the_analysis_cannot_read_is_undetermined(label, sql):
    """A16. Every way the resolution can decline ends in `undetermined`, never in a clean answer.

    Each row differs from a resolvable statement in exactly one respect, and each of those respects
    is a way the rows behind the aggregate could differ from what the outer query appears to join.
    A set-operation body sums its arms; `DISTINCT` and an aggregate collapse rows; a join inside the
    body multiplies where the outer walk does not look; a body reading two tables or none, or a
    table the model does not declare, resolves to nothing that can be reasoned about; and two CTEs
    reading each other would not terminate at all.

    The last six are the edge rather than the body: a composite or absent join key is not the single
    -column cardinality rule's to state, an inequality and a literal are not a key, a far side that
    resolves to nothing has no grain to compare against, and a grain written as an expression has no
    column name to compare a join key to.

    They share one assertion because they share one contract. This layer is allowed to say a number
    was multiplied, or that it was not, or that it could not tell — and every one of these is the
    third. What none of them may be is absent or clean: `not_multiplied` on any row here would be
    the receipt asserting a statement is sound on the strength of a body it could not read.
    """
    reports = _reports(sql)
    assert [a.status for a in reports] == [rt.UNDETERMINED], (label, reports)
    assert [j for a in reports for j in a.joins] == [], (label, reports)


def test_an_undeclared_grain_is_undetermined_rather_than_a_default_cardinality():
    """A26. An EMPTY declared grain is not a grain, and the guard sits before the inference.

    `infer_cardinality` tests `bool(from_pk)` and `bool(to_pk)`, so a table declared `grain: []` and
    a table with no grain at all are the same thing to it, and both fall through to its
    `many_to_one` default. That default is right for a model-authoring tool proposing an edge for
    review. It is wrong here, where the answer lands on a receipt as a fact: the cardinality would
    be one nobody declared.

    Measured, and this is why the guard is placed before the call rather than after: with the guard
    removed this same statement reports `not_multiplied`. The inference makes the CTE the ONE side
    of a `one_to_many` — its own grain covers the join key, the far side's does not — so no fan
    fires and the item claims the number is clean. A false clean out of a cardinality nobody
    declared is the exact failure this spec exists to remove.

    Its own model, because the branch is unreachable on `_sales_org` and weakening a fixture to
    reach a branch changes what every other test on that fixture is asserting.
    """
    pf = rt.pre_flight_check(GRAIN_CHANGING_CTE, _no_grain_org())
    assert pf.unchecked is None, pf.unchecked
    assert [(a.aggregate, a.status) for a in pf.aggregates] == [
        ("SUM(orders.total_amount)", rt.UNDETERMINED)], pf.aggregates


def test_a_cte_name_is_resolved_case_insensitively_but_reported_as_written():
    """The fold hazard, pinned in both directions on one statement.

    `_cte_names` lowercases and `_model_table_index` is keyed folded, while `_alias_map` preserves
    exactly what the statement wrote. So `WITH OI AS (…) … JOIN OI` has to fold on the way IN — to
    recognise `OI` as a CTE and to look up its source's grain — and preserve on the way OUT, because
    the derived edge has to name the source the way the scope map holds it or the detector matches
    nothing at all.

    A comparison that folded on neither side would leave this reading clean, which is the direction
    that matters: `OI` is `order_items` under another spelling and the fan is real.
    """
    reports = _reports(CASE_FOLDED_CTE)
    assert [a.status for a in reports] == [rt.MULTIPLIED], reports
    assert reports[0].joins == [FAN_EDGE], reports[0].joins


# --- A12: the map a caller may reason about joins from ----------------------


@pytest.mark.parametrize("label,sql,scopes,expected", [
    ("a reference inside a CTE body is not in the outer scope", CTE_LAUNDERED_FAN,
     ["cte:oi", "main", "main"], {"orders": "orders", "oi": "oi"}),
    ("a reference inside a derived table is not in the outer scope", DERIVED_TABLE_FAN,
     ["main", "subquery"], {"orders": "orders", "d": ""}),
    ("both arms of a set operation are the main query",
     "SELECT SUM(orders.total_amount) FROM orders "
     "UNION SELECT SUM(order_items.quantity) FROM order_items",
     ["main#1", "main#2"], {"orders": "orders", "order_items": "order_items"}),
])
def test_the_scope_filtered_alias_map_keeps_only_references_the_select_can_see(
    label, sql, scopes, expected,
):
    """A12. Asserted on the function, because the filter is a claim about the map and not about a
    receipt.

    Three shapes, one per thing the filter has to get right. A reference scoped `cte:oi` is written
    in a body whose rows the outer query only sees through the CTE, so it is not a table the outer
    FROM/JOIN clauses bind; a reference scoped `subquery` is the same case one construct over; and
    an arm of a set operation IS the main query, which is why the family is compared after the arm
    ordinal is stripped rather than before.

    Each case asserts the scopes its references actually carry BEFORE asserting the map, so a case
    proves what it claims rather than passing because the shape it names never arose. The third one
    needs it most: `main#1` and `main#2` are two different scope STRINGS, equal to `"main"` only
    once the ordinal is split off the right, and a filter written as a bare `scope == "main"` would
    drop both arms and leave an empty map — which no assertion on the map alone tells apart from a
    filter that is simply too keen.
    """
    tree = _parse(sql)
    assert sorted(r.scope for r in rt._table_references(tree)) == sorted(scopes), label
    assert rt._alias_map(tree, in_scope_only=True) == expected, label


def test_the_arm_ordinal_is_split_off_the_right_and_a_cte_name_can_carry_no_hash():
    """`_scope_family` splits from the RIGHT, and both halves of that choice are pinned.

    The ordinal `_arm_suffixes` appends is ours and always last, while everything to its left can
    hold a CTE name, which is caller-written text. Splitting from the left would read `cte:a#b` as
    the family `cte:a` — a scope that is not the main query either way, so nothing observable moves
    today, but the rule would be about the wrong thing.

    That no CTE name can actually carry a `#` is a fact about `_echo_name` and not about this
    function, so it is measured rather than assumed: the label a reference carries is echoed, and
    `#` is not a character that survives echoing. The right-side split is simply the form that stays
    correct without depending on it.
    """
    assert rt._scope_family("main") == "main"
    assert rt._scope_family("main#2") == "main"
    assert rt._scope_family("cte:recent#1") == "cte:recent"
    assert rt._scope_family("subquery") == "subquery"
    assert rt._echo_name("a#b") == "a?b"

    tree = _parse("SELECT SUM(orders.total_amount) FROM orders "
                  "UNION SELECT SUM(order_items.quantity) FROM order_items")
    assert sorted(r.scope for r in rt._table_references(tree)) == ["main#1", "main#2"]


@pytest.mark.parametrize("label,sql,expected", [
    ("an aliased derived table in a JOIN", DERIVED_TABLE_FAN,
     {"orders": "orders", "order_items": "order_items"}),
    ("an unaliased VALUES list in a comma join", UNALIASED_VALUES_FAN, {"orders": "orders"}),
])
def test_the_default_alias_map_is_unchanged_by_the_derived_binding(label, sql, expected):
    """A12b. The default path is bit-identical, on the shapes that could have moved.

    `tests/test_ace099_resolver_parity.py` pins the default against the helper it replaced, and it
    stays unedited — but it covers CTE and subquery shapes, none of which bind a FROM/JOIN derived
    source. Those are precisely the shapes the new walk added a node type for, so they are precisely
    the ones where a default that quietly picked up the binding would go unnoticed.

    Three callers read this map on the default and two of them would change what they DISCLOSE if
    the filter or the binding reached them: `_projected_sensitive` refuses, and `assemble_receipt`
    builds the receipt's `joins` and `tables` sections. So the assertion is equality with the exact
    map, plus the absence of the two keys the binding would have added.
    """
    tree = _parse(sql)
    assert rt._alias_map(tree) == expected, label
    assert "" not in rt._alias_map(tree), label
    assert "d" not in rt._alias_map(tree), label


# --- the false clean the filter itself creates ------------------------------


@pytest.mark.parametrize("label,sql", [
    ("the aggregate's column is qualified", DERIVED_TABLE_FAN),
    ("the aggregate's column is unqualified", DERIVED_TABLE_UNQUALIFIED),
])
def test_an_alias_bound_to_nothing_is_undetermined_rather_than_clean(label, sql):
    """The proof that the derived binding had to ship in the same commit as the scope filter.

    `d` is an `exp.Subquery`, so it never entered the alias map at all, and `order_items` reached
    the outer map only by leaking out of `d`'s body — which is the single reason this shape reported
    `multiplied` before. Land the filter alone and the leak stops, the outer scope is the one table
    `orders`, `SUM(orders.total_amount)` resolves perfectly, and a statement whose rows a join
    genuinely multiplied reports `not_multiplied`. That is the receipt asserting something false,
    and no test on `main` would have failed.

    The second row is the same shape with the column unqualified, and it needs both halves to fail
    before it reads clean: with `d` bound, the scope holds two entries and `_resolve_col_table`
    declines to attribute an unqualified column at all; with `d` absent, `orders` is the only
    candidate and the column resolves cleanly to it. Either mechanism alone would have caught this
    one, so it is the row that says the two are not the same mechanism.
    """
    reports = _reports(sql)
    assert [a.status for a in reports] == [rt.UNDETERMINED], (label, reports)
    assert [j for a in reports for j in a.joins] == [], (label, reports)


def test_a_parenthesized_table_is_the_table_and_not_a_source_of_its_own():
    """`FROM (orders)` binds no name, so the scope-completeness rule must not fire on it.

    sqlglot parses it to `Subquery(this=Table)` with an empty alias — structurally the same node
    kind as a derived table, and semantically just a bracket around a table name the `exp.Table`
    arm has already bound. Binding it too would put an empty key in the map, and by the
    scope-completeness conjunct that one entry turns EVERY aggregate in the SELECT `undetermined`.

    Measured both ways while the guard was being placed: without the discriminator this statement
    reports `undetermined`, with it `not_multiplied`. The distinction is `.this` being an
    `exp.Table` and nothing else — an unaliased `VALUES`, `LATERAL` or derived `SELECT` in the same
    position introduces rows nothing can name and MUST bind, which is the corpus member above.
    """
    reports = _reports(PARENTHESIZED_TABLE)
    assert [(a.aggregate, a.status) for a in reports] == [
        ("SUM(orders.total_amount)", rt.NOT_MULTIPLIED)], reports
    assert rt._alias_map(_parse(PARENTHESIZED_TABLE), in_scope_only=True) == {"orders": "orders"}


@pytest.mark.parametrize("label,sql", [
    ("a VALUES list", VALUES_SOURCE),
    ("a LATERAL", LATERAL_SOURCE),
])
def test_a_source_the_guarded_path_refuses_is_still_answered_honestly_on_the_prepare_surface(
    label, sql,
):
    """Why the `exp.Lateral` and `exp.Values` binding arms are not dead code.

    `check_scopable` (ACE-037) refuses both of these source kinds at the `execute_guarded`
    chokepoint, so no query that RUNS ever reaches the multiplication analysis carrying one. That is
    asserted here rather than assumed, because it is the premise of the whole question.

    But `cmd_preflight` and `cmd_prepare` in `semantic_model/cli.py` call `pre_flight_check`
    directly, with no gate battery at all — `cmd_prepare`'s own docstring says it is not a gate and
    never was — and the query skill runs `sm prepare` on every tier. On that surface both of these
    reported `not_multiplied` over a source that can produce many rows per order. A reader of a
    prepare receipt has no way to know a gate they never invoked would have refused the statement;
    what they have is the sentence in front of them, and it said the number was sound.

    So the analysis answers honestly on both surfaces, and the two arms exist for the one that has
    no gate in front of it. `pre_flight_check` is called directly here for the same reason: driving
    this through `execute_guarded` would measure the refusal and never reach the claim.
    """
    assert rt.check_scopable(sql, _sales_org()) is not None, label
    pf = rt.pre_flight_check(sql, _sales_org())
    assert pf.unchecked is None, (label, pf.unchecked)
    assert [a.status for a in pf.aggregates] == [rt.UNDETERMINED], (label, pf.aggregates)


# --- what the narrowed scope map costs the semantic checks ------------------


@pytest.mark.parametrize("label,sql", [
    ("qualified", "SELECT SUM(facts.unit_price) FROM facts"),
    ("unqualified, one table in scope", "SELECT SUM(unit_price) FROM facts"),
])
def test_an_aggregation_class_violation_still_fires_on_the_statement_that_makes_it(label, sql):
    """`_check_aggregation_semantics` reads the same narrowed map, so its floor is pinned here.

    It resolves the summed column's table through the scope map `_preflight_select` hands it, and
    that map now holds only what this SELECT's own FROM/JOIN clauses bind. Two `bad_aggregation`
    findings were measured to stop firing as a result, and both fired only by reading a table out of
    a scope the outer statement cannot see: `SUM(unit_price) FROM (SELECT unit_price FROM facts) f`
    and its CTE equivalent. That is ACE-099's rule applied consistently — a column in a CTE body is
    not a reference of the outer statement — and it is recorded as a contract change rather than
    hidden, since one of the two traded a lost finding for a killed false clean.

    What may not narrow is the case the check exists for: a statement that sums an `averageable`
    column off a table it plainly reads. Both spellings are pinned, because they take different
    paths through the resolver — the qualified one through the map, the unqualified one through the
    single-in-scope-table fallback — and the fallback reads the map's VALUES, which is the half the
    filter changed.
    """
    pf = rt.pre_flight_check(sql, _averageable_org())
    assert [f.risk for f in pf.findings] == ["bad_aggregation"], (label, pf.findings)
    assert "unit_price" in pf.findings[0].reason, (label, pf.findings[0].reason)


def test_the_chasm_still_reports_both_of_the_aggregates_it_inflates():
    """The chasm reads `table_set` off the same map the filter narrowed, so it is re-pinned here.

    `tests/test_semantic_model_runtime.py::test_chasm_trap_is_reported` is the gate and stays
    unedited; this asserts the same property from the file that changed the map, so a narrowing that
    cost the chasm one of its two items fails beside the change that caused it rather than in
    another file. Both aggregates are inflated by the cross-product and both must say so — a chasm
    reported on one of the two numbers it ruins is a receipt that reads as half a fact.
    """
    sql = ("SELECT c.id, SUM(o.revenue), COUNT(t.id) FROM customers c "
           "LEFT JOIN orders o ON o.customer_id = c.id "
           "LEFT JOIN tickets t ON t.customer_id = c.id GROUP BY c.id")
    reports = _reports(sql)
    assert [(a.aggregate, a.status) for a in reports] == [
        ("SUM(o.revenue)", rt.MULTIPLIED), ("COUNT(t.id)", rt.MULTIPLIED)], reports
    assert [f.risk for a in reports for f in a.findings] == ["chasm_trap", "chasm_trap"], reports
