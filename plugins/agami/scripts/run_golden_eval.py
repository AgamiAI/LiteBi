#!/usr/bin/env python3
"""Run one golden dataset for a profile and print the verdicts as JSON.

`semantic_model.golden_run.run_golden_dataset` is a function with five required keyword arguments
and no command of its own, so this is the wiring that lets a person — or a skill — reach it. It
does three things nothing upstream does: it chooses the dataset, it renders the tables and columns
the generator is given, and it decides what a verdict looks like on a terminal.

**Stdout carries verdicts and never SQL.** Neither the answer key nor the generated statement
appears in the printed payload, so a terminal that gets pasted into a chat window carries no
statement with it. Both are written to a JSON artifact instead, beside the run's other output.

Unlike the stdlib-only helpers beside it, this one imports the agami-core package: the runner, the
reader, the chokepoint and the model loader all live there, and re-implementing any of them here
would be a second definition of a verdict.

Usage:

    python3 run_golden_eval.py --profile main --list
    python3 run_golden_eval.py --profile main --dataset orders --timeout-s 120
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# A dev checkout keeps the package beside the plugin and does not install it; a marketplace install
# ships the plugin alone, with the package already importable. Prepending the checkout's source when
# it is actually there covers both without asking which install this is.
_PKG_SRC = Path(__file__).resolve().parents[3] / "packages" / "agami-core" / "src"
if _PKG_SRC.is_dir() and str(_PKG_SRC) not in sys.path:
    sys.path.insert(0, str(_PKG_SRC))

try:
    import agami_paths
    import execute_sql
    import tools
    from semantic_model import loader, runtime
    from semantic_model.golden import GoldenDataset, load_golden_datasets
    from semantic_model.golden_run import ClaudeCliGenerator, GoldenRunResult, run_golden_dataset
    from semantic_model.models import Datasource
    from semantic_model.sql_dialect import DialectUnresolved, resolve_datasource_dialect
except ImportError as exc:
    # A fresh plugin install genuinely lacks these, and the traceback a bare ImportError prints
    # names an internal module rather than the thing to install.
    print(
        "run_golden_eval needs agami-core and its model extra (pydantic, sqlglot, pyyaml): "
        f"install `agami-core[model]` into this interpreter. ({exc})",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc

# Presentation order: failures first because they are the actionable thing, errors next because the
# run itself broke, unscored next because nothing could be judged, unconfirmed after that because
# those ran but can never gate, and passes last because they need no action. The report slice reads
# the same order, so the two cannot disagree about what a run looks like.
_SECTION_ORDER = ("failure", "error", "unscored", "unconfirmed", "pass")

# Where a run's exit code is decided is a later slice's problem. This one exits 0 for any run that
# reached the end of its wiring — a run with failures in it is a run that worked — and non-zero
# only when it could not start.
_CANNOT_START = 2


def _section(outcome: Any) -> str:
    """Which section an item is printed under.

    Order matters here and not only in the output: an item whose generation errored is an error
    whether or not anybody confirmed its answer key, and an unconfirmed item is never a failure
    because it can never gate a run.

    An unscored item is not a failure either, and the check for it has to come before `passed`:
    nothing was compared, so `passed` is False for a reason that has nothing to do with the answer.
    It counts in `unscored` and in neither `failed` nor `gating_failures`, so filing it under
    failures would print a failure list above a summary saying there were none.
    """
    if outcome.score.status == "error":
        return "error"
    if not outcome.confirmed:
        return "unconfirmed"
    if outcome.score.status == "unscored":
        return "unscored"
    return "pass" if outcome.passed else "failure"


def _schema_text(org: Datasource) -> str:
    """The tables and columns the generator may write against, one table per line.

    This helper owns the rendering because nothing upstream produces it: `ClaudeCliGenerator` takes
    the schema already flattened, and the generating context has no tool with which to read a model
    off disk. Built in-process rather than by shelling out to the `sm` CLI, whose `areas` and
    `bundle` subcommands are two-line wrappers over exactly these two calls.

    A table that belongs to more than one subject area is rendered once: the generator is being
    given a vocabulary, and the same table twice reads as two of them.
    """
    tables: dict[str, str] = {}
    for area in runtime.list_subject_areas(org):
        bundle = loader.get_subject_area_bundle(org, area["name"])
        for name, table in bundle["tables"].items():
            if name in tables:
                continue
            columns = ", ".join(
                f"{column['name']} {column.get('type') or ''}".strip()
                for column in table.get("columns", [])
            )
            tables[name] = f"{name}({columns})"
    return "\n".join(tables.values())


def _finding_lines(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The three fields a reader acts on. `severity` and `suggestion` belong to the validator's own
    surfaces, and repeating them here would make this payload a second rendering of a finding."""
    return [
        {"code": finding["code"], "message": finding["message"], "locator": finding["locator"]}
        for finding in findings
    ]


