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
    assert payload["summary"] == {"added": 2, "replaced": 0}
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
    assert doc["description"] == ""


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
ANCHORED_SQL = (
    "SELECT COUNT(*) AS order_count FROM orders WHERE placed_at >= CURRENT_DATE - 7"
)


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
        tmp_path, monkeypatch, capsys, _save_argv(_item_file(tmp_path), "orders", "--confirm-replace")
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
