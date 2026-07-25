"""Loader for the agami semantic-model-v2 on-disk layout.

On-disk tree (design doc's "Storage layout on disk"), rooted at the profile dir
``<artifacts_dir>/<profile>/``:

    <root>/
      datasource.yaml                                 # org desc + storage_connections + subject_areas refs
      datasources/<connection>/storage.yaml    # physical: storage_type, storage_config
      subject_areas/<name>/
        subject_area.yaml                      # desc, default_time_window, tables (TableRefs)
        tables/<t>.yaml                        # canonical Table definitions
        entities/<e>.yaml
        metrics/<m>.yaml
        relationships.yaml                     # intra-area FK graph (list)
      cross_subject_area_relationships.yaml    # optional, org-level
      cross_subject_area_entities.yaml         # optional, org-level
      cross_subject_area_metrics.yaml          # optional, org-level
      prompt_examples/<subject_area>/examples.yaml

The loader parses the tree into a single `Datasource` model (so the validator
and runtime work on one in-memory object). It also provides the context-assembly
functions the runtime depends on:

  - collect_default_filters(...)   — union of in-scope tables' default_filters,
                                     with :param substitution.
  - get_table_context(...)         — the compound call (columns + default_filters
                                     + relationships + caveats + value_transforms).
  - get_table_index(...)           — mode=index column listing honoring
                                     TableRef.expose_column_groups.
  - get_subject_area_bundle(...)   — one-shot for small areas.

Nothing here imports Pydantic-free; the whole module is v2-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from .models import (
    Column,
    CrossSubjectAreaRelationship,
    Datasource,
    Entity,
    Metric,
    Relationship,
    StorageConnection,
    SubjectArea,
    Table,
    TableRef,
    bare_name,
)

# The default `include` set for get_table_context / `sm context`. performance_hints
# is in it so estimated_row_count reaches the answer receipt (the "≈N rows"
# provenance) and the scan-risk check — without it the receipt falls back to "rows
# unknown" even when the model has the count. Single source so the three call sites
# (get_table_context, get_subject_area_bundle, the `sm context --include` default)
# can't drift. The bundle adds "metrics" on top (see DEFAULT_BUNDLE_INCLUDE).
DEFAULT_CONTEXT_INCLUDE = (
    "default_filters", "relationships", "caveats", "value_transforms", "performance_hints",
)
DEFAULT_BUNDLE_INCLUDE = DEFAULT_CONTEXT_INCLUDE + ("metrics",)


# ---------------------------------------------------------------------------
# Reading the tree
# ---------------------------------------------------------------------------


def _read_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_datasource(root: str | Path, *, include_rejected: bool = False) -> Datasource:
    """Parse a v2 profile directory into a Datasource model.

    By default, entries the curator excluded (`review_state: rejected`) are dropped
    so the runtime never sees them. Pass `include_rejected=True` for the curation
    tools (agami-model), which must show excluded entries to toggle them.
    """
    root = Path(root)
    datasource_path = root / "datasource.yaml"
    if not datasource_path.exists():
        raise FileNotFoundError(f"no datasource.yaml at {datasource_path}")
    ds_doc: dict[str, Any] = _read_yaml(datasource_path) or {}

    # storage connections — inline list OR refs into datasources/<c>/storage.yaml
    connections: list[StorageConnection] = []
    for sc in ds_doc.get("storage_connections", []) or []:
        if isinstance(sc, dict) and "ref" in sc:
            ref_path = root / sc["ref"]
            connections.append(StorageConnection(**(_read_yaml(ref_path) or {})))
        elif isinstance(sc, dict) and "storage_type" in sc:
            connections.append(StorageConnection(**sc))
        else:
            # {name, ref} where ref missing -> try datasources/<name>/storage.yaml
            name = sc.get("name") if isinstance(sc, dict) else str(sc)
            guess = root / "datasources" / str(name) / "storage.yaml"
            if guess.exists():
                connections.append(StorageConnection(**(_read_yaml(guess) or {})))

    # subject areas — each referenced by directory name
    subject_areas: list[SubjectArea] = []
    for sa_ref in ds_doc.get("subject_areas", []) or []:
        sa_dir = root / (sa_ref if isinstance(sa_ref, str) else sa_ref.get("path", ""))
        if not sa_dir.exists():
            # also accept a bare name under subject_areas/
            sa_dir = root / "subject_areas" / str(sa_ref)
        subject_areas.append(_load_subject_area(sa_dir, include_rejected=include_rejected))

    org = Datasource(
        org_id=ds_doc.get("org_id"),  # F14: minted uuid4 (None for pre-F14 files)
        datasource=ds_doc.get("datasource", root.name),
        version=ds_doc.get("version", 1),
        description=ds_doc.get("description", ""),
        fiscal_year_start_month=ds_doc.get("fiscal_year_start_month", 1),
        storage_connections=connections,
        subject_areas=subject_areas,
        cross_subject_area_relationships=_load_cross_rels(root, ds_doc),
        cross_subject_area_entities=_load_cross_entities(root, ds_doc),
        cross_subject_area_metrics=_load_cross_metrics(root, ds_doc),
        key_terminology=ds_doc.get("key_terminology", {}) or {},
    )
    return org


def load_org_id(root: str | Path) -> str | None:
    """Return the minted org_id recorded in ``<root>/datasource.yaml``, or ``None`` if the file is absent
    or predates F14 (no ``org_id`` key). This is the serve-time identity read (F14 / ACE-056).

    Deliberately read-only and lenient — unlike ``load_datasource`` it never raises on a missing
    file — so the single-tenant resolver can fall through to its ``"local"`` default. Reads only the
    top-level key; it does NOT build the full pydantic model (identity resolution runs per process,
    not per query, so it stays cheap)."""
    datasource_path = Path(root) / "datasource.yaml"
    if not datasource_path.exists():
        return None
    doc = _read_yaml(datasource_path) or {}
    oid = doc.get("org_id")
    return oid or None


def deployment_org_id(artifacts_dir: str | Path) -> str | None:
    """The deployment-wide org identity: the first ``org_id`` found across ``<artifacts_dir>/*/datasource.yaml``,
    or ``None`` if no profile carries one (F14 / ACE-056, the *deployment-scoped* rule).

    A deployment is ONE tenant even with several datasource profiles, so the minted id is shared across
    every profile's ``datasource.yaml``. Resolving by scanning the artifacts dir (rather than one 'active'
    profile) means the deploy stamp and the serve resolver agree even when ``AGAMI_PROFILE`` is unset and
    the real model lives under a named profile (e.g. ``northpeak_salesforce``, not ``default``). Read-only;
    never raises. Profiles are expected to agree; the first (sorted) is returned deterministically."""
    root = Path(artifacts_dir)
    if not root.is_dir():
        return None
    for prof in sorted(root.iterdir()):
        if prof.is_dir():
            oid = load_org_id(prof)
            if oid:
                return oid
    return None


def _rejected(obj) -> bool:
    return getattr(obj, "review_state", None) == "rejected"


def _prune_column_groups(t: Table) -> None:
    """Drop excluded/missing columns from a table's `column_groups` (and its descriptions),
    removing any group left empty. Keeps a loaded model self-consistent after per-column
    exclusions — without this, a group still listing a dropped column fails validation."""
    if not t.column_groups:
        return
    live = {c.name for c in t.columns}
    pruned = {g: [c for c in cols if c in live] for g, cols in t.column_groups.items()}
    pruned = {g: cols for g, cols in pruned.items() if cols}
    t.column_groups = pruned
    if t.column_group_descriptions:
        t.column_group_descriptions = {g: d for g, d in t.column_group_descriptions.items() if g in pruned}


def _reconcile_table_ref_exposes(refs: "list[TableRef]", tables_defined: "list[Table]") -> None:
    """Drop `expose_column_groups` names that no longer exist on the table (a group emptied by
    column exclusion). Mirrors curate._reconcile_expose_groups: clear the field entirely when it
    would expose every surviving group (or nothing). Keeps a loaded model self-consistent."""
    groups_by_table = {t.name: set(t.column_groups.keys()) for t in tables_defined}
    for r in refs:
        if not r.expose_column_groups:
            continue
        valid = groups_by_table.get(r.table)
        if valid is None:
            continue
        kept = [g for g in r.expose_column_groups if g in valid]
        if not kept or set(kept) == valid:
            r.expose_column_groups = None
        elif kept != r.expose_column_groups:
            r.expose_column_groups = kept


def _load_subject_area(sa_dir: Path, include_rejected: bool = False) -> SubjectArea:
    sa_doc: dict[str, Any] = _read_yaml(sa_dir / "subject_area.yaml") or {}

    tables_defined: list[Table] = []
    tdir = sa_dir / "tables"
    if tdir.exists():
        for tf in sorted(tdir.glob("*.yaml")):
            t = Table(**(_read_yaml(tf) or {}))
            if not include_rejected:
                if _rejected(t):
                    continue  # whole table excluded by the curator
                # drop per-column exclusions
                t.columns = [c for c in t.columns if not _rejected(c)]
                # ...and prune those dropped columns out of column_groups, else a group still
                # naming an excluded column fails the column_group_missing_column check on load
                # (excluding a deep table's column would otherwise break the whole model).
                _prune_column_groups(t)
            tables_defined.append(t)

    live_tables = {t.name for t in tables_defined}

    entities: list[Entity] = []
    edir = sa_dir / "entities"
    if edir.exists():
        for ef in sorted(edir.glob("*.yaml")):
            e = Entity(**(_read_yaml(ef) or {}))
            if not include_rejected and _rejected(e):
                continue
            entities.append(e)

    metrics: list[Metric] = []
    mdir = sa_dir / "metrics"
    if mdir.exists():
        for mf in sorted(mdir.glob("*.yaml")):
            mm = Metric(**(_read_yaml(mf) or {}))
            if not include_rejected and _rejected(mm):
                continue
            metrics.append(mm)

    relationships: list[Relationship] = []
    rel_file = sa_dir / "relationships.yaml"
    if rel_file.exists():
        rels_doc = _read_yaml(rel_file) or []
        if isinstance(rels_doc, dict):
            rels_doc = rels_doc.get("relationships", [])
        for r in rels_doc:
            rel = Relationship(**r)
            if not include_rejected:
                # drop rejected joins, and joins whose endpoint table was excluded
                if _rejected(rel):
                    continue
                if (bare_name(rel.from_table) not in live_tables
                        or bare_name(rel.to_table) not in live_tables):
                    continue
            relationships.append(rel)

    # TableRefs are kept as-is — they resolve org-wide (multi-membership), and a
    # ref to a rejected table simply won't resolve at runtime.
    table_refs = [TableRef(**t) for t in (sa_doc.get("tables", []) or [])]
    if not include_rejected:
        # a column-group that lost all its columns to exclusion was pruned above; drop any
        # TableRef.expose_column_groups still naming it, else `unknown_column_group` fails load.
        _reconcile_table_ref_exposes(table_refs, tables_defined)

    return SubjectArea(
        name=sa_doc.get("name", sa_dir.name),
        description=sa_doc.get("description", ""),
        default_time_window=sa_doc.get("default_time_window"),
        tables=table_refs,
        tables_defined=tables_defined,
        entities=entities,
        metrics=metrics,
        relationships=relationships,
    )


def _load_cross_rels(root: Path, ds_doc: dict) -> list[CrossSubjectAreaRelationship]:
    out: list[CrossSubjectAreaRelationship] = []
    # inline on datasource.yaml
    for r in ds_doc.get("cross_subject_area_relationships", []) or []:
        out.append(CrossSubjectAreaRelationship(**r))
    # or a sidecar file
    f = root / "cross_subject_area_relationships.yaml"
    if f.exists():
        doc = _read_yaml(f) or {}
        for r in doc.get("edges", doc if isinstance(doc, list) else []):
            out.append(CrossSubjectAreaRelationship(**r))
    return out


def _load_cross_entities(root: Path, ds_doc: dict) -> list[Entity]:
    out = [Entity(**e) for e in (ds_doc.get("cross_subject_area_entities", []) or [])]
    f = root / "cross_subject_area_entities.yaml"
    if f.exists():
        doc = _read_yaml(f) or {}
        for e in doc.get("entities", doc if isinstance(doc, list) else []):
            out.append(Entity(**e))
    return out


def _load_cross_metrics(root: Path, ds_doc: dict) -> list[Metric]:
    out = [Metric(**mm) for mm in (ds_doc.get("cross_subject_area_metrics", []) or [])]
    f = root / "cross_subject_area_metrics.yaml"
    if f.exists():
        doc = _read_yaml(f) or {}
        for mm in doc.get("metrics", doc if isinstance(doc, list) else []):
            out.append(Metric(**mm))
    return out


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TableIndex:
    """O(1) name→Table lookup that reproduces `_find_table` EXACTLY, so the schema hot path stops
    linear-scanning the whole model per table (ACE-047, scalability-audit finding P12). Match rule:
    a defined table's name equals the query OR its bare form; on a name clash the first table in
    scan order wins; area scoping and the TableRef multi-area fallback are preserved. Built once
    from the cached model and threaded through get_table_context; byte-identical to the scan."""

    org_wide: dict[str, tuple[Table, int]]           # t.name -> (table, global scan rank)
    per_area: dict[str, dict[str, tuple[Table, int]]]  # area -> {t.name -> (table, rank)}
    area_refs: dict[str, set[str]]                   # area -> {ref.table} for the fallback test

    @staticmethod
    def _pick(m: dict[str, tuple[Table, int]], query: str, bare: str) -> Optional[Table]:
        # Return the earlier-in-scan-order table among a name-match and a bare-match — exactly what
        # `_find_table`'s in-order `t.name == query or t.name == bare` scan would have returned.
        a = m.get(query)
        b = m.get(bare) if bare != query else None
        if a and b:
            return a[0] if a[1] <= b[1] else b[0]
        return a[0] if a else (b[0] if b else None)

    def find(self, table_name: str, area: Optional[str] = None) -> Optional[Table]:
        bare = bare_name(table_name)
        if area:
            hit = self._pick(self.per_area.get(area, {}), table_name, bare)
            if hit is not None:
                return hit
            refs = self.area_refs.get(area, set())
            if table_name in refs or bare in refs:
                return self._pick(self.org_wide, table_name, bare)
            return None
        return self._pick(self.org_wide, table_name, bare)


def build_table_index(org: Datasource) -> TableIndex:
    """Build the O(1) name→Table index once per schema call (ACE-047). Scan order (areas, then
    tables) matches `_find_table`, and first-occurrence-wins via setdefault keeps a clash resolving
    identically."""
    org_wide: dict[str, tuple[Table, int]] = {}
    per_area: dict[str, dict[str, tuple[Table, int]]] = {}
    area_refs: dict[str, set[str]] = {}
    rank = 0
    for sa in org.subject_areas:
        amap: dict[str, tuple[Table, int]] = {}
        for t in sa.tables_defined:
            org_wide.setdefault(t.name, (t, rank))
            amap.setdefault(t.name, (t, rank))
            rank += 1
        # Area names are NOT enforced unique; `_find_table` resolves an area via
        # `org.subject_area(area)`, which returns the FIRST area of that name — so the per-area maps
        # must be first-wins too (setdefault), or a duplicate name would resolve the wrong area.
        per_area.setdefault(sa.name, amap)
        area_refs.setdefault(sa.name, {ref.table for ref in sa.tables})
    return TableIndex(org_wide=org_wide, per_area=per_area, area_refs=area_refs)


def _find_table(
    org: Datasource, table_name: str, area: Optional[str] = None,
    *, index: Optional[TableIndex] = None,
) -> Optional[Table]:
    if index is not None:
        return index.find(table_name, area)
    bare = bare_name(table_name)
    areas = [org.subject_area(area)] if area else org.subject_areas
    for sa in areas:
        if sa is None:
            continue
        for t in sa.tables_defined:
            if t.name == table_name or t.name == bare:
                return t
    # Multi-area membership: a table is DEFINED in exactly one area but may be REFERENCED from
    # others via a TableRef (a shared dimension like `sys_user`). When the named area lists it
    # among its `tables`, fall back to the org-wide definition instead of reporting "not found".
    if area:
        sa = org.subject_area(area)
        if sa and any(ref.table == table_name or ref.table == bare for ref in sa.tables):
            return _find_table(org, table_name, area=None)
    return None


def _table_alias(table_name: str) -> str:
    return bare_name(table_name)


def collect_default_filters(
    org: Datasource,
    table_names: Iterable[str],
    *,
    area: Optional[str] = None,
    params: Optional[dict[str, str]] = None,
    index: Optional[TableIndex] = None,
) -> list[str]:
    """Union of default_filters for the in-scope tables, with :param substitution.

    `{alias}` placeholders are replaced with the table's bare name; `:param`
    bind markers are replaced from `params` when provided (else left as-is so the
    executor can bind them). Deduped, order-stable.
    """
    params = params or {}
    out: list[str] = []
    seen: set[str] = set()
    for name in table_names:
        table = _find_table(org, name, area, index=index)
        if table is None:
            continue
        alias = _table_alias(table.name)
        for flt in table.default_filters:
            resolved = flt.replace("{alias}", alias)
            for k, val in params.items():
                resolved = resolved.replace(f":{k}", str(val))
            if resolved not in seen:
                seen.add(resolved)
                out.append(resolved)
    return out


def get_table_index(
    table: Table, expose_column_groups: Optional[list[str]] = None
) -> dict[str, Any]:
    """mode=index: compact column listing (name + type + 1-line desc), scoped by
    expose_column_groups when set (honors the subject area's view of a wide table)."""
    visible = _visible_columns(table, expose_column_groups)
    return {
        "name": table.name,
        "schema": table.schema_name,
        "description": table.description,
        "grain": table.grain,
        "column_count_total": len(table.columns),
        "column_count_visible": len(visible),
        "columns": [
            {"name": c.name, "type": c.type, "description": c.description} for c in visible
        ],
    }


def _visible_columns(
    table: Table, expose_column_groups: Optional[list[str]]
) -> list[Column]:
    if not expose_column_groups:
        return list(table.columns)
    allowed: set[str] = set()
    for g in expose_column_groups:
        allowed.update(table.column_groups.get(g, []))
    # if a table declares column_groups, restrict; if not, expose all (defensive)
    if not allowed:
        return list(table.columns)
    return [c for c in table.columns if c.name in allowed]


def _exposed_groups_for(sa: SubjectArea, table_name: str) -> Optional[list[str]]:
    for ref in sa.tables:
        if ref.table == table_name or ref.table == _table_alias(table_name):
            return ref.expose_column_groups
    return None


def get_table_context(
    org: Datasource,
    tables: list[str],
    *,
    area: Optional[str] = None,
    columns: Optional[list[str]] = None,
    include: Optional[list[str]] = None,
    params: Optional[dict[str, str]] = None,
    index: Optional[TableIndex] = None,
) -> dict[str, Any]:
    """Compound context fetch — the primary mechanism for keeping inference rounds
    low. Returns columns (full detail for the requested set, honoring
    expose_column_groups), plus any of: default_filters, relationships, caveats,
    value_transforms, metrics.

    Pass `index` (from `build_table_index`) to resolve tables in O(1) instead of a per-table linear
    scan — used by the schema hot path on wide models (ACE-047). Behaviour is identical either way.
    """
    include = include or list(DEFAULT_CONTEXT_INCLUDE)
    sa = org.subject_area(area) if area else None
    result: dict[str, Any] = {"tables": {}}

    for name in tables:
        table = _find_table(org, name, area, index=index)
        if table is None:
            result["tables"][name] = {"error": "not found in scope"}
            continue
        exposed = _exposed_groups_for(sa, table.name) if sa else None
        visible = _visible_columns(table, exposed)
        if columns:
            wanted = set(columns)
            chosen = [c for c in visible if c.name in wanted]
        else:
            chosen = visible

        tinfo: dict[str, Any] = {
            "name": table.name,
            "schema": table.schema_name,
            "description": table.description,
            "grain": table.grain,
            "source_type": table.source_type,
            "columns": [_column_detail(c, include) for c in chosen],
        }
        if "caveats" in include and table.caveats:
            tinfo["caveats"] = table.caveats
        if "default_filters" in include:
            tinfo["default_filters"] = collect_default_filters(
                org, [table.name], area=area, params=params, index=index
            )
        if "performance_hints" in include and table.performance_hints:
            tinfo["performance_hints"] = table.performance_hints.model_dump(exclude_none=True)
        result["tables"][table.name] = tinfo

    if "relationships" in include:
        result["relationships"] = _relationships_among(org, tables, area)
    if "metrics" in include:
        result["metrics"] = _metrics_for(org, tables, area)

    return result


def _column_detail(col: Column, include: list[str]) -> dict[str, Any]:
    d: dict[str, Any] = {"name": col.name, "type": col.type, "description": col.description}
    if col.primary_key:
        d["primary_key"] = True
    if col.foreign_key:
        d["foreign_key"] = col.foreign_key.model_dump(exclude_none=True)
    if col.choice_field:
        d["choice_field"] = col.choice_field
    if col.sensitive:
        d["sensitive"] = True
    if col.unit:
        d["unit"] = col.unit
    if col.date_format:
        d["date_format"] = col.date_format   # e.g. epoch_s → convert in SQL + show as a date
    if col.timezone:
        d["timezone"] = col.timezone
    if "value_transforms" in include and col.value_transform:
        d["value_transform"] = col.value_transform
    if col.denormalized_from:
        d["denormalized_from"] = col.denormalized_from.model_dump(exclude_none=True)
    if "caveats" in include and col.caveats:
        d["caveats"] = col.caveats
    return d


def _relationships_among(
    org: Datasource, tables: list[str], area: Optional[str]
) -> list[dict[str, Any]]:
    names = {_table_alias(t) for t in tables} | set(tables)
    out: list[dict[str, Any]] = []
    areas = [org.subject_area(area)] if area else org.subject_areas
    for sa in areas:
        if sa is None:
            continue
        for rel in sa.relationships:
            if _table_alias(rel.from_table) in names or _table_alias(rel.to_table) in names:
                out.append(rel.model_dump(exclude_none=True))
    # cross-area edges touching these tables
    for rel in org.cross_subject_area_relationships:
        if _table_alias(rel.from_table) in names or _table_alias(rel.to_table) in names:
            out.append(rel.model_dump(exclude_none=True))
    return out


def _metrics_for(org: Datasource, tables: list[str], area: Optional[str]) -> list[dict[str, Any]]:
    names = {_table_alias(t) for t in tables} | set(tables)
    out: list[dict[str, Any]] = []
    areas = [org.subject_area(area)] if area else org.subject_areas
    for sa in areas:
        if sa is None:
            continue
        for met in sa.metrics:
            if not met.source_tables or any(_table_alias(s) in names for s in met.source_tables):
                out.append(met.model_dump(exclude_none=True))
    return out


def get_subject_area_bundle(org: Datasource, area: str) -> dict[str, Any]:
    """One-shot bundle for small subject areas (a few dozen tables)."""
    sa = org.subject_area(area)
    if sa is None:
        raise KeyError(f"no subject area {area!r}")
    table_names = [t.name for t in sa.tables_defined]
    # multi-area membership: also include tables this area REFERENCES (TableRef) but that are
    # defined in another area — they resolve via _find_table's org-wide fallback.
    defined = set(table_names)
    for ref in sa.tables:
        if ref.table not in defined:
            table_names.append(ref.table)
            defined.add(ref.table)
    bundle = get_table_context(
        org,
        table_names,
        area=area,
        include=list(DEFAULT_BUNDLE_INCLUDE),
    )
    bundle["subject_area"] = {
        "name": sa.name,
        "description": sa.description,
        "default_time_window": sa.default_time_window,
    }
    bundle["entities"] = [e.model_dump(exclude_none=True) for e in sa.entities]
    return bundle


def list_prompt_examples(root: str | Path, area: str,
                         *, include_rejected: bool = False) -> list[dict[str, Any]]:
    """Load scope-tagged examples for a subject area (prompt_examples/<area>/examples.yaml).

    Examples the curator rejected (`status: rejected`) are dropped by default so the
    runtime ranker never anchors on them — mirroring how `load_datasource` drops
    `review_state: rejected` model entries. Pass `include_rejected=True` for the curation
    view (re-render, dedup, audit), where a rejected example must still be visible.
    """
    f = Path(root) / "prompt_examples" / area / "examples.yaml"
    if not f.exists():
        return []
    doc = _read_yaml(f) or {}
    items = doc if isinstance(doc, list) else doc.get("examples", [])
    if include_rejected:
        return items
    return [e for e in items if (e or {}).get("status") != "rejected"]


__all__ = [
    "load_datasource",
    "load_org_id",
    "deployment_org_id",
    "collect_default_filters",
    "get_table_index",
    "get_table_context",
    "get_subject_area_bundle",
    "list_prompt_examples",
]
