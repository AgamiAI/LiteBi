"""The eval helper's exit contract: what a run's exit code says, and what it refuses to say.

`run_golden_eval.py` printed verdicts and exited 0 for every run that reached the end of its
wiring, which made it unusable as a gate — CI cannot tell a clean suite from one where the
generator was never on the machine. This file pins the three answers it now gives (`0` green,
`1` a confirmed regression, `2` no verdict at all) and, more importantly, the ORDER in which they
are decided: a run that both stopped partway and has failures in it reports the broken pipeline,
because sending somebody to debug a model change that never happened is the expensive mistake.

The fixtures are the sibling file's: the shipped sample store, the real reader, chokepoint and
comparator, and a scripted generator in place of the client — a run in CI must not spend a token,
and a live model would make an exit-code assertion ambiguous between the contract and whatever
statement the model happened to write.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any, Optional

import pytest
import yaml

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))
SAMPLE_DIR = REPO_ROOT / "plugins" / "agami" / "samples" / "store"
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))
sys.path.insert(0, str(SAMPLE_DIR))

import build_sample  # noqa: E402
import run_golden_eval  # noqa: E402
from semantic_model import golden_run as gr  # noqa: E402

PROFILE = "agami-example"

# Planted in the two statements so the no-SQL rule can be asserted at the exit-code surface too.
# Column aliases, because the comparator pairs by value and ignores names.
GOLDEN_SENTINEL = "goldensentinelzzq"
GENERATED_SENTINEL = "generatedsentinelzzq"

Q_PASS = "How many orders have been placed?"
Q_FAIL = "How many customers are on file?"
Q_UNCONFIRMED = "How many payments have been taken?"

GENERATED = {
    Q_PASS: "SELECT COUNT(id) AS n FROM orders",
    # A different number off the same table, so the miss is a real disagreement about rows rather
    # than an accident of which table is larger.
    Q_FAIL: f"SELECT COUNT(DISTINCT country) AS {GENERATED_SENTINEL} FROM customers",
    Q_UNCONFIRMED: "SELECT COUNT(*) AS n FROM refunds",
}

PASSING_ITEM: dict[str, Any] = {
    "id": "orders-count",
    "query": Q_PASS,
    "expected": {"sql": "SELECT COUNT(*) AS n FROM orders", "sql_confirmed": True},
}

# Confirmed and scored a miss, so it is the one thing a run may exit `1` on. Its answer key carries
# the sentinel, because the reasons that name a column are only ever built on a disagreement.
FAILING_ITEM: dict[str, Any] = {
    "id": "customers-count",
    "query": Q_FAIL,
    "expected": {
        "sql": f"SELECT COUNT(*) AS {GOLDEN_SENTINEL} FROM customers",
        "sql_confirmed": True,
    },
}

# Scores a miss and can never gate: nobody has confirmed its answer key.
UNCONFIRMED_ITEM: dict[str, Any] = {
    "id": "payments-count",
    "query": Q_UNCONFIRMED,
    "expected": {"sql": "SELECT COUNT(*) AS n FROM payments", "sql_confirmed": False},
}

# The shape the confirmed-only rule is invisible in until you look at the exit code: something
# failed, and nothing that failed can gate.
PASS_AND_UNCONFIRMED_MISS: dict[str, Any] = {"test_cases": [PASSING_ITEM, UNCONFIRMED_ITEM]}

# The failing case FIRST, which is also what makes it usable for the ordering: a generator that
# raises on its second question leaves a run that is both incomplete and carrying a gating failure.
ONE_FAILURE: dict[str, Any] = {"test_cases": [FAILING_ITEM, PASSING_ITEM]}

ONLY_UNCONFIRMED: dict[str, Any] = {"test_cases": [UNCONFIRMED_ITEM]}


def _tagged(item: dict[str, Any], *tags: str) -> dict[str, Any]:
    """The same case, carrying tags. A copy, so the untagged datasets above stay untagged."""
    return {**item, "tags": list(tags)}


# The tags overlap on purpose: `smoke` and `revenue` both name the failing case, so a selection of
# the two has a different size under OR (two cases) than under AND (one) — which is the only way a
# test can tell the two readings apart. `draft` names the unconfirmed case alone, the selection
# that runs correctly and can gate on nothing.
TAGGED: dict[str, Any] = {
    "test_cases": [
        _tagged(PASSING_ITEM, "smoke"),
        _tagged(FAILING_ITEM, "smoke", "revenue"),
        _tagged(UNCONFIRMED_ITEM, "draft"),
    ]
}


class _Scripted:
    """The shipped generator's stand-in, constructed the way the script constructs the real one."""

    def __init__(self, schema: str, *, timeout_s: float) -> None:
        self.schema = schema
        self.timeout_s = timeout_s

    def generate(self, question: str, org: str, datasource: Optional[str]) -> gr.GeneratedSql:
        sql = GENERATED.get(question)
        if sql is None:
            return gr.GeneratedSql(sql="", error="no statement was scripted for this question")
        return gr.GeneratedSql(sql=sql, error=None)


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory) -> Path:
    """The shipped sample database, built once for the whole file."""
    db = tmp_path_factory.mktemp("store") / "store.db"
    build_sample.build(db, prefer_cli=False)
    return db


