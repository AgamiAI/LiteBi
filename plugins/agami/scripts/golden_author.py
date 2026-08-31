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

**Every write goes through one funnel.** `_write_items` is it: both doors call it and AH-111's
promotion will too, so the append-only rule — a change to an item that already exists renders the
before and the after and stops for an explicit yes — is a property of the software rather than a
rule each caller has to remember.

Usage:

    python3 golden_author.py parse  --csv /path/to/question-bank.csv
    python3 golden_author.py import --profile main --dataset orders --rows /path/to/parsed.json
    python3 golden_author.py save   --profile main --dataset orders --item /path/to/item.json

Stdout is always one JSON document; every refusal and every warning goes to stderr with the prefix
below, so a caller can parse the one and strip the other. The parse door is stdlib only, plus
`reconcile.parse_value` for the expected-value column; the write doors go through AH-100's models,
which is what the guarded import below is for.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Sibling scripts are imported plainly, and this is what makes that work in every layout the
# plugin ships in — the marketplace cache invokes these scripts by absolute path, where the
# interpreter's own `sys.path[0]` is not something to rely on.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Three layouts reach this script and only one of them has `packages/` on disk: a dev checkout keeps
# the source beside the plugin, a marketplace install ships `<version>/lib` and no checkout at all,
# and a pip install has the library importable already. `_agami_lib` is the one helper that knows
# all three, and every other runtime script in this directory calls it.
import _agami_lib  # noqa: E402

_agami_lib.ensure_importable()

# A sibling script and stdlib-only, so it is imported plainly: it has none of the dependencies the
# guard below exists for.
import reconcile

try:
    import agami_paths
    import yaml
    from semantic_model.golden import (
        GoldenConfirmedBy,
        GoldenDataset,
        GoldenExpected,
        GoldenItem,
        GoldenRecorded,
        _lint_relativity,
        load_golden_datasets,
    )
    from semantic_model.validator import ValidationResult
