#!/usr/bin/env python3
"""CLI entrypoint for the agami semantic model.

One dispatch surface shared by the skills and the test suite, so behavior is
identical wherever the model is exercised. Subcommands:

    validate  <root>                      — parse + validate a profile tree
    context   <root> --area A --tables ... — assemble get_table_context output
    bundle    <root> --area A             — one-shot subject-area bundle
    areas     <root>                      — list subject areas
    examples  <root> --area A --query Q   — rank prompt examples for a query
    preflight <root> --sql SQL            — fan-trap / chasm-trap pre-flight check
    seed-validate <root> --area A --profile P — run every written seed via execute_sql
                                            (safety pass on) → examples-validation items
    remove-example <root> --area A --question Q — reject example(s) by question
                                            (status: rejected — kept for audit, off runtime)

Every subcommand emits JSON on stdout (so callers parse one shape) except
`validate`, which prints a human report and sets the exit code (0 ok, 1 errors).
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import loader as L
from . import runtime as RT
from . import validator as V


def _print_json(obj) -> None:
    json.dump(obj, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


def _utc_now_iso() -> str:
    """ISO-8601 UTC (`...Z`) — the sign-off timestamp format the validator expects."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stamp_approve_ops(ops: list) -> list:
    """Stamp `at` on any `approve` op that lacks one. curate.apply deliberately can't read
    the clock itself (`op.get("at")` — "caller stamps"); an approve op without `at` records
    `signed_off_at: null` and the validator then rejects the whole batch. The CLI process
    *can* read the clock, so it stamps here — one place, for every CLI caller."""
    now = _utc_now_iso()
    for op in ops:
        if isinstance(op, dict) and op.get("op") == "approve" and not op.get("at"):
            op["at"] = now
    return ops


def cmd_validate(args) -> int:
    # Validate the model AS AUTHORED, including curator-excluded entries
    # (include_rejected=True) — matching the curate engine's own pre-write check.
    # Excluding a table flips its tables/<T>.yaml to review_state: rejected but
    # intentionally leaves the subject_area.yaml `tables:` ref in place (the loader
    # tolerates the dangling ref at runtime, dropping both). Loading with the default
    # include_rejected=False here would drop the rejected table from org_tables while
    # its ref remains, so _check_table_refs_resolve would false-flag every intentional
    # exclusion as orphan_table_ref — even though curate passed and the runtime is fine.
    # A GENUINE orphan (a ref to a never-defined table) still fails, since no YAML
    # exists for it even with rejected included.
    org = L.load_organization(args.root, include_rejected=True)
    res = V.validate(org)
    print(V.format_result(res))
    return 0 if res.ok else 1


def cmd_snapshot(args) -> int:
    """Stamp the model_version snapshot for a profile tree. Introspect/curate do this
    automatically; this command is for paths that write a model WITHOUT going through
    them — e.g. agami-connect's sample copy (6A) drops the prebuilt model into place,
    then calls this so the answer receipt has a real model_version."""
    from . import snapshot as SN
    h = SN.write_snapshot(args.root)
    print(h or "(no model to snapshot)")
    return 0 if h else 1


def cmd_ensure_org_id(args) -> int:
    """Mint + persist the deployment org_id into <root>/org.yaml if absent (F14 / ACE-056; relocated
    by F15 / ACE-067), then print it. Idempotent — an already-minted id is returned unchanged. The
    introspect/curate paths mint inline via write_tree; this command is for paths that write a model
    WITHOUT them — e.g. agami-connect's sample copy (6A) drops a prebuilt model that carries no id.
    Deployment-scoped: the id now comes from the root ``organization.yaml`` record (minted on first
    use), so every profile in the dir resolves the same one — no sibling scan."""
    from pathlib import Path

    from . import build
    root = Path(args.root)
    org_path = root / "org.yaml"
    doc = L._read_yaml(org_path) or {}
    existing = doc.get("org_id")
    oid = build.ensure_org_id(root, existing or None)
    if oid != existing:
        doc["org_id"] = oid
        org_path.write_text(build._dump(doc), encoding="utf-8")
    print(oid)
    return 0


def cmd_context(args) -> int:
    org = L.load_organization(args.root)
    out = L.get_table_context(
        org,
        args.tables,
        area=args.area,
        columns=args.columns,
        include=args.include,
    )
    _print_json(out)
    return 0


def cmd_bundle(args) -> int:
    org = L.load_organization(args.root)
    _print_json(L.get_subject_area_bundle(org, args.area))
    return 0


def cmd_areas(args) -> int:
    org = L.load_organization(args.root)
    _print_json(RT.list_subject_areas(org))
    return 0


def cmd_org_draft(args) -> int:
    # A human-narrative STARTER for ORGANIZATION.md (skip path) — a prompt only, no model
    # facts. Those are derived at read time (see `org-context`), so they never get baked into
    # the editable prose file where a human could clobber them.
    from . import org_draft
    org = L.load_organization(args.root, include_rejected=False)
    sys.stdout.write(org_draft.starter_organization_md(org))
    return 0


def cmd_org_context(args) -> int:
    # The full domain context for the LLM, two-level (F15 / ACE-069): the shared COMPANY block from the
    # deployment record (<artifacts_dir>/organization.yaml + the root ORGANIZATION.md), then this
    # datasource's source-specific narrative + model-derived summary. With no record it degrades to the
    # pre-F15 per-profile assembly (byte-identical). This is what the query path injects as context.
    from . import org_draft
    from . import org_record as OR
    root = Path(args.root)
    org = L.load_organization(root, include_rejected=False)
    src_md = root / "ORGANIZATION.md"
    source_narrative = src_md.read_text(encoding="utf-8") if src_md.exists() else ""
    art = root.parent  # the artifacts dir holds the deployment-level record + company narrative
    record = OR.load_org_record(art)
    comp_md = OR.narrative_path(art)
    company_narrative = comp_md.read_text(encoding="utf-8") if comp_md.exists() else ""
    sys.stdout.write(
        org_draft.compose_org_context(
            record, [org], company_narrative=company_narrative, source_narratives=[source_narrative]
        )
    )
    return 0


def cmd_set_org(args) -> int:
    # Write the human-authored company fields (name / short description) into the deployment-level
    # organization record. Onboarding calls this after asking the company question, so the record
    # carries more than just its org_id. Only the flags passed are updated; others are left as-is.
    from . import org_record as OR

    record = OR.set_org_fields(
        args.artifacts_dir, name=args.name, description=args.description
    )
    _print_json(
        {"org_id": record.org_id, "name": record.name, "description": record.description}
    )
    return 0


def cmd_examples(args) -> int:
    examples = L.list_prompt_examples(args.root, args.area)
    matches = RT.get_prompt_examples(args.query, examples, top_k=args.top_k)
    _print_json(
        {
            "high_confidence": RT.is_high_confidence(matches),
            "matches": [{"score": round(m.score, 3), "example": m.example} for m in matches],
        }
    )
    return 0


def cmd_preflight(args) -> int:
    org = L.load_organization(args.root)
    result = RT.pre_flight_check(args.sql, org)
    _print_json(result.as_dict())
    return 0


