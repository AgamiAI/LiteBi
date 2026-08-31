"""The parse door of the golden-authoring helper: what it reads, what it refuses, and what it
does NOT do.

`golden_author.py parse` is the step between a spreadsheet and a dataset, and it is deliberately
inert: it reads a CSV, matches its header against a column contract, derives an id per row, and
prints what it found. **It writes nothing.** That is the spec's third success criterion, and
`test_parse_writes_nothing` is the whole of it — "the rows are confirmed before anything is
written" is only a decidable claim if the parse itself cannot write, which is a property of the
code rather than a promise about how a skill drives it.

The refusals matter as much as the happy path. A question bank whose question column cannot be
identified is a sheet somebody laid out differently, not a sheet whose first column is the
question — guessing column 0 would import a bank of ids or timestamps as questions and every one
of them would read back as a model failure later. So the refusal names every header it actually
found, which is the only thing the person needs in order to fix it.

Every fixture is a question over the shipped sample store database, so nothing here names a real
dataset, table or question.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import golden_author  # noqa: E402

PROFILE = "demo"

QUERY = "How many orders have been placed?"
SQL = "SELECT COUNT(*) AS order_count FROM orders"


def _csv(tmp_path, body: str) -> str:
    path = tmp_path / "questions.csv"
    path.write_text(body, encoding="utf-8")
    return str(path)


def _parse(tmp_path, monkeypatch, capsys, body: str):
    """Run the parse verb over `body` and return (exit code, stdout payload, stderr)."""
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    code = golden_author.main(["parse", "--csv", _csv(tmp_path, body)])
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out.strip() else None
    return code, payload, captured.err


# --- the point of the slice ---


def test_parse_writes_nothing(tmp_path, monkeypatch, capsys):
    """SC-3. A successful parse leaves the model tree exactly as it found it.

    The confirmation step this slice exists to enable is only real if the parse cannot write, so
    this asserts the absence of the directory a write would have to create rather than the absence
    of a particular file.
    """
    code, payload, _ = _parse(tmp_path, monkeypatch, capsys, f"question,sql\n{QUERY},{SQL}\n")
    assert code == 0
    assert payload["summary"] == {"parsed": 1, "skipped": 0}
    assert not (tmp_path / PROFILE / "golden_datasets").exists()


# --- the refusals ---


def test_an_unmatched_question_column_refuses_and_names_the_headers(tmp_path, monkeypatch, capsys):
    """SC-4. A sheet with no recognisable question column stops, and says what it saw.

    Never a fallback to column 0: a bank of ids imported as questions produces items that fail
    every run for a reason that looks like a model regression and is not one. Naming the headers is
    what lets the person rename one and re-invoke.
    """
    code, payload, err = _parse(
        tmp_path, monkeypatch, capsys, "ticket ref,owner,notes\nT-1,analyst,check this\n"
    )
    assert code == 2
    assert payload is None
    for header in ("ticket ref", "owner", "notes"):
        assert header in err


def test_a_file_with_no_header_row_refuses_and_names_its_first_row(tmp_path, monkeypatch, capsys):
    """A headerless sheet cannot have a column contract, so it is the same refusal.

    The first row is data, and importing it as a header would silently drop a real question as
    well as mis-key every column below it.
    """
    code, payload, err = _parse(
        tmp_path, monkeypatch, capsys, f"{QUERY},42\nHow many customers?,7\n"
    )
    assert code == 2
    assert payload is None
    assert "header" in err.lower()
    assert QUERY in err


# --- the per-row rules ---


def test_an_empty_question_is_skipped_and_counted(tmp_path, monkeypatch, capsys):
    """SC-5. A blank question is reported as skipped, never dropped.

    A sheet that comes back one row shorter than it went in, with nothing said about it, is how an
    import quietly loses a question nobody notices is missing.
    """
    code, payload, _ = _parse(
        tmp_path,
        monkeypatch,
        capsys,
        f"question\n{QUERY}\n   \nHow many customers are on file?\n",
    )
    assert code == 0
    assert payload["summary"] == {"parsed": 2, "skipped": 1}
    assert payload["skipped"] == [{"row": 2, "reason": "empty question"}]
    assert [row["query"] for row in payload["rows"]] == [QUERY, "How many customers are on file?"]


def test_a_question_of_only_punctuation_is_skipped_and_counted(tmp_path, monkeypatch, capsys):
    """A question that slugs to nothing has no id, so it is skipped for a stated reason.

    Writing it under an empty id would collide with the next one like it, and the append-only
    duplicate path would then fire on two rows that have nothing to do with each other.
    """
    code, payload, _ = _parse(tmp_path, monkeypatch, capsys, f"question\n???\n{QUERY}\n")
    assert code == 0
    assert payload["summary"] == {"parsed": 1, "skipped": 1}
    assert payload["skipped"] == [{"row": 1, "reason": "question has no usable characters"}]


def test_two_questions_that_slug_alike_get_distinct_ids(tmp_path, monkeypatch, capsys):
    """Different questions that derive the same slug must not collide inside one parse.

    `GoldenItem.item_key` is exactly the id, so two rows sharing one would be a duplicate on the
    write path — and the person would be asked to resolve a clash between two questions they can
    both legitimately keep.
    """
    code, payload, _ = _parse(
        tmp_path, monkeypatch, capsys, "question\nHow many orders?\nHow many orders!\n"
    )
    assert code == 0
    assert [row["id"] for row in payload["rows"]] == ["how-many-orders", "how-many-orders-2"]


def test_an_explicit_id_column_wins_over_the_derived_slug(tmp_path, monkeypatch, capsys):
    """A sheet that already keys its rows keeps its own keys.

    The slug exists so re-importing an unkeyed sheet is idempotent; a sheet with an id column
    already has that property, and overriding it would break the person's own cross-reference.
    """
    code, payload, _ = _parse(tmp_path, monkeypatch, capsys, f"id,question\norders-count,{QUERY}\n")
    assert code == 0
    assert [row["id"] for row in payload["rows"]] == ["orders-count"]


def test_header_aliases_fold_case_and_underscores(tmp_path, monkeypatch, capsys):
    """`NL_Question` is the same column as `nl question`, and a spreadsheet writes either.

    Folding is exact-match against a named alias set, not fuzzy matching: a header this contract
    does not know is an analyst's own note column and is ignored, which is only safe while a match
    is never approximate.
    """
    code, payload, _ = _parse(
        tmp_path, monkeypatch, capsys, f"NL_Question, Expected-Value \n{QUERY},7\n"
    )
    assert code == 0
    assert payload["columns"] == ["NL_Question", " Expected-Value "]
    assert payload["rows"][0]["query"] == QUERY
    assert payload["rows"][0]["expected_value"] == 7.0


def test_expected_values_are_normalized_and_an_unparseable_one_is_null(
    tmp_path, monkeypatch, capsys
):
    """The expected column goes through `reconcile.parse_value`, dashboard spellings and all.

    Reusing the reconcile parser rather than re-deriving number parsing is the decision under test:
    a second definition of what `$1.2M` means is a second answer to the same question. A cell it
    cannot read is `null` rather than a refusal — this value is shown in the confirmation table and
    never becomes an answer key, so an unreadable one costs the person nothing.
    """
    code, payload, _ = _parse(
        tmp_path,
        monkeypatch,
        capsys,
        f"question,expected\n{QUERY},$1.2M\nWhat is our total revenue?,about a dozen\n",
    )
    assert code == 0
    assert [row["expected_value"] for row in payload["rows"]] == [1200000.0, None]


def test_a_statement_column_is_carried_without_claiming_confirmation(tmp_path, monkeypatch, capsys):
    """A sheet may already hold SQL, and carrying it must not read as verification.

    Nobody ran that statement here, so nothing in this payload may say it was confirmed: the
    confirmed-only rule is what makes a golden run's green mean something, and a spreadsheet
    column is not a person who looked at a result.
    """
    code, payload, _ = _parse(tmp_path, monkeypatch, capsys, f"question,statement\n{QUERY},{SQL}\n")
    assert code == 0
    assert payload["rows"][0]["sql"] == SQL
    assert "confirm" not in json.dumps(payload).lower()


def test_tags_split_on_commas(tmp_path, monkeypatch, capsys):
    """A tag cell is a list a person typed into one box, and empties are noise rather than tags."""
    code, payload, _ = _parse(
        tmp_path, monkeypatch, capsys, f'question,tags\n{QUERY},"smoke, revenue,,"\n'
    )
    assert code == 0
    assert payload["rows"][0]["tags"] == ["smoke", "revenue"]
