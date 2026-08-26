"""Running a golden dataset: what reaches the generator, what reaches the warehouse, what is scored.

A golden run asks a model to answer a question the answer to which is already agreed, runs both
statements, and scores one against the other. Two properties decide whether the resulting number
means anything at all, and neither is visible in the score:

* **The generating context must never hold the answer key.** A model that can see `expected.sql`
  is grading itself, and every assertion over the scores would still pass. The isolation is
  structural — the generator is handed a question and nothing else — so the tests below assert
  over what the generator was GIVEN rather than over what it returned.
* **The generating context must never hold a result row.** Generation strictly precedes execution,
  so there is no row in existence to leak at the moment the generator is called; these tests pin
  that ordering by looking for the rows afterwards.

Everything here stubs the generator, so nothing spends a token or needs a client installed. The
fixtures are synthetic throughout — an `acme` profile over `orders`, fabricated ids and totals.
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
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import execute_sql  # noqa: E402
import guardrail  # noqa: E402
from semantic_model import golden_run as gr  # noqa: E402
from semantic_model.golden import GoldenDataset, GoldenItem  # noqa: E402

PROFILE = "acme"
ORG = "demo"
DATASOURCE = "acme-sqlite"
DIALECT = "sqlite"

QUESTION = "How many orders have been placed?"
GOLDEN_SQL = "SELECT COUNT(*) AS order_count FROM orders"
GENERATED_SQL = "SELECT COUNT(order_id) AS n FROM orders"
# The one value that stands for "a row the run produced". Distinctive so a substring search over
# everything the generator was handed is a real check rather than a coincidence.
ROW_VALUE = 40041


def _item(item_id: str = "orders-count", **kw) -> GoldenItem:
    """One golden case. Confirmed and `exact` unless a test says otherwise."""
    expected = {"sql": GOLDEN_SQL, "sql_confirmed": True, **kw.pop("expected", {})}
    return GoldenItem.model_validate({"id": item_id, "query": QUESTION, "expected": expected, **kw})


def _dataset(*items: GoldenItem) -> GoldenDataset:
    return GoldenDataset(name="orders", test_cases=list(items or (_item(),)))


class _StubGenerator:
    """Answers with a fixed statement, and records every argument it was handed.

    The recording half is the adversarial instrument: the egress criteria are claims about what the
    generator did NOT receive, so the test has to hold on to everything it did.
    """

    def __init__(self, sql: str = GENERATED_SQL, error: str | None = None) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self._sql, self._error = sql, error

    def generate(self, question: str, org: str, datasource: str | None) -> gr.GeneratedSql:
        self.calls.append((question, org, datasource))
        return gr.GeneratedSql(sql="" if self._error else self._sql, error=self._error)


class _SpyExecutor:
    """Records every statement that reached the connect-and-run step, and what it answered with."""

    def __init__(self, rows_by_sql: dict[str, list[tuple]] | None = None) -> None:
        self.calls: list[tuple[str, dict, str]] = []
        self._rows = rows_by_sql or {}

    def execute(self, vetted_sql: str, creds: dict, *, profile: str) -> execute_sql.ExecResult:
        self.calls.append((vetted_sql, creds, profile))
        rows = self._rows.get(vetted_sql, [(ROW_VALUE,)])
        columns = [f"c{index}" for index in range(len(rows[0]))]
        return execute_sql.ExecResult(columns=columns, rows=rows, truncated=False)


@pytest.fixture()
def chokepoint(monkeypatch):
    """Let `execute_guarded` reach the injected executor without a model or a warehouse.

    The gates themselves are pinned by their own batteries; what these tests need from the
    chokepoint is that both statements go THROUGH it, which the spy records.
    """
    monkeypatch.setattr(
        execute_sql, "_load_credentials", lambda p, org_id="local": {"type": "sqlite", "path": ":memory:"}
    )
    monkeypatch.setattr(execute_sql, "_model_safety", lambda s, p, a: (s, None))


def _run(dataset, generator, executor, **kw) -> gr.GoldenRunResult:
    return gr.run_golden_dataset(
        dataset,
        profile=PROFILE,
        generator=generator,
        executor=executor,
        org=ORG,
        datasource=DATASOURCE,
        dialect=DIALECT,
        **kw,
    )


# --- the executor seam ------------------------------------------------------------------------


def test_both_statements_execute_through_the_injected_executor(chokepoint):
    """Criterion 4. Two statements per item, both through the chokepoint, and no other path.

    The spy is the only way out of this process, so a count of exactly two per item is also the
    assertion that nothing ran a statement around `execute_guarded`.
    """
    spy = _SpyExecutor()
    result = _run(_dataset(_item()), _StubGenerator(), spy)

    assert [call[0] for call in spy.calls] == [GOLDEN_SQL, GENERATED_SQL]
    assert [call[2] for call in spy.calls] == [PROFILE, PROFILE]
    assert result.completed and len(result.outcomes) == 1


def test_a_refused_golden_statement_is_unscored_and_the_run_finishes(chokepoint, monkeypatch):
    """Criterion 5. The answer key itself being refused says nothing about the generated statement,
    so the item is unscored and carries the gate's own sentence — and the next item still runs."""
    refusal = guardrail.refuse(
        guardrail.RULE_TABLE_SCOPE, detail="out of scope", remediation="Ask about a declared table."
    )
    monkeypatch.setattr(
        execute_sql,
        "_model_safety",
        lambda s, p, a: (s, refusal if s == GOLDEN_SQL else None),
    )
    spy = _SpyExecutor()

    result = _run(_dataset(_item("a"), _item("b")), _StubGenerator(), spy)

    assert [o.item_key for o in result.outcomes] == ["a", "b"]
    assert [o.score.status for o in result.outcomes] == ["unscored", "unscored"]
    assert result.outcomes[0].score.reason == refusal.detail
    assert result.completed and result.unscored == 2
    # The generated statement is never run once the answer key could not be.
    assert spy.calls == []


