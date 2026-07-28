"""The deployment-level organization record (F15 / ACE-067).

One ``OrgRecord`` lives at ``<artifacts_dir>/organization.yaml`` — ABOVE the per-profile
``<artifacts_dir>/<profile>/datasource.yaml`` models — and holds the company-wide facts (name, description,
fiscal year, display conventions, glossary) that would otherwise be duplicated into every profile's
``datasource.yaml`` and drift. The company narrative lives beside it at ``<artifacts_dir>/organization.md``.

This module owns:

  * ``load_org_record(art)``   — read the record (``None`` when absent — the graceful-degradation path
    the composition layer, ACE-069, relies on).
  * ``ensure_org_record(art)`` — read-or-mint. Relocates F14's ``org_id`` up into the record: the id is
    minted ONCE (``uuid4``, immutable, deployment-scoped — F14's rules verbatim) and, for a deployment
    that already carried a per-profile id (post-F14), LIFTED up instead of re-minted.

No network egress — pure local file I/O (stdlib + PyYAML + pydantic). ``tests/test_privacy_no_network.py``
is a static source scan of this tree; keep the imports egress-free.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

from .models import (
    CrossDatasourceMetric,
    CrossDatasourceRelationship,
    MigratedFrom,
    OrgRecord,
)

# The record and the company narrative both sit at the artifacts-dir ROOT (one deployment = one company),
# NOT under a profile dir — that is the whole point: written once, shared by every datasource.
RECORD_FILENAME = "organization.yaml"
NARRATIVE_FILENAME = "organization.md"

# Deployment-level cross-datasource bridges (F16 / ACE-072) merge from three sources on load:
#   1. inline on organization.yaml (parsed by model_validate);
#   2. a sidecar at the artifacts-dir root (this file);
#   3. the legacy skill-side file the connect flow used before bridges were a model type.
BRIDGES_FILENAME = "cross_datasource_relationships.yaml"
LEGACY_BRIDGES_PATH = ("local", "cross_profile_relationships.yaml")

# Deployment-level cross-datasource metrics (F16 / ACE-073) merge from two sources on load: inline on
# organization.yaml, and an optional root sidecar (this file). There is no legacy source — metrics
# never had a skill-side file (bridges did). Deduped by `name` (a metric always has one; a bridge is
# anonymous, so it dedups by endpoint instead).
METRICS_FILENAME = "cross_datasource_metrics.yaml"


def record_path(artifacts_dir: str | Path) -> Path:
    return Path(artifacts_dir) / RECORD_FILENAME


def narrative_path(artifacts_dir: str | Path) -> Path:
    return Path(artifacts_dir) / NARRATIVE_FILENAME


def load_org_record(artifacts_dir: str | Path) -> Optional[OrgRecord]:
    """Return the ``OrgRecord`` at ``<artifacts_dir>/organization.yaml``, or ``None`` if the deployment
    has no record yet. Read-only and lenient (never raises on a missing file) so a pre-F15 deployment
    degrades to today's per-profile behaviour rather than erroring.

    The record's ``cross_datasource_relationships`` (F16 / ACE-072) are the MERGE of three sources —
    inline, a root sidecar, and a legacy skill-side file — de-duplicated by endpoint so re-loading is
    idempotent. Its ``cross_datasource_metrics`` (F16 / ACE-073) merge two sources — inline + a root
    sidecar — de-duplicated by ``name`` (no legacy source; metrics never had a skill-side file). The
    merge is in-memory only; ``load_org_record`` writes nothing (a legacy migration surfaces the edges
    without mutating disk)."""
    path = record_path(artifacts_dir)
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    record = OrgRecord.model_validate(doc)
    updates = {}
    merged = _merge_bridges(record.cross_datasource_relationships + _load_bridges(artifacts_dir))
    if merged != record.cross_datasource_relationships:
        updates["cross_datasource_relationships"] = merged
    merged_metrics = _merge_metrics(record.cross_datasource_metrics + _load_metrics(artifacts_dir))
    if merged_metrics != record.cross_datasource_metrics:
        updates["cross_datasource_metrics"] = merged_metrics
    if updates:
        record = record.model_copy(update=updates)
    return record


def _merge_bridges(
    bridges: list[CrossDatasourceRelationship],
) -> list[CrossDatasourceRelationship]:
    """De-duplicate bridges by their endpoint identity, keeping the FIRST occurrence (inline wins
    over sidecar wins over legacy — the order they are concatenated). Idempotent: a re-load that
    picks up the same legacy file again collapses back to one edge per endpoint tuple."""
    out: list[CrossDatasourceRelationship] = []
    seen: set[tuple] = set()
    for b in bridges:
        if b.endpoint_key in seen:
            continue
        seen.add(b.endpoint_key)
        out.append(b)
    return out


def _bridge_entries(doc, key: str = "relationships") -> list:
    """The entry list from a parsed doc — a ``{key}:`` mapping OR a bare YAML list (both accepted,
    mirroring ``loader._load_cross_rels``); anything else yields ``[]``. ``key`` is ``relationships``
    for bridges and ``metrics`` for cross-datasource metrics. Explicit rather than ``doc.get(...)``
    because ``.get`` on a bare list raises ``AttributeError``."""
    if isinstance(doc, dict):
        return doc.get(key) or []
    if isinstance(doc, list):
        return doc
    return []


def _load_bridges(artifacts_dir: str | Path) -> list[CrossDatasourceRelationship]:
    """Load the non-inline bridge sources: the root sidecar (source 2) and the legacy skill-side
    file (source 3). Both are optional; a deployment with neither yields an empty list. Kept out of
    ``load_org_record`` so that function stays a thin read + merge."""
    art = Path(artifacts_dir)
    out: list[CrossDatasourceRelationship] = []

    # Source 2 — sidecar at the artifacts-dir root; key `relationships:` or a bare list (mirrors
    # loader._load_cross_rels). Entries are already in the model's field shape.
    sidecar = art / BRIDGES_FILENAME
    if sidecar.exists():
        try:
            doc = yaml.safe_load(sidecar.read_text(encoding="utf-8")) or {}
        except Exception:
            # A syntactically corrupt sidecar file degrades to no bridges — same never-raise
            # contract as the per-entry skip below (load_org_record is on runtime paths).
            doc = {}
        for r in _bridge_entries(doc):
            try:
                out.append(CrossDatasourceRelationship(**r))
            except Exception:
                # load_org_record is lenient by contract (never raises — it's on runtime paths). A
                # malformed entry is skipped so its valid siblings in the same file still load.
                continue

    # Source 3 — legacy migration: the skill-side file the connect flow wrote before bridges were a
    # model type. Its `from_profile`/`to_profile` become `from_datasource`/`to_datasource`; the edge is
    # a `split`/`unreviewed` bridge stamped with a `migrated_from` marker (so nothing is lost, and a
    # future write is idempotent). Read-only here — the migration is surfaced, not persisted.
    legacy = art / LEGACY_BRIDGES_PATH[0] / LEGACY_BRIDGES_PATH[1]
    if legacy.exists():
        try:
            doc = yaml.safe_load(legacy.read_text(encoding="utf-8")) or {}
        except Exception:
            # A corrupt legacy file is likewise skipped, not raised — a stale hand-edited file
            # can't take down a runtime load_org_record call.
            doc = {}
        for r in _bridge_entries(doc):
            try:
                out.append(_migrate_legacy_bridge(r, str(legacy)))
            except Exception:
                # Same leniency: a legacy entry missing a required key is skipped, not raised, so a
                # single malformed row can't take down a runtime load_org_record call.
                continue
    return out


def _migrate_legacy_bridge(entry: dict, source_file: str) -> CrossDatasourceRelationship:
    """Map one legacy ``cross_profile_relationships.yaml`` entry into a ``CrossDatasourceRelationship``.
    The legacy shape keyed the endpoints on ``from_profile``/``to_profile`` (a profile = a datasource);
    everything else carries. The edge is unreviewed by construction — a migrated guess a human still
    signs off — and split (a cross-datasource edge spans two engines)."""
    return CrossDatasourceRelationship(
        from_datasource=entry["from_profile"],
        to_datasource=entry["to_profile"],
        from_dataset=entry["from_dataset"],
        to_dataset=entry["to_dataset"],
        from_columns=entry["from_columns"],
        to_columns=entry["to_columns"],
        executable="split",
        relationship=entry.get("relationship"),
        description=entry.get("description", ""),
        name=entry.get("name"),
        review_state="unreviewed",
        migrated_from=MigratedFrom(source_file=source_file),
    )


def _merge_metrics(
    metrics: list[CrossDatasourceMetric],
) -> list[CrossDatasourceMetric]:
    """De-duplicate metrics by ``name``, keeping the FIRST occurrence (inline wins over sidecar — the
    order they are concatenated). Metrics dedup by ``name`` (they always carry one — it's required),
    unlike anonymous bridges which dedup by endpoint. Idempotent: a re-load collapses back to one per
    name."""
    out: list[CrossDatasourceMetric] = []
    seen: set[str] = set()
    for m in metrics:
        if m.name in seen:
            continue
        seen.add(m.name)
        out.append(m)
    return out


def _load_metrics(artifacts_dir: str | Path) -> list[CrossDatasourceMetric]:
    """Load the non-inline metric source: the optional root sidecar (``cross_datasource_metrics.yaml``),
    keyed ``metrics:`` or a bare list. There is NO legacy source — metrics never had a skill-side file
    (bridges did). Read-only and lenient: a corrupt file or a malformed entry is skipped, not raised,
    matching the bridge loader's never-raise contract (``load_org_record`` is on runtime paths)."""
    art = Path(artifacts_dir)
    out: list[CrossDatasourceMetric] = []
    sidecar = art / METRICS_FILENAME
    if sidecar.exists():
        try:
            doc = yaml.safe_load(sidecar.read_text(encoding="utf-8")) or {}
        except Exception:
            # A syntactically corrupt sidecar degrades to no metrics — same never-raise contract as
            # the per-entry skip below.
            doc = {}
        for m in _bridge_entries(doc, key="metrics"):
            try:
                out.append(CrossDatasourceMetric(**m))
            except Exception:
                # A malformed metric entry is skipped so its valid siblings in the same file still load.
                continue
    return out


def ensure_org_record(artifacts_dir: str | Path) -> OrgRecord:
    """Read the deployment's ``OrgRecord``, or mint a fresh one and persist it. The ``org_id`` mint
    chokepoint (relocated here from the per-profile ``datasource.yaml`` — F14's ``ensure_org_id``):

      1. an existing ``organization.yaml`` is returned unchanged (mint-once / immutable);
      2. else, if a profile ``datasource.yaml`` already carries an id (a post-F14 deployment), that id is
         LIFTED up into a new record — never re-minted (preserves F14's immutable value);
      3. else a fresh ``uuid4().hex`` is minted into a new record.

    Idempotent: a second call returns the same record (same id). Pure-local — the uuid4 is generated
    on-box with no coordinator (the only option under F14's no-egress invariant)."""
    existing = load_org_record(artifacts_dir)
    if existing is not None:
        return existing

    record = OrgRecord(org_id=_lifted_or_minted_org_id(artifacts_dir))
    write_org_record(artifacts_dir, record)
    return record


def write_org_record(artifacts_dir: str | Path, record: OrgRecord) -> Path:
    """Persist ``record`` to ``<artifacts_dir>/organization.yaml`` (creating the dir if needed) and
    return the path. Written with the same default permissions as the sibling ``datasource.yaml`` — the record
    holds company context, not secrets, and mode-600 model files are unreadable by the deploy
    container user (a known crash-loop), so this deliberately does NOT ``chmod 600``."""
    path = record_path(artifacts_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = record.model_dump(mode="json", exclude_none=True)
    path.write_text(
        yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8",
    )
    return path


def set_org_fields(
    artifacts_dir: str | Path,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
) -> OrgRecord:
    """Set the human-authored company fields on the record, minting it first if absent. Only the fields
    passed (non-``None``) are updated; the rest are left untouched. Persists and returns the record.
    This is the write path onboarding uses to populate ``name``/``description`` (the record is otherwise
    minted with just an ``org_id``)."""
    record = ensure_org_record(artifacts_dir)
    changes = {k: v for k, v in {"name": name, "description": description}.items() if v is not None}
    if changes:
        record = record.model_copy(update=changes)
        write_org_record(artifacts_dir, record)
    return record


def refresh_datasources(artifacts_dir: str | Path) -> Optional[OrgRecord]:
    """Rebuild the record's ``datasources`` list from the profile directories actually present on disk
    (each immediate subdir holding an ``datasource.yaml``), so the list is auto-maintained and can never drift.
    Returns ``None`` (and writes nothing) when there is neither a record nor any profile yet; otherwise
    mints the record if needed, updates the list, persists, and returns it."""
    art = Path(artifacts_dir)
    names = (
        sorted(p.name for p in art.iterdir() if p.is_dir() and (p / "datasource.yaml").exists())
        if art.is_dir()
        else []
    )
    existing = load_org_record(artifacts_dir)
    if existing is None and not names:
        return None
    record = existing or ensure_org_record(artifacts_dir)
    if record.datasources != names:
        record = record.model_copy(update={"datasources": names})
        write_org_record(artifacts_dir, record)
    return record


def _lifted_or_minted_org_id(artifacts_dir: str | Path) -> str:
    """The legacy lift: reuse a per-profile ``org_id`` if one exists (post-F14 deployment), else mint.
    Kept here (not in the resolver) so the id is written into the record exactly once."""
    from uuid import uuid4  # local generation only — no egress (F14 invariant)

    from . import loader

    return loader.deployment_org_id(artifacts_dir) or uuid4().hex
