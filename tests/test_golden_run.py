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
import os
import subprocess
import sys
from pathlib import Path
from typing import get_args

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
from semantic_model import comparator  # noqa: E402
from semantic_model import golden_run as gr  # noqa: E402
from semantic_model.golden import GoldenDataset, GoldenItem, MatchLevel  # noqa: E402

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


def test_both_statements_resolve_credentials_for_the_org_the_run_names(monkeypatch):
    """The org the run was asked about is the org whose warehouse both statements run against.

    The chokepoint resolves credentials per `(org, profile)`, and the run's `org` reached only the
    generator's prompt — so every item was scored against whatever the org-less default resolved
    to. The two notions were one word, and this pins them apart at the only place it shows.
    """
    resolved_for: list[str] = []

    def _credentials(profile, org_id="local"):
        resolved_for.append(org_id)
        return {"type": "sqlite", "path": ":memory:"}

    monkeypatch.setattr(execute_sql, "_load_credentials", _credentials)
    monkeypatch.setattr(execute_sql, "_model_safety", lambda s, p, a: (s, None))

    result = _run(_dataset(_item()), _StubGenerator(), _SpyExecutor())

    assert resolved_for == [ORG, ORG]  # the answer key and the generated statement, both
    assert result.completed and result.outcomes[0].passed


def test_a_named_org_is_not_scored_against_the_shared_warehouse(monkeypatch, tmp_path):
    """The concrete failure the org above prevents, at the credential channel rather than the seam.

    `DATASOURCE_URL` and `DATASOURCE_URL__<PROFILE>` are the single-tenant forms, and the
    chokepoint offers them to org `local` alone: a named tenant whose own variable is unset must
    fail closed rather than quietly score its dataset against whatever warehouse the host has.
    """
    monkeypatch.setattr(execute_sql, "_model_safety", lambda s, p, a: (s, None))
    monkeypatch.setattr(execute_sql, "CREDENTIALS_PATH", tmp_path / "no-credentials-file")
    monkeypatch.setenv("DATASOURCE_URL", "sqlite:///shared-warehouse.db")
    monkeypatch.setenv("DATASOURCE_URL__ACME", "sqlite:///shared-warehouse.db")
    for var in (f"{ORG.upper()}_DATASOURCE_URL", f"{ORG.upper()}_DATASOURCE_URL__ACME"):
        monkeypatch.delenv(var, raising=False)
    spy = _SpyExecutor()

    result = _run(_dataset(_item()), _StubGenerator(), spy)

    assert result.outcomes[0].score.status == "error"
    assert spy.calls == []  # nothing ran, which is the whole of failing closed


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
    # And the whole run survives the trip whatever persists and renders it takes it on.
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


def test_a_generator_that_breaks_says_why_in_the_runs_findings(chokepoint):
    """A stopped run is otherwise a run with no items and no reason, which reads as a dataset that
    was empty. The exception's TYPE is relayed and its message is not — the message is somebody
    else's text, and a run's result travels."""

    class _Breaks:
        def generate(self, question, org, datasource):
            raise RuntimeError("cannot reach warehouse-marker.example with the key it was given")

    result = _run(_dataset(_item("a"), _item("b")), _Breaks(), _SpyExecutor())

    assert result.completed is False
    assert [finding["code"] for finding in result.findings] == ["golden_generator_failed"]
    assert "RuntimeError" in result.findings[0]["message"]
    assert "warehouse-marker.example" not in json.dumps(result.as_dict())


def test_a_run_that_stops_partway_keeps_the_items_it_already_scored(chokepoint):
    """A run that stopped after two of forty is the case `completed` exists for, and the two are
    worth as much as they were before the third item broke — so they are kept rather than
    discarded with the run."""

    class _BreaksOnTheThird:
        def __init__(self) -> None:
            self.calls = 0

        def generate(self, question, org, datasource):
            self.calls += 1
            if self.calls == 3:
                raise RuntimeError("the adapter is misconfigured")
            return gr.GeneratedSql(sql=GENERATED_SQL, error=None)

    dataset = _dataset(_item("a"), _item("b"), _item("c"), _item("d"))

    result = _run(dataset, _BreaksOnTheThird(), _SpyExecutor())

    assert result.completed is False
    assert [outcome.item_key for outcome in result.outcomes] == ["a", "b"]
    assert result.passed == 2


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

    assert result.outcomes[0].passed
    # There is no second statement to compare against, so every claim reads `unknown` — which is
    # the shape that says "nothing was compared" rather than "the two agreed".
    claims = result.outcomes[0].claims
    assert {claim["status"] for claim in claims["claims"]} == {"unknown"}
    assert claims["gated"] is False
    assert result.outcomes[1].score.status == "unscored"
    # One statement ran in total: neither case has an answer key, and the second never got that far.
    assert [call[0] for call in spy.calls] == [GENERATED_SQL]


