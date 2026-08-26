"""The eval helper: what it prints, in what order, and what it refuses to print.

`run_golden_eval.py` is the only caller of `run_golden_dataset` that a person can invoke, so the
things it decides are not covered anywhere else: which dataset a bare invocation runs, what a
verdict looks like on a terminal, and — the load-bearing one — that the statements never reach
that terminal. Two sentinel strings, one planted in the answer key and one in the generated
statement, make the last of those a real assertion rather than an inspection.

The generator is replaced with a scripted one, for the reason the end-to-end file gives: a run in
CI must not spend a token, and a live model would make a failing ordering assertion ambiguous
between the ordering and the statement it happened to write. Everything else is real — the shipped
sample store, the reader, the chokepoint and the comparator.
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

import agami_paths  # noqa: E402
import build_sample  # noqa: E402
import run_golden_eval  # noqa: E402
from semantic_model import golden_run as gr  # noqa: E402

PROFILE = "agami-example"

# Planted in the two statements so the no-SQL rule can be asserted rather than eyeballed. Column
# aliases, because the comparator pairs by value and ignores names — so carrying them changes no
# verdict, which is what makes them safe to plant in a passing case.
GOLDEN_SENTINEL = "goldensentinelzzq"
GENERATED_SENTINEL = "generatedsentinelzzq"

Q_PASS = "How many orders have been placed?"
Q_FAIL = "How many customers are on file?"
Q_ERROR = "How many products are listed?"
Q_UNCONFIRMED = "How many payments have been taken?"

GENERATED = {
    Q_PASS: f"SELECT COUNT(id) AS {GENERATED_SENTINEL} FROM orders",
    # A different number from the same table, so the failure is a real disagreement over rows
    # rather than an accident of which table happens to be larger.
    Q_FAIL: "SELECT COUNT(DISTINCT country) AS n FROM customers",
    Q_UNCONFIRMED: "SELECT COUNT(*) AS n FROM refunds",
    # Q_ERROR is deliberately absent: the scripted generator answers nothing for it.
}

# One case of each section, written in an order that is NOT the presentation order — a file that
# already listed them failures-first would let a run that preserves the file's order pass the
# ordering assertion.
MIXED_DATASET: dict[str, Any] = {
    "description": "One case of each kind over the sample store.",
    "test_cases": [
        {
            "id": "orders-count",
            "query": Q_PASS,
            "expected": {
                "sql": f"SELECT COUNT(*) AS {GOLDEN_SENTINEL} FROM orders",
                "sql_confirmed": True,
            },
        },
        {
            "id": "payments-count",
            "query": Q_UNCONFIRMED,
            # Nobody has confirmed this answer key, so it reports its score and cannot gate.
            "expected": {"sql": "SELECT COUNT(*) AS n FROM payments", "sql_confirmed": False},
        },
        {
            "id": "products-count",
            "query": Q_ERROR,
            "expected": {"sql": "SELECT COUNT(*) AS n FROM products", "sql_confirmed": True},
        },
        {
            "id": "customers-count",
            "query": Q_FAIL,
            "expected": {"sql": "SELECT COUNT(*) AS n FROM customers", "sql_confirmed": True},
        },
    ],
}

PASSING_DATASET: dict[str, Any] = {
    "test_cases": [
        {
            "id": "orders-count",
            "query": Q_PASS,
            "expected": {
                "sql": f"SELECT COUNT(*) AS {GOLDEN_SENTINEL} FROM orders",
                "sql_confirmed": True,
            },
        },
    ],
}


class _Scripted:
    """The shipped generator's stand-in, constructed the same way the script constructs the real
    one — so a change to that call site fails here rather than silently bypassing the schema."""

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
    """A profile wired the way the onboarding path wires one — and with no golden datasets yet,
    which is both the normal starting state and the `--list` case that has to stay quiet."""
    art = tmp_path / "artifacts"
    shutil.copytree(SAMPLE_DIR / "model", art / PROFILE)

    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))
    # The org-less form, because this helper runs on one operator's own machine and resolves to the
    # single-tenant `local` org — the only org those vars are offered to.
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


def _sections(payload: dict[str, Any]) -> list[str]:
    return [item["section"] for item in payload["items"]]


# ---------------------------------------------------------------------------
# --list — the cheap answer, with no database and no generator
# ---------------------------------------------------------------------------

def test_listing_a_profile_with_no_datasets_is_not_an_error(artifacts, capsys):
    """The starting state of every profile. It has to name the directory to create, because that
    is the whole of the advice a caller can give from it."""
    code, payload, _ = _run(capsys, "--list")

    assert code == 0
    assert payload["datasets"] == [] and payload["findings"] == []
    assert payload["datasets_dir"] == str(artifacts / PROFILE / "golden_datasets")


def test_listing_reports_what_is_confirmed_and_what_is_not(artifacts, capsys):
    """A dataset of four cases of which one is unconfirmed answers "this dataset has nothing that
    can gate" without running anything."""
    _write(artifacts, "mixed", MIXED_DATASET)

    _, payload, _ = _run(capsys, "--list")

    assert payload["datasets"] == [
        {"name": "mixed", "total": 4, "confirmed": 3, "unconfirmed": 1}
    ]


