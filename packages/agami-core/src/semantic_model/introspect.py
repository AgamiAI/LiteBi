"""Live-DB introspection → the agami semantic model.

Replaces the legacy introspection (`information_schema`-only, LLM-authored YAMLs)
with a deterministic, capability-aware engine that builds the structural model
directly from a live database, across every supported dialect. The skill layers
LLM enrichment (prose descriptions, entities, metrics, value_patterns) on top.

Two modes, auto-detected **per capability** (never block on a missing catalog):

  * Catalog mode — `information_schema` / PRAGMA / data-dictionary reachable →
    precise structure (declared types, PKs, FKs, row estimates).
  * Probe mode — catalog denied → recover structure from the data itself through
    the same CSV executor: column names via the universal `WHERE 1=0` header,
    types from a value sample, grain from uniqueness probes, FKs from name+type
    match confirmed by value-overlap. Everything inferred lands at lower
    confidence / unreviewed → the trust-layer review queue.

The DB is reached through an injected `runner(sql) -> list[dict]` so the engine
is testable without a live database (tests pass a canned runner). The default
runner shells out to the sibling `execute_sql.py` and parses its CSV — the same
local-only execution path the rest of agami uses; no new egress.
"""

from __future__ import annotations

import csv
import io
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

# Introspection-run timestamp for system sign-offs on auto-approved structure.
_NOW = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# At/above this row count a full scan is slow enough that the query path warns + suggests
# narrowing. For such tables we record the date/time columns as `recommended_filters` (the
# natural "narrow by date" answer) so the warning fires only when a query lacks one.
_LARGE_TABLE_ROWS = 1_000_000
# ...but only when there's a clear handful of date columns. A wide mart with a dozen+ date
# columns (e.g. per-category open/close dates) has no single obvious scan key, so leave it
# empty for the real partition/index pass rather than listing noise.
_MAX_DATE_FILTER_COLS = 6
# Above this row count we SKIP the COUNT(DISTINCT) grain-uniqueness probe. With no catalog
# PK, that probe full-scans the table once PER id-ish candidate column to find a unique one.
# On a 40M-row / 40GB fact table with a dozen *_id candidates and a composite (or absent)
# grain, that's a dozen fruitless full scans — tens of minutes of pure waste — to arrive at
# the empty grain that is the CORRECT answer for such a table anyway (its real grain is
# composite, which a single-column probe can't discover). A catalog PK is always honored
# regardless of size; this guard only governs the scan-heavy fallback. Tuned to clear all
# ordinary dimension/operational tables while catching the giant fact tables.
_GRAIN_PROBE_MAX_ROWS = 5_000_000

from . import build
from . import dialects as D
from .models import (
    Column,
    Organization,
    PerformanceHints,
    Relationship,
    StorageConnection,
    Table,
    bare_name,
)

Runner = Callable[[str], list[dict]]


