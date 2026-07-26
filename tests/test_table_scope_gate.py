"""Table-scope guard: a query may only reference tables the semantic model declares.

Runs in the SAME shared safety pass as the fan/chasm pre-flight and the sensitive
gate (execute_sql.py:_model_safety), so every engine entry point refuses a query
that touches a table outside the model — not just whichever path obeyed a prose
rule. Only physical table refs count; CTE names and derived-subquery aliases are
not tables. Excluded (review_state='rejected') tables are dropped by the loader,
so they never reach `_model_table_index` and land in the same "not declared →
refuse" path exercised here via undeclared names.
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
    """Org declaring exactly two tables: orders, customers."""
    def _t(name):
        return m.Table(name=name, schema="public", storage_connection="c", grain=["id"],
                       description=name, columns=[m.Column(name="id", type="integer")])
    return m.Datasource(datasource="Shop",
                          subject_areas=[m.SubjectArea(name="sales",
                              tables_defined=[_t("orders"), _t("customers")])])


def test_declared_table_allowed():
    assert rt.check_table_scope("SELECT * FROM orders", _scope_org()).action == "allow"


def test_undeclared_table_refused():
    res = rt.check_table_scope("SELECT * FROM sqlite_master", _scope_org())
    assert res.action == "refuse"
    assert res.offending_tables == ["sqlite_master"]
    assert "sqlite_master" in res.reason


def test_join_all_declared_allowed():
    res = rt.check_table_scope(
        "SELECT * FROM orders o JOIN customers c ON o.customer_id = c.id", _scope_org())
    assert res.action == "allow"


def test_join_with_undeclared_refused_lists_only_bad_one():
    res = rt.check_table_scope(
        "SELECT * FROM orders o JOIN payments p ON p.order_id = o.id", _scope_org())
    assert res.action == "refuse"
    assert res.offending_tables == ["payments"]


def test_cte_reference_allowed():
    # `t` is a CTE name, not a physical table — must not be flagged.
    res = rt.check_table_scope(
        "WITH t AS (SELECT * FROM orders) SELECT * FROM t", _scope_org())
    assert res.action == "allow"


def test_cte_body_referencing_undeclared_refused():
    res = rt.check_table_scope(
        "WITH t AS (SELECT * FROM secret_table) SELECT * FROM t", _scope_org())
    assert res.action == "refuse"
    assert res.offending_tables == ["secret_table"]


def test_subquery_alias_allowed():
    # derived-table alias `x` is not a table; the inner `orders` is declared.
    res = rt.check_table_scope("SELECT * FROM (SELECT id FROM orders) x", _scope_org())
    assert res.action == "allow"


def test_schema_qualified_declared_allowed():
    assert rt.check_table_scope("SELECT * FROM public.orders", _scope_org()).action == "allow"


def test_case_insensitive_match():
    assert rt.check_table_scope("SELECT * FROM ORDERS", _scope_org()).action == "allow"


def test_empty_model_allows():
    org = m.Datasource(datasource="Empty", subject_areas=[m.SubjectArea(name="s")])
    assert rt.check_table_scope("SELECT * FROM anything", org).action == "allow"


def test_set_operation_arm_scoped():
    # A UNION parses to exp.Union, not exp.Select: the guard must still scope every
    # arm (regression for the set-operation bypass), not blanket-allow.
    res = rt.check_table_scope(
        "SELECT id FROM orders UNION SELECT id FROM secret_table", _scope_org())
    assert res.action == "refuse"
    assert res.offending_tables == ["secret_table"]


def test_set_operation_all_declared_allowed():
    res = rt.check_table_scope(
        "SELECT id FROM orders UNION ALL SELECT id FROM customers", _scope_org())
    assert res.action == "allow"


def test_non_select_degrades_to_allow():
    # Non-SELECT is the upstream read-only guard's job; this gate defers (allow).
    assert rt.check_table_scope("DELETE FROM orders", _scope_org()).action == "allow"


def test_unparseable_degrades_to_allow():
    assert rt.check_table_scope("SELECT FROM WHERE ((", _scope_org()).action == "allow"
