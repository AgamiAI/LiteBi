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


def test_fan_trap_auto_rewrite():
    org = _sales_org()
    pf = rt.pre_flight_check(
        "SELECT SUM(orders.total_amount) FROM orders JOIN order_items ON order_items.order_id = orders.id",
        org)
    assert pf.risk == "fan_trap" and pf.action == "auto_rewrite"
    assert "order_items" not in pf.rewritten_sql


def test_chasm_trap_refuse_with_suggestion():
    org = _sales_org()
    pf = rt.pre_flight_check(
        "SELECT c.id, SUM(o.revenue), COUNT(t.id) FROM customers c "
        "LEFT JOIN orders o ON o.customer_id=c.id LEFT JOIN tickets t ON t.customer_id=c.id "
        "GROUP BY c.id", org)
    assert pf.risk == "chasm_trap" and pf.action == "refuse" and pf.suggestion


def test_fan_trap_mixed_raw_and_aggregate_refuse():
    org = _sales_org()
    pf = rt.pre_flight_check(
        "SELECT orders.id, orders.created_at, SUM(orders.total_amount) FROM orders "
        "JOIN order_items ON order_items.order_id=orders.id GROUP BY orders.id, orders.created_at",
        org)
    assert pf.risk == "fan_trap" and pf.action == "refuse"


def test_explicit_cross_product_allowed():
    org = _sales_org()
    pf = rt.pre_flight_check(
        "SELECT * FROM orders, tickets WHERE orders.customer_id = tickets.customer_id", org)
    assert pf.action == "allow" and pf.risk is None


def test_fan_trap_in_a_set_operation_arm_is_refused():
    """A fan/chasm trap in ANY set-operation arm refuses the whole query. The set
    operation parses to exp.SetOperation, so gating on isinstance(tree, exp.Select) would
    skip every arm. Arms are not auto-rewritten, so a would-be auto_rewrite fan trap
    (see test_fan_trap_auto_rewrite) becomes a refuse when it sits inside a UNION arm."""
    org = _sales_org()
    fan = ("SELECT SUM(orders.total_amount) FROM orders "
           "JOIN order_items ON order_items.order_id = orders.id")
    pf = rt.pre_flight_check(f"SELECT 1 AS n UNION ALL {fan}", org)
    assert pf.risk == "fan_trap" and pf.action == "refuse"


def test_clean_set_operation_passes_preflight():
    """A set operation with no trapped arm still passes — arm-walking must not over-refuse."""
    org = _sales_org()
    pf = rt.pre_flight_check(
        "SELECT id FROM orders UNION SELECT id FROM customers", org)
    assert pf.action == "allow" and pf.risk is None


def test_aggregating_many_side_is_allowed():
    # aggregating the MANY side (order_items) is legitimate, not a fan trap
    org = _sales_org()
    pf = rt.pre_flight_check(
        "SELECT SUM(order_items.quantity) FROM orders JOIN order_items ON order_items.order_id=orders.id",
        org)
    assert pf.action == "allow"


# --- pre-flight: what a fan-out can actually inflate ---
#
# The fan rule answers "would duplicating rows change this number?". Three inputs decide that,
# and each block below pins one: WHICH aggregate (duplication-sensitive or idempotent), WHICH
# value is being aggregated (the one-side measure, or a many-side quantity a one-side rate
# scales), and WHICH tables are even in scope (a CTE body is not).

_JOIN = "FROM orders JOIN order_items ON order_items.order_id = orders.id"


@pytest.mark.parametrize("agg", [
    "COUNT(DISTINCT orders.id)",     # duplicates are folded away before counting
    "MIN(orders.total_amount)",      # duplicating a row cannot move an extreme
    "MAX(orders.total_amount)",
    "SUM(DISTINCT orders.total_amount)",
    "AVG(DISTINCT orders.total_amount)",
    "BOOL_AND(orders.is_paid)",      # an AND/OR fold over the rows is idempotent
    "BOOL_OR(orders.is_paid)",
    "ARRAY_AGG(DISTINCT orders.status)",
])
def test_fan_immune_aggregates_are_not_fan_traps(agg):
    """An aggregate idempotent under row duplication is not inflated by a one-to-many join, so
    the query was already correct and the guard must leave it alone. Refusing it is a refusal of
    a right answer."""
    org = _sales_org()
    pf = rt.pre_flight_check(f"SELECT {agg} {_JOIN}", org)
    assert pf.risk is None and pf.action == "allow" and pf.certainty == "provable"


