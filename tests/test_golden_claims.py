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

import json
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
        assert claims.filter_predicates == frozenset(
            {"eq(orders.status, 'paid')", "eq(orders.customer_id, customers.id)"}
        )
        assert {"status", "customer_id", "id"} <= claims.filtered_columns

    def test_a_claim_key_is_structural_rather_than_sql(self, engine):
        """Regenerating SQL from a parsed tree is banned across this package, and the ban is right
        here for a second reason: a claim rides on output the calling model reads as
        server-authored, and a claim that looked like SQL would be read as SQL. So the operator is
        NAMED rather than spelled, and nothing in the key can be mistaken for a statement."""
        claims = gc.read_claims(
            "SELECT 1 FROM orders o WHERE o.order_date BETWEEN '2025-01-01' AND '2025-12-31'",
            dialect=sqlglot_dialect(engine),
        )

        assert claims.filter_predicates == frozenset(
            {"between(orders.order_date, '2025-01-01', '2025-12-31')"}
        )

    def test_a_predicate_deeper_than_the_key_renders_is_cut_off_rather_than_raising(self, engine):
        """sqlglot builds a wide OR LEFT-DEEP, so a generated predicate is a tree as deep as it is
        wide — and rendering it must not be what takes an eval run down."""
        wide = " OR ".join(f"o.region = '{n}'" for n in range(40))
        claims = gc.read_claims(
            f"SELECT 1 FROM orders o WHERE {wide}", dialect=sqlglot_dialect(engine)
        )

        assert claims.unreadable is None
        assert [key for key in claims.filter_predicates if "\u2026" in key]

    def test_a_bracketed_predicate_reads_as_the_predicate_inside_it(self, engine):
        """A bracket is the author's readability rather than a change of meaning, so two statements
        that differ only in their brackets must produce one key — at the top of a conjunct, where
        the AND flattener unwraps it, and nested inside one, where this module does."""
        dialect = sqlglot_dialect(engine)
        for bracketed, plain in (
            ("(o.status = 'paid')", "o.status = 'paid'"),
            ("o.amount > (100)", "o.amount > 100"),
        ):
            read = [
                gc.read_claims(f"SELECT 1 FROM orders o WHERE {sql}", dialect=dialect)
                for sql in (bracketed, plain)
            ]
            assert read[0].filter_predicates == read[1].filter_predicates

    def test_a_limit_that_is_not_a_plain_integer_reads_as_no_limit(self, engine):
        """A computed row cap is not a number two statements can be compared on, so it reads as
        absent rather than as some wrong count."""
        claims = gc.read_claims(
            "SELECT 1 FROM orders o LIMIT 1 + 1", dialect=sqlglot_dialect(engine)
        )

        assert claims.limit is None

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
        dialect = sqlglot_dialect(engine)
        for sql in ("", "   ", "not sql at all", "SELECT 'unterminated", "DELETE FROM orders"):
            assert gc.read_claims(sql, dialect=dialect).unreadable

        # The three below PARSE, and reading one used to raise anyway: a numeric literal SQL
        # accepts happily but `int()` does not. A number this module cannot compare is a claim it
        # declines to make, which is the same None as any other shape it does not model.
        assert gc.read_claims("SELECT 1 FROM orders o LIMIT 1.5", dialect=dialect).limit is None
        assert gc.read_claims("SELECT 1 FROM orders o LIMIT 1e3", dialect=dialect).limit is None
        assert (
            gc.read_claims(
                "SELECT 1 FROM orders o WHERE EXTRACT(YEAR FROM o.order_date) = 2025.5",
                dialect=dialect,
            ).date_window
            is None
        )


def _window(sql: str, engine: str):
    """The window one WHERE clause over `orders` resolves to."""
    return gc.read_claims(
        f"SELECT o.region FROM orders o WHERE {sql}", dialect=sqlglot_dialect(engine)
    ).date_window