def cmd_prepare(args) -> int:
    """Tier-independent safety pass: run the fan/chasm pre-flight, then (unless it
    refuses) apply the area's default_filters. Returns the SQL to actually execute.
    The query skill calls this on EVERY tier before handing SQL to psql/mysql/etc.,
    so the safety guarantees don't depend on going through execute_sql.py."""
    sql = args.sql
    if args.sql_file:
        sql = Path(args.sql_file).read_text()
    org = L.load_organization(args.root)
    pf = RT.pre_flight_check(sql, org)
    if pf.risk and pf.action == "refuse":
        _print_json({"action": "refuse", "risk": pf.risk, "reason": pf.reason,
                     "suggestion": pf.suggestion, "sql": sql})
        return 1
    run_sql = pf.rewritten_sql if (pf.action == "auto_rewrite" and pf.rewritten_sql) else sql
    final_sql, applied = RT.apply_default_filters(run_sql, org, area=args.area)
    _print_json({
        "action": pf.action,
        "risk": pf.risk,
        "sql": final_sql,
        "rewritten": bool(pf.action == "auto_rewrite"),
        "applied_filters": applied,
        # {output_column: unit}, traced through the final SQL — feed straight to
        # `format-table --units` so summed/aliased currency formats correctly.
        "units": RT.resolve_result_units(org, final_sql),
        "reason": pf.reason if pf.risk else None,
    })
    return 0


def cmd_receipt(args) -> int:
    """Assemble the trust receipt for an executed query — deterministically, from the
    SQL + the model. Replaces the LLM hand-building the receipt JSON in prose: tables,
    relationships, metrics, unreviewed-warnings, and model_version all come from
    parsing the SQL against the model (the SAME `runtime.assemble_receipt` the MCP
    server uses). The LLM may still append ad-hoc metrics + assumptions afterward."""
    from . import snapshot as SN
    sql = args.sql
    if args.sql_file:
        sql = Path(args.sql_file).read_text()
    org = L.load_organization(args.root)
    pf = RT.pre_flight_check(sql, org)
    applied = json.loads(args.applied_filters) if args.applied_filters else None
    receipt = RT.assemble_receipt(
        org, sql,
        model_version=SN.newest_version(args.root),
        applied_filters=applied,
        pre_flight=pf,
        freshness=args.freshness,
    )
    _print_json(receipt)
    return 0


def cmd_review_queue(args) -> int:
    from . import curate
    org = L.load_organization(args.root)
    _print_json(curate.review_queue(org))
    return 0


def cmd_review_items(args) -> int:
    from . import curate
    org = L.load_organization(args.root, include_rejected=True)
    _print_json(curate.all_items(org, scope=args.scope))
    return 0


def cmd_curate_gate(args) -> int:
    """The Phase-4 curate gate decision in ONE call: count still-queryable PII columns
    + pending pre-seed sign-offs, and say whether to open the explorer. Replaces the
    skill running `sm sensitive` + `sm review-items --scope preseed` and branching by
    hand. Turn-boundary-safe (same answer on a fresh run or a resume)."""
    from . import curate
    org = L.load_organization(args.root, include_rejected=True)
    pii = curate.sensitive_columns(org).get("count", 0)
    preseed = len(curate.all_items(org, scope="preseed"))
    _print_json({"pii_count": pii, "preseed_count": preseed,
                 "should_open_explorer": pii > 0 or preseed > 0})
    return 0


def cmd_model_tree(args) -> int:
    from . import curate
    org = L.load_organization(args.root, include_rejected=True)
    _print_json(curate.model_tree(org))
    return 0


def cmd_coverage(args) -> int:
    """Per-table column-description coverage + the enrichment-completeness verdict.
    The skill runs this at the end of Phase 2 (and reports it in the Phase 7 summary):
    `ok: false` with untouched columns means enrichment skipped the column pass."""
    from . import curate
    org = L.load_organization(args.root, include_rejected=True)
    _print_json(curate.column_coverage(org))
    return 0


def cmd_choice_coverage(args) -> int:
    """Coded columns whose `choice_field` skeleton still has blank labels. The enrichment
    runs this to confirm the value-enum decode ran — `ok: false` means coded columns are
    missing their {code:label} maps (the generator can't translate 'high' → 1 without them)."""
    from . import curate
    org = L.load_organization(args.root, include_rejected=True)
    _print_json(curate.unlabeled_choice_fields(org))
    return 0


def cmd_sensitive(args) -> int:
    """List the columns flagged `sensitive` (PII) that are still queryable — already-
    excluded ones (a rejected column, or any column under a rejected table) are NOT
    counted, since they're no longer in the runtime. The agami-connect Phase 4 curate gate
    uses this count to decide whether to open the explorer (so the gate stops re-opening
    once the user has excluded the flagged columns)."""
    from . import curate
    org = L.load_organization(args.root, include_rejected=True)
    flagged = curate.sensitive_columns(org)
    suspected = curate.suspected_sensitive_columns(org)
    # `count`/`columns` keep their meaning (flagged PII — the gate signal); `suspected` is the
    # second review tier (might-be-PII the strict flag missed, for the PII tab to confirm).
    _print_json({**flagged, "suspected": suspected["columns"], "suspected_count": suspected["count"]})
    return 0


def cmd_set_terminology(args) -> int:
    """Write the org-level domain glossary (term -> definition) onto org.yaml's
    `key_terminology` — the decoded-abbreviation legend enrichment produces. Merges by
    default (layers over a human's edits); --replace overwrites. Validated + committed."""
    from . import curate
    with open(args.file) as fh:
        terms = json.load(fh)
    if isinstance(terms, dict) and "key_terminology" in terms:
        terms = terms["key_terminology"]
    res = curate.set_key_terminology(args.root, terms, merge=not args.replace)
    _print_json({"applied": res.applied, "validated": res.validated,
                 "committed": res.committed, "errors": res.errors})
    return 0 if res.validated else 1


def cmd_curate(args) -> int:
    from . import curate
    with open(args.ops_file) as fh:
        ops = json.load(fh)
    if isinstance(ops, dict):
        ops = ops.get("ops", [])
    _stamp_approve_ops(ops)
    res = curate.apply(args.root, ops, signer=args.signer, role=args.role)
    _print_json(res.as_dict())
    return 0 if res.validated else 1


def cmd_approve_queue(args) -> int:
    """Sign off the whole pending review queue in one call — the first-class no-browser
    path (the HTML explorer's CLI equivalent). Reads the same queue `review-queue` builds
    (Rule 1 metrics/named-filters + Rule 2 joins/entities), turns each item into a
    self-stamped `approve` op, and applies via curate.apply. `--kind` narrows to one item
    type; `--dry-run` prints the ops without touching the model."""
    from . import curate
    org = L.load_organization(args.root)
    queue = curate.review_queue(org)
    kinds = set(args.kind) if args.kind else None
    ops = [{"op": "approve", "kind": it["kind"], "area": it["area"],
            "name": it["name"], "at": _utc_now_iso()}
           for bucket in ("rule_1", "rule_2") for it in queue.get(bucket, [])
           if kinds is None or it["kind"] in kinds]
    if args.dry_run:
        _print_json({"dry_run": True, "count": len(ops), "ops": ops})
        return 0
    res = curate.apply(args.root, ops, signer=args.signer, role=args.role)
    _print_json({"approved": len(ops), **res.as_dict()})
    return 0 if res.validated else 1


def cmd_add(args) -> int:
    from . import curate
    with open(args.file) as fh:
        items = json.load(fh)
    if isinstance(items, dict):
        items = items.get(args.kind + "s", items.get("items", []))
    res = curate.write_items(args.root, args.area, args.kind, items,
                             signer=args.signer, role=args.role)
    _print_json(res.as_dict())
    return 0 if res.validated else 1


def cmd_add_example(args) -> int:
    from . import curate
    with open(args.file) as fh:
        items = json.load(fh)
    if isinstance(items, dict):
        items = items.get("examples", items.get("items", []))
    res = curate.add_examples(args.root, args.area, items, signer=args.signer, role=args.role)
    _print_json(res.as_dict())
    return 0 if (res.applied or not res.skipped) else 1