@pytest.mark.parametrize("agg", [
    "SUM(orders.total_amount)",      # the genuine trap: the measure IS the one-side column
    "AVG(orders.total_amount)",      # a fan duplicates rows UNEVENLY, so the mean moves too
    "COUNT(orders.id)",
    "STRING_AGG(orders.status, ',')",
    "ARRAY_AGG(orders.status)",
])
def test_fan_sensitive_aggregates_still_trip_the_fan_rule(agg):
    """The bound on the exemption above. Each of these IS inflated by the join, so each must
    still be caught — one case per aggregate so the fan-immune set cannot widen unnoticed."""
    org = _sales_org()
    pf = rt.pre_flight_check(f"SELECT {agg} {_JOIN}", org)
    assert pf.risk == "fan_trap" and pf.action != "allow"


def test_count_star_over_the_join_is_not_attributed_to_either_side():
    """`COUNT(*)` names no column, so it belongs to no table and the fan rule has nothing to
    attribute — it is left alone, exactly as before this change. Pinned so the fan-immune
    exemption is never mistaken for the reason."""
    org = _sales_org()
    pf = rt.pre_flight_check(f"SELECT COUNT(*) {_JOIN}", org)
    assert pf.risk is None and pf.action == "allow"


def test_every_is_not_read_as_an_aggregate_at_all():
    """`EVERY` is the SQL-standard spelling of `BOOL_AND` and is equally fan-immune, but the
    parser reads it as an anonymous function rather than an aggregate, so it never reaches the
    fan rule. Same verdict, different route — pinned so a parser change that starts recognising
    it does not silently turn a correct query into a refusal."""
    org = _sales_org()
    pf = rt.pre_flight_check(f"SELECT EVERY(orders.is_paid) {_JOIN}", org)
    assert pf.risk is None and pf.action == "allow"


def test_many_side_measure_scaled_by_a_one_side_rate_is_not_a_fan_trap():
    """A one-side column INSIDE the aggregate is not the same as the one-side column BEING
    aggregated. Here the aggregated value is one per order_items row and the one-side amount is
    a scalar co-factor scaling it, so nothing is duplicated — the shape every currency
    conversion and rate-scaled measure takes."""
    org = _sales_org()
    pf = rt.pre_flight_check(f"SELECT SUM(order_items.quantity * orders.total_amount) {_JOIN}", org)
    assert pf.risk is None and pf.action == "allow" and pf.certainty == "provable"


def test_one_side_dimension_grouping_a_many_side_measure_stays_allowed():
    """The correct-and-common shape: the fact is on the many side, the dimension on the one
    side. Nothing fans."""
    org = _sales_org()
    pf = rt.pre_flight_check(
        "SELECT c.region, SUM(o.amount) FROM orders o JOIN customers c ON c.id = o.customer_id "
        "GROUP BY c.region", org)
    assert pf.risk is None and pf.action == "allow"


# --- pre-flight: the scope walk ---


def test_pre_aggregating_in_a_cte_before_joining_is_not_a_fan_trap():
    """This is the shape the guard's own refusal text asks for — "pre-aggregate the one-side
    measure in a CTE before joining". `oi` is grouped to `order_id`, so it is one row per order
    and the join is 1:1. Reading `order_items` out of the CTE body and back into the outer scope
    refuses the remediation the guard just recommended."""
    org = _sales_org()
    pf = rt.pre_flight_check(
        "WITH oi AS (SELECT order_id, SUM(quantity) AS q FROM order_items GROUP BY order_id) "
        "SELECT SUM(orders.total_amount) FROM orders JOIN oi ON oi.order_id = orders.id", org)
    assert pf.risk is None and pf.action == "allow" and pf.certainty == "provable"


def test_a_grain_preserving_cte_does_not_hide_a_fan_trap():
    """The other direction, and the worse one. `o` is a plain pass-through of `orders`, so this
    is the same inflated total as the bare join — but the alias hid the one side entirely and the
    query came back clean. Every policy is one WITH clause away from bypass until this resolves."""
    org = _sales_org()
    pf = rt.pre_flight_check(
        "WITH o AS (SELECT * FROM orders) "
        "SELECT SUM(o.total_amount) FROM o JOIN order_items ON order_items.order_id = o.id", org)
    assert pf.risk == "fan_trap" and pf.action != "allow"


def test_a_grain_preserving_cte_resolves_through_another_one():
    """Resolution is transitive, so chaining pass-through CTEs is not a way around it."""
    org = _sales_org()
    pf = rt.pre_flight_check(
        "WITH o1 AS (SELECT * FROM orders), o2 AS (SELECT id, total_amount FROM o1) "
        "SELECT SUM(o2.total_amount) FROM o2 JOIN order_items ON order_items.order_id = o2.id",
        org)
    assert pf.risk == "fan_trap" and pf.action != "allow"


