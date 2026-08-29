#!/usr/bin/env python3
"""Run one golden dataset for a profile and print the verdicts as JSON.

`semantic_model.golden_run.run_golden_dataset` is a function with five required keyword arguments
and no command of its own, so this is the wiring that lets a person — or a skill — reach it. It
does three things nothing upstream does: it chooses the dataset, it renders the tables and columns
the generator is given, and it decides what a verdict looks like on a terminal.

**Stdout carries verdicts and never SQL.** Neither the answer key nor the generated statement
appears in the printed payload — nor the answer key's own column names, which two of the
comparator's reasons are built out of — so a terminal that gets pasted into a chat window carries
no statement with it. All of it is written to a JSON artifact instead, beside the run's other
output.

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
import re
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# The semantic-model CLI, beside this script in every layout the plugin ships in.
_SM = Path(__file__).resolve().parent / "sm"

# Three layouts reach this script and only one of them has `packages/` on disk: a dev checkout
# keeps the source beside the plugin, a marketplace install ships `<version>/lib` and no checkout
# at all, and a pip install has the library importable already. `_agami_lib` is the one helper
# that knows all three, and every other runtime script in this directory calls it — resolving the
# path here instead would have covered the checkout and told a marketplace user to pip-install a
# library their plugin already ships.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _agami_lib  # noqa: E402

_agami_lib.ensure_importable()

try:
    import agami_paths
    import execute_sql
    import tools
    from pydantic import ValidationError
    from semantic_model import loader
    from semantic_model.comparator import ItemScore
    from semantic_model.golden import GoldenDataset, load_golden_datasets
    from semantic_model.golden_run import ClaudeCliGenerator, GoldenRunResult, run_golden_dataset
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


class SmFailed(RuntimeError):
    """An `sm` call this run cannot continue without."""


def _sm(*args: str, json_out: bool = True) -> Any:
    """Run one semantic-model CLI command and return what it printed.

    Shelled out rather than called in-process, and that is the decision. The Python functions behind
    these subcommands are importable, but `agami-query` reaches them THROUGH this CLI, and the whole
    point of sourcing context here is that the eval and the product read the model the same way. An
    in-process call would be a second route to the same data, which is the drift this replaces.

    `org-context` prints markdown and the rest print JSON, so the caller says which it wants.
    """
    completed = subprocess.run(
        ["bash", str(_SM), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        # Exit 3 is `no model` from the CLI and `no usable interpreter` from the wrapper, and the
        # `{"error": "no_model"}` payload is what tells them apart. Neither message is relayed:
        # both quote the artifacts path, which encodes the tenant on a hosted deployment.
        raise SmFailed(f"`sm {args[0]}` exited {completed.returncode}")
    if not json_out:
        return completed.stdout
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SmFailed(f"`sm {args[0]}` did not print JSON") from exc


def _column_text(column: dict) -> str:
    """One column as the generator sees it: name, type, and the model's own description.

    The description is the point. REQ-010 sends "the question and the model context", and a
    name-and-type rendering is a narrower reading than that: it drops the sentence a curator wrote
    precisely so a reader would filter the column correctly. A column called `type` on a table that
    mixes invoices, payments and sales orders is unanswerable from its name — the description is
    where the warning lives, and the generator has no tool with which to go and read it.
    """
    head = f"{column['name']} {column.get('type') or ''}".strip()
    description = (column.get("description") or "").strip()
    return f"{head} -- {description}" if description else head


def _metrics_text(bundles: list[dict]) -> str:
    """The approved metrics, with the binding a correct answer reuses verbatim.

    A metric is the curated form of an aggregation a team has already argued about and signed off:
    `billings` is not "sum the totals", it is "sum the totals of invoices only, and never as one
    bare number across currencies". Withheld, the generator hand-rolls the aggregate and the run
    scores an invented definition against a reviewed one.
    """
    lines: list[str] = []
    seen: set[str] = set()
    for bundle in bundles:
        for metric in bundle.get("metrics") or []:
            name = metric.get("name") or ""
            if not name or name in seen:
                continue
            seen.add(name)
            parts = [name]
            aliases = metric.get("other_names") or []
            if aliases:
                parts.append(f"(also: {', '.join(str(a) for a in aliases)})")
            for field in ("description", "calculation"):
                value = (metric.get(field) or "").strip()
                if value:
                    parts.append(value)
            bindings = metric.get("bindings") or {}
            if isinstance(bindings, dict) and bindings:
                parts.append(f"SQL: {str(next(iter(bindings.values()))).strip()}")
            lines.append(" -- ".join(parts))
    return "\n".join(lines)


def _entities_text(bundles: list[dict]) -> str:
    """The vocabulary a reader uses for the things in the model, and what each one maps to.

    An entity is the bridge between the words in a question and the columns that answer it: a
    question naming "invoices" is answerable only by whoever knows that word reaches
    `transactions.type`. The generator is given the question in the reader's words, so without the
    entities it is matching those words against column names alone.
    """
    lines: list[str] = []
    seen: set[str] = set()
    for bundle in bundles:
        for entity in bundle.get("entities") or []:
            name = entity.get("name") or ""
            if not name or name in seen:
                continue
            seen.add(name)
            parts = [name]
            aliases = [str(a) for a in (entity.get("other_names") or []) if a]
            plural = entity.get("plural")
            if plural:
                aliases.insert(0, str(plural))
            if aliases:
                parts.append(f"(also: {', '.join(aliases)})")
            # A list of {table, column, primary} rather than a string, and rendered rather than
            # stringified: the repr of a dict in a prompt reads as Python to a model being asked
            # for SQL.
            targets = [
                f"{m.get('table')}.{m.get('column')}"
                for m in (entity.get("maps_to") or [])
                if isinstance(m, dict) and m.get("table") and m.get("column")
            ]
            if targets:
                parts.append(f"maps to {', '.join(targets)}")
            description = (entity.get("description") or "").strip()
            if description:
                parts.append(description)
            lines.append(" -- ".join(parts))
    return "\n".join(lines)


def _relationships_text(bundles: list[dict]) -> str:
    """How the tables join, with the cardinality that decides whether a join fans a total out.

    Cardinality is the half that matters to a scored answer. A one-to-many joined the wrong way
    multiplies the rows on the one side, and a SUM over that is wrong by a factor nobody can see in
    the number. The generator cannot infer it from column names.
    """
    lines: list[str] = []
    seen: set[str] = set()
    for bundle in bundles:
        for rel in bundle.get("relationships") or []:
            left, right = rel.get("from_table"), rel.get("to_table")
            if not left or not right:
                continue
            # Qualified on both sides, matching how `_schema_text` names a table: an edge between
            # two same-named tables in different schemas is two edges, and the generator is being
            # given one vocabulary for both.
            if rel.get("from_schema"):
                left = f"{rel['from_schema']}.{left}"
            if rel.get("to_schema"):
                right = f"{rel['to_schema']}.{right}"
            on = f"{left}.{rel.get('from_column')} = {right}.{rel.get('to_column')}"
            if on in seen:
                continue
            seen.add(on)
            parts = [f"{left} -> {right}", f"on {on}"]
            for field in ("relationship", "join_type"):
                value = rel.get(field)
                if value:
                    parts.append(str(value))
            description = (rel.get("description") or "").strip()
            if description:
                parts.append(description)
            lines.append(" -- ".join(parts))
    return "\n".join(lines)


def _examples_text(root: Path, areas: list[str], question: str, top_k: int) -> str:
    """The worked question/SQL pairs this team confirmed, ranked for THIS question.

    Ranked rather than dumped, because that is what `agami-query` does and the ranker is the same
    one: `sm examples --query` is the product's own signal for which curated answer is nearest. A
    bulk dump sends every example to every item and leaves the choosing to the model, which is a
    different reading of the same library.

    Every area is ranked and the best few overall are kept, because `sm examples` reads one area's
    library at a time and an eval is not told which area a question belongs to. `agami-query` picks
    the area by reading the descriptions, which is a judgement this has no model to make; taking
    the top of the merged list is the approximation, and it is the one that cannot send an incident
    question the asset library because that is the area that sorted first.

    The convention is the point and it is written nowhere else: which of two equally correct date
    spellings this team uses, whether a currency is broken out, how a join is phrased.
    """
    scored: list[tuple[float, dict]] = []
    for area in areas:
        ranked = _sm(
            "examples", str(root), "--area", area, "--query", question, "--top-k", str(top_k)
        )
        for match in ranked.get("matches") or []:
            scored.append((match.get("score") or 0.0, match.get("example") or {}))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    lines: list[str] = []
    for _, example in scored[:top_k]:
        asked = (example.get("question") or "").strip()
        sql = " ".join(str(example.get("sql") or "").split())
        if asked and sql:
            lines.append(f"Q: {asked}\nA: {sql}")
    return "\n\n".join(lines)


def _model_context(cached: dict, question: str) -> str:
    """Everything the generator is given about the model, assembled for one question.

    One flattened string rather than several prompt fields, because `SqlGenerator.generate` takes
    the schema already flattened and widening that signature is a contract change — the argument
    list is the isolation boundary.

    Section order mirrors `agami-query/SKILL.md` Phase 2b: the vocabulary, then the datasource
    context, then the examples. Only the last of these depends on the question; everything above it
    was fetched once for the run.
    """
    sections = [cached["schema"]]
    for heading, body in (
        ("What this datasource means, and what its codes stand for:", cached["org_context"]),
        (
            "Approved metrics — reuse a binding verbatim when the question names one:",
            cached["metrics"],
        ),
        ("The words a reader uses for these things:", cached["entities"]),
        ("How these tables join:", cached["relationships"]),
        (
            "Worked examples this team has confirmed, nearest first — follow their conventions:",
            _examples_text(cached["root"], cached["areas"], question, cached["top_k"]),
        ),
    ):
        if body:
            sections.append(f"{heading}\n{body}")
    return "\n\n".join(sections)


def _schema_text(bundles: list[dict]) -> str:
    """The tables and columns the generator may write against, one table per line.

    Every table is named the way the rest of the repo names one — `schema.table` where the model
    declares a schema, the bare name where it does not. This string is the whole of the vocabulary
    the generator is given, so a bare name here is an unqualified statement there: on a profile
    whose schema is not on the connection's search path every item would error, and the run would
    read as a model regression rather than as a wiring bug.

    A table that belongs to more than one subject area is rendered once, keyed on that qualified
    name: the generator is being given a vocabulary, the same table twice reads as two of them, and
    two same-named tables in two schemas really are two.
    """
    tables: dict[str, str] = {}
    for bundle in bundles:
        for table in (bundle.get("tables") or {}).values():
            schema = table.get("schema")
            name = f"{schema}.{table['name']}" if schema else table["name"]
            if name in tables:
                continue
            columns = ", ".join(_column_text(column) for column in table.get("columns", []))
            tables[name] = f"{name}({columns})"
    return "\n".join(tables.values())


def _fetch_context(root: Path, top_k: int) -> dict:
    """The three `sm` calls that do not depend on the question, run once for the whole run.

    Cached because each costs about a second of interpreter start-up, and a per-item fetch would
    spend that on every case to receive the same bytes back. `examples` is the only one the question
    changes, so it is the only one left in the loop.
    """
    areas = _sm("areas", str(root))
    if not areas:
        raise SmFailed("this profile declares no subject areas")
    bundles = [_sm("bundle", str(root), "--area", area["name"]) for area in areas]
    return {
        "root": root,
        # Every area, because the ranker reads one library at a time and the question decides which
        # one matters — not the order `sm areas` happens to return.
        "areas": [area["name"] for area in areas],
        "top_k": top_k,
        "schema": _schema_text(bundles),
        "org_context": _sm("org-context", str(root), json_out=False).strip(),
        "metrics": _metrics_text(bundles),
        "entities": _entities_text(bundles),
        "relationships": _relationships_text(bundles),
    }


# The second reason built from an answer-key column name, and the one that carries no structured
# field to rebuild it from: `shape` pairs columns positionally and quotes the golden side's name.
# Matched rather than reconstructed, because the name belongs in the artifact and it is only this
# surface that refuses it.
_TYPED_COLUMN_REASON = re.compile(r"^column .+ does not carry the type the answer key does$")
_TYPED_COLUMN_SAFE = "a column does not carry the type the answer key does"


def _safe_reason(score: ItemScore) -> str:
    """One item's `reason`, with the answer key's column names taken out of it.

    Two of the comparator's reasons are built from the aliases the author wrote in `expected.sql`,
    and this payload is read straight into a chat table — so a mismatch would put the answer key's
    vocabulary in a transcript that is promised no SQL. A count says the same actionable thing:
    the answer key asked for something the generated result does not carry. The artifact keeps the
    score whole, names included, which is where a drill-down reads them from.
    """
    unmatched = score.unmatched_golden_columns
    if unmatched:
        verb = "has" if len(unmatched) == 1 else "have"
        return (
            f"{len(unmatched)} of the answer key's columns {verb} no counterpart in the "
            "generated result"
        )
    return _TYPED_COLUMN_SAFE if _TYPED_COLUMN_REASON.match(score.reason) else score.reason


def _finding_lines(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The three fields a reader acts on, the message cut to its first line.

    `severity` and `suggestion` belong to the validator's own surfaces, and repeating them here
    would make this payload a second rendering of a finding.

    The first line only, because a YAML parse error carries the offending source line back inside
    its own text — and on a golden dataset that line is the answer key. `code` and `locator` are
    what tells a dataset breakage apart from a scored failure, so both stay whole.
    """
    return [
        {
            "code": finding["code"],
            "message": finding["message"].split("\n", 1)[0],
            "locator": finding["locator"],
        }
        for finding in findings
    ]


