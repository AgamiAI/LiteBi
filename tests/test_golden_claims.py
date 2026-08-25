"""What two statements are allowed to say about each other, and what they are not.

`golden_claims` reads a statement into seven structured claims and compares two of them. It is a
DESCRIBER with two gates, and the split is the whole design: five of the seven claims are reported
for a person to read, and only two — a required column that is filtered nowhere, and a date window
that resolves to a different interval — are allowed to decide anything. Those two were selected
because neither can false-positive: a column is either constrained somewhere in the statement's own
predicates or it is not, and a window either resolves on both sides or the comparison is not made.

Three properties this file exists to hold still, because each is tempting to "improve":

* **Unresolved is not disagreement.** A window written in a form the resolver does not model reads
  `unknown` and gates nothing. Folding it to a partial interval would let a statement that filters
  correctly fail a golden item on the resolver's own incompleteness.
* **The comparison of two windows ignores the column they constrain.** Two statements over the same
  table may spell the same column `o.order_date` and `order_date`; a gate that read those as two
  different windows would refuse a correct rewrite on a qualifier. The four bound fields decide, and
  the column rides along so the report can name it.
* **The must_filter scan errs toward "filtered".** It reads every predicate the statement writes —
  WHERE, every join's ON, HAVING, QUALIFY, and an aggregate's own FILTER — because the gate's job is
  to catch an outright absence, and a scan that missed one of those spellings would call a filtered
  statement unfiltered.

Synthetic fixtures throughout: a `demo` shop over `orders` and `customers`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

from semantic_model import golden_claims as gc  # noqa: E402
from semantic_model.sql_dialect import sqlglot_dialect  # noqa: E402

# Both grammars every criterion below is re-asserted in. Resolved through `sqlglot_dialect` rather
# than written as sqlglot's own names, because that map is the one place an engine's grammar is
# decided and a test that spelled the dialect itself would keep passing after the map changed.
ENGINES = ["PostgreSQL", "SQLite"]


@pytest.mark.parametrize("engine", ENGINES)
class TestReadingAStatement:
    """One statement in, seven claims out — the reader half, before anything is compared."""

    def test_a_statement_is_read_into_the_claims_it_writes(self, engine):
        claims = gc.read_claims(
            "SELECT o.region, SUM(o.amount) AS revenue "
            "FROM orders o JOIN customers c ON o.customer_id = c.id "
            "WHERE o.status = 'paid' "
            "GROUP BY o.region ORDER BY revenue DESC LIMIT 10",
            dialect=sqlglot_dialect(engine),
        )

        assert claims.unreadable is None
        assert claims.tables == frozenset({"orders", "customers"})
        assert claims.group_keys == ("orders.region",)
        assert claims.ordering == (("revenue", "desc"),)
        assert claims.limit == 10
        assert claims.join_keys == frozenset(
            {frozenset({("orders", "customer_id"), ("customers", "id")})}
        )
        assert "orders.status" in " ".join(claims.filter_predicates)
        assert {"status", "customer_id", "id"} <= claims.filtered_columns

    def test_an_alias_and_the_table_it_names_read_the_same(self, engine):
        """Criterion 1's mechanism, isolated: a qualifier is bound to the table its own SELECT
        reads, so a rewrite that renames the alias changes no claim."""
        dialect = sqlglot_dialect(engine)
        aliased = gc.read_claims(
            "SELECT o.region FROM orders o WHERE o.status = 'paid'", dialect=dialect
        )
        spelled_out = gc.read_claims(
            "SELECT orders.region FROM orders WHERE orders.status = 'paid'", dialect=dialect
        )

        assert aliased.filter_predicates == spelled_out.filter_predicates
        assert aliased.group_keys == spelled_out.group_keys
        assert aliased.tables == spelled_out.tables

    def test_a_group_by_reads_the_same_whichever_order_it_was_written_in(self, engine):
        """GROUP BY is a set — reordering it changes no row — so comparing it as written would
        report a difference that has no effect on the answer."""
        dialect = sqlglot_dialect(engine)
        one = gc.read_claims(
            "SELECT region, status FROM orders GROUP BY region, status", dialect=dialect
        )
        other = gc.read_claims(
            "SELECT region, status FROM orders GROUP BY status, region", dialect=dialect
        )

        assert one.group_keys == other.group_keys

    def test_an_order_by_keeps_the_order_it_was_written_in(self, engine):
        """ORDER BY is not a set, and two statements sorting by the same two columns in opposite
        order return their rows in a different sequence."""
        dialect = sqlglot_dialect(engine)
        one = gc.read_claims("SELECT region FROM orders ORDER BY region, status", dialect=dialect)
        other = gc.read_claims("SELECT region FROM orders ORDER BY status, region", dialect=dialect)

        assert one.ordering != other.ordering
        assert one.ordering == (("region", "asc"), ("status", "asc"))

    def test_an_unparseable_statement_is_read_as_unreadable_rather_than_as_empty(self, engine):
        """The reason `unreadable` exists: an empty claim set must never be asked to mean both
        "the statement constrains nothing" and "the statement could not be read"."""
        claims = gc.read_claims("SELECT FROM WHERE ,", dialect=sqlglot_dialect(engine))

        assert claims.unreadable
        assert claims.tables == frozenset()
        assert claims.date_window is None

    def test_a_statement_that_is_not_a_single_select_is_read_as_unreadable(self, engine):
        """A set operation parses, but its claims belong to its arms rather than to the statement,
        and reading one arm's tables as the statement's would be a false claim rather than a
        missing one."""
        claims = gc.read_claims(
            "SELECT region FROM orders UNION SELECT region FROM customers",
            dialect=sqlglot_dialect(engine),
        )

        assert claims.unreadable
        assert claims.tables == frozenset()

    def test_reading_a_statement_never_raises(self, engine):
        """Every input below is something a generator can emit; none of them may take the eval
        run down with it."""
        for sql in ("", "   ", "not sql at all", "SELECT 'unterminated", "DELETE FROM orders"):
            assert gc.read_claims(sql, dialect=sqlglot_dialect(engine)).unreadable