@pytest.mark.parametrize("engine", ENGINES)
class TestResolvingADateWindow:
    """The temporal fold: three spellings in, one interval out — or nothing at all."""

    def test_three_spellings_of_a_year_resolve_to_one_interval(self, engine):
        """A calendar year written as a half-open comparison chain, as the same chain against a
        typed date literal, and as an EXTRACT are the same question, and a golden item that failed
        one against another would be failing on spelling."""
        chained = _window("o.order_date >= '2025-01-01' AND o.order_date < '2026-01-01'", engine)
        typed = _window(
            "o.order_date >= DATE '2025-01-01' AND o.order_date < DATE '2026-01-01'", engine
        )
        extracted = _window("EXTRACT(YEAR FROM o.order_date) = 2025", engine)

        for resolved in (chained, typed, extracted):
            assert (resolved.start, resolved.start_inclusive) == ("2025-01-01", True)
            assert (resolved.end, resolved.end_inclusive) == ("2026-01-01", False)
        assert chained.column == "orders.order_date"

    def test_an_inclusive_upper_bound_is_never_shifted_to_the_next_day(self, engine):
        """Reading `BETWEEN … AND '2025-12-31'` as `< '2026-01-01'` is only sound on a DATE column,
        and nothing this module is handed says the column is one. So the bound stays as written and
        the two intervals are reported as the different intervals they are on a timestamp."""
        between = _window("o.order_date BETWEEN '2025-01-01' AND '2025-12-31'", engine)

        assert (between.start, between.start_inclusive) == ("2025-01-01", True)
        assert (between.end, between.end_inclusive) == ("2025-12-31", True)

    def test_a_bound_written_with_the_literal_first_reads_the_same(self, engine):
        """`'2025-01-01' <= d` is `d >= '2025-01-01'` written the other way round, so the bound it
        puts on the column is the mirror of the operator, not the operator itself."""
        mirrored = _window("'2025-01-01' <= o.order_date AND '2026-01-01' > o.order_date", engine)

        assert (mirrored.start, mirrored.start_inclusive) == ("2025-01-01", True)
        assert (mirrored.end, mirrored.end_inclusive) == ("2026-01-01", False)

    def test_a_spelling_the_resolver_does_not_model_resolves_to_nothing(self, engine):
        """The resolver's incompleteness must read as `unknown`, never as an interval it guessed —
        a guessed interval is what would fail a statement that filters correctly."""
        assert _window("DATE_TRUNC('quarter', o.order_date) = '2025-04-01'", engine) is None

    def test_a_partial_reduction_is_discarded_whole(self, engine):
        """One conjunct over the date column reduced and the other did not, so what reduced is
        HALF the constraint — and half an interval reported as a whole one is exactly the shape
        that gates a correct statement."""
        assert (
            _window(
                "o.order_date >= '2025-01-01' AND DATE_TRUNC('quarter', o.order_date) = "
                "'2025-04-01'",
                engine,
            )
            is None
        )

    def test_two_columns_carrying_a_window_resolve_to_nothing(self, engine):
        """A window over two columns is a shape this module does not model, and picking one of them
        would make the answer depend on the order the conjuncts were written in."""
        assert (
            _window("o.order_date >= '2025-01-01' AND o.shipped_date < '2026-01-01'", engine)
            is None
        )

    def test_two_bounds_on_one_side_that_disagree_resolve_to_nothing(self, engine):
        """Two lower bounds are two claims about where the interval starts, and picking the wider
        or the narrower would be this module deciding which one the author meant."""
        assert (
            _window("o.order_date >= '2025-01-01' AND o.order_date >= '2025-02-01'", engine) is None
        )

    def test_a_range_over_something_that_is_not_a_date_is_not_a_window(self, engine):
        """A BETWEEN and a comparison are temporal only when what they compare against is a date;
        reading a money range as an interval would invent a window the statement never wrote."""
        assert _window("o.amount BETWEEN 1 AND 10", engine) is None
        assert _window("o.amount > 100", engine) is None

    def test_a_comparison_between_two_literals_is_not_a_bound(self, engine):
        """Neither side names a column, so there is nothing for the comparison to bound — and it
        must not disturb the window the rest of the WHERE does resolve."""
        resolved = _window("1 < 2 AND o.order_date >= '2025-01-01'", engine)

        assert (resolved.start, resolved.start_inclusive) == ("2025-01-01", True)

    def test_only_a_year_is_read_out_of_an_extract(self, engine):
        """A month or a quarter pulled out the same way is a real interval too, but one no corpus
        here exercises — and an interval derived from an unexercised rule is the kind that gates a
        correct statement. An extracted year compared against a column is not a bound either."""
        assert _window("EXTRACT(MONTH FROM o.order_date) = 4", engine) is None
        assert _window("EXTRACT(YEAR FROM o.order_date) = o.fiscal_year", engine) is None

    def test_a_statement_with_no_temporal_predicate_resolves_to_nothing(self, engine):
        assert _window("o.status = 'paid'", engine) is None

    def test_a_predicate_on_another_column_does_not_disturb_the_window(self, engine):
        """The unreduced conjuncts that matter are the ones touching the date column; a filter on
        an unrelated column is not evidence that the window is partial."""
        resolved = _window(
            "o.status = 'paid' AND o.order_date >= '2025-01-01' AND o.order_date < '2026-01-01'",
            engine,
        )

        assert (resolved.start, resolved.end) == ("2025-01-01", "2026-01-01")