def cmd_remove_example(args) -> int:
    """Reject prompt example(s) by question — `status: rejected`, kept in examples.yaml for
    audit but dropped from the runtime ranker. The packaged path for the dashboard's
    `reject N`, so a skill never hand-rewrites examples.yaml to drop an example."""
    from . import curate
    res = curate.remove_examples(args.root, args.area, args.question,
                                 signer=args.signer, role=args.role)
    _print_json({"rejected": res.applied, "skipped": res.skipped,
                 "validated": res.validated, "committed": res.committed})
    return 0 if (res.applied or not res.skipped) else 1


def cmd_suggest_units(args) -> int:
    """List the numeric **money** columns (so a currency `unit` can be stamped) using the
    tested name matcher — never a hand-rolled regex that mis-handles `count` inside
    `discount`. Skips columns that already carry a `unit`. Emits
    {money_columns: [{area, table, column, type}]} for the caller to confirm + apply."""
    from . import build as B
    org = L.load_organization(args.root)
    numeric = {"integer", "decimal", "float"}
    money = []
    for sa in org.subject_areas:
        for t in sa.tables_defined:
            for c in t.columns:
                if not c.unit and c.type in numeric and B.detect_money_column(c.name):
                    money.append({"area": sa.name, "table": t.name, "column": c.name, "type": c.type})
    _print_json({"money_columns": money})
    return 0


def cmd_set_units(args) -> int:
    """The APPLY half of suggest-units: stamp a currency/unit on every detected money column in
    ONE validated curate batch — so a skill never pipes suggest-units JSON through a hand-rolled
    script (the fragile glue that broke on empty stdin). Detects the same money columns (tested
    matcher); `--columns` overrides detection for non-obvious names; `--area` scopes it."""
    from . import build as B
    from . import curate
    unit = args.currency or args.unit
    if not unit:
        _print_json({"set": 0, "error": "pass --currency <ISO> or --unit <name>"})
        return 1
    org = L.load_organization(args.root)
    numeric = {"integer", "decimal", "float"}
    explicit = set(args.columns or [])
    ops = []
    for sa in org.subject_areas:
        if args.area and sa.name != args.area:
            continue
        for t in sa.tables_defined:
            for c in t.columns:
                if c.unit:
                    continue
                if explicit:
                    if f"{t.name}.{c.name}" not in explicit and c.name not in explicit:
                        continue
                elif not (c.type in numeric and B.detect_money_column(c.name)):
                    continue
                ops.append({"op": "edit", "kind": "table", "area": sa.name, "name": t.name,
                            "column": c.name, "field": "unit", "value": unit})
    if not ops:
        _print_json({"set": 0, "unit": unit, "reason": "no matching money columns"})
        return 0
    res = curate.apply(args.root, ops)
    _print_json({"set": len(res.applied), "unit": unit, "errors": res.errors})
    return 0 if res.validated else 1


def cmd_suggest_metrics(args) -> int:
    """Infer a sensible per-table metric set (count + SUM of additive cols + AVG of averageable
    cols, gated on aggregation class) and write them PROPOSED/unreviewed for bulk sign-off in the
    explorer — instead of asking the user to pick ~4 upfront. Rule 1 keeps proposed metrics out of
    any answer until approved, so a large suggested set can't degrade results."""
    from . import build as B
    from . import curate
    from . import dialects as D
    from . import introspect as INTRO
    org = L.load_organization(args.root)
    conn_type = {sc.name: sc.storage_type for sc in org.storage_connections}
    default_type = org.storage_connections[0].storage_type if org.storage_connections else "PostgreSQL"
    _dcache: dict = {}

    def _dialect(st: str):
        if st not in _dcache:
            try:
                _dcache[st] = D.get_dialect(st)
            except Exception:
                _dcache[st] = D.get_dialect("postgresql")
        return _dcache[st]

    suggested = written = auto = skipped_opaque = 0
    errors: list[str] = []
    for sa in org.subject_areas:
        if args.area and sa.name != args.area:
            continue
        existing = {m.name for m in sa.metrics}
        items: list[dict] = []
        for t in sa.tables_defined:
            # columns agami couldn't read yield no metric until described — count them so the
            # user knows describing the "couldn't read" pile unlocks more metrics on a re-run.
            skipped_opaque += sum(
                1 for c in t.columns
                if c.description_source == "ai_unknown" and not c.primary_key
                and (c.type == "boolean" or c.aggregation in ("additive", "averageable")))
            st = conn_type.get(t.storage_connection, default_type)
            for met in B.suggest_metrics(t, _dialect(st), max_per_table=args.max_per_table,
                                         now=INTRO._NOW):
                if met["name"] in existing:
                    continue
                existing.add(met["name"])
                items.append(met)
                suggested += 1
                if met.get("review_state") == "approved":
                    auto += 1
        if items:
            res = curate.write_items(args.root, sa.name, "metric", items)
            written += len(res.applied)
            errors += res.errors
    note = ("basic COUNT/SUM/AVG auto-approved (system-signed); flag rates & durations left "
            "proposed — review & sign off in the explorer (/agami-model)")
    if skipped_opaque:
        note += (f". {skipped_opaque} column(s) skipped as un-described (ai_unknown) — describe "
                 "them, then re-run suggest-metrics to pick up their metrics (incremental, no dupes)")
    _print_json({"suggested": suggested, "written": written, "auto_approved": auto,
                 "skipped_opaque": skipped_opaque, "errors": errors, "note": note})
    return 0 if not errors else 1


def cmd_describe_file(args) -> int:
    """Apply many column descriptions from a lightweight TSV — one per line,
    `<table.column>` or `<area.table.column>` then a TAB then the description — as ONE validated
    curate batch. So a skill emits a flat list (cheap, auditable) instead of authoring a Python
    generator script to build ops. `source:ai` → ai_unvalidated (earns trust through use)."""
    from . import curate
    text = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
    ops = []
    for ln in text.splitlines():
        if not ln.strip() or ln.lstrip().startswith("#") or "\t" not in ln:
            continue
        loc, desc = (p.strip() for p in ln.split("\t", 1))
        segs = loc.split(".")
        if not desc or len(segs) not in (2, 3):
            continue
        op = {"op": "edit", "kind": "table", "field": "description", "value": desc, "source": "ai"}
        if len(segs) == 3:
            op["area"], op["name"], op["column"] = segs
        else:
            op["name"], op["column"] = segs
        ops.append(op)
    if not ops:
        _print_json({"described": 0, "reason": "no valid '<loc>\\t<description>' lines"})
        return 0
    res = curate.apply(args.root, ops)
    _print_json({"described": len(res.applied), "errors": res.errors})
    return 0 if res.validated else 1


def cmd_format_table(args) -> int:
    """Format a result CSV into a deterministic markdown table — exact numbers, full
    grouping + currency symbols, never abbreviated. The skill (and later the MCP) emits
    this verbatim so verification numbers don't depend on the LLM's formatting."""
    import csv as _csv

    from . import units
    text = Path(args.csv_file).read_text() if args.csv_file else sys.stdin.read()
    reader = list(_csv.reader(io.StringIO(text)))
    if not reader:
        print("")
        return 0
    headers, rows = reader[0], reader[1:]
    unit_map = json.loads(args.units) if args.units else {}
    print(units.format_table(headers, rows, unit_map))
    return 0


