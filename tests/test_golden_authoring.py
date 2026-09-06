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

import pytest

# The write doors go through AH-100's models, so the whole module needs the optional model extra
# even though the parse door is stdlib-only.
pytest.importorskip("pydantic")

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import golden_author  # noqa: E402
import yaml  # noqa: E402
from semantic_model.golden import load_golden_datasets  # noqa: E402

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
    # Sheet row 3: the header is row 1 and the blank sits under the first question.
    assert payload["skipped"] == [{"row": 3, "reason": "empty question"}]
    assert [row["query"] for row in payload["rows"]] == [QUERY, "How many customers are on file?"]


def test_a_question_of_only_punctuation_is_skipped_and_counted(tmp_path, monkeypatch, capsys):
    """A question that slugs to nothing has no id, so it is skipped for a stated reason.

    Writing it under an empty id would collide with the next one like it, and the append-only
    duplicate path would then fire on two rows that have nothing to do with each other.
    """
    code, payload, _ = _parse(tmp_path, monkeypatch, capsys, f"question\n???\n{QUERY}\n")
    assert code == 0
    assert payload["summary"] == {"parsed": 1, "skipped": 1}
    # Sheet row 2 — the first line under the header.
    assert payload["skipped"] == [{"row": 2, "reason": "question has no usable characters"}]


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


# --- the write core, and the import door on top of it ---
#
# Every write in this helper — both doors, and AH-111's promotion later — goes through one funnel,
# so the append-only rule and the write-then-re-read gate are asserted once here and inherited.
# The reader is the only validator there is (`semantic_model.golden` ships no writer and no
# standalone `validate()`), which is why every one of these tests ends by reading the file back
# rather than by inspecting the YAML it just wrote.

REVENUE = "What is our total revenue?"
REVENUE_SQL = "SELECT ROUND(SUM(total_amount), 2) AS revenue FROM orders"


def _run(tmp_path, monkeypatch, capsys, argv: list[str]):
    """Run any verb and return (exit code, stdout payload, stderr)."""
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    code = golden_author.main(argv)
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out.strip() else None
    return code, payload, captured.err


def _row(query: str = QUERY, **kw) -> dict:
    """One row of the `parse` payload, keyed the way the parse door keys it."""
    return {
        "id": golden_author._slug(query),
        "query": query,
        "expected_value": None,
        "sql": None,
        "tags": [],
        **kw,
    }


def _rows_file(tmp_path, rows: list[dict]) -> str:
    """The `parse` payload as the skill hands it back after the person has confirmed it."""
    path = tmp_path / "parsed.json"
    path.write_text(json.dumps({"rows": rows}), encoding="utf-8")
    return str(path)


def _import_argv(rows_path: str, stem: str = "orders", *extra: str) -> list[str]:
    return ["import", "--profile", PROFILE, "--dataset", stem, "--rows", rows_path, *extra]


def _dataset_file(tmp_path, stem: str = "orders") -> Path:
    return tmp_path / PROFILE / "golden_datasets" / f"{stem}.yaml"


def _items(tmp_path, stem: str = "orders"):
    """The dataset's cases as the runner would see them, asserting the read was clean."""
    datasets, res = load_golden_datasets(PROFILE, tmp_path)
    assert res.ok, res.errors
    return next(d.test_cases for d in datasets if d.name == stem)


def test_imported_items_read_back_through_the_reader(tmp_path, monkeypatch, capsys):
    """SC-1. A sheet of questions becomes items the runner can read, in sheet order.

    Reading them back through `load_golden_datasets` rather than through the YAML is the whole
    assertion: the writer's only claim to correctness is that the reader the runner uses accepts
    what it wrote, so a test that inspected the dump would be checking the writer against itself.
    """
    rows = [_row(), _row(REVENUE)]
    code, payload, _ = _run(tmp_path, monkeypatch, capsys, _import_argv(_rows_file(tmp_path, rows)))
    assert code == 0
    assert payload["added"] == ["how-many-orders-have-been-placed", "what-is-our-total-revenue"]
    assert payload["replaced"] == []
    assert payload["summary"] == {"added": 2, "replaced": 0, "removed": 0}
    assert [item.query for item in _items(tmp_path)] == [QUERY, REVENUE]


def test_an_import_never_marks_its_own_rows_confirmed(tmp_path, monkeypatch, capsys):
    """SC-2. Not one imported row is confirmed — least of all the row that came with SQL.

    A sheet's statement column is carried, because throwing it away would lose work; but nobody
    ran it, and an import that marked its own rows verified would forge the gate the confirmed-only
    rule exists to be. The row with a statement is in this fixture precisely because it is the one
    a writer would be tempted to promote.
    """
    rows = [_row(), _row(REVENUE, sql=REVENUE_SQL)]
    code, _, _ = _run(tmp_path, monkeypatch, capsys, _import_argv(_rows_file(tmp_path, rows)))
    assert code == 0
    items = _items(tmp_path)
    assert [item.expected.sql_confirmed for item in items] == [False, False]
    assert items[1].expected.sql == REVENUE_SQL


def test_the_written_file_never_declares_its_own_name(tmp_path, monkeypatch, capsys):
    """The dataset's name is its filename, and a file that says so again is refused by the reader.

    `load_golden_datasets` rejects a `name:` key outright (`golden_invalid_dataset`), so a dumper
    that serialized the model whole would write a file it could never read back. The reader would
    catch it, but only after the write — this pins the shape at the point it is produced.
    """
    _run(tmp_path, monkeypatch, capsys, _import_argv(_rows_file(tmp_path, [_row()])))
    doc = yaml.safe_load(_dataset_file(tmp_path).read_text(encoding="utf-8"))
    assert "name" not in doc


