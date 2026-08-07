"""The frozen verdict table for the three semantic-model scope gates.

**Every expectation in this file was generated from the PRE-REFACTOR code** — the
`TableScopeResult` / `StarCheckResult` / `ColumnScopeResult` era, harvested at
`446cc20` before a single line of the `Refusal | None` conversion was written. That
ordering is the whole point: a table written after a refactor can only ever agree with
the refactor, while this one was fixed in advance and the refactor has to reproduce it.
It is kept permanently, because the later slices move this same enforcement surface
again and a dropped check here is a data breach, not a failing assertion.

`GOLDEN_VERDICTS` is the union of every SQL string the four guard suites
(test_table_scope_gate, test_column_scope_gate, test_column_scope_adversarial,
test_guard_context) run through the gates, paired with the model fixture that suite runs
it against. Each row is run through ALL THREE gates, not just the one its original suite
exercised, so the table is a denser pin than the suites it was harvested from.

What each row asserts:
  * `None` <-> the pre-refactor "allow", a `Refusal` <-> the pre-refactor "refuse" — the
    polarity, which is the one thing a mechanical rewrite can invert silently.
  * the refusal's `rule` and `reason` are the pinned contract values for that gate.
  * `detail` and `remediation` are byte-exact — reconstructed from static prose plus the
    offending identifiers this table records. Byte-exact is deliberate: it pins the
    "echo the caller's own identifiers, never enumerate the declared surface" property,
    because any *extra* identifier that leaked in would break the equality.
  * a `ctx` call and a non-`ctx` call return equal objects (the ACE-045 parity property,
    now a plain `==` on a frozen dataclass).
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

# --- fixtures: verbatim copies of the four suites' model builders ------------
#
# Copied rather than imported so this table stays readable on its own and cannot be
# silently re-pointed at a different model by an edit to another test file.

def _fx_table_scope() -> m.Datasource:
    """test_table_scope_gate.py::_scope_org — orders + customers, one `id` column each."""
    def _t(name):
        return m.Table(name=name, schema="public", storage_connection="c", grain=["id"],
                       description=name, columns=[m.Column(name="id", type="integer")])
    return m.Datasource(datasource="Shop",
                        subject_areas=[m.SubjectArea(name="sales",
                            tables_defined=[_t("orders"), _t("customers")])])


def _fx_column_scope() -> m.Datasource:
    """test_column_scope_gate.py / test_column_scope_adversarial.py::_scope_org."""
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


def _fx_guard_ctx() -> m.Datasource:
    """test_guard_context.py::_org — adds a sensitive column, a default_filter, a relationship."""
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


def _fx_empty() -> m.Datasource:
    """The zero-declared-tables model both scope suites use for the degrade-to-allow case."""
    return m.Datasource(datasource="Empty", subject_areas=[m.SubjectArea(name="s")])


FIXTURES = {
    "table_scope": _fx_table_scope,
    "column_scope": _fx_column_scope,
    "guard_ctx": _fx_guard_ctx,
    "empty": _fx_empty,
}


# --- the pinned prose -------------------------------------------------------
#
# Harvested verbatim from the pre-refactor `reason` / `suggestion` fields. The rewrite
# carries these across unchanged: they are already echo-only (static prose plus the
# identifiers the caller itself sent), and rewording one is how an enumeration of the
# declared surface would get introduced without anybody noticing.

def _table_scope_detail(names: tuple[str, ...]) -> str:
    return ("query references table(s) not in the semantic model: " + ", ".join(names)
            + " — only tables declared in the model may be queried.")


_TABLE_SCOPE_REMEDIATION = ("Add the table to the model (agami-connect / '/agami-model'), "
                            "or remove it from the query.")

_SELECT_STAR_DETAIL = ("query uses SELECT * — every column must be named so it can be "
                       "checked against the semantic model.")

_SELECT_STAR_REMEDIATION = "List the columns explicitly instead of '*'."


def _column_scope_detail(names: tuple[str, ...]) -> str:
    return ("query references column(s) not in the semantic model: " + ", ".join(names)
            + " — only columns declared on the model's tables may be queried.")


_COLUMN_SCOPE_REMEDIATION = ("Add the column to the model (agami-connect / '/agami-model'), "
                             "or remove it from the query.")


# --- the frozen table -------------------------------------------------------
#
# (fixture, sql,
#  table_scope verdict, its offending tables, select_star verdict,
#  column_scope verdict, its offending columns)

GOLDEN_VERDICTS: tuple[tuple[str, str, str, tuple[str, ...], str, str, tuple[str, ...]], ...] = (
    # ---- harvested from tests/test_table_scope_gate.py ----
    ('table_scope', 'SELECT * FROM orders',
     'allow', (), 'refuse', 'allow', ()),
    ('table_scope', 'SELECT * FROM sqlite_master',
     'refuse', ('sqlite_master',), 'refuse', 'allow', ()),
    ('table_scope', 'SELECT * FROM orders o JOIN customers c ON o.customer_id = c.id',
     'allow', (), 'refuse', 'refuse', ('orders.customer_id',)),
    ('table_scope', 'SELECT * FROM orders o JOIN payments p ON p.order_id = o.id',
     'refuse', ('payments',), 'refuse', 'allow', ()),
    ('table_scope', 'WITH t AS (SELECT * FROM orders) SELECT * FROM t',
     'allow', (), 'refuse', 'allow', ()),
    ('table_scope', 'WITH t AS (SELECT * FROM secret_table) SELECT * FROM t',
     'refuse', ('secret_table',), 'refuse', 'allow', ()),
    ('table_scope', 'SELECT * FROM (SELECT id FROM orders) x',
     'allow', (), 'refuse', 'allow', ()),
    ('table_scope', 'SELECT * FROM public.orders',
     'allow', (), 'refuse', 'allow', ()),
    ('table_scope', 'SELECT * FROM ORDERS',
     'allow', (), 'refuse', 'allow', ()),
    ('table_scope', 'SELECT id FROM orders UNION SELECT id FROM secret_table',
     'refuse', ('secret_table',), 'allow', 'allow', ()),
    ('table_scope', 'SELECT id FROM orders UNION ALL SELECT id FROM customers',
     'allow', (), 'allow', 'allow', ()),
    ('table_scope', 'DELETE FROM orders',
     'allow', (), 'allow', 'allow', ()),
    ('table_scope', 'SELECT FROM WHERE ((',
     'allow', (), 'allow', 'allow', ()),
    ('empty', 'SELECT * FROM anything',
     'allow', (), 'refuse', 'allow', ()),

    # ---- harvested from tests/test_column_scope_gate.py ----
    ('column_scope', 'SELECT * FROM orders',
     'allow', (), 'refuse', 'allow', ()),
    ('column_scope', 'SELECT o.* FROM orders o',
     'allow', (), 'refuse', 'refuse', ('orders.*',)),
    ('column_scope', 'SELECT id, amount FROM orders',
     'allow', (), 'allow', 'allow', ()),
    ('column_scope', 'SELECT COUNT(*) FROM orders',
     'allow', (), 'allow', 'allow', ()),
    ('column_scope', 'DELETE FROM orders',
     'allow', (), 'allow', 'allow', ()),
    ('column_scope', 'SELECT FROM WHERE ((',
     'allow', (), 'allow', 'allow', ()),
    ('column_scope', 'SELECT o.amount FROM orders o',
     'allow', (), 'allow', 'allow', ()),
    ('column_scope', 'SELECT o.amount, c.name FROM orders o JOIN customers c ON o.customer_id = c.id',
     'allow', (), 'allow', 'allow', ()),
    ('column_scope', 'SELECT id FROM orders o JOIN customers c ON o.customer_id = c.id',
     'allow', (), 'allow', 'allow', ()),
    ('column_scope', 'SELECT status, COUNT(*) FROM orders WHERE amount > 0 GROUP BY status',
     'allow', (), 'allow', 'allow', ()),
    ('column_scope', 'SELECT bogus FROM orders',
     'allow', (), 'allow', 'refuse', ('bogus',)),
    ('column_scope', 'SELECT o.bogus FROM orders o',
     'allow', (), 'allow', 'refuse', ('orders.bogus',)),
    ('column_scope', 'SELECT id FROM orders WHERE bogus > 1',
     'allow', (), 'allow', 'refuse', ('bogus',)),
    ('column_scope', 'WITH t AS (SELECT bogus FROM orders) SELECT id FROM t',
     'allow', (), 'allow', 'refuse', ('bogus',)),
    ('column_scope', 'WITH t AS (SELECT id, amount FROM orders) SELECT id, amount FROM t',
     'allow', (), 'allow', 'allow', ()),
    ('column_scope', 'SELECT x.total FROM (SELECT SUM(amount) AS total FROM orders) x',
     'allow', (), 'allow', 'allow', ()),
    ('column_scope', 'SELECT amount AS a FROM orders ORDER BY a',
     'allow', (), 'allow', 'allow', ()),
    ('column_scope', 'SELECT ID, AMOUNT FROM orders',
     'allow', (), 'allow', 'allow', ()),
    ('empty', 'SELECT anything FROM whatever',
     'allow', (), 'allow', 'allow', ()),

    # ---- harvested from tests/test_column_scope_adversarial.py ----
    ('column_scope', 'SELECT id FROM (SELECT * FROM orders) x',
     'allow', (), 'refuse', 'allow', ()),
    ('column_scope', 'WITH t AS (SELECT * FROM orders) SELECT id FROM t',
     'allow', (), 'refuse', 'allow', ()),
    ('column_scope', 'SELECT id FROM orders UNION SELECT * FROM customers',
     'allow', (), 'refuse', 'allow', ()),
    ('column_scope', 'SELECT (SELECT * FROM customers LIMIT 1) FROM orders',
     'allow', (), 'refuse', 'allow', ()),
    ('column_scope', 'SELECT/**/ * FROM orders',
     'allow', (), 'refuse', 'allow', ()),
    ('column_scope', 'select * from orders',
     'allow', (), 'refuse', 'allow', ()),
    ('column_scope', 'SELECT COUNT(DISTINCT id) FROM orders',
     'allow', (), 'allow', 'allow', ()),
    ('column_scope', 'SELECT status, COUNT(*) AS n FROM orders GROUP BY status',
     'allow', (), 'allow', 'allow', ()),
    ('column_scope', 'SELECT id FROM orders GROUP BY bogus',
     'allow', (), 'allow', 'refuse', ('bogus',)),
    ('column_scope', 'SELECT customer_id, SUM(amount) FROM orders GROUP BY customer_id HAVING SUM(bogus) > 0',
     'allow', (), 'allow', 'refuse', ('bogus',)),
    ('column_scope', 'SELECT id FROM orders ORDER BY bogus',
     'allow', (), 'allow', 'refuse', ('bogus',)),
    ('column_scope', 'SELECT o.id FROM orders o JOIN customers c ON o.bogus = c.id',
     'allow', (), 'allow', 'refuse', ('orders.bogus',)),
    ('column_scope', 'SELECT UPPER(bogus) FROM orders',
     'allow', (), 'allow', 'refuse', ('bogus',)),
    ('column_scope', 'SELECT amount + bogus FROM orders',
     'allow', (), 'allow', 'refuse', ('bogus',)),
    ('column_scope', 'SELECT SUM(bogus) FROM orders',
     'allow', (), 'allow', 'refuse', ('bogus',)),
    ('column_scope', 'SELECT CASE WHEN bogus > 0 THEN 1 ELSE 0 END FROM orders',
     'allow', (), 'allow', 'refuse', ('bogus',)),
    ('column_scope', 'SELECT ROW_NUMBER() OVER (ORDER BY bogus) FROM orders',
     'allow', (), 'allow', 'refuse', ('bogus',)),
    ('column_scope', 'SELECT bogus AS id FROM orders',
     'allow', (), 'allow', 'refuse', ('bogus',)),
    ('column_scope', 'SELECT id FROM orders UNION SELECT bogus FROM customers',
     'allow', (), 'allow', 'refuse', ('bogus',)),
    ('column_scope', 'SELECT o.id FROM orders o WHERE EXISTS (SELECT 1 FROM customers c WHERE c.id = o.bogus)',
     'allow', (), 'allow', 'refuse', ('orders.bogus',)),
    ('column_scope', 'SELECT bogus FROM orders WHERE id IN (SELECT id FROM customers)',
     'allow', (), 'allow', 'refuse', ('bogus',)),
    ('column_scope', 'SELECT "BOGUS" FROM orders',
     'allow', (), 'allow', 'refuse', ('BOGUS',)),
    ('column_scope', 'SELECT o.amount FROM orders o WHERE EXISTS (SELECT 1 FROM customers o WHERE o.id = 1)',
     'allow', (), 'allow', 'allow', ()),
    ('column_scope', 'SELECT bogus FROM orders WHERE id IN (SELECT id AS bogus FROM customers)',
     'allow', (), 'allow', 'refuse', ('bogus',)),
    ('column_scope', 'SELECT x.whatever FROM (SELECT id AS whatever FROM orders) x',
     'allow', (), 'allow', 'allow', ()),
    ('column_scope', 'WITH orders AS (SELECT 1 AS bogus) SELECT bogus FROM orders',
     'allow', (), 'allow', 'allow', ()),
    # Owned upstream by the read-only guard; pinned here too so the corpus is a true union
    # of the four suites and a stacked statement's scope verdicts cannot drift unobserved.
    ('column_scope', 'SELECT id FROM orders; SELECT * FROM secret',
     'refuse', ('secret',), 'refuse', 'allow', ()),

    # ---- harvested from tests/test_guard_context.py ----
    ('guard_ctx', 'SELECT customers.name, COUNT(orders.id) AS n FROM customers JOIN orders ON orders.customer_id = customers.id GROUP BY customers.name',
     'allow', (), 'allow', 'allow', ()),
    ('guard_ctx', 'SELECT * FROM orders',
     'allow', (), 'refuse', 'allow', ()),
    ('guard_ctx', 'SELECT customers.email FROM customers',
     'allow', (), 'allow', 'allow', ()),
    ('guard_ctx', 'SELECT customers.bogus_col FROM customers',
     'allow', (), 'allow', 'refuse', ('customers.bogus_col',)),
    ('guard_ctx', 'SELECT ghost.x FROM ghost',
     'refuse', ('ghost',), 'allow', 'allow', ()),
    ('guard_ctx', 'SELECT customers.name FROM customers',
     'allow', (), 'allow', 'allow', ()),
    ('guard_ctx', 'SELECT COUNT(orders.id) AS n FROM orders',
     'allow', (), 'allow', 'allow', ()),
    ('guard_ctx', 'NOT SQL AT ALL ;;;',
     'allow', (), 'allow', 'allow', ()),
)


def _ids() -> list[str]:
    return [f"{fixture}::{sql}" for fixture, sql, *_ in GOLDEN_VERDICTS]


def _check(refusal, verdict: str, rule: str, detail: str, remediation: str) -> None:
    """Assert one gate's `Refusal | None` against one pre-refactor verdict."""
    if verdict == "allow":
        assert refusal is None
        return
    assert refusal is not None
    assert refusal.rule == rule
    assert refusal.reason == guardrail.REASON_FOR_RULE[rule]
    # Byte-exact, not `in`: an `in` check would still pass if the refusal had grown a list of
    # the tables/columns the model DOES declare, which is exactly the disclosure to prevent.
    assert refusal.detail == detail
    assert refusal.remediation == remediation


