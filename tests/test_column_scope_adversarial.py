"""Adversarial suite for the SELECT * ban + column-scope guard.

Each case is an attempt to slip an undeclared column or a `*` past the gate. A
green happy-path suite (test_column_scope_gate.py) does NOT prove the gate holds
against evasion — these do. Cases fall into four groups:

  * star-ban evasion            -> must refuse (kind: select_star)
  * column-scope evasion        -> must refuse (kind: column_out_of_scope)
  * documented accepted fail-open -> asserts *allow*, so a future narrowing of the
                                    boundary is a conscious, test-breaking decision
  * upstream-owned              -> asserts a different layer already catches it

The set-operation cases (UNION arm) are the regressions that prove the fix for the
`parse_one -> exp.Union is not exp.Select` bypass that also affected check_table_scope.
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
from semantic_model import runtime as rt  # noqa: E402


def _scope_org():
    """Org declaring orders(id, amount, customer_id, status) + customers(id, name, region)."""
    def _t(name, cols):
        return m.Table(name=name, schema="public", storage_connection="c", grain=["id"],
                       description=name,
                       columns=[m.Column(name=c, type=typ) for c, typ in cols])
    orders = _t("orders", [("id", "integer"), ("amount", "decimal"),
                           ("customer_id", "integer"), ("status", "string")])
    customers = _t("customers", [("id", "integer"), ("name", "string"), ("region", "string")])
    return m.Datasource(datasource="Shop",
                          subject_areas=[m.SubjectArea(name="sales",
                              tables_defined=[orders, customers])])


# ===========================================================================
# Star-ban evasion -> must refuse
# ===========================================================================

STAR_EVASIONS = [
    "SELECT id FROM (SELECT * FROM orders) x",            # star hidden in a subquery
    "WITH t AS (SELECT * FROM orders) SELECT id FROM t",  # star hidden in a CTE body
    "SELECT id FROM orders UNION SELECT * FROM customers",  # star in a set-operation arm
    "SELECT o.* FROM orders o",                            # qualified star
    "SELECT (SELECT * FROM customers LIMIT 1) FROM orders",  # star in a scalar subquery
    "SELECT/**/ * FROM orders",                            # comment obfuscation
    "select * from orders",                                # lowercase
]


@pytest.mark.parametrize("sql", STAR_EVASIONS)
def test_star_evasion_refused(sql):
    assert rt.check_no_select_star(sql).action == "refuse"


# COUNT(*) / agg(*) must NOT be over-blocked by the star ban.
@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM orders",
    "SELECT COUNT(DISTINCT id) FROM orders",
    "SELECT status, COUNT(*) AS n FROM orders GROUP BY status",
])
def test_aggregate_star_allowed(sql):
    assert rt.check_no_select_star(sql).action == "allow"


# ===========================================================================
# Column-scope evasion -> must refuse
# ===========================================================================

# An undeclared column smuggled into a non-SELECT clause.
CLAUSE_SMUGGLES = [
    "SELECT id FROM orders WHERE bogus > 1",
    "SELECT id FROM orders GROUP BY bogus",
    "SELECT customer_id, SUM(amount) FROM orders GROUP BY customer_id HAVING SUM(bogus) > 0",
    "SELECT id FROM orders ORDER BY bogus",
    "SELECT o.id FROM orders o JOIN customers c ON o.bogus = c.id",
]


@pytest.mark.parametrize("sql", CLAUSE_SMUGGLES)
def test_undeclared_column_in_any_clause_refused(sql):
    assert rt.check_column_scope(sql, _scope_org()).action == "refuse"


# An undeclared column wrapped in an expression / function / window.
EXPR_WRAPS = [
    "SELECT UPPER(bogus) FROM orders",
    "SELECT amount + bogus FROM orders",
    "SELECT SUM(bogus) FROM orders",
    "SELECT CASE WHEN bogus > 0 THEN 1 ELSE 0 END FROM orders",
    "SELECT ROW_NUMBER() OVER (ORDER BY bogus) FROM orders",
]


@pytest.mark.parametrize("sql", EXPR_WRAPS)
def test_undeclared_column_in_expression_refused(sql):
    assert rt.check_column_scope(sql, _scope_org()).action == "refuse"


def test_alias_masquerade_refused():
    # The OUTPUT alias `id` is declared, but the underlying `bogus` is not — we
    # validate the underlying column, not the alias it is renamed to.
    res = rt.check_column_scope("SELECT bogus AS id FROM orders", _scope_org())
    assert res.action == "refuse"
    assert res.columns == ["bogus"]


def test_undeclared_column_in_union_arm_refused():
    res = rt.check_column_scope(
        "SELECT id FROM orders UNION SELECT bogus FROM customers", _scope_org())
    assert res.action == "refuse"
    assert res.columns == ["bogus"]


def test_correlated_subquery_qualified_smuggle_refused():
    # `o.bogus` is qualified to the physical `orders` — caught regardless of the
    # surrounding subquery.
    res = rt.check_column_scope(
        "SELECT o.id FROM orders o "
        "WHERE EXISTS (SELECT 1 FROM customers c WHERE c.id = o.bogus)", _scope_org())
    assert res.action == "refuse"
    assert res.columns == ["orders.bogus"]


def test_undeclared_column_alongside_where_subquery_refused():
    # A WHERE/IN subquery adds no columns to the outer select's scope, so a bare
    # undeclared column in the outer query is still caught.
    res = rt.check_column_scope(
        "SELECT bogus FROM orders WHERE id IN (SELECT id FROM customers)", _scope_org())
    assert res.action == "refuse"
    assert res.columns == ["bogus"]


def test_quoted_identifier_undeclared_refused():
    # Documents the case-insensitive-match behavior: a quoted undeclared name is
    # still refused.
    res = rt.check_column_scope('SELECT "BOGUS" FROM orders', _scope_org())
    assert res.action == "refuse"


# ===========================================================================
# Per-SELECT scope correctness (regressions for the global-map bugs)
# ===========================================================================

def test_alias_reused_across_scopes_resolves_locally():
    # `o` aliases orders in the outer query and customers in the correlated
    # subquery. A global alias map would resolve outer `o.amount` against the wrong
    # table (last-write-wins) and false-refuse; per-select resolution keeps it valid.
    res = rt.check_column_scope(
        "SELECT o.amount FROM orders o "
        "WHERE EXISTS (SELECT 1 FROM customers o WHERE o.id = 1)", _scope_org())
    assert res.action == "allow"


def test_nested_output_alias_does_not_mask_outer_column():
    # An inner `AS bogus` must NOT let an unrelated outer `bogus` slip through. A
    # global output-alias set would skip the outer column; per-select scoping refuses.
    res = rt.check_column_scope(
        "SELECT bogus FROM orders WHERE id IN (SELECT id AS bogus FROM customers)",
        _scope_org())
    assert res.action == "refuse"
    assert res.columns == ["bogus"]


# ===========================================================================
# Documented accepted fail-opens -> allow (the intended boundary)
# ===========================================================================

def test_fail_open_derived_alias_qualified_column():
    # `x.whatever` is qualified by a derived-table alias, not a physical table —
    # validated at the subquery's own body; the outer reference is not re-checked.
    res = rt.check_column_scope(
        "SELECT x.whatever FROM (SELECT id AS whatever FROM orders) x", _scope_org())
    assert res.action == "allow"


def test_fail_open_cte_shadowing_table_name():
    # A CTE named after a real table shadows it; its body defines its own columns,
    # so the outer reference traces to no physical table (DB is the backstop).
    res = rt.check_column_scope(
        "WITH orders AS (SELECT 1 AS bogus) SELECT bogus FROM orders", _scope_org())
    assert res.action == "allow"


# ===========================================================================
# Upstream-owned -> a different layer catches it
# ===========================================================================

def test_multistatement_stacking_caught_by_read_only_guard():
    # Column smuggled into a stacked second statement is rejected before
    # _model_safety runs, by the read-only guard.
    import sql_guard
    assert sql_guard.check_read_only(
        "SELECT id FROM orders; SELECT * FROM secret") is not None