def test_re_importing_the_same_sheet_asks_before_it_doubles_anything(tmp_path, monkeypatch, capsys):
    """The determinism of the derived id is what makes a second import a duplicate, not a doubling.

    This is the payoff of `_slug`: sequential ids would land the same questions under fresh keys,
    the append-only rule would never fire on the import door, and a dataset would silently grow a
    second copy of every question each time somebody re-ran the sheet.
    """
    argv = _import_argv(_rows_file(tmp_path, [_row()]))
    assert _run(tmp_path, monkeypatch, capsys, argv)[0] == 0
    code, payload, _ = _run(tmp_path, monkeypatch, capsys, argv)
    assert code == 1
    assert [entry["id"] for entry in payload["needs_confirmation"]] == [
        "how-many-orders-have-been-placed"
    ]
    assert len(_items(tmp_path)) == 1


def test_a_broken_dataset_elsewhere_neither_blocks_the_write_nor_gets_repaired(
    tmp_path, monkeypatch, capsys
):
    """A correct write must not be rolled back by somebody else's already-broken file.

    The re-read validates the whole profile, so an unrelated dataset that was already failing would
    otherwise fail our write too — rolling back a correct file and reporting a fault at a locator
    the person did not touch. The other half matters as much: the broken file is left exactly as it
    was, because quietly rewriting a neighbour is not this door's business either.
    """
    gdir = tmp_path / PROFILE / "golden_datasets"
    gdir.mkdir(parents=True)
    broken = gdir / "legacy.yaml"
    # `sql_confirmed` is the one field with no default, so this case cannot be read at all.
    broken.write_text(
        yaml.safe_dump({"test_cases": [{"id": "stale", "query": QUERY, "expected": {}}]}),
        encoding="utf-8",
    )
    before = broken.read_bytes()

    code, _, _ = _run(tmp_path, monkeypatch, capsys, _import_argv(_rows_file(tmp_path, [_row()])))
    assert code == 0
    assert _dataset_file(tmp_path).exists()
    assert broken.read_bytes() == before

    datasets, res = load_golden_datasets(PROFILE, tmp_path)
    assert [d.name for d in datasets] == ["legacy", "orders"]
    assert [f.locator for f in res.findings] == ["legacy.yaml[stale]"]


def _break_the_reread(monkeypatch, stem: str, marker: str) -> None:
    """Make the re-read report an error for `stem` once `marker` reaches disk.

    There is no input this door can construct that the reader then refuses — the writer builds
    `GoldenItem`s, so pydantic has already accepted everything by the time it is dumped. That is
    the design working, and it also means the rollback has no natural fixture: the only way to
    exercise it is to fail the re-read on purpose.
    """
    real = golden_author.load_golden_datasets

    def fake(profile, art=None):
        datasets, res = real(profile, art)
        path = golden_author._datasets_dir(profile) / f"{stem}.yaml"
        if path.exists() and marker in path.read_text(encoding="utf-8"):
            locator = f"{stem}.yaml[{marker}]"
            res.error("golden_invalid_case", f"{locator}: injected by the test", locator=locator)
        return datasets, res

    monkeypatch.setattr(golden_author, "load_golden_datasets", fake)


def test_a_failed_reread_restores_the_previous_bytes(tmp_path, monkeypatch, capsys):
    """A write the reader rejects leaves the dataset byte-identical to what it was.

    Byte-identical rather than merely equivalent: a rollback that re-dumped the parsed model would
    silently reformat a file the person may have hand-written, which is a change nobody asked for
    on the path where nothing was supposed to change at all.
    """
    assert _run(tmp_path, monkeypatch, capsys, _import_argv(_rows_file(tmp_path, [_row()])))[0] == 0
    before = _dataset_file(tmp_path).read_bytes()

    _break_the_reread(monkeypatch, "orders", "what-is-our-total-revenue")
    code, _, err = _run(
        tmp_path, monkeypatch, capsys, _import_argv(_rows_file(tmp_path, [_row(REVENUE)]))
    )
    assert code == 2
    assert _dataset_file(tmp_path).read_bytes() == before
    assert "injected by the test" in err


def test_a_failed_reread_on_a_new_dataset_leaves_nothing_behind(tmp_path, monkeypatch, capsys):
    """The first write to a profile creates the directory too, so the rollback has to unmake both.

    A half-created `golden_datasets/` holding a file the reader refused is worse than no dataset:
    the next run reads it, reports the fault, and the person has to work out that nothing they did
    ever succeeded.
    """
    _break_the_reread(monkeypatch, "orders", "how-many-orders-have-been-placed")
    code, _, _ = _run(tmp_path, monkeypatch, capsys, _import_argv(_rows_file(tmp_path, [_row()])))
    assert code == 2
    assert not (tmp_path / PROFILE / "golden_datasets").exists()


# --- the save door, and the lint it is refused by ---
#
# The second door is the only one that can produce an item able to gate a run, because it is the
# only one behind which a person looked at a result. It writes through the same funnel as the
# import door, so everything asserted above holds here too and is not re-asserted.

METHOD = "read on screen and accepted"
RELATIVE = "How many orders were placed in the last 7 days?"
FROZEN_SQL = "SELECT COUNT(*) AS order_count FROM orders WHERE placed_at >= '2026-01-01'"
ANCHORED_SQL = "SELECT COUNT(*) AS order_count FROM orders WHERE placed_at >= CURRENT_DATE - 7"