def _preseed_gate(org) -> Optional[dict]:
    """Phase-4 enforcement, in code rather than prose: return a refusal payload when
    seeds must NOT be generated yet — i.e. metrics/entities the seeds would reference
    are still unreviewed. Generating few-shots on top of a guessed metric definition
    bakes that guess in. Returns None when nothing is pending (count 0). The skill's
    Phase-4c "continue anyway" path bypasses this with --after-review (the only place
    that's sanctioned, AFTER the user has been in the explorer)."""
    from . import curate
    pending = curate.all_items(org, scope="preseed")
    if not pending:
        return None
    return {
        "refused": "preseed_review_pending",
        "pending_count": len(pending),
        "pending": [{"name": it["name"], "entity_type": it["entity_type"]} for it in pending],
        "message": (
            f"{len(pending)} metric(s)/entity(ies) still need review — generating seeds "
            f"now would bake guessed definitions into your few-shots. Open /agami-model "
            f"preseed, sign off (or reject) them, then re-run. If you have already reviewed "
            f"in the explorer and accept proceeding with items still unreviewed, re-run with "
            f"--after-review."
        ),
    }


def _coverage_gate(org) -> Optional[dict]:
    """Enrichment-completeness gate: refuse to seed (or finish) a model whose column pass
    didn't finish. Two failure modes (see curate.column_coverage):
      - `columns_unenriched` — a table with NO column descriptions at all (the pass never ran).
      - `columns_underenriched` — a table the pass touched but which still has a wall of blank
        columns with non-self-evident names (skipped meaningful columns like `bptype`).
    Naked columns degrade the explorer AND every NL→SQL answer (descriptions are the
    generator's context). NOT bypassable: the fix is to finish the column pass."""
    from . import curate
    cov = curate.column_coverage(org)
    if cov["ok"]:
        return None
    unenr = cov["unenriched_tables"]
    if unenr:
        return {
            "refused": "columns_unenriched",
            "table_count": len(unenr),
            "unenriched_tables": unenr,
            "coverage_pct": cov["totals"]["coverage_pct"],
            "message": (
                f"{len(unenr)} table(s) have NO column descriptions at all — enrichment wrote the "
                f"table descriptions but skipped the column pass. Run Phase 2's column pass: "
                f"describe each meaningful column from sampled values, mark genuinely-opaque ones "
                f"description_source=ai_unknown (self-evident id/timestamps may stay blank), then "
                f"re-run. Tables: {', '.join(unenr[:10])}" + ("…" if len(unenr) > 10 else "")
            ),
        }
    under = cov["under_enriched_tables"]
    under_set = set(under)   # compute once, not per row
    skipped = {r["table"]: r["blank_meaningful_columns"]
               for r in cov["tables"] if r["table"] in under_set}
    return {
        "refused": "columns_underenriched",
        "table_count": len(under),
        "under_enriched_tables": under,
        "meaningful_blank": cov["totals"]["meaningful_blank"],
        "coverage_pct": cov["totals"]["coverage_pct"],
        "skipped_columns": skipped,
        "message": (
            f"{len(under)} table(s) have many MEANINGFUL columns left blank — the column pass "
            f"described the easy ones and stopped ({cov['totals']['meaningful_blank']} non-self-evident "
            f"blanks total). Describe them from sampled values (or mark genuinely-opaque ones "
            f"description_source=ai_unknown), then re-run. e.g. "
            + "; ".join(f"{t}: {', '.join(cols[:5])}" for t, cols in list(skipped.items())[:3])
        ),
    }


def cmd_seed_examples(args) -> int:
    """Validate candidate seed examples against the live DB and write the passing ones —
    the whole Phase-5 mechanical loop in one call (no throwaway validate-and-write script)."""
    from . import curate
    from .introspect import make_execute_sql_runner
    org = L.load_organization(args.root, include_rejected=True)
    # Gate 1 — enrichment completeness (NOT bypassable): every kept column must be described
    # or explicitly ai_unknown. Catches a model that enriched tables but skipped columns.
    block = _coverage_gate(org)
    if block is not None:
        _print_json(block)
        return 2
    # Gate 2 — preseed review (Phase 4): no seeding on unreviewed metrics/entities. Bypassable
    # only via the Phase-4c --after-review path, after the user has been in the explorer.
    if not getattr(args, "after_review", False):
        block = _preseed_gate(org)
        if block is not None:
            _print_json(block)
            return 2
    with open(args.file) as fh:
        cands = json.load(fh)
    if isinstance(cands, dict):
        cands = cands.get("examples", cands.get("items", []))
    runner = make_execute_sql_runner(args.profile)
    passing, rejected = curate.validate_seeds(cands, runner)
    res = curate.add_examples(args.root, args.area, passing) if passing else curate.ApplyResult()
    _print_json({"added": res.applied, "written": res.validated,
                 "committed": res.committed, "rejected": rejected})
    return 0


def cmd_seed_validate(args) -> int:
    """Phase-6 trust onboarding: run every written seed against the live DB and emit the
    examples-validation items. Each seed runs THROUGH execute_sql.py (the agami-query path)
    so the fan/chasm pre-flight + default_filters always apply — a raw driver could skip
    that and let a fan-out scan the whole table. A refused/errored seed is surfaced with
    its `error`, not faked. Replaces ad-hoc 'run all the seeds' scripts."""
    import csv as _csv
    import os
    import subprocess

    from . import units

    seeds = L.list_prompt_examples(args.root, args.area)
    exe = sys.executable
    script = str(Path(__file__).resolve().parent.parent / "execute_sql.py")
    # the safety pass inside execute_sql.py finds the model via AGAMI_ARTIFACTS_DIR —
    # point it at the profile dir's parent so the right model loads (fan/chasm + filters).
    env = {**os.environ, "AGAMI_ARTIFACTS_DIR": str(Path(args.root).resolve().parent)}
    cap = max(0, args.preview)
    # Load the model once so result numbers are formatted by the SAME units.py the query
    # path uses (currency symbol + grouping). The validation preview must MATCH the real
    # answer — a column with unit: INR shows ₹ here too, not a bare number. If the model
    # can't load, fall back to raw cells (no regression).
    try:
        fmt_org = L.load_organization(args.root)
    except Exception:
        fmt_org = None
    items: list[dict] = []
    for i, ex in enumerate(seeds, 1):
        sql = (ex.get("sql") or "").strip()
        item: dict = {"n": i, "question": ex.get("question", ""), "sql": sql,
                      "state": "unreviewed", "row_headers": [], "row_preview": [], "row_count": 0}
        if not sql:
            item["error"] = "seed has no SQL"
            items.append(item)
            continue
        proc = subprocess.run(
            [exe, script, "--profile", args.profile, "--area", args.area, "--sql", sql],
            capture_output=True, text=True, env=env,
        )
        if proc.returncode != 0:
            # SQL error, or a fan/chasm pre-flight refusal — surface it; never fake a result.
            item["error"] = (proc.stderr or "").strip()[:600] or f"execute_sql exit {proc.returncode}"
        else:
            rows = list(_csv.reader(io.StringIO(proc.stdout)))
            if rows:
                headers = rows[0]
                data = rows[1:]
                # trace each output column → unit through the seed's SQL, then format every
                # cell exactly like the query path (units.format_table): a unit'd column gets
                # its symbol/grouping, a bare number gets grouping; non-numbers pass through.
                unit_map = {}
                if fmt_org is not None:
                    try:
                        unit_map = RT.resolve_result_units(fmt_org, sql)
                    except Exception:
                        unit_map = {}
                if data:
                    # match a column's unit by name OR positional key (#ci) — like
                    # units.format_table — so a dialect that re-cases result headers (e.g.
                    # Snowflake returns TOTAL_OUTSTANDING for alias `total_outstanding`)
                    # still resolves the unit and shows the currency symbol.
                    data = [[units.format_cell(c, unit_map.get(h) or unit_map.get(f"#{ci}"))
                             for ci, (h, c) in enumerate(zip(headers, row))]
                            for row in data]
                item["row_headers"] = headers
                item["row_count"] = len(data)
                item["row_preview"] = data[:cap]
        items.append(item)
    _print_json(items)
    return 0