@pytest.mark.parametrize(
    "fixture,sql,ts_verdict,ts_tables,star_verdict,cs_verdict,cs_columns",
    GOLDEN_VERDICTS, ids=_ids())
def test_gate_verdicts_match_the_pre_refactor_table(
        fixture, sql, ts_verdict, ts_tables, star_verdict, cs_verdict, cs_columns):
    """Each gate reproduces the verdict the pre-refactor code gave this statement."""
    org = FIXTURES[fixture]()

    _check(rt.check_table_scope(sql, org), ts_verdict, guardrail.RULE_TABLE_SCOPE,
           _table_scope_detail(ts_tables), _TABLE_SCOPE_REMEDIATION)
    _check(rt.check_no_select_star(sql), star_verdict, guardrail.RULE_SELECT_STAR,
           _SELECT_STAR_DETAIL, _SELECT_STAR_REMEDIATION)
    _check(rt.check_column_scope(sql, org), cs_verdict, guardrail.RULE_COLUMN_SCOPE,
           _column_scope_detail(cs_columns), _COLUMN_SCOPE_REMEDIATION)


@pytest.mark.parametrize(
    "fixture,sql,ts_verdict,ts_tables,star_verdict,cs_verdict,cs_columns",
    GOLDEN_VERDICTS, ids=_ids())
