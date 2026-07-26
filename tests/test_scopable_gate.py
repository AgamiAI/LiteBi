"""Scopability gate: fail-closed when a query passes read-only but can't be fully scoped.

The object-scope gates only reject the `exp.Table` sources they FIND, so a query whose FROM/JOIN
source isn't a plain named table — a table-function / `ROWS FROM` (empty-name Table), or a
`VALUES` / `UNNEST` / `LATERAL` node — or that doesn't parse at all, would silently ALLOW. This
gate closes that: `check_scopable` returns a safety `Verdict` (`unscopable_sql`) for those, and
`None` for anything the scope checks can resolve. Runs in the same `_model_safety` pass, so every
surface fails closed identically.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

from semantic_model import models as m  # noqa: E402
from semantic_model import runtime as rt  # noqa: E402


def _scope_org():
    """Org declaring exactly two tables: orders, customers."""

    def _t(name):
        return m.Table(
            name=name,
            schema="public",
            storage_connection="c",
            grain=["id"],
            description=name,
            columns=[m.Column(name="id", type="integer")],
        )

    return m.Datasource(
        datasource="Shop",
        subject_areas=[m.SubjectArea(name="sales", tables_defined=[_t("orders"), _t("customers")])],
    )


# ── scopable → None (allow) ──────────────────────────────────────────────────

SCOPABLE = [
    "SELECT id FROM orders",  # plain named table
    "SELECT o.id FROM orders o, customers c",  # comma-join of declared tables
    "SELECT o.id FROM orders o JOIN customers c ON c.id = o.id",  # explicit join
    "WITH t AS (SELECT id FROM orders) SELECT id FROM t",  # CTE reference
    "SELECT x FROM (SELECT id AS x FROM orders) s",  # derived subquery
    "SELECT id FROM orders UNION SELECT id FROM customers",  # set-op, both arms declared
    "SELECT 1",  # no FROM — nothing to scope
]


@pytest.mark.parametrize("sql", SCOPABLE)
def test_scopable_queries_allow(sql):
    assert rt.check_scopable(sql, _scope_org()) is None


# ── unscopable → a safety Verdict (refuse) ───────────────────────────────────

UNSCOPABLE = [
    "SELECT * FROM generate_series(1, 10) AS g",  # table-function (empty-name Table)
    "SELECT * FROM ROWS FROM (generate_series(1, 3)) AS t(a)",  # ROWS FROM (empty-name Tables)
    "SELECT x FROM (VALUES (1), (2)) AS v(x)",  # VALUES source
    "SELECT x FROM UNNEST(ARRAY[1, 2]) AS t(x)",  # UNNEST source
    "SELECT a FROM orders o, LATERAL (SELECT 1 AS a) l",  # LATERAL source
    "SELECT FROM WHERE ((",  # unparseable
]


@pytest.mark.parametrize("sql", UNSCOPABLE)
def test_unscopable_queries_refuse(sql):
    v = rt.check_scopable(sql, _scope_org())
    assert v is not None
    assert v.cls == "safety" and v.rule == "unscopable_sql" and v.certainty == "provable"


def test_unscopable_source_in_a_set_op_arm_is_caught():
    # A table-function hidden in one UNION arm must be refused, not just the first (declared) arm.
    v = rt.check_scopable(
        "SELECT id FROM orders UNION SELECT * FROM generate_series(1, 3) g", _scope_org()
    )
    assert v is not None and v.rule == "unscopable_sql"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT o.id FROM orders o, (VALUES (1), (2)) AS v(x)",  # VALUES as the 2nd comma-join source
        "SELECT o.id FROM orders o, UNNEST(ARRAY[1, 2]) AS u(x)",  # UNNEST as the 2nd comma-join source
        "SELECT o.id FROM orders o, generate_series(1, 10) AS t(g)",  # table-fn as the 2nd source
    ],
)
def test_unscopable_comma_join_source_is_caught(sql):
    # Copilot review: an unscopable source appearing as an ADDITIONAL comma-join source — the FIRST
    # source being a declared table — must still be refused. The gate walks EVERY FROM/JOIN source
    # (sqlglot normalizes `FROM t1, <src>` to a Join whose `.this` is <src>; other versions hang it on
    # `From.expressions`), so a valid leading table can't shield an unscopable trailing source.
    v = rt.check_scopable(sql, _scope_org())
    assert v is not None and v.rule == "unscopable_sql", sql


def test_empty_model_allows_even_an_unscopable_query():
    # A deployment with no declared surface isn't scoping — the gate is inert (like the scope gates).
    empty = m.Datasource(datasource="Empty", subject_areas=[m.SubjectArea(name="s")])
    assert rt.check_scopable("SELECT * FROM generate_series(1, 10)", empty) is None


def test_sqlglot_unavailable_fails_closed(monkeypatch):
    # Without a parser we can't scope anything -> fail closed (refuse), never degrade to allow.
    monkeypatch.setattr(rt, "_HAVE_SQLGLOT", False)
    v = rt.check_scopable("SELECT id FROM orders", _scope_org())
    assert v is not None and v.rule == "unscopable_sql"


def test_parseable_but_no_select_fails_closed():
    # A parseable non-SELECT (the read-only guard blocks these upstream; the gate still fails closed).
    v = rt.check_scopable("SHOW TABLES", _scope_org())
    assert v is not None and v.rule == "unscopable_sql"


def test_hive_lateral_view_is_unscopable():
    # `LATERAL VIEW` attaches to the SELECT (not a From/Join.this), so the whole-tree LATERAL sweep
    # is what catches it — the gate is self-sufficient, not reliant on downstream column/table scope.
    v = rt.check_scopable("SELECT x FROM orders LATERAL VIEW explode(arr) t AS x", _scope_org())
    assert v is not None and v.rule == "unscopable_sql"


def test_gate_reuses_ctx_tree_no_second_parse():
    # Parity: with vs without the prebuilt GuardContext yields the same verdict (single parse reused).
    org = _scope_org()
    for sql in SCOPABLE + [s for s in UNSCOPABLE if s != "SELECT FROM WHERE (("]:
        ctx = rt.build_guard_context(sql, org)
        assert rt.check_scopable(sql, org) == rt.check_scopable(sql, org, ctx=ctx)