def _normalize_table_list(raw: Optional[list[str]]) -> Optional[list[str]]:
    """Split any element that arrived as one whitespace/comma-joined blob into separate names.
    Defends against the zsh gotcha where an unquoted `$TBLS` passes all 52 names as ONE argument
    (`--tables "incident orders …"` → one giant bogus table name). Idempotent on a clean list."""
    if not raw:
        return raw
    out: list[str] = []
    for item in raw:
        out.extend(p for p in re.split(r"[\s,]+", item.strip()) if p)
    return out or None


def cmd_introspect(args) -> int:
    from . import introspect as INTRO
    from . import validator as V

    tables = _normalize_table_list(args.tables)
    if getattr(args, "tables_file", None):
        # a newline-separated allowlist file — the shell-quoting-proof way to pass a big kept set
        # (no word-splitting to get wrong). `#` comments and blank lines ignored.
        text = Path(args.tables_file).expanduser().read_text(encoding="utf-8")
        from_file = [ln.strip() for ln in text.splitlines()
                     if ln.strip() and not ln.lstrip().startswith("#")]
        tables = (tables or []) + _normalize_table_list(from_file)

    runner = INTRO.make_execute_sql_runner(args.profile)
    progress = args.progress or str(
        Path(args.artifacts).expanduser() / args.profile / ".introspect" / "progress.log")
    org, report = INTRO.introspect(
        args.profile,
        args.db_type,
        runner=runner,
        artifacts_dir=args.artifacts,
        out_dir=args.out,
        tables=tables,
        exclude_columns=args.exclude_columns,
        dry_run=args.dry_run,
        bigquery_region=args.bigquery_region,
        progress_path=progress,
        append=getattr(args, "append", False),
    )
    res = V.validate(org)
    print(report.render())
    print(V.format_result(res))
    return 0 if res.ok else 1


def cmd_enrich_metadata(args) -> int:
    """Deterministically enrich columns from the database's OWN metadata/lookup tables —
    descriptions + choice_field labels — in one validated curate batch. No LLM, no generator
    script, no external doc fetch. Recognizes presets (e.g. ServiceNow sys_dictionary + sys_choice)
    or takes --preset; the authoritative platform metadata becomes the model's enrichment."""
    from . import curate
    from . import dialects as D
    from . import introspect as INTRO
    from . import metadata_sources as MS
    from .loader import load_organization

    root = Path(args.root).expanduser()
    org = load_organization(root)
    model_tables = [t.name for sa in org.subject_areas for t in sa.tables_defined]
    valid = {(t.name.lower(), c.name.lower())
             for sa in org.subject_areas for t in sa.tables_defined for c in t.columns}
    schema_of: dict[str, str] = {}
    for sa in org.subject_areas:
        for t in sa.tables_defined:
            schema_of.setdefault(t.name.lower(), t.schema_name)

    preset = args.preset or MS.detect_preset(model_tables)
    if not preset:
        _print_json({"enriched": False, "reason": "no metadata preset detected — pass --preset"})
        return 1
    sources = MS.usable_sources(preset, model_tables)
    if not sources:
        _print_json({"enriched": False, "preset": preset,
                     "reason": "preset's source table(s) are not in the model"})
        return 1

    dialect = D.get_dialect(args.db_type)
    runner = INTRO.make_execute_sql_runner(args.profile)
    ops: list[dict] = []
    fetched: dict[str, int] = {}
    dict_rows: list[dict] = []
    dict_cfg: dict = {}
    for role, cfg in sources.items():
        src = cfg["source"]
        sch = schema_of.get(src.lower())
        fq = dialect.qualified(sch, src) if sch else dialect.quote_ident(src)
        col_names = [v for k, v in cfg.items() if k.endswith("_col")]
        sel = ", ".join(dialect.quote_ident(c) for c in col_names)
        rows = runner(f"SELECT {sel} FROM {fq}") or []
        fetched[role] = len(rows)
        if role == "choice":
            ops += MS.choice_field_ops(rows, table_col=cfg["table_col"], column_col=cfg["column_col"],
                                       value_col=cfg["value_col"], label_col=cfg["label_col"], valid=valid)
        elif role == "dictionary":
            ops += MS.description_ops(rows, table_col=cfg["table_col"], column_col=cfg["column_col"],
                                      label_col=cfg.get("label_col"), comment_col=cfg.get("comment_col"),
                                      valid=valid)
            dict_rows, dict_cfg = rows, cfg

    # Inheritance: ServiceNow (and any table-inheritance platform) declares a shared field's
    # description ONCE under the parent table (`task`), but the column physically lives on every
    # child (`incident`/`problem`/…). description_ops keys on the declaring table, so the children
    # stay blank. Propagate the unambiguous element→description to any modeled column that's still
    # blank and didn't get a table-specific description above.
    inherited = 0
    if dict_rows and ("label_col" in dict_cfg or "comment_col" in dict_cfg):
        by_el = MS.descriptions_by_element(
            dict_rows, column_col=dict_cfg["column_col"],
            label_col=dict_cfg.get("label_col"), comment_col=dict_cfg.get("comment_col"))
        direct = {(o["name"].lower(), (o.get("column") or "").lower()) for o in ops
                  if o.get("kind") == "table" and o.get("column") and o.get("field") == "description"}
        for sa in org.subject_areas:
            for t in sa.tables_defined:
                for c in t.columns:
                    if (c.description or "").strip():
                        continue  # don't override an existing description
                    if (t.name.lower(), c.name.lower()) in direct:
                        continue  # already got a table-specific dictionary description
                    d = by_el.get(c.name.lower())
                    if d:
                        ops.append({"op": "edit", "kind": "table", "name": t.name, "column": c.name,
                                    "field": "description", "value": d, "source": "metadata"})
                        inherited += 1

    # 1) descriptions + choice_field — one validated curate batch
    cols_applied = 0
    errors: list[str] = []
    if ops:
        res = curate.apply(root, ops)
        cols_applied = len(res.applied)
        errors += res.errors

    # 2) reference/FK edges from the dictionary — the join-graph half. The table/area structure
    # the curate batch above touched only descriptions/choices, so the loaded `org` is still
    # valid for routing references (names, schemas, grain, areas unchanged).
    refs_added = refs_skipped_target = refs_declared = refs_unverified = 0
    if not args.skip_references:
        # field-name → target, from this instance's dictionary AND the preset's KNOWN reference
        # graph (declarative config). Instance declarations override the preset on conflict.
        decls = (MS.reference_declarations(
                    dict_rows, column_col=dict_cfg["column_col"], type_col=dict_cfg["type_col"],
                    reference_col=dict_cfg["reference_col"],
                    reference_type=dict_cfg.get("reference_type", "reference"))
                 if (dict_rows and "reference_col" in dict_cfg) else {})
        ref_map = {**MS.known_reference_graph(preset), **decls}
        refs_declared = len(ref_map)
        if ref_map:
            intra, cross, refs_skipped_target, refs_unverified = _build_verified_references(
                org, runner, dialect, ref_map)
            if intra or cross:
                rres = curate.add_relationships(root, intra=intra, cross=cross)
                refs_added = len(rres.applied)
                errors += rres.errors

    if not cols_applied and not refs_added:
        _print_json({"enriched": False, "preset": preset, "fetched": fetched,
                     "reference_fields_known": refs_declared,
                     "relationships_skipped_target_not_modelled": refs_skipped_target,
                     "reason": "nothing applicable for modelled columns", "errors": errors})
        return 0
    _print_json({"enriched": not errors, "preset": preset, "fetched": fetched,
                 "columns_enriched": cols_applied, "descriptions_inherited": inherited,
                 "relationships_added": refs_added,
                 "relationships_added_unverified": refs_unverified,
                 "reference_fields_known": refs_declared,
                 "relationships_skipped_target_not_modelled": refs_skipped_target,
                 "errors": errors})
    return 0 if not errors else 1


