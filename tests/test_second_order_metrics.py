"""Case (b): second-order statistics — an aggregate OF an aggregate at a finer grain
(AVG of a daily SUM). The engine deterministically synthesizes the CTE from the
declaration (base metric + inner_grain + outer agg); illegal AVG(SUM(...)) is never
emitted, and the same question yields the same defensible SQL every time.
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


def _metric(name, binding, *, base=None, inner_grain=None, tables=("orders",)):
    return m.Metric(name=name, calculation="c", bindings={"PostgreSQL": binding},
                    base_metrics=list(base or []), inner_grain=list(inner_grain or []),
                    source_tables=list(tables))


def _org(metrics):
    sa = m.SubjectArea(name="sales", description="d", metrics=metrics)
    return m.Datasource(datasource="o", version=1, subject_areas=[sa])


def test_synthesizes_avg_daily_revenue_cte():
    rev = _metric("daily_revenue", "SUM(amount)")
    avg = _metric("avg_daily_revenue", "AVG({daily_revenue})",
                  base=["daily_revenue"], inner_grain=["order_date"])
    idx = D.metric_index(_org([rev, avg]))
    sql = D.resolve_metric_sql(avg, "PostgreSQL", idx)
    assert sql == (
        "(SELECT AVG(daily_revenue) FROM ("
        "SELECT order_date, SUM(amount) AS daily_revenue "
        "FROM orders GROUP BY order_date) _inner)"
    )


def test_synthesized_sql_is_valid_and_not_nested():
    import sqlglot
    rev = _metric("monthly_orders", "COUNT(*)")
    peak = _metric("peak_monthly_orders", "MAX({monthly_orders})",
                   base=["monthly_orders"], inner_grain=["order_month"])
    idx = D.metric_index(_org([rev, peak]))
    sql = D.resolve_metric_sql(peak, "PostgreSQL", idx)
    # parses cleanly and contains NO aggregate-inside-aggregate
    tree = sqlglot.parse_one(sql)
    assert tree is not None
    assert not D._has_nested_aggregate(sql.replace("(SELECT", "").replace(")", ""))


def test_is_second_order_detection():
    assert D.is_second_order(_metric("x", "AVG({y})", base=["y"], inner_grain=["d"]))
    assert not D.is_second_order(_metric("z", "SUM(amount)"))


def test_missing_inner_grain_refuses_nested():
    # nested aggregate WITHOUT inner_grain stays an error (we can't guess the grain)
    rev = _metric("daily_revenue", "SUM(amount)")
    avg = _metric("avg_daily_revenue", "AVG({daily_revenue})", base=["daily_revenue"])
    idx = D.metric_index(_org([rev, avg]))
    with pytest.raises(D.DerivedError):
        D.resolve_metric_sql(avg, "PostgreSQL", idx)


def test_bad_shape_refused():
    # second-order binding must be exactly OUTERAGG({base})
    rev = _metric("daily_revenue", "SUM(amount)")
    bad = _metric("x", "AVG({daily_revenue}) + 1", base=["daily_revenue"], inner_grain=["d"])
    idx = D.metric_index(_org([rev, bad]))
    with pytest.raises(D.DerivedError):
        D.resolve_metric_sql(bad, "PostgreSQL", idx)


def test_multi_table_inner_refused():
    rev = _metric("daily_revenue", "SUM(amount)", tables=("orders", "refunds"))
    avg = _metric("avg_daily_revenue", "AVG({daily_revenue})",
                  base=["daily_revenue"], inner_grain=["order_date"])
    idx = D.metric_index(_org([rev, avg]))
    with pytest.raises(D.DerivedError):
        D.resolve_metric_sql(avg, "PostgreSQL", idx)


def test_validator_accepts_well_formed_second_order():
    rev = _metric("daily_revenue", "SUM(amount)")
    avg = _metric("avg_daily_revenue", "AVG({daily_revenue})",
                  base=["daily_revenue"], inner_grain=["order_date"])
    res = V.validate(_org([rev, avg]))
    assert res.ok, res.errors


def test_validator_blocks_second_order_missing_grain_via_bad_binding():
    # inner_grain set but binding isn't OUTERAGG({base}) → deploy-blocking error
    rev = _metric("daily_revenue", "SUM(amount)")
    bad = _metric("x", "SUM(amount) / 2", inner_grain=["order_date"])
    res = V.validate(_org([rev, bad]))
    assert not res.ok


def test_resolve_metrics_surfaces_synthesized_cte():
    rev = _metric("daily_revenue", "SUM(amount)")
    avg = _metric("average daily revenue", "AVG({daily_revenue})",
                  base=["daily_revenue"], inner_grain=["order_date"])
    hits = RT.resolve_metrics("average daily revenue", _org([rev, avg]))
    top = next(h for h in hits if h["metric"] == "average daily revenue")
    assert top["bindings"]["PostgreSQL"].startswith("(SELECT AVG(daily_revenue) FROM (")
