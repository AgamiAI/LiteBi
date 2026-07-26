"""The deployment-level organization record (F15 / ACE-067).

One ``OrgRecord`` lives at ``<artifacts_dir>/organization.yaml`` — ABOVE the per-profile
``<artifacts_dir>/<profile>/org.yaml`` models — and holds the company-wide facts (name, description,
fiscal year, display conventions, glossary) that would otherwise be duplicated into every profile's
``org.yaml`` and drift. The company narrative lives beside it at ``<artifacts_dir>/ORGANIZATION.md``.

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

from .models import OrgRecord

# The record and the company narrative both sit at the artifacts-dir ROOT (one deployment = one company),
# NOT under a profile dir — that is the whole point: written once, shared by every datasource.
RECORD_FILENAME = "organization.yaml"
NARRATIVE_FILENAME = "ORGANIZATION.md"


def record_path(artifacts_dir: str | Path) -> Path:
    return Path(artifacts_dir) / RECORD_FILENAME


def narrative_path(artifacts_dir: str | Path) -> Path:
    return Path(artifacts_dir) / NARRATIVE_FILENAME


def load_org_record(artifacts_dir: str | Path) -> Optional[OrgRecord]:
    """Return the ``OrgRecord`` at ``<artifacts_dir>/organization.yaml``, or ``None`` if the deployment
    has no record yet. Read-only and lenient (never raises on a missing file) so a pre-F15 deployment
    degrades to today's per-profile behaviour rather than erroring."""
    path = record_path(artifacts_dir)
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    return OrgRecord.model_validate(doc)


def ensure_org_record(artifacts_dir: str | Path) -> OrgRecord:
    """Read the deployment's ``OrgRecord``, or mint a fresh one and persist it. The ``org_id`` mint
    chokepoint (relocated here from the per-profile ``org.yaml`` — F14's ``ensure_org_id``):

      1. an existing ``organization.yaml`` is returned unchanged (mint-once / immutable);
      2. else, if a profile ``org.yaml`` already carries an id (a post-F14 deployment), that id is
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
    return the path. Written with the same default permissions as the sibling ``org.yaml`` — the record
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
    (each immediate subdir holding an ``org.yaml``), so the list is auto-maintained and can never drift.
    Returns ``None`` (and writes nothing) when there is neither a record nor any profile yet; otherwise
    mints the record if needed, updates the list, persists, and returns it."""
    art = Path(artifacts_dir)
    names = (
        sorted(p.name for p in art.iterdir() if p.is_dir() and (p / "org.yaml").exists())
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