def _build_verified_references(org, runner, dialect, ref_map: dict) -> tuple[dict, list, int, int]:
    """Turn a `field → target` map (preset known graph ∪ dictionary declarations) into reference
    relationships, applied to EVERY modelled column with that name (inheritance-aware), and VERIFY
    each candidate by value-overlap against the live data before keeping it:
      overlap holds            → confirmed + system-approved (the standard join matches THIS data),
      overlap can't be checked → inferred/unreviewed (target/from too big to scan, or probe cap hit),
      overlap FAILS            → DROPPED (the standard join doesn't apply to this export — never
                                 blindly trust the preset).
    Returns `(intra, cross, skipped_target_count, unverified_count)`."""
    from . import introspect as INTRO
    specs: list[dict] = []
    skipped_target = unverified = probes = 0
    # resolve targets up front so we can verify before routing
    tindex = {t.name.lower(): t for sa in org.subject_areas for t in sa.tables_defined}
    for sa in org.subject_areas:
        for t in sa.tables_defined:
            for c in t.columns:
                tgt = ref_map.get(c.name.lower())
                if not tgt:
                    continue
                ttab = tindex.get(tgt.lower())
                if ttab is None:
                    skipped_target += 1
                    continue
                if not ttab.grain:
                    continue
                verified = None  # None = couldn't check
                if INTRO._too_big_to_probe(t) or INTRO._too_big_to_probe(ttab):
                    verified = None
                elif probes < INTRO.FK_OVERLAP_PROBE_CAP:
                    probes += 1
                    verified = INTRO._overlaps(dialect, runner, t, c.name, ttab, ttab.grain[0])
                if verified is False:
                    continue  # standard join doesn't hold in this data → drop
                spec = {"from_table": t.name, "from_column": c.name, "to_table": ttab.name}
                if verified:
                    spec.update(confidence="confirmed", review_state="approved",
                                signed_off_by="agami_enrich", signed_off_role="system",
                                signed_off_at=INTRO._NOW,
                                description=f"reference: {c.name} → {ttab.name}.{ttab.grain[0]} (value-overlap verified)")
                else:
                    unverified += 1
                    spec["description"] = f"reference: {c.name} → {ttab.name}.{ttab.grain[0]} (not overlap-verified — confirm)"
                specs.append(spec)
    intra, cross, skipped_more = _route_references(org, specs)
    return intra, cross, skipped_target + skipped_more, unverified


def _route_references(org, specs: list[dict]) -> tuple[dict, list, int]:
    """Resolve `{from_table, from_column, to_table}` specs into routed relationship dicts:
    `(intra: {area: [rel,…]}, cross: [xrel,…], skipped_target_count)`. SELF-references are kept
    (a hierarchical `parent → same table` is a real join). Edges whose TARGET isn't modelled are
    skipped and COUNTED (so pruning that drops a join target is reported, not silent). Already-
    present edges are skipped. References are `many_to_one` to the target's grain, written
    `inferred`/`unreviewed` (authoritative declaration, signed off in the explorer)."""
    from collections import defaultdict
    tindex: dict = {}
    for sa in org.subject_areas:
        for t in sa.tables_defined:
            tindex.setdefault(t.name.lower(), (t, sa.name))
    existing = set()
    for sa in org.subject_areas:
        for r in sa.relationships:
            existing.add((r.from_table.lower(), r.from_column.lower(), r.to_table.lower()))
    for r in org.cross_subject_area_relationships:
        existing.add((r.from_table.lower(), r.from_column.lower(), r.to_table.lower()))

    intra: dict = defaultdict(list)
    cross: list = []
    skipped_target = 0
    for spec in specs:
        ft, fc, tt = spec["from_table"], spec["from_column"], spec["to_table"]
        key = (ft.lower(), fc.lower(), tt.lower())
        if key in existing:
            continue
        fr, to = tindex.get(ft.lower()), tindex.get(tt.lower())
        if fr is None:
            continue
        if to is None:                 # target table pruned / not in the model → can't join, but count it
            skipped_target += 1
            continue
        ftab, farea = fr
        ttab, tarea = to
        if not ttab.grain:
            continue
        existing.add(key)
        base = {"from_table": ft, "from_column": fc, "to_table": tt, "to_column": ttab.grain[0],
                "from_schema": ftab.schema_name, "to_schema": ttab.schema_name,
                "relationship": "many_to_one", "join_type": "LEFT",
                # per-spec trust (a verified candidate is confirmed/approved); defaults otherwise
                "confidence": spec.get("confidence", "inferred"),
                "review_state": spec.get("review_state", "unreviewed"),
                "description": spec.get("description", f"reference: {fc} → {tt}.{ttab.grain[0]}")}
        for k in ("signed_off_by", "signed_off_role", "signed_off_at"):
            if spec.get(k):
                base[k] = spec[k]
        if farea == tarea:
            intra[farea].append(base)
        else:
            cross.append({**base, "from_subject_area": farea, "to_subject_area": tarea})
    return dict(intra), cross, skipped_target


def _load_render_prune():
    """Import the `render_prune` script (the prune-page renderer) and return the module.

    render_prune lives in the PLUGIN (`plugins/agami/scripts/`), not this package — a package-relative
    import fails on every layout (`site-packages/` installed, `src/` in a dev checkout; neither has it).
    Locate it via `AGAMI_PLUGIN_ROOT`, which the `sm` launcher always exports (deriving it from its own
    location if the caller didn't), so it's present in this `python -m semantic_model.cli` subprocess even
    under `sm`'s `cd /` + `PYTHONSAFEPATH` (an explicit `sys.path.insert` is honored). Raise a clear error
    if it's unset or doesn't point at the plugin.
    """
    plugin_root = os.environ.get("AGAMI_PLUGIN_ROOT")
    scripts_dir = Path(plugin_root).expanduser() / "scripts" if plugin_root else None
    if scripts_dir and (scripts_dir / "render_prune.py").is_file():
        d = str(scripts_dir)
        if d not in sys.path:
            sys.path.insert(0, d)
        import render_prune  # noqa: E402
        return render_prune
    raise RuntimeError(
        "render_prune.py not found — set AGAMI_PLUGIN_ROOT to the agami plugin dir (its scripts/ holds "
        f"the prune-page renderer). AGAMI_PLUGIN_ROOT={plugin_root!r}"
    )


