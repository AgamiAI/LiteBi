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
"""

from __future__ import annotations

from typing import get_args

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

# `rt` is imported FROM the fixture's module rather than beside it, and that is load-bearing.
# `test_semantic_model_runtime` puts `plugins/agami/scripts` on `sys.path`, while the ACE-060 and
# ACE-099 batteries put `packages/agami-core/src` there. Importing `semantic_model.runtime` down a
# different path than the fixture came down yields a second module object with its own
# `MULTIPLIED` / `NOT_MULTIPLIED` string objects and its own detector — so an assertion here could
# compare a status produced by one copy against a constant defined by the other. One import.
from test_semantic_model_runtime import _sales_org, rt  # noqa: E402

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
]

# A conditional count and a conditional sum over the many side. Both are honestly clean: one row
# per order item is exactly what they count, so the join multiplies nothing they read.
CONDITIONAL_COUNT = f"SELECT COUNT(CASE WHEN order_items.quantity > 0 THEN 1 END) {FAN_JOIN}"
CONDITIONAL_SUM = (
    f"SELECT SUM(CASE WHEN order_items.quantity > 0 THEN 1 ELSE 0 END) {FAN_JOIN}"
)

FAN_EDGE = "orders (1) <- order_items (N)"

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
