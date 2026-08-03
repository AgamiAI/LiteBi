"""Adversarial suite for the SELECT * ban + column-scope guard.

Each case is an attempt to slip an undeclared column or a `*` past the gate. A
green happy-path suite (test_column_scope_gate.py) does NOT prove the gate holds
against evasion — these do. Cases fall into four groups:

  * star-ban evasion            -> must refuse (rule: select_star)
  * column-scope evasion        -> must refuse (rule: column_scope)
  * documented accepted fail-open -> asserts *allow*, so a future narrowing of the
                                    boundary is a conscious, test-breaking decision
  * upstream-owned              -> asserts a different layer already catches it

The set-operation cases (UNION arm) are the regressions that prove the fix for the
`parse_one -> exp.Union is not exp.Select` bypass that also affected check_table_scope.

Both gates return `guardrail.Refusal | None`, so "must refuse" reads `is not None` and
"accepted fail-open" reads `is None`. The rule id on every refusal is asserted too: an
evasion caught by the WRONG gate would otherwise pass as if the intended one held.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import guardrail  # noqa: E402
from semantic_model import models as m  # noqa: E402
from semantic_model import runtime as rt  # noqa: E402


def _scope_org():
    """Org declaring orders(id, amount, customer_id, status, payload) + customers(id, name, region).

    `payload` is typed `json` because the semi-structured residual below needs a declared column
    whose contents the model cannot describe. Every other test here ignores it.
    """
    def _t(name, cols):
        return m.Table(name=name, schema="public", storage_connection="c", grain=["id"],
                       description=name,
                       columns=[m.Column(name=c, type=typ) for c, typ in cols])
    orders = _t("orders", [("id", "integer"), ("amount", "decimal"),
                           ("customer_id", "integer"), ("status", "string"),
                           ("payload", "json")])
    customers = _t("customers", [("id", "integer"), ("name", "string"), ("region", "string")])
    return m.Datasource(datasource="Shop",
                          subject_areas=[m.SubjectArea(name="sales",
                              tables_defined=[orders, customers])])


def _assert_columns_refused(refusal, *columns: str) -> None:
    """The refusal names exactly `columns` and nothing else — the successor to `.columns == [...]`.

    Exact rather than substring: an `in` check would also pass for a refusal that had listed the
    columns the model DOES declare, which is precisely the enumeration the contract forbids.
    """
    assert refusal is not None
    assert refusal.rule == guardrail.RULE_COLUMN_SCOPE
    assert refusal.detail == ("query references column(s) not in the semantic model: "
                              + ", ".join(columns)
                              + " — only columns declared on the model's tables may be queried.")


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
    refusal = rt.check_no_select_star(sql)
    assert refusal is not None
    assert refusal.rule == guardrail.RULE_SELECT_STAR


# COUNT(*) / agg(*) must NOT be over-blocked by the star ban.
@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM orders",
    "SELECT COUNT(DISTINCT id) FROM orders",
    "SELECT status, COUNT(*) AS n FROM orders GROUP BY status",
])
def test_aggregate_star_allowed(sql):
    assert rt.check_no_select_star(sql) is None


# ===========================================================================
# Column-scope evasion -> must refuse
# ===========================================================================

# An undeclared column smuggled into a non-SELECT clause.
CLAUSE_SMUGGLES = [
    ("SELECT id FROM orders WHERE bogus > 1", "bogus"),
    ("SELECT id FROM orders GROUP BY bogus", "bogus"),
    ("SELECT customer_id, SUM(amount) FROM orders GROUP BY customer_id HAVING SUM(bogus) > 0",
     "bogus"),
    ("SELECT id FROM orders ORDER BY bogus", "bogus"),
    ("SELECT o.id FROM orders o JOIN customers c ON o.bogus = c.id", "orders.bogus"),
]


@pytest.mark.parametrize("sql,offender", CLAUSE_SMUGGLES)
def test_undeclared_column_in_any_clause_refused(sql, offender):
    _assert_columns_refused(rt.check_column_scope(sql, _scope_org()), offender)


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
    _assert_columns_refused(rt.check_column_scope(sql, _scope_org()), "bogus")


def test_alias_masquerade_refused():
    # The OUTPUT alias `id` is declared, but the underlying `bogus` is not — we
    # validate the underlying column, not the alias it is renamed to.
    _assert_columns_refused(
        rt.check_column_scope("SELECT bogus AS id FROM orders", _scope_org()), "bogus")


def test_undeclared_column_in_union_arm_refused():
    _assert_columns_refused(rt.check_column_scope(
        "SELECT id FROM orders UNION SELECT bogus FROM customers", _scope_org()), "bogus")


def test_correlated_subquery_qualified_smuggle_refused():
    # `o.bogus` is qualified to the physical `orders` — caught regardless of the
    # surrounding subquery.
    _assert_columns_refused(rt.check_column_scope(
        "SELECT o.id FROM orders o "
        "WHERE EXISTS (SELECT 1 FROM customers c WHERE c.id = o.bogus)", _scope_org()),
        "orders.bogus")


def test_undeclared_column_alongside_where_subquery_refused():
    # A WHERE/IN subquery adds no columns to the outer select's scope, so a bare
    # undeclared column in the outer query is still caught.
    _assert_columns_refused(rt.check_column_scope(
        "SELECT bogus FROM orders WHERE id IN (SELECT id FROM customers)", _scope_org()), "bogus")


def test_quoted_identifier_undeclared_refused():
    # Documents the case-insensitive-match behavior: a quoted undeclared name is
    # still refused — and echoed back with the caller's own casing.
    _assert_columns_refused(
        rt.check_column_scope('SELECT "BOGUS" FROM orders', _scope_org()), "BOGUS")


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
    assert res is None


def test_nested_output_alias_does_not_mask_outer_column():
    # An inner `AS bogus` must NOT let an unrelated outer `bogus` slip through. A
    # global output-alias set would skip the outer column; per-select scoping refuses.
    _assert_columns_refused(rt.check_column_scope(
        "SELECT bogus FROM orders WHERE id IN (SELECT id AS bogus FROM customers)",
        _scope_org()), "bogus")


# ===========================================================================
# Documented accepted fail-opens -> allow (the intended boundary)
# ===========================================================================

def test_fail_open_derived_alias_qualified_column():
    # `x.whatever` is qualified by a derived-table alias, not a physical table —
    # validated at the subquery's own body; the outer reference is not re-checked.
    res = rt.check_column_scope(
        "SELECT x.whatever FROM (SELECT id AS whatever FROM orders) x", _scope_org())
    assert res is None


def test_fail_open_cte_shadowing_table_name():
    # A CTE named after a real table shadows it; its body defines its own columns,
    # so the outer reference traces to no physical table (DB is the backstop).
    res = rt.check_column_scope(
        "WITH orders AS (SELECT 1 AS bogus) SELECT bogus FROM orders", _scope_org())
    assert res is None


# ===========================================================================
# The semi-structured residual -> the gate's unit of exposure is the COLUMN
#
# `SELECT payload:ssn` reaches `payload`, which the model declares. Nothing undeclared
# was reached, so this gate allows it and is right to: principle 4b refuses a reach to a
# table or column the model does not expose, and a field inside a declared column is
# neither. Making the unit finer than a column would amend 4b rather than fix a gate.
#
# The residual is bounded by the modelling rule REQ-021 states — a column carrying values
# that must not be readable is not declared — and the second test is that bound holding:
# undeclare the root and the reach is refused like any other. Both directions are pinned
# so a future narrowing is a conscious, test-breaking decision rather than a silent one.
# Affects every engine with path access: Snowflake VARIANT, BigQuery JSON, Postgres jsonb.
# ===========================================================================

SEMI_STRUCTURED_INTO_DECLARED = [
    "SELECT payload:ssn FROM orders",                 # Snowflake colon path
    "SELECT payload['ssn'] FROM orders",              # bracket subscript
    "SELECT payload:cust.ssn FROM orders",            # nested path
    "SELECT id FROM orders WHERE payload:ssn = 'x'",  # in a predicate, not a projection
]


@pytest.mark.parametrize("sql", SEMI_STRUCTURED_INTO_DECLARED)
def test_path_into_a_declared_column_is_allowed(sql):
    """The residual, stated as a test. This is not the gate failing — it is the gate's unit."""
    assert rt.check_column_scope(sql, _scope_org()) is None


SEMI_STRUCTURED_INTO_UNDECLARED = [
    "SELECT nope:ssn FROM orders",
    "SELECT nope['ssn'] FROM orders",
]


@pytest.mark.parametrize("sql", SEMI_STRUCTURED_INTO_UNDECLARED)
def test_path_into_an_undeclared_column_is_refused(sql):
    """The bound. Not declaring the root column is what closes the reach, and it closes it
    through the ordinary column-scope path with no semi-structured machinery at all."""
    _assert_columns_refused(rt.check_column_scope(sql, _scope_org()), "nope")


# ===========================================================================
# Upstream-owned -> a different layer catches it
# ===========================================================================

def test_multistatement_stacking_caught_by_read_only_guard():
    # Column smuggled into a stacked second statement is rejected before
    # _model_safety runs, by the read-only guard.
    import sql_guard
    assert sql_guard.check_read_only(
        "SELECT id FROM orders; SELECT * FROM secret") is not None
