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
this docstring: **every tool and every MCP server is switched off by an explicit flag** and the
child starts in an empty directory, so the schema inlined in its prompt is the only thing it can
read; its environment is an explicit **allowlist** rather than the inherited one (the parent's
environment carries the dataset's own root and the warehouse credentials); and its **stderr is
never surfaced** — a client can echo the prompt there, and a failed generation is reported as a
fixed, value-free sentence.

The flags are the load-bearing half of that first decision, and it is worth saying why here too:
in this client the ABSENCE of a tool flag means every built-in tool, not none of them. A child left
at its defaults could read files, glob and run commands, and `HOME` is on the allowlist — which is
the whole of the route to the artifacts pointer, the dataset and the answer key inside it.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
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
_NO_ANSWER_KEY = "the item has no answer key, and its match level judges the result against one"

# How much of an injected generator's own error sentence is persisted with the run. A generator is
# somebody else's code, and a raised exception's text is dropped here for exactly that reason; a
# RETURNED error is no more trustworthy, so it is bounded rather than relayed whole. The shipped
# generator's sentences are all far shorter than this, so the cut only ever falls on text the
# runner did not write.
_MAX_RELAYED_ERROR = 200
_ERROR_TRUNCATED = "…"

# What a run says when its generator raised. `completed=False` is the fact and not the reason, and
# a forty-item run that broke on the first item is otherwise indistinguishable from a run over an
# empty dataset. The exception's TYPE is relayed and its message is not: a message quotes whatever
# broke — a host, a key, a row — and this module's whole posture is that such text does not travel.
_GENERATOR_RAISED_CODE = "golden_generator_failed"
_GENERATOR_RAISED = (
    "the generator raised {exception} on this item, so the run stopped rather than scoring the "
    "cases after it"
)


@dataclass(frozen=True)
class GeneratedSql:
    """What a generator answered with: a statement, or the reason there is not one.

    A generator SHOULD leave `sql` empty exactly when it sets `error`, and its error should be a
    fixed sentence it chose — never a client's stderr, never the prompt, and never anything the
    model wrote. Neither is enforceable across the seam: `SqlGenerator` is a Protocol and the
    implementation is somebody else's, so the runner checks both halves rather than trusting them.
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

        This counts items that were SCORED, so it is not a verdict on its own and must not be read
        as one. A run in which every confirmed item's generation errored — a model writing prose
        where a statement was asked for — has zero gating failures and is not a green run. A caller
        deciding a verdict reads `completed`, `gating_failures` AND `errored`, all three.
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

    `org` is BOTH things it looks like, and that is why it is one parameter: the tenant the question
    is asked about, which the generator is told, and the tenant whose warehouse both statements are
    executed against, which the chokepoint resolves credentials for. Splitting them would let a run
    score one tenant's dataset against another's rows.

    `findings` are the reader's — what it dropped getting this dataset together. They are carried
    rather than swallowed: a run over a dataset that lost three cases to a typo is not the same run
    as one over a whole dataset, and nothing else downstream can tell the difference.

    **A verdict is not `gating_failures == 0`.** That counter is over items that were SCORED, so a
    run whose every generation came back unreadable reports zero of them. Whoever decides a run
    passed reads three values: `completed` (the loop got through the dataset), `gating_failures`
    (no confirmed item was scored a miss) and `errored` (no item failed to produce a statement).
    """
    run_id = uuid.uuid4().hex
    outcomes: list[ItemOutcome] = []
    run_findings = [asdict(finding) for finding in findings]
    completed = True
    for item in dataset.test_cases:
        try:
            generated = generator.generate(item.query, org, datasource)
        except Exception as exc:
            # The generator is the one injected seam in this loop that is allowed to raise, and an
            # adapter that raises is not answering. Its message is dropped rather than reported:
            # this is somebody else's exception text, and a run's result travels. The TYPE is not
            # the message, and a stopped run that says nothing about why is one nobody can act on,
            # so it goes where the run already carries what it could not do — the findings.
            completed = False
            run_findings.append(
                asdict(
                    Finding(
                        severity="error",
                        code=_GENERATOR_RAISED_CODE,
                        message=_GENERATOR_RAISED.format(exception=type(exc).__name__),
                        locator=item.item_key,
                    )
                )
            )
            break
        outcomes.append(
            _run_item(item, generated, profile=profile, org=org, executor=executor, dialect=dialect)
        )
    return GoldenRunResult(
        run_id=run_id,
        profile=profile,
        dataset=dataset.name,
        outcomes=tuple(outcomes),
        findings=tuple(run_findings),
        completed=completed,
    )


def _run_item(
    item: GoldenItem,
    generated: GeneratedSql,
    *,
    profile: str,
    org: str,
    executor: "Executor",
    dialect: str,
) -> ItemOutcome:
    """Score one case: execute, compare the results, then compare the statements."""
    confirmed = item.expected.sql_confirmed
    if generated.error is not None or not generated.sql.strip():
        return ItemOutcome(
            item_key=item.item_key,
            score=ItemScore(status="error", accuracy=None, reason=_relayed_error(generated.error)),
            generated_sql="",
            claims=None,
            gated=False,
            confirmed=confirmed,
        )

    golden_sql = item.expected.sql or ""
    score = _score(
        item,
        generated.sql,
        golden_sql,
        profile=profile,
        org=org,
        executor=executor,
        dialect=dialect,
    )
    # After the score, and on EVERY item that produced a statement — an answer key is not the
    # condition. The diff is what turns "the rows disagree" into a reason, and one of its two gates
    # reads the generated statement alone: `must_filter` is the DATASET's requirement rather than a
    # property of the golden statement, so a keyless case that declares one still has it to meet.
    # Skipping the diff for want of an answer key is how such a case passed on its band while
    # filtering nothing. An empty golden side reads as unreadable, which makes every claim
    # `unknown` — the shape that says nothing was compared, rather than that the two agreed.
    diff = compare_statements(
        generated.sql, golden_sql, must_filter=item.must_filter, dialect=dialect
    )
    return ItemOutcome(
        item_key=item.item_key,
        score=score,
        generated_sql=generated.sql,
        claims=diff.as_dict(),
        gated=diff.gated,
        confirmed=confirmed,
    )


def _relayed_error(error: Optional[str]) -> str:
    """The generator's own reason, bounded, or the runner's sentence when there is not one.

    A generator that returned nothing and set no error still has to say something to the reader, and
    a generator that did set one wrote it itself — this reason is persisted and rendered, so it is
    cut to a length rather than passed through at whatever size the adapter felt like.
    """
    if not error:
        return _NOTHING_GENERATED
    if len(error) <= _MAX_RELAYED_ERROR:
        return error
    # The marker is part of the bound, not added past it. `_MAX_RELAYED_ERROR` is what a reader of
    # this field may assume about its length, and a cut that overshoots the number it is named for
    # is the one length nobody downstream planned for.
    return error[: _MAX_RELAYED_ERROR - len(_ERROR_TRUNCATED)] + _ERROR_TRUNCATED


def _score(
    item: GoldenItem,
    generated_sql: str,
    golden_sql: str,
    *,
    profile: str,
    org: str,
    executor: "Executor",
    dialect: str,
) -> ItemScore:
    """Run both statements through the chokepoint and score what came back.

    `org` reaches the chokepoint because that is where a tenant's warehouse is chosen. The org-less
    credential names are offered to the single-tenant `local` org and to no other, so a named tenant
    arriving here without its own id would resolve the shared warehouse instead of failing closed —
    and the run would score that tenant's dataset against somebody else's rows.

    The MATCH LEVEL is read before either statement runs, because it decides whether there are two.
    A level that judges the generated result on its own terms never looks at the answer key, so an
    item written at one is not charged for running a statement whose rows would be discarded — and,
    more than a wasted query, is not left unscored because the guard refused a statement the level
    never needed.

    Otherwise the answer key goes first. If it cannot be run there is nothing to judge the generated
    statement against, so the generated statement is not run either — executing it would spend a
    warehouse query on a comparison that cannot happen.
    """
    if item.match in _SELF_JUDGING_LEVELS:
        golden_result = ExecResult(columns=[], rows=[])
    elif golden_sql:
        envelope = execute_guarded(golden_sql, profile, None, executor=executor, org_id=org)
        if envelope.status != "ok":
            return _not_ok(envelope, answer_key=True)
        golden_result = envelope.data
    else:
        return ItemScore(status="unscored", accuracy=None, reason=_NO_ANSWER_KEY)

    envelope = execute_guarded(generated_sql, profile, None, executor=executor, org_id=org)
    if envelope.status != "ok":
        return _not_ok(envelope, answer_key=False)
    return compare_result_sets(
        golden_result,
        envelope.data,
        match=item.match,
        # Withheld at a self-judging level, which never ran the answer key: reading its ORDER BY
        # would set `order_sensitive` from a statement that had no part in the score, and a
        # diagnostic naming evidence nobody consulted is worse than one naming none.
        golden_sql=None if item.match in _SELF_JUDGING_LEVELS else (golden_sql or None),
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


# ---------------------------------------------------------------------------
# The shipped generator: the operator's own client, as a child process
# ---------------------------------------------------------------------------

# The child's argument list, in full, and a tuple so that nothing can conditionally append to it:
# every run of this generator gets the same four decisions, or none of them.
#
# Do not delete a flag here as noise. In this client the ABSENCE of a flag is not deny-all, it is
# the permissive default, and each of these three overrides one:
#
# * `--tools ""` — omitting it gives the child EVERY built-in tool, Read and Glob and Bash among
#   them. `""` is the documented way to spell "none of them".
# * `--strict-mcp-config` — the default loads the operator's user-scope, project-scope, `.mcp.json`
#   and plugin MCP servers. With this flag the child uses only the servers `--mcp-config` names,
#   and no `--mcp-config` is passed, so that is the empty set.
# * `--setting-sources ""` — the default loads the user, project and local setting sources, which
#   is how a `CLAUDE.md`, a `.claude/settings.json` or a `.mcp.json` would reach the child.
#
# All three matter because `HOME` is on the environment allowlist below, and every path this run
# withholds the name of is computed from it: the artifacts pointer, the artifacts directory it
# names, the dataset inside that carrying `expected.sql` and its recorded rows, and the warehouse
# credentials file beside it. A child with no tool to read a file has no use for `HOME`, which is
# what makes withholding the NAME of the path sufficient. `-p` is print mode — read a prompt from
# stdin, write an answer, exit. What the model needs to write a statement — the tables and
# columns — is inlined in the prompt instead, by whoever built this generator.
_CLIENT_ARGV = (
    "claude",
    "-p",
    "--tools",
    "",
    "--strict-mcp-config",
    "--setting-sources",
    "",
)

# The child's whole environment, by name. An ALLOWLIST and not a filter, and that is the decision:
# `subprocess.run` passes the parent's environment through by default, and the parent's carries the
# dataset's own root (`AGAMI_ARTIFACTS_DIR`) and the warehouse DSN (`DATASOURCE_URL…`). A filter is
# a list of things somebody has to remember to add to; an allowlist is one nobody can forget.
#
# `ANTHROPIC_API_KEY` is here because it is the one credential the child EXISTS to use — the
# operator's own key for their own client, in a process whose only job is to answer a question. It
# is not a datasource credential, and no datasource credential has a name that could reach this
# tuple.
#
# `HOME` is here because a client that cannot find its own configuration cannot start, and it is
# inert only because of the flags above: the artifacts pointer, the dataset and the credentials file
# are all under it, so this list withholds the NAME of a path the child has no tool to open. The two
# halves are one decision — never add a tool without revisiting this tuple.
_CHILD_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "ANTHROPIC_API_KEY")

# Why a generation produced no statement. Fixed sentences, and the fixedness is the point: a client
# can echo the whole prompt on stderr, and this text is rendered beside the item and persisted with
# the run. Nothing the child wrote — stdout, stderr, exit code, the command line — reaches any of
# them.
_GENERATION_UNAVAILABLE = "the generator command could not be started on this machine"
_GENERATION_TIMED_OUT = "the generator did not answer within the time this run allows"
_GENERATION_EXITED = "the generator exited without answering"
_GENERATION_UNREADABLE = "the generator's answer did not carry a statement this run could read"

_PROMPT = """\
Write one SQL statement that answers a question about a database.

Organization: {org}
Datasource: {datasource}

The tables and columns you may use:
{schema}

The question:
{question}

Reply with a single JSON object and no other text: {{"sql": "<one SELECT statement>"}}
Write one read-only SELECT over the tables above. You have no tools here and nothing you write is
executed by you, so do not try to run it, verify it, or read any data.
Answer exactly what was asked and nothing more: no extra columns, no extra grouping, no volunteered
breakdown. A metric's guidance on how to present a result applies to the question that asks for that
result, not to every question touching the same table. The answer is scored against one a person
already agreed to, so a richer statement is a different answer rather than a better one.
"""


def _generation_prompt(question: str, org: str, datasource: Optional[str], schema: str) -> str:
    """The whole of what the child is asked. Every value in it came from the question's own side of
    the run — never from `expected`, and never from a result set."""
    return _PROMPT.format(
        org=org, datasource=datasource or "(unnamed)", schema=schema, question=question
    )


def _first_json_object(text: str) -> Optional[dict[str, Any]]:
    """The first complete JSON object in `text`, or None when there is not one.

    Scanning for a balanced pair of braces rather than reaching for the whole string is what makes
    this fence-tolerant for free: a model that wrapped its answer in prose or a ```json fence has
    put the object somewhere inside, and an item lost to a code fence is a wrong number in the run
    rather than a wrong answer by the model. Strings are tracked so a brace inside a SQL literal
    does not close the object early.
    """
    depth, start, in_string, escaped = 0, -1, False, False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0:
                try:
                    value = json.loads(text[start : index + 1])
                except ValueError:
                    return None
                return value if isinstance(value, dict) else None
    return None


def _child_env() -> dict[str, str]:
    """The environment the child is given: the allowlist, and only the names that are actually set."""
    return {key: os.environ[key] for key in _CHILD_ENV_KEYS if key in os.environ}


class ClaudeCliGenerator:
    """Answer a question by spawning the operator's own client, once, with every tool switched off.

    `schema` is the tables and columns the model may write against, already rendered by whoever
    built this generator — inlining it is what makes the no-tools invocation above sufficient.
    `timeout_s` is `subprocess.run`'s own bound, so a client that hangs is killed rather than waited
    on. Execution has no timer of its own here: its bound is the deployment's, at the chokepoint.

    That bound is THIS generator's and not the runner's, and the difference matters to whoever
    injects another one. `SqlGenerator.generate` has no parameter that could carry a timeout — the
    argument list is the isolation, so widening it is a contract change — which means an injected
    generator that hangs hangs the run, with nothing above it to cut the call off.
    """

    def __init__(self, schema: str, *, timeout_s: float) -> None:
        self.schema = schema
        self.timeout_s = timeout_s

    def generate(self, question: str, org: str, datasource: Optional[str]) -> GeneratedSql:
        """One question in, one statement out — or a fixed sentence saying why there is not one."""
        prompt = _generation_prompt(question, org, datasource, self.schema)
        try:
            # A directory of its own, empty, thrown away afterwards. The child would otherwise start
            # in whatever directory the eval was launched from, and a `CLAUDE.md`,
            # `.claude/settings.json` or `.mcp.json` sitting there is project configuration the
            # client reads. `--setting-sources ""` already refuses to load those; starting somewhere
            # that has none of them means the two would have to fail together.
            with tempfile.TemporaryDirectory(prefix="agami-generation-") as workdir:
                # The prompt goes on STDIN rather than in the argument list: a schema is long, an
                # argument list is bounded, and a process list is readable by other users on most
                # systems.
                completed = subprocess.run(
                    list(_CLIENT_ARGV),
                    input=prompt,
                    stdout=subprocess.PIPE,
                    # Discarded by the OS rather than captured and then not read. A client can
                    # echo the whole prompt on stderr, and the prompt carries the model's
                    # vocabulary; the fixed sentences this module relays are written here, never
                    # taken from the child. Holding it in memory to ignore it is a copy of
                    # something with no reader and one way to leak.
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=self.timeout_s,
                    env=_child_env(),
                    cwd=workdir,
                    check=False,
                )
        except subprocess.TimeoutExpired:
            # `TimeoutExpired` carries the command and whatever output was captured before the kill.
            # None of it is read.
            return GeneratedSql(sql="", error=_GENERATION_TIMED_OUT)
        except OSError:
            # No client installed, or nothing executable at that name. `OSError.__str__`
            # interpolates the path it tried, which is why the exception is not relayed.
            return GeneratedSql(sql="", error=_GENERATION_UNAVAILABLE)
        if completed.returncode != 0:
            return GeneratedSql(sql="", error=_GENERATION_EXITED)
        answer = _first_json_object(completed.stdout)
        sql = answer.get("sql") if answer else None
        if not isinstance(sql, str) or not sql.strip():
            return GeneratedSql(sql="", error=_GENERATION_UNREADABLE)
        return GeneratedSql(sql=sql.strip(), error=None)


__all__ = [
    "ClaudeCliGenerator",
    "GeneratedSql",
    "GoldenRunResult",
    "ItemOutcome",
    "SqlGenerator",
    "run_golden_dataset",
]
