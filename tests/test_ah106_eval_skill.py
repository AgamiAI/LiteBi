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
import re
import shutil
import subprocess
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
Q_UNSCORED = "How many orders are in a status nobody uses?"
Q_TYPED = "What does the first order carry in the column the answer key picked?"

# A predicate the seed matches nothing on, so both sides come back empty and the comparator has
# nothing to compare. Both statements are the same one: the point is an item nobody can judge, not
# a disagreement.
EMPTY_SQL = "SELECT id FROM orders WHERE status = 'no-such-status'"

GENERATED = {
    Q_PASS: f"SELECT COUNT(id) AS {GENERATED_SENTINEL} FROM orders",
    # A different number from the same table, so the failure is a real disagreement over rows
    # rather than an accident of which table happens to be larger.
    Q_FAIL: "SELECT COUNT(DISTINCT country) AS n FROM customers",
    Q_UNCONFIRMED: "SELECT COUNT(*) AS n FROM refunds",
    Q_UNSCORED: EMPTY_SQL,
    # A text column where the answer key selected a numeric one, which is the one disagreement the
    # `shape` level can see — and the only reason it builds that names a column.
    Q_TYPED: "SELECT status FROM orders LIMIT 1",
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

# Confirmed, so nothing about its answer key excuses it — and still not a failure, because nothing
# was compared.
UNSCORED_CASE: dict[str, Any] = {
    "id": "unused-status",
    "query": Q_UNSCORED,
    "expected": {"sql": EMPTY_SQL, "sql_confirmed": True},
}

# The mixed dataset plus the case nothing can be judged for, so one run exercises all five
# sections. Kept apart from MIXED_DATASET because the counts asserted against that one are its own,
# and its unscored case is written last so the ordering assertion has something to reorder.
ALL_SECTIONS_DATASET: dict[str, Any] = {
    "test_cases": [*MIXED_DATASET["test_cases"], UNSCORED_CASE],
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


# The three datasets whose answer keys alias a column with the sentinel AND then miss, so the
# comparator builds a reason out of that alias. A passing case cannot expose this: the reasons that
# name a column are only ever built on a disagreement.
ONE_UNMATCHED_DATASET: dict[str, Any] = {
    "test_cases": [
        {
            "id": "customers-count",
            "query": Q_FAIL,
            "expected": {
                "sql": f"SELECT COUNT(*) AS {GOLDEN_SENTINEL} FROM customers",
                "sql_confirmed": True,
            },
        },
    ],
}

TWO_UNMATCHED_DATASET: dict[str, Any] = {
    "test_cases": [
        {
            "id": "customers-count",
            "query": Q_FAIL,
            "expected": {
                "sql": (
                    f"SELECT COUNT(*) AS {GOLDEN_SENTINEL}, "
                    f"COUNT(DISTINCT id) AS {GOLDEN_SENTINEL}2 FROM customers"
                ),
                "sql_confirmed": True,
            },
        },
    ],
}

TYPE_MISMATCH_DATASET: dict[str, Any] = {
    "test_cases": [
        {
            "id": "first-order",
            "query": Q_TYPED,
            "match": "shape",
            "expected": {
                "sql": f"SELECT id AS {GOLDEN_SENTINEL} FROM orders LIMIT 1",
                "sql_confirmed": True,
            },
        },
    ],
}

# Right rows, missing filter. The only shape in which an item scores 1.0 and still does not pass,
# and the only one that puts anything in `gates`.
GATED_DATASET: dict[str, Any] = {
    "test_cases": [
        {
            "id": "orders-count",
            "query": Q_PASS,
            "must_filter": ["status"],
            "expected": {"sql": "SELECT COUNT(*) AS n FROM orders", "sql_confirmed": True},
        },
    ],
}

# A `sql:` value carrying an unquoted `: `, which YAML reads as a nested mapping and refuses —
# and PyYAML quotes the offending source line back inside the error it raises. Written as text
# rather than dumped, because a dumper would quote it and there would be no error.
UNPARSEABLE_TEXT = f"""test_cases:
  - id: broken
    query: How many orders were placed?
    expected:
      sql: SELECT COUNT(*) AS {GOLDEN_SENTINEL} FROM orders WHERE note: 'x'
      sql_confirmed: true
"""


class _Scripted:
    """The shipped generator's stand-in, constructed the same way the script constructs the real
    one — so a change to that call site fails here rather than silently bypassing the schema."""

    def __init__(self, schema, *, timeout_s: float) -> None:
        self.schema = schema
        self.timeout_s = timeout_s

    def generate(self, question: str, org: str, datasource: Optional[str]) -> gr.GeneratedSql:
        # Resolved and discarded, because the real generator resolves it per question and the
        # ranking it triggers is the observable this file asserts on. A stand-in that skipped it
        # would leave the per-question fetch untested while every test still passed.
        self.schema(question) if callable(self.schema) else self.schema
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
def scripted(monkeypatch, sm) -> None:
    """Answer every question from a table instead of spawning the operator's client.

    Takes `sm` too: a run that scripts the generator still builds a context first, and letting that
    reach the real CLI would spend a second per call to receive a fixture back.
    """
    monkeypatch.setattr(run_golden_eval, "ClaudeCliGenerator", _Scripted)


class _RecordedSm:
    """Stand in for the `sm` CLI, and record what was asked of it.

    Stubbed rather than run for the reason every other subprocess in this suite is stubbed: a real
    call starts an interpreter, and at about a second each that is most of this file's runtime. The
    payloads are the shapes `cli.py` documents, small enough to read here.
    """

    AREAS = [{"name": "sales"}, {"name": "ops"}]
    BUNDLES = {
        "sales": {
            "tables": {
                "orders": {
                    "name": "orders",
                    "schema": "main",
                    "columns": [{"name": "status", "type": "string", "description": "paid or not"}],
                }
            },
            "metrics": [
                {"name": "revenue", "other_names": ["sales"], "bindings": {"SQLite": "SUM(total)"}}
            ],
            "entities": [
                {
                    "name": "order",
                    "plural": "orders",
                    "maps_to": [{"table": "orders", "column": "id"}],
                }
            ],
            "relationships": [
                {
                    "from_table": "orders",
                    "to_table": "customers",
                    "from_column": "customer_id",
                    "to_column": "id",
                    "relationship": "many_to_one",
                }
            ],
        },
        "ops": {"tables": {}, "metrics": [], "entities": [], "relationships": []},
    }

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        # argv is ["bash", "<path>/sm", <subcommand>, ...]
        self.calls.append(list(argv[2:]))
        sub = argv[2]
        if sub == "areas":
            out = json.dumps(self.AREAS)
        elif sub == "bundle":
            out = json.dumps(self.BUNDLES[argv[argv.index("--area") + 1]])
        elif sub == "org-context":
            out = "# demo\nCustInvc -- a customer invoice"
        elif sub == "examples":
            asked = argv[argv.index("--query") + 1]
            out = json.dumps(
                {
                    "high_confidence": False,
                    "matches": [
                        {"score": 0.5, "example": {"question": f"like: {asked}", "sql": "SELECT 1"}}
                    ],
                }
            )
        else:  # pragma: no cover — a subcommand this helper does not call
            raise AssertionError(f"unexpected sm subcommand {sub!r}")
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    def subcommands(self) -> list[str]:
        return [call[0] for call in self.calls]


@pytest.fixture()
def sm(monkeypatch) -> _RecordedSm:
    recorder = _RecordedSm()
    monkeypatch.setattr(run_golden_eval.subprocess, "run", recorder)
    return recorder


def _write(art: Path, name: str, dataset: dict[str, Any]) -> None:
    _write_text(art, name, yaml.safe_dump(dataset))


def _write_text(art: Path, name: str, text: str) -> None:
    """The same, for a file that has to reach the reader exactly as written — a dumper would quote
    its way out of the syntax error the test is about."""
    golden = art / PROFILE / "golden_datasets"
    golden.mkdir(exist_ok=True)
    (golden / f"{name}.yaml").write_text(text, encoding="utf-8")


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

    assert payload["datasets"] == [{"name": "mixed", "total": 4, "confirmed": 3, "unconfirmed": 1}]


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
    the items that were printed.

    The dataset carries one confirmed miss, so the run exits `1` — the payload is the point here
    and it prints in full either way, which is the reason the verdict is computed after it."""
    _write(artifacts, "mixed", MIXED_DATASET)

    code, payload, _ = _run(capsys, "--dataset", "mixed")

    sections = _sections(payload)
    assert code == 1
    assert payload["summary"]["total"] == len(payload["items"]) == 4
    assert payload["summary"]["passed"] == sections.count("pass")
    assert payload["summary"]["errored"] == sections.count("error")
    assert payload["summary"]["gating_failures"] == sections.count("failure")
    assert {item["item_key"] for item in payload["items"]} == {
        "orders-count",
        "payments-count",
        "products-count",
        "customers-count",
    }


def test_the_sections_are_emitted_failures_first(artifacts, scripted, capsys):
    """The ordering criterion, asserted on the sequence alone. The dataset lists its cases
    pass-first, so a run that simply echoed the file's order fails here."""
    _write(artifacts, "all-five", ALL_SECTIONS_DATASET)

    _, payload, _ = _run(capsys, "--dataset", "all-five")

    assert _sections(payload) == ["failure", "error", "unscored", "unconfirmed", "pass"]


def test_the_section_counts_describe_the_rows_that_were_printed(artifacts, scripted, capsys):
    """The summary's own headline counts, so what is claimed and what is rendered cannot disagree.

    They are not the runner's counters and this run shows why: the unconfirmed miss counts in
    `failed` while its row sits under `unconfirmed`, and the unscored item counts in neither
    `failed` nor `gating_failures` while still occupying a row."""
    _write(artifacts, "all-five", ALL_SECTIONS_DATASET)

    _, payload, _ = _run(capsys, "--dataset", "all-five")

    sections = _sections(payload)
    assert payload["summary"]["sections"] == {
        section: sections.count(section)
        for section in ("failure", "error", "unscored", "unconfirmed", "pass")
    }
    assert sum(payload["summary"]["sections"].values()) == payload["summary"]["total"] == 5
    assert payload["summary"]["failed"] == 2 and payload["summary"]["sections"]["failure"] == 1


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
    a run with nothing wrong in it.

    It is confirmed and it did not pass, and it is still not a failure: nothing was compared, so it
    is its own category. A run that filed it under failures would report `failed: 0` above a list
    with a row in it."""
    _write(artifacts, "empty", {"test_cases": [UNSCORED_CASE]})

    _, payload, _ = _run(capsys, "--dataset", "empty")

    assert payload["summary"]["unscored"] == 1
    assert [item["item_key"] for item in payload["items"]] == ["unused-status"]
    assert payload["items"][0]["status"] == "unscored"
    assert payload["items"][0]["confirmed"] is True and payload["items"][0]["passed"] is False
    assert _sections(payload) == ["unscored"]
    assert payload["summary"]["failed"] == 0 and payload["summary"]["gating_failures"] == 0


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


def test_a_mismatch_counts_the_answer_keys_columns_rather_than_naming_them(
    artifacts, scripted, capsys
):
    """`reason` is the one field that reaches the chat table, and the comparator builds this one
    out of the aliases the author wrote in `expected.sql`. A count says the same thing — the answer
    key asked for something the result does not carry — and carries no identifier with it.

    The names are not lost: the artifact keeps the score whole, which is where a drill-down reads
    them from."""
    _write(artifacts, "one-unmatched", ONE_UNMATCHED_DATASET)
    _write(artifacts, "two-unmatched", TWO_UNMATCHED_DATASET)

    _, one, _ = _run(capsys, "--dataset", "one-unmatched")
    _, two, _ = _run(capsys, "--dataset", "two-unmatched")

    assert GOLDEN_SENTINEL not in json.dumps(one) + json.dumps(two)
    assert one["items"][0]["section"] == "failure"
    assert one["items"][0]["reason"] == (
        "1 of the answer key's columns has no counterpart in the generated result"
    )
    assert two["items"][0]["reason"] == (
        "2 of the answer key's columns have no counterpart in the generated result"
    )
    joined = json.loads(Path(one["artifact"]).read_text(encoding="utf-8"))
    assert joined["items"][0]["score"]["unmatched_golden_columns"] == [GOLDEN_SENTINEL]


def test_a_type_mismatch_does_not_name_the_answer_keys_column(artifacts, scripted, capsys):
    """The `shape` level's own reason is the second one built from an alias. Same rule, and the
    same surface — nothing about `shape` makes a name safer to print than `exact` does."""
    _write(artifacts, "typed", TYPE_MISMATCH_DATASET)

    _, payload, _ = _run(capsys, "--dataset", "typed")

    assert GOLDEN_SENTINEL not in json.dumps(payload)
    assert payload["items"][0]["section"] == "failure"
    assert payload["items"][0]["reason"] == ("a column does not carry the type the answer key does")


def test_a_dataset_that_does_not_parse_does_not_relay_the_offending_line(artifacts, capsys):
    """PyYAML quotes ~75 characters of the source line back inside its error, so a `sql:` value
    with an unquoted colon in it puts a fragment of the answer key on a surface that promises no
    SQL. Only the first line of a finding is relayed; `code` and `locator` stay whole, because
    those are what tells a dataset breakage apart from a scored failure."""
    _write_text(artifacts, "broken", UNPARSEABLE_TEXT)

    _, payload, _ = _run(capsys, "--list")

    assert GOLDEN_SENTINEL not in json.dumps(payload)
    finding = payload["findings"][0]
    assert finding["code"] == "golden_unreadable_file"
    assert finding["locator"] == "broken.yaml"
    assert "\n" not in finding["message"] and "broken.yaml" in finding["message"]


def test_a_required_filter_the_statement_never_wrote_fails_a_perfect_score(
    artifacts, scripted, capsys
):
    """`gates` is the only nested structure another module puts on this payload, and this is the
    only shape that fills it: the rows are right, the required filter is absent, and the item does
    not pass at an accuracy of 1.0."""
    _write(artifacts, "gated", GATED_DATASET)

    _, payload, _ = _run(capsys, "--dataset", "gated")

    item = payload["items"][0]
    assert item["section"] == "failure"
    assert item["accuracy"] == 1.0 and item["passed"] is False and item["gated"] is True
    assert [gate["kind"] for gate in item["gates"]] == ["must_filter"]
    assert item["gates"][0]["column"] == "status"


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


def test_the_artifact_carries_what_the_report_renders(artifacts, scripted, capsys):
    """The verdict fields and the claim difference, per item.

    The report renderer is stdlib-only — it cannot parse a statement — so the table-set delta it
    prints above the two statements has no input unless the run writes the difference down here.
    The same goes for `confirmed` / `passed` / `gated` / `section`: they are the verdict, and
    re-deriving any of them at render time would be a second definition of one."""
    _write(artifacts, "mixed", MIXED_DATASET)

    _, payload, _ = _run(capsys, "--dataset", "mixed")

    joined = json.loads(Path(payload["artifact"]).read_text(encoding="utf-8"))
    by_key = {item["item_key"]: item for item in joined["items"]}
    passing = by_key["orders-count"]
    assert passing["section"] == "pass"
    assert passing["confirmed"] is True and passing["passed"] is True and passing["gated"] is False
    # The tables claim is the first of the seven, and it is the one the report renders.
    assert passing["claims"]["claims"][0] == {
        "name": "tables", "status": "agrees", "generated": ["orders"], "golden": ["orders"],
    }
    # A run that never got a statement has no claim difference at all, so the report has to render
    # the absence rather than assume the key is there.
    assert by_key["products-count"]["section"] == "error"
    assert by_key["products-count"]["claims"] is None
    assert by_key["payments-count"]["section"] == "unconfirmed"
    assert by_key["customers-count"]["section"] == "failure"


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
    """Also the exit-0 anchor: an all-pass run over one confirmed case is the only shape that
    is green on every one of the verdict's checks."""
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
    dataset, so the run does too — an empty summary rather than a failure.

    It exits `0` because the run worked, and it says on stderr that it gated on nothing, because a
    green exit over zero confirmed cases is the one most easily mistaken for evidence."""
    _write(artifacts, "blank", {"description": "nothing yet"})

    code, payload, err = _run(capsys, "--dataset", "blank")

    assert code == 0 and payload["items"] == []
    assert payload["summary"]["total"] == 0 and payload["summary"]["completed"] is True
    assert "gated on nothing" in err


def test_a_dataset_whose_only_case_is_unconfirmed_gates_on_nothing(artifacts, scripted, capsys):
    """The state most profiles are actually in. It runs, it reports, and its verdict rests on
    nothing — which the summary has to make visible."""
    _write(
        artifacts,
        "draft",
        {
            "test_cases": [
                {
                    "id": "payments-count",
                    "query": Q_UNCONFIRMED,
                    "expected": {
                        "sql": "SELECT COUNT(*) AS n FROM payments",
                        "sql_confirmed": False,
                    },
                },
            ]
        },
    )

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
    failures would call this run clean, which is why the exit code refuses to give a verdict at
    all rather than reporting the fraction of the dataset it reached."""

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

    assert code == 2
    assert payload["summary"]["completed"] is False
    assert [item["item_key"] for item in payload["items"]] == ["orders-count"]
    assert payload["summary"]["total"] == 1


def test_a_run_where_every_item_errors_is_not_green(artifacts, monkeypatch, capsys):
    """Zero gating failures, and nothing was answered. The counter is over items that were SCORED,
    so `errored` is the only field that distinguishes this from a clean run — and the exit code
    reads it, which is what keeps a runner with no `claude` on it from passing CI."""

    class _AnswersNothing:
        def __init__(self, schema: str, *, timeout_s: float) -> None:
            pass

        def generate(self, question, org, datasource):
            return gr.GeneratedSql(sql="", error="no statement was scripted for this question")

    monkeypatch.setattr(run_golden_eval, "ClaudeCliGenerator", _AnswersNothing)
    _write(artifacts, "mixed", MIXED_DATASET)

    code, payload, _ = _run(capsys, "--dataset", "mixed")

    assert code == 2
    assert payload["summary"]["gating_failures"] == 0
    assert payload["summary"]["errored"] == 4 and payload["summary"]["completed"] is True
    assert _sections(payload) == ["error", "error", "error", "error"]


# ---------------------------------------------------------------------------
# The one raise on the run path
# ---------------------------------------------------------------------------


def test_the_artifact_write_failing_does_not_discard_the_run(artifacts, scripted, capsys):
    """A run costs a model call and two warehouse queries per case. A directory it cannot write to
    loses the drill-down and nothing else — the verdicts still print, the exit code is still the
    dataset's own (`0` here: one confirmed case, and it passed), and the path is not relayed onto
    stderr. A missing artifact is not a broken run, so it does not become a `2`."""
    _write(artifacts, "green", PASSING_DATASET)
    out = agami_paths.dashboard_dir("eval", PROFILE, artifacts)
    out.mkdir(parents=True)
    out.chmod(0o500)
    try:
        code, payload, err = _run(capsys, "--dataset", "green")
    finally:
        out.chmod(0o700)

    assert code == 0
    assert _sections(payload) == ["pass"] and payload["summary"]["passed"] == 1
    assert payload["artifact"] == ""
    assert err.startswith("agami-eval:") and "Traceback" not in err
    assert str(artifacts) not in err


def test_a_profile_whose_model_cannot_be_read_stops_without_a_traceback(
    artifacts, scripted, capsys
):
    """The model load sits on the run path too, and a profile that was never connected raises
    straight through it — printing the absolute artifacts path, which every other refusal on this
    path withholds."""
    _write(artifacts, "green", PASSING_DATASET)
    (artifacts / PROFILE / "datasource.yaml").unlink()

    code, payload, err = _run(capsys, "--dataset", "green")

    assert code != 0 and payload == {}
    assert err.startswith("agami-eval:") and "Traceback" not in err
    assert str(artifacts) not in err


def test_a_model_that_does_not_parse_stops_without_a_traceback(artifacts, scripted, capsys):
    """The second way the load fails, and the one whose exception text quotes the offending value
    back — so the count of problems is relayed and the values are not."""
    _write(artifacts, "green", PASSING_DATASET)
    path = artifacts / PROFILE / "datasource.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["description"] = 5
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")

    code, payload, err = _run(capsys, "--dataset", "green")

    assert code != 0 and payload == {}
    assert err.startswith("agami-eval:") and "Traceback" not in err
    assert "input_value" not in err


def test_a_datasource_with_no_storage_connection_fails_preflight(artifacts, scripted, capsys):
    """`resolve_datasource_dialect` is one of three raises the run path reaches, so it is reported
    as a preflight failure rather than as a traceback."""
    _write(artifacts, "green", PASSING_DATASET)
    path = artifacts / PROFILE / "datasource.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["storage_connections"] = []
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")

    code, _, err = _run(capsys, "--dataset", "green")

    assert code != 0
    assert "storage connection" in err and "Traceback" not in err


def test_the_generator_is_handed_every_section_and_the_timeout(artifacts, monkeypatch, sm, capsys):
    """What reaches the generator is asserted here or nowhere: it is assembled in this helper and
    handed straight to a subprocess.

    The schema arrives as a CALLABLE now, because the examples are ranked per question. Resolving it
    is what the real generator does per item."""
    seen: dict[str, Any] = {}

    class _Records(_Scripted):
        def __init__(self, schema, *, timeout_s: float) -> None:
            super().__init__(schema, timeout_s=timeout_s)
            seen["schema"] = schema
            seen["timeout_s"] = timeout_s

    monkeypatch.setattr(run_golden_eval, "ClaudeCliGenerator", _Records)
    _write(artifacts, "green", PASSING_DATASET)

    _run(capsys, "--dataset", "green", "--timeout-s", "7")

    assert seen["timeout_s"] == 7.0
    context = seen["schema"]("How many orders?")
    # The first blank-line-delimited section is the vocabulary; the headings introduce the rest.
    assert context.split("\n\n")[0] == "main.orders(status string -- paid or not)"
    assert "CustInvc -- a customer invoice" in context  # org-context, verbatim
    assert "revenue -- (also: sales) -- SQL: SUM(total)" in context  # a metric's binding
    assert "order -- (also: orders) -- maps to orders.id" in context  # an entity
    assert "orders -> customers -- on orders.customer_id = customers.id" in context  # a join
    assert "many_to_one" in context  # …with the cardinality that decides fan-out


def test_the_examples_are_ranked_for_the_question_being_asked(artifacts, scripted, sm, capsys):
    """The reason for shelling out at all: `sm examples --query` is the product's own ranker, so the
    eval sends the handful nearest THIS question rather than the whole library to every item."""
    _write(artifacts, "green", PASSING_DATASET)

    _run(capsys, "--dataset", "green")

    example_calls = [call for call in sm.calls if call[0] == "examples"]
    assert {call[call.index("--query") + 1] for call in example_calls} == {
        item["query"] for item in PASSING_DATASET["test_cases"]
    }
    assert all("--top-k" in call for call in example_calls)


def test_every_area_is_ranked_rather_than_whichever_sorted_first(artifacts, scripted, sm, capsys):
    """`sm examples` reads ONE area's library, and an eval is not told which area a question belongs
    to. Ranking only the first would hand a question the wrong library entirely — on a profile whose
    areas are asset, change, incident … an incident question got asset examples."""
    _write(artifacts, "green", PASSING_DATASET)

    _run(capsys, "--dataset", "green")

    asked = [call[call.index("--area") + 1] for call in sm.calls if call[0] == "examples"]
    assert set(asked) == {area["name"] for area in _RecordedSm.AREAS}


def test_the_question_independent_context_is_fetched_once_for_the_whole_run(
    artifacts, scripted, sm, capsys
):
    """Each `sm` call starts an interpreter. Only the ranking depends on the question, so the rest
    is fetched once — a per-item fetch would spend a second an item to receive the same bytes."""
    _write(artifacts, "green", PASSING_DATASET)

    _run(capsys, "--dataset", "green")

    subcommands = sm.subcommands()
    assert subcommands.count("areas") == 1
    assert subcommands.count("org-context") == 1
    assert subcommands.count("bundle") == len(_RecordedSm.AREAS)
    # The ranking is the one call the question changes, and it reads one area's library at a time.
    assert subcommands.count("examples") == len(PASSING_DATASET["test_cases"]) * len(
        _RecordedSm.AREAS
    )


def test_no_refusal_prints_the_artifacts_path(artifacts, scripted, capsys):
    """Every refusal here withholds the artifacts directory, because on a hosted deployment the
    path encodes the tenant. The no-datasets one quoted it in full while saying where to put the
    first file; `--list` is where a caller asks for the resolved path instead."""
    code, _, err = _run(capsys)  # no datasets written, so this is the empty-profile refusal

    assert code == run_golden_eval._CANNOT_START
    assert "golden_datasets" in err  # still says WHERE, in relative terms
    assert str(artifacts) not in err  # …and never the absolute path


def test_a_context_that_cannot_be_built_stops_the_run_before_the_first_item(
    artifacts, scripted, monkeypatch, capsys
):
    """A run whose context failed has no generator, and reporting that as every item erroring would
    read as a model regression rather than as the wiring fault it is."""

    def _fails(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 3, stdout="", stderr="")

    monkeypatch.setattr(run_golden_eval.subprocess, "run", _fails)
    _write(artifacts, "green", PASSING_DATASET)

    code, payload, err = _run(capsys, "--dataset", "green")

    assert code == run_golden_eval._CANNOT_START
    assert payload == {}
    assert "model context" in err and "Traceback" not in err
    assert str(artifacts) not in err  # the path encodes the tenant on a hosted deployment


def test_the_schema_qualifies_every_table_that_declares_one():
    """The rendered schema is the entire vocabulary the generator gets, so a table rendered bare is
    a statement written bare — which on a profile whose schema is not on the connection's search
    path errors on every item and reads as a model regression. The sqlite sample cannot show it,
    so this is asserted over the rendering alone.

    Qualifying also fixes the dedup: two same-named tables in two schemas are two tables, and a
    key on the bare name silently kept one of them."""
    bundles = {
        "billing": {
            "tables": {
                "invoices": {
                    "name": "invoices",
                    "schema": "billing",
                    "columns": [{"name": "id", "type": "integer"}],
                }
            }
        },
        "reporting": {
            "tables": {
                "invoices": {
                    "name": "invoices",
                    "schema": "reporting",
                    "columns": [{"name": "id", "type": "integer"}],
                }
            }
        },
        # A model that declares no schema — the repo's convention renders the bare name.
        "staging": {
            "tables": {
                "drafts": {
                    "name": "drafts",
                    "schema": None,
                    "columns": [{"name": "id", "type": "integer"}],
                }
            }
        },
    }
    lines = run_golden_eval._schema_text(list(bundles.values())).splitlines()

    assert lines == [
        "billing.invoices(id integer)",
        "reporting.invoices(id integer)",
        "drafts(id integer)",
    ]


# ---------------------------------------------------------------------------
# The skill's prose
#
# Everything below asserts on SKILL.md rather than on code, for the reason
# `test_skill_guardrails.py` gives: these behaviors have no code-level equivalent. The helper cannot
# make the model separate the unconfirmed from the failures, refuse to paste a statement it can
# read out of the artifact, or say that a run of nothing but errors is not green — the skill is the
# only place those live, so "the skill says X" is the only decidable check there is.
# ---------------------------------------------------------------------------

SKILL = (REPO_ROOT / "plugins" / "agami" / "skills" / "agami-eval" / "SKILL.md").read_text(
    encoding="utf-8"
)
FRONTMATTER = SKILL.split("---")[1]


def test_the_skill_carries_the_four_frontmatter_keys():
    """The house shape. `argument-hint` is the one a new skill forgets, and without it the dataset
    name has nowhere to arrive from."""
    assert SKILL.startswith("---\n")  # frontmatter at the top, not prose
    assert "name: agami-eval" in FRONTMATTER
    assert "description:" in FRONTMATTER
    assert "when_to_use:" in FRONTMATTER
    assert 'argument-hint: "[dataset-name]"' in FRONTMATTER


def test_the_skill_refuses_in_plan_mode():
    """A run executes SQL and writes a report, so it cannot proceed read-only."""
    assert "shared/plan-mode-check.md" in SKILL  # the shared detection logic
    assert "I can't run an eval in plan mode" in SKILL  # …and the refusal it ends the turn on
    assert "DO NOT call `ExitPlanMode`" in SKILL  # …without leaving a plan file behind


def test_the_skill_routes_authoring_to_the_shared_shape():
    """Naming the reference at the authoring moment is what keeps the model from globbing another
    profile's datasets to learn the shape — an answer key is a tenant's data."""
    assert "shared/golden-dataset-shape.md" in SKILL
    assert "never read another profile" in SKILL


def test_the_summary_names_the_unscored_count():
    """A dataset whose relative windows have outrun its data must not read as a clean run.

    Asserted on the summary TEMPLATE rather than on the file, because the criterion is that the
    count appears beside the failure count. Deleting it from the template left the word elsewhere
    in the file and a check for the word alone stayed green."""
    summary_line = next(
        line for line in SKILL.splitlines() if line.startswith("Ran <dataset> on <profile>:")
    )

    assert "unscored" in summary_line
    assert "Unscored" in SKILL  # …and its own section, not only a number in the summary line


def test_the_skill_separates_the_unconfirmed_from_the_failures():
    """They ran and they reported and they can never gate, so a reader who scans the failures must
    not find one of these in the list.

    "Visibly apart" is an ORDERING claim, so the assertion is on the order: a 3e that had been
    moved above the failures section still contained every phrase a presence check looked for."""
    assert "can never gate" in SKILL
    assert SKILL.index("### 3e") > SKILL.index("### 3b")


def test_the_skill_says_a_dataset_with_nothing_confirmed_gates_on_nothing():
    """The state most profiles are actually in. The run is worth having and its verdict rests on
    nothing, and only the skill can say so — the payload's counts do not say it by themselves."""
    assert "has no confirmed cases" in SKILL
    assert "rests on nothing" in SKILL


def test_the_skill_says_what_to_do_when_the_model_cannot_be_read():
    """One of the three ways the run path refuses before anything runs, so the cheat sheet carries
    it beside the other two."""
    assert "cannot read the semantic model" in SKILL


def test_the_skill_forbids_pasting_sql():
    """The helper withholds both statements; the artifact carries them, and the model can read it."""
    assert "Never paste SQL in chat" in SKILL
    assert "do not read it out of the artifact" in SKILL


def test_the_skill_says_a_verdict_is_not_zero_failures():
    """`gating_failures` counts items that were SCORED, so a run where every generation errored has
    zero of them and is not green. The skill reads `completed` and `errored` too."""
    assert 'A verdict is not "zero failures"' in SKILL
    assert "completed" in SKILL and "gating_failures" in SKILL and "errored" in SKILL


# ---------------------------------------------------------------------------
# The shared docs
#
# A skill nobody is told to invoke is a skill nobody invokes, and a path nobody documents is one
# the next author invents a second spelling for. Both are prose, so both are asserted as prose.
# ---------------------------------------------------------------------------

SKILLS_DIR = REPO_ROOT / "plugins" / "agami" / "skills"
SHARED = REPO_ROOT / "plugins" / "agami" / "shared"
CONVENTIONS = (SHARED / "invocation-conventions.md").read_text(encoding="utf-8")
FILE_LAYOUT = (SHARED / "file-layout.md").read_text(encoding="utf-8")

# The opening sentence counts in words, so a test that reads it has to spell them too. Only the
# range a plugin can plausibly ship — a count outside it is a drift worth failing on by itself.
NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}


def test_invocation_conventions_lists_the_skill():
    """The routing doc is where a caller learns a skill exists. Absent from it, `/agami-eval` is
    reachable only by someone who already knows the name."""
    assert "| agami-eval |" in CONVENTIONS
    assert "`/agami-eval`" in CONVENTIONS


def test_the_routing_triggers_are_the_skills_own():
    """The table header says the triggers come from `when_to_use`, so a phrase in the row that the
    frontmatter does not carry routes nothing — it reads as a trigger and is not one."""
    row = next(line for line in CONVENTIONS.splitlines() if line.startswith("| agami-eval |"))
    quoted = re.findall(r'"([^"]+)"', row)

    assert quoted, "the agami-eval row no longer quotes any trigger phrase"
    for phrase in quoted:
        assert phrase in FRONTMATTER


def test_invocation_conventions_count_matches_the_directory():
    """The doc said "five" while the directory held seven — two skills shipped and nothing caught
    it, because the count was prose and the truth was a directory listing. Derive both."""
    shipped = sorted(path.name for path in SKILLS_DIR.iterdir() if path.is_dir())
    match = re.search(r"agami ships (\w+) skills", CONVENTIONS)

    assert match, "the opening sentence no longer states how many skills agami ships"
    assert NUMBER_WORDS.get(match.group(1)) == len(shipped)
    # …and the table is what a reader actually routes from, so it carries every one of them.
    for name in shipped:
        assert f"| {name} |" in CONVENTIONS


def test_file_layout_documents_the_golden_dataset_path():
    """Where a dataset is authored and where a run's artifact lands. Neither appeared in the layout
    doc before this skill, so an author had only the skill's prose to go on."""
    assert "golden_datasets" in FILE_LAYOUT
    assert "eval" in FILE_LAYOUT