@pytest.fixture()
def artifacts(tmp_path, monkeypatch, warehouse) -> Path:
    """A profile wired the way the onboarding path wires one, with no datasets in it yet."""
    art = tmp_path / "artifacts"
    shutil.copytree(SAMPLE_DIR / "model", art / PROFILE)

    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))
    # The org-less form: this helper runs on one operator's machine and resolves to the
    # single-tenant `local` org, the only org those vars are offered to.
    monkeypatch.setenv("DATASOURCE_URL__AGAMI_EXAMPLE", f"sqlite:///{warehouse}")
    for var in ("AGAMI_DB_URL", "APP_DATABASE_URL", "AGAMI_ORG_ID", "AGAMI_SQL_TIMEOUT_S"):
        monkeypatch.delenv(var, raising=False)
    return art


@pytest.fixture()
def scripted(monkeypatch) -> None:
    """Answer every question from a table instead of spawning the operator's client."""
    monkeypatch.setattr(run_golden_eval, "ClaudeCliGenerator", _Scripted)


def _write(art: Path, name: str, dataset: dict[str, Any]) -> None:
    golden = art / PROFILE / "golden_datasets"
    golden.mkdir(exist_ok=True)
    (golden / f"{name}.yaml").write_text(yaml.safe_dump(dataset), encoding="utf-8")


def _run(capsys, *argv: str) -> tuple[int, dict[str, Any], str]:
    """Invoke the helper and read back its exit code, its JSON and its stderr."""
    code = run_golden_eval.main(["--profile", PROFILE, *argv])
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out.strip() else {}
    return code, payload, captured.err


# ---------------------------------------------------------------------------
# The three verdicts
# ---------------------------------------------------------------------------

def test_a_run_whose_confirmed_cases_all_pass_is_green(artifacts, scripted, capsys):
    """And it stays green with a failing UNCONFIRMED case beside them.

    This is the confirmed-only rule observed at the exit status rather than in a counter: the run
    reports `failed: 1`, and the item behind that number has an answer key nobody has reviewed, so
    gating on it would fail CI on something no human has agreed to."""
    _write(artifacts, "mixed", PASS_AND_UNCONFIRMED_MISS)

    code, payload, _ = _run(capsys, "--dataset", "mixed")

    assert code == 0
    assert payload["summary"]["failed"] == 1
    assert payload["summary"]["gating_failures"] == 0


def test_one_confirmed_failure_exits_one_and_names_the_item(artifacts, scripted, capsys):
    """The regression verdict. The exit code is what CI reads; the item key is what a person needs
    in order to act on it, so both are asserted at once."""
    _write(artifacts, "regressed", ONE_FAILURE)

    code, payload, _ = _run(capsys, "--dataset", "regressed")

    assert code == 1
    failures = [item for item in payload["items"] if item["section"] == "failure"]
    assert [item["item_key"] for item in failures] == ["customers-count"]


