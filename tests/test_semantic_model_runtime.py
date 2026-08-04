"""Unit tests for semantic_model/runtime.py — traversal, entity ID, pre-flight."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import models as m  # noqa: E402
from semantic_model import runtime as rt  # noqa: E402


def _sales_org():
    rels = [
        m.Relationship(from_table="order_items", to_table="orders", from_column="order_id",
                       to_column="id", relationship="many_to_one"),
        m.Relationship(from_table="orders", to_table="customers", from_column="customer_id",
                       to_column="id", relationship="many_to_one"),
        m.Relationship(from_table="tickets", to_table="customers", from_column="customer_id",
                       to_column="id", relationship="many_to_one"),
    ]
    return m.Datasource(datasource="Shop",
                          subject_areas=[m.SubjectArea(name="sales", relationships=rels)])


# --- pre-flight ---


def test_the_aggregation_only_fan_trap_is_reported_not_rewritten_and_not_refused():
    """The shape with the longest history here. It was auto-rewritten by dropping the join, on the
    grounds that the transform was result-preserving; ACE-093 deleted that and left it refusing;
    this reports it and runs the statement.

    Both subtractions came from the same place. The transform WAS result-preserving, and the
    refusal WAS well-founded, and neither was ours to decide: whether a multiplied total is wrong
    depends on the question, and this layer has the statement and the model and never the question.

    The finding is asserted for what it does not carry. There is no `suggestion` — that field gave
    a refused caller a way forward, and naming one on an answer that came back presumes intent."""
    org = _sales_org()
    sql = ("SELECT SUM(orders.total_amount) FROM orders "
           "JOIN order_items ON order_items.order_id = orders.id")
    pf = rt.pre_flight_check(sql, org)

    assert [f.risk for f in pf.findings] == ["fan_trap"]
    assert "rewrite" not in pf.findings[0].reason.lower(), pf.findings[0].reason
    assert not hasattr(pf.findings[0], "suggestion")
    assert "order_items" in pf.findings[0].reason  # names the many side rather than removing it
    assert pf.findings[0].triggering_joins == ["orders (1) <- order_items (N)"]


def test_chasm_trap_is_reported():
    """One entry per AGGREGATE the cross-product inflates, not one for the pair of tables.

    This asserted a single finding, which was right when a finding was about the pair. The caller
    holds two totals and both are inflated, so both are reported — and `pf.aggregates` is the shape
    that says which is which.
    """
    org = _sales_org()
    pf = rt.pre_flight_check(
        "SELECT c.id, SUM(o.revenue), COUNT(t.id) FROM customers c "
        "LEFT JOIN orders o ON o.customer_id=c.id LEFT JOIN tickets t ON t.customer_id=c.id "
        "GROUP BY c.id", org)
    assert [f.risk for f in pf.findings] == ["chasm_trap", "chasm_trap"]
    assert [(a.aggregate, a.status) for a in pf.aggregates] == [
        ("SUM(o.revenue)", rt.MULTIPLIED), ("COUNT(t.id)", rt.MULTIPLIED)]


def test_fan_trap_mixed_raw_and_aggregate_is_reported():
    org = _sales_org()
    pf = rt.pre_flight_check(
        "SELECT orders.id, orders.created_at, SUM(orders.total_amount) FROM orders "
        "JOIN order_items ON order_items.order_id=orders.id GROUP BY orders.id, orders.created_at",
        org)
    assert [f.risk for f in pf.findings] == ["fan_trap"]


def test_explicit_cross_product_is_not_a_trap():
    org = _sales_org()
    pf = rt.pre_flight_check(
        "SELECT * FROM orders, tickets WHERE orders.customer_id = tickets.customer_id", org)
    assert pf.findings == []


def test_fan_trap_in_a_set_operation_arm_is_reported():
    """A trap in ANY set-operation arm is reported. The set operation parses to exp.SetOperation,
    so gating on isinstance(tree, exp.Select) would skip every arm, and the finding has to come
    from visiting the arm since the statement as a whole is not a SELECT.

    The walk used to stop at the first arm that would have refused, which is right for a verdict
    and wrong for a description: the second arm's trap is not made false by the first one's. It
    accumulates now, and `test_every_trapped_arm_is_reported` pins that."""
    org = _sales_org()
    fan = ("SELECT SUM(orders.total_amount) FROM orders "
           "JOIN order_items ON order_items.order_id = orders.id")
    pf = rt.pre_flight_check(f"SELECT 1 AS n UNION ALL {fan}", org)
    assert [f.risk for f in pf.findings] == ["fan_trap"]


def test_every_trapped_arm_is_reported():
    """Two trapped arms, two findings. The old walk returned on the first, so the caller was told
    one arm's aggregate was inflated and never that the other one's was too."""
    org = _sales_org()
    fan = ("SELECT SUM(orders.total_amount) FROM orders "
           "JOIN order_items ON order_items.order_id = orders.id")
    pf = rt.pre_flight_check(f"{fan} UNION ALL {fan}", org)
    assert [f.risk for f in pf.findings] == ["fan_trap", "fan_trap"]


def test_clean_set_operation_passes_preflight():
    """A set operation with no trapped arm reports nothing — arm-walking must not over-report."""
    org = _sales_org()
    pf = rt.pre_flight_check(
        "SELECT id FROM orders UNION SELECT id FROM customers", org)
    assert pf.findings == []


def test_aggregating_many_side_is_allowed():
    # aggregating the MANY side (order_items) is legitimate, not a fan trap
    org = _sales_org()
    pf = rt.pre_flight_check(
        "SELECT SUM(order_items.quantity) FROM orders JOIN order_items ON order_items.order_id=orders.id",
        org)
    assert pf.findings == []


# --- examples-first ---


def test_examples_high_confidence_short_circuit():
    exs = [{"question": "top 5 sellers this month"}, {"question": "average price by region"}]
    matches = rt.get_prompt_examples("average PRICE by region", exs)
    assert matches[0].example["question"] == "average price by region"
    assert rt.is_high_confidence(matches)


def test_examples_low_confidence():
    exs = [{"question": "something totally unrelated about widgets"}]
    matches = rt.get_prompt_examples("how many orders were placed", exs)
    assert not rt.is_high_confidence(matches)


# --- identify_entity ---


def _entity_org():
    o1 = m.Entity(name="Order", value_pattern=r"^(ORD|SH)\w+$",
                  maps_to=[m.EntityMapping(table="orders", column="order_no", primary=True)])
    o2 = m.Entity(name="Shipment", value_pattern=r"^SH\w+$",
                  maps_to=[m.EntityMapping(table="shipments", column="ship_no", primary=True)])
    return m.Datasource(datasource="Acme",
                          subject_areas=[m.SubjectArea(name="b", entities=[o1, o2])])


def test_identify_entity_resolved():
    org = _entity_org()
    res = rt.identify_entity("SHAH2304", org, probe=lambda t, c, v: t == "orders")
    assert res.status == "resolved" and res.candidates[0]["entity"] == "Order"


def test_identify_entity_overlap_clarify():
    org = _entity_org()
    res = rt.identify_entity("SHAH2304", org, probe=lambda t, c, v: True)
    assert res.status == "clarify" and len(res.candidates) == 2 and res.question_template


def test_identify_entity_unrecognized():
    org = _entity_org()
    res = rt.identify_entity("ZZZ-not-matching", org, probe=lambda t, c, v: True)
    assert res.status == "unrecognized"


# --- instance resolution strategy ---


@pytest.mark.parametrize("kwargs,expected", [
    ({"sensitive": True}, "db_probe"),
    ({"cardinality": 20}, "enum"),
    ({"cardinality": 5000}, "cached_index"),
    ({"cardinality": 50000}, "db_probe"),
    ({}, "db_probe"),
])
def test_resolve_entity_instance(kwargs, expected):
    e = m.Entity(name="X")
    assert rt.resolve_entity_instance(e, **kwargs) == expected


# The three `apply_default_filters` tests that lived here were deleted by ACE-042 along with the
# injector they asserted. Their replacement is tests/test_ace042_no_filter_injection.py, which
# pins the absence at the `_model_safety` chokepoint plus the CTE case the injection got wrong.


# --- receipt ---
#
# `build_receipt` was a SECOND receipt builder here — its own key names (`from`/`to`, `caveats`,
# `original_sql`/`rewritten_sql`), assembled from a caller-supplied relationship list rather than
# from the SQL. Its only caller was the test that covered it, so it was two descriptions of one
# thing with one of them unreachable. `assemble_receipt` is the builder that ships, and the whole
# of its behaviour is covered by tests/test_ace088_receipt_sections.py. Deleted rather than
# deprecated: nothing else was covering it, because there was nothing else to cover.
