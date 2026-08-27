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
Q_FAIL_TOO = "How many products are listed?"
Q_UNCONFIRMED = "How many payments have been taken?"
# Deliberately absent from `GENERATED` below, which is what makes its case error.
Q_ERRORS = "How many invoices were issued?"

# The statement that makes the confirmed case miss, kept as a name so a test can put it back after
# monkeypatching a passing answer over it.
GENERATED_FAILING = f"SELECT COUNT(DISTINCT country) AS {GENERATED_SENTINEL} FROM customers"

GENERATED = {
    Q_PASS: "SELECT COUNT(id) AS n FROM orders",
    # A different number off the same table, so the miss is a real disagreement about rows rather
    # than an accident of which table is larger.
    Q_FAIL: GENERATED_FAILING,
    Q_FAIL_TOO: "SELECT COUNT(DISTINCT category_id) AS n FROM products",
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

# A second confirmed miss, so a run can record a failure LIST rather than a single failure — which
# is the only shape in which "the list narrowed" is observable.
SECOND_FAILING_ITEM: dict[str, Any] = {
    "id": "products-count",
    "query": Q_FAIL_TOO,
    "expected": {"sql": "SELECT COUNT(*) AS n FROM products", "sql_confirmed": True},
}

# Confirmed, and its generation produces no statement at all. It is `passed: false` like a failure
# and it is not one — nothing was scored.
ERRORING_ITEM: dict[str, Any] = {
    "id": "invoices-count",
    "query": Q_ERRORS,
    "expected": {"sql": "SELECT COUNT(*) AS n FROM invoices", "sql_confirmed": True},
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


def test_the_summary_line_goes_to_stderr_and_leaves_stdout_parseable(artifacts, scripted, capsys):
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


# ---------------------------------------------------------------------------
# The artifact, and re-running what failed
# ---------------------------------------------------------------------------


def _artifacts_in(art: Path) -> list[Path]:
    return sorted((art / "local" / "eval" / PROFILE).glob("*.json"))


def _artifact(art: Path) -> dict[str, Any]:
    return json.loads(_artifacts_in(art)[-1].read_text(encoding="utf-8"))


def test_the_artifact_records_the_verdict_and_still_records_the_statements(
    artifacts, scripted, capsys
):
    """The four verdict fields are what makes a failure list recoverable, and they are ADDITIVE:
    the report slice reads this same file, so nothing that was here before may go."""
    _write(artifacts, "mixed", ONE_FAILURE)

    _run(capsys, "--dataset", "mixed")

    item = _artifact(artifacts)["items"][0]
    assert {"confirmed", "passed", "gated", "section"} <= set(item)
    # Everything the record carried before the widening is still here.
    assert {"item_key", "question", "expected_sql", "generated_sql", "score"} <= set(item)


def test_the_recorded_section_is_the_one_the_terminal_printed(artifacts, scripted, capsys):
    """A re-run selects on `section`, so the file and the screen must classify identically —
    otherwise "re-run the failures" runs something the reader never saw under that heading."""
    _write(artifacts, "mixed", ONE_FAILURE)

    _, payload, _ = _run(capsys, "--dataset", "mixed")

    on_screen = {item["item_key"]: item["section"] for item in payload["items"]}
    on_disk = {item["item_key"]: item["section"] for item in _artifact(artifacts)["items"]}
    assert on_screen == on_disk


def test_a_rerun_runs_exactly_what_the_last_run_recorded_as_failures(artifacts, scripted, capsys):
    """Asserted against the artifact rather than a hard-coded list, so the test cannot agree with
    a bug that mis-records and then mis-selects the same way."""
    _write(artifacts, "mixed", ONE_FAILURE)
    first, _, _ = _run(capsys, "--dataset", "mixed")
    assert first == 1
    failed = [i["item_key"] for i in _artifact(artifacts)["items"] if i["section"] == "failure"]
    assert failed, "the first run must have failed something for this to mean anything"

    code, payload, _ = _run(capsys, "--dataset", "mixed", "--rerun-failures")

    assert [item["item_key"] for item in payload["items"]] == failed
    assert code == 1


def test_a_rerun_with_no_previous_run_refuses(artifacts, scripted, capsys):
    """It must not fall back to running everything: a narrow question answered widely costs a
    model call per case and buries the answer that was asked for."""
    _write(artifacts, "mixed", ONE_FAILURE)

    code, payload, err = _run(capsys, "--dataset", "mixed", "--rerun-failures")

    assert code == 2
    assert payload == {}
    assert "no previous run" in err


def test_a_rerun_after_a_clean_run_does_nothing_and_says_so(artifacts, scripted, capsys):
    """Nothing to re-run is not a wrong selector, so it is a clean exit — but a silent one would
    read exactly like a re-run that fixed everything."""
    _write(artifacts, "clean", {"test_cases": [PASSING_ITEM]})
    assert _run(capsys, "--dataset", "clean")[0] == 0

    before = len(_artifacts_in(artifacts))
    code, payload, err = _run(capsys, "--dataset", "clean", "--rerun-failures")

    assert code == 0
    assert "recorded no failures" in err
    # Nothing ran, so nothing printed and nothing was written: a 0-case artifact would become this
    # dataset's newest record and shadow the real one a later re-run needs.
    assert payload == {}
    assert len(_artifacts_in(artifacts)) == before


def test_a_rerun_reads_its_own_datasets_artifact_and_not_the_newest_one(
    artifacts, scripted, capsys
):
    """The directory is keyed on the profile, so the newest file may describe another dataset.
    Matching on the recorded name is what stops a re-run executing the wrong cases."""
    _write(artifacts, "mixed", ONE_FAILURE)
    _run(capsys, "--dataset", "mixed")
    mixed_failed = [
        i["item_key"] for i in _artifact(artifacts)["items"] if i["section"] == "failure"
    ]
    # A later run of a different dataset, which is now the newest file in the directory.
    _write(artifacts, "clean", {"test_cases": [PASSING_ITEM]})
    _run(capsys, "--dataset", "clean")
    assert _artifact(artifacts)["dataset"] == "clean"

    _, payload, _ = _run(capsys, "--dataset", "mixed", "--rerun-failures")

    assert [item["item_key"] for item in payload["items"]] == mixed_failed


def test_a_rerun_ignores_a_report_beside_the_artifacts(artifacts, scripted, capsys):
    """The report slice writes its HTML into this directory. Globbing `*` rather than `*.json`
    would try to read one as a run and refuse."""
    _write(artifacts, "mixed", ONE_FAILURE)
    _run(capsys, "--dataset", "mixed")
    (artifacts / "local" / "eval" / PROFILE / "99999999T999999999999Z.html").write_text(
        "<html>a report</html>", encoding="utf-8"
    )

    code, payload, _ = _run(capsys, "--dataset", "mixed", "--rerun-failures")

    assert code == 1
    assert payload["items"]


def test_a_rerun_skips_an_artifact_it_cannot_read(artifacts, scripted, capsys):
    """These accumulate for months; one truncated file from a full disk must not make the feature
    unusable for every run after it."""
    _write(artifacts, "mixed", ONE_FAILURE)
    _run(capsys, "--dataset", "mixed")
    failed = [i["item_key"] for i in _artifact(artifacts)["items"] if i["section"] == "failure"]
    (artifacts / "local" / "eval" / PROFILE / "99999999T999999999999Z.json").write_text(
        "{ this is not json", encoding="utf-8"
    )

    code, payload, _ = _run(capsys, "--dataset", "mixed", "--rerun-failures")

    assert code == 1
    assert [item["item_key"] for item in payload["items"]] == failed


def test_a_rerun_skips_a_failure_the_dataset_no_longer_has_and_says_how_many(
    artifacts, scripted, capsys
):
    """Editing a case away between runs is ordinary. Running fewer cases than the last run
    reported without saying so is not."""
    _write(artifacts, "mixed", ONE_FAILURE)
    _run(capsys, "--dataset", "mixed")
    # The failing case is edited out; only the passing one remains.
    _write(artifacts, "mixed", {"test_cases": [PASSING_ITEM]})

    code, payload, err = _run(capsys, "--dataset", "mixed", "--rerun-failures")

    assert code == 0
    assert payload == {}
    assert "no longer in" in err
    # And it must NOT also claim the last run was clean — it recorded a failure, which was deleted.
    # A skill reads that sentence back to the user as "the previous run was green".
    assert "recorded no failures" not in err


def test_naming_both_selections_is_refused(artifacts, scripted, capsys):
    """Two ways of narrowing the same dataset — asking for both is a question with no answer."""
    _write(artifacts, "tagged", TAGGED)

    with pytest.raises(SystemExit) as raised:
        run_golden_eval.main(
            ["--profile", PROFILE, "--dataset", "tagged", "--tag", "smoke", "--rerun-failures"]
        )

    assert raised.value.code != 0
    assert "not allowed with" in capsys.readouterr().err


def test_a_rerun_carries_no_statement_on_stdout(artifacts, scripted, capsys):
    """The no-SQL rule holds on the re-run path too, asserted on a FAILING re-run: a passing
    case's reason is empty and would prove nothing."""
    _write(artifacts, "mixed", ONE_FAILURE)
    _run(capsys, "--dataset", "mixed")

    _, payload, err = _run(capsys, "--dataset", "mixed", "--rerun-failures")

    rendered = json.dumps(payload)
    assert GOLDEN_SENTINEL not in rendered
    assert GENERATED_SENTINEL not in rendered
    assert GOLDEN_SENTINEL not in err
    assert GENERATED_SENTINEL not in err


# ---------------------------------------------------------------------------
# A re-run may only trust a whole, concluded run
# ---------------------------------------------------------------------------

# Named apart from the file's other erroring case on purpose: that one errors because the
# generator refuses the question, this one because no generator was scripted for it at all, and
# a re-run has to tell the sections apart rather than the reasons.
UNSCRIPTED_ITEM: dict[str, Any] = {
    "id": "unscripted-count",
    "query": "A question no generator has an answer for",
    "expected": {"sql": "SELECT COUNT(*) AS n FROM products", "sql_confirmed": True},
}

# One of each section a re-run must tell apart: a confirmed miss, an unconfirmed miss that can
# never gate, and a case whose generation produced nothing at all.
ONE_OF_EACH: dict[str, Any] = {"test_cases": [FAILING_ITEM, UNCONFIRMED_ITEM, UNSCRIPTED_ITEM]}


def test_a_tagged_slice_does_not_become_the_datasets_failure_record(
    artifacts, scripted, capsys, monkeypatch
):
    """The false green this refusal exists for. A slice describes some of the dataset, so its
    failure list is narrower than the truth — and inheriting it exits 0 over a live regression."""
    _write(artifacts, "tagged", TAGGED)
    assert _run(capsys, "--dataset", "tagged")[0] == 1
    # The tagged case now passes, so a slice over it alone records no failures at all.
    monkeypatch.setitem(GENERATED, Q_FAIL, "SELECT COUNT(*) AS n FROM customers")
    assert _run(capsys, "--dataset", "tagged", "--tag", "revenue")[0] == 0
    monkeypatch.setitem(GENERATED, Q_FAIL, GENERATED_FAILING)

    code, payload, err = _run(capsys, "--dataset", "tagged", "--rerun-failures")

    assert "recorded no failures" not in err
    assert [item["item_key"] for item in payload["items"]] == ["customers-count"]
    assert code == 1


def test_a_run_that_produced_no_verdict_is_not_read_as_a_clean_one(artifacts, capsys, monkeypatch):
    """An all-errored run records no failures because it scored nothing, not because nothing was
    wrong. Trusting its empty list arrives at exactly the green the `2` branch refuses."""
    _write(artifacts, "mixed", ONE_FAILURE)

    class _Silent(_Scripted):
        def generate(self, question, org, datasource):
            return gr.GeneratedSql(sql="", error="the generator did not answer")

    monkeypatch.setattr(run_golden_eval, "ClaudeCliGenerator", _Silent)
    assert _run(capsys, "--dataset", "mixed")[0] == 2

    monkeypatch.setattr(run_golden_eval, "ClaudeCliGenerator", _Scripted)
    code, payload, err = _run(capsys, "--dataset", "mixed", "--rerun-failures")

    # No usable record exists, so it refuses rather than reporting a clean re-run.
    assert code == 2
    assert payload == {}
    assert "no previous run" in err


def test_an_incomplete_run_is_not_read_as_a_failure_record(artifacts, capsys, monkeypatch):
    """Same rule for a run that stopped partway: its truncated list would narrow every re-run
    after it."""
    _write(artifacts, "mixed", ONE_FAILURE)

    class _Raises(_Scripted):
        def __init__(self, schema, *, timeout_s):
            super().__init__(schema, timeout_s=timeout_s)
            self.asked = 0

        def generate(self, question, org, datasource):
            self.asked += 1
            if self.asked > 1:
                raise RuntimeError("the client fell over")
            return super().generate(question, org, datasource)

    monkeypatch.setattr(run_golden_eval, "ClaudeCliGenerator", _Raises)
    assert _run(capsys, "--dataset", "mixed")[0] == 2

    monkeypatch.setattr(run_golden_eval, "ClaudeCliGenerator", _Scripted)
    code, _, err = _run(capsys, "--dataset", "mixed", "--rerun-failures")

    assert code == 2
    assert "no previous run" in err


def test_a_rerun_selects_the_failures_and_not_merely_what_did_not_pass(artifacts, scripted, capsys):
    """`section` and not `passed`. An errored case did not pass and an unconfirmed miss did not
    pass, and neither is a failure — selecting on `passed` would re-run the whole dataset."""
    _write(artifacts, "each", ONE_OF_EACH)
    first, payload, _ = _run(capsys, "--dataset", "each")
    assert first == 1
    sections = {item["section"] for item in payload["items"]}
    assert {"failure", "error", "unconfirmed"} <= sections, sections

    _, payload, _ = _run(capsys, "--dataset", "each", "--rerun-failures")

    assert [item["item_key"] for item in payload["items"]] == ["customers-count"]


def test_an_unreachable_datasource_cannot_produce_a_verdict(
    artifacts, scripted, capsys, monkeypatch
):
    """The criterion this contract exists for. It reaches `2` by a different route than a
    generation failure does — the statements are written, and then nothing can execute them."""
    _write(artifacts, "mixed", ONE_FAILURE)
    monkeypatch.delenv("DATASOURCE_URL__AGAMI_EXAMPLE")

    code, payload, _ = _run(capsys, "--dataset", "mixed")

    assert code == 2
    assert payload["summary"]["errored"] == payload["summary"]["total"]