def _datasets_dir(profile: str) -> Path:
    """Where a profile's datasets live. Named in both refusals a caller can hit, so it is composed
    once — two spellings of this path would be two answers to "where do I put one"."""
    return agami_paths.profile_dir(profile) / "golden_datasets"


def _list_payload(profile: str) -> dict[str, Any]:
    """What a profile's datasets are, without running one.

    A missing `golden_datasets/` directory is the normal starting state and the reader is silent
    about it, so this reports zero datasets rather than an error — and always names the directory,
    because naming the path to create is the whole of the advice a caller can give from here.
    """
    datasets, findings = load_golden_datasets(profile)
    return {
        "profile": profile,
        "datasets_dir": str(_datasets_dir(profile)),
        "datasets": [
            {
                "name": dataset.name,
                "total": len(dataset.test_cases),
                "confirmed": sum(1 for item in dataset.test_cases if item.expected.sql_confirmed),
                "unconfirmed": sum(
                    1 for item in dataset.test_cases if not item.expected.sql_confirmed
                ),
            }
            for dataset in datasets
        ],
        "findings": _finding_lines([asdict(finding) for finding in findings.findings]),
    }


def _pick(
    datasets: list[GoldenDataset], wanted: Optional[str], datasets_dir: Path
) -> Optional[GoldenDataset]:
    """The dataset to run, or None having said on stderr why there is not one.

    A bare invocation against a single dataset is the common case and needs no argument. Against
    several it stops: choosing for the person would run the wrong dataset silently, and asking them
    which one is the skill's job rather than this helper's. Every refusal names what is present,
    because a name is what the next invocation needs — and when nothing is present, the directory
    to create instead, which is the only thing a caller can act on from there.
    """
    names = ", ".join(dataset.name for dataset in datasets)
    if wanted is not None:
        for dataset in datasets:
            if dataset.name == wanted:
                return dataset
        _stop(f"no golden dataset named {wanted!r}. Datasets present: {names or 'none'}")
    elif not datasets:
        _stop(
            "this profile has no golden datasets to run. The first one goes in "
            f"{datasets_dir}/<name>.yaml"
        )
    elif len(datasets) > 1:
        _stop(f"name a dataset with --dataset. Datasets present: {names}")
    else:
        return datasets[0]
    return None