def test_a_required_filter_is_checked_on_an_item_that_has_no_answer_key(chokepoint):
    """`must_filter` is the DATASET's requirement, not a property of the golden statement.

    A keyless case is exactly the shape that used to declare one and never be checked for it: the
    diff was guarded on there being two statements, so the item passed on its band alone while the
    column it was required to constrain went unfiltered.
    """
    item = _item(
        "banded-scoped",
        match="bounded",
        bounds={"min_rows": 1, "max_rows": 10},
        must_filter=["region"],
        expected={"sql": None, "sql_confirmed": False},
    )

    result = _run(_dataset(item), _StubGenerator(), _SpyExecutor())

    outcome = result.outcomes[0]
    assert outcome.score.accuracy == 1.0  # the band the author wrote is satisfied
    assert outcome.gated is True and outcome.passed is False
    assert [gate["kind"] for gate in outcome.claims["gates"]] == ["must_filter"]
    assert [gate["column"] for gate in outcome.claims["gates"]] == ["region"]


def test_a_self_judging_level_does_not_run_the_answer_key_it_happens_to_have(chokepoint):
    """`bounded` judges the generated result against a band and never reads the golden rows, so an
    item that also carries an answer key must not spend a warehouse query producing rows nobody
    looks at."""
    item = _item("banded", match="bounded", bounds={"min_rows": 1})
    spy = _SpyExecutor()

    result = _run(_dataset(item), _StubGenerator(), spy)

    assert [call[0] for call in spy.calls] == [GENERATED_SQL]
    assert result.outcomes[0].passed


def test_a_refused_answer_key_cannot_unscore_a_level_that_never_needed_it(chokepoint, monkeypatch):
    """The reason the ordering above is a correctness fix and not a saved query.

    Running the answer key first meant a guard refusal of it returned `unscored` — so a `bounded`
    item inherited a refusal from a statement its match level does not consult, and an eval lost a
    case it could have scored.
    """
    refusal = guardrail.refuse(
        guardrail.RULE_TABLE_SCOPE, detail="out of scope", remediation="Ask about a declared table."
    )
    monkeypatch.setattr(
        execute_sql,
        "_model_safety",
        lambda s, p, a: (s, refusal if s == GOLDEN_SQL else None),
    )
    item = _item("banded", match="bounded", bounds={"min_rows": 1})

    result = _run(_dataset(item), _StubGenerator(), _SpyExecutor())

    assert result.outcomes[0].score.status == "scored"
    assert result.outcomes[0].passed


def test_every_match_level_is_either_self_judging_or_keyed(chokepoint):
    """The runner's list of self-judging levels is the complement of the comparator's keyed ones,
    and nothing but this holds the two together. A sixth level would land in neither, and `_score`
    would quietly report 'no answer key' for a level that judges the result on its own terms."""
    assert set(gr._SELF_JUDGING_LEVELS) | set(comparator._KEYED_LEVELS) == set(get_args(MatchLevel))
    assert not set(gr._SELF_JUDGING_LEVELS) & set(comparator._KEYED_LEVELS)


def test_a_generators_own_error_text_is_bounded_before_it_is_persisted(chokepoint):
    """A returned error is as much somebody else's text as a raised one, and a raised one is
    dropped. This reason is persisted with the run and rendered beside the item, so an adapter that
    hands back a whole log gets it cut to a length rather than relayed at whatever size it chose."""
    result = _run(_dataset(_item()), _StubGenerator(error="x" * 5_000), _SpyExecutor())

    reason = result.outcomes[0].score.reason
    assert len(reason) < 5_000
    assert reason.startswith("x") and reason.endswith("…")


