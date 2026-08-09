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
    return m.Datasource(datasource="o", version=1, subject_areas=[sa])


# --- #2 aggregation-class enforcement --------------------------------------

def test_sum_of_averageable_is_reported():
    org = _org([_col("id", "integer", "dimension"), _col("unit_price", "decimal", "averageable")])
    r = RT.pre_flight_check("SELECT SUM(unit_price) FROM facts", org)
    assert [f.risk for f in r.findings] == ["bad_aggregation"]
    assert "unit_price" in r.findings[0].reason


def test_sum_of_dimension_id_is_reported():
    org = _org([_col("customer_id", "integer", "dimension"), _col("amount", "decimal", "additive")])
    r = RT.pre_flight_check("SELECT SUM(customer_id) FROM facts", org)
    assert [f.risk for f in r.findings] == ["bad_aggregation"]


def test_avg_of_dimension_is_reported():
    org = _org([_col("zip_code", "integer", "dimension")])
    r = RT.pre_flight_check("SELECT AVG(zip_code) FROM facts", org)
    assert [f.risk for f in r.findings] == ["bad_aggregation"]


def test_sum_of_additive_is_allowed():
    org = _org([_col("amount", "decimal", "additive")])
    r = RT.pre_flight_check("SELECT SUM(amount) FROM facts", org)
    assert r.findings == []


def test_avg_of_averageable_is_allowed():
    org = _org([_col("unit_price", "decimal", "averageable")])
    r = RT.pre_flight_check("SELECT AVG(unit_price) FROM facts", org)
    assert r.findings == []


def test_unknown_class_is_never_enforced():
    # back-compat: legacy columns default to unknown and must not be flagged
    org = _org([_col("mystery", "decimal", "unknown")])
    r = RT.pre_flight_check("SELECT SUM(mystery) FROM facts", org)
    assert r.findings == []


def test_composite_sum_not_falsely_flagged():
    # SUM(price * qty) is additive revenue even though price alone is averageable
    org = _org([_col("unit_price", "decimal", "averageable"), _col("qty", "integer", "additive")])
    r = RT.pre_flight_check("SELECT SUM(unit_price * qty) FROM facts", org)
    assert r.findings == []


def test_count_of_id_is_allowed():
    org = _org([_col("customer_id", "integer", "dimension")])
    r = RT.pre_flight_check("SELECT COUNT(DISTINCT customer_id) FROM facts", org)
    assert r.findings == []


@pytest.mark.parametrize("predicate", [
    "state NOT IN (6, 7, 8)",   # exp.In      — was NOT caught, so this was a false positive
    "state BETWEEN 1 AND 3",    # exp.Between — likewise
    "state = 1",                # exp.EQ      — caught only because EQ happens to be exp.Binary
    "state IS NULL",            # exp.Is      — likewise
    "state IN (1) AND state != 9",
])
def test_a_case_predicates_column_is_not_the_summed_value(predicate):
    """`SUM(CASE WHEN <predicate> THEN 1 ELSE 0 END)` sums the literal 1, not the column the
    predicate tests. Reading the column out of the predicate reported "SUM(state) is meaningless"
    about an expression the statement never wrote — and put that beside the receipt's own
    `columns` section calling the same output the org's approved, signed-off metric.

    The old guard (`find(exp.Binary) is None`) caught `=` and `IS NULL` by accident and missed
    `IN`/`BETWEEN`, which are not Binary subclasses, so the same conditional count was flagged or
    cleared on nothing but which operator it happened to use. All five spellings are one query.
    """
    org = _org([_col("state", "integer", "dimension")])
    r = RT.pre_flight_check(
        f"SELECT SUM(CASE WHEN {predicate} THEN 1 ELSE 0 END) FROM facts", org)
    assert r.findings == []


def test_a_bare_column_is_still_the_summed_value():
    """The narrowing must not cost the check its real cases — SUM over a dimension, and over a
    DISTINCT dimension, are what this rule exists to catch."""
    org = _org([_col("state", "integer", "dimension")])
    for sql in ("SELECT SUM(state) FROM facts", "SELECT SUM(DISTINCT state) FROM facts"):
        r = RT.pre_flight_check(sql, org)
        assert [f.risk for f in r.findings] == ["bad_aggregation"], sql
        assert "state" in r.findings[0].reason


def test_the_finding_quotes_the_aggregate_the_statement_wrote():
    """The reason is synthesized from the extracted column while the `aggregate` field carries the
    parsed expression, so a wrong extraction printed a reason naming an expression that appears
    nowhere in the statement. Pin that the two agree."""
    org = _org([_col("state", "integer", "dimension")])
    r = RT.pre_flight_check("SELECT SUM(state) FROM facts", org)
    assert r.findings[0].aggregate == "SUM(state)"
    assert "SUM(state)" in r.findings[0].reason


# --- #3 semi-additive enforcement ------------------------------------------

def _balance_org():
    cols = [_col("account_id", "integer", "dimension"),
            _col("balance", "decimal", "additive"),
            _col("snapshot_date", "date", "dimension")]
    metric = m.Metric(name="total balance", calculation="sum of balances at period end",
                      bindings={"PostgreSQL": "SUM(balance)"}, source_tables=["facts"],
                      non_additive_dimensions=["time"], semi_additive_agg="last")
    return _org(cols, metrics=[metric])


def test_semi_additive_sum_over_time_is_reported():
    """The finding names the column and the metric. It no longer carries a `suggestion`: that
    field told a REFUSED caller a way forward, and naming one on an answer that came back
    presumes an intent principle 6 forbids us to presume."""
    org = _balance_org()
    sql = "SELECT snapshot_date, SUM(balance) FROM facts GROUP BY snapshot_date"
    r = RT.pre_flight_check(sql, org)
    assert [f.risk for f in r.findings] == ["semi_additive"]
    assert "balance" in r.findings[0].reason
    assert not hasattr(r.findings[0], "suggestion")


def test_semi_additive_sum_without_time_group_is_allowed():
    # summing balance across accounts (no time grain) is valid
    org = _balance_org()
    r = RT.pre_flight_check("SELECT account_id, SUM(balance) FROM facts GROUP BY account_id", org)
    assert r.findings == []


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
    org = m.Datasource(datasource="o", version=1, subject_areas=[sa])
    # ledger.balance is NOT the semi-additive one → nothing to report
    r = RT.pre_flight_check("SELECT entry_date, SUM(balance) FROM ledger GROUP BY entry_date", org)
    assert r.findings == []
    # facts.balance IS → reported
    r2 = RT.pre_flight_check("SELECT snapshot_date, SUM(balance) FROM facts GROUP BY snapshot_date", org)
    assert [f.risk for f in r2.findings] == ["semi_additive"]


def test_a_multi_column_distinct_is_not_one_bare_column():
    """`COUNT(DISTINCT a, b)` aggregates a TUPLE. DISTINCT carries a list, so unwrapping it
    blindly would hand back its first element and describe a two-column aggregate as a
    one-column one. COUNT is exempt from the class check, so this guards the extractor itself
    rather than a finding."""
    import sqlglot
    from sqlglot import exp

    agg = next(sqlglot.parse_one("SELECT COUNT(DISTINCT a, b) FROM t").find_all(exp.AggFunc))
    assert RT._bare_aggregate_column(agg) is None