def _list_payload(profile: str) -> dict[str, Any]:
    """What a profile's datasets are, without running one.

    A missing `golden_datasets/` directory is the normal starting state and the reader is silent
    about it, so this reports zero datasets rather than an error — and always names the directory,
    because naming the path to create is the whole of the advice a caller can give from here.
    """
    datasets, findings = load_golden_datasets(profile)
    return {
        "profile": profile,
        "datasets_dir": str(agami_paths.profile_dir(profile) / "golden_datasets"),
        "datasets": [
            {
                "name": dataset.name,
                "total": len(dataset.test_cases),
                "confirmed": sum(
                    1 for item in dataset.test_cases if item.expected.sql_confirmed
                ),
                "unconfirmed": sum(
                    1 for item in dataset.test_cases if not item.expected.sql_confirmed
                ),
            }
            for dataset in datasets
        ],
        "findings": _finding_lines([asdict(finding) for finding in findings.findings]),
    }


def _pick(datasets: list[GoldenDataset], wanted: Optional[str]) -> Optional[GoldenDataset]:
    """The dataset to run, or None having said on stderr why there is not one.

    A bare invocation against a single dataset is the common case and needs no argument. Against
    several it stops: choosing for the person would run the wrong dataset silently, and asking them
    which one is the skill's job rather than this helper's. Every refusal names what is present,
    because a name is what the next invocation needs.
    """
    names = ", ".join(dataset.name for dataset in datasets)
    if wanted is not None:
        for dataset in datasets:
            if dataset.name == wanted:
                return dataset
        _stop(f"no golden dataset named {wanted!r}. Datasets present: {names or 'none'}")
    elif not datasets:
        _stop("this profile has no golden datasets to run")
    elif len(datasets) > 1:
        _stop(f"name a dataset with --dataset. Datasets present: {names}")
    else:
        return datasets[0]
    return None


def _stop(reason: str) -> None:
    """Say why the run cannot start. One prefix everywhere, so a caller can strip it."""
    print(f"agami-eval: {reason}", file=sys.stderr)


def _summary(result: GoldenRunResult) -> dict[str, Any]:
    """The runner's own counters plus `completed`, which is not derivable from them.

    All three of the values a verdict rests on are here — `completed`, `gating_failures` and
    `errored` — because the runner's docstring says a caller reads all three: a generator that
    raised truncates the outcomes, so the items after it are absent and the counts alone would
    read as a clean run.
    """
    return {**result.as_dict()["summary"], "completed": result.completed}


def _section_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    """How many rows are printed under each heading, counted from the rows themselves.

    These and the runner's counters answer different questions, and both are printed: this is what
    is on screen, and `failed` / `gating_failures` / `errored` are what a verdict rests on. The two
    do not line up — an unscored item is in neither `failed` nor `gating_failures` and still takes a
    row, an unconfirmed miss counts in `failed` and is rendered somewhere else — so the rendered
    counts are derived from the rendered items rather than recomputed from the run.
    """
    return {
        section: sum(1 for item in items if item["section"] == section)
        for section in _SECTION_ORDER
    }


