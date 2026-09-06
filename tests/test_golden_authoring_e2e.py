"""Both doors, end to end, over one dataset — the flow the feature contract describes.

The unit file beside this one pins each door's own behavior; what it cannot show is the shape a
profile is actually left in after a team uses this feature, because that shape is the two doors
interleaved. A question bank arrives, every question lands unconfirmed, somebody asks one of them,
looks at the answer and saves it — and the dataset the runner then reads holds both kinds at once.
That last read is the assertion that matters: the writer's only claim to correctness is that
AH-100's reader, the one `/agami-eval` uses, accepts what it wrote.

AH-111 adds a third way in and no third writer: a reconcile run's agreeing rows are promoted
through this same save door. Those tests go further than a read-back — they hand the written item
to the comparator `/agami-eval` scores with, because a promoted row whose band its own recorded
result falls outside of would fail the next run as a false alarm on the day it was written.

The script is driven in-process (`golden_author.main`) rather than through a subprocess so that a
failure surfaces as a traceback in the code under test rather than as a non-zero exit code. Nothing
is stubbed: the real column contract, the real writer, the real reader.

Every question is synthetic, over the shipped sample `store` database (orders / customers /
channels), so nothing here names a real dataset, table or question.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
# The comparator parses the answer key's statement to find out whether the author ordered the rows.
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import golden_author  # noqa: E402
import reconcile  # noqa: E402
from execute_sql import ExecResult  # noqa: E402
from semantic_model.comparator import compare_result_sets  # noqa: E402
from semantic_model.golden import load_golden_datasets  # noqa: E402

PROFILE = "demo"
DATASET = "orders"

# The question bank as an analyst hands it over: one column of questions, one note column the
# contract does not know (and must leave alone), and one row that already carries a tag.
QUESTION_BANK = """NL_Question,Owner note,tags
How many orders have been placed?,the headline number,"orders,smoke"
How many paid orders came through each channel in 2024?,split by channel,orders
How many customers have placed at least one order?,,customers
"""

PAID_BY_CHANNEL = "How many paid orders came through each channel in 2024?"
PAID_BY_CHANNEL_SQL = (
    "SELECT channel, COUNT(*) AS order_count FROM orders "
    "WHERE status = 'paid' AND placed_at >= '2024-01-01' AND placed_at < '2025-01-01' "
    "GROUP BY channel"
)
METHOD = "read on screen and accepted by the analyst who asked"


def _run(tmp_path, monkeypatch, capsys, argv: list[str]):
    """Run one verb the way the skill runs it, and return (exit code, stdout payload)."""
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    code = golden_author.main(argv)
    captured = capsys.readouterr()
    return code, (json.loads(captured.out) if captured.out.strip() else None)


def _write(tmp_path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_a_question_bank_imports_unconfirmed_and_one_verified_answer_confirms_it(
    tmp_path, monkeypatch, capsys
):
    """The whole feature in one flow: import, then save, then read the result back.

    The three acceptance points of the contract are asserted in the order a team meets them, and
    the middle one is deliberately a save against a question the import already created — that is
    the ordinary path (you import the bank, then work through it), and it is also the path that
    goes through the append-only stop, so the two-step confirmation is exercised as part of the
    flow rather than as a special case.
    """
    # --- 1. the import door: a sheet becomes items, and not one of them is confirmed ---
    parse_code, parsed = _run(
        tmp_path,
        monkeypatch,
        capsys,
        ["parse", "--csv", _write(tmp_path, "bank.csv", QUESTION_BANK)],
    )
    assert parse_code == 0
    assert parsed["summary"] == {"parsed": 3, "skipped": 0}
    # The parse is inert by construction; nothing exists to be read back yet.
    assert not (tmp_path / PROFILE / "golden_datasets").exists()

    rows_file = _write(tmp_path, "parsed.json", json.dumps(parsed))
    import_code, imported = _run(
        tmp_path,
        monkeypatch,
        capsys,
        [
            "import",
            "--profile",
            PROFILE,
            "--dataset",
            DATASET,
            "--rows",
            rows_file,
            "--description",
            "Order-volume questions over the sample store database.",
        ],
    )
    assert import_code == 0
    assert imported["summary"] == {"added": 3, "replaced": 0, "removed": 0}

    datasets, res = load_golden_datasets(PROFILE, tmp_path)
    items = {item.id: item for item in next(d.test_cases for d in datasets if d.name == DATASET)}
    assert len(items) == 3
    # Every one of them, without exception. An import writes the questions a team cares about; it
    # does not write answers, and a row that claimed to be confirmed would gate a run on nothing.
    assert not any(item.expected.sql_confirmed for item in items.values())
    assert not any(item.expected.sql for item in items.values())

    # --- 2. the save door: one answer somebody looked at, against a question already imported ---
    target = golden_author._slug(PAID_BY_CHANNEL)
    assert target in items  # the derived id is what makes this a save INTO the bank, not beside it

    item_file = _write(
        tmp_path,
        "item.json",
        json.dumps(
            {
                "query": PAID_BY_CHANNEL,
                "sql": PAID_BY_CHANNEL_SQL,
                "match": "values",
                # Carried explicitly. A replacement is wholesale — the item written is the item
                # sent — so the tag the sheet supplied survives only because the save repeats it.
                "tags": ["orders"],
                "recorded": {
                    "columns": ["channel", "order_count"],
                    "rows": [["web", 812], ["mobile", 517]],
                },
                "confirmed_by": {"method": METHOD},
            }
        ),
    )
    save_argv = ["save", "--profile", PROFILE, "--dataset", DATASET, "--item", item_file]

    # The item exists already, so the append-only rule fires first: the person is shown what is
    # there and what would take its place, and nothing is written until they say yes.
    stop_code, stop = _run(tmp_path, monkeypatch, capsys, save_argv)
    assert stop_code == 1
    (pending,) = stop["needs_confirmation"]
    assert pending["id"] == target
    assert pending["before"]["expected"]["sql_confirmed"] is False
    assert pending["after"]["expected"]["sql"] == PAID_BY_CHANNEL_SQL

    saved_code, saved = _run(tmp_path, monkeypatch, capsys, [*save_argv, "--confirm-replace"])
    assert saved_code == 0
    assert saved["summary"] == {"added": 0, "replaced": 1, "removed": 0}

    # --- 3. imported and saved together, read back through the reader the runner uses ---
    datasets, res = load_golden_datasets(PROFILE, tmp_path)
    assert res.ok, res.errors
    assert not [f for f in res.findings if (f.locator or "").startswith(f"{DATASET}.yaml")]

    dataset = next(d for d in datasets if d.name == DATASET)
    assert dataset.description == "Order-volume questions over the sample store database."
    # The replacement landed in place rather than at the end: the id is the key results already
    # hang off, so a save must not reorder the file it merged into.
    assert [item.id for item in dataset.test_cases] == [item_id for item_id in items]

    confirmed = next(item for item in dataset.test_cases if item.id == target)
    assert confirmed.expected.sql_confirmed is True
    assert confirmed.expected.sql == PAID_BY_CHANNEL_SQL
    assert confirmed.match == "values"
    assert confirmed.tags == ["orders"]
    # The receipt: what the answer looked like on the day, and how it was vouched for.
    assert confirmed.recorded.columns == ["channel", "order_count"]
    assert confirmed.recorded.rows == [["web", 812], ["mobile", 517]]
    assert confirmed.confirmed_by.method == METHOD
    assert confirmed.recorded.at and confirmed.confirmed_by.at

    # …and the other two are untouched: still questions with no answer, still unable to gate.
    others = [item for item in dataset.test_cases if item.id != target]
    assert [item.expected.sql_confirmed for item in others] == [False, False]


# --- the promotion door: a reconcile run's agreeing rows, written through the same save ---------

# The dataset a promotion lands in. Separate from the imported bank on purpose: the rows a
# reconcile run agreed on are their own body of evidence, and the two are only forced together
# when a promoted question happens to be one somebody already imported.
RECONCILED = "reconciled"

Q3_REVENUE = "What was total revenue in Q3 2025?"
Q3_REVENUE_SQL = (
    "SELECT SUM(o.amount) AS total_revenue FROM orders o "
    "WHERE o.placed_at >= '2025-07-01' AND o.placed_at < '2025-10-01'"
)
Q3_ACTUAL = 3890000.0
TOLERANCE = 0.01

# The two provenance shapes the skill documents. They differ in words rather than only in tone,
# because a reader a year from now has to be able to tell "the dashboard agreed" from "the
# dashboard disagreed and a person overruled it".
AGREED_METHOD = "reconciled against the finance dashboard on 2026-08-31; agreed within ±1%"
RESOLVED_METHOD = (
    "reconciled against the finance dashboard on 2026-08-31; disagreed beyond ±1%, "
    "resolved in agami's favour by the analyst"
)


def _promotion_item(
    query: str, sql: str, actual: float, method: str, column: str = "total_revenue"
) -> dict:
    """One kept row as the skill writes it out — `id` omitted, band from the helper.

    The band goes through `reconcile.band` rather than being written out here for the same reason
    the skill is told to call it: two spellings of ±1% is two answers to what the run's tolerance
    meant, and this one would be the one nobody runs.
    """
    return {
        "query": query,
        "sql": sql,
        "match": "bounded",
        "bounds": reconcile.band(actual, tolerance=TOLERANCE),
        "recorded": {"columns": [column], "rows": [[actual]]},
        "tags": ["reconciled"],
        "confirmed_by": {"method": method},
    }


def test_an_agreeing_row_promotes_and_scores_on_its_own_next_run(tmp_path, monkeypatch, capsys):
    """The promotion end to end, ending on the check that matters most.

    A read-back only proves the file parses. What a promotion actually claims is that this item can
    gate a run, so the last step hands the written `match` and `bounds` to the comparator
    `/agami-eval` scores with, over the very result the band was drawn around. An item that scored
    anything but a clean pass there would fail its next run as a false alarm on the day it was
    written — which is exactly the trap the skill's relativity guidance exists to avoid, reached by
    a different road.
    """
    item_file = _write(
        tmp_path,
        "promotion.json",
        json.dumps(_promotion_item(Q3_REVENUE, Q3_REVENUE_SQL, Q3_ACTUAL, AGREED_METHOD)),
    )
    code, saved = _run(
        tmp_path,
        monkeypatch,
        capsys,
        ["save", "--profile", PROFILE, "--dataset", RECONCILED, "--item", item_file],
    )
    assert code == 0
    assert saved["summary"] == {"added": 1, "replaced": 0, "removed": 0}

    datasets, res = load_golden_datasets(PROFILE, tmp_path)
    assert res.ok, res.errors
    (promoted,) = next(d.test_cases for d in datasets if d.name == RECONCILED)

    # It came through the save door, so it is confirmed and it carries the statement it verified.
    assert promoted.expected.sql_confirmed is True
    assert promoted.expected.sql == Q3_REVENUE_SQL
    # A reconciled number legitimately moves, so it is banded rather than pinned.
    assert promoted.match == "bounded"
    assert promoted.bounds.min_value < Q3_ACTUAL < promoted.bounds.max_value
    # The receipt is the run's own recorded result, forwarded rather than rebuilt from a number.
    assert promoted.recorded.columns == ["total_revenue"]
    assert promoted.recorded.rows == [[Q3_ACTUAL]]
    assert promoted.tags == ["reconciled"]
    # Provenance names the source, the day and the tolerance — the whole of what makes the claim
    # auditable later.
    for part in ("the finance dashboard", "2026-08-31", "±1%"):
        assert part in promoted.confirmed_by.method

    # …and it passes its own next run.
    result = ExecResult(columns=list(promoted.recorded.columns), rows=[(Q3_ACTUAL,)])
    score = compare_result_sets(
        result,
        result,
        match=promoted.match,
        bounds=promoted.bounds,
        golden_sql=promoted.expected.sql,
    )
    assert score.status == "scored"
    assert score.accuracy == 1.0

    # A promotion writes the answer key and nothing else. The examples library is a different
    # skill's business, and a reconcile run that quietly taught the model would be one.
    assert not (tmp_path / PROFILE / "prompt_examples").exists()


def test_a_relative_question_is_refused_until_it_names_the_window_it_meant(
    tmp_path, monkeypatch, capsys
):
    """The common case, not the exotic one: the skill's own question generator produces "over the
    last 30 days" from a label reading `Mean order size last 30 days`, and the statement that
    answered it names the thirty days that were current when it ran.

    The fix is the question, never the SQL. Anchoring the statement to `CURRENT_DATE` would also
    clear the lint and would band a sliding window around one day's value; the edit asserted here
    leaves the statement exactly as it ran, and the derived id follows the new wording — which is
    the thing that makes the edit real rather than cosmetic.
    """
    relative = "What's the average order size over the last 30 days?"
    named = "What's the average order size in August 2026?"
    sql = (
        "SELECT AVG(o.amount) AS avg_order_size FROM orders o "
        "WHERE o.placed_at >= '2026-08-01' AND o.placed_at < '2026-09-01'"
    )
    item = _promotion_item(relative, sql, 84.5, AGREED_METHOD, column="avg_order_size")

    refused_file = _write(tmp_path, "relative.json", json.dumps(item))
    argv = ["save", "--profile", PROFILE, "--dataset", RECONCILED, "--item", refused_file]
    code, payload = _run(tmp_path, monkeypatch, capsys, argv)
    assert code == 2
    assert payload is None
    assert not (tmp_path / PROFILE / "golden_datasets").exists()

    edited_file = _write(tmp_path, "named.json", json.dumps({**item, "query": named}))
    code, saved = _run(
        tmp_path,
        monkeypatch,
        capsys,
        ["save", "--profile", PROFILE, "--dataset", RECONCILED, "--item", edited_file],
    )
    assert code == 0

    datasets, res = load_golden_datasets(PROFILE, tmp_path)
    assert res.ok, res.errors
    (promoted,) = next(d.test_cases for d in datasets if d.name == RECONCILED)
    # The edited question is what was written, its id was derived from the edit, and the statement
    # is byte-for-byte the one that ran.
    assert promoted.query == named
    assert promoted.id == golden_author._slug(named)
    assert promoted.id != golden_author._slug(relative)
    assert promoted.expected.sql == sql


def test_a_promotion_onto_an_existing_question_stops_until_it_is_confirmed(
    tmp_path, monkeypatch, capsys
):
    """The append-only stop, reached the way a promotion reaches it.

    The second write here is the resolution path — the same question, now vouched for as a
    disagreement a person overruled — which is exactly the case where seeing the `before` matters:
    the item being replaced says the dashboard agreed, and the one replacing it says it did not.
    """
    argv = ["save", "--profile", PROFILE, "--dataset", RECONCILED, "--item"]
    first = _write(
        tmp_path,
        "agreed.json",
        json.dumps(_promotion_item(Q3_REVENUE, Q3_REVENUE_SQL, Q3_ACTUAL, AGREED_METHOD)),
    )
    assert _run(tmp_path, monkeypatch, capsys, [*argv, first])[0] == 0

    # The tag is deliberately NOT repeated on the replacement. A replacement is wholesale — the
    # item sent is the item written — and an item that repeated it could not show that.
    resolution = _promotion_item(Q3_REVENUE, Q3_REVENUE_SQL, Q3_ACTUAL, RESOLVED_METHOD)
    del resolution["tags"]
    second = _write(tmp_path, "resolved.json", json.dumps(resolution))
    stop_code, stop = _run(tmp_path, monkeypatch, capsys, [*argv, second])
    assert stop_code == 1
    (pending,) = stop["needs_confirmation"]
    assert pending["id"] == golden_author._slug(Q3_REVENUE)
    # Both sides, and the two provenance shapes are what tells them apart.
    assert pending["before"]["confirmed_by"]["method"] == AGREED_METHOD
    assert pending["after"]["confirmed_by"]["method"] == RESOLVED_METHOD
    assert AGREED_METHOD != RESOLVED_METHOD

    # Nothing was written by the stop: the file on disk still says the dashboard agreed.
    datasets, _ = load_golden_datasets(PROFILE, tmp_path)
    (still,) = next(d.test_cases for d in datasets if d.name == RECONCILED)
    assert still.confirmed_by.method == AGREED_METHOD

    replaced_code, replaced = _run(
        tmp_path, monkeypatch, capsys, [*argv, second, "--confirm-replace"]
    )
    assert replaced_code == 0
    assert replaced["summary"] == {"added": 0, "replaced": 1, "removed": 0}

    datasets, res = load_golden_datasets(PROFILE, tmp_path)
    assert res.ok, res.errors
    (resolved,) = next(d.test_cases for d in datasets if d.name == RECONCILED)
    assert resolved.confirmed_by.method == RESOLVED_METHOD
    # …and the tag the first item carried is GONE, because the second one did not repeat it. This
    # is why the skill is told to read the `before` and carry forward what still applies: a
    # replacement that forgets a key does not merge it, it drops it.
    assert resolved.tags == []


# The keys `_save` actually reads off a promotion payload. A documented key outside this set is a
# key the door drops in silence, which is the drift the test below exists to catch.
_KEYS_THE_DOOR_READS = {
    "id",
    "query",
    "sql",
    "match",
    "bounds",
    "must_filter",
    "recorded",
    "tags",
    "confirmed_by",
}

RECONCILE_SKILL = (
    REPO_ROOT / "plugins" / "agami" / "skills" / "agami-reconcile" / "SKILL.md"
).read_text(encoding="utf-8")


def _documented_promotion_item() -> dict:
    """The skill's own item block, with the two placeholders filled the way a run fills them."""
    section = RECONCILE_SKILL.split("#### What gets written, per row kept as a test")[1]
    item = json.loads(section.split("```json")[1].split("```")[0])
    item["sql"] = Q3_REVENUE_SQL
    item["confirmed_by"]["method"] = item["confirmed_by"]["method"].replace(
        "<the run's tolerance>", "1%"
    )
    return item