def _item_file(tmp_path, **kw) -> str:
    """One answer somebody accepted, in the shape AH-111 will send."""
    payload = {
        "id": None,
        "query": QUERY,
        "sql": SQL,
        "match": None,
        "tags": None,
        "recorded": {"columns": ["order_count"], "rows": [[42]]},
        "confirmed_by": {"method": METHOD},
    }
    payload.update(kw)
    path = tmp_path / "item.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _save_argv(item_path: str, stem: str = "orders", *extra: str) -> list[str]:
    return ["save", "--profile", PROFILE, "--dataset", stem, "--item", item_path, *extra]


def test_a_saved_answer_carries_its_statement_and_receipt(tmp_path, monkeypatch, capsys):
    """SC-6. The one door that can produce an item able to gate a run.

    All four parts have to survive the round trip together: without the statement there is nothing
    to compare, without `sql_confirmed` the runner will not let the case gate, and without the
    receipt a reviewer months later cannot see what the answer looked like on the day somebody
    accepted it. `confirmed_by.method` is carried verbatim — AH-100 types it as free text on
    purpose, so nothing here narrows it to a vocabulary.
    """
    code, _, _ = _run(tmp_path, monkeypatch, capsys, _save_argv(_item_file(tmp_path)))
    assert code == 0
    item = _items(tmp_path)[0]
    assert item.id == "how-many-orders-have-been-placed"
    assert item.expected.sql_confirmed is True
    assert item.expected.sql == SQL
    assert item.recorded.columns == ["order_count"] and item.recorded.rows == [[42]]
    assert item.confirmed_by.method == METHOD
    # Both stamps are set here rather than left None: a receipt that cannot say when it was taken
    # is not much of a receipt.
    assert item.recorded.at and item.confirmed_by.at
    assert item.match == "exact"


def test_a_duplicate_save_declined_leaves_the_file_byte_identical(tmp_path, monkeypatch, capsys):
    """SC-7, the decline direction. An answer key that can be overwritten in passing is not one.

    This is the whole of the append-only objection: the risk was never that the door exists, but
    that it would make a failing item easy to replace. So the person is shown the statement that is
    there and the statement that would take its place, and until they say yes the file is not
    touched at all — byte-identical, not merely equivalent.
    """
    _run(tmp_path, monkeypatch, capsys, _save_argv(_item_file(tmp_path)))
    before = _dataset_file(tmp_path).read_bytes()

    replacement = _item_file(
        tmp_path, sql=REVENUE_SQL, recorded={"columns": ["revenue"], "rows": [[1234.56]]}
    )
    code, payload, _ = _run(tmp_path, monkeypatch, capsys, _save_argv(replacement))
    assert code == 1
    assert _dataset_file(tmp_path).read_bytes() == before

    entry = payload["needs_confirmation"][0]
    assert entry["id"] == "how-many-orders-have-been-placed"
    assert entry["before"]["expected"]["sql"] == SQL
    assert entry["after"]["expected"]["sql"] == REVENUE_SQL


def test_a_duplicate_save_confirmed_replaces_the_item_in_place(tmp_path, monkeypatch, capsys):
    """SC-7, the accept direction. The yes replaces one item and disturbs nothing else.

    In place rather than appended: the id is the key every stored result is already filed under,
    and moving the case to the end of the file would reorder a diff nobody asked to reorder. Its
    siblings staying unconfirmed is the other half — a confirmation is about one answer, and a
    door that promoted the whole file would be the forged gate again by another route.
    """
    rows = [_row(REVENUE), _row(), _row("How many customers are on file?")]
    assert _run(tmp_path, monkeypatch, capsys, _import_argv(_rows_file(tmp_path, rows)))[0] == 0

    code, payload, _ = _run(
        tmp_path,
        monkeypatch,
        capsys,
        _save_argv(_item_file(tmp_path), "orders", "--confirm-replace"),
    )
    assert code == 0
    assert payload["replaced"] == ["how-many-orders-have-been-placed"]
    assert payload["added"] == []

    items = _items(tmp_path)
    assert [item.id for item in items] == [
        "what-is-our-total-revenue",
        "how-many-orders-have-been-placed",
        "how-many-customers-are-on-file",
    ]
    assert items[1].expected.sql == SQL
    assert [item.expected.sql_confirmed for item in items] == [False, True, False]


def test_a_relative_question_with_frozen_sql_is_refused_at_save_time(tmp_path, monkeypatch, capsys):
    """SC-8. A question that moves with time and an answer key that does not is refused now.

    The lint is AH-100's, called rather than restated — a second copy of those three regexes would
    drift, and this is exactly the "no second parser" failure the repo guards against. The point of
    running it here is the timing: the reader would report this at the next run, by which time the
    dataset is written and whoever reads the finding has to work out that saving it was the mistake.
    """
    code, _, err = _run(
        tmp_path,
        monkeypatch,
        capsys,
        _save_argv(_item_file(tmp_path, query=RELATIVE, sql=FROZEN_SQL)),
    )
    assert code == 2
    assert "moves with time" in err
    # Nothing was written, not even the directory — the refusal is before the write, not a rollback.
    assert not (tmp_path / PROFILE / "golden_datasets").exists()


def test_a_relative_question_anchored_on_the_current_date_saves(tmp_path, monkeypatch, capsys):
    """The control for the refusal above: the lint objects to the pinning, not to the question.

    Without this, a pre-check that refused every question phrased against today would pass the test
    beside it while making the save door useless for the commonest question there is.
    """
    code, _, _ = _run(
        tmp_path,
        monkeypatch,
        capsys,
        _save_argv(_item_file(tmp_path, query=RELATIVE, sql=ANCHORED_SQL)),
    )
    assert code == 0
    assert _items(tmp_path)[0].expected.sql == ANCHORED_SQL