def test_a_run_that_stopped_partway_cannot_produce_a_verdict(artifacts, monkeypatch, capsys):
    """`completed: false` means the cases after the stop were never attempted. The counts describe
    a fraction of the dataset, so there is no verdict to give — not a green one and not a red one.

    Everything the run DID reach passed, so nothing but `completed` distinguishes it from a clean
    suite."""

    class _RaisesOnTheSecond:
        def __init__(self, schema: str, *, timeout_s: float) -> None:
            self.answered = 0

        def generate(self, question, org, datasource):
            self.answered += 1
            if self.answered > 1:
                raise RuntimeError("the client fell over")
            return gr.GeneratedSql(sql=GENERATED[Q_PASS], error=None)

    monkeypatch.setattr(run_golden_eval, "ClaudeCliGenerator", _RaisesOnTheSecond)
    _write(artifacts, "stopped", {"test_cases": [PASSING_ITEM, FAILING_ITEM]})

    code, payload, _ = _run(capsys, "--dataset", "stopped")

    assert code == 2
    assert payload["summary"]["completed"] is False
    assert payload["summary"]["gating_failures"] == 0


def test_a_run_where_every_generation_errored_cannot_produce_a_verdict(
    artifacts, monkeypatch, capsys
):
    """The `claude` binary not being on the runner. Nothing was scored, so this is neither a
    regression nor a passing suite — and the rule is categorical rather than proportional, because
    any fraction here would be the pass-rate threshold this deliberately does not have."""

    class _AnswersNothing:
        def __init__(self, schema: str, *, timeout_s: float) -> None:
            pass

        def generate(self, question, org, datasource):
            return gr.GeneratedSql(sql="", error="no statement was scripted for this question")

    monkeypatch.setattr(run_golden_eval, "ClaudeCliGenerator", _AnswersNothing)
    _write(artifacts, "regressed", ONE_FAILURE)

    code, payload, _ = _run(capsys, "--dataset", "regressed")

    assert code == 2
    total = payload["summary"]["total"]
    assert payload["summary"]["errored"] == total > 0
    assert payload["summary"]["gating_failures"] == 0


def test_an_incomplete_run_with_failures_in_it_reports_the_broken_pipeline(
    artifacts, monkeypatch, capsys
):
    """The ordering, and the whole reason the checks are in the order they are.

    This run has a confirmed failure in it AND stopped partway, so both rules apply and only one
    code can be returned. It is `2`: the failure is real but the run around it is not trustworthy,
    and sending somebody to debug a model change against a run that never finished is the expensive
    mistake this ordering exists to prevent."""

    class _RaisesAfterTheFirst:
        def __init__(self, schema: str, *, timeout_s: float) -> None:
            self.answered = 0

        def generate(self, question, org, datasource):
            self.answered += 1
            if self.answered > 1:
                raise RuntimeError("the client fell over")
            return gr.GeneratedSql(sql=GENERATED[Q_FAIL], error=None)

    monkeypatch.setattr(run_golden_eval, "ClaudeCliGenerator", _RaisesAfterTheFirst)
    _write(artifacts, "half", ONE_FAILURE)

    code, payload, _ = _run(capsys, "--dataset", "half")

    # Both conditions really are present, or the ordering is not what is being asserted.
    assert payload["summary"]["completed"] is False
    assert payload["summary"]["gating_failures"] == 1
    assert code == 2


# ---------------------------------------------------------------------------
# What a green run has to admit about itself
# ---------------------------------------------------------------------------