def test_the_item_the_skill_documents_is_the_item_the_save_door_reads(
    tmp_path, monkeypatch, capsys
):
    """The skill's JSON block, run through the real door instead of re-typed beside it.

    Every other test in this file builds the promotion payload by hand, which means the shape the
    skill tells a model to write and the shape `_save` reads could drift apart without one of them
    failing. This is the only place the two are the same bytes.
    """
    item = _documented_promotion_item()
    assert set(item) <= _KEYS_THE_DOOR_READS, set(item) - _KEYS_THE_DOOR_READS
    # The band in the block is the helper's own output, not arithmetic somebody wrote in prose.
    assert item["bounds"] == reconcile.band(item["recorded"]["rows"][0][0], tolerance=TOLERANCE)

    code, saved = _run(
        tmp_path,
        monkeypatch,
        capsys,
        [
            "save",
            "--profile",
            PROFILE,
            "--dataset",
            RECONCILED,
            "--item",
            _write(tmp_path, "documented.json", json.dumps(item)),
        ],
    )
    assert code == 0
    assert saved["summary"] == {"added": 1, "replaced": 0, "removed": 0}

    datasets, res = load_golden_datasets(PROFILE, tmp_path)
    assert res.ok, res.errors
    (promoted,) = next(d.test_cases for d in datasets if d.name == RECONCILED)

    # Each documented key arrived where the door puts it — `id` derived from the question the
    # block carries, which is what makes the omission in the block deliberate rather than lossy.
    assert promoted.id == golden_author._slug(item["query"])
    assert promoted.query == item["query"]
    assert promoted.expected.sql == item["sql"]
    assert promoted.expected.sql_confirmed is True
    assert promoted.match == item["match"]
    assert promoted.bounds.model_dump(exclude_none=True) == item["bounds"]
    assert promoted.recorded.columns == item["recorded"]["columns"]
    assert promoted.recorded.rows == [list(row) for row in item["recorded"]["rows"]]
    assert promoted.tags == item["tags"]
    assert promoted.confirmed_by.method == item["confirmed_by"]["method"]