except ImportError as exc:
    # A fresh plugin install genuinely lacks these, and the traceback a bare ImportError prints
    # names an internal module rather than the thing to install.
    print(
        "golden_author's write doors need agami-core and its model extra (pydantic, pyyaml): "
        f"install `agami-core[model]` into this interpreter. ({exc})",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc

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


# ---------------------------------------------------------------------------
# The write core — one funnel, and the append-only rule lives in it
# ---------------------------------------------------------------------------


def _datasets_dir(profile: str) -> Path:
    """Where a profile's datasets live.

    The same path `run_golden_eval.py` names. A door that wrote somewhere the runner does not read
    would produce a dataset nobody ever runs, and two spellings of this path would be two answers to
    "where does one go".
    """
    return agami_paths.profile_dir(profile) / "golden_datasets"


def _drop_empty(doc: dict[str, Any], *keys: str) -> None:
    """Remove the named keys when they hold an empty list.

    `exclude_none` does not drop `[]`, so a model dumped straight out writes `tags: []`,
    `must_filter: []` and `tables_used: []` into every item — noise in a file a person reads and
    diffs, and a shape nobody would ever author by hand.
    """
    for key in keys:
        if doc.get(key) == []:
            del doc[key]


def _item_doc(item: GoldenItem) -> dict[str, Any]:
    """One case as it goes on disk, and as the confirmation prompt shows it."""
    doc = item.model_dump(mode="json", exclude_none=True)
    _drop_empty(doc, "tags", "must_filter")
    _drop_empty(doc["expected"], "tables_used")
    if "recorded" in doc:
        _drop_empty(doc["recorded"], "columns", "rows")
    return doc


def _dataset_doc(dataset: GoldenDataset) -> dict[str, Any]:
    """A whole dataset file as it goes on disk.

    `name` is excluded, and that is the one exclusion that is not cosmetic: the reader injects the
    name from the filename and REFUSES a file that declares one, so dumping the model whole would
    write a file this helper could never read back.
    """
    doc = dataset.model_dump(mode="json", exclude_none=True, exclude={"name", "test_cases"})
    doc["test_cases"] = [_item_doc(item) for item in dataset.test_cases]
    return doc


def _our_faults(res: ValidationResult, stem: str) -> list[str]:
    """The error findings this write is answerable for.

    The reader validates the whole profile, so a neighbouring dataset that was already broken lands
    in the same result. Rolling our write back because of it would restore a correct file and blame
    a locator the person never touched, so only findings against this stem count.
    """
    prefix = f"{stem}.yaml"
    return [
        finding.message
        for finding in res.findings
        if finding.severity == "error" and (finding.locator or "").startswith(prefix)
    ]


def _relativity_fault(item: GoldenItem, stem: str) -> Optional[str]:
    """AH-100's relativity lint, asked about one item before it is written.

    Called, never restated. The rule is three regexes in `semantic_model.golden`, and a second copy
    of them would drift — the failure the repo's "no second SQL parser" guardrail exists to prevent.

    What running it here buys is the timing. The reader runs the same lint on every read, so a
    question phrased against today with an answer key pinned to a fixed date would surface anyway —
    as a finding at the next run, with the file already written and whoever reads it having to work
    out that saving it was the mistake. Refused at the moment of saving, it is the one person who
    can still fix it being told.

    The lint only fires on an item that has a statement, so in practice this is a save-door rule;
    it lives in the funnel anyway, so the import door inherits it for the sheets that carry SQL.
    """
    res = ValidationResult()
    _lint_relativity(item, stem, res)
    return res.errors[0] if res.errors else None


def _rollback(path: Path, snapshot: Optional[bytes], created_dir: bool) -> None:
    """Put the tree back exactly as it was before this write.

    The original bytes rather than a re-dump of the parsed model: a file the person hand-wrote has
    to come back with their formatting, on the path where nothing was supposed to change at all. A
    file that is new goes away entirely, along with the directory if this write is what made it —
    a half-created `golden_datasets/` holding a file the reader refuses is worse than no dataset,
    because the next run reads it and reports a fault nobody meant to create.
    """
    if snapshot is not None:
        path.write_bytes(snapshot)
        return
    path.unlink()
    if created_dir and not any(path.parent.iterdir()):
        path.parent.rmdir()


def _write_items(
    profile: str,
    stem: str,
    items: list[GoldenItem],
    *,
    confirm_replace: bool,
    description: Optional[str] = None,
) -> int:
    """Merge `items` into a profile's dataset, print what happened, and return the exit code.

    THE write path. Both doors call it and AH-111 will too, so the append-only rule is enforced
    once rather than remembered by each caller. The order of the steps is the contract: nothing is
    written until the person has agreed to every replacement, and nothing survives a re-read the
    runner's own reader would refuse.
    """
    path = _datasets_dir(profile) / f"{stem}.yaml"
    snapshot = path.read_bytes() if path.exists() else None
    existing: list[GoldenItem] = []
    fields: dict[str, Any] = {}

    if snapshot is not None:
        datasets, res = load_golden_datasets(profile)
        prior = _our_faults(res, stem)
        if prior:
            # Merging into a file the reader cannot fully read would silently drop whatever it
            # could not parse — this write deleting somebody's case to make room for its own.
            _stop(
                f"{stem}.yaml cannot be read as it stands, so nothing may be merged into it: "
                f"{prior[0]}"
            )
            return _CANNOT_START
        found = next(dataset for dataset in datasets if dataset.name == stem)
        existing = list(found.test_cases)
        fields = found.model_dump(exclude={"name", "test_cases"}, exclude_none=True)

    by_id = {item.id: item for item in existing}
    added = [item for item in items if item.id not in by_id]
    replaced = [item for item in items if item.id in by_id]

    if replaced and not confirm_replace:
        # The before AND the after, because "this id already exists" is not enough for a person to
        # decide with: an answer key is the thing being overwritten, and they have to see both.
        print(
            json.dumps(
                {
                    "dataset": stem,
                    "path": str(path),
                    "added": [item.id for item in added],
                    "needs_confirmation": [
                        {
                            "id": item.id,
                            "before": _item_doc(by_id[item.id]),
                            "after": _item_doc(item),
                        }
                        for item in replaced
                    ],
                },
                indent=2,
            )
        )
        return _NEEDS_CONFIRMATION

    for item in items:
        fault = _relativity_fault(item, stem)
        if fault:
            _stop(fault)
            return _CANNOT_START

    incoming = {item.id: item for item in items}
    # A replacement lands in place: the id is the key results are already stored under, and moving
    # a case to the end of the file would reorder a diff for no reason anyone asked for.
    merged = [incoming.get(item.id, item) for item in existing] + added
    if description is not None:
        fields["description"] = description
    doc = _dataset_doc(GoldenDataset(name=stem, test_cases=merged, **fields))

    created_dir = not path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    # The canonical dump kwargs, the same ones `build.py` writes every other model file with.
    path.write_text(
        yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100), encoding="utf-8"
    )

    # Reading IS validating: `semantic_model.golden` ships no writer and no standalone `validate()`,
    # so the only way to know the runner will accept this file is to hand it to the runner's reader.
    _, res = load_golden_datasets(profile)
    faults = _our_faults(res, stem)
    if faults:
        _rollback(path, snapshot, created_dir)
        for fault in faults:
            _stop(fault)
        return _CANNOT_START

    print(
        json.dumps(
            {
                "dataset": stem,
                "path": str(path),
                "added": [item.id for item in added],
                "replaced": [item.id for item in replaced],
                "summary": {"added": len(added), "replaced": len(replaced)},
            },
            indent=2,
        )
    )
    return 0