def test_a_run_with_nothing_confirmed_is_green_and_says_it_gated_on_nothing(
    artifacts, scripted, capsys
):
    """The state most profiles are actually in. The run worked, so it exits `0` — but a green
    status that verified nothing is the one that gets mistaken for evidence, so it has to say so
    out loud rather than only in a counter nobody reads."""
    _write(artifacts, "draft", ONLY_UNCONFIRMED)

    code, _, err = _run(capsys, "--dataset", "draft")

    assert code == 0
    # The same prefix the refusals carry, so a log has one thing to grep and a caller one thing
    # to strip.
    warning = next(line for line in err.splitlines() if "gated on nothing" in line)
    assert warning.startswith("agami-eval: ")


def test_a_run_with_a_confirmed_case_does_not_warn_about_gating(artifacts, scripted, capsys):
    """The other half of the warning: it fires on the absence of a confirmed case and not on every
    run, or it is noise and a reader learns to skip the line."""
    _write(artifacts, "regressed", ONE_FAILURE)

    _, _, err = _run(capsys, "--dataset", "regressed")

    assert "gated on nothing" not in err


# ---------------------------------------------------------------------------
# The human line, and the stream it does not go on
# ---------------------------------------------------------------------------

def test_the_summary_line_goes_to_stderr_and_leaves_stdout_parseable(
    artifacts, scripted, capsys
):
    """A pipeline log needs one line a person can read the run against, and every caller of this
    helper parses stdout as a JSON document — so the sentence goes on stderr, where the prefixed
    refusals already are. `_run` parses stdout, so a line printed there fails this outright."""
    _write(artifacts, "mixed", PASS_AND_UNCONFIRMED_MISS)

    _, payload, err = _run(capsys, "--dataset", "mixed")

    # `_run` parsed stdout as JSON to hand back `payload`, so a sentence printed there fails
    # before this line is reached — and the counts are spelled out rather than read back off the
    # payload, which would assert the line against itself.
    assert payload["items"]
    assert (
        f"Ran mixed on {PROFILE}: 0 failed, 0 errored, 0 unscored, 1 unconfirmed, 1 passed "
        "— run completed: yes."
    ) in err


def test_the_summary_line_says_when_the_run_did_not_complete(artifacts, monkeypatch, capsys):
    """The one fact the counts cannot carry: a run that stopped reports numbers over the cases it
    reached, and a reader comparing them to last week's needs to be told not to."""

    class _RaisesImmediately:
        def __init__(self, schema: str, *, timeout_s: float) -> None:
            pass

        def generate(self, question, org, datasource):
            raise RuntimeError("the client fell over")

    monkeypatch.setattr(run_golden_eval, "ClaudeCliGenerator", _RaisesImmediately)
    _write(artifacts, "stopped", ONE_FAILURE)

    _, _, err = _run(capsys, "--dataset", "stopped")

    assert "run completed: no." in err


# ---------------------------------------------------------------------------
# The rule the payload exists to keep, at the surface that now gates
# ---------------------------------------------------------------------------

def test_a_failing_run_still_carries_neither_statement(artifacts, scripted, capsys):
    """Asserted on a FAILING case, because a passing one proves nothing: its `reason` is empty, and
    the two reasons built out of the answer key's own column names are only ever built on a
    disagreement. The exit code is now what a pipeline surfaces, so the payload behind it is the
    thing most likely to be pasted into a chat window."""
    _write(artifacts, "regressed", ONE_FAILURE)

    code, payload, err = _run(capsys, "--dataset", "regressed")

    rendered = json.dumps(payload)
    assert code == 1 and any(item["section"] == "failure" for item in payload["items"])
    assert GOLDEN_SENTINEL not in rendered
    assert GENERATED_SENTINEL not in rendered
    # …and the stderr the same pipeline log carries is held to the same rule.
    assert GOLDEN_SENTINEL not in err and GENERATED_SENTINEL not in err


# ---------------------------------------------------------------------------
# Selecting a slice of a dataset by tag
# ---------------------------------------------------------------------------

def test_a_tag_runs_only_the_cases_carrying_it(artifacts, scripted, capsys):
    """The selection is asserted on what the run actually contains rather than on a printed count:
    a filter that narrowed nothing and a filter that narrowed correctly say the same thing in a
    summary line, and differ only in which cases have verdicts."""
    _write(artifacts, "suite", TAGGED)

    _, payload, _ = _run(capsys, "--dataset", "suite", "--tag", "smoke")

    assert len(payload["items"]) == 2
    assert {item["item_key"] for item in payload["items"]} == {"orders-count", "customers-count"}