# --- AH-112: the explorer page's queued actions reach the file -------------
#
# The page queues, the parser reads the block back, and this door writes it. The two criteria the
# round trip exists to prove are that a queued change survives all three, and that a removal shows
# what is about to be lost before it goes.


def _seed(tmp_path, monkeypatch, capsys) -> None:
    """One confirmed item with a receipt, and one unconfirmed one to remove."""
    rows = [
        {"id": "orders-count", "query": "How many orders?", "tags": ["smoke"]},
        {"id": "orders-stale", "query": "An old question nobody kept", "tags": []},
    ]
    rows_path = _write(tmp_path, "rows.json", json.dumps({"rows": rows}))
    code, _ = _run(
        tmp_path,
        monkeypatch,
        capsys,
        ["import", "--profile", PROFILE, "--dataset", DATASET, "--rows", rows_path],
    )
    assert code == 0


def _block(*ops: dict) -> str:
    """The back-channel block exactly as the page's `generateFeedback()` builds it."""
    return f"profile: {PROFILE}\ngolden-ops:\n{json.dumps(list(ops))}\ndone\n"


def test_a_queued_tag_change_round_trips_from_the_page_to_the_file(tmp_path, monkeypatch, capsys):
    """AH-112 SC7. Queued in the page, parsed from the block, applied through this door, and back
    out of the reader the runner uses — the whole path, not the three halves of it."""
    import parse_golden_feedback

    _seed(tmp_path, monkeypatch, capsys)

    # --- 1. The page hands back a block, and the parser reads it.
    data, anomalies, needs = parse_golden_feedback.parse(
        _block({"op": "add-tag", "dataset": DATASET, "id": "orders-count", "value": "nightly"})
    )
    assert anomalies == [] and needs is None
    assert data["profile"] == PROFILE

    # --- 2. The parser's own document goes through the write door, byte for byte as the skill
    # redirects it. Re-wrapping it here would test a shape nothing produces, and would have hidden
    # the door reading `ops` from the top level while the parser puts it under `data`.
    ops_path = _write(
        tmp_path,
        "ops.json",
        json.dumps({"ok": True, "data": data, "anomalies": anomalies, "needs_judgment": needs}),
    )
    code, payload = _run(
        tmp_path,
        monkeypatch,
        capsys,
        [
            "apply",
            "--profile",
            PROFILE,
            "--dataset",
            DATASET,
            "--ops",
            ops_path,
            "--confirm-replace",
        ],
    )
    assert code == 0
    assert payload["summary"] == {"added": 0, "replaced": 1, "removed": 0}

    # --- 3. And it is there on the next read, which is what a re-render would show.
    datasets, res = load_golden_datasets(PROFILE, tmp_path)
    assert res.ok
    item = next(i for i in datasets[0].test_cases if i.id == "orders-count")
    assert item.tags == ["nightly", "smoke"]