def _diff(generated_sql: str, golden_sql: str, engine: str, must_filter=()):
    return gc.compare_statements(
        generated_sql, golden_sql, must_filter=must_filter, dialect=sqlglot_dialect(engine)
    )


def _claim(diff, name: str):
    return next(claim for claim in diff.claims if claim.name == name)


# The statement a golden item would carry: a filtered, windowed, joined, grouped, ordered, capped
# aggregate over the demo shop. Every one of the seven claims is present in it, which is what lets
# the rewrite below assert that all seven AGREE rather than that none of them differs.
GOLDEN_SHAPE = (
    "SELECT o.region, SUM(o.amount) AS revenue "
    "FROM orders o JOIN customers c ON o.customer_id = c.id "
    "WHERE o.status = 'paid' AND o.order_date >= '2025-01-01' AND o.order_date < '2026-01-01' "
    "GROUP BY o.region ORDER BY revenue DESC LIMIT 10"
)


@pytest.mark.parametrize("engine", ENGINES)
class TestComparingTwoStatements:
    """Seven claims out, and exactly two of them allowed to decide anything."""

    def test_the_claim_set_is_exactly_seven_claims(self, engine):
        """Seven is the contract, not an implementation detail: an eighth claim changes what a
        golden item is allowed to assert about a statement."""
        diff = _diff(GOLDEN_SHAPE, GOLDEN_SHAPE, engine)

        assert gc.CLAIM_NAMES == (
            "tables",
            "filter_predicates",
            "date_window",
            "group_keys",
            "join_keys",
            "ordering",
            "limit",
        )
        assert len(diff.claims) == 7
        assert tuple(claim.name for claim in diff.claims) == gc.CLAIM_NAMES

    def test_an_aliased_reordered_rewrite_agrees_on_every_claim(self, engine):
        """The property the whole module rests on: two spellings of one question produce identical
        claims. If this ever reports a difference, every difference the module reports is
        suspect."""
        rewritten = (
            "SELECT SUM(ord.amount) AS revenue, ord.region "
            "FROM orders ord JOIN customers cust ON ord.customer_id = cust.id "
            "WHERE ord.order_date < '2026-01-01' AND ord.status = 'paid' "
            "AND ord.order_date >= '2025-01-01' "
            "GROUP BY ord.region ORDER BY revenue DESC LIMIT 10"
        )
        diff = _diff(rewritten, GOLDEN_SHAPE, engine)

        assert [claim.status for claim in diff.claims] == [gc.AGREES] * 7
        assert diff.gates == []
        assert diff.gated is False

    def test_a_window_shifted_by_a_quarter_differs_and_names_both(self, engine):
        """The failure this module exists to explain: same tables, same grouping, same everything
        except the interval — which a row count cannot tell apart from a different question."""
        diff = _diff(
            "SELECT SUM(o.amount) FROM orders o "
            "WHERE o.order_date >= '2025-04-01' AND o.order_date < '2025-07-01'",
            "SELECT SUM(o.amount) FROM orders o "
            "WHERE o.order_date >= '2025-01-01' AND o.order_date < '2025-04-01'",
            engine,
        )
        window = _claim(diff, "date_window")

        assert window.status == gc.DIFFERS
        assert window.generated["start"] == "2025-04-01"
        assert window.golden["start"] == "2025-01-01"
        assert window.generated["column"] == window.golden["column"] == "orders.order_date"
        assert [gate.kind for gate in diff.gates] == ["date_window"]
        assert diff.gated is True

    def test_a_between_upper_bound_does_not_agree_with_the_half_open_year(self, engine):
        """The two intervals differ at the upper bound and nowhere else, and the claim carries both
        bounds so a reader can see which one moved."""
        diff = _diff(
            "SELECT SUM(o.amount) FROM orders o "
            "WHERE o.order_date BETWEEN '2025-01-01' AND '2025-12-31'",
            "SELECT SUM(o.amount) FROM orders o "
            "WHERE o.order_date >= '2025-01-01' AND o.order_date < '2026-01-01'",
            engine,
        )
        window = _claim(diff, "date_window")

        assert window.status == gc.DIFFERS
        assert window.generated["start"] == window.golden["start"] == "2025-01-01"
        assert (window.generated["end"], window.generated["end_inclusive"]) == ("2025-12-31", True)
        assert (window.golden["end"], window.golden["end_inclusive"]) == ("2026-01-01", False)
        assert [gate.kind for gate in diff.gates] == ["date_window"]

    def test_a_window_agrees_however_the_column_is_qualified(self, engine):
        """Agreement is decided on the four bound fields and NOT on the column, because two
        statements over one table may qualify it differently — and refusing a correct rewrite over
        a qualifier is exactly the false gate the two gates were selected to avoid."""
        diff = _diff(
            "SELECT SUM(o.amount) FROM orders o WHERE o.order_date >= '2025-01-01'",
            "SELECT SUM(amount) FROM orders WHERE order_date >= '2025-01-01'",
            engine,
        )

        assert _claim(diff, "date_window").status == gc.AGREES
        assert diff.gates == []

    def test_an_omitted_must_filter_column_gates_and_names_the_column(self, engine):
        """The other gate: the dataset says this column must be constrained, and the statement
        constrains it nowhere. The verdict names the column, and its reason carries no statement
        text, so it is printable beside a failing item."""
        diff = _diff(
            "SELECT SUM(o.amount) FROM orders o WHERE o.status = 'paid'",
            "SELECT SUM(o.amount) FROM orders o WHERE o.status = 'paid' AND o.region = 'EU'",
            engine,
            must_filter=["region"],
        )

        assert [(gate.kind, gate.column) for gate in diff.gates] == [("must_filter", "region")]
        assert diff.gated is True
        assert "region" not in diff.gates[0].reason
        assert "SELECT" not in diff.gates[0].reason

    def test_a_join_on_predicate_counts_as_filtering(self, engine):
        """A required column constrained in a join's ON is constrained. A gate that only read the
        WHERE would fail a statement that filters correctly, which is the one thing these two gates
        may never do."""
        diff = _diff(
            "SELECT SUM(o.amount) FROM orders o "
            "JOIN customers c ON o.customer_id = c.id AND c.region = 'EU'",
            "SELECT SUM(o.amount) FROM orders o JOIN customers c ON o.customer_id = c.id "
            "WHERE c.region = 'EU'",
            engine,
            must_filter=["region"],
        )

        assert diff.gates == []
        assert diff.gated is False

    def test_a_predicate_moved_into_the_aggregate_differs_without_gating(self, engine):
        """`SUM(x) FILTER (WHERE …)` filters the aggregate rather than the row set, so the two
        statements really do differ on their filtering predicates — and the column is still plainly
        constrained, so the difference is REPORTED and nothing gates."""
        diff = _diff(
            "SELECT SUM(o.amount) FILTER (WHERE o.region = 'EU') FROM orders o",
            "SELECT SUM(o.amount) FROM orders o WHERE o.region = 'EU'",
            engine,
            must_filter=["region"],
        )

        assert _claim(diff, "filter_predicates").status == gc.DIFFERS
        assert diff.gates == []
        assert diff.gated is False

    def test_a_predicate_the_resolver_cannot_fold_gates_nothing(self, engine):
        """A window this module does not model reads `unknown`, and `unknown` never gates. The
        column is still mentioned in a predicate, so the required-filter gate stays quiet too."""
        diff = _diff(
            "SELECT SUM(o.amount) FROM orders o "
            "WHERE DATE_TRUNC('quarter', o.order_date) = '2025-04-01'",
            "SELECT SUM(o.amount) FROM orders o "
            "WHERE o.order_date >= '2025-01-01' AND o.order_date < '2026-01-01'",
            engine,
            must_filter=["order_date"],
        )

        assert _claim(diff, "date_window").status == gc.UNKNOWN
        assert diff.gates == []
        assert diff.gated is False

    def test_an_unparseable_statement_reports_unknown_rather_than_raising(self, engine):
        """A generator can emit anything. Every claim reads `unknown` — not `differs`, which would
        be a definite comparison against a statement that was never read — and nothing gates, for
        the same reason."""
        diff = _diff("SELECT FROM WHERE ,", GOLDEN_SHAPE, engine, must_filter=["region"])

        assert [claim.status for claim in diff.claims] == [gc.UNKNOWN] * 7
        assert diff.gates == []
        assert diff.gated is False