# --- the names the doors are handed, and the tree they may not leave ---
#
# `--profile` and `--dataset` are joined straight into a filesystem path this helper then WRITES,
# so they are the one pair of inputs where being wrong is destructive rather than merely useless.
# Every test here asserts the neighbour is untouched as well as the refusal, because a refusal that
# still landed the file would pass a test that only read the exit code.


def _neighbour(tmp_path) -> Path:
    """A model file beside the datasets dir, standing in for whatever the profile already holds."""
    path = tmp_path / PROFILE / "datasource.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("datasource: store\n", encoding="utf-8")
    return path


def test_a_dataset_name_that_climbs_out_of_the_profile_is_refused(tmp_path, monkeypatch, capsys):
    """A stem is one filename, so a stem that walks up out of the profile is not a stem.

    The escape is the whole finding: `../../other/golden_datasets/stolen` writes an answer key into
    a sibling profile, which in a hosted deployment is another tenant's tree. Refused on the name,
    before any path is built from it.
    """
    rows = _rows_file(tmp_path, [_row()])
    stem = "../../othertenant/golden_datasets/stolen"
    code, _, err = _run(tmp_path, monkeypatch, capsys, _import_argv(rows, stem))
    assert code == 2
    assert "dataset" in err
    assert not (tmp_path / "othertenant").exists()


def test_a_profile_name_that_climbs_out_of_the_artifacts_dir_is_refused(
    tmp_path, monkeypatch, capsys
):
    """The other half of the join. A profile is a directory name under the artifacts dir, and one
    that climbs above it leaves the tree agami owns entirely."""
    rows = _rows_file(tmp_path, [_row()])
    code, _, err = _run(
        tmp_path,
        monkeypatch,
        capsys,
        ["import", "--profile", "../outside", "--dataset", "orders", "--rows", rows],
    )
    assert code == 2
    assert "profile" in err
    assert not (tmp_path.parent / "outside").exists()


def test_a_dataset_name_carrying_a_separator_is_refused(tmp_path, monkeypatch, capsys):
    """`sub/nested` needs no `..` to break the contract, which is why the rule is a whitelist.

    The reader globs `*.yaml` in one directory and never recurses, so a file written a level down is
    one the re-read cannot see: `_our_faults` finds nothing, and the write reports itself validated
    when nothing validated it.
    """
    rows = _rows_file(tmp_path, [_row()])
    code, _, err = _run(tmp_path, monkeypatch, capsys, _import_argv(rows, "sub/nested"))
    assert code == 2
    assert "dataset" in err
    assert not (tmp_path / PROFILE / "golden_datasets" / "sub").exists()


def test_a_dataset_name_carrying_its_own_extension_is_refused(tmp_path, monkeypatch, capsys):
    """`--dataset orders.yaml` reads as helpful and writes `orders.yaml.yaml`.

    The stem is the dataset's name, and the reader takes that name from the filename stem — so the
    extension the author typed becomes part of what their dataset is called. `orders.yml` is the
    worse half: the reader refuses a real `.yml` file as misnamed, so a dataset called `orders.yml`
    reads back fine and contradicts the one rule the directory has.
    """
    rows = _rows_file(tmp_path, [_row()])
    for stem in ("orders.yaml", "orders.yml", "ORDERS.YML"):
        code, _, err = _run(tmp_path, monkeypatch, capsys, _import_argv(rows, stem))
        assert code == 2, stem
        assert "without the extension" in err, stem
    assert not (tmp_path / PROFILE / "golden_datasets").exists()


def test_a_traversing_dataset_name_never_touches_the_file_it_names(tmp_path, monkeypatch, capsys):
    """The destructive direction, and the reason the check runs before any I/O.

    `../datasource` resolves onto the profile's own semantic model, and this door truncates and
    rewrites whatever `<stem>.yaml` names — so an unchecked stem does not merely write in the wrong
    place, it destroys the file already there and reports success.
    """
    neighbour = _neighbour(tmp_path)
    before = neighbour.read_bytes()
    rows = _rows_file(tmp_path, [_row()])
    code, _, _ = _run(tmp_path, monkeypatch, capsys, _import_argv(rows, "../datasource"))
    assert code == 2
    assert neighbour.read_bytes() == before


def test_an_ordinary_stem_with_dots_and_dashes_still_writes(tmp_path, monkeypatch, capsys):
    """The control. The rule refuses separators and parent hops, not the names people use."""
    rows = _rows_file(tmp_path, [_row()])
    code, _, _ = _run(tmp_path, monkeypatch, capsys, _import_argv(rows, "orders.v2-smoke"))
    assert code == 0
    assert [item.query for item in _items(tmp_path, "orders.v2-smoke")] == [QUERY]


# --- exit 1 means one thing, so nothing unexpected may produce it ---
#
# The skill reads the exit code before the payload and `1` tells it to render a `needs_confirmation`
# block and re-run with `--confirm-replace`. An uncaught exception exiting 1 therefore escalates a
# crash into a confirmed-overwrite attempt, which is why every one of these asserts on the code
# rather than only on the message.


def _json_file(tmp_path, name: str, body) -> str:
    path = tmp_path / name
    path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    return str(path)