def test_ctx_and_non_ctx_agree_across_the_table(
        fixture, sql, ts_verdict, ts_tables, star_verdict, cs_verdict, cs_columns):
    """A shared `GuardContext` changes only the cost, never the verdict — over the whole table
    rather than the six statements test_guard_context.py samples."""
    org = FIXTURES[fixture]()
    ctx = rt.build_guard_context(sql, org)

    assert rt.check_table_scope(sql, org) == rt.check_table_scope(sql, org, ctx=ctx)
    assert rt.check_no_select_star(sql) == rt.check_no_select_star(sql, ctx=ctx)
    assert rt.check_column_scope(sql, org) == rt.check_column_scope(sql, org, ctx=ctx)


def test_table_is_deduplicated():
    """A duplicated (fixture, sql) row would silently halve a gate's real coverage."""
    keys = [(fixture, sql) for fixture, sql, *_ in GOLDEN_VERDICTS]
    assert len(keys) == len(set(keys))


def test_table_still_covers_every_polarity():
    """Both polarities are present for all three gates, so an all-allow (or all-refuse) rewrite
    cannot pass this file by accident."""
    for column, gate in ((2, "table_scope"), (4, "select_star"), (5, "column_scope")):
        verdicts = {row[column] for row in GOLDEN_VERDICTS}
        assert verdicts == {"allow", "refuse"}, gate