@pytest.mark.parametrize("engine", ENGINES)
class TestWhatTheDiffIsAllowedToCarry:
    """The output contract: JSON-able, deterministic, and never a statement."""

    def test_the_diff_survives_a_json_round_trip(self, engine):
        """The diff is handed to a stdlib-only renderer that reads JSON, so every set in it has to
        have become a sorted list on the way out — and sorted rather than merely listed, because a
        report has to read the same way twice for the same pair of statements."""
        diff = _diff(GOLDEN_SHAPE, GOLDEN_SHAPE, engine, must_filter=["region"])

        rendered = diff.as_dict()
        assert json.loads(json.dumps(rendered)) == rendered

        claims = gc.read_claims(GOLDEN_SHAPE, dialect=sqlglot_dialect(engine)).as_dict()
        assert json.loads(json.dumps(claims)) == claims

    def test_two_runs_over_one_pair_render_identically(self, engine):
        """Frozensets iterate in an order that is stable within a process and not across them, so
        anything derived from one has to be sorted before it is rendered."""
        first = json.dumps(_diff(GOLDEN_SHAPE, GOLDEN_SHAPE, engine).as_dict())
        second = json.dumps(_diff(GOLDEN_SHAPE, GOLDEN_SHAPE, engine).as_dict())

        assert first == second

    def test_the_diff_never_echoes_a_statement(self, engine):
        """The diff rides on output the calling model reads as server-authored, so it carries
        identifiers, bounds and counts — never a clause of the SQL it was derived from. Asserted
        three ways: the statement does not appear whole, no clause keyword appears among the values
        the diff carries (the schema's own field names are ours, so they are not scanned), and no
        three-word span of the statement survives into the JSON."""
        diff = _diff(GOLDEN_SHAPE, GOLDEN_SHAPE, engine, must_filter=["region"])
        rendered = json.dumps(diff.as_dict())
        carried = json.dumps(
            [[claim.generated, claim.golden] for claim in diff.claims]
            + [gate.reason for gate in diff.gates]
        ).upper()

        assert GOLDEN_SHAPE not in rendered
        for keyword in ("SELECT", "FROM", "JOIN", "GROUP BY", "ORDER BY", "WHERE", "UNION"):
            assert keyword not in carried, keyword
        words = GOLDEN_SHAPE.split()
        spans = {" ".join(words[i : i + 3]) for i in range(len(words) - 2)}
        assert [span for span in spans if span in rendered] == []
        # ...and it is not vacuous: the claims that ARE carried name the model's own objects.
        assert "orders" in rendered and "2026-01-01" in rendered


def test_the_two_engines_are_two_different_grammars():
    """Guard against a matrix that re-asserts every criterion twice in one grammar, which would
    look identical from the outside and prove half as much."""
    dialects = {sqlglot_dialect(engine) for engine in ENGINES}

    assert len(dialects) == len(ENGINES)


def test_the_module_imports_no_client():
    """No model call and no network egress: the comparison is deterministic in code, which is what
    lets one eval run be compared against another."""
    source = (PKG_SRC / "semantic_model" / "golden_claims.py").read_text()

    for forbidden in ("requests", "httpx", "urllib", "socket", "http.client", "openai"):
        assert forbidden not in source, forbidden


def test_the_module_exposes_no_score():
    """One implementation per eval mode: the deterministic scorer folds these claims in, and a
    second scoring surface living here would be a second answer to one question."""
    assert [name for name in dir(gc) if "score" in name.lower()] == []