def test_two_tags_run_their_union_and_not_their_intersection(artifacts, scripted, capsys):
    """OR across tags: a suite is the union of the slices asked for. `smoke` and `revenue` overlap
    on the failing case, so AND would run that one case alone — the count is what separates the two
    readings, and the intersection is spelled out here so a future change to OR fails loudly."""
    _write(artifacts, "suite", TAGGED)

    _, payload, _ = _run(capsys, "--dataset", "suite", "--tag", "smoke", "--tag", "revenue")

    keys = {item["item_key"] for item in payload["items"]}
    assert keys == {"orders-count", "customers-count"}
    # The case both tags name. Under AND this would be the whole run.
    assert keys != {"customers-count"}


def test_a_tag_no_case_carries_refuses_and_names_the_tags_that_exist(artifacts, scripted, capsys):
    """A tag nothing carries is a wrong selector, not an empty result, so it is `2` and not a green
    run over zero cases — a typo in CI would otherwise pass forever. The refusal names the tags the
    dataset does have, because that list is the whole of what the next invocation needs."""
    _write(artifacts, "suite", TAGGED)

    code, payload, err = _run(capsys, "--dataset", "suite", "--tag", "smoek")

    assert code == 2
    assert payload == {}
    assert "draft" in err and "revenue" in err and "smoke" in err


def test_tag_matching_is_case_sensitive(artifacts, scripted, capsys):
    """Tags are free text the dataset's author wrote, and nothing else that reads them folds case.
    Matching loosely here would invent a vocabulary the file itself does not have."""
    _write(artifacts, "suite", TAGGED)

    code, _, _ = _run(capsys, "--dataset", "suite", "--tag", "Smoke")

    assert code == 2


def test_a_tag_selecting_only_unconfirmed_cases_is_green_and_says_it_gated_on_nothing(
    artifacts, scripted, capsys
):
    """The confusing pair with the refusal above, and the reason it is tested next to it: this
    selector was right and the run was fine — there was simply nothing in the slice that could
    gate. A `2` here would tell CI the harness is broken when nothing is."""
    _write(artifacts, "suite", TAGGED)

    code, payload, err = _run(capsys, "--dataset", "suite", "--tag", "draft")

    assert code == 0
    assert [item["item_key"] for item in payload["items"]] == ["payments-count"]
    assert "gated on nothing" in err


def test_a_tag_selecting_a_confirmed_failure_still_exits_one(artifacts, scripted, capsys):
    """Selecting a slice narrows what runs and changes nothing about how a verdict is reached."""
    _write(artifacts, "suite", TAGGED)

    code, payload, _ = _run(capsys, "--dataset", "suite", "--tag", "revenue")

    assert code == 1
    assert [item["item_key"] for item in payload["items"]] == ["customers-count"]


def test_no_tag_runs_every_case(artifacts, scripted, capsys):
    """The default is unchanged: a dataset whose cases carry tags runs whole when none is named."""
    _write(artifacts, "suite", TAGGED)

    _, payload, _ = _run(capsys, "--dataset", "suite")

    assert len(payload["items"]) == 3


def test_a_tag_against_a_dataset_with_no_tags_says_so(artifacts, scripted, capsys):
    """The state every dataset starts in. There is no list of tags to name, so the refusal says
    that in words rather than printing an empty one — "Tags present:" followed by nothing reads as
    a broken message and tells the reader nothing about what to do next."""
    _write(artifacts, "plain", ONE_FAILURE)

    code, _, err = _run(capsys, "--dataset", "plain", "--tag", "smoke")

    assert code == 2
    assert "carries a tag at all" in err
    # The list-shaped half of the refusal is the thing that would have rendered empty.
    assert "Tags present" not in err