def test_listing_reads_no_credentials(artifacts, monkeypatch, capsys):
    """`--list` is what a caller reaches for before anything is connected, so it must not touch the
    warehouse. With the DSN removed a run would fail; the listing still answers."""
    _write(artifacts, "mixed", MIXED_DATASET)
    monkeypatch.delenv("DATASOURCE_URL__AGAMI_EXAMPLE")

    code, payload, _ = _run(capsys, "--list")

    assert code == 0 and payload["datasets"][0]["total"] == 4


# ---------------------------------------------------------------------------
# A run — every item, in presentation order
# ---------------------------------------------------------------------------

def test_a_run_reports_every_item_and_the_summary_counts_them(artifacts, scripted, capsys):
    """Nothing is dropped between the dataset and the payload, and the summary describes exactly
    the items that were printed."""
    _write(artifacts, "mixed", MIXED_DATASET)

    code, payload, _ = _run(capsys, "--dataset", "mixed")

    sections = _sections(payload)
    assert code == 0
    assert payload["summary"]["total"] == len(payload["items"]) == 4
    assert payload["summary"]["passed"] == sections.count("pass")
    assert payload["summary"]["errored"] == sections.count("error")
    assert payload["summary"]["gating_failures"] == sections.count("failure")
    assert {item["item_key"] for item in payload["items"]} == {
        "orders-count", "payments-count", "products-count", "customers-count"
    }


def test_the_sections_are_emitted_failures_first(artifacts, scripted, capsys):
    """The ordering criterion, asserted on the sequence alone. The dataset lists its cases
    pass-first, so a run that simply echoed the file's order fails here."""
    _write(artifacts, "mixed", MIXED_DATASET)

    _, payload, _ = _run(capsys, "--dataset", "mixed")

    assert _sections(payload) == ["failure", "error", "unconfirmed", "pass"]


def test_an_all_passing_run_still_emits_a_full_payload(artifacts, scripted, capsys):
    """A green run is not an empty one — a reader has to be able to see what passed."""
    _write(artifacts, "green", PASSING_DATASET)

    _, payload, _ = _run(capsys, "--dataset", "green")

    assert _sections(payload) == ["pass"]
    assert payload["items"][0]["question"] == Q_PASS
    assert payload["items"][0]["accuracy"] == 1.0
    assert payload["summary"]["passed"] == 1 and payload["summary"]["gating_failures"] == 0


def test_an_unconfirmed_item_is_its_own_section_and_never_a_gating_failure(
    artifacts, scripted, capsys
):
    """The unconfirmed case in the mixed dataset scores a miss. It is reported in full, it is
    counted in `failed`, and it still cannot fail the run."""
    _write(artifacts, "mixed", MIXED_DATASET)

    _, payload, _ = _run(capsys, "--dataset", "mixed")

    unconfirmed = [item for item in payload["items"] if item["section"] == "unconfirmed"]
    assert [item["item_key"] for item in unconfirmed] == ["payments-count"]
    assert unconfirmed[0]["confirmed"] is False and unconfirmed[0]["passed"] is False
    # Two scored misses, only one of which a verdict may rest on.
    assert payload["summary"]["failed"] == 2 and payload["summary"]["gating_failures"] == 1


def test_the_summary_carries_completed_gating_failures_and_errored(artifacts, scripted, capsys):
    """The runner's docstring says a verdict reads all three, so all three are printed."""
    _write(artifacts, "mixed", MIXED_DATASET)

    _, payload, _ = _run(capsys, "--dataset", "mixed")

    assert payload["summary"]["completed"] is True
    assert payload["summary"]["gating_failures"] == 1
    assert payload["summary"]["errored"] == 1