def test_a_refused_generated_statement_fails_rather_than_excusing_the_item(chokepoint, monkeypatch):
    """A statement the guard will not run is a wrong answer, not an unjudgeable one.

    Scoring it `unscored` would mean a generator that emits something out of scope could never
    fail a run, which is the one outcome an eval must not have.
    """
    refusal = guardrail.refuse(
        guardrail.RULE_TABLE_SCOPE, detail="out of scope", remediation="Ask about a declared table."
    )
    monkeypatch.setattr(
        execute_sql,
        "_model_safety",
        lambda s, p, a: (s, refusal if s == GENERATED_SQL else None),
    )

    result = _run(_dataset(_item()), _StubGenerator(), _SpyExecutor())

    outcome = result.outcomes[0]
    assert outcome.score.status == "scored" and outcome.score.accuracy == 0.0
    assert outcome.score.reason == refusal.detail
    assert result.failed == 1 and result.gating_failures == 1


def test_an_item_that_cannot_be_generated_errors_and_the_rest_still_run(chokepoint):
    """Criterion 6. A generation failure is this item's error and not the run's."""
    sentence = "the generator did not answer"

    class _FailsFirst:
        def __init__(self) -> None:
            self.calls = 0

        def generate(self, question, org, datasource):
            self.calls += 1
            if self.calls == 1:
                return gr.GeneratedSql(sql="", error=sentence)
            return gr.GeneratedSql(sql=GENERATED_SQL, error=None)

    result = _run(_dataset(_item("a"), _item("b")), _FailsFirst(), _SpyExecutor())

    assert result.outcomes[0].score.status == "error"
    assert result.outcomes[0].score.reason == sentence
    assert result.outcomes[0].generated_sql == ""
    assert result.outcomes[1].passed
    assert result.completed and result.errored == 1 and result.passed == 1


def test_an_unconfirmed_item_never_fails_the_run(chokepoint):
    """Criterion 7. An unconfirmed item reports its score and is not one the run may gate on."""
    item = _item("draft", expected={"sql_confirmed": False})
    spy = _SpyExecutor({GENERATED_SQL: [(7,)]})

    result = _run(_dataset(item), _StubGenerator(), spy)

    assert result.outcomes[0].score.status == "scored"
    assert result.outcomes[0].score.accuracy == 0.0
    assert result.failed == 1  # the score is reported in full
    assert result.gating_failures == 0  # and the run may not fail on it


def test_the_run_summary_counts_every_outcome(chokepoint, monkeypatch):
    """Criterion 15. Every outcome lands in exactly one bucket, and the totals are arithmetic over
    the items rather than anything a model was asked."""
    refusal = guardrail.refuse(
        guardrail.RULE_TABLE_SCOPE, detail="out of scope", remediation="Ask about a declared table."
    )
    unscorable = "SELECT 1 FROM refused_table"
    monkeypatch.setattr(
        execute_sql,
        "_model_safety",
        lambda s, p, a: (s, refusal if s == unscorable else None),
    )
    agrees = "SELECT COUNT(1) AS n FROM orders"
    disagrees = "SELECT COUNT(DISTINCT customer_id) AS n FROM orders"
    answers = {"q-pass": agrees, "q-fail": disagrees, "q-unscored": agrees}

    class _PerQuestion:
        def generate(self, question, org, datasource):
            if question not in answers:
                return gr.GeneratedSql(sql="", error="the generator did not answer")
            return gr.GeneratedSql(sql=answers[question], error=None)

    items = [
        _item("passes", query="q-pass"),
        _item("fails", query="q-fail"),
        _item("unscored", query="q-unscored", expected={"sql": unscorable}),
        _item("errors", query="q-none"),
    ]
    spy = _SpyExecutor({disagrees: [(ROW_VALUE + 1,)]})

    result = _run(_dataset(*items), _PerQuestion(), spy)

    assert (result.passed, result.failed, result.unscored, result.errored) == (1, 1, 1, 1)
    assert result.passed + result.failed + result.unscored + result.errored == len(result.outcomes)
    # And the whole run survives the trip AH-110/AH-104 take it on.
    assert json.loads(json.dumps(result.as_dict()))["summary"]["passed"] == 1


