"""Phase 1 (scorecard #2): column-intrinsic aggregation semantics.

`Column.aggregation` classifies how a column may be aggregated as a measure
(additive / averageable / dimension / unknown). Set by a name+type heuristic at
introspection, refined by the curator, never enforced against when `unknown`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import build  # noqa: E402
from semantic_model import curate as C  # noqa: E402
from semantic_model import introspect as I  # noqa: E402
from semantic_model import models as m  # noqa: E402
from semantic_model import validator as V  # noqa: E402

from catalog_helpers import col as _col  # noqa: E402
from catalog_helpers import make_catalog_runner

# --- classify_aggregation heuristic ----------------------------------------

@pytest.mark.parametrize("name,ctype,is_key,expected", [
    # keys / non-numeric → dimension
    ("id", "integer", True, "dimension"),
    ("customer_id", "integer", False, "dimension"),
    ("order_no", "integer", False, "dimension"),
    ("zip_code", "integer", False, "dimension"),
    ("fiscal_year", "integer", False, "dimension"),
    ("status", "string", False, "dimension"),         # non-numeric
    ("created_at", "timestamp", False, "dimension"),   # non-numeric
    ("is_active", "boolean", False, "dimension"),      # non-numeric
    # averageable (rates/prices/ratios) — even when numeric & money-ish
    ("unit_price", "decimal", False, "averageable"),
    ("discount_rate", "decimal", False, "averageable"),
    ("conversion_pct", "float", False, "averageable"),
    ("avg_balance", "decimal", False, "averageable"),  # 'avg' beats 'balance'
    ("cost_per_unit", "decimal", False, "averageable"),  # 'per' beats 'cost'
    ("credit_score", "integer", False, "averageable"),
    # additive (money / quantity / stocks)
    ("order_amount", "decimal", False, "additive"),
    ("quantity", "integer", False, "additive"),
    ("revenue", "decimal", False, "additive"),
    ("total_cost", "decimal", False, "additive"),
    ("account_balance", "decimal", False, "additive"),   # stock: summable across accounts
    ("inventory_on_hand", "integer", False, "additive"),
    # unrecognized numeric → unknown (safe; never enforced)
    ("xyz", "decimal", False, "unknown"),
    ("v_1", "float", False, "unknown"),
])
def test_classify_aggregation(name, ctype, is_key, expected):
    assert build.classify_aggregation(name, ctype, is_key=is_key) == expected


def test_default_is_unknown_on_model():
    col = m.Column(name="foo", type="decimal")
    assert col.aggregation == "unknown"  # back-compat: legacy models never falsely enforced


def test_invalid_aggregation_value_rejected():
    with pytest.raises(Exception):
        m.Column(name="foo", type="decimal", aggregation="summable")  # not in the Literal


# --- introspection stamps the class ----------------------------------------

_catalog_runner = make_catalog_runner(
    tables=["orders"],
    columns={"orders": [
        _col("id", "integer", nullable=False),
        _col("customer_id", "integer"),
        _col("amount", "numeric", scale=2),
        _col("discount_rate", "numeric", scale=4),
    ]},
    estimate=None,
)


def test_introspect_stamps_aggregation(tmp_path):
    org, _ = I.introspect("shop", "postgres", runner=_catalog_runner,
                          artifacts_dir=tmp_path, dry_run=True)
    assert V.validate(org).ok
    t = org.subject_areas[0].defined_table("orders")
    cls = {c.name: c.aggregation for c in t.columns}
    assert cls["id"] == "dimension"            # primary key
    assert cls["customer_id"] == "dimension"   # *_id
    assert cls["amount"] == "additive"
    assert cls["discount_rate"] == "averageable"


# --- curator can correct it (and the strict schema guards the value) --------

def test_curator_can_edit_aggregation(tmp_path):
    org, _ = I.introspect("shop", "postgres", runner=_catalog_runner,
                          artifacts_dir=tmp_path)  # writes the tree
    root = tmp_path / "shop"
    area = org.subject_areas[0].name
    res = C.apply(root, [{
        "op": "edit", "kind": "table", "area": area, "name": "orders",
        "column": "amount", "field": "aggregation", "value": "averageable",
    }])
    assert not res.errors, res.errors
    reloaded = __import__("semantic_model.loader", fromlist=["load_datasource"]).load_datasource(root)
    t = reloaded.subject_areas[0].defined_table("orders")
    assert t.get_column("amount").aggregation == "averageable"


def test_curator_edit_rejects_bad_value(tmp_path):
    org, _ = I.introspect("shop", "postgres", runner=_catalog_runner, artifacts_dir=tmp_path)
    root = tmp_path / "shop"
    area = org.subject_areas[0].name
    res = C.apply(root, [{
        "op": "edit", "kind": "table", "area": area, "name": "orders",
        "column": "amount", "field": "aggregation", "value": "bogus",
    }])
    # strict schema rejects the batch (reverted); the model on disk is unchanged
    assert res.errors


def test_suggest_metrics_gated_on_aggregation():
    from semantic_model import dialects as D
    t = m.Table(name="orders", schema="public", storage_connection="c", grain=["id"],
                description="o", columns=[
                    m.Column(name="id", type="integer", primary_key=True, aggregation="dimension"),
                    m.Column(name="amount", type="decimal", aggregation="additive"),
                    m.Column(name="discount_rate", type="decimal", aggregation="averageable"),
                    m.Column(name="status", type="string", aggregation="dimension"),
                    m.Column(name="weird", type="decimal", aggregation="unknown")])
    mets = build.suggest_metrics(t, D.get_dialect("postgresql"))
    names = {x["name"] for x in mets}
    assert "orders_count" in names
    assert "orders_total_amount" in names           # additive → SUM
    assert "orders_avg_discount_rate" in names      # averageable → AVG
    assert not any("status" in n or "weird" in n for n in names)  # dimension/unknown skipped
    # COUNT(*)/SUM(col)/AVG(col) are mechanically trivial -> auto-approved with a system sign-off
    assert all(x["confidence"] == "confirmed" and x["review_state"] == "approved" for x in mets)
    assert all(x["signed_off_by"] == "agami_suggest" and x["signed_off_role"] == "system" for x in mets)
    # every suggested metric is single-table -> anchored to that table for the explorer view
    assert all(x["primary_table"] == "orders" for x in mets)
    amt = next(x for x in mets if x["name"] == "orders_total_amount")
    assert amt["bindings"] == {"PostgreSQL": "SUM(amount)"} and amt["source_tables"] == ["orders"]


def test_suggest_metrics_rate_and_duration_patterns():
    from semantic_model import dialects as D
    t = m.Table(name="incident", schema="public", storage_connection="c", grain=["id"],
                description="i", columns=[
                    m.Column(name="id", type="integer", primary_key=True),
                    m.Column(name="made_sla", type="boolean"),
                    m.Column(name="is_active", type="integer"),       # int flag → rate
                    m.Column(name="opened_at", type="timestamp"),
                    m.Column(name="resolved_at", type="timestamp")])
    mets = {x["name"]: x for x in build.suggest_metrics(t, D.get_dialect("redshift"))}
    assert mets["incident_made_sla_rate"]["bindings"]["Redshift"] == \
        "AVG(CASE WHEN made_sla THEN 1.0 ELSE 0.0 END)"
    assert mets["incident_is_active_rate"]["bindings"]["Redshift"] == \
        "AVG(CASE WHEN is_active <> 0 THEN 1.0 ELSE 0.0 END)"
    dur = mets["incident_avg_duration_days"]   # start+end timestamp pair → dialect DATEDIFF
    assert dur["bindings"]["Redshift"] == "AVG(DATEDIFF('day', opened_at, resolved_at))"
    assert dur["unit"] == "days"
    # auto-approve policy: COUNT(*) is judgment-free → approved; flag RATES (CASE) and the
    # DURATION pair (heuristic start/end) carry a choice the engine could miss → stay proposed.
    assert mets["incident_count"]["review_state"] == "approved"
    assert mets["incident_count"]["confidence"] == "confirmed"
    assert mets["incident_made_sla_rate"]["review_state"] == "unreviewed"
    assert mets["incident_is_active_rate"]["review_state"] == "unreviewed"
    assert dur["review_state"] == "unreviewed" and dur["confidence"] == "proposed"


def test_suggest_metrics_skips_opaque_columns_until_described():
    from semantic_model import dialects as D
    # active_to is a boolean agami couldn't read; cost is a described additive column.
    cols = [
        m.Column(name="id", type="integer", primary_key=True),
        m.Column(name="active_to", type="boolean", description_source="ai_unknown"),
        m.Column(name="cost", type="decimal", aggregation="additive", description="acquisition cost"),
    ]
    t = m.Table(name="alm_asset", schema="public", storage_connection="c", grain=["id"],
                description="a", columns=cols)
    names = {x["name"] for x in build.suggest_metrics(t, D.get_dialect("postgresql"))}
    assert "alm_asset_active_to_rate" not in names   # opaque column → no metric proposed
    assert "alm_asset_total_cost" in names           # described column → metric proposed
    assert "alm_asset_count" in names                # COUNT(*) is column-independent, always kept

    # once the column is described, the SAME call now proposes its metric (the re-pass).
    cols[1].description_source = "ai"
    cols[1].description = "whether the asset is still within its active period"
    names2 = {x["name"] for x in build.suggest_metrics(t, D.get_dialect("postgresql"))}
    assert "alm_asset_active_to_rate" in names2


@pytest.mark.parametrize("name,values,expected_substr", [
    ("currency", ["USD", "EUR", "INR", "USD"], "ISO 4217"),
    ("ccy_code", ["USD", "GBP"], "ISO 4217"),
    ("country", ["US", "IN", "GB"], "ISO 3166"),
    ("user_timezone", ["America/New_York", "Asia/Kolkata"], "IANA"),
    ("locale", ["en", "en_US", "pt-BR"], "locale"),
    # name signal but wrong value shape → no false description
    ("currency", ["dollars", "euros"], None),
    # value shape but no name signal → no guess
    ("status", ["USD", "EUR"], None),
])
def test_canonical_description(name, values, expected_substr):
    out = build.canonical_description(name, values)
    if expected_substr is None:
        assert out is None
    else:
        assert out and expected_substr in out


def test_suggest_metrics_inherits_column_unit():
    from semantic_model import dialects as D
    t = m.Table(name="alm_asset", schema="public", storage_connection="c", grain=["id"],
                description="a", columns=[
                    m.Column(name="id", type="integer", primary_key=True),
                    m.Column(name="cost", type="decimal", aggregation="additive", unit="USD"),
                    m.Column(name="quantity", type="integer", aggregation="additive"),          # no unit
                    m.Column(name="margin_pct", type="decimal", aggregation="averageable", unit="percent")])
    mets = {x["name"]: x for x in build.suggest_metrics(t, D.get_dialect("postgresql"))}
    assert mets["alm_asset_total_cost"]["unit"] == "USD"          # SUM(cost) inherits USD
    assert mets["alm_asset_avg_margin_pct"]["unit"] == "percent"  # AVG inherits percent
    assert "unit" not in mets["alm_asset_total_quantity"]         # column has no unit → metric has none
    assert "unit" not in mets["alm_asset_count"]                  # COUNT(*) is unitless


def test_suggest_metrics_auto_approve_stamps_signoff_timestamp():
    from semantic_model import dialects as D
    t = m.Table(name="orders", schema="public", storage_connection="c", grain=["id"],
                description="o", columns=[
                    m.Column(name="id", type="integer", primary_key=True),
                    m.Column(name="amount", type="decimal", aggregation="additive")])
    mets = {x["name"]: x for x in build.suggest_metrics(
        t, D.get_dialect("postgresql"), now="2026-06-16T00:00:00Z")}
    # the trivial COUNT/SUM carry the full sign-off block (Rule-1 trust parity)
    for nm in ("orders_count", "orders_total_amount"):
        assert mets[nm]["review_state"] == "approved"
        assert mets[nm]["signed_off_at"] == "2026-06-16T00:00:00Z"
        assert mets[nm]["signed_off_by"] == "agami_suggest"
