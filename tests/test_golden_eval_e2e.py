"""A golden run end to end: a dataset on disk, a real model, a real database, real rows.

Every other test of the runner stubs something. This one stubs only the model — the part that would
cost a token — and runs everything else for real: the reader parses a dataset file, both statements
go through `execute_guarded` against the shipped sample store database, the comparator scores what
SQLite actually returned, and the statement comparator reads both statements in SQLite's own
grammar.

That is what makes it worth its runtime. A unit test asserts each seam in isolation and would still
pass if two of them disagreed about the shape they exchange — a match level the comparator names
differently from the reader, a dialect the claim reader cannot parse, a score that does not survive
`json.dumps`. This one fails instead.

The sample store is synthetic and ships with this repository, so nothing here names real data.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

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
import execute_sql  # noqa: E402
from semantic_model import golden_run as gr  # noqa: E402
from semantic_model import loader as L  # noqa: E402
from semantic_model.golden import load_golden_datasets  # noqa: E402
from semantic_model.sql_dialect import resolve_datasource_dialect  # noqa: E402

PROFILE = "agami-example"
ORG = "demo"

# The dataset, as an author would write it. Four cases covering the four things a run has to get
# right: an equivalent statement passes, a different question fails, a required filter left out
# fails a statement whose numbers agree, and a case with a band and no answer key is judged on its
# own terms.
COUNT_QUESTION = "How many orders have been placed?"
BY_STATUS_QUESTION = "How many orders are there in each status?"
SCOPED_QUESTION = "How many orders are there, by status?"
BANDED_QUESTION = "Roughly how many orders are on file?"

GENERATED = {
    # Aliased differently and counting a column rather than the rows — the same answer, spelled
    # another way, which is the case a name-based or position-based comparison would fail.
    COUNT_QUESTION: "SELECT COUNT(id) AS n FROM orders",
    # The columns come back in the other order. Pairing is by value, so this still answers it.
    BY_STATUS_QUESTION: "SELECT COUNT(*) AS n, status FROM orders GROUP BY status ORDER BY status",
    # Right shape, and it answers a narrower question than the one that was asked.
    SCOPED_QUESTION: "SELECT status, COUNT(*) AS n FROM orders GROUP BY status ORDER BY status",
    BANDED_QUESTION: "SELECT COUNT(*) AS n FROM orders",
}

DATASET = {
    "description": "Order questions over the sample store.",
    "test_cases": [
        {
            "id": "orders-count",
            "query": COUNT_QUESTION,
            "expected": {
                "sql": "SELECT COUNT(*) AS order_count FROM orders",
                "sql_confirmed": True,
            },
        },
        {
            "id": "orders-by-status",
            "query": BY_STATUS_QUESTION,
            "expected": {
                "sql": (
                    "SELECT status, COUNT(*) AS order_count FROM orders "
                    "GROUP BY status ORDER BY status"
                ),
                "sql_confirmed": True,
            },
        },
        {
            "id": "orders-by-status-scoped",
            "query": SCOPED_QUESTION,
            # The dataset requires the channel to be constrained, and the generated statement
            # constrains nothing. Its rows will agree with the answer key's all the same.
            "must_filter": ["channel"],
            "expected": {
                "sql": (
                    "SELECT status, COUNT(*) AS order_count FROM orders "
                    "GROUP BY status ORDER BY status"
                ),
                "sql_confirmed": True,
            },
        },
        {
            "id": "orders-roughly",
            "query": BANDED_QUESTION,
            "match": "bounded",
            "bounds": {"min_value": 1000, "max_value": 10000},
            # No answer key at all, which `golden.py` allows for an unconfirmed case.
            "expected": {"sql_confirmed": False},
        },
    ],
}


class _ScriptedGenerator:
    """The model's stand-in: a fixed statement per question, and a record of what it was asked.

    Scripted rather than live for the obvious reason — a run in CI must not spend a token — and for
    a second one: the point of this file is that the other five components agree, and a live model
    would make a failure ambiguous between them and the answer it happened to write.
    """

    def __init__(self) -> None:
        self.asked: list[str] = []

    def generate(self, question: str, org: str, datasource: str | None) -> gr.GeneratedSql:
        self.asked.append(question)
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
def profile(tmp_path, monkeypatch, warehouse):
    """A profile wired the way the onboarding path wires one: the sample model, the sample database
    and a golden dataset beside them."""
    artifacts = tmp_path / "artifacts"
    shutil.copytree(SAMPLE_DIR / "model", artifacts / PROFILE)
    golden = artifacts / PROFILE / "golden_datasets"
    golden.mkdir()
    (golden / "orders.yaml").write_text(yaml.safe_dump(DATASET), encoding="utf-8")

    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(artifacts))
    monkeypatch.setenv("DATASOURCE_URL__AGAMI_EXAMPLE", f"sqlite:///{warehouse}")
    for var in ("AGAMI_DB_URL", "APP_DATABASE_URL", "AGAMI_ORG_ID", "AGAMI_SQL_TIMEOUT_S"):
        monkeypatch.delenv(var, raising=False)
    return artifacts


def _run(profile_dir: Path, generator=None) -> gr.GoldenRunResult:
    """Read the dataset off disk and run it, exactly as a caller would."""
    datasets, findings = load_golden_datasets(PROFILE)
    assert [dataset.name for dataset in datasets] == ["orders"]
    org = L.load_datasource(profile_dir / PROFILE)
    return gr.run_golden_dataset(
        datasets[0],
        profile=PROFILE,
        generator=generator or _ScriptedGenerator(),
        executor=execute_sql.BUILTIN_EXECUTOR,
        org=ORG,
        datasource=PROFILE,
        dialect=resolve_datasource_dialect(org),
        findings=findings.findings,
    )


def _by_key(result: gr.GoldenRunResult) -> dict[str, gr.ItemOutcome]:
    return {outcome.item_key: outcome for outcome in result.outcomes}


def test_a_dataset_read_off_disk_runs_against_the_real_database(profile):
    """The whole path in one assertion: reader, runner, chokepoint, driver, comparator.

    The row counts are the sample store's own frozen numbers, so a score of 1.0 here means SQLite
    really ran both statements and really returned 4000 orders — not that two empty results agreed.
    """
    result = _run(profile)

    outcomes = _by_key(result)
    assert result.completed and len(result.outcomes) == 4
    assert outcomes["orders-count"].passed
    assert outcomes["orders-count"].score.golden_row_count == 1
    assert outcomes["orders-by-status"].passed
    # Six statuses in the sample store, one row each, both sides.
    assert outcomes["orders-by-status"].score.generated_row_count == 6


def test_a_required_filter_left_out_fails_a_statement_whose_numbers_agree(profile):
    """The rows are identical and the statement still did not answer the question the dataset
    requires. Only a run over a real database can show those two facts at once."""
    outcome = _by_key(_run(profile))["orders-by-status-scoped"]

    assert outcome.score.accuracy == 1.0
    assert outcome.gated is True and outcome.passed is False
    assert [gate["kind"] for gate in outcome.claims["gates"]] == ["must_filter"]
    assert [gate["column"] for gate in outcome.claims["gates"]] == ["channel"]


def test_a_case_with_a_band_and_no_answer_key_is_judged_on_its_own(profile):
    """A bounded case has no answer key by design, so the run executes one statement for it and
    judges the number that came back against the band the author wrote."""
    outcome = _by_key(_run(profile))["orders-roughly"]

    assert outcome.passed and outcome.confirmed is False
    assert outcome.claims is None  # there is no second statement to compare against


def test_an_unanswered_question_errors_without_costing_the_rest_of_the_run(profile):
    """One case the generator has no answer for is one case lost, not a run lost."""

    class _AnswersNothing:
        def generate(self, question, org, datasource):
            return gr.GeneratedSql(sql="", error="no statement was scripted for this question")

    result = _run(profile, _AnswersNothing())

    assert result.completed and result.errored == 4 and result.passed == 0


def test_the_run_summary_is_the_same_on_a_second_run(profile):
    """Determinism, end to end. Two runs over one dataset agree on every count; only the run's own
    id moves, which is the one thing that is supposed to."""
    first, second = _run(profile), _run(profile)

    assert first.as_dict()["summary"] == second.as_dict()["summary"]
    assert first.as_dict()["summary"] == {
        "total": 4, "passed": 3, "failed": 1, "unscored": 0, "errored": 0, "gating_failures": 1,
    }
    assert first.run_id != second.run_id


def test_the_run_is_json_and_carries_no_filesystem_path(profile, tmp_path):
    """What a run hands back is persisted and rendered elsewhere, so it has to survive the trip —
    and it must not carry where it read the dataset from, which `golden.py` never discloses."""
    rendered = json.dumps(_run(profile).as_dict())

    assert str(tmp_path) not in rendered
    assert "golden_datasets" not in rendered
    assert json.loads(rendered)["profile"] == PROFILE
