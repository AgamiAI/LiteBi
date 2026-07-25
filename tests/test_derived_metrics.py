"""Phase 3 (scorecard #1): composable derived metrics.

A derived metric defines its binding via {base metric} placeholders; the resolver
expands them into standalone SQL (single source of truth, no drift). Cycles,
unknown bases, and second-order (aggregate-of-aggregate) nestings are refused.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import derived as D  # noqa: E402
from semantic_model import models as m  # noqa: E402
from semantic_model import runtime as RT  # noqa: E402
from semantic_model import validator as V  # noqa: E402


def _metric(name, binding, *, base=None, calc="x", tables=("orders",)):
    return m.Metric(name=name, calculation=calc, bindings={"PostgreSQL": binding},
                    base_metrics=list(base or []), source_tables=list(tables))


def _org(metrics):
    sa = m.SubjectArea(name="sales", description="d", metrics=metrics)
    return m.Organization(datasource="o", version=1, subject_areas=[sa])


# --- resolver ---------------------------------------------------------------

def test_expand_simple_ratio():
    rev = _metric("revenue", "SUM(amount)")
    cnt = _metric("order_count", "COUNT(DISTINCT order_id)")
    aov = _metric("avg_order_value", "{revenue} / {order_count}",
                  base=["revenue", "order_count"])
    idx = D.metric_index(_org([rev, cnt, aov]))
    assert D.expand_binding(aov, "PostgreSQL", idx) == "(SUM(amount)) / (COUNT(DISTINCT order_id))"


def test_expand_is_recursive():
    rev = _metric("revenue", "SUM(amount)")
    cnt = _metric("order_count", "COUNT(*)")
    aov = _metric("aov", "{revenue} / {order_count}", base=["revenue", "order_count"])
    margin = _metric("aov_x2", "{aov} * 2", base=["aov"])
    idx = D.metric_index(_org([rev, cnt, aov, margin]))
    assert D.expand_binding(margin, "PostgreSQL", idx) == "((SUM(amount)) / (COUNT(*))) * 2"


def test_unknown_base_raises():
    bad = _metric("x", "{nonexistent} + 1", base=["nonexistent"])
    idx = D.metric_index(_org([bad]))
    with pytest.raises(D.DerivedError):
        D.expand_binding(bad, "PostgreSQL", idx)


def test_cycle_raises():
    a = _metric("a", "{b} + 1", base=["b"])
    b = _metric("b", "{a} + 1", base=["a"])
    idx = D.metric_index(_org([a, b]))
    with pytest.raises(D.DerivedError):
        D.expand_binding(a, "PostgreSQL", idx)


def test_second_order_nesting_refused():
    # AVG of a daily SUM = aggregate of an aggregate → needs CTE (deferred to #4)
    daily = _metric("daily_revenue", "SUM(amount)")
    avg = _metric("avg_daily_revenue", "AVG({daily_revenue})", base=["daily_revenue"])
    idx = D.metric_index(_org([daily, avg]))
    with pytest.raises(D.DerivedError) as e:
        D.expand_binding(avg, "PostgreSQL", idx)
    assert "second-order" in str(e.value) or "#4" in str(e.value)


def test_is_derived_detection():
    assert D.is_derived(_metric("x", "{revenue} / 2", base=["revenue"]))
    assert D.is_derived(_metric("y", "{revenue} / 2"))      # placeholder, no base_metrics
    assert not D.is_derived(_metric("z", "SUM(amount)"))


# --- validator gate ---------------------------------------------------------

def test_validator_blocks_cycle():
    a = _metric("a", "{b} + 1", base=["b"])
    b = _metric("b", "{a} + 1", base=["a"])
    res = V.validate(_org([a, b]))
    assert not res.ok
    assert any("cycle" in e for e in res.errors)


def test_validator_passes_clean_derived():
    rev = _metric("revenue", "SUM(amount)")
    cnt = _metric("order_count", "COUNT(*)")
    aov = _metric("aov", "{revenue} / {order_count}", base=["revenue", "order_count"])
    res = V.validate(_org([rev, cnt, aov]))
    assert res.ok, res.errors


def test_validator_warns_cross_grain():
    rev = _metric("revenue", "SUM(amount)", tables=("orders",))
    cust = _metric("customer_count", "COUNT(*)", tables=("customers",))
    rpc = _metric("revenue_per_customer", "{revenue} / {customer_count}",
                  base=["revenue", "customer_count"], tables=("orders",))
    res = V.validate(_org([rev, cust, rpc]))
    assert res.ok  # warning, not error
    assert any("grain" in w for w in res.warnings)


# --- runtime surfacing -------------------------------------------------------

def test_resolve_metrics_surfaces_composed_sql():
    rev = _metric("revenue", "SUM(amount)")
    cnt = _metric("order_count", "COUNT(DISTINCT order_id)")
    aov = _metric("average order value", "{revenue} / {order_count}",
                  base=["revenue", "order_count"])
    hits = RT.resolve_metrics("average order value", _org([rev, cnt, aov]))
    top = next(h for h in hits if h["metric"] == "average order value")
    assert top["bindings"]["PostgreSQL"] == "(SUM(amount)) / (COUNT(DISTINCT order_id))"
