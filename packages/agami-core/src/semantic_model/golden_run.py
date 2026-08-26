"""Run a golden dataset: ask for a statement, run both sides, score every item.

A golden dataset is a file of questions whose answers are already agreed. `golden.py` reads it,
`comparator.py` scores one result set against another and `golden_claims.py` says where two
statements disagree. This module is the loop that puts the three together: for each case it asks
the product to answer the question, executes the answer key and the generated statement, scores
them, and folds the statement diff's two gates into the verdict.

Three properties decide whether the resulting numbers mean anything, and none of them is visible
in a score:

* **The generating context never holds the answer key.** A model that can see `expected.sql` is
  grading itself against a key it can read, and every assertion over the scores would still pass.
  So `SqlGenerator.generate` takes a question and nothing else — the isolation is the SHAPE of the
  seam rather than a rule a caller has to keep. `run_golden_dataset` reads `expected.sql` only
  after the generator has already answered.
* **The generating context never holds a result row.** Generation strictly precedes execution, so
  at the moment the generator is called there is no row in existence to leak.
* **The run is deterministic.** Every verdict is arithmetic over two result sets and two parsed
  statements. Nothing here asks a model to judge anything, and the pass mark is `comparator`'s
  alone — exactly `accuracy == 1.0`, with no second threshold applied on top of the match level
  the author already chose.

**Nothing in the loop raises.** `execute_guarded` is total, `compare_result_sets` never raises and
`compare_statements` never raises, so one bad case costs that case rather than the run. The one
exception is the injected generator, which is somebody else's code: a generator that RAISES is not
answering, so the run stops there and reports that it did not finish, rather than scoring the
remaining items against something that is not working.

The one shipped generator, `ClaudeCliGenerator`, spawns the operator's own client as a child
process. Three decisions about that child are load-bearing and are pinned by tests rather than by
this docstring: it is given **no tools and no MCP configuration** (the schema is inlined in the
prompt instead, so there is no path from the generating model to the warehouse to police), its
environment is an explicit **allowlist** rather than the inherited one (the parent's environment
carries the dataset's own root and the warehouse credentials), and its **stderr is never
surfaced** — a client can echo the prompt there, and a failed generation is reported as a fixed,
value-free sentence.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Optional, Protocol

from execute_sql import ExecResult, execute_guarded
from guardrail import Envelope

from .comparator import ItemScore, compare_result_sets
from .golden import GoldenDataset, GoldenItem
from .golden_claims import compare_statements
from .validator import Finding

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ports import Executor

# The two match levels that judge a generated result on its own terms. Every other level compares
# it against the rows the answer key produced, so an item written without one has nothing to be
# compared to — `golden.py` deliberately allows that shape for an unconfirmed, in-progress case,
# and this is what a run does when it reaches one.
_SELF_JUDGING_LEVELS = ("bounded", "nonempty")

# Why an item could not be judged. Sentences rather than codes, because they are printed beside the
# item, and value-free because a run's result is persisted and rendered further on.
_NOTHING_GENERATED = "the generator returned no statement for this question"
_NO_ANSWER_KEY = (
    "the item has no answer key, and its match level judges the result against one"
)


@dataclass(frozen=True)
class GeneratedSql:
    """What a generator answered with: a statement, or the reason there is not one.

    `sql` is empty exactly when `error` is set. The error is a fixed sentence chosen by the
    generator — never a client's stderr, never the prompt, and never anything the model wrote.
    """

    sql: str
    error: Optional[str]


class SqlGenerator(Protocol):
    """Turn one question into one statement.

    The argument list is the whole of the isolation: a question, the organization it is asked
    about, and the datasource it is asked against. An implementation cannot be handed the answer
    key or a result row because there is no parameter that could carry one.
    """

    def generate(self, question: str, org: str, datasource: Optional[str]) -> GeneratedSql: ...


@dataclass(frozen=True)
class ItemOutcome:
    """One case's verdict, and the evidence a reader needs to act on it.

    `score` is `comparator`'s own value, not a second one: this module decides which two result
    sets are compared and what an item that never got that far is worth, and nothing else.

    `gated` and `score` are deliberately separate fields. A statement can produce exactly the right
    rows while leaving out a filter the dataset requires, and collapsing the two would hide which
    of the two things went wrong; `passed` is where they are folded together.
    """

    item_key: str
    score: ItemScore
    generated_sql: str
    claims: Optional[dict[str, Any]]
    gated: bool
    confirmed: bool

    @property
    def passed(self) -> bool:
        """Exactly `comparator`'s pass mark, minus whatever the statement diff gated on.

        `accuracy == 1.0` is the whole test — an accuracy of None means nothing was scored, so an
        unscored or errored item answers False here without a status check.
        """
        return self.score.accuracy == 1.0 and not self.gated

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_key": self.item_key,
            "confirmed": self.confirmed,
            "passed": self.passed,
            "gated": self.gated,
            "generated_sql": self.generated_sql,
            "score": asdict(self.score),
            "claims": self.claims,
        }


@dataclass(frozen=True)
class GoldenRunResult:
    """One dataset's run: what every case scored, and whether the run got through them.

    `completed` is not derivable from the counts. A run that finished with no failure and a run
    that stopped after two of forty cases both have zero failures, and only the first of them is a
    green run — so the distinction is carried rather than inferred.
    """

    run_id: str
    profile: str
    dataset: str
    outcomes: tuple[ItemOutcome, ...]
    findings: tuple[dict[str, Any], ...]
    completed: bool

    @property
    def passed(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.passed)

    @property
    def failed(self) -> int:
        """Every case that was compared and did not pass, confirmed or not — an unconfirmed case's
        score is still reported in full; see `gating_failures` for the ones a run may fail on."""
        return sum(
            1
            for outcome in self.outcomes
            if outcome.score.status == "scored" and not outcome.passed
        )

    @property
    def unscored(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.score.status == "unscored")

    @property
    def errored(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.score.status == "error")

    @property
    def gating_failures(self) -> int:
        """The failures a run's verdict is allowed to rest on: the confirmed ones.

        A case whose answer key nobody has confirmed cannot fail a run — it would be gating on an
        answer that is itself unreviewed — so it reports its score and stops there.
        """
        return sum(
            1
            for outcome in self.outcomes
            if outcome.confirmed and outcome.score.status == "scored" and not outcome.passed
        )

    def as_dict(self) -> dict[str, Any]:
        """The whole run as JSON-able values — this is what a caller persists and renders."""
        return {
            "run_id": self.run_id,
            "profile": self.profile,
            "dataset": self.dataset,
            "completed": self.completed,
            "summary": {
                "total": len(self.outcomes),
                "passed": self.passed,
                "failed": self.failed,
                "unscored": self.unscored,
                "errored": self.errored,
                "gating_failures": self.gating_failures,
            },
            "outcomes": [outcome.as_dict() for outcome in self.outcomes],
            "findings": list(self.findings),
        }


def run_golden_dataset(
    dataset: GoldenDataset,
    *,
    profile: str,
    generator: SqlGenerator,
    executor: "Executor",
    org: str,
    datasource: Optional[str] = None,
    dialect: str,
    findings: Sequence[Finding] = (),
) -> GoldenRunResult:
    """Run every case in `dataset`, in order, and hand back the scored run.

    `findings` are the reader's — what it dropped getting this dataset together. They are carried
    rather than swallowed: a run over a dataset that lost three cases to a typo is not the same run
    as one over a whole dataset, and nothing else downstream can tell the difference.
    """
    run_id = uuid.uuid4().hex
    outcomes: list[ItemOutcome] = []
    completed = True
    for item in dataset.test_cases:
        try:
            generated = generator.generate(item.query, org, datasource)
        except Exception:
            # The generator is the one injected seam in this loop that is allowed to raise, and an
            # adapter that raises is not answering. Its message is dropped rather than reported:
            # this is somebody else's exception text, and a run's result travels.
            completed = False
            break
        outcomes.append(
            _run_item(item, generated, profile=profile, executor=executor, dialect=dialect)
        )
    return GoldenRunResult(
        run_id=run_id,
        profile=profile,
        dataset=dataset.name,
        outcomes=tuple(outcomes),
        findings=tuple(asdict(finding) for finding in findings),
        completed=completed,
    )


def _run_item(
    item: GoldenItem,
    generated: GeneratedSql,
    *,
    profile: str,
    executor: "Executor",
    dialect: str,
) -> ItemOutcome:
    """Score one case: execute, compare the results, then compare the statements."""
    confirmed = item.expected.sql_confirmed
    if generated.error is not None or not generated.sql.strip():
        return ItemOutcome(
            item_key=item.item_key,
            score=ItemScore(
                status="error", accuracy=None, reason=generated.error or _NOTHING_GENERATED
            ),
            generated_sql="",
            claims=None,
            gated=False,
            confirmed=confirmed,
        )

    golden_sql = item.expected.sql or ""
    score = _score(item, generated.sql, golden_sql, profile=profile, executor=executor,
                   dialect=dialect)
    # After the score, and only when there are two statements to read. The diff is what turns "the
    # rows disagree" into a reason, so it is worth having on a failing item as much as on a passing
    # one; what it decides is narrower than what it describes — two gates out of seven claims.
    diff = (
        compare_statements(
            generated.sql, golden_sql, must_filter=item.must_filter, dialect=dialect
        )
        if golden_sql
        else None
    )
    return ItemOutcome(
        item_key=item.item_key,
        score=score,
        generated_sql=generated.sql,
        claims=diff.as_dict() if diff is not None else None,
        gated=diff.gated if diff is not None else False,
        confirmed=confirmed,
    )


def _score(
    item: GoldenItem,
    generated_sql: str,
    golden_sql: str,
    *,
    profile: str,
    executor: "Executor",
    dialect: str,
) -> ItemScore:
    """Run both statements through the chokepoint and score what came back.

    The answer key goes first. If it cannot be run there is nothing to judge the generated
    statement against, so the generated statement is not run either — executing it would spend a
    warehouse query on a comparison that cannot happen.
    """
    if golden_sql:
        envelope = execute_guarded(golden_sql, profile, None, executor=executor)
        if envelope.status != "ok":
            return _not_ok(envelope, answer_key=True)
        golden_result = envelope.data
    elif item.match in _SELF_JUDGING_LEVELS:
        golden_result = ExecResult(columns=[], rows=[])
    else:
        return ItemScore(status="unscored", accuracy=None, reason=_NO_ANSWER_KEY)

    envelope = execute_guarded(generated_sql, profile, None, executor=executor)
    if envelope.status != "ok":
        return _not_ok(envelope, answer_key=False)
    return compare_result_sets(
        golden_result,
        envelope.data,
        match=item.match,
        golden_sql=golden_sql or None,
        bounds=item.bounds,
        dialect=dialect,
    )


def _not_ok(envelope: Envelope, *, answer_key: bool) -> ItemScore:
    """What an item is worth when one of its two statements did not run.

    The two sides are read differently on a refusal, and the asymmetry is the point. The answer
    key being refused says nothing about the generated statement, so the item is UNSCORED. The
    generated statement being refused is a wrong answer — the model wrote something the guard will
    not run — and scoring that unscored would mean a generator emitting out-of-scope SQL could
    never fail a run.

    A failure is neither: the database rejected a statement or the connection broke, which is a
    fact about the run rather than about either statement. Both sentences are the envelope's own
    and both are value-free by contract, so they are relayed rather than re-authored.
    """
    if envelope.status == "refused":
        if answer_key:
            return ItemScore(status="unscored", accuracy=None, reason=envelope.refusal.detail)
        return ItemScore(status="scored", accuracy=0.0, reason=envelope.refusal.detail)
    return ItemScore(status="error", accuracy=None, reason=envelope.failure.message)


__all__ = [
    "GeneratedSql",
    "GoldenRunResult",
    "ItemOutcome",
    "SqlGenerator",
    "run_golden_dataset",
]