# --- the shipped generator: what the child process is, and is not, given ------------------------

SCHEMA = "orders(id INTEGER, customer_id INTEGER, region TEXT, total NUMERIC)"
# A distinctive answer key and a distinctive row value, so a substring search over everything the
# child was handed is a real check rather than a coincidence.
ANSWER_KEY = "SELECT COUNT(*) AS orders_placed_marker FROM orders"
ANSWER = '{"sql": "SELECT COUNT(order_id) AS n FROM orders"}'


class _RecordedSpawn:
    """Stands in for the client process, keeping everything it was invoked with.

    The whole of criteria 1-3 is asserted against these recordings — the argv, the prompt on stdin
    and the environment ACTUALLY PASSED, never `os.environ`, which is exactly what the child would
    have inherited had the allowlist not been built.
    """

    def __init__(self, stdout: str = ANSWER, returncode: int = 0, stderr: str = "",
                 raises: BaseException | None = None) -> None:
        self.invocations: list[tuple[list[str], dict]] = []
        self.working_dirs: list[tuple[str | None, list[str] | None]] = []
        self._stdout, self._returncode, self._stderr = stdout, returncode, stderr
        self._raises = raises

    def __call__(self, args, **kwargs):
        self.invocations.append((list(args), kwargs))
        # The working directory is read HERE and not by the test: it is a temporary directory that
        # exists only for the duration of the call, so its contents are unknowable afterwards.
        cwd = kwargs.get("cwd")
        self.working_dirs.append((cwd, sorted(os.listdir(cwd)) if cwd else None))
        if self._raises is not None:
            raise self._raises
        return subprocess.CompletedProcess(args, self._returncode, self._stdout, self._stderr)

    def everything_given(self) -> str:
        """Every string the child could read: its arguments, its stdin, and its environment."""
        parts: list[str] = []
        for args, kwargs in self.invocations:
            parts += args
            parts.append(kwargs.get("input") or "")
            parts += [f"{key}={value}" for key, value in (kwargs.get("env") or {}).items()]
        return "\n".join(parts)


@pytest.fixture()
def spawn(monkeypatch):
    recorder = _RecordedSpawn()
    monkeypatch.setattr(gr.subprocess, "run", recorder)
    return recorder


def _cli_generator() -> gr.ClaudeCliGenerator:
    return gr.ClaudeCliGenerator(SCHEMA, timeout_s=30.0)


def _flag_value(args: list[str], flag: str) -> str | None:
    """The value that follows `flag` in an argument list, or None when the flag is not there."""
    return args[args.index(flag) + 1] if flag in args else None


def test_the_generator_is_invoked_with_every_tool_and_mcp_source_switched_off(spawn):
    """The tools, the MCP servers and the setting sources are closed by NAME, one flag each.

    This test used to assert the opposite shape — that the argument list was `claude -p` and
    carried no `--` argument at all. That is not a security property in this client: an omitted
    `--tools` means every built-in tool, an omitted `--strict-mcp-config` loads the operator's own
    MCP servers, and omitted `--setting-sources` reads the launch directory's project settings.
    Absence of a flag was the bug, so what is pinned here is the PRESENCE of the three that close
    each default, plus the empty working directory the child is started in.
    """
    generated = _cli_generator().generate(QUESTION, ORG, DATASOURCE)

    args, kwargs = spawn.invocations[0]
    assert args[:2] == ["claude", "-p"]
    assert _flag_value(args, "--tools") == ""  # "" is this client's spelling of "no tools"
    assert "--strict-mcp-config" in args  # and no --mcp-config, so: no MCP servers
    assert _flag_value(args, "--setting-sources") == ""  # no user / project / local settings
    workdir, contents = spawn.working_dirs[0]
    assert workdir and workdir != os.getcwd()
    assert contents == []  # nothing to read even if a setting source were loaded
    assert SCHEMA in kwargs["input"] and QUESTION in kwargs["input"]
    assert kwargs["timeout"] == 30.0
    assert generated.sql == "SELECT COUNT(order_id) AS n FROM orders" and generated.error is None


