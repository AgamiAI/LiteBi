#!/usr/bin/env python3
"""Turn a spreadsheet of questions into golden-dataset rows, and print them for confirmation.

The `agami-save-golden` skill has two doors and this helper is the deterministic half of the first
one. An analyst's question bank lives in a spreadsheet; a golden dataset item is `id`, `query` and
an answer key. Getting from one to the other is column matching, id derivation and number
normalization — all of it decidable, and none of it something a model should be reading a file to
do. What the skill keeps is the judgement: which sheet, and whether the parsed rows are right.

**`parse` writes nothing.** That is the point of the verb rather than an accident of it. The
contract says the rows are confirmed before anything is written, and the only way that is a fact
about the software rather than a claim about how a skill behaves is for the reading step to have
no write in it at all. The write door is a separate verb.

**Nothing here confirms an answer key.** A sheet may carry a statement column and it is carried
through as `sql`, but nobody ran it, so an imported row is unverified by construction — the
confirmed-only rule is what makes a green golden run mean something, and a spreadsheet column is
not a person who looked at a result.

Usage:

    python3 golden_author.py parse --csv /path/to/question-bank.csv

Stdout is always one JSON document; every refusal and every warning goes to stderr with the prefix
below, so a caller can parse the one and strip the other. Stdlib only, plus `reconcile.parse_value`
for the expected-value column.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

# Sibling scripts are imported plainly, and this is what makes that work in every layout the
# plugin ships in — the marketplace cache invokes these scripts by absolute path, where the
# interpreter's own `sys.path[0]` is not something to rely on.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import reconcile  # noqa: E402

# Marks the lines a caller is meant to strip — the refusals and the warnings, this helper talking
# about itself.
_PREFIX = "agami-save-golden:"

# What an exit code means. `_NEEDS_CONFIRMATION` belongs to the write door: a change the person has
# not agreed to yet is neither a success nor a breakage, and a pipeline that treats it as either
# would be wrong in both directions.
_NEEDS_CONFIRMATION = 1
_CANNOT_START = 2

# The column contract. Matching is EXACT against these alias sets after folding, and never fuzzy:
# a header this contract does not know is the analyst's own note column, which is only safe to
# ignore silently while no match can be approximate. `query` names the question and `query sql`
# names the statement, which is exactly the kind of near-collision a fuzzy matcher gets wrong.
_ALIASES: dict[str, frozenset[str]] = {
    "query": frozenset({"question", "query", "nl", "nl question", "prompt", "ask"}),
    "id": frozenset({"id", "key", "item id", "case id"}),
    "expected_value": frozenset({"expected", "expected value", "answer", "value"}),
    "sql": frozenset({"sql", "statement", "query sql", "expected sql"}),
    "tags": frozenset({"tags", "tag", "labels"}),
}

# How long a derived id may be. An id is a filename-safe key a person reads in a diff and types
# into a `--rerun` argument, and a whole question spelled out is neither.
_MAX_SLUG = 60


def _stop(reason: str) -> None:
    """Say why the parse cannot start."""
    print(f"{_PREFIX} {reason}", file=sys.stderr)


def _warn(reason: str) -> None:
    """Say something about a parse that still finished.

    Same stream and prefix as `_stop`, and a separate name because a call reading `_stop(...)`
    where nothing stops misdescribes the control flow at the call site.
    """
    print(f"{_PREFIX} {reason}", file=sys.stderr)


def _fold(header: str) -> str:
    """One header cell as the contract sees it.

    Spreadsheets spell the same column `NL_Question`, `nl question` and ` Expected-Value `, and the
    difference is typography rather than meaning. Underscores, hyphens and runs of whitespace all
    fold to one space so that a single alias covers every spelling of it.
    """
    return re.sub(r"[\s_-]+", " ", header).strip().lower()


def _slug(query: str) -> str:
    """A deterministic id for a question that has none.

    Deterministic is the whole requirement. `GoldenItem.item_key` is exactly the id, so the id IS
    the append-only duplicate check: re-importing the same sheet has to derive the same ids and
    land on the duplicate path, where sequential ids would append a second copy of every row under
    fresh keys and the append-only rule would never fire on the import door at all.

    Truncated at a `-` boundary rather than mid-word, because a person reads these in a diff and a
    key ending in a half-word reads as corruption. A single word longer than the limit has no
    boundary to trim back to and is cut where it falls.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")
    if len(slug) <= _MAX_SLUG:
        return slug
    head = slug[:_MAX_SLUG]
    # Already on a boundary: the next character is the separator, so nothing is cut in half.
    if slug[_MAX_SLUG] != "-":
        head = head.rpartition("-")[0] or head
    return head.strip("-")


def _columns(header: list[str]) -> dict[str, int]:
    """Which column holds which field, by index.

    First match wins: a sheet with two columns folding to the same alias is one column and one
    duplicate, and the person put the real one first.
    """
    found: dict[str, int] = {}
    for index, cell in enumerate(header):
        folded = _fold(cell)
        for field, aliases in _ALIASES.items():
            if folded in aliases and field not in found:
                found[field] = index
    return found