class _Row(dict):
    """Case-insensitive row lookup that preserves original key casing.

    Catalog metadata queries read fixed lowercase keys (``r["column_name"]``),
    but uppercasing dialects (Snowflake, Oracle, …) return the header as
    ``COLUMN_NAME``. Lookups here fold case so the engine finds them, while
    iteration / ``.keys()`` still yields the original casing — probe mode reads
    real column names off ``.keys()`` and must keep the DB's true casing.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._ci = {k.lower(): k for k in self.keys()}

    def __getitem__(self, key):
        if super().__contains__(key):  # exact hit first (preserves dict semantics)
            return super().__getitem__(key)
        return super().__getitem__(self._ci[key.lower()])

    def __contains__(self, key):
        return super().__contains__(key) or key.lower() in self._ci

    def get(self, key, default=None):
        return self[key] if key in self else default


SCRIPT_DIR = Path(__file__).resolve().parent.parent  # plugins/agami/scripts
SAMPLE_ROWS = 500
ENUM_MAX_DISTINCT = 25
PROBE_TABLE_CAP = 200  # cap value-overlap probing work
# Safety valve for confirming DECLARED-but-unenforced catalog FKs (Redshift/Databricks/Trino)
# by value-overlap: each confirmation is one sequential COUNT probe, so a schema that declares
# hundreds of FKs could otherwise fire hundreds of round-trips. Past the budget we stop probing
# and leave the remaining declared FKs inferred/unreviewed (they still surface for sign-off).
FK_OVERLAP_PROBE_CAP = 300
# Above this FROM-table row estimate we SKIP the value-overlap scan and leave the join
# unverified (inferred/unreviewed) rather than scan millions of rows over the network — the
# guard that stops a 50-table Redshift introspect from crawling for half an hour on big fact
# tables (incident/task/metric_instance). Mirrors the grain-probe size guard.
_OVERLAP_PROBE_MAX_ROWS = 3_000_000


def _progress(path: Optional[Path], msg: str) -> None:
    """Append a FLUSHED progress line so a tailing skill can surface a heartbeat during a long
    introspection (the engine is otherwise silent until it returns — which reads as 'stuck' on a
    big Redshift schema). Best-effort; never raises."""
    if path is None:
        return
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(msg.rstrip("\n") + "\n")
            fh.flush()
    except Exception:
        pass


@dataclass
class IntrospectReport:
    profile: str
    db_type: str
    out_dir: str
    dry_run: bool
    mode_per_capability: dict[str, str] = field(default_factory=dict)  # capability -> catalog|probe
    schemas: list[str] = field(default_factory=list)
    table_count: int = 0
    relationship_count: int = 0
    subject_areas: list[str] = field(default_factory=list)
    deep_tables: list[str] = field(default_factory=list)
    sensitive_columns: int = 0
    notes: list[str] = field(default_factory=list)
    files_written: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"# introspection report — {self.profile} ({self.db_type})",
            f"out_dir: {self.out_dir}   (dry_run={self.dry_run})",
            f"capability modes: {self.mode_per_capability}",
            f"schemas: {self.schemas}",
            f"tables: {self.table_count}   relationships: {self.relationship_count}",
            f"subject_areas ({len(self.subject_areas)}): {', '.join(self.subject_areas)}",
            f"deep tables: {', '.join(self.deep_tables) or '(none)'}",
            f"sensitive columns: {self.sensitive_columns}",
            "## notes",
            *[f"  - {n}" for n in self.notes],
            f"## files ({len(self.files_written)})",
            *[f"  {f}" for f in self.files_written[:60]],
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Default runner: shell to execute_sql.py, parse CSV
# ---------------------------------------------------------------------------


def make_execute_sql_runner(profile: str, python: Optional[str] = None) -> Runner:
    exe = python or sys.executable
    script = str(SCRIPT_DIR / "execute_sql.py")

    def run(sql: str) -> list[dict]:
        proc = subprocess.run(
            [exe, script, "--profile", profile, "--sql", sql],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or f"execute_sql exit {proc.returncode}")
        return [_Row(r) for r in csv.DictReader(io.StringIO(proc.stdout))]

    return run


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


def introspect(
    profile: str,
    db_type: str,
    *,
    runner: Runner,
    artifacts_dir: str | Path,
    out_dir: Optional[str | Path] = None,
    tables: Optional[list[str]] = None,
    exclude_columns: Optional[list[str]] = None,
    dry_run: bool = False,
    bigquery_region: str = "region-us",
    progress_path: Optional[str | Path] = None,
    append: bool = False,
) -> tuple[Organization, IntrospectReport]:
    """Introspect a live DB into the semantic model and (unless dry_run) write the
    canonical tree. `tables` (optional) is the allowlist of `schema.table` to build —
    the prune step's kept set (also the no-catalog case where enumeration is denied).
    `exclude_columns` (optional, `schema.table.column`) marks those columns excluded
    (the prune step's dropped columns).
    """
    dialect = D.get_dialect(db_type)
    if isinstance(dialect, D.BigQuery):
        dialect = D.BigQuery(region=bigquery_region)
    conn_name = f"{profile}_{dialect.name.lower()}"
    artifacts_dir = Path(artifacts_dir)
    out = Path(out_dir) if out_dir else (artifacts_dir / profile)

    report = IntrospectReport(profile=profile, db_type=db_type, out_dir=str(out), dry_run=dry_run)

    pp = Path(progress_path) if progress_path else None
    if pp is not None:
        try:
            pp.parent.mkdir(parents=True, exist_ok=True)
            pp.write_text("", encoding="utf-8")  # fresh log per run
        except Exception:
            pp = None

    # 1. discover (schema, table) pairs
    pairs = _discover_tables(dialect, runner, tables, report)
    if not pairs:
        raise RuntimeError(
            "no tables discovered. If the catalog is locked down, pass an explicit "
            "table allowlist (schema.table) so probe mode can describe them."
        )
    _progress(pp, f"discovered {len(pairs)} tables — reading columns, grain, keys")

    # 2. per-table columns + grain
    built: list[Table] = []
    grain_by_table: dict[str, set[str]] = {}
    _step = max(1, len(pairs) // 10)   # throttle the heartbeat to ~10 lines, not one per table
    for i, (schema, table) in enumerate(pairs, 1):
        t = _build_table(dialect, runner, schema, table, conn_name, report)
        built.append(t)
        grain_by_table[t.name] = set(t.grain)
        if i % _step == 0 or i == len(pairs):
            _progress(pp, f"columns+grain {i}/{len(pairs)}")

    # Fail-fast on tables that yielded NO columns — a bogus allowlist entry (e.g. a malformed
    # --tables blob that mis-joined 52 names into one) describes nothing, and we must NOT persist
    # a garbage table. Drop them with a clear note; if that leaves nothing, raise rather than
    # write an empty model.
    empty = [t.name for t in built if not t.columns]
    if empty:
        built = [t for t in built if t.columns]
        grain_by_table = {t.name: set(t.grain) for t in built}
        report.notes.append(
            f"dropped {len(empty)} table(s) with no readable columns (not found / bad allowlist): "
            + ", ".join(empty[:8]) + (" …" if len(empty) > 8 else ""))
    if not built:
        raise RuntimeError(
            "no allowlisted table could be described — every name yielded zero columns. "
            "Check the --tables list (a shell-quoting issue can mis-join all names into one); "
            "or pass --tables-file with one schema.table per line.")

    # 2b. apply prune-step column exclusions (the user dropped these in the prune
    # view). Match on schema.table.column, with a schema-less table.column fallback.
    # Excluded columns are marked `rejected` so they're kept for audit but off the
    # runtime — same state the model explorer's "exclude columns" produces.
    if exclude_columns:
        ex = set(exclude_columns)
        for t in built:
            for c in t.columns:
                if (f"{t.schema_name}.{t.name}.{c.name}" in ex
                        or f"{t.name}.{c.name}" in ex):
                    c.review_state = "rejected"

    # 2c. APPEND (batched introspection): fold the EXISTING model's tables back in — loaded from
    # disk, never re-queried — so each batch call builds only its own tables but writes the full
    # union. Lets the skill introspect ~10 tables per quick foreground call (natural progress, no
    # background monitor) and still end with a complete model. Safe during onboarding (no hand-
    # edits to lose); the relationship pass below rebuilds across the union, prior edges kept.
    prev_rels: list[Relationship] = []
    skip_keys: set = set()
    if append and not dry_run and (out / "datasource.yaml").exists():
        from .loader import load_organization
        prev = load_organization(out, include_rejected=True)
        batch_names = {t.name for t in built}
        for sa in prev.subject_areas:
            for t in sa.tables_defined:
                if t.name not in batch_names:        # keep prior tables (don't re-query)
                    built.append(t)
                    grain_by_table.setdefault(t.name, set(t.grain))
        for sa in prev.subject_areas:
            prev_rels.extend(sa.relationships)
        prev_rels.extend(prev.cross_subject_area_relationships)
        skip_keys = {(r.from_table.lower(), r.from_column.lower(), r.to_table.lower()) for r in prev_rels}
        report.notes.append(f"append: merged {len(batch_names)} new table(s) into {len(built)} total")

    # 3. relationships (catalog FKs, else probe by name+type+overlap). Use the FULL built set's
    # (schema, table) pairs so an --append batch builds relationships across the whole union, not
    # just its own batch (covers every schema present).
    _progress(pp, f"building relationships ({len(built)} tables; FK + value-overlap probes)…")
    rel_pairs = [(t.schema_name, t.name) for t in built]
    rels = _build_relationships(dialect, runner, rel_pairs, built, grain_by_table, report, pp=pp)
    if prev_rels:   # keep prior edges; add only genuinely-new ones (dedup by endpoint+column)
        rels = list(prev_rels) + [
            r for r in rels
            if (r.from_table.lower(), r.from_column.lower(), r.to_table.lower()) not in skip_keys]
    report.table_count = len(built)
    report.relationship_count = len(rels)
    _progress(pp, f"done: {len(built)} tables, {len(rels)} relationships")

    # 3b. now that joins are known, re-derive deep-table column_groups so FK (reference) columns
    # group under `references` instead of scattering through `misc` — references live on the
    # Relationship, not on the column, so the initial per-table grouping couldn't see them.
    _regroup_columns_with_references(built, rels)

    # 4. propose subject areas + cross-area edges
    areas, notes = build.propose_subject_areas(built, rels, conn_name, profile)
    report.notes.extend(notes)
    report.subject_areas = [a.name for a in areas]
    cross = build.extract_cross_area_relationships(areas, rels)

    storage = StorageConnection(
        name=conn_name,
        storage_type=dialect.name,
        storage_config={"profile": profile, "credentials_ref": "<artifacts_dir>/local/credentials"},
    )
    org = Organization(
        datasource=profile,
        version=1,
        storage_connections=[storage],
        subject_areas=areas,
        cross_subject_area_relationships=cross,
    )

    # 5. write (backing up any legacy model at the profile root)
    if out == artifacts_dir / profile and not dry_run:
        _backup_legacy_model(out)
    wr = build.write_tree(org, out, dry_run=dry_run)
    report.files_written = wr.files_written
    return org, report


def discover_inventory(
    profile: str,
    db_type: str,
    *,
    runner: Runner,
    tables: Optional[list[str]] = None,
    schemas: Optional[list[str]] = None,
    bigquery_region: str = "region-us",
) -> dict:
    """Cheap first-pass discovery for the prune UI: every (schema, table) and its
    columns — and NOTHING expensive. No grain count-distincts, no FK-overlap probes,
    no row-count scans, no date sniffing. The user prunes the returned table list and
    the kept set then flows to `introspect(..., tables=<kept>)` for the full build.

    `schemas` (optional) restricts discovery to those schemas (the user's 1.3 schema
    pick) so a 50-schema warehouse isn't fully enumerated just to prune.

    Returns a JSON-serializable inventory:
        {profile, db_type, schemas:[...], table_count, column_mode,
         tables:[{schema, table, columns:[{name, type}]}, ...]}
    """
    dialect = D.get_dialect(db_type)
    if isinstance(dialect, D.BigQuery):
        dialect = D.BigQuery(region=bigquery_region)

    report = IntrospectReport(profile=profile, db_type=db_type, out_dir="", dry_run=True)
    pairs = _discover_tables(dialect, runner, tables, report)
    if schemas:
        keep = {s for s in schemas}
        pairs = [(s, t) for s, t in pairs if s in keep]
    if not pairs:
        raise RuntimeError(
            "no tables discovered. If the catalog is locked down, pass an explicit "
            "table allowlist (schema.table) so the prune view can describe them."
        )

    # One bulk catalog read for all columns where the dialect supports it (so a
    # 500-table DB is a single round-trip), else per-table.
    schemas = sorted({s for s, _ in pairs if s})
    cols_by_tt: dict[tuple[Optional[str], str], list[dict]] = {}
    used_bulk = False
    bulk_sql = dialect.sql_columns_bulk(schemas) if schemas else None
    if bulk_sql:
        rows = _try(runner, bulk_sql)
        if rows:
            used_bulk = True
            for r in rows:
                key = (r.get("table_schema"), r.get("table_name"))
                scale = r.get("numeric_scale")
                scale_i = int(scale) if scale not in (None, "", "NULL") else None
                ctype = dialect.map_type(r.get("data_type", ""), numeric_scale=scale_i)
                cols_by_tt.setdefault(key, []).append({"name": r["column_name"], "type": ctype})

    inv_tables: list[dict] = []
    column_mode = "catalog-bulk" if used_bulk else "per-table"
    for schema, table in pairs:
        cols = cols_by_tt.get((schema, table))
        if cols is None:
            # bulk missed this table (or no bulk support) → per-table catalog/probe read
            built, cmode = _columns(dialect, runner, schema, table)
            cols = [{"name": c.name, "type": c.type} for c in built]
            if not used_bulk:
                column_mode = cmode
        inv_tables.append({"schema": schema, "table": table, "columns": cols})

    return {
        "profile": profile,
        "db_type": db_type,
        "schemas": report.schemas or schemas,
        "table_count": len(inv_tables),
        "column_mode": column_mode,
        "tables": inv_tables,
    }


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _try(runner: Runner, sql: str) -> Optional[list[dict]]:
    try:
        return runner(sql)
    except Exception:
        return None


# Supabase (hosted Postgres) ships system schemas the user almost never wants modeled — and
# some hold secrets (vault, pgsodium). We detect Supabase by its signature schemas and drop
# the whole system set, so onboarding models only the user's app tables (e.g. public).
_SUPABASE_SYS_SCHEMAS = frozenset({
    "auth", "storage", "vault", "realtime", "_realtime", "extensions",
    "graphql", "graphql_public", "pgsodium", "pgsodium_masks", "pgbouncer",
    "supabase_functions", "supabase_migrations", "net", "cron", "pgtle",
    "_analytics", "_supavisor", "supabase_admin",
})
# Require several signature schemas together — a plain-Postgres DB with a lone `auth` or
# `storage` schema of its own is NOT Supabase and must not be filtered.
_SUPABASE_SIGNATURE = ("auth", "storage", "extensions", "realtime", "vault")


def _filter_supabase_system_schemas(schemas: list[str], report: IntrospectReport) -> list[str]:
    low = {s.lower() for s in schemas}
    if sum(sig in low for sig in _SUPABASE_SIGNATURE) < 3:
        return schemas  # not Supabase — leave every schema as-is
    kept = [s for s in schemas if s.lower() not in _SUPABASE_SYS_SCHEMAS]
    skipped = [s for s in schemas if s.lower() in _SUPABASE_SYS_SCHEMAS]
    if skipped:
        report.notes.append(
            "Supabase detected — skipped its system schemas (kept your app schemas): "
            + ", ".join(sorted(skipped)) + ". Pass an explicit --tables allowlist to include any."
        )
    return kept


def _discover_tables(
    dialect: D.Dialect, runner: Runner, allowlist: Optional[list[str]], report: IntrospectReport
) -> list[tuple[Optional[str], str]]:
    if allowlist:
        report.mode_per_capability["tables"] = "allowlist"
        out = []
        for item in allowlist:
            if "." in item:
                s, t = item.split(".", 1)
                out.append((s, t))
            else:
                out.append((None, item))
        return out

    # catalog mode: schemas -> tables
    schemas = _try(runner, dialect.sql_schemas())
    if schemas is not None:
        report.mode_per_capability["tables"] = "catalog"
        report.schemas = _filter_supabase_system_schemas(
            [r["schema_name"] for r in schemas], report)
        pairs: list[tuple[Optional[str], str]] = []
        for s in report.schemas:
            trows = _try(runner, dialect.sql_tables(s)) or []
            for r in trows:
                pairs.append((r.get("schema_name", s), r["table_name"]))
        return pairs

    report.mode_per_capability["tables"] = "unavailable"
    report.notes.append(
        "catalog table enumeration denied and no allowlist provided — cannot "
        "discover tables; re-run with an explicit table list."
    )
    return []


def _build_table(
    dialect: D.Dialect, runner: Runner, schema: Optional[str], table: str,
    conn_name: str, report: IntrospectReport,
) -> Table:
    # columns: catalog first, probe fallback
    cols, mode = _columns(dialect, runner, schema, table)
    report.mode_per_capability.setdefault("columns", mode)

    # Estimate row count ONCE up front (a catalog stat — instant, zero scan). Reused for
    # the grain-probe size guard below AND for performance_hints, so we never fetch twice.
    est_rows = _estimate_rows(dialect, runner, schema, table)

    # grain: catalog PKs, else uniqueness probe (skipped on very large tables — see _grain)
    grain, gmode = _grain(dialect, runner, schema, table, cols, est_rows=est_rows)
    report.mode_per_capability.setdefault("grain", gmode)
    if gmode == "probe_skipped_large":
        report.notes.append(
            f"{table}: skipped grain probe (~{est_rows:,} rows > "
            f"{_GRAIN_PROBE_MAX_ROWS:,}) — left grain empty rather than full-scanning "
            f"each id column. A composite/absent grain is expected for a fact table this "
            f"size; set it by hand if you know the key."
        )
    pk_set = set(grain)
    for c in cols:
        c.primary_key = c.name in pk_set
        # column-intrinsic aggregation class (additive / averageable / dimension / unknown).
        # Keys are dimensions; the rest is a name+type heuristic the curator can refine.
        c.aggregation = build.classify_aggregation(c.name, c.type, is_key=c.primary_key)
        if build.detect_sensitive(table, c.name):
            c.sensitive = True
            report.sensitive_columns += 1

    # One live sample → date encoding/timezone AND choice_field skeletons for low-cardinality
    # coded columns (so catalog-mode coded columns get an enum skeleton the LLM labels, not
    # just the probe-mode path).
    _enrich_from_sample(dialect, runner, schema, table, cols)

    column_groups = build.maybe_column_groups(cols)
    column_group_descriptions = build.column_group_descriptions(column_groups) if column_groups else {}
    if column_groups:
        report.deep_tables.append(table)

    perf = None
    if est_rows is not None:
        perf = PerformanceHints(
            estimated_row_count=est_rows,
            estimated_row_count_at=_NOW,  # so the receipt can show "estimated as of <date>"
        )

    # On a large table, seed `recommended_filters` with the date/time columns — the natural
    # way to narrow a scan. The query path's scan-risk warning then fires only when a query
    # lacks a filter on one of these (and can name the column). Real index / partition /
    # clustering keys are layered in per-dialect separately.
    if perf and (perf.estimated_row_count or 0) >= _LARGE_TABLE_ROWS:
        date_cols = [c.name for c in cols if c.type in ("date", "timestamp", "time")]
        if 1 <= len(date_cols) <= _MAX_DATE_FILTER_COLS:
            perf.recommended_filters = date_cols

    return Table(
        name=table,
        schema=schema,
        storage_connection=conn_name,
        grain=grain,
        description="",  # LLM enrichment fills this in
        column_groups=column_groups,
        column_group_descriptions=column_group_descriptions,
        performance_hints=perf,
        columns=cols,
    )


def _columns(
    dialect: D.Dialect, runner: Runner, schema: Optional[str], table: str
) -> tuple[list[Column], str]:
    rows = _try(runner, dialect.sql_columns(schema, table))
    if rows:
        cols: list[Column] = []
        for r in rows:
            scale = r.get("numeric_scale")
            scale_i = int(scale) if scale not in (None, "", "NULL") else None
            ctype = dialect.map_type(r.get("data_type", ""), numeric_scale=scale_i)
            cols.append(Column(name=r["column_name"], type=ctype, description=""))
        return cols, "catalog"
    # probe: header for names, sample for types
    return _probe_columns(dialect, runner, schema, table), "probe"


def _probe_columns(
    dialect: D.Dialect, runner: Runner, schema: Optional[str], table: str
) -> list[Column]:
    header = _try(runner, dialect.header_sql(schema, table)) or []
    names: list[str]
    if header and isinstance(header, list):
        # DictReader on a 0-row result yields no rows but we need the fieldnames;
        # the runner returns [] for 0 rows, so fall back to a sample for names too.
        names = list(header[0].keys()) if header else []
    else:
        names = []
    sample = _try(runner, dialect.sample_sql(schema, table, SAMPLE_ROWS)) or []
    if not names and sample:
        names = list(sample[0].keys())
    cols: list[Column] = []
    for n in names:
        values = [row.get(n) for row in sample if row.get(n) not in (None, "")]
        cols.append(Column(name=n, type=_infer_value_type(values), description="",
                            choice_field=_maybe_choice(values)))
    return cols


def _estimate_rows(
    dialect: D.Dialect, runner: Runner, schema: Optional[str], table: str
) -> Optional[int]:
    """Row count from the catalog statistics (pg_class.reltuples, etc.) — instant, no scan.
    None when the dialect has no estimate query or the stat is missing/unparseable."""
    q = dialect.sql_row_estimate(schema, table)
    if not q:
        return None
    est = _try(runner, q)
    if not est or est[0].get("estimated_rows") in (None, ""):
        return None
    try:
        return int(float(est[0]["estimated_rows"]))
    except (ValueError, TypeError):
        return None


def _grain(
    dialect: D.Dialect, runner: Runner, schema: Optional[str], table: str, cols: list[Column],
    *, est_rows: Optional[int] = None,
) -> tuple[list[str], str]:
    pks = _try(runner, dialect.sql_primary_keys(schema, table))
    if pks:
        return [r["column_name"] for r in pks], "catalog"
    # Size guard: the probe below COUNT(DISTINCT)-scans the table once per candidate. On a
    # huge fact table with no catalog PK that's tens of minutes of scans yielding nothing —
    # so skip it above the threshold and report an empty grain (the correct answer here; the
    # real grain is composite, beyond a single-column probe). See _GRAIN_PROBE_MAX_ROWS.
    if est_rows is not None and est_rows >= _GRAIN_PROBE_MAX_ROWS:
        return [], "probe_skipped_large"
    # probe: a single id-ish column that is unique + non-null
    candidates = [c.name for c in cols if c.name.lower() == "id" or c.name.lower().endswith("_id")]
    candidates = candidates or [c.name for c in cols[:3]]
    for cand in candidates:
        res = _try(runner, dialect.count_distinct_sql(schema, table, cand))
        if not res:
            continue
        row = res[0]
        try:
            total = int(float(row.get("total", 0)))
            distinct = int(float(row.get("distinct_count", 0)))
            nulls = int(float(row.get("null_count", 0)))
        except (ValueError, TypeError):
            continue
        if total > 0 and distinct == total and nulls == 0:
            return [cand], "probe"
    return [], "probe"


def _build_relationships(
    dialect: D.Dialect, runner: Runner, pairs: list[tuple[Optional[str], str]],
    tables: list[Table], grain_by_table: dict[str, set[str]], report: IntrospectReport,
    pp: Optional[Path] = None,
) -> list[Relationship]:
    # catalog FKs per schema
    schemas = {s for s, _ in pairs if s}
    catalog_fks: list[dict] = []
    catalog_ok = False
    if isinstance(dialect, D.SQLite):
        for _, t in pairs:
            rows = _try(runner, dialect.sql_foreign_keys_for_table(t))
            if rows is not None:
                catalog_ok = True
                catalog_fks.extend(rows)
    else:
        for s in (schemas or {None}):
            rows = _try(runner, dialect.sql_foreign_keys(s)) if s else None
            if rows is not None:
                catalog_ok = True
                catalog_fks.extend(rows)

    tables_by_name: dict[str, list[Table]] = {}
    for t in tables:
        tables_by_name.setdefault(t.name, []).append(t)

    rels: list[Relationship] = []
    overlap_probes = 0   # budget guard for declared-FK overlap confirmation (see FK_OVERLAP_PROBE_CAP)
    if catalog_ok and catalog_fks:
        report.mode_per_capability["relationships"] = "catalog"
        _fk_step = max(1, len(catalog_fks) // 10)   # ~10 heartbeat lines over the (slow) FK pass
        for _fi, fk in enumerate(catalog_fks, 1):
            if _fi % _fk_step == 0 or _fi == len(catalog_fks):
                _progress(pp, f"relationships: declared FK {_fi}/{len(catalog_fks)}")
            ft, fc, tt, tc = fk.get("from_table"), fk.get("from_column"), fk.get("to_table"), fk.get("to_column")
            if not (ft and fc and tt and tc):
                continue
            # Schema each endpoint lives in: the dialect's FK query supplies it on schema-ful
            # DBs (Postgres/MySQL); otherwise resolve it from the table list (same-schema first).
            from_schema = fk.get("from_schema") or _schema_of(ft, tables_by_name)
            to_schema = fk.get("to_schema") or _schema_of(tt, tables_by_name, prefer=from_schema, report=report)
            cross = bool(from_schema and to_schema and from_schema != to_schema)
            card = build.infer_cardinality(ft, tt, [fc], [tc], grain_by_table)
            # declared-but-unenforced FKs (Redshift/Databricks/Trino) -> confirm by overlap.
            # The catalog's FK is a *declaration*, not a guarantee, so probe the data: if the
            # child values actually live in the parent key, treat it like an enforced FK
            # (confirmed + auto-approved) instead of dumping it in the review queue. Only FKs we
            # can't confirm against the data stay inferred/unreviewed for a human glance. This is
            # what keeps inheritance-heavy schemas (ServiceNow on Redshift) from flooding review.
            confidence = "confirmed" if dialect.fk_enforced else "inferred"
            overlap_confirmed = False
            if not dialect.fk_enforced and overlap_probes < FK_OVERLAP_PROBE_CAP:
                fobj = _pick_target(tables_by_name.get(ft, []), prefer=from_schema)
                tobj = _pick_target(tables_by_name.get(tt, []), prefer=to_schema)
                # Skip the scan on huge FROM tables (the slow case on Redshift fact tables) —
                # leave the declared FK inferred/unreviewed rather than full-scan to confirm it.
                if fobj is not None and tobj is not None and not _too_big_to_probe(fobj):
                    overlap_probes += 1
                    if _overlaps(dialect, runner, fobj, fc, tobj, tc):
                        confidence = "confirmed"
                        overlap_confirmed = True
            trusted = dialect.fk_enforced or overlap_confirmed
            # trusted joins are sound structure -> auto-approve with a system sign-off (don't
            # flood the review queue); unconfirmed ones need a glance.
            # EXCEPTION: a join that spans two schemas is an architectural claim — the model now
            # reaches across namespaces — so surface it for review even when the FK is trusted.
            desc = ""
            if trusted and not cross:
                kw: dict = {"review_state": "approved", "signed_off_by": "agami_introspect",
                            "signed_off_role": "system", "signed_off_at": _NOW}
                if overlap_confirmed:
                    desc = "declared FK, confirmed by value-overlap probe"
            else:
                kw = {"review_state": "unreviewed"}
            if cross:
                desc = f"crosses schemas: {from_schema} → {to_schema} (declared FK) — confirm it belongs in this model"
                report.notes.append(f"cross-schema relationship: {from_schema}.{ft} → {to_schema}.{tt} (declared FK)")
            rels.append(Relationship(
                from_table=ft, from_column=fc, to_table=tt, to_column=tc,
                from_schema=from_schema, to_schema=to_schema,
                relationship=card, join_type="LEFT", confidence=confidence, description=desc, **kw,
            ))
        if overlap_probes >= FK_OVERLAP_PROBE_CAP:
            report.notes.append(
                f"declared-FK overlap confirmation capped at {FK_OVERLAP_PROBE_CAP} probes — "
                "remaining declared FKs left unreviewed for sign-off on /agami-model")
    else:
        # probe: infer FKs from name+type match, confirm by value-overlap
        report.mode_per_capability["relationships"] = "probe"
        rels.extend(_probe_relationships(dialect, runner, tables, grain_by_table, report))

    # Tier 2 — name-based reference promotion. A `<x>_id` column whose target table is
    # identifiable by name becomes an inferred/unreviewed join even WITHOUT value overlap
    # (which under-detects on sparse data). Conservative + type-checked, and `unreviewed` so
    # it's discoverable but never asserted as fact — the user signs it off (or it self-approves
    # through use). This is what lifts a sparse demo from ~9 joins toward the real graph.
    rels.extend(_promote_reference_fields(tables, grain_by_table, rels, report))
    return rels


def _regroup_columns_with_references(tables: list[Table], rels: list[Relationship]) -> None:
    """Re-derive column_groups (+ descriptions) for deep tables now that relationships exist, so
    each table's FK (FROM-side) columns land in the `references` group. Mutates `tables` in place;
    a no-op for narrow tables (no groups) and tables with no outgoing simple-FK joins. Schema-aware
    so a same-named table in another schema doesn't borrow the wrong references."""
    ref_by_table: dict[tuple, set[str]] = {}
    for r in rels:
        if not r.from_column:   # skip on:-expression joins (no single FROM column)
            continue
        ref_by_table.setdefault((r.from_schema, bare_name(r.from_table)), set()).add(r.from_column)
    for t in tables:
        if not t.column_groups:   # only deep tables carry groups
            continue
        ref_cols = ref_by_table.get((t.schema_name, t.name)) or ref_by_table.get((None, t.name)) or set()
        if not ref_cols:
            continue
        t.column_groups = build.maybe_column_groups(t.columns, reference_columns=ref_cols)
        t.column_group_descriptions = build.column_group_descriptions(t.column_groups)


_REF_ID_RE = re.compile(r"^(.+?)_id$", re.IGNORECASE)

# Columns that are NEVER an enum to decode, regardless of cardinality: join keys / reference
# actors (`*_id`, `*_by` — usually GUID-valued FKs) and counts / quantities (`*_count`, `*_qty`,
# … — measures, not codes). General naming conventions, not vendor-specific. A low-cardinality
# `resolved_by` (few users) or `reopen_count` (0/1/2) would otherwise be mis-seeded as a choice.
_NOT_CHOICE_NAME_RE = re.compile(
    r"^(id|guid|uuid)$|_(id|guid|uuid|by|count|cnt|qty|quantity|seq|num|no)$", re.IGNORECASE)
# A value that looks like an identifier (32-hex ServiceNow sys_id, or a dashed UUID) — its column
# is a join key, never an enum. One such distinct value is enough to disqualify the whole column.
_GUID_VALUE_RE = re.compile(
    r"^[0-9a-f]{32}$|^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def _infer_reference_target(stem: str, by_name: dict, from_table: Table) -> Optional[Table]:
    """Find the table a `<stem>_id` column points at — by exact name, plural, or singular.
    Skips self-reference and prefers the from-table's own schema."""
    cands = (by_name.get(stem) or by_name.get(stem + "s") or by_name.get(stem + "es")
             or (by_name.get(stem[:-1]) if stem.endswith("s") else None) or [])
    cands = [t for t in cands if t.name != from_table.name]
    if not cands:
        return None
    same = [t for t in cands if t.schema_name == from_table.schema_name]
    return (same or cands)[0]


def _promote_reference_fields(
    tables: list[Table], grain_by_table: dict[str, set[str]],
    existing: list[Relationship], report: IntrospectReport,
) -> list[Relationship]:
    """Promote `<x>_id` reference columns to inferred/unreviewed joins when the target table is
    identifiable by name and key-type-compatible — never requiring value overlap. Conservative:
    target must exist, have a single-column grain, and a key whose type matches the ref column."""
    by_name: dict[str, list[Table]] = {}
    for t in tables:
        by_name.setdefault(t.name.lower(), []).append(t)
    # schema-aware key so a same-named table in another schema doesn't suppress promotion here
    seen = {(r.from_schema, r.from_table, r.from_column) for r in existing}
    out: list[Relationship] = []
    for t in tables:
        grain = set(t.grain)   # THIS table's key (grain_by_table is keyed by bare name → ambiguous)
        for c in t.columns:
            mo = _REF_ID_RE.match(c.name)
            if not mo or c.primary_key or c.name in grain or (t.schema_name, t.name, c.name) in seen:
                continue
            target = _infer_reference_target(mo.group(1).lower(), by_name, t)
            if target is None or len(target.grain) != 1:
                continue
            to_col = target.grain[0]
            to_pk = next((tc for tc in target.columns if tc.name == to_col), None)
            if to_pk is not None and to_pk.type != c.type:   # key-type mismatch → not a real ref
                continue
            card = build.infer_cardinality(t.name, target.name, [c.name], [to_col], grain_by_table)
            out.append(Relationship(
                from_table=t.name, from_column=c.name, to_table=target.name, to_column=to_col,
                from_schema=t.schema_name, to_schema=target.schema_name,
                relationship=card, join_type="LEFT", confidence="inferred", review_state="unreviewed",
                description=f"inferred reference: {c.name} → {target.name}.{to_col} "
                            "(name match; not overlap-verified — confirm)",
            ))
            seen.add((t.schema_name, t.name, c.name))
    if out:
        report.notes.append(
            f"promoted {len(out)} reference field(s) (<x>_id) to inferred joins — review on /agami-model")
    return out


def _schema_of(
    name: str, tables_by_name: dict[str, list[Table]],
    prefer: Optional[str] = None, report: Optional[IntrospectReport] = None,
) -> Optional[str]:
    """The schema a bare table name lives in. When the name exists in several schemas,
    prefer `prefer` (the other endpoint's schema) so a same-schema FK resolves correctly;
    otherwise pick deterministically and note the ambiguity for the user to disambiguate."""
    schemas = sorted({t.schema_name for t in tables_by_name.get(name, []) if t.schema_name})
    if len(schemas) == 1:
        return schemas[0]
    if len(schemas) > 1:
        if prefer and prefer in schemas:
            return prefer
        if report is not None:
            report.notes.append(
                f"ambiguous table name {name!r} across schemas {schemas} — picked {schemas[0]}; "
                "edit the relationship if it should point elsewhere")
        return schemas[0]
    return None


def _pick_target(cands: list[Table], prefer: Optional[str]) -> Optional[Table]:
    """Choose the actual target Table for a probed join, preferring the from-table's own
    schema so a `users` in two schemas doesn't bind to the wrong one (the old bare-name dict
    silently kept whichever was inserted last)."""
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    for t in cands:
        if t.schema_name == prefer:
            return t
    return sorted(cands, key=lambda t: (t.schema_name or ""))[0]


def _probe_relationships(
    dialect: D.Dialect, runner: Runner, tables: list[Table],
    grain_by_table: dict[str, set[str]], report: IntrospectReport,
) -> list[Relationship]:
    tables_by_name: dict[str, list[Table]] = {}
    for t in tables:
        tables_by_name.setdefault(t.name, []).append(t)
    # candidate target keys: single-column grains. (grain_by_table is bare-name-keyed, so its
    # key column is best-effort under a cross-schema name clash — the actual target Table is
    # resolved schema-aware below, which is what the overlap probe + schema stamp depend on.)
    targets = {name: next(iter(g)) for name, g in grain_by_table.items() if len(g) == 1}
    rels: list[Relationship] = []
    for t in tables:
        for c in t.columns:
            low = c.name.lower()
            if not low.endswith("_id") and low != "id":
                continue
            stem = low[:-3] if low.endswith("_id") else None
            # match a target table whose name resembles the stem
            for tgt_name, tgt_col in targets.items():
                if tgt_name == t.name:
                    continue
                tn = tgt_name.lower()
                if stem and (stem in tn or tn in stem or tn.rstrip("s") == stem.rstrip("s")):
                    tgt = _pick_target(tables_by_name.get(tgt_name, []), prefer=t.schema_name)
                    if tgt is None:
                        continue
                    if _overlaps(dialect, runner, t, c.name, tgt, tgt_col):
                        cross = bool(t.schema_name and tgt.schema_name and t.schema_name != tgt.schema_name)
                        desc = "inferred by name+type match + value-overlap probe"
                        if cross:
                            desc += f"; crosses schemas {t.schema_name} → {tgt.schema_name}"
                            report.notes.append(
                                f"cross-schema relationship: {t.schema_name}.{t.name} → "
                                f"{tgt.schema_name}.{tgt_name} (inferred)")
                        rels.append(Relationship(
                            from_table=t.name, from_column=c.name,
                            to_table=tgt_name, to_column=tgt_col,
                            from_schema=t.schema_name, to_schema=tgt.schema_name,
                            relationship=build.infer_cardinality(
                                t.name, tgt_name, [c.name], [tgt_col], grain_by_table),
                            join_type="LEFT", confidence="proposed", review_state="unreviewed",
                            description=desc,
                        ))
                        break
    return rels


def _too_big_to_probe(t: Table) -> bool:
    """True when a table's estimated row count is above the overlap-probe guard — scanning it to
    confirm a join would crawl, so we skip the scan and leave the join unverified."""
    perf = getattr(t, "performance_hints", None)
    rows = getattr(perf, "estimated_row_count", None) if perf else None
    return rows is not None and rows > _OVERLAP_PROBE_MAX_ROWS


def _overlaps(dialect: D.Dialect, runner: Runner, ft: Table, fc: str, tt: Table, tc: str) -> bool:
    """Sample from-column values and check they exist in the target key."""
    fq_from = dialect.qualified(ft.schema_name, ft.name)
    fq_to = dialect.qualified(tt.schema_name, tt.name)
    col_f = dialect.quote_ident(fc)
    col_t = dialect.quote_ident(tc)
    sql = (
        f"SELECT COUNT(*) AS matched FROM (SELECT DISTINCT {col_f} AS v FROM {fq_from} "
        f"WHERE {col_f} IS NOT NULL {('LIMIT 50' if dialect.limit_style=='limit' else '')}) src "
        f"WHERE EXISTS (SELECT 1 FROM {fq_to} t WHERE t.{col_t} = src.v)"
    )
    res = _try(runner, sql)
    if not res:
        return False
    try:
        return int(float(res[0].get("matched", 0))) > 0
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Value-based inference (probe mode)
# ---------------------------------------------------------------------------

_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}")
_BOOL_VALUES = {"true", "false", "t", "f", "0", "1", "yes", "no"}


# Column names that suggest a date/time value (gates epoch/yyyymmdd detection so a
# random 10-digit id isn't mistaken for a Unix timestamp).
_DATE_NAME_RE = re.compile(
    r"(^|_)(date|time|datetime|timestamp|ts|epoch|created|updated|modified|deleted|"
    r"expir\w*|scheduled|started|ended|completed|occurred|dob|birth\w*)($|_)"
    r"|_at$|_on$|_dt$|_ts$|_time$|_date$", re.I)
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?")
_OFFSET_RE = re.compile(r"(Z|[+-]\d{2}:?\d{2})$")
_INT_ONLY_RE = re.compile(r"-?\d+$")
# (date_format, low, high) epoch ranges by scale — ~2001..2286 for seconds, ×1000 each step
_EPOCH_RANGES = (
    ("epoch_s", 1_000_000_000, 9_999_999_999),
    ("epoch_ms", 1_000_000_000_000, 9_999_999_999_999),
    ("epoch_us", 1_000_000_000_000_000, 9_999_999_999_999_999),
    ("epoch_ns", 1_000_000_000_000_000_000, 9_999_999_999_999_999_999),
)


def _looks_time_named(name: str) -> bool:
    return bool(_DATE_NAME_RE.search(name or ""))


def _sniff_date(name: str, ctype: str, values: list) -> tuple[Optional[str], Optional[str]]:
    """Sniff a column's date storage encoding + timezone from sample values, returning
    (date_format, timezone). Conservative — an encoded date (epoch/yyyymmdd/iso) is only
    claimed for a time-named column whose sample values all fit the shape:
      - native date/timestamp/time → date_format None (DB returns it readable); tz only
        if the sample carries an offset;
      - integer epoch in a plausible range (time-named) → epoch_s/ms/us/ns, tz UTC;
      - integer 20240115 (time-named) → yyyymmdd;
      - ISO-8601 strings (time-named) → iso8601, tz offset-aware if an offset is present.
    """
    vals = [str(v).strip() for v in values if v not in (None, "")][:50]
    if not vals:
        return (None, None)
    if ctype in ("date", "timestamp", "time"):
        if ctype == "timestamp" and any(_OFFSET_RE.search(v) for v in vals):
            return (None, "offset-aware")
        return (None, None)
    if not _looks_time_named(name):
        return (None, None)
    if all(_INT_ONLY_RE.match(v) for v in vals):
        ints = [int(v) for v in vals]
        if all(19000101 <= i <= 29991231 and 1 <= (i // 100) % 100 <= 12 and 1 <= i % 100 <= 31
               for i in ints):
            return ("yyyymmdd", None)
        for fmt, lo, hi in _EPOCH_RANGES:
            if all(lo <= i <= hi for i in ints):
                return (fmt, "UTC")
        return (None, None)
    if all(_ISO_RE.match(v) for v in vals):
        tz = "offset-aware" if any(_OFFSET_RE.search(v) for v in vals) else None
        return ("iso8601", tz)
    return (None, None)


def _enrich_from_sample(
    dialect: D.Dialect, runner: Runner, schema: Optional[str], table: str, cols: list[Column]
) -> None:
    """ONE live sample → (a) date_format/timezone on date-candidate columns, and
    (b) a `choice_field` skeleton `{value: ""}` on low-cardinality CODED columns (the LLM
    enrichment fills the labels). (b) is the catalog-mode counterpart of probe mode's
    `_maybe_choice` — without it, a catalog DB's coded columns (e.g. a ServiceNow integer
    `severity`) never get an enum skeleton, so the decode can only ever live in prose."""
    date_cands = [c for c in cols if c.type in ("date", "timestamp", "time")
                  or (c.type in ("integer", "decimal", "string") and _looks_time_named(c.name))]
    # choice candidates: codeable columns with no choice_field yet, that aren't keys, dates,
    # or free-text-ish. A short integer/string column with few distinct values is an enum.
    # Exclude reference-named `*_id`/`sys_id` columns by NAME: introspection never populates
    # Column.foreign_key, so that guard alone wouldn't catch a low-cardinality `dealer_id`
    # (few dealers) — which is a JOIN target, not an enum to decode.
    choice_cands = [c for c in cols
                    if c.choice_field is None and not c.primary_key and c.foreign_key is None
                    and c.type in ("integer", "string") and not _looks_time_named(c.name)
                    and not _NOT_CHOICE_NAME_RE.search(c.name)]
    if not date_cands and not choice_cands:
        return
    sample = _try(runner, dialect.sample_sql(schema, table, SAMPLE_ROWS)) or []
    if not sample:
        return
    for c in date_cands:
        df, tz = _sniff_date(c.name, c.type, [row.get(c.name) for row in sample])
        if df:
            c.date_format = df
        if tz:
            c.timezone = tz
    for c in choice_cands:
        ch = _maybe_choice([row.get(c.name) for row in sample])
        # only genuinely code-like: skip long free-text values (names, descriptions, ids).
        if ch and all(len(str(k)) <= 40 for k in ch):
            c.choice_field = ch
    # Deterministic descriptions for universal coded formats (currency/country/timezone/locale) —
    # fills a column the LLM would otherwise leave `ai_unknown`, on any database. Only when blank.
    for c in cols:
        if (c.description or "").strip() or c.primary_key:
            continue
        cd = build.canonical_description(c.name, [row.get(c.name) for row in sample])
        if cd:
            c.description = cd
            c.description_source = "metadata"  # authoritative deterministic fact (not an LLM guess)


def _infer_value_type(values: list) -> str:
    vals = [str(v).strip() for v in values if str(v).strip() != ""][:200]
    if not vals:
        return "string"
    if all(v.lower() in _BOOL_VALUES for v in vals) and len({v.lower() for v in vals}) <= 2:
        return "boolean"
    if all(_INT_RE.match(v) for v in vals):
        return "integer"
    if all(_FLOAT_RE.match(v) for v in vals):
        return "decimal"
    if all(_TS_RE.match(v) for v in vals):
        return "timestamp"
    if all(_DATE_RE.match(v) for v in vals):
        return "date"
    return "string"


def _maybe_choice(values: list) -> Optional[dict[str, str]]:
    vals = [str(v).strip() for v in values if str(v).strip() != ""]
    distinct = sorted(set(vals))
    if not (0 < len(distinct) <= ENUM_MAX_DISTINCT and len(vals) >= max(10, 2 * len(distinct))):
        return None
    # GUID/UUID-valued columns are join keys, not enums — a real enum (severity 1/2/3, status
    # open/closed) never holds identifier values. One such distinct value disqualifies the column.
    if any(_GUID_VALUE_RE.match(d) for d in distinct):
        return None
    # low-cardinality, non-identifier values -> likely an enum
    return {d: "" for d in distinct}  # labels filled in by LLM enrichment


def _backup_legacy_model(profile_root: Path) -> None:
    """Move any legacy (v1) model artifacts at the profile root into .legacy_backup/ before
    writing the new tree, so re-onboarding never silently clobbers the old model."""
    legacy = ["index.yaml"]
    backup = profile_root / ".legacy_backup"
    moved = False
    for name in legacy:
        src = profile_root / name
        if src.exists():
            backup.mkdir(parents=True, exist_ok=True)
            src.rename(backup / name)
            moved = True
    # per-schema legacy dirs contain a _schema.yaml; move those too
    if profile_root.exists():
        for child in list(profile_root.iterdir()):
            if child.is_dir() and (child / "_schema.yaml").exists():
                backup.mkdir(parents=True, exist_ok=True)
                child.rename(backup / child.name)
                moved = True
    return None


__all__ = ["introspect", "make_execute_sql_runner", "IntrospectReport", "Runner"]