@pytest.mark.parametrize("body,note", [
    ("SELECT orders.id AS id, orders.total_amount AS total_amount FROM orders "
     "JOIN customers ON customers.id = orders.customer_id", "joins"),
    ("SELECT id, total_amount FROM orders UNION ALL SELECT id, total_amount FROM orders", "unions"),
    ("SELECT id, total_amount FROM mystery_table", "reads a table outside the model"),
])
def test_an_unclassifiable_cte_is_uncertain_never_a_silent_allow(body, note):
    """When the body is anything the scope walk cannot tie to one declared table, the aggregate's
    grain is unknown. "No trap found" is then a statement about what the analysis SAW, not about
    the query, and returning a bare allow presents the second as if it were the first. Governance
    does not fail closed, so the action stays allow — but it says so."""
    org = _sales_org()
    pf = rt.pre_flight_check(
        f"WITH cte AS ({body}) "
        "SELECT SUM(cte.total_amount) FROM cte JOIN order_items ON order_items.order_id = cte.id",
        org)
    assert pf.certainty == "uncertain", note
    assert pf.action == "allow"


def test_tables_in_scope_stops_at_a_cte_body_and_tables_anywhere_does_not():
    """Asserted on the two functions directly, so the leak cannot come back through a refactor.

    They differ ON PURPOSE. A cardinality question is about the tables the outer query joins, so
    `order_items` must not leak out of the CTE body. Provenance and row scoping are coverage
    questions — the answer really did read `order_items`, and its declared filters really might
    apply — so those keep the flat walk."""
    sql = ("WITH oi AS (SELECT order_id, SUM(quantity) AS q FROM order_items GROUP BY order_id) "
           "SELECT SUM(orders.total_amount) FROM orders JOIN oi ON oi.order_id = orders.id")
    tree = rt._parse_sql(sql, "postgres")
    assert "order_items" not in rt._tables_in_scope(tree).values()
    assert "order_items" in rt._tables_anywhere(tree).values()


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


# --- apply_default_filters ---


def _filter_org():
    t = m.Table(name="orders", schema="public", storage_connection="c", grain=["id"],
                description="o",
                columns=[m.Column(name="id", type="integer"),
                         m.Column(name="deleted_at", type="timestamp"),
                         m.Column(name="total", type="decimal"),
                         m.Column(name="tenant_id", type="integer")],
                default_filters=["{alias}.deleted_at IS NULL"])
    return m.Datasource(datasource="S",
                          subject_areas=[m.SubjectArea(name="s",
                              tables=[m.TableRef(storage_connection="c", schema="public", table="orders")],
                              tables_defined=[t])])


def test_apply_default_filters_with_where():
    org = _filter_org()
    new, applied = rt.apply_default_filters("SELECT SUM(o.total) FROM orders o WHERE o.total > 0",
                                            org, area="s")
    assert "deleted_at IS NULL" in new and "o.total > 0" in new and applied


def test_apply_default_filters_no_where():
    org = _filter_org()
    new, applied = rt.apply_default_filters("SELECT SUM(orders.total) FROM orders", org, area="s")
    assert "WHERE" in new.upper() and applied


def test_apply_default_filters_skips_unresolved_param():
    org = _filter_org()
    org.subject_areas[0].tables_defined[0].default_filters.append("{alias}.tenant_id = :tenant_id")
    new, applied = rt.apply_default_filters("SELECT 1 FROM orders o", org, area="s")
    assert not any("tenant_id" in a for a in applied)


# --- receipt ---


def test_build_receipt_surfaces_trust_and_rewrite():
    rel = m.Relationship(from_table="a", to_table="b", from_column="x", to_column="y",
                         relationship="many_to_one", confidence="confirmed",
                         signed_off_by="dl@x.com", signed_off_role="data_lead")
    pf = rt.PreFlightResult("fan_trap", "auto_rewrite", "SELECT SUM(a.v) FROM a JOIN b ON ...",
                            rewritten_sql="SELECT SUM(a.v) FROM a", reason="dropped fan-out join")
    receipt = rt.build_receipt(sql="SELECT SUM(a.v) FROM a", relationships_used=[rel],
                               pre_flight=pf, caveats=["heads up"],
                               default_filters_applied=["a.deleted_at IS NULL"])
    assert receipt["relationships"][0]["signed_off_by"] == "dl@x.com"
    assert receipt["pre_flight"]["action"] == "auto_rewrite"
    assert receipt["pre_flight"]["rewritten_sql"] == "SELECT SUM(a.v) FROM a"
    assert receipt["caveats"] == ["heads up"]
    assert receipt["default_filters_applied"] == ["a.deleted_at IS NULL"]