def test_an_item_that_could_not_be_scored_is_counted_rather_than_dropped(
    artifacts, scripted, capsys
):
    """Two empty result sets agree about nothing, so the comparator scores neither side. The item
    still has to appear, and `unscored` has to say so — otherwise a dataset of such cases reads as
    a run with nothing wrong in it."""
    question = "How many orders are in a status nobody uses?"
    empty = "SELECT id FROM orders WHERE status = 'no-such-status'"
    _write(artifacts, "empty", {"test_cases": [
        {"id": "both-empty", "query": question,
         "expected": {"sql": empty, "sql_confirmed": True}},
    ]})
    GENERATED[question] = empty
    try:
        _, payload, _ = _run(capsys, "--dataset", "empty")
    finally:
        del GENERATED[question]

    assert payload["summary"]["unscored"] == 1
    assert [item["item_key"] for item in payload["items"]] == ["both-empty"]
    assert payload["items"][0]["status"] == "unscored"


# ---------------------------------------------------------------------------
# The rule the whole payload exists to keep
# ---------------------------------------------------------------------------

def test_stdout_carries_neither_statement(artifacts, scripted, capsys):
    """Neither the answer key nor the generated statement appears anywhere in what is printed —
    asserted over the serialized payload, so a field added later is covered without being named."""
    _write(artifacts, "mixed", MIXED_DATASET)

    _, payload, _ = _run(capsys, "--dataset", "mixed")

    rendered = json.dumps(payload)
    assert GOLDEN_SENTINEL not in rendered
    assert GENERATED_SENTINEL not in rendered


def test_the_artifact_lands_in_the_eval_dashboard_dir_with_both_statements(
    artifacts, scripted, capsys
):
    """What stdout withholds is on disk, joined per item, because the report slice renders the two
    statements side by side and `GoldenRunResult` carries neither the question nor the key."""
    _write(artifacts, "mixed", MIXED_DATASET)

    _, payload, _ = _run(capsys, "--dataset", "mixed")

    artifact = Path(payload["artifact"])
    assert artifact.parent == agami_paths.dashboard_dir("eval", PROFILE, artifacts)
    joined = json.loads(artifact.read_text(encoding="utf-8"))
    assert GOLDEN_SENTINEL in artifact.read_text(encoding="utf-8")
    assert GENERATED_SENTINEL in artifact.read_text(encoding="utf-8")
    assert joined["dataset"] == "mixed" and joined["profile"] == PROFILE
    assert joined["summary"] == payload["summary"]
    by_key = {item["item_key"]: item for item in joined["items"]}
    assert by_key["orders-count"]["question"] == Q_PASS
    assert by_key["orders-count"]["score"]["accuracy"] == 1.0


def test_what_the_reader_dropped_reaches_the_findings(artifacts, scripted, capsys):
    """A case too broken to read costs that case, and the run says so — otherwise the dataset
    quietly shrinks and the summary looks the same."""
    broken = dict(PASSING_DATASET)
    broken["test_cases"] = PASSING_DATASET["test_cases"] + [
        # No `sql_confirmed`, which the reader refuses rather than guessing.
        {"id": "unreadable", "query": "How many refunds were issued?", "expected": {}},
    ]
    _write(artifacts, "green", broken)

    _, payload, _ = _run(capsys, "--dataset", "green")

    assert payload["summary"]["total"] == 1
    assert any("unreadable" in (finding["locator"] or "") for finding in payload["findings"])


# ---------------------------------------------------------------------------
# Choosing the dataset
# ---------------------------------------------------------------------------

def test_a_single_dataset_needs_no_argument(artifacts, scripted, capsys):
    _write(artifacts, "green", PASSING_DATASET)

    code, payload, _ = _run(capsys)

    assert code == 0 and payload["summary"]["total"] == 1


def test_several_datasets_and_no_argument_stops_and_names_them(artifacts, scripted, capsys):
    """Asking the person which one is the skill's job, so the helper refuses rather than guessing."""
    _write(artifacts, "green", PASSING_DATASET)
    _write(artifacts, "mixed", MIXED_DATASET)

    code, payload, err = _run(capsys)

    assert code != 0 and payload == {}
    assert "green" in err and "mixed" in err


def test_naming_a_dataset_that_does_not_exist_lists_what_does(artifacts, scripted, capsys):
    _write(artifacts, "green", PASSING_DATASET)

    code, _, err = _run(capsys, "--dataset", "absent")

    assert code != 0 and "green" in err


def test_a_dataset_with_no_cases_runs_and_reports_nothing(artifacts, scripted, capsys):
    """An author who created the file but has not written a case yet. The reader calls that a
    dataset, so the run does too — an empty summary rather than a failure."""
    _write(artifacts, "blank", {"description": "nothing yet"})

    code, payload, _ = _run(capsys, "--dataset", "blank")

    assert code == 0 and payload["items"] == []
    assert payload["summary"]["total"] == 0 and payload["summary"]["completed"] is True


