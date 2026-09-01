"""Both doors, end to end, over one dataset — the flow the feature contract describes.

The unit file beside this one pins each door's own behavior; what it cannot show is the shape a
profile is actually left in after a team uses this feature, because that shape is the two doors
interleaved. A question bank arrives, every question lands unconfirmed, somebody asks one of them,
looks at the answer and saves it — and the dataset the runner then reads holds both kinds at once.
That last read is the assertion that matters: the writer's only claim to correctness is that
AH-100's reader, the one `/agami-eval` uses, accepts what it wrote.

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

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import golden_author  # noqa: E402
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
        tmp_path, monkeypatch, capsys, ["parse", "--csv", _write(tmp_path, "bank.csv", QUESTION_BANK)]
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
        ["import", "--profile", PROFILE, "--dataset", DATASET, "--rows", rows_file,
         "--description", "Order-volume questions over the sample store database."],
    )
    assert import_code == 0
    assert imported["summary"] == {"added": 3, "replaced": 0}

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
    assert saved["summary"] == {"added": 0, "replaced": 1}

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