def test_an_item_missing_its_statement_refuses_rather_than_crashing(tmp_path, monkeypatch, capsys):
    """A save-door item with no `sql` key: ordinary input, and a `KeyError` before the fix."""
    item = _json_file(tmp_path, "item.json", {"query": QUERY, "confirmed_by": {"method": METHOD}})
    code, _, err = _run(tmp_path, monkeypatch, capsys, _save_argv(item))
    assert code == 2
    assert golden_author._PREFIX in err
    assert "sql" in err
    assert "Traceback" not in err


def test_an_item_whose_statement_is_null_refuses_without_echoing_the_item(
    tmp_path, monkeypatch, capsys
):
    """`sql: null` builds a confirmed case with no answer key, which pydantic refuses.

    The value must not travel with the refusal: pydantic's own text carries `input_value=`, which
    for this model is the answer key and the recorded result — the exact thing
    `golden._validation_digest` exists to strip before a finding is forwarded anywhere.
    """
    code, _, err = _run(tmp_path, monkeypatch, capsys, _save_argv(_item_file(tmp_path, sql=None)))
    assert code == 2
    assert "input_value" not in err
    assert QUERY not in err
    assert "order_count" not in err
    assert "Traceback" not in err


def test_a_malformed_item_file_refuses_with_its_own_sentence(tmp_path, monkeypatch, capsys):
    """A truncated or hand-mangled JSON file is a caller mistake, not a crash."""
    item = _json_file(tmp_path, "item.json", '{"query": "How many orders?",')
    code, _, err = _run(tmp_path, monkeypatch, capsys, _save_argv(item))
    assert code == 2
    assert "JSON" in err
    assert "Traceback" not in err


def test_a_missing_input_file_refuses_with_its_own_sentence(tmp_path, monkeypatch, capsys):
    """The commonest mistake of all — a path that is not there — and the least useful traceback."""
    missing = str(tmp_path / "not-here.csv")
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    code = golden_author.main(["parse", "--csv", missing])
    err = capsys.readouterr().err
    assert code == 2
    assert "not-here.csv" in err
    assert "Traceback" not in err


# --- a dataset that already holds a relative/frozen case is not a dataset nobody may write to ---