def test_the_working_directory_is_not_left_behind_after_the_generation(spawn):
    """The directory exists for the call and no longer. An eval runs once per question, and a
    generator that littered a temporary directory per item would be a slow leak on a long run."""
    _cli_generator().generate(QUESTION, ORG, DATASOURCE)

    workdir, _ = spawn.working_dirs[0]
    assert not os.path.exists(workdir)


def test_the_child_is_given_no_way_to_read_the_answer_key_off_disk(spawn, monkeypatch):
    """`HOME` is on the environment allowlist, and that is the whole of the route to the key.

    From `HOME` a child with a file-reading tool reaches `~/.config/agami/path`, which NAMES the
    artifacts directory; the dataset under it carries `expected.sql` and the rows a previous run
    recorded, and the warehouse credentials file sits beside it. Withholding `AGAMI_ARTIFACTS_DIR`
    hides the name of that path, not the root it is computed from — so the three flags are what
    make the `HOME` in this environment inert, rather than decoration on the argument list.
    """
    monkeypatch.setenv("HOME", "/home/example")

    _cli_generator().generate(QUESTION, ORG, DATASOURCE)

    args, kwargs = spawn.invocations[0]
    assert kwargs["env"]["HOME"] == "/home/example"
    assert _flag_value(args, "--tools") == ""
    assert "--strict-mcp-config" in args and "--mcp-config" not in args
    assert _flag_value(args, "--setting-sources") == ""


def test_the_invocation_is_the_same_on_every_call(spawn):
    """One tuple, no branch: a flag that some runs get and others do not is a flag that will be
    missing on the run that mattered. The module-level constant is what makes that unrepresentable,
    so a second generation is asserted to be argument-for-argument the first."""
    generator = _cli_generator()
    generator.generate(QUESTION, ORG, DATASOURCE)
    generator.generate("How many customers are there?", ORG, None)

    assert spawn.invocations[0][0] == spawn.invocations[1][0]
    assert isinstance(gr._CLIENT_ARGV, tuple)


def test_the_answer_key_is_in_nothing_the_generator_was_given(chokepoint, spawn):
    """Criterion 1. A model that can see `expected.sql` is grading itself, and no assertion over
    the scores would reveal it — so the assertion is over what the child was handed."""
    item = _item("keyed", expected={"sql": ANSWER_KEY, "sql_confirmed": True})

    _run(_dataset(item, _item("keyed-2", expected={"sql": ANSWER_KEY, "sql_confirmed": True})),
         _cli_generator(), _SpyExecutor())

    given = spawn.everything_given()
    assert len(spawn.invocations) == 2
    assert ANSWER_KEY not in given
    assert "orders_placed_marker" not in given


def test_no_result_row_is_in_anything_the_generator_was_given(chokepoint, spawn):
    """Criterion 3. The second item is generated AFTER the first item's rows exist, which is the
    only ordering under which this could have failed."""
    _run(_dataset(_item("a"), _item("b")), _cli_generator(), _SpyExecutor())

    assert len(spawn.invocations) == 2
    assert str(ROW_VALUE) not in spawn.everything_given()


def test_the_dataset_path_is_in_no_argument_and_no_environment_value(
    chokepoint, spawn, monkeypatch, tmp_path
):
    """Criterion 2. The child's environment is built, not inherited.

    `subprocess.run` passes the parent's environment through by default, and the parent's carries
    the dataset's own root and the warehouse DSN. Every argument-level assertion would still pass
    while the path was one `os.environ` read away inside the child.
    """
    artifacts = tmp_path / "artifacts-marker"
    artifacts.mkdir()
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(artifacts))
    monkeypatch.setenv("DATASOURCE_URL__ACME", "sqlite:///warehouse-marker.db")

    _run(_dataset(_item()), _cli_generator(), _SpyExecutor())

    args, kwargs = spawn.invocations[0]
    passed = kwargs["env"]
    assert [key for key in passed if key.startswith("AGAMI_")] == []
    assert [key for key in passed if "DATASOURCE" in key] == []
    given = spawn.everything_given()
    assert str(artifacts) not in given and "artifacts-marker" not in given
    assert "warehouse-marker" not in given