def test_the_run_carries_the_readers_findings_rather_than_swallowing_them(chokepoint):
    """A dataset that was read with faults is still run; the faults ride on the result."""
    from semantic_model.validator import ValidationResult

    res = ValidationResult()
    res.error("golden_invalid_case", "orders.yaml[broken]: expected.sql_confirmed: field required")

    result = _run(_dataset(_item()), _StubGenerator(), _SpyExecutor(), findings=res.findings)

    assert [f["code"] for f in result.findings] == ["golden_invalid_case"]
    assert json.dumps(result.as_dict())  # still JSON, findings included


def test_a_generator_that_breaks_stops_the_run_rather_than_scoring_the_rest(chokepoint):
    """A generator that RAISES is not answering, so the items after it would be scored against
    something that is not working. The run stops, keeps what it has, and says it did not finish."""

    class _Breaks:
        def generate(self, question, org, datasource):
            raise RuntimeError("the adapter is misconfigured")

    result = _run(_dataset(_item("a"), _item("b")), _Breaks(), _SpyExecutor())

    assert result.completed is False
    assert result.outcomes == ()


# --- the statement diff, and the two things it may decide ---------------------------------------


def test_a_required_filter_left_out_turns_a_would_be_pass_into_a_fail(chokepoint):
    """The rows agree and the statement still did not answer the question the dataset requires.

    The score stays what the comparison found — the two are separate fields precisely so a reader
    can see that the numbers matched and the statement was still wrong.
    """
    item = _item("scoped", must_filter=["region"])

    result = _run(_dataset(item), _StubGenerator(), _SpyExecutor())

    outcome = result.outcomes[0]
    assert outcome.score.accuracy == 1.0
    assert outcome.gated is True and outcome.passed is False
    assert [gate["kind"] for gate in outcome.claims["gates"]] == ["must_filter"]
    assert result.failed == 1 and result.gating_failures == 1


def test_a_scored_item_carries_the_seven_claims(chokepoint):
    """A failing item's whole value is the sentence after 'the rows disagree', so the diff rides on
    every item that had two statements to read."""
    result = _run(_dataset(_item()), _StubGenerator(), _SpyExecutor())

    claims = result.outcomes[0].claims
    assert [claim["name"] for claim in claims["claims"]] == [
        "tables", "filter_predicates", "date_window", "group_keys", "join_keys", "ordering", "limit",
    ]
    assert claims["gated"] is False and result.outcomes[0].passed


def test_a_case_with_no_answer_key_is_judged_on_its_own_only_where_the_level_allows_it(chokepoint):
    """`golden.py` lets an unconfirmed case ship with no answer key. A level that judges the result
    on its own terms runs it; a level that compares against rows has nothing to compare to."""
    graded = _item("has-rows", match="nonempty", expected={"sql": None, "sql_confirmed": False})
    keyed = _item("needs-a-key", expected={"sql": None, "sql_confirmed": False})
    spy = _SpyExecutor()

    result = _run(_dataset(graded, keyed), _StubGenerator(), spy)

    assert result.outcomes[0].passed and result.outcomes[0].claims is None
    assert result.outcomes[1].score.status == "unscored"
    # One statement ran in total: neither case has an answer key, and the second never got that far.
    assert [call[0] for call in spy.calls] == [GENERATED_SQL]


def test_the_pass_mark_is_the_comparators_alone(chokepoint):
    """An item passes at exactly 1.0. A near miss is a fail here, with no second threshold applied
    on top of the match level the author already chose."""
    # Both columns carry the same values on both sides, so both pair; one of the three rows lines
    # up. That is a two-thirds-wrong answer, and it is a fail.
    spy = _SpyExecutor(
        {
            GOLDEN_SQL: [(1, "a"), (2, "b"), (3, "c")],
            GENERATED_SQL: [(1, "a"), (2, "c"), (3, "b")],
        }
    )

    result = _run(_dataset(_item(match="values")), _StubGenerator(), spy)

    assert result.outcomes[0].score.accuracy == pytest.approx(1 / 3)
    assert result.outcomes[0].passed is False and result.failed == 1