def test_a_queued_removal_shows_the_item_before_it_is_applied(tmp_path, monkeypatch, capsys):
    """AH-112 SC10. A removal has no `after`, so the `before` is the whole of what somebody is
    agreeing to lose. Deciding from an id alone is deciding from a slug."""
    _seed(tmp_path, monkeypatch, capsys)
    ops_path = _write(
        tmp_path,
        "ops.json",
        json.dumps({"ops": [{"op": "remove-item", "dataset": DATASET, "id": "orders-stale"}]}),
    )
    argv = ["apply", "--profile", PROFILE, "--dataset", DATASET, "--ops", ops_path]

    code, payload = _run(tmp_path, monkeypatch, capsys, argv)

    assert code == 1
    shown = payload["needs_confirmation_removals"]
    assert [row["id"] for row in shown] == ["orders-stale"]
    assert shown[0]["before"]["query"] == "An old question nobody kept"
    # Nothing went yet: a preview that had already deleted the item would not be a preview.
    assert {i.id for i in load_golden_datasets(PROFILE, tmp_path)[0][0].test_cases} == {
        "orders-count",
        "orders-stale",
    }

    code, payload = _run(tmp_path, monkeypatch, capsys, [*argv, "--confirm-replace"])

    assert code == 0
    assert payload["removed"] == ["orders-stale"]
    assert {i.id for i in load_golden_datasets(PROFILE, tmp_path)[0][0].test_cases} == {
        "orders-count"
    }


