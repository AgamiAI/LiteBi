#!/usr/bin/env python3
"""
Golden-dataset explorer renderer.

Reads every golden dataset for a profile, reads the model the explorer reads, and writes one
self-contained HTML file — the sixth of these, after the chart, the model explorer, the
examples-validation queue, the prune view and the golden-run report, and deliberately the same
shape as the five.

A run report says what broke. This page says what the dataset IS, which is the question asked
before a run and after it: how many cases exist, how many can actually fail a run, what the model
defines that no answer key exercises, and which cases the reader has already reported a fault on.

Three properties are the whole point of the page, and each is pinned by a test rather than by this
paragraph:

* **What can gate is counted apart from what exists.** A dataset of forty questions with three
  confirmed answer keys gates on three, and a page that printed one number would read as forty
  questions of coverage.
* **Coverage is computed, never declared.** The tables a confirmed answer key reads come from
  `semantic_model.golden_claims`, the one reader of a statement in this repository — the
  author-declared `expected.tables_used` is exactly the blind spot this page exists to catch.
* **The page may weaken a claim and may never strengthen one.** Withdrawing confirmation needs no
  evidence; granting it from a browser is forging ground truth, and it is the easiest possible way
  to make a failing suite green. There is no control for it here and no statement is editable.

Unlike the golden-run report beside it this one is not stdlib-only, and the difference is
deliberate: reading a claim needs a SQL parser, and the alternative — trusting what the author
declared the statement reads — would report coverage the dataset does not have.

Usage:

    python3 render_golden_datasets.py \\
        --profile demo \\
        --artifacts-dir ~/agami-artifacts \\
        --out <artifacts_dir>/local/eval/demo/datasets-20260901-141500.html
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

# Sibling scripts are imported plainly, and this is what makes that work in every layout the
# plugin ships in — the marketplace cache invokes these scripts by absolute path, where the
# interpreter's own `sys.path[0]` is not something to rely on.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _agami_lib  # noqa: E402

_agami_lib.ensure_importable()

# Re-exec under agami's configured interpreter if this one lacks PyYAML, exactly as the model
# explorer does — this renderer reads the same YAML through the same loader.
import _interp  # noqa: E402,F401

# The explorer's own manifest builder, REUSED rather than re-derived. The coverage gap is model
# tables minus exercised tables, so a second walk of the same YAML would be a second answer to what
# the model holds — and the two answers would drift until this page and the explorer disagreed
# about which tables exist.
import render_model_explorer  # noqa: E402

try:
    import agami_paths
    from semantic_model.golden import load_golden_datasets
    from semantic_model.golden_claims import read_claims
    from semantic_model.sql_dialect import sqlglot_dialect
except ImportError as exc:
    # A fresh plugin install genuinely lacks these, and the traceback a bare ImportError prints
    # names an internal module rather than the thing to install.
    print(
        "render_golden_datasets needs agami-core and its model extra (pydantic, sqlglot, pyyaml): "
        f"install `agami-core[model]` into this interpreter. ({exc})",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc

SHARED_DIR = Path(__file__).resolve().parent.parent / "shared"
TEMPLATE_PATH = SHARED_DIR / "golden-datasets-template.html"
LOGO_DARK_PATH = SHARED_DIR / "agami-logo-dark.svg"
LOGO_LIGHT_PATH = SHARED_DIR / "agami-logo-light.svg"


def _item(item: Any, dataset_name: str, verdict: Optional[dict]) -> dict[str, Any]:
    """One case, reduced to what the page draws.

    Named field by field rather than passed through, and that is this function's whole job. A
    dataset file is free to grow, and a page assembled by copying one would render whatever turned
    up. What is not named here cannot reach the file.

    `recorded` is the one field deliberately reduced rather than carried: it holds the rows the
    author saw on the day — their own data — and a self-contained file full of rows is the kind of
    thing that gets pasted into a chat window. Whether a case has a receipt is what the page draws;
    what the receipt says is not.
    """
    return {
        "id": item.id,
        "dataset": dataset_name,
        "question": item.query,
        "confirmed": item.expected.sql_confirmed,
        "match": item.match,
        "tags": list(item.tags),
        "must_filter": list(item.must_filter),
        "has_recorded": item.recorded is not None,
        "has_confirmed_by": item.confirmed_by is not None,
        # The answer key itself, which this page renders in full and no control on it may edit.
        # The rule that keeps a key off a terminal is about stdout and about what a model's context
        # holds; a gitignored file the dataset's own author opens is neither.
        "sql": item.expected.sql or "",
        "verdict": verdict,
    }


def _last_run_verdicts(profile: str, art: Optional[Path]) -> dict[str, dict[str, dict]]:
    """Each dataset's most recent WHOLE run, as dataset → item key → what that run decided.

    The selection rules are AH-110's `_last_failures`, mirrored rather than reinvented: artifacts
    are keyed on the profile and not the dataset, so the newest file in the directory may describe
    something else entirely, and a run that was itself a `--tag` slice describes only some of the
    dataset. Keyed per dataset for a reason of this page's own: an item id is unique within its
    file and nothing makes it unique across a profile, so a flat map would show one dataset's
    verdict beside another dataset's question.

    No result file at all returns `{}`, which the page renders as a dataset nobody has run yet —
    a normal state, and never an error.
    """
    out = agami_paths.dashboard_dir("eval", profile, art)
    if not out.is_dir():
        return {}
    verdicts: dict[str, dict[str, dict]] = {}
    for path in sorted(out.glob("*.json"), reverse=True):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            # Newest first, so the first readable whole run of a dataset is the one to show and
            # every older record of it is skipped.
            if record["dataset"] in verdicts:
                continue
            if record["selection"] is not None:
                continue
            verdicts[record["dataset"]] = {
                item["item_key"]: {
                    "passed": item["passed"],
                    "gated": item["gated"],
                    "confirmed": item["confirmed"],
                    "section": item["section"],
                    "score": item["score"]["accuracy"],
                }
                for item in record["items"]
            }
        except (OSError, ValueError, KeyError, TypeError):
            # A record this version cannot read, including one written before the verdict fields
            # were added. The page shows no verdict rather than a wrong one.
            continue
    return verdicts


def _model_tables(manifest: dict) -> list[str]:
    """Every table the model still exposes, bare and case-folded.

    Bare and folded because that is how `read_claims` renders a table it read, and the gap below is
    a set difference between the two. Excluded tables are left out: the runtime drops them, so a
    dataset that never asks about one has no gap to answer for.
    """
    return sorted(
        {
            table["name"].lower()
            for schema in manifest["schemas"]
            for table in schema["tables"]
            if not table["excluded"]
        }
    )


def _coverage(manifest: dict, datasets: list) -> dict[str, Any]:
    """What the confirmed answer keys exercise, and what the model defines that they do not.

    Only a confirmed item counts. An unconfirmed case cannot fail a run, so a table only it reads
    is a table nothing is holding the model to — counting it would report coverage that gates on
    nothing, which is the false comfort this tab exists to remove.

    Tables and metrics are two different strengths of evidence and are kept under separate keys for
    that reason. A table is one of the seven claims a statement is read into, so "this answer key
    reads `orders`" is a fact about the statement. A metric is not one of those seven, so it is
    matched by name against the statement's text — a weaker signal, and the page says so.
    """
    dialect = sqlglot_dialect(manifest["storage_type"])
    statements = [
        item.expected.sql
        for dataset in datasets
        for item in dataset.test_cases
        if item.expected.sql_confirmed and item.expected.sql
    ]
    exercised: set[str] = set()
    for sql in statements:
        exercised |= read_claims(sql, dialect=dialect).tables

    tables = _model_tables(manifest)
    text = "\n".join(statements)
    named = {
        metric["name"]
        for metric in manifest["metrics"]
        if not metric["excluded"]
        and re.search(rf"\b{re.escape(metric['name'])}\b", text, re.IGNORECASE)
    }
    metrics = sorted(metric["name"] for metric in manifest["metrics"] if not metric["excluded"])

    return {
        "dialect": dialect,
        # Sorted rather than in statement order: this is a set, and two renders of one profile
        # should produce the same page.
        "tables_exercised": sorted(exercised),
        "tables_untouched": [name for name in tables if name not in exercised],
        "metrics_named": sorted(named),
        "metrics_unnamed": [name for name in metrics if name not in named],
    }


def _lint(res: Any, datasets: list) -> list[dict[str, Any]]:
    """Everything wrong with the datasets: what the reader reported, then this page's own two.

    The reader's findings are carried verbatim — its own text, its own locator — because a page
    that disagreed with the validator about the same file would be worse than no page at all. What
    it does NOT carry is the statement a finding is about: `semantic_model/golden.py` deliberately
    keeps the answer key out of a finding, since a finding travels wherever its caller sends it, and
    a lint row that quoted the key would stop matching what the reader reports.
    """
    rows = [
        {
            "severity": finding.severity,
            "code": finding.code,
            "message": finding.message,
            "locator": finding.locator or "",
        }
        for finding in res.findings
    ]
    for dataset in datasets:
        for item in dataset.test_cases:
            locator = f"{dataset.name}.yaml[{item.id}]"
            if item.expected.sql_confirmed and item.confirmed_by is None:
                rows.append(
                    {
                        "severity": "warning",
                        "code": "golden_confirmed_without_confirmed_by",
                        # A warning rather than an error: the case gates correctly, and what is
                        # missing is the record of who vouched for it — which matters on the day
                        # somebody asks why the suite trusts this answer.
                        "message": f"{locator}: the answer key is confirmed and nothing records "
                        "who confirmed it or how",
                        "locator": locator,
                    }
                )
            if item.recorded is None:
                rows.append(
                    {
                        "severity": "warning",
                        "code": "golden_no_recorded_receipt",
                        "message": f"{locator}: no recorded receipt, so nobody can see what the "
                        "answer looked like on the day it was agreed",
                        "locator": locator,
                    }
                )
    return rows


def build_payload(profile: str, art: Optional[Path] = None) -> dict[str, Any]:
    """Everything the page draws: the datasets, the model's own view of itself, and the last run."""
    datasets, res = load_golden_datasets(profile, art)
    manifest = render_model_explorer.build_manifest(agami_paths.profile_dir(profile, art), profile)
    verdicts = _last_run_verdicts(profile, art)

    items = [
        _item(item, dataset.name, verdicts.get(dataset.name, {}).get(item.item_key))
        for dataset in datasets
        for item in dataset.test_cases
    ]
    gating = sum(1 for item in items if item["confirmed"] is True)
    return {
        "profile": profile,
        "totals": {
            "datasets": len(datasets),
            # Two counts, never one. A dataset of forty items with three confirmed gates on three,
            # and a single number would read as forty items of coverage.
            "total": len(items),
            "gating": gating,
        },
        "datasets": [
            {
                "name": dataset.name,
                "description": dataset.description,
                "category": dataset.category or "",
                "total": len(dataset.test_cases),
                "gating": sum(1 for item in dataset.test_cases if item.expected.sql_confirmed),
            }
            for dataset in datasets
        ],
        "items": items,
        "coverage": _coverage(manifest, datasets),
        "lint": _lint(res, datasets),
    }


