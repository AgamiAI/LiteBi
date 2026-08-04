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

Everything here passes on the base implementation. That is the point: these are not the tests for
the new behaviour, they are the tests for the behaviour that must survive it.
"""

from __future__ import annotations

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
