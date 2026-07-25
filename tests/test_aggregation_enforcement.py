"""Phase 4 (scorecard #4): query-time enforcement of #2 (aggregation class) and
#3 (additivity) — the SEMANTIC checks the join-based fan/chasm detector is blind to
(they need no join). pre_flight_check refuses SUM(rate)/SUM(id)/AVG(id) and a
semi-additive SUM over a time grain.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import models as m  # noqa: E402
from semantic_model import runtime as RT  # noqa: E402


def _col(name, type_, agg):
    return m.Column(name=name, type=type_, aggregation=agg)


def _org(columns, metrics=None, table="facts"):
    t = m.Table(name=table, schema="public", storage_connection="c", grain=["id"],
                columns=columns)
    sa = m.SubjectArea(name="area", description="d", tables_defined=[t],
                       metrics=metrics or [])
    return m.Organization(datasource="o", version=1, subject_areas=[sa])


# --- #2 aggregation-class enforcement --------------------------------------

def test_sum_of_averageable_is_refused():
    org = _org([_col("id", "integer", "dimension"), _col("unit_price", "decimal", "averageable")])
    r = RT.pre_flight_check("SELECT SUM(unit_price) FROM facts", org)
    assert r.action == "refuse" and r.risk == "bad_aggregation"
    assert "unit_price" in r.reason


def test_sum_of_dimension_id_is_refused():
    org = _org([_col("customer_id", "integer", "dimension"), _col("amount", "decimal", "additive")])
    r = RT.pre_flight_check("SELECT SUM(customer_id) FROM facts", org)
    assert r.action == "refuse" and r.risk == "bad_aggregation"


def test_avg_of_dimension_is_refused():
    org = _org([_col("zip_code", "integer", "dimension")])
    r = RT.pre_flight_check("SELECT AVG(zip_code) FROM facts", org)
    assert r.action == "refuse"


def test_sum_of_additive_is_allowed():
    org = _org([_col("amount", "decimal", "additive")])
    r = RT.pre_flight_check("SELECT SUM(amount) FROM facts", org)
    assert r.action == "allow"


def test_avg_of_averageable_is_allowed():
    org = _org([_col("unit_price", "decimal", "averageable")])
    r = RT.pre_flight_check("SELECT AVG(unit_price) FROM facts", org)
    assert r.action == "allow"


def test_unknown_class_is_never_enforced():
    # back-compat: legacy columns default to unknown and must not be flagged
    org = _org([_col("mystery", "decimal", "unknown")])
    r = RT.pre_flight_check("SELECT SUM(mystery) FROM facts", org)
    assert r.action == "allow"


def test_composite_sum_not_falsely_flagged():
    # SUM(price * qty) is additive revenue even though price alone is averageable
    org = _org([_col("unit_price", "decimal", "averageable"), _col("qty", "integer", "additive")])
    r = RT.pre_flight_check("SELECT SUM(unit_price * qty) FROM facts", org)
    assert r.action == "allow"


def test_count_of_id_is_allowed():
    org = _org([_col("customer_id", "integer", "dimension")])
    r = RT.pre_flight_check("SELECT COUNT(DISTINCT customer_id) FROM facts", org)
    assert r.action == "allow"


# --- #3 semi-additive enforcement ------------------------------------------

def _balance_org():
    cols = [_col("account_id", "integer", "dimension"),
            _col("balance", "decimal", "additive"),
            _col("snapshot_date", "date", "dimension")]
    metric = m.Metric(name="total balance", calculation="sum of balances at period end",
                      bindings={"PostgreSQL": "SUM(balance)"}, source_tables=["facts"],
                      non_additive_dimensions=["time"], semi_additive_agg="last")
    return _org(cols, metrics=[metric])


def test_semi_additive_sum_over_time_is_refused():
    org = _balance_org()
    sql = "SELECT snapshot_date, SUM(balance) FROM facts GROUP BY snapshot_date"
    r = RT.pre_flight_check(sql, org)
    assert r.action == "refuse" and r.risk == "semi_additive"
    assert "balance" in r.reason and "last" in (r.suggestion or "")


def test_semi_additive_sum_without_time_group_is_allowed():
    # summing balance across accounts (no time grain) is valid
    org = _balance_org()
    r = RT.pre_flight_check("SELECT account_id, SUM(balance) FROM facts GROUP BY account_id", org)
    assert r.action == "allow"


def test_semi_additive_is_table_scoped_no_cross_table_misfire():
    # a SECOND table also has a `balance` column but its metric is fully additive — summing
    # THAT balance over time must NOT be refused (keyed by (table, column), not bare name).
    facts = m.Table(name="facts", schema="public", storage_connection="c", grain=["id"],
                    columns=[_col("balance", "decimal", "additive"), _col("snapshot_date", "date", "dimension")])
    ledger = m.Table(name="ledger", schema="public", storage_connection="c", grain=["id"],
                     columns=[_col("balance", "decimal", "additive"), _col("entry_date", "date", "dimension")])
    semi = m.Metric(name="account balance", calculation="period-end balance",
                    bindings={"PostgreSQL": "SUM(balance)"}, source_tables=["facts"],
                    non_additive_dimensions=["time"], semi_additive_agg="last")
    sa = m.SubjectArea(name="area", description="d", tables_defined=[facts, ledger], metrics=[semi])
    org = m.Organization(datasource="o", version=1, subject_areas=[sa])
    # ledger.balance is NOT the semi-additive one → allowed
    r = RT.pre_flight_check("SELECT entry_date, SUM(balance) FROM ledger GROUP BY entry_date", org)
    assert r.action == "allow"
    # facts.balance IS → refused
    r2 = RT.pre_flight_check("SELECT snapshot_date, SUM(balance) FROM facts GROUP BY snapshot_date", org)
    assert r2.action == "refuse" and r2.risk == "semi_additive"