def cmd_discover(args) -> int:
    """First pass: cheap discovery (tables + columns only) → inventory JSON +
    a prune HTML page. The user prunes, then `introspect --tables <kept>` runs
    the full build on only the kept tables. No grain/FK/row-count probes here."""
    from . import introspect as INTRO

    runner = INTRO.make_execute_sql_runner(args.profile)
    inventory = INTRO.discover_inventory(
        args.profile,
        args.db_type,
        runner=runner,
        tables=args.tables,
        schemas=args.schemas,
        bigquery_region=args.bigquery_region,
    )

    artifacts = Path(args.artifacts).expanduser()
    inv_path = (Path(args.inventory_out).expanduser() if args.inventory_out
                else artifacts / args.profile / ".introspect" / "inventory.json")
    inv_path.parent.mkdir(parents=True, exist_ok=True)
    inv_path.write_text(json.dumps(inventory, indent=2, default=str), encoding="utf-8")

    # Render the standalone prune page via the plugin's render_prune script (located by AGAMI_PLUGIN_ROOT).
    render_prune = _load_render_prune()

    manifest = render_prune.build_manifest(inventory)
    out_path = Path(args.out).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_prune.render(manifest), encoding="utf-8")

    _print_json({
        "profile": args.profile,
        "table_count": inventory["table_count"],
        "column_mode": inventory["column_mode"],
        "schemas": inventory["schemas"],
        "inventory_path": str(inv_path),
        "prune_html": str(out_path),
    })
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="semantic_model", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("validate", help="validate a profile tree")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_validate)

    sp = sub.add_parser("snapshot", help="stamp the model_version snapshot for a profile (e.g. after copying a model)")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_snapshot)

    sp = sub.add_parser("ensure-org-id", help="mint + persist the deployment org_id into org.yaml if absent (e.g. after copying a sample model)")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_ensure_org_id)

    sp = sub.add_parser("context", help="assemble get_table_context")
    sp.add_argument("root")
    sp.add_argument("--area", default=None)
    sp.add_argument("--tables", nargs="+", required=True)
    sp.add_argument("--columns", nargs="*", default=None)
    sp.add_argument(
        "--include",
        nargs="*",
        default=list(L.DEFAULT_CONTEXT_INCLUDE),
    )
    sp.set_defaults(func=cmd_context)

    sp = sub.add_parser("bundle", help="one-shot subject-area bundle")
    sp.add_argument("root")
    sp.add_argument("--area", required=True)
    sp.set_defaults(func=cmd_bundle)

    sp = sub.add_parser("areas", help="list subject areas")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_areas)

    sp = sub.add_parser("org-draft", help="print a human-narrative STARTER for ORGANIZATION.md (prompt only, no facts)")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_org_draft)

    sp = sub.add_parser("org-context", help="print the full domain context (human narrative + model-derived summary + glossary) for the LLM")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_org_context)

    sp = sub.add_parser("set-org", help="write the company name / description into the deployment organization record")
    sp.add_argument("artifacts_dir")
    sp.add_argument("--name", default=None)
    sp.add_argument("--description", default=None)
    sp.set_defaults(func=cmd_set_org)

    sp = sub.add_parser("examples", help="rank prompt examples for a query")
    sp.add_argument("root")
    sp.add_argument("--area", required=True)
    sp.add_argument("--query", required=True)
    sp.add_argument("--top-k", type=int, default=5)
    sp.set_defaults(func=cmd_examples)

    sp = sub.add_parser("preflight", help="fan-trap / chasm-trap pre-flight check")
    sp.add_argument("root")
    sp.add_argument("--sql", required=True)
    sp.set_defaults(func=cmd_preflight)

    sp = sub.add_parser("prepare", help="tier-independent safety pass: pre-flight + default_filters → SQL to run")
    sp.add_argument("root")
    sp.add_argument("--area", default=None)
    sp.add_argument("--sql", default=None)
    sp.add_argument("--sql-file", default=None, dest="sql_file")
    sp.set_defaults(func=cmd_prepare)

    sp = sub.add_parser("receipt", help="assemble the trust receipt for a query (deterministic, from SQL + model) — replaces hand-building it")
    sp.add_argument("root")
    sp.add_argument("--sql", default=None)
    sp.add_argument("--sql-file", default=None, dest="sql_file")
    sp.add_argument("--applied-filters", default=None, dest="applied_filters",
                    help="JSON list of default_filters applied (from `sm prepare`)")
    sp.add_argument("--freshness", default=None, help="optional freshness timestamp for tables_used")
    sp.set_defaults(func=cmd_receipt)

    sp = sub.add_parser("review-queue", help="trust-review items needing sign-off (Rule 1/2)")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_review_queue)

    sp = sub.add_parser("review-items", help="curatable entries, tab-classified (4-tab dashboard)")
    sp.add_argument("root")
    sp.add_argument("--scope", default="all", choices=["all", "rule1", "rule2", "preseed"],
                    help="all | rule1 (metrics/named-filters) | rule2 | preseed (metrics+entities seeds depend on)")
    sp.set_defaults(func=cmd_review_items)

    sp = sub.add_parser("curate-gate", help="Phase-4 gate decision: PII + pre-seed sign-off counts → should the explorer open?")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_curate_gate)

    sp = sub.add_parser("model-tree", help="browsable area→table→column tree (incl. rejected)")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_model_tree)

    sp = sub.add_parser("coverage", help="per-table column-description coverage + enrichment-completeness verdict (ok:false ⇒ columns were skipped)")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_coverage)

    sp = sub.add_parser("choice-coverage", help="coded columns whose choice_field labels are still blank (value-enum decode not done)")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_choice_coverage)

    sp = sub.add_parser("sensitive", help="list still-queryable columns flagged sensitive/PII + a count (excludes already-rejected columns/tables) — the Phase 4 curate gate signal")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_sensitive)

    sp = sub.add_parser("set-terminology", help="write the org domain glossary (term→definition) onto org.yaml key_terminology")
    sp.add_argument("root")
    sp.add_argument("--file", required=True, help="JSON object {term: definition, ...} (or {key_terminology: {...}})")
    sp.add_argument("--replace", action="store_true", help="replace the glossary instead of merging over it")
    sp.set_defaults(func=cmd_set_terminology)

    sp = sub.add_parser("curate", help="apply exclude/include/approve/reject/edit ops (validated)")
    sp.add_argument("root")
    sp.add_argument("--ops-file", required=True, help="JSON file: a list of op objects (or {ops:[...]})")
    sp.add_argument("--signer", default=None, help="sign-off email for approve ops")
    sp.add_argument("--role", default=None, help="sign-off role for approve ops")
    sp.set_defaults(func=cmd_curate)

    sp = sub.add_parser("approve-queue",
                        help="sign off the whole pending review queue in one call (no-browser path)")
    sp.add_argument("root")
    # required: curate._set_trust only stamps signed_off_* when a signer is present, so an
    # approve with no signer records an incomplete trust block and the validator reverts the
    # whole batch. A sign-off must record who — mirror that at the CLI surface.
    sp.add_argument("--signer", required=True, help="sign-off email recorded on each item")
    sp.add_argument("--role", required=True, help="sign-off role recorded on each item")
    sp.add_argument("--kind", action="append", choices=["metric", "entity", "relationship"],
                    help="restrict to one item kind (repeatable); default approves all")
    sp.add_argument("--dry-run", action="store_true", dest="dry_run",
                    help="print the approve ops that would be applied, without applying them")
    sp.set_defaults(func=cmd_approve_queue)

    sp = sub.add_parser("add", help="create metric/entity YAMLs from a JSON file (validated, revertable)")
    sp.add_argument("root")
    sp.add_argument("--kind", required=True, choices=["metric", "entity"])
    sp.add_argument("--area", required=True)
    sp.add_argument("--file", required=True, help="JSON: a list of items (or {metrics:[…]}/{entities:[…]})")
    sp.add_argument("--signer", default=None)
    sp.add_argument("--role", default=None)
    sp.set_defaults(func=cmd_add)

    sp = sub.add_parser("add-example", help="append/replace NL→SQL examples (prompt_examples/<area>/examples.yaml)")
    sp.add_argument("root")
    sp.add_argument("--area", required=True)
    sp.add_argument("--file", required=True,
                    help="JSON list of {question, sql, [tables, columns, metric, source, status, created_at]}")
    sp.add_argument("--signer", default=None)
    sp.add_argument("--role", default=None)
    sp.set_defaults(func=cmd_add_example)

    sp = sub.add_parser("remove-example", help="reject prompt example(s) by question (status: rejected — kept for audit, dropped from the runtime ranker)")
    sp.add_argument("root")
    sp.add_argument("--area", required=True)
    sp.add_argument("--question", required=True, action="append",
                    help="exact question text of an example to reject (repeatable for several)")
    sp.add_argument("--signer", default=None, help="who rejected it (recorded in the curation log)")
    sp.add_argument("--role", default=None)
    sp.set_defaults(func=cmd_remove_example)

    sp = sub.add_parser("set-units", help="stamp a currency/unit on detected money columns — the apply half of suggest-units, one validated batch")
    sp.add_argument("root")
    sp.add_argument("--currency", default=None, help="ISO currency code (USD/EUR/INR/…)")
    sp.add_argument("--unit", default=None, help="non-currency unit (cents/percent/days/…)")
    sp.add_argument("--area", default=None, help="restrict to one subject area")
    sp.add_argument("--columns", nargs="*", default=None,
                    help="explicit table.column (or bare column) list — overrides money detection")
    sp.set_defaults(func=cmd_set_units)

    sp = sub.add_parser("suggest-metrics", help="infer per-table measures (count/sum/avg, gated on aggregation class) as proposed/unreviewed for bulk sign-off — replaces ask-for-4")
    sp.add_argument("root")
    sp.add_argument("--area", default=None, help="restrict to one subject area")
    sp.add_argument("--max-per-table", type=int, default=10, dest="max_per_table",
                    help="cap measures per table (count always kept)")
    sp.set_defaults(func=cmd_suggest_metrics)

    sp = sub.add_parser("describe-file", help="apply many column descriptions from a TSV (loc<TAB>description, stdin or --file) in one validated batch — no generator script")
    sp.add_argument("root")
    sp.add_argument("--file", default=None, help="TSV path (default: read stdin)")
    sp.set_defaults(func=cmd_describe_file)

    sp = sub.add_parser("suggest-units", help="list numeric money columns (for currency-unit stamping) via the tested name matcher")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_suggest_units)

    sp = sub.add_parser("format-table", help="format a result CSV into a deterministic markdown table (exact numbers)")
    sp.add_argument("--csv-file", default=None, help="result CSV (header row + rows); omit to read stdin")
    sp.add_argument("--units", default=None, help='JSON header->unit map, e.g. {"outstanding":"INR"}')
    sp.set_defaults(func=cmd_format_table)

    sp = sub.add_parser("seed-examples", help="validate candidate seeds against the live DB + write the passing ones")
    sp.add_argument("root")
    sp.add_argument("--area", required=True)
    sp.add_argument("--profile", required=True, help="credentials profile (for the live-DB validation)")
    sp.add_argument("--file", required=True, help="JSON list of candidate {question, sql, [tables, columns, metric]}")
    sp.add_argument("--after-review", action="store_true",
                    help="bypass the preseed-review gate (Phase 4c only — the user has already "
                         "been in the explorer and chose to proceed with items still unreviewed)")
    sp.set_defaults(func=cmd_seed_examples)

    sp = sub.add_parser("seed-validate", help="run every written seed against the live DB (through execute_sql's safety pass) + emit examples-validation items")
    sp.add_argument("root")
    sp.add_argument("--area", required=True)
    sp.add_argument("--profile", required=True, help="credentials profile (for live-DB execution)")
    sp.add_argument("--preview", type=int, default=10, help="max result rows per seed in the dashboard preview (default 10)")
    sp.set_defaults(func=cmd_seed_validate)

    sp = sub.add_parser("introspect", help="introspect a live DB into the semantic model")
    sp.add_argument("--profile", required=True)
    sp.add_argument("--db-type", required=True, dest="db_type",
                    help="postgres|mysql|snowflake|bigquery|redshift|sqlite|sqlserver|"
                         "databricks|trino|oracle|duckdb|supabase")
    sp.add_argument("--artifacts", required=True, help="artifacts_dir (output: <artifacts>/<profile>/)")
    sp.add_argument("--out", default=None, help="override output dir")
    sp.add_argument("--tables", nargs="*", default=None,
                    help="schema.table allowlist — the prune step's kept set (also the "
                         "no-catalog/probe-only case). A single whitespace/comma-joined blob is "
                         "split defensively (zsh-safe).")
    sp.add_argument("--tables-file", default=None, dest="tables_file",
                    help="path to a newline-separated allowlist file — the shell-quoting-proof way "
                         "to pass a large kept set (no word-splitting to get wrong)")
    sp.add_argument("--exclude-columns", nargs="*", default=None, dest="exclude_columns",
                    help="schema.table.column list to mark excluded (the prune step's dropped columns)")
    sp.add_argument("--bigquery-region", default="region-us", dest="bigquery_region")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--progress", default=None,
                    help="progress-log path (flushed per phase/table; default "
                         "<artifacts>/<profile>/.introspect/progress.log) — tail it for a heartbeat")
    sp.add_argument("--append", action="store_true",
                    help="batched build: introspect only --tables this call, MERGE into the existing "
                         "model (prior tables loaded, not re-queried), write the union. Call once per "
                         "batch for quick foreground progress on a big schema (no background monitor).")
    sp.set_defaults(func=cmd_introspect)

    sp = sub.add_parser("enrich-metadata",
                        help="enrich columns from the DB's own metadata/lookup tables "
                             "(descriptions + choice_field), e.g. ServiceNow sys_dictionary/sys_choice")
    sp.add_argument("root")
    sp.add_argument("--profile", required=True)
    sp.add_argument("--db-type", required=True, dest="db_type")
    sp.add_argument("--preset", default=None,
                    help="metadata preset (e.g. servicenow); auto-detected from the model if omitted")
    sp.add_argument("--skip-references", action="store_true", dest="skip_references",
                    help="only descriptions + choice_field; do not add reference/FK relationships")
    sp.set_defaults(func=cmd_enrich_metadata)

    sp = sub.add_parser("discover",
                        help="cheap first pass: list tables + columns and render the prune page "
                             "(no grain/FK/row-count probes) — prune, then introspect the kept set")
    sp.add_argument("--profile", required=True)
    sp.add_argument("--db-type", required=True, dest="db_type",
                    help="postgres|mysql|snowflake|bigquery|redshift|sqlite|sqlserver|"
                         "databricks|trino|oracle|duckdb|supabase")
    sp.add_argument("--artifacts", required=True, help="artifacts_dir (inventory: <artifacts>/<profile>/.introspect/)")
    sp.add_argument("--out", required=True, help="output HTML path for the prune page")
    sp.add_argument("--inventory-out", default=None, dest="inventory_out",
                    help="override inventory JSON path (default <artifacts>/<profile>/.introspect/inventory.json)")
    sp.add_argument("--tables", nargs="*", default=None,
                    help="optional schema.table allowlist to scope discovery (probe-only case)")
    sp.add_argument("--schemas", nargs="*", default=None,
                    help="restrict discovery to these schemas (the user's schema pick)")
    sp.add_argument("--bigquery-region", default="region-us", dest="bigquery_region")
    sp.set_defaults(func=cmd_discover)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as e:
        # No model at this root yet. This is an expected, clean signal (not a crash):
        # callers fold the "does a model exist?" check into their first real command
        # (e.g. `sm areas`) instead of a separate filesystem probe. Exit 3 = "no model
        # here — run agami-connect"; distinct from validation errors (1) and usage (2).
        if "org.yaml" in str(e):
            _print_json({"error": "no_model", "detail": str(e)})
            return 3
        raise


if __name__ == "__main__":
    raise SystemExit(main())
