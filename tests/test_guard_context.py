"""ACE-045 Slice 1: the shared `GuardContext` parses the SQL once and builds each model
index once, and every guard returns an identical verdict with or without a `ctx` — so
threading it through `_model_safety` is behaviour-preserving, only cheaper."""

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


def _org():
    customers = m.Table(
        name="customers", schema="public", storage_connection="c", grain=["id"],
        default_filters=["{alias}.active = true"],
        columns=[
            m.Column(name="id", type="integer"),
            m.Column(name="name", type="string"),
            m.Column(name="email", type="string", sensitive=True),
        ],
    )
    orders = m.Table(
        name="orders", schema="public", storage_connection="c", grain=["id"],
        columns=[
            m.Column(name="id", type="integer"),
            m.Column(name="customer_id", type="integer"),
            m.Column(name="total_amount", type="decimal", aggregation="additive"),
        ],
    )
    rels = [m.Relationship(from_table="orders", to_table="customers",
                           from_column="customer_id", to_column="id",
                           relationship="many_to_one")]
    return m.Datasource(
        datasource="Shop",
        subject_areas=[m.SubjectArea(name="sales", tables_defined=[customers, orders],
                                     relationships=rels)],
    )


_CLEAN = ("SELECT customers.name, COUNT(orders.id) AS n FROM customers "
          "JOIN orders ON orders.customer_id = customers.id GROUP BY customers.name")


def test_build_context_parses_and_indexes_each_once(monkeypatch):
    """build_guard_context does the shared work once; the guards given `ctx` add none."""
    org = _org()
    counts = {"parse": 0, "_column_index": 0, "_cardinality_index": 0,
              "_sensitive_by_table": 0, "_model_table_index": 0}

    real_parse = rt.sqlglot.parse_one
    monkeypatch.setattr(rt.sqlglot, "parse_one",
                        lambda *a, **k: (counts.__setitem__("parse", counts["parse"] + 1)
                                         or real_parse(*a, **k)))
    for name in ("_column_index", "_cardinality_index", "_sensitive_by_table", "_model_table_index"):
        real = getattr(rt, name)

        def wrapper(org, _real=real, _name=name):
            counts[_name] += 1
            return _real(org)

        monkeypatch.setattr(rt, name, wrapper)

    # A query on `orders` only — which declares no default_filters — so apply_default_filters
    # injects nothing and we isolate the "guards don't re-parse the query SQL" claim (a table
    # WITH default_filters legitimately parses each filter fragment to inject it).
    sql = "SELECT COUNT(orders.id) AS n FROM orders"
    ctx = rt.build_guard_context(sql, org)
    # Full battery WITH ctx — none of these should parse or rebuild an index again.
    rt.check_table_scope(sql, org, ctx=ctx)
    rt.check_no_select_star(sql, ctx=ctx)
    rt.check_column_scope(sql, org, ctx=ctx)
    rt.pre_flight_check(sql, org, ctx=ctx)
    rt.check_sensitive_projection(sql, org, ctx=ctx)
    rt.apply_default_filters(sql, org, ctx=ctx)

    assert counts == {"parse": 1, "_column_index": 1, "_cardinality_index": 1,
                      "_sensitive_by_table": 1, "_model_table_index": 1}


@pytest.mark.parametrize("sql", [
    _CLEAN,                                             # allow
    "SELECT * FROM orders",                             # star ban
    "SELECT customers.email FROM customers",            # sensitive projection refuse
    "SELECT customers.bogus_col FROM customers",        # column-scope refuse
    "SELECT ghost.x FROM ghost",                        # table-scope refuse
    "SELECT customers.name FROM customers",             # allow + default_filter applied
])
def test_verdict_parity_with_and_without_ctx(sql):
    """Every guard returns byte-identical results whether it builds its own work or is
    handed a shared ctx — the behaviour-preserving guarantee."""
    org = _org()
    ctx = rt.build_guard_context(sql, org)
    assert rt.check_table_scope(sql, org) == rt.check_table_scope(sql, org, ctx=ctx)
    assert rt.check_no_select_star(sql) == rt.check_no_select_star(sql, ctx=ctx)
    assert rt.check_column_scope(sql, org) == rt.check_column_scope(sql, org, ctx=ctx)
    assert rt.pre_flight_check(sql, org).as_dict() == rt.pre_flight_check(sql, org, ctx=ctx).as_dict()
    assert (rt.check_sensitive_projection(sql, org).as_dict()
            == rt.check_sensitive_projection(sql, org, ctx=ctx).as_dict())
    assert rt.apply_default_filters(sql, org) == rt.apply_default_filters(sql, org, ctx=ctx)


def test_unparseable_sql_ctx_tree_is_none_and_guards_allow():
    """A GuardContext over unparseable SQL carries tree=None; guards degrade to allow,
    matching the standalone path."""
    org = _org()
    bad = "NOT SQL AT ALL ;;;"
    ctx = rt.build_guard_context(bad, org)
    assert rt.check_table_scope(bad, org, ctx=ctx) is None
    assert rt.check_no_select_star(bad, ctx=ctx) is None
