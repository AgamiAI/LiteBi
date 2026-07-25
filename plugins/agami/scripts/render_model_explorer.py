#!/usr/bin/env python3
"""
Model-explorer renderer for the agami-model skill.

Walks every YAML under <artifacts_dir>/<profile>/ exactly once, builds a
compact manifest of schemas → tables → fields (with current excluded /
review_state per entity), and writes a self-contained HTML file that lets
the user search the model and queue exclude / include actions for tables
and columns. Stdlib only (yaml is from PyYAML, the same dep used elsewhere
in the plugin).

This script does the YAML reading so the LLM doesn't have to. Total cost
per render: zero LLM tokens for the file walk, plus the one-shot write
cost of the static HTML template.

Usage:

    python3 render_model_explorer.py \\
        --profile main \\
        --artifacts-dir ~/agami-artifacts \\
        --out <artifacts_dir>/local/model/20260512-101500.html

The HTML's MANIFEST_JSON schema is documented at the top of
shared/model-explorer-template.html.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

import _interp  # noqa: F401 — re-exec under agami's configured interpreter if PyYAML is missing

SHARED_DIR = Path(__file__).resolve().parent.parent / "shared"
TEMPLATE_PATH = SHARED_DIR / "model-explorer-template.html"
LOGO_DARK_PATH = SHARED_DIR / "agami-logo-dark.svg"
LOGO_LIGHT_PATH = SHARED_DIR / "agami-logo-light.svg"


def build_manifest(profile_dir: Path, profile: str) -> dict:
    """Build the explorer manifest from the semantic model. Subject areas map to
    the explorer's top-level groups ("schemas"), tables → tables, columns → fields.
    Loads with include_rejected=True so excluded entries show (toggleable in the UI).
    """
    import sys as _sys
    _scripts = str(Path(__file__).resolve().parent)
    if _scripts not in _sys.path:
        _sys.path.insert(0, _scripts)
    from semantic_model import build as _B
    from semantic_model import loader as _L

    org = _L.load_organization(profile_dir, include_rejected=True)
    storage_type = (org.storage_connections[0].storage_type
                    if getattr(org, "storage_connections", None) else "")
    out_schemas: list[dict] = []
    total_tables = total_fields = total_excluded_tables = total_excluded_fields = 0
    # flat top-level lists (each entry tagged with its `area`) — mirrors the existing
    # flat metrics section, so the template renders them with one proven pattern
    metrics_out: list[dict] = []
    entities_out: list[dict] = []
    rels_out: list[dict] = []
    examples_out: list[dict] = []
    areas_out: list[dict] = []

    org_md_path = profile_dir / "datasource.md"
    organization_md = org_md_path.read_text(encoding="utf-8") if org_md_path.exists() else ""
    import re as _re

    from semantic_model import org_draft as _OD
    # `organization_md` is the human's narrative ONLY — this is what the edit box writes back,
    # so model facts must never be folded in here (or saving would persist them). If blank,
    # offer the starter prompt. The model-derived summary (subject areas, conventions, decoded
    # glossary) is a SEPARATE read-only field, computed fresh — see `derived_context`.
    if not _re.sub(r"<!--.*?-->", "", organization_md, flags=_re.DOTALL).strip():
        organization_md = _OD.starter_organization_md(org)
    # Read-only derived block WITHOUT the curated glossary — that's rendered as an editable
    # panel (the curated key_terminology is a first-class structured field users add/correct).
    derived_context_md = _OD.derived_context(org, with_curated_glossary=False)
    key_terminology = dict(getattr(org, "key_terminology", {}) or {})

    for sa in org.subject_areas:
        out_tables: list[dict] = []
        for t in sa.tables_defined:
            total_tables += 1
            t_excluded = t.review_state == "rejected"
            if t_excluded:
                total_excluded_tables += 1
            qname = f"{sa.name}.{t.name}"
            # column → its semantic group (for the wide-table grouped view); a column may
            # appear in at most one group, so invert the table's column_groups once.
            col_group = {cn: gname for gname, members in (t.column_groups or {}).items()
                         for cn in members}
            fields_out: list[dict] = []
            for c in t.columns:
                total_fields += 1
                f_excluded = c.review_state == "rejected"
                if f_excluded:
                    total_excluded_fields += 1
                fields_out.append({
                    "name": c.name, "qname": f"{qname}.{c.name}", "type": c.type,
                    "description": c.description, "description_source": c.description_source,
                    "aggregation": c.aggregation,
                    "review_state": c.review_state,
                    "origin": "", "confidence": c.confidence, "excluded": f_excluded,
                    "sensitive": c.sensitive,
                    # broader "might be PII" the strict flag missed — drives the PII tab's
                    # suspected tier (review aid; never auto-marks).
                    "suspected_pii": (not c.sensitive) and _B.suspected_pii(c.name),
                    "unit": c.unit, "caveats": c.caveats,
                    "date_format": c.date_format, "timezone": c.timezone,
                    "group": col_group.get(c.name, ""),
                })
            out_tables.append({
                "name": t.name, "qname": qname, "description": t.description,
                "description_source": t.description_source,
                "row_count": (t.performance_hints.estimated_row_count
                              if t.performance_hints else None),
                "review_state": t.review_state, "origin": "", "excluded": t_excluded,
                "yaml_path": f"subject_areas/{sa.name}/tables/{t.name}.yaml",
                "grain": t.grain, "caveats": t.caveats, "default_filters": t.default_filters,
                "synonyms": [], "area": sa.name, "db_schema": t.schema_name or "",
                # ordered group names for the wide-table grouped field view (empty on
                # narrow tables — the UI then just lists fields flat)
                "column_groups": list((t.column_groups or {}).keys()),
                "column_group_descriptions": t.column_group_descriptions or {},
                "fields": fields_out,
            })

        for e in sa.entities:
            entities_out.append({
                "name": e.name, "qname": f"{sa.name}.{e.name}", "plural": e.plural,
                "other_names": e.other_names, "value_pattern": e.value_pattern,
                "maps_to": [f"{m.table}.{m.column}" for m in e.maps_to],
                "primary_table": e.resolved_primary_table,
                "description": e.description, "review_state": e.review_state,
                "confidence": e.confidence, "excluded": e.review_state == "rejected",
                "signed_off_by": e.signed_off_by, "signed_off_role": e.signed_off_role,
                "rule": 2, "area": sa.name,
            })

        for mm in sa.metrics:
            metrics_out.append({
                "name": mm.name, "qname": f"{sa.name}.{mm.name}", "calculation": mm.calculation,
                "bindings": mm.bindings, "unit": mm.unit, "other_names": mm.other_names,
                "non_additive_dimensions": mm.non_additive_dimensions,
                "semi_additive_agg": mm.semi_additive_agg,
                "source_tables": mm.source_tables, "primary_table": mm.primary_table,
                "description": mm.description,
                "review_state": mm.review_state, "confidence": mm.confidence,
                "excluded": mm.review_state == "rejected",
                "signed_off_by": mm.signed_off_by, "signed_off_role": mm.signed_off_role,
                "rule": 1, "area": sa.name,
            })

        for r in sa.relationships:
            rels_out.append({
                "qname": f"{sa.name}.{r.from_table}->{r.to_table}",
                "from_table": r.from_table, "from_column": r.from_column,
                "to_table": r.to_table, "to_column": r.to_column, "on": r.on,
                "from_schema": r.from_schema, "to_schema": r.to_schema,
                "cross_schema": r.cross_schema,
                "cardinality": r.relationship, "description": r.description,
                "review_state": r.review_state, "confidence": r.confidence,
                "excluded": r.review_state == "rejected",
                "signed_off_by": r.signed_off_by, "signed_off_role": r.signed_off_role,
                "rule": 2, "area": sa.name,
            })

        for i, ex in enumerate(_L.list_prompt_examples(profile_dir, sa.name)):
            examples_out.append({
                "n": i, "qname": f"{sa.name}#{i}", "question": ex.get("question", ""),
                "sql": ex.get("sql", ""), "tables": ex.get("tables", []),
                "source": ex.get("source", ""), "status": ex.get("status", ""), "area": sa.name,
            })

        out_schemas.append({"name": sa.name, "description": sa.description, "tables": out_tables})

        # subject-area metadata for the Subject Areas tab (a subject area is the
        # model's primary unit — the engine proposes one for small DBs, several for large)
        db_schemas = sorted({t.schema_name for t in sa.tables_defined if t.schema_name})
        areas_out.append({
            "name": sa.name,
            "description": sa.description,
            "default_time_window": sa.default_time_window,
            "db_schemas": db_schemas,
            "tables": [t.name for t in sa.tables_defined],
            "table_count": len(sa.tables_defined),
            "entity_count": len(sa.entities),
            "metric_count": len(sa.metrics),
            "relationship_count": len(sa.relationships),
        })

    # org-level cross-area joins (edges between two subject areas)
    cross_out: list[dict] = []
    for r in getattr(org, "cross_subject_area_relationships", []) or []:
        fa = getattr(r, "from_subject_area", "")
        ta = getattr(r, "to_subject_area", "")
        cross_out.append({
            "qname": f"{fa}.{r.from_table}->{ta}.{r.to_table}",
            "from_subject_area": fa, "to_subject_area": ta,
            "from_table": r.from_table, "from_column": r.from_column,
            "to_table": r.to_table, "to_column": r.to_column, "on": r.on,
            "from_schema": r.from_schema, "to_schema": r.to_schema,
            "cross_schema": r.cross_schema,
            "cardinality": r.relationship, "description": r.description,
            "review_state": r.review_state, "confidence": r.confidence,
            "excluded": r.review_state == "rejected",
            "signed_off_by": r.signed_off_by, "signed_off_role": r.signed_off_role,
            # rendered as a reviewable join (Joins tab + Review queue), like intra-area joins;
            # `area` (the from-side) is what a curate op targets — the org-level fallback in
            # curate resolves it to cross_subject_area_relationships.
            "rule": 2, "cross_area": True, "area": fa,
        })

    return {
        "profile": profile,
        "organization_md": organization_md,
        # model-derived domain summary (read-only in the UI; not part of the editable file)
        "derived_context_md": derived_context_md,
        # the curated glossary (term → definition) — EDITABLE in the explorer, written back
        # via `cli set-terminology`. Separate from the read-only derived block above.
        "key_terminology": key_terminology,
        "storage_type": storage_type,
        "totals": {
            "schemas": len(out_schemas), "subject_areas": len(areas_out),
            "tables": total_tables, "fields": total_fields,
            "excluded_tables": total_excluded_tables, "excluded_fields": total_excluded_fields,
            "metrics": len(metrics_out), "entities": len(entities_out),
            "relationships": len(rels_out), "examples": len(examples_out), "named_filters": 0,
            "cross_relationships": len(cross_out),
            # the model was validated on write (the loader only succeeds on a valid tree),
            # so a successful render implies a structurally-valid model.
            "validated": True,
        },
        "schemas": out_schemas, "subject_areas": areas_out, "cross_relationships": cross_out,
        "metrics": metrics_out, "entities": entities_out,
        "relationships": rels_out, "examples": examples_out, "named_filters": [],
    }


def render(*, title: str, profile: str, manifest: dict) -> str:
    template = TEMPLATE_PATH.read_text()
    logo_dark = LOGO_DARK_PATH.read_text() if LOGO_DARK_PATH.exists() else ""
    logo_light = LOGO_LIGHT_PATH.read_text() if LOGO_LIGHT_PATH.exists() else ""
    theme_css = (SHARED_DIR / "theme.css").read_text()

    # The manifest embeds arbitrary model text (descriptions, datasource.md, SQL).
    # Escape `</` so a `</script>` in that text can't terminate the <script> block that
    # holds `const manifest = …` (JS unescapes `<\/` back to `</`). The template's doc
    # comment carries no real `{{…}}` tokens, so a `-->` in the text can't close it.
    manifest_json = json.dumps(manifest, separators=(",", ":")).replace("</", "<\\/")

    # Human-readable generation timestamp (e.g. "10 Jun 2026, 13:57 UTC") — the
    # explorer shows this verbatim, so format it here rather than re-parsing ISO in JS.
    now = datetime.datetime.now(datetime.timezone.utc)
    generated_at = now.strftime("%-d %b %Y, %H:%M UTC")

    out = (
        template
        .replace("{{REPORT_TITLE}}", title)
        .replace("{{PROFILE}}", profile)
        .replace("{{GENERATED_AT}}", generated_at)
        .replace("{{MANIFEST_JSON}}", manifest_json)
        .replace("{{AGAMI_LOGO_DARK_TEXT}}", logo_dark)
        .replace("{{AGAMI_LOGO_LIGHT_TEXT}}", logo_light)
        .replace("{{THEME_CSS}}", theme_css)
    )
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--profile", required=True,
                   help="Profile name (e.g., 'main', 'staging', 'analytics')")
    p.add_argument("--artifacts-dir", required=True,
                   help="Root artifacts directory (typically ~/agami-artifacts)")
    p.add_argument("--title",
                   help="Dashboard title (default: 'Model explorer · <profile>')")
    p.add_argument("--out", required=True,
                   help="Output HTML path")
    p.add_argument("--manifest-out",
                   help="Optional: also dump the raw manifest JSON to this path")
    p.add_argument("--initial-tab", default="auto",
                   choices=["auto", "organization", "tables", "metrics", "entities",
                            "joins", "examples", "review", "queued"],
                   help="Tab the dashboard opens on. 'auto' (default) opens on Review when "
                        "anything needs sign-off, else Tables; 'review' forces the sign-off queue.")
    args = p.parse_args()

    artifacts_dir = Path(os.path.expanduser(args.artifacts_dir)).resolve()
    profile_dir = artifacts_dir / args.profile
    if not profile_dir.is_dir():
        sys.stderr.write(f"agami: profile dir not found at {profile_dir}\n")
        return 1

    manifest = build_manifest(profile_dir, args.profile)
    # 'auto' default: lead with the sign-off queue when there's pending review work
    # (unreviewed metrics/entities/joins, or columns agami couldn't read), else Tables.
    initial = args.initial_tab
    if initial == "auto":
        pending = 0
        for arr in (manifest["metrics"], manifest["entities"], manifest["relationships"]):
            pending += sum(1 for x in arr
                           if x.get("review_state") in ("unreviewed", "stale") and not x.get("excluded"))
        for sc in manifest["schemas"]:
            for t in sc.get("tables", []):
                pending += sum(1 for f in t.get("fields", [])
                               if f.get("description_source") == "ai_unknown" and not f.get("excluded"))
        initial = "review" if pending > 0 else "tables"
    manifest["initial_tab"] = initial
    if not manifest["schemas"]:
        sys.stderr.write(
            f"agami: no schemas found in {profile_dir}. Run /agami-connect first.\n"
        )
        return 1

    title = args.title or f"Model explorer · {args.profile}"
    out_path = Path(os.path.expanduser(args.out)).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render(title=title, profile=args.profile, manifest=manifest))

    if args.manifest_out:
        mpath = Path(os.path.expanduser(args.manifest_out)).resolve()
        mpath.parent.mkdir(parents=True, exist_ok=True)
        mpath.write_text(json.dumps(manifest, indent=2))

    t = manifest["totals"]

    def _plural(n: int, word: str) -> str:
        return f"{n} {word}{'' if n == 1 else 's'}"

    print(
        f"Wrote {out_path} "
        f"({_plural(t['schemas'], 'schema')} · {_plural(t['tables'], 'table')} · "
        f"{_plural(t['fields'], 'field')}; "
        f"{t['excluded_tables']} tables + {t['excluded_fields']} columns currently excluded)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
