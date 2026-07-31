"""SELECT * ban + column-scope guard: a projected/referenced column must be named
and declared on the table it binds to.

Enforced in the SAME shared safety pass as the table-scope and sensitive gates
(execute_sql.py:_model_safety), so every engine entry point refuses a query that
names an undeclared column — not just whichever path obeyed a prose rule. Posture:
strict where a column binds to a declared physical table; fail-open on
CTE/subquery-output and select-list-alias columns. The evasion attempts and the
documented fail-open boundary live in test_column_scope_adversarial.py.

Both gates return `guardrail.Refusal | None`: `None` is "satisfied", a `Refusal` is
a stop.
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


def _assert_star_refused(refusal) -> None:
    """The star ban's refusal, in full. It names no identifier at all — the offending token is
    `*` — so the whole text is static and can be pinned exactly."""
    assert refusal is not None
    assert refusal.rule == guardrail.RULE_SELECT_STAR
    assert refusal.reason == "out_of_scope"
    assert refusal.detail == ("query uses SELECT * — every column must be named so it can be "
                              "checked against the semantic model.")
    assert refusal.remediation == "List the columns explicitly instead of '*'."


def _assert_columns_refused(refusal, *columns: str) -> None:
    """The refusal names exactly `columns` and nothing else.

    Replaces the old `.columns == [...]` + `"x" in .reason` pair: the offending names are now
    echoed into `detail`. Exact equality rather than substring is strictly stronger — it also
    catches a refusal that had grown a list of the columns the model DOES declare.
    """
    assert refusal is not None
    assert refusal.rule == guardrail.RULE_COLUMN_SCOPE
    assert refusal.reason == "out_of_scope"
    assert refusal.detail == ("query references column(s) not in the semantic model: "
                              + ", ".join(columns)
                              + " — only columns declared on the model's tables may be queried.")
    assert refusal.remediation == ("Add the column to the model (agami-connect / '/agami-model'), "
                                   "or remove it from the query.")


# --- SELECT * ban ----------------------------------------------------------

def test_select_star_refused():
    _assert_star_refused(rt.check_no_select_star("SELECT * FROM orders"))


def test_qualified_star_refused():
    _assert_star_refused(rt.check_no_select_star("SELECT o.* FROM orders o"))


def test_named_columns_allowed():
    assert rt.check_no_select_star("SELECT id, amount FROM orders") is None


def test_count_star_allowed():
    # COUNT(*) is not a projection-level star.
    assert rt.check_no_select_star("SELECT COUNT(*) FROM orders") is None


def test_star_non_select_degrades_to_allow():
    # Non-SELECT is the upstream read-only guard's job; this gate defers (allow).
    assert rt.check_no_select_star("DELETE FROM orders") is None


def test_star_unparseable_degrades_to_allow():
    assert rt.check_no_select_star("SELECT FROM WHERE ((") is None


# --- column-scope: declared columns pass -----------------------------------

def test_declared_columns_allowed():
    assert rt.check_column_scope("SELECT id, amount FROM orders", _scope_org()) is None


def test_qualified_declared_column_allowed():
    assert rt.check_column_scope("SELECT o.amount FROM orders o", _scope_org()) is None


def test_join_column_from_each_side_allowed():
    res = rt.check_column_scope(
        "SELECT o.amount, c.name FROM orders o JOIN customers c ON o.customer_id = c.id",
        _scope_org())
    assert res is None


def test_join_ambiguous_but_declared_allowed():
    # `id` exists on both tables — declared, so allow (don't false-reject on ambiguity).
    res = rt.check_column_scope(
        "SELECT id FROM orders o JOIN customers c ON o.customer_id = c.id", _scope_org())
    assert res is None


def test_declared_column_in_where_and_group_allowed():
    res = rt.check_column_scope(
        "SELECT status, COUNT(*) FROM orders WHERE amount > 0 GROUP BY status", _scope_org())
    assert res is None


# --- column-scope: undeclared columns refused ------------------------------

def test_undeclared_column_refused():
    _assert_columns_refused(rt.check_column_scope("SELECT bogus FROM orders", _scope_org()),
                            "bogus")


def test_qualified_undeclared_column_refused():
    _assert_columns_refused(rt.check_column_scope("SELECT o.bogus FROM orders o", _scope_org()),
                            "orders.bogus")


def test_undeclared_column_in_where_refused():
    _assert_columns_refused(
        rt.check_column_scope("SELECT id FROM orders WHERE bogus > 1", _scope_org()), "bogus")


def test_cte_body_undeclared_column_refused():
    # `bogus` binds directly to the physical `orders` inside the CTE body -> caught.
    _assert_columns_refused(rt.check_column_scope(
        "WITH t AS (SELECT bogus FROM orders) SELECT id FROM t", _scope_org()), "bogus")


# --- column-scope: legitimate complex SQL passes ---------------------------

def test_cte_output_column_allowed():
    res = rt.check_column_scope(
        "WITH t AS (SELECT id, amount FROM orders) SELECT id, amount FROM t", _scope_org())
    assert res is None


def test_subquery_derived_column_allowed():
    res = rt.check_column_scope(
        "SELECT x.total FROM (SELECT SUM(amount) AS total FROM orders) x", _scope_org())
    assert res is None


def test_select_list_alias_reuse_allowed():
    res = rt.check_column_scope(
        "SELECT amount AS a FROM orders ORDER BY a", _scope_org())
    assert res is None


def test_case_insensitive_match():
    assert rt.check_column_scope("SELECT ID, AMOUNT FROM orders", _scope_org()) is None


# --- column-scope: degrade-to-allow ----------------------------------------

def test_column_empty_model_allows():
    org = m.Datasource(datasource="Empty", subject_areas=[m.SubjectArea(name="s")])
    assert rt.check_column_scope("SELECT anything FROM whatever", org) is None


def test_column_non_select_degrades_to_allow():
    assert rt.check_column_scope("DELETE FROM orders", _scope_org()) is None


def test_column_unparseable_degrades_to_allow():
    assert rt.check_column_scope("SELECT FROM WHERE ((", _scope_org()) is None