def _cell(row: list[str], index: Optional[int]) -> str:
    """One cell, for a column the sheet may not have and a row that may stop short of it.

    A short row is ordinary — a spreadsheet exported with trailing empties trimmed — and it means
    the cell is blank rather than that the file is malformed.
    """
    if index is None or index >= len(row):
        return ""
    return row[index].strip()


def _read_rows(path: str) -> list[list[str]]:
    """The CSV as rows, blank lines included.

    Blank rows are kept because `skipped` reports a row number a person uses to find the row in
    their own sheet, and dropping anything ahead of the numbering makes every number after it
    point at the wrong line.
    """
    with Path(path).expanduser().open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.reader(handle))


def _parse_rows(header: list[str], body: list[list[str]]) -> dict[str, Any]:
    """The rows and the skips, in sheet order.

    Every row is accounted for in exactly one of the two lists. A sheet that comes back shorter
    than it went in, with nothing said about the difference, is how an import quietly loses a
    question nobody notices is missing.
    """
    columns = _columns(header)
    rows: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    # Derived ids only. An explicit id that repeats is a real duplicate in the person's own sheet,
    # and the write door's append-only path is where that gets resolved — silently suffixing it
    # here would turn a clash they need to see into two rows that both look fine.
    derived: dict[str, int] = {}

    for number, row in enumerate(body, start=1):
        query = _cell(row, columns.get("query"))
        if not query:
            skipped.append({"row": number, "reason": "empty question"})
            continue
        item_id = _cell(row, columns.get("id"))
        if not item_id:
            slug = _slug(query)
            if not slug:
                skipped.append({"row": number, "reason": "question has no usable characters"})
                continue
            derived[slug] = derived.get(slug, 0) + 1
            item_id = slug if derived[slug] == 1 else f"{slug}-{derived[slug]}"
        statement = _cell(row, columns.get("sql"))
        rows.append(
            {
                "id": item_id,
                "query": query,
                # Normalized through the reconcile parser rather than re-derived, so `$1.2M` means
                # one thing across the plugin. Unreadable is `null` and not a refusal: this value
                # is shown in the confirmation table and never becomes an answer key, so a cell
                # nobody can read costs the person nothing.
                "expected_value": reconcile.parse_value(_cell(row, columns.get("expected_value"))),
                "sql": statement or None,
                "tags": [
                    tag.strip() for tag in _cell(row, columns.get("tags")).split(",") if tag.strip()
                ],
            }
        )
    return {
        "columns": header,
        "rows": rows,
        "skipped": skipped,
        "summary": {"parsed": len(rows), "skipped": len(skipped)},
    }


def _parse(path: str) -> Optional[dict[str, Any]]:
    """The whole parse, or None having said on stderr why there is not one.

    Both refusals are the same event: no column can be identified as the question. Never a fallback
    to column 0 — a bank of ids or timestamps imported as questions produces items that fail every
    run for a reason that looks exactly like a model regression and is not one. Each refusal names
    the cells it actually read, because that list is the whole of what the person needs to rename a
    column and re-invoke.

    An alias match decides whether row 0 is the header, and `_looks_like_header` only chooses which
    sentence to refuse with. That ordering is deliberate: the alias set is exact, so a cell folding
    to `question` is a header and nothing else, whereas reconcile's heuristic reads the SECOND cell
    and answers `False` for a one-column sheet — which is the shape a question bank most often has.
    """
    all_rows = _read_rows(path)
    if not all_rows:
        _stop("this file is empty — the sheet needs a header row naming its question column")
        return None
    header = all_rows[0]
    if "query" not in _columns(header):
        cells = ", ".join(repr(cell.strip()) for cell in header)
        if reconcile._looks_like_header(header):
            _stop(
                f"no column here holds the question. Columns found: {cells}. Rename one of them "
                "to 'question' (or 'query', 'nl question', 'prompt', 'ask')"
            )
        else:
            _stop(
                "this file has no header row, so no column can be identified as the question. Its "
                f"first row reads: {cells}. Add a header naming one column 'question'"
            )
        return None
    payload = _parse_rows(header, all_rows[1:])
    if payload["skipped"]:
        # The counts are in the payload, but a person reading a terminal sees the summary line, and
        # a skip they never notice is a question missing from their dataset.
        _warn(f"{len(payload['skipped'])} row(s) were skipped — see `skipped` in the payload")
    return payload


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Author golden-dataset items from a spreadsheet.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    parse_cmd = sub.add_parser("parse", help="Read a question-bank CSV and print what it holds.")
    parse_cmd.add_argument("--csv", required=True, help="the question bank to read")

    args = parser.parse_args(argv)

    payload = _parse(args.csv)
    if payload is None:
        return _CANNOT_START
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