def test_a_dataset_whose_only_case_is_unconfirmed_gates_on_nothing(artifacts, scripted, capsys):
    """The state most profiles are actually in. It runs, it reports, and its verdict rests on
    nothing — which the summary has to make visible."""
    _write(artifacts, "draft", {"test_cases": [
        {"id": "payments-count", "query": Q_UNCONFIRMED,
         "expected": {"sql": "SELECT COUNT(*) AS n FROM payments", "sql_confirmed": False}},
    ]})

    _, payload, _ = _run(capsys, "--dataset", "draft")

    assert _sections(payload) == ["unconfirmed"]
    assert payload["summary"]["gating_failures"] == 0


# ---------------------------------------------------------------------------
# The two runs that are not green and do not look like failures
# ---------------------------------------------------------------------------

def test_a_generator_that_raises_stops_the_run_and_the_payload_says_so(
    artifacts, monkeypatch, capsys
):
    """`completed: false` and the cases after the raise simply absent. A reader who only counted
    failures would call this run clean."""

    class _RaisesOnTheSecond:
        def __init__(self, schema: str, *, timeout_s: float) -> None:
            self.answered = 0

        def generate(self, question, org, datasource):
            self.answered += 1
            if self.answered > 1:
                raise RuntimeError("the client fell over")
            return gr.GeneratedSql(sql=GENERATED[Q_PASS], error=None)

    monkeypatch.setattr(run_golden_eval, "ClaudeCliGenerator", _RaisesOnTheSecond)
    _write(artifacts, "mixed", MIXED_DATASET)

    code, payload, _ = _run(capsys, "--dataset", "mixed")

    assert code == 0
    assert payload["summary"]["completed"] is False
    assert [item["item_key"] for item in payload["items"]] == ["orders-count"]
    assert payload["summary"]["total"] == 1


def test_a_run_where_every_item_errors_is_not_green(artifacts, monkeypatch, capsys):
    """Zero gating failures, and nothing was answered. The counter is over items that were SCORED,
    so `errored` is the only field that distinguishes this from a clean run."""

    class _AnswersNothing:
        def __init__(self, schema: str, *, timeout_s: float) -> None:
            pass

        def generate(self, question, org, datasource):
            return gr.GeneratedSql(sql="", error="no statement was scripted for this question")

    monkeypatch.setattr(run_golden_eval, "ClaudeCliGenerator", _AnswersNothing)
    _write(artifacts, "mixed", MIXED_DATASET)

    _, payload, _ = _run(capsys, "--dataset", "mixed")

    assert payload["summary"]["gating_failures"] == 0
    assert payload["summary"]["errored"] == 4 and payload["summary"]["completed"] is True
    assert _sections(payload) == ["error", "error", "error", "error"]


# ---------------------------------------------------------------------------
# The one raise on the run path
# ---------------------------------------------------------------------------

def test_a_datasource_with_no_storage_connection_fails_preflight(artifacts, scripted, capsys):
    """`resolve_datasource_dialect` is the only unguarded raise the run path reaches, so it is
    reported as a preflight failure rather than as a traceback."""
    _write(artifacts, "green", PASSING_DATASET)
    path = artifacts / PROFILE / "datasource.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["storage_connections"] = []
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")

    code, _, err = _run(capsys, "--dataset", "green")

    assert code != 0
    assert "storage connection" in err and "Traceback" not in err


def test_the_generator_is_handed_the_tables_and_the_timeout(artifacts, monkeypatch, capsys):
    """The schema is this helper's own to build — nothing upstream renders one — so what reaches
    the generator is asserted here or nowhere."""
    seen: dict[str, Any] = {}

    class _Records(_Scripted):
        def __init__(self, schema: str, *, timeout_s: float) -> None:
            super().__init__(schema, timeout_s=timeout_s)
            seen["schema"] = schema
            seen["timeout_s"] = timeout_s

    monkeypatch.setattr(run_golden_eval, "ClaudeCliGenerator", _Records)
    _write(artifacts, "green", PASSING_DATASET)

    _run(capsys, "--dataset", "green", "--timeout-s", "7")

    assert seen["timeout_s"] == 7.0
    lines = seen["schema"].splitlines()
    # Every table in the sample store, once each, rendered as `name(col TYPE, …)`.
    assert len(lines) == len(set(lines)) == 11
    assert any(line.startswith("orders(") and "status string" in line for line in lines)
