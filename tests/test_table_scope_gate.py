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
    assert rt.check_table_scope("SELECT * FROM orders", _scope_org()) is None


def test_undeclared_table_refused():
    res = rt.check_table_scope("SELECT * FROM sqlite_master", _scope_org())
    assert res is not None
    assert "sqlite_master" in res.detail


def test_join_all_declared_allowed():
    res = rt.check_table_scope(
        "SELECT * FROM orders o JOIN customers c ON o.customer_id = c.id", _scope_org())
    assert res is None


def test_join_with_undeclared_refused_lists_only_bad_one():
    res = rt.check_table_scope(
        "SELECT * FROM orders o JOIN payments p ON p.order_id = o.id", _scope_org())
    assert res is not None
    assert "payments" in res.detail
    assert "orders" not in res.detail  # only the undeclared table is named, not the declared one


def test_cte_reference_allowed():
    # `t` is a CTE name, not a physical table — must not be flagged.
    res = rt.check_table_scope(
        "WITH t AS (SELECT * FROM orders) SELECT * FROM t", _scope_org())
    assert res is None


def test_cte_body_referencing_undeclared_refused():
    res = rt.check_table_scope(
        "WITH t AS (SELECT * FROM secret_table) SELECT * FROM t", _scope_org())
    assert res is not None
    assert "secret_table" in res.detail


def test_subquery_alias_allowed():
    # derived-table alias `x` is not a table; the inner `orders` is declared.
    res = rt.check_table_scope("SELECT * FROM (SELECT id FROM orders) x", _scope_org())
    assert res is None


def test_schema_qualified_declared_allowed():
    assert rt.check_table_scope("SELECT * FROM public.orders", _scope_org()) is None


def test_schema_qualified_undeclared_schema_refused():
    # `orders` is declared, but ONLY in `public`. A reference to a same-named table in a DIFFERENT,
    # undeclared schema targets a table the model never declared — refuse it, so bare-name matching
    # can't admit `secret_schema.orders` when the datasource role can see more than one schema.
    res = rt.check_table_scope("SELECT * FROM secret_schema.orders", _scope_org())
    assert res is not None
    assert res.rule == "table_scope"
    assert "secret_schema.orders" in res.detail  # the offending qualified ref is named


def test_schema_qualified_undeclared_schema_refused_in_a_join():
    # The same-named-but-wrong-schema table hidden in a JOIN is caught too (whole-tree table sweep),
    # and the correctly-schema'd table alongside it is NOT flagged.
    res = rt.check_table_scope(
        "SELECT * FROM public.orders o JOIN hr.customers c ON c.id = o.customer_id", _scope_org())
    assert res is not None
    assert "hr.customers" in res.detail
    assert "public.orders" not in res.detail  # the correctly-declared schema-qualified table is fine


def test_unqualified_name_still_matches_by_name():
    # A bare (unqualified) reference to a declared table stays allowed — we don't require a schema
    # qualifier; the datasource search_path resolves it. Only a WRONG explicit schema is refused.
    assert rt.check_table_scope("SELECT * FROM orders", _scope_org()) is None


def test_case_insensitive_match():
    assert rt.check_table_scope("SELECT * FROM ORDERS", _scope_org()) is None


def test_empty_model_allows():
    org = m.Datasource(datasource="Empty", subject_areas=[m.SubjectArea(name="s")])
    assert rt.check_table_scope("SELECT * FROM anything", org) is None


def test_set_operation_arm_scoped():
    # A UNION parses to exp.Union, not exp.Select: the guard must still scope every
    # arm (regression for the set-operation bypass), not blanket-allow.
    res = rt.check_table_scope(
        "SELECT id FROM orders UNION SELECT id FROM secret_table", _scope_org())
    assert res is not None
    assert "secret_table" in res.detail
    assert "orders" not in res.detail  # the declared arm isn't flagged — only the undeclared one


def test_set_operation_all_declared_allowed():
    res = rt.check_table_scope(
        "SELECT id FROM orders UNION ALL SELECT id FROM customers", _scope_org())
    assert res is None


def test_non_select_degrades_to_allow():
    # Non-SELECT is the upstream read-only guard's job; this gate defers (allow).
    assert rt.check_table_scope("DELETE FROM orders", _scope_org()) is None


def test_unparseable_degrades_to_allow():
    assert rt.check_table_scope("SELECT FROM WHERE ((", _scope_org()) is None