def test_a_failed_generation_reports_a_fixed_sentence_rather_than_stderr(monkeypatch):
    """A client can echo the prompt on stderr, and a score is rendered and persisted further on.
    So a failed generation reports a fixed sentence and the child's output is dropped."""
    echoed = "the whole prompt, echoed back, including " + ANSWER_KEY
    monkeypatch.setattr(
        gr.subprocess, "run", _RecordedSpawn(stdout="", returncode=1, stderr=echoed)
    )

    generated = _cli_generator().generate(QUESTION, ORG, DATASOURCE)

    assert generated.sql == ""
    assert generated.error and echoed not in generated.error
    assert ANSWER_KEY not in generated.error and "stderr" not in generated.error


def test_an_unreadable_answer_is_an_error_rather_than_a_statement(monkeypatch):
    """Whatever a model wrote is not SQL until it parses as the object we asked for."""
    monkeypatch.setattr(gr.subprocess, "run", _RecordedSpawn(stdout="I could not work that out."))

    generated = _cli_generator().generate(QUESTION, ORG, DATASOURCE)

    assert generated.sql == "" and generated.error


def test_a_fenced_answer_is_still_read(monkeypatch):
    """Models fence their JSON as often as not, and an item lost to a code fence is a wrong number
    in the run rather than a wrong answer by the model."""
    fenced = "Here you go:\n```json\n" + ANSWER + "\n```\nHope that helps.\n"
    monkeypatch.setattr(gr.subprocess, "run", _RecordedSpawn(stdout=fenced))

    assert _cli_generator().generate(QUESTION, ORG, DATASOURCE).sql.startswith("SELECT COUNT")


def test_a_generation_that_hangs_is_cut_off_and_the_run_returns(chokepoint, monkeypatch):
    """Criterion 8. The bound is `subprocess.run`'s own, so the child is killed rather than waited
    on, and the item it belonged to is an error like any other."""
    hangs = _RecordedSpawn(raises=subprocess.TimeoutExpired(cmd=["claude", "-p"], timeout=30.0))
    monkeypatch.setattr(gr.subprocess, "run", hangs)

    result = _run(_dataset(_item("a"), _item("b")), _cli_generator(), _SpyExecutor())

    assert hangs.invocations[0][1]["timeout"] == 30.0
    assert [outcome.score.status for outcome in result.outcomes] == ["error", "error"]
    assert result.completed and result.errored == 2


def test_the_runner_makes_no_model_call_of_its_own(chokepoint, monkeypatch):
    """Criterion 16. The injected generator is the only thing that may reach a model.

    Pinned by making the module's own spawn fatal: a run driven by a stub generator completes
    without it being touched, so there is no second, unasserted egress in the scoring path.
    """

    def _forbidden(*args, **kwargs):
        raise AssertionError("the runner spawned a process of its own")

    monkeypatch.setattr(gr.subprocess, "run", _forbidden)
    generator = _StubGenerator()

    result = _run(_dataset(_item("a"), _item("b")), generator, _SpyExecutor())

    assert result.completed and len(result.outcomes) == 2
    assert [call[0] for call in generator.calls] == [QUESTION, QUESTION]


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


def test_a_near_miss_is_a_fail_at_every_decimal_place(chokepoint):
    """The pass mark is the raw 1.0, and a two-thirds-wrong answer does not discriminate that from
    any threshold a reader might reach for instead.

    So this is the comparator's own near miss: 4002 of 4004 rows is 0.99950…, which rounds up at
    three decimals and clears any threshold below it. Both sides carry the same values in both
    columns, so the columns pair and only the pairing of the two together moved.
    """
    golden_rows = [(index, index) for index in range(4004)]
    # Two rows have their second cell swapped with each other, so each column's values are
    # untouched and exactly two rows no longer say what the answer key says.
    generated_rows = [(0, 1), (1, 0), *golden_rows[2:]]
    spy = _SpyExecutor({GOLDEN_SQL: golden_rows, GENERATED_SQL: generated_rows})

    result = _run(_dataset(_item()), _StubGenerator(), spy)

    accuracy = result.outcomes[0].score.accuracy
    assert accuracy == pytest.approx(4002 / 4004)
    assert round(accuracy, 3) == 1.0  # which is why the pass mark reads the unrounded value
    assert result.outcomes[0].passed is False and result.gating_failures == 1