def _write_artifact(
    result: GoldenRunResult,
    questions: dict[str, str],
    keys: dict[str, str],
    summary: dict[str, Any],
) -> Path:
    """Join the run to the dataset it ran and persist both statements.

    `GoldenRunResult` carries neither the question nor the answer key, so this is the only place
    that holds all three — and the report slice renders the two statements side by side, which is
    why they are kept rather than dropped with stdout's. The summary is handed in rather than built
    again here, so the file and the terminal describe the run with one set of numbers.

    This lands under `local/`, which is gitignored per-user state, and the answer key it repeats is
    already on disk in the dataset file the run just read, so it adds no exposure.
    """
    joined = {
        "run_id": result.run_id,
        "profile": result.profile,
        "dataset": result.dataset,
        "summary": summary,
        "items": [
            {
                "item_key": outcome.item_key,
                "question": questions.get(outcome.item_key, ""),
                "expected_sql": keys.get(outcome.item_key, ""),
                "generated_sql": outcome.generated_sql,
                "score": outcome.as_dict()["score"],
            }
            for outcome in result.outcomes
        ],
        "findings": list(result.findings),
    }
    out = agami_paths.dashboard_dir("eval", result.profile)
    out.mkdir(parents=True, exist_ok=True)
    # Microseconds because a person sorts these by name and two runs a second apart must not land
    # on the same one — an overwritten run is a report that silently describes something else.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = out / f"{stamp}.json"
    path.write_text(json.dumps(joined, indent=2), encoding="utf-8")
    return path


def _run_payload(result: GoldenRunResult, questions: dict[str, str]) -> dict[str, Any]:
    """The verdicts, in presentation order, with no statement anywhere in them."""
    items = [
        {
            "section": _section(outcome),
            "item_key": outcome.item_key,
            "question": questions.get(outcome.item_key, ""),
            "confirmed": outcome.confirmed,
            "passed": outcome.passed,
            "gated": outcome.gated,
            "status": outcome.score.status,
            "accuracy": outcome.score.accuracy,
            "reason": outcome.score.reason,
            "golden_row_count": outcome.score.golden_row_count,
            "generated_row_count": outcome.score.generated_row_count,
            "gates": outcome.claims["gates"] if outcome.claims else [],
        }
        for outcome in result.outcomes
    ]
    # Stable within a section, so items keep the order their author wrote them in.
    items.sort(key=lambda item: _SECTION_ORDER.index(item["section"]))
    return {
        "summary": {**_summary(result), "sections": _section_counts(items)},
        "items": items,
        "findings": _finding_lines(list(result.findings)),
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run a golden dataset and score every case.")
    parser.add_argument("--profile", required=True, help="the semantic-model profile to run")
    parser.add_argument("--dataset", help="which dataset to run (needed only if there are several)")
    parser.add_argument(
        "--list", action="store_true", help="describe the profile's datasets and run nothing"
    )
    parser.add_argument(
        "--timeout-s", type=float, default=120.0, help="how long one generation may take"
    )
    args = parser.parse_args(argv)

    if args.list:
        print(json.dumps(_list_payload(args.profile), indent=2))
        return 0

    datasets, findings = load_golden_datasets(args.profile)
    dataset = _pick(datasets, args.dataset)
    if dataset is None:
        return _CANNOT_START

    org_model = loader.load_datasource(agami_paths.profile_dir(args.profile))
    try:
        dialect = resolve_datasource_dialect(org_model)
    except DialectUnresolved as exc:
        # The one unguarded raise on this path: everything below it is total. Its message is
        # value-free by contract, so it is relayed as the preflight reason rather than as a stack.
        _stop(f"cannot run this profile — {exc}")
        return _CANNOT_START

    result = run_golden_dataset(
        dataset,
        profile=args.profile,
        generator=ClaudeCliGenerator(_schema_text(org_model), timeout_s=args.timeout_s),
        executor=execute_sql.BUILTIN_EXECUTOR,
        # The deployment's own resolver, so this run scores a tenant's dataset against the warehouse
        # that tenant's credentials resolve to — `local` on the single-operator path.
        org=tools.resolved_org_id(),
        datasource=args.profile,
        dialect=dialect,
        findings=findings.findings,
    )

    questions = {item.item_key: item.query for item in dataset.test_cases}
    keys = {item.item_key: item.expected.sql or "" for item in dataset.test_cases}
    payload = _run_payload(result, questions)
    payload["artifact"] = str(_write_artifact(result, questions, keys, payload["summary"]))
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