def _import(
    profile: str,
    stem: str,
    rows_path: str,
    *,
    confirm_replace: bool,
    description: Optional[str],
) -> int:
    """Turn a confirmed `parse` payload into items, every one of them unverified.

    `expected.sql_confirmed` is False for every row without exception, including the rows whose
    sheet carried a statement: nobody ran it, and an import that marked its own rows verified would
    forge the gate the confirmed-only rule exists to be.

    `expected_value` is dropped. AH-100's shape has no home for it, and parking it in
    `validation_notes` would put a number nothing compares against into a field a reader would
    reasonably read as an answer key.
    """
    payload = json.loads(Path(rows_path).expanduser().read_text(encoding="utf-8"))
    items = [
        GoldenItem(
            id=row["id"],
            query=row["query"],
            expected=GoldenExpected(sql=row.get("sql"), sql_confirmed=False),
            tags=row.get("tags") or [],
        )
        for row in payload["rows"]
    ]
    return _write_items(
        profile, stem, items, confirm_replace=confirm_replace, description=description
    )


def _save(
    profile: str,
    stem: str,
    item_path: str,
    *,
    confirm_replace: bool,
    description: Optional[str],
) -> int:
    """Write one answer somebody looked at and accepted.

    The only door that can produce an item able to gate a run, and everything that makes it one is
    written here: the statement, `sql_confirmed`, the receipt of what the answer looked like, and
    how it was confirmed. AH-111 is a caller of this door, not a second write path.

    `confirmed_by.method` is free text by AH-100's deliberate choice — a `Literal` would refuse
    files for saying something reasonable — so the only check is that somebody said something. An
    item with no method is the one refusal: an answer key whose provenance is blank cannot be
    audited later, which is most of what a receipt is for.
    """
    payload = json.loads(Path(item_path).expanduser().read_text(encoding="utf-8"))
    method = ((payload.get("confirmed_by") or {}).get("method") or "").strip()
    if not method:
        _stop(
            "this item does not say how its answer was confirmed; set confirmed_by.method to how "
            "the result was checked"
        )
        return _CANNOT_START

    # Stamped here when the caller did not stamp it. AH-100 types both as optional, but a receipt
    # that cannot say when it was taken is not much of a receipt.
    now = datetime.now(timezone.utc).isoformat()
    recorded = payload.get("recorded") or {}
    fields: dict[str, Any] = {
        "id": payload.get("id") or _slug(payload["query"]),
        "query": payload["query"],
        "expected": GoldenExpected(sql=payload["sql"], sql_confirmed=True),
        "tags": payload.get("tags") or [],
        "recorded": GoldenRecorded(
            columns=recorded.get("columns") or [],
            rows=recorded.get("rows") or [],
            at=recorded.get("at") or now,
        ),
        "confirmed_by": GoldenConfirmedBy(
            method=method, at=(payload.get("confirmed_by") or {}).get("at") or now
        ),
    }
    if payload.get("match"):
        # Omitted rather than defaulted, so how strictly a saved item compares stays AH-100's
        # answer to that question and not a second one written down here.
        fields["match"] = payload["match"]

    return _write_items(
        profile, stem, [GoldenItem(**fields)], confirm_replace=confirm_replace,
        description=description,
    )


def _add_write_args(cmd: argparse.ArgumentParser) -> None:
    """The flags both write doors take. They go through one funnel, so they take one set."""
    cmd.add_argument("--profile", required=True, help="the profile whose dataset to write")
    cmd.add_argument("--dataset", required=True, help="the dataset's filename stem")
    cmd.add_argument(
        "--confirm-replace",
        action="store_true",
        help="agree to overwrite the items whose id already exists",
    )
    cmd.add_argument("--description", help="the dataset's description")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Author golden-dataset items from a spreadsheet.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    parse_cmd = sub.add_parser("parse", help="Read a question-bank CSV and print what it holds.")
    parse_cmd.add_argument("--csv", required=True, help="the question bank to read")

    import_cmd = sub.add_parser("import", help="Write confirmed parse rows as unverified items.")
    import_cmd.add_argument("--rows", required=True, help="the confirmed `parse` payload")
    _add_write_args(import_cmd)

    save_cmd = sub.add_parser("save", help="Write one confirmed answer, statement and receipt.")
    save_cmd.add_argument("--item", required=True, help="the accepted answer, as JSON")
    _add_write_args(save_cmd)

    args = parser.parse_args(argv)

    if args.cmd == "parse":
        payload = _parse(args.csv)
        if payload is None:
            return _CANNOT_START
        print(json.dumps(payload, indent=2))
        return 0

    if args.cmd == "import":
        return _import(
            args.profile,
            args.dataset,
            args.rows,
            confirm_replace=args.confirm_replace,
            description=args.description,
        )

    return _save(
        args.profile,
        args.dataset,
        args.item,
        confirm_replace=args.confirm_replace,
        description=args.description,
    )


if __name__ == "__main__":
    sys.exit(main())