def test_the_page_cannot_confirm_an_item_through_this_door_either(tmp_path, monkeypatch, capsys):
    """The rule holds at all three layers. The parser refuses the block, and if a caller skipped
    the parser and hand-wrote the ops, `withdraw-confirmation` is the only verb touching the flag
    and it only ever clears it."""
    import parse_golden_feedback

    _seed(tmp_path, monkeypatch, capsys)
    data, _, needs = parse_golden_feedback.parse(
        _block(
            {
                "op": "add-tag",
                "dataset": DATASET,
                "id": "orders-count",
                "value": "t",
                "sql_confirmed": True,
            }
        )
    )

    assert needs["kind"] == "confirmation_cannot_be_granted"
    assert data["ops"] == []

    # And the door itself knows no verb that could grant it.
    assert "sql_confirmed" not in golden_author._QUEUEABLE
    for verb in golden_author._QUEUEABLE:
        assert verb != "confirm"


def test_a_block_the_parser_refused_applies_nothing_at_the_door_either(
    tmp_path, monkeypatch, capsys
):
    """The parser refuses at the block level, and the door reads that verdict rather than the ops
    beside it. A caller who piped the refusal straight through would otherwise apply the neighbours
    of the op that was refused."""
    import parse_golden_feedback

    _seed(tmp_path, monkeypatch, capsys)
    data, anomalies, needs = parse_golden_feedback.parse(
        _block(
            {"op": "add-tag", "dataset": DATASET, "id": "orders-count", "value": "keep"},
            {
                "op": "add-tag",
                "dataset": DATASET,
                "id": "orders-count",
                "value": "x",
                "sql_confirmed": True,
            },
        )
    )
    assert needs["kind"] == "confirmation_cannot_be_granted"
    ops_path = _write(
        tmp_path,
        "ops.json",
        json.dumps({"ok": True, "data": data, "anomalies": anomalies, "needs_judgment": needs}),
    )

    code, _ = _run(
        tmp_path,
        monkeypatch,
        capsys,
        [
            "apply",
            "--profile",
            PROFILE,
            "--dataset",
            DATASET,
            "--ops",
            ops_path,
            "--confirm-replace",
        ],
    )

    assert code == 2
    item = next(
        i
        for i in load_golden_datasets(PROFILE, tmp_path)[0][0].test_cases
        if i.id == "orders-count"
    )
    assert "keep" not in item.tags