def _stop(reason: str) -> None:
    """Say why the run cannot start. One prefix everywhere, so a caller can strip it."""
    print(f"agami-eval: {reason}", file=sys.stderr)


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
                # Indexed rather than defaulted: both dicts are built from the same `test_cases`
                # the run was handed, and a key that stopped resolving should say so rather than
                # write an artifact with an empty question in it.
                "question": questions[outcome.item_key],
                "expected_sql": keys[outcome.item_key],
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
    """The verdicts, in presentation order, with no statement and no column of one in them."""
    items = [
        {
            "section": _section(outcome),
            "item_key": outcome.item_key,
            "question": questions[outcome.item_key],
            "confirmed": outcome.confirmed,
            "passed": outcome.passed,
            "gated": outcome.gated,
            "status": outcome.score.status,
            "accuracy": outcome.score.accuracy,
            "reason": _safe_reason(outcome.score),
            "golden_row_count": outcome.score.golden_row_count,
            "generated_row_count": outcome.score.generated_row_count,
            "gates": outcome.claims["gates"] if outcome.claims else [],
        }
        for outcome in result.outcomes
    ]
    # Stable within a section, so items keep the order their author wrote them in.
    items.sort(key=lambda item: _SECTION_ORDER.index(item["section"]))
    return {
        "summary": {
            # The runner's own counters, plus `completed`, which is not derivable from them: a
            # generator that raised truncates the outcomes, so the items after it are absent and
            # the counts alone would read as a clean run. All three of the values a verdict rests
            # on — `completed`, `gating_failures` and `errored` — are therefore here.
            **result.as_dict()["summary"],
            "completed": result.completed,
            "sections": _section_counts(items),
        },
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
    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="how many ranked prompt examples to give the generator per question",
    )
    args = parser.parse_args(argv)

    if args.list:
        print(json.dumps(_list_payload(args.profile), indent=2))
        return 0

    datasets, findings = load_golden_datasets(args.profile)
    dataset = _pick(datasets, args.dataset, _datasets_dir(args.profile))
    if dataset is None:
        return _CANNOT_START

    # Reading the model and resolving its dialect are the three raises this path reaches, and
    # everything below them is total. None of them is relayed as a stack: a traceback prints the
    # absolute artifacts path, which encodes the tenant in a hosted deployment and which every
    # other refusal here withholds.
    try:
        org_model = loader.load_datasource(agami_paths.profile_dir(args.profile))
        dialect = resolve_datasource_dialect(org_model)
    except FileNotFoundError:
        _stop(
            f"cannot read the semantic model for profile {args.profile!r} — "
            "run agami-connect to build one"
        )
        return _CANNOT_START
    except ValidationError as exc:
        # The count and not the text: pydantic's own message quotes the value that failed back,
        # and here that value is a piece of the tenant's model.
        _stop(
            f"the semantic model for profile {args.profile!r} does not parse "
            f"({exc.error_count()} problem(s)) — agami-connect can rebuild it"
        )
        return _CANNOT_START
    except DialectUnresolved as exc:
        # This one's message is value-free by contract, so it is relayed as the preflight reason.
        _stop(f"cannot run this profile — {exc}")
        return _CANNOT_START

    try:
        cached = _fetch_context(agami_paths.profile_dir(args.profile), args.top_k)
    except SmFailed as exc:
        # Before the first item rather than during one: a run whose context could not be built has
        # no generator, and reporting that as every item erroring would read as a model regression.
        _stop(f"cannot build the model context for profile {args.profile!r} — {exc}")
        return _CANNOT_START

    result = run_golden_dataset(
        dataset,
        profile=args.profile,
        generator=ClaudeCliGenerator(
            lambda question: _model_context(cached, question),
            timeout_s=args.timeout_s,
        ),
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
    try:
        payload["artifact"] = str(_write_artifact(result, questions, keys, payload["summary"]))
    except OSError as exc:
        # The run is already paid for — a model call and up to two warehouse queries per case — so
        # a directory it cannot write to costs the drill-down and nothing else. The verdicts print
        # either way. The path is not relayed, for the reason the preflight guards give.
        payload["artifact"] = ""
        _stop(
            "the verdicts are below, but this run's artifact could not be written "
            f"({exc.__class__.__name__}: {exc.strerror or 'cannot be written'})"
        )
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