def _with_frozen_case(tmp_path, stem: str = "orders") -> Path:
    """A hand-written dataset holding one item the relativity lint reports and does not drop.

    Hand-authoring is a supported path, and the lint deliberately keeps the item — so this file is
    readable, complete, and reports a finding forever. A guard that treated any finding as a reason
    to refuse a merge would make it a dataset nobody can ever add to again.
    """
    path = _dataset_file(tmp_path, stem)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "test_cases": [
                    {
                        "id": "stale-window",
                        "query": RELATIVE,
                        "expected": {"sql": FROZEN_SQL, "sql_confirmed": True},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def _cases(tmp_path, stem: str = "orders"):
    """The dataset's cases without asserting the read was clean — for the files that report one."""
    datasets, _ = load_golden_datasets(PROFILE, tmp_path)
    return next(d.test_cases for d in datasets if d.name == stem)


def test_a_dataset_holding_a_frozen_case_still_accepts_a_save(tmp_path, monkeypatch, capsys):
    """The deadlock. The lint keeps the item, so its finding is permanent — and a pre-read guard
    that refused on it would refuse every future write to this dataset, with Hard Rule 6 forbidding
    the hand edit that is the only way out."""
    _with_frozen_case(tmp_path)
    code, _, err = _run(tmp_path, monkeypatch, capsys, _save_argv(_item_file(tmp_path)))
    assert code == 0, err
    assert [item.id for item in _cases(tmp_path)] == [
        "stale-window",
        "how-many-orders-have-been-placed",
    ]


def test_a_dataset_holding_a_frozen_case_still_accepts_an_import(tmp_path, monkeypatch, capsys):
    """The same deadlock through the other door, because the guard is in the shared funnel."""
    _with_frozen_case(tmp_path)
    code, _, err = _run(
        tmp_path, monkeypatch, capsys, _import_argv(_rows_file(tmp_path, [_row(REVENUE)]))
    )
    assert code == 0, err
    assert len(_cases(tmp_path)) == 2


def test_a_dataset_the_reader_would_drop_a_case_from_refuses_the_merge(
    tmp_path, monkeypatch, capsys
):
    """The guard's real job, which the fix above must not remove.

    A case the reader cannot parse is a case it DROPS, so merging into this file would rewrite it
    without the dropped case — this write deleting somebody else's question to make room for its
    own. Refused, and the file is left exactly as it was.
    """
    path = _dataset_file(tmp_path, "orders")
    path.parent.mkdir(parents=True, exist_ok=True)
    # `sql_confirmed` is the one field with no default, so this case cannot be read at all.
    path.write_text(
        yaml.safe_dump({"test_cases": [{"id": "stale", "query": QUERY, "expected": {}}]}),
        encoding="utf-8",
    )
    before = path.read_bytes()

    code, _, err = _run(tmp_path, monkeypatch, capsys, _save_argv(_item_file(tmp_path)))
    assert code == 2
    assert "cannot be read as it stands" in err
    assert path.read_bytes() == before


def test_a_frozen_item_over_an_existing_id_is_refused_before_the_confirmation_walk(
    tmp_path, monkeypatch, capsys
):
    """The relativity refusal is unconditional, so asking about the replacement first is asking a
    question whose every answer is refused. It comes before the append-only stop."""
    rows = _rows_file(tmp_path, [_row(RELATIVE)])
    assert _run(tmp_path, monkeypatch, capsys, _import_argv(rows))[0] == 0
    code, payload, err = _run(
        tmp_path,
        monkeypatch,
        capsys,
        _save_argv(_item_file(tmp_path, query=RELATIVE, sql=FROZEN_SQL)),
    )
    assert code == 2
    assert payload is None
    assert "moves with time" in err


# --- one id, one item: a duplicate inside a single write is refused, never merged away ---


def _dup_rows(tmp_path) -> str:
    """A sheet whose own `id` column repeats — two different questions under one key."""
    return _rows_file(tmp_path, [_row(QUERY, id="q1"), _row(REVENUE, id="q1")])


def test_two_rows_sharing_an_id_are_refused_against_the_sheet_not_the_file(
    tmp_path, monkeypatch, capsys
):
    """A repeated `id` column value is a clash in the person's own sheet, and has to read as one.

    Left to the re-read, the fault arrives as `orders.yaml[q1]: this id was already used in this
    file` and the WHOLE import is rolled back — 200 good rows lost, blamed on a file the person did
    not write, for a duplicate that is two rows of their spreadsheet.
    """
    code, _, err = _run(tmp_path, monkeypatch, capsys, _import_argv(_dup_rows(tmp_path)))
    assert code == 2
    assert "q1" in err
    assert "already used in this file" not in err
    assert not (tmp_path / PROFILE / "golden_datasets").exists()


def test_a_duplicated_id_never_reports_more_items_than_it_wrote(tmp_path, monkeypatch, capsys):
    """The payload is what the skill reports to the person, so it may not overcount the file.

    `added` / `replaced` are counted off the raw list while the merge keys on the id, so with
    `--confirm-replace` the duplicate lands: exit 0, `replaced: [q1, q1]`, and one item on disk with
    the first row's question gone and nothing said about it.
    """
    assert (
        _run(
            tmp_path,
            monkeypatch,
            capsys,
            _import_argv(_rows_file(tmp_path, [_row(QUERY, id="q1")])),
        )[0]
        == 0
    )
    code, payload, err = _run(
        tmp_path,
        monkeypatch,
        capsys,
        _import_argv(_dup_rows(tmp_path), "orders", "--confirm-replace"),
    )
    assert code == 2, payload
    assert "q1" in err
    assert [item.query for item in _items(tmp_path)] == [QUERY]


# --- the fields the item JSON documents, carried rather than dropped ---


def test_a_must_filter_survives_the_save(tmp_path, monkeypatch, capsys):
    """`must_filter` gates HOW the answer was reached, and the skill tells the model to carry it
    forward on a replacement — so a door that dropped it would lose the gate on every save-over,
    silently and at exit 0."""
    code, _, _ = _run(
        tmp_path, monkeypatch, capsys, _save_argv(_item_file(tmp_path, must_filter=["status"]))
    )
    assert code == 0
    assert _items(tmp_path)[0].must_filter == ["status"]


def test_a_bounded_item_saves_with_its_band(tmp_path, monkeypatch, capsys):
    """`bounds` is the other half of `match: bounded`, and AH-100 refuses either half alone."""
    code, _, err = _run(
        tmp_path,
        monkeypatch,
        capsys,
        _save_argv(_item_file(tmp_path, match="bounded", bounds={"min_rows": 1})),
    )
    assert code == 0, err
    item = _items(tmp_path)[0]
    assert item.match == "bounded"
    assert item.bounds.min_rows == 1


def test_a_bounded_item_with_no_band_is_refused_not_crashed(tmp_path, monkeypatch, capsys):
    """A band nothing consults and a level with nothing to consult both keep passing, so AH-100
    refuses the shape — and that refusal has to reach the person as a sentence, not a traceback."""
    code, _, err = _run(
        tmp_path, monkeypatch, capsys, _save_argv(_item_file(tmp_path, match="bounded"))
    )
    assert code == 2
    assert "bounds" in err
    assert "Traceback" not in err


# --- the small ones ---


def test_a_neighbour_named_after_this_stem_does_not_roll_our_write_back(
    tmp_path, monkeypatch, capsys
):
    """`orders.yaml.yaml` is a different dataset whose locator starts with `orders.yaml`.

    Prefix matching would attribute its findings to this write and roll back a correct file, which
    is the same misattribution `_our_faults` exists to prevent for any other neighbour.
    """
    gdir = tmp_path / PROFILE / "golden_datasets"
    gdir.mkdir(parents=True)
    (gdir / "orders.yaml.yaml").write_text(
        yaml.safe_dump({"test_cases": [{"id": "stale", "query": QUERY, "expected": {}}]}),
        encoding="utf-8",
    )
    code, _, err = _run(tmp_path, monkeypatch, capsys, _import_argv(_rows_file(tmp_path, [_row()])))
    assert code == 0, err
    assert _dataset_file(tmp_path).exists()


def test_a_dataset_written_without_a_description_carries_no_empty_one(
    tmp_path, monkeypatch, capsys
):
    """`description: ''` as line 1 is exactly the noise `_drop_empty` exists to keep out of a file
    a person reads and diffs — the reader defaults it, so writing it says nothing."""
    _run(tmp_path, monkeypatch, capsys, _import_argv(_rows_file(tmp_path, [_row()])))
    doc = yaml.safe_load(_dataset_file(tmp_path).read_text(encoding="utf-8"))
    assert "description" not in doc


def test_an_item_with_no_confirmation_method_is_refused(tmp_path, monkeypatch, capsys):
    """An answer key whose provenance is blank cannot be audited later, which is most of what a
    receipt is for. Blank rather than absent: a whitespace method is the likelier mistake."""
    code, _, err = _run(
        tmp_path,
        monkeypatch,
        capsys,
        _save_argv(_item_file(tmp_path, confirmed_by={"method": "  "})),
    )
    assert code == 2
    assert "confirmed_by.method" in err
    assert not (tmp_path / PROFILE / "golden_datasets").exists()


def test_an_empty_sheet_refuses_and_says_what_is_missing(tmp_path, monkeypatch, capsys):
    """A zero-byte CSV has no header row, so there is no column contract to match at all."""
    code, payload, err = _parse(tmp_path, monkeypatch, capsys, "")
    assert code == 2
    assert payload is None
    assert "empty" in err


# --- The explorer page's door -------------------------------------------
#
# `apply` is the third door and the only one that deletes. Its ops come from the page's
# back-channel, which is text somebody pasted, so every one of them is checked here as well.


def _seeded(tmp_path, monkeypatch, capsys, stem="orders"):
    rows = [_row(QUERY, id="orders-count"), _row("An old one", id="orders-stale")]
    code, _, _ = _run(tmp_path, monkeypatch, capsys, _import_argv(_rows_file(tmp_path, rows), stem))
    assert code == 0


def _apply_argv(ops_path, stem="orders", *extra):
    return ["apply", "--profile", PROFILE, "--dataset", stem, "--ops", ops_path, *extra]


def _ops_file(tmp_path, *ops):
    return _json_file(tmp_path, "ops.json", json.dumps({"ops": list(ops)}))


def _op(op, item_id="orders-count", value=None):
    entry = {"op": op, "dataset": "orders", "id": item_id}
    if value is not None:
        entry["value"] = value
    return entry


def test_each_queueable_action_lands(tmp_path, monkeypatch, capsys):
    """The five that edit, all in one batch, because the page queues them that way."""
    _seeded(tmp_path, monkeypatch, capsys)
    ops = _ops_file(
        tmp_path,
        _op("add-tag", value="nightly"),
        _op("set-match", value="values"),
        _op("edit-question", value="How many orders were placed?"),
    )

    code, payload, _ = _run(
        tmp_path, monkeypatch, capsys, _apply_argv(ops, "orders", "--confirm-replace")
    )

    assert code == 0 and payload["summary"]["replaced"] == 1
    item = next(i for i in _items(tmp_path) if i.id == "orders-count")
    assert "nightly" in item.tags
    assert item.match == "values"
    assert item.query == "How many orders were placed?"


def test_withdrawing_confirmation_clears_the_signature_and_keeps_the_statement(
    tmp_path, monkeypatch, capsys
):
    """The claim being withdrawn is that somebody verified it, not that the statement existed. A
    signature left on an unconfirmed item names a person for a claim the file no longer makes."""
    _seeded(tmp_path, monkeypatch, capsys)
    item_path = _item_file(tmp_path, query=QUERY, sql=SQL)
    _run(tmp_path, monkeypatch, capsys, _save_argv(item_path, "orders", "--confirm-replace"))
    ops = _ops_file(tmp_path, _op("withdraw-confirmation", item_id=golden_author._slug(QUERY)))

    code, _, _ = _run(
        tmp_path, monkeypatch, capsys, _apply_argv(ops, "orders", "--confirm-replace")
    )

    assert code == 0
    item = next(i for i in _items(tmp_path) if i.id == golden_author._slug(QUERY))
    assert item.expected.sql_confirmed is False
    assert item.confirmed_by is None
    assert item.expected.sql == SQL


def test_two_ops_on_one_item_both_land(tmp_path, monkeypatch, capsys):
    """Folded onto the running edit rather than the file's copy, or the second would overwrite the
    first with a version of the item that never saw it."""
    _seeded(tmp_path, monkeypatch, capsys)
    ops = _ops_file(tmp_path, _op("add-tag", value="a"), _op("add-tag", value="b"))

    code, _, _ = _run(
        tmp_path, monkeypatch, capsys, _apply_argv(ops, "orders", "--confirm-replace")
    )

    assert code == 0
    item = next(i for i in _items(tmp_path) if i.id == "orders-count")
    assert {"a", "b"} <= set(item.tags)


def test_an_op_naming_an_id_the_file_does_not_hold_is_refused(tmp_path, monkeypatch, capsys):
    """A page rendered against a dataset that has since moved. Succeeding quietly would report an
    edit that never happened."""
    _seeded(tmp_path, monkeypatch, capsys)
    ops = _ops_file(tmp_path, _op("add-tag", item_id="never-existed", value="t"))

    code, _, err = _run(tmp_path, monkeypatch, capsys, _apply_argv(ops))

    assert code == 2
    assert "never-existed" in err


def test_a_verb_the_page_may_not_queue_is_refused(tmp_path, monkeypatch, capsys):
    """The parser refuses this too. Kept here because this is the layer that writes."""
    _seeded(tmp_path, monkeypatch, capsys)
    ops = _ops_file(tmp_path, {"op": "confirm", "dataset": "orders", "id": "orders-count"})

    code, _, err = _run(tmp_path, monkeypatch, capsys, _apply_argv(ops))

    assert code == 2
    assert "confirm" in err
    assert [i.expected.sql_confirmed for i in _items(tmp_path)] == [False, False]


def test_an_item_queued_for_both_an_edit_and_a_removal_is_refused(tmp_path, monkeypatch, capsys):
    """Neither order is obviously right, so neither is chosen for them."""
    _seeded(tmp_path, monkeypatch, capsys)
    ops = _ops_file(tmp_path, _op("add-tag", value="t"), _op("remove-item"))

    code, _, err = _run(
        tmp_path, monkeypatch, capsys, _apply_argv(ops, "orders", "--confirm-replace")
    )

    assert code == 2
    assert "orders-count" in err
    assert len(_items(tmp_path)) == 2


def test_a_dataset_name_carrying_a_separator_is_refused_by_this_door_too(
    tmp_path, monkeypatch, capsys
):
    """`apply` builds a path from the stem exactly as the other two doors do, so it is guarded
    exactly as they are — and it is the door that deletes."""
    _seeded(tmp_path, monkeypatch, capsys)
    neighbour = _neighbour(tmp_path)
    ops = _ops_file(tmp_path, _op("remove-item"))

    code, _, err = _run(
        tmp_path, monkeypatch, capsys, _apply_argv(ops, "../datasource", "--confirm-replace")
    )

    assert code == 2
    assert "dataset" in err
    assert neighbour.exists()


# --- The convention check (#264) ----------------------------------------------------------------
#
# Measured rather than imagined. On the first real dataset authored against a live warehouse, nine
# of fifteen items failed and six were one mistake: the answer keys filtered on one timestamp column
# where that profile's own examples used another, 24 times out of 36. The generator read those
# examples, followed the convention, and was marked wrong by a key that had never looked at them.


def _example(sql: str, question: str = QUERY):
    """Stand in for the ranker, which shells out to a CLI these tests do not have."""
    return {"question": question, "sql": sql}


def test_a_key_that_departs_from_the_profiles_examples_stops_before_writing(
    tmp_path, monkeypatch, capsys
):
    """The write is held, not undone. A warning that arrives once the answer key is on disk is a
    warning about a file the reader now has to decide whether to undo — and the append-only rule
    makes undoing it a second confirmation."""
    monkeypatch.setattr(
        golden_author,
        "_nearest_example",
        lambda profile, question: _example(
            "SELECT COUNT(*) AS order_count FROM orders WHERE created_at >= '2024-01-01'"
        ),
    )
    item = _item_file(
        tmp_path,
        sql="SELECT COUNT(*) AS order_count FROM orders WHERE placed_at >= '2024-01-01'",
    )

    code, payload, _ = _run(tmp_path, monkeypatch, capsys, _save_argv(item))

    assert code == golden_author._NEEDS_CONFIRMATION
    assert payload["added"] == []
    divergence = payload["needs_confirmation_convention"]
    assert divergence["example_sql"].count("created_at")
    assert [claim["name"] for claim in divergence["claims"]] == ["filter_predicates"]
    # Nothing reached disk, so there is nothing for the person to undo if they say no.
    assert not (tmp_path / PROFILE / "golden_datasets").exists()


def test_the_departure_is_written_once_somebody_says_it_is_deliberate(
    tmp_path, monkeypatch, capsys
):
    """Reported and never enforced. A golden item may legitimately depart from convention — that is
    sometimes exactly why one is written — so the check asks and the person decides."""
    monkeypatch.setattr(
        golden_author,
        "_nearest_example",
        lambda profile, question: _example(
            "SELECT COUNT(*) AS order_count FROM orders WHERE created_at >= '2024-01-01'"
        ),
    )
    item = _item_file(
        tmp_path,
        sql="SELECT COUNT(*) AS order_count FROM orders WHERE placed_at >= '2024-01-01'",
    )

    code, _, _ = _run(tmp_path, monkeypatch, capsys, _save_argv(item, "orders", "--confirm-convention"))

    assert code == 0
    assert _items(tmp_path)[0].expected.sql.count("placed_at")


def test_a_key_that_matches_the_convention_is_written_without_a_question(
    tmp_path, monkeypatch, capsys
):
    """The check has to be silent when there is nothing to say, or it becomes a prompt people learn
    to click through."""
    monkeypatch.setattr(
        golden_author, "_nearest_example", lambda profile, question: _example(SQL)
    )

    code, _, _ = _run(tmp_path, monkeypatch, capsys, _save_argv(_item_file(tmp_path)))

    assert code == 0 and _items(tmp_path)[0].expected.sql == SQL


def test_the_check_never_costs_a_save_when_it_cannot_run(tmp_path, monkeypatch, capsys):
    """Total by construction. No model, no CLI, a profile mid-rebuild — every one of them returns no
    opinion, because a check that could refuse a save would be a new way to fail to write down a
    correct answer."""

    def _explodes(profile, question):
        raise RuntimeError("no model here")

    monkeypatch.setattr(golden_author, "_nearest_example", _explodes)

    code, _, _ = _run(tmp_path, monkeypatch, capsys, _save_argv(_item_file(tmp_path)))

    assert code == 0 and _items(tmp_path)[0].expected.sql == SQL


def test_only_the_claims_that_change_which_rows_are_counted_are_reported(
    tmp_path, monkeypatch, capsys
):
    """Two statements answering one question legitimately differ in ordering and limit, and
    reporting those would make the check noise a person learns to click through."""
    monkeypatch.setattr(
        golden_author,
        "_nearest_example",
        lambda profile, question: _example(
            "SELECT status, COUNT(*) AS n FROM orders GROUP BY status ORDER BY n DESC LIMIT 5"
        ),
    )
    item = _item_file(
        tmp_path, sql="SELECT status, COUNT(*) AS n FROM orders GROUP BY status ORDER BY status"
    )

    code, _, _ = _run(tmp_path, monkeypatch, capsys, _save_argv(item))

    assert code == 0, "ordering and limit alone are not a departure worth stopping for"
