"""Model snapshots — the source of an answer's `model_version` pin.

An answer's trust receipt reports `model_version` = the newest directory name
under `<profile>/.snapshots/`, where the directory name IS a content hash of the
model (read by `mcp_server._model_version` and the agami-query skill's
`ls -t .snapshots/ | head -1`). Nothing used to WRITE that directory, so
`model_version` was `null` for every profile. This module is the writer: it is
called from the deterministic model-write chokepoints (introspect's
`build.write_tree` and every curation commit via `curate._git_commit`) so a
snapshot is stamped whenever the model changes — independent of git, identical
across the skill / MCP server / cron.

Design:
  * The directory NAME is a 12-char SHA-256 over the model's content (every file
    under the profile root except machine-state: .snapshots/, .introspect/,
    .legacy_backup/, .git/, curation_log.jsonl). So the hash changes iff the
    model changes, and writing is idempotent — an unchanged model re-uses its
    existing directory (we just refresh its mtime so it stays "newest").
  * Each snapshot dir holds a deterministic `manifest.json` (model hash + per-file
    hashes) — no timestamp in the content, so a committed snapshot never churns;
    recency comes from the directory mtime, which is what the readers use.
  * Best-effort: never raises into the write path; a snapshot failure must not
    break introspection or curation.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

SNAPSHOT_DIR = ".snapshots"
KEEP = 20  # cap retained snapshots; prune oldest beyond this

# Machine-state that is NOT part of the model's identity.
_EXCLUDE_TOP = {SNAPSHOT_DIR, ".introspect", ".legacy_backup", ".git"}
_EXCLUDE_NAMES = {"curation_log.jsonl"}


def _model_files(root: Path) -> list[Path]:
    """Every model file under root (sorted), excluding machine-state."""
    out: list[Path] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if rel.parts and rel.parts[0] in _EXCLUDE_TOP:
            continue
        if p.name in _EXCLUDE_NAMES:
            continue
        out.append(p)
    return out


def _hash_and_manifest(
    root: Path, files: list[Path], *, want_manifest: bool = True
) -> tuple[str, dict[str, str]]:
    """Read each model file ONCE and derive the rolling 12-char model hash (path + bytes, in sorted
    order) — and, when `want_manifest`, the per-file manifest shas from the same bytes. Folding the
    manifest build into the hash pass is what drops a changed-model snapshot from two whole-tree byte
    reads to one (ACE-046); the hash-only callers skip the extra per-file digest. Both outputs are
    byte-identical to computing them separately."""
    h = hashlib.sha256()
    manifest: dict[str, str] = {}
    for p in files:
        rel = str(p.relative_to(root)).replace(os.sep, "/")
        data = p.read_bytes()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(data)
        h.update(b"\0")
        if want_manifest:
            manifest[rel] = hashlib.sha256(data).hexdigest()
    return h.hexdigest()[:12], manifest


def compute_model_hash(root: str | os.PathLike) -> str:
    """12-char content hash over the model tree (path + bytes of each file)."""
    root = Path(root)
    digest, _ = _hash_and_manifest(root, _model_files(root), want_manifest=False)
    return digest


def _snapshot_dirs_newest_first(snaps: Path) -> list[Path]:
    return sorted((p for p in snaps.iterdir() if p.is_dir()),
                  key=lambda p: p.stat().st_mtime, reverse=True)


def newest_version(root: str | os.PathLike) -> str | None:
    """The `model_version` pin = newest dir name under <root>/.snapshots (the content
    hash). Single reader for the CLI, the MCP server, and the skill. None if there are
    no snapshots yet / on any error (a legacy model that was never stamped)."""
    try:
        dirs = _snapshot_dirs_newest_first(Path(root) / SNAPSHOT_DIR)
        return dirs[0].name if dirs else None
    except Exception:
        return None


def _prune(snaps: Path, keep: int) -> None:
    for old in _snapshot_dirs_newest_first(snaps)[keep:]:
        shutil.rmtree(old, ignore_errors=True)


def write_snapshot(root: str | os.PathLike) -> str | None:
    """Stamp a snapshot of the current model state. Returns the hash (the
    `model_version`), or None if there's no model / on any error (best-effort)."""
    try:
        root = Path(root)
        if not (root / "datasource.yaml").exists():
            return None  # not a model root — nothing to snapshot
        # One pass: the hash (to name the dir) and the manifest shas (written only on a new dir)
        # come from a single read of each file, not two.
        digest, manifest_files = _hash_and_manifest(root, _model_files(root))
        snaps = root / SNAPSHOT_DIR
        snaps.mkdir(exist_ok=True)
        d = snaps / digest
        if d.exists():
            os.utime(d, None)  # unchanged model → keep its dir, mark it newest
        else:
            d.mkdir()
            manifest = {
                "schema_version": 1,
                "model_hash": digest,
                "files": manifest_files,
            }
            (d / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        _prune(snaps, KEEP)
        return digest
    except Exception:
        return None