def render(*, title: str, profile: str, payload: dict) -> str:
    """Render the explorer for one profile's golden datasets."""
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    logo_dark_svg = LOGO_DARK_PATH.read_text(encoding="utf-8")
    logo_light_svg = LOGO_LIGHT_PATH.read_text(encoding="utf-8")
    theme_css = (SHARED_DIR / "theme.css").read_text(encoding="utf-8")

    # Escape `</` so a `</script>` in a question or an answer key can't terminate the <script>
    # block holding the payload (JS unescapes `<\/` → `</`).
    datasets_json = json.dumps(payload).replace("</", "<\\/")

    # The payload goes in LAST, after every other placeholder, and that is deliberate rather than
    # incidental: it is somebody's question and somebody's SQL, so a case asking "how many
    # {{THEME_CSS}} orders?" would otherwise have the stylesheet spliced into the object literal by
    # a later replace. The JSON then stops parsing, the script throws at load, and — because the
    # whole body is built by that script — the page renders blank with nothing saying why.
    return (
        template.replace("{{REPORT_TITLE}}", title)
        .replace(
            "{{GENERATED_AT}}",
            datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        )
        .replace("{{PROFILE}}", profile)
        .replace("{{AGAMI_LOGO_DARK_TEXT}}", logo_dark_svg)
        .replace("{{AGAMI_LOGO_LIGHT_TEXT}}", logo_light_svg)
        .replace("{{THEME_CSS}}", theme_css)
        .replace("{{DATASETS_JSON}}", datasets_json)
    )


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(
        description="Render a profile's golden datasets as one self-contained page."
    )
    p.add_argument("--profile", required=True, help="Active profile name")
    p.add_argument("--artifacts-dir", help="Root artifacts directory (default: agami's own)")
    p.add_argument("--title", help="Page title (default: 'Golden datasets · <profile>')")
    # The caller owns the output path, as it does for every other rendered surface here.
    p.add_argument("--out", required=True, help="Output HTML path")
    args = p.parse_args(argv)

    art = Path(os.path.expanduser(args.artifacts_dir)).resolve() if args.artifacts_dir else None
    payload = build_payload(args.profile, art)

    out_path = Path(os.path.expanduser(args.out))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        render(
            title=args.title or f"Golden datasets · {args.profile}",
            profile=args.profile,
            payload=payload,
        ),
        encoding="utf-8",
    )
    totals = payload["totals"]
    print(
        f"Wrote {out_path} ({totals['datasets']} dataset"
        f"{'' if totals['datasets'] == 1 else 's'} · {totals['total']} item"
        f"{'' if totals['total'] == 1 else 's'}, {totals['gating']} able to gate a run)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
