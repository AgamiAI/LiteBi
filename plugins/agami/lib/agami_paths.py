#!/usr/bin/env python3
"""Single source of truth for agami's on-disk paths — stdlib only.

agami keeps everything under ONE folder the user chooses (the "artifacts dir"):

    <artifacts_dir>/
      local/              # gitignored — secrets + per-user state (NEVER committed)
        credentials       # chmod 600 — the only place credentials live
        .config           # JSON: active_profile, reviewer_email/role, tool_paths, …
        .pgpass, …        # provider-native auth files materialized from credentials
        query_log.jsonl   # personal query history
        charts/, exports/ # per-query outputs
        model/, review/, examples-validation/   # rendered dashboards
        tunnels/, serve/  # ssh tunnels, the copied MCP server
      <profile>/          # the committable semantic model (datasource.yaml + subject_areas/…)
      USER_MEMORY.md      # committable cross-database preferences
      .gitignore          # ignores local/

So there is no separate `~/.agami` — credentials and the model live side by side,
self-contained, and a teammate who clones the committed folder simply never gets
`local/` (they have their own credentials).

The ONE thing outside the folder is a non-sensitive pointer at `~/.config/agami/path`
holding the folder's location, so a fresh shell can find it. `AGAMI_ARTIFACTS_DIR`
overrides everything.
"""

from __future__ import annotations

import os
from pathlib import Path

POINTER_PATH = Path.home() / ".config" / "agami" / "path"
DEFAULT_ARTIFACTS_DIR = Path.home() / "agami-artifacts"
LEGACY_HOME = Path.home() / ".agami"          # the pre-consolidation location
LOCAL_SUBDIR = "local"                          # gitignored secrets/state dir inside the artifacts dir


def artifacts_dir() -> Path:
    """Resolve the artifacts dir: AGAMI_ARTIFACTS_DIR → the pointer file → default."""
    env = os.environ.get("AGAMI_ARTIFACTS_DIR")
    if env:
        return Path(os.path.expanduser(env)).resolve()
    try:
        if POINTER_PATH.exists():
            p = POINTER_PATH.read_text(encoding="utf-8").strip()
            if p:
                return Path(os.path.expanduser(p)).resolve()
    except OSError:
        pass
    return DEFAULT_ARTIFACTS_DIR.resolve()


def set_artifacts_dir(path: str | os.PathLike) -> Path:
    """Persist the chosen artifacts dir to the pointer file (so it survives sessions)."""
    resolved = Path(os.path.expanduser(str(path))).resolve()
    POINTER_PATH.parent.mkdir(parents=True, exist_ok=True)
    POINTER_PATH.write_text(str(resolved) + "\n", encoding="utf-8")
    return resolved


def local_dir(art: Path | None = None) -> Path:
    """The gitignored secrets/state dir — the consolidated replacement for ~/.agami."""
    return (art or artifacts_dir()) / LOCAL_SUBDIR


# --- specific paths (everything that used to live under ~/.agami) -----------

def credentials_path(art: Path | None = None) -> Path:
    return local_dir(art) / "credentials"


def config_path(art: Path | None = None) -> Path:
    # JSON file; keeps the legacy name `.config` so the one-shot migration is a pure move.
    return local_dir(art) / ".config"


def query_log_path(art: Path | None = None) -> Path:
    return local_dir(art) / "query_log.jsonl"


def dashboard_dir(kind: str, profile: str, art: Path | None = None) -> Path:
    """Rendered dashboards: kind in {model, review, examples-validation}."""
    return local_dir(art) / kind / profile


def serve_dir(art: Path | None = None) -> Path:
    return local_dir(art) / "serve"


def profile_dir(profile: str, art: Path | None = None) -> Path:
    """The committable per-profile semantic model root."""
    return (art or artifacts_dir()) / profile


# --- gitignore + migration --------------------------------------------------

def ensure_gitignore(art: Path | None = None) -> None:
    """Make sure the artifacts dir's .gitignore excludes the secrets/state dir."""
    art = art or artifacts_dir()
    gi = art / ".gitignore"
    line = f"{LOCAL_SUBDIR}/"
    try:
        existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
        if line not in existing.split():
            art.mkdir(parents=True, exist_ok=True)
            with gi.open("a", encoding="utf-8") as fh:
                if existing and not existing.endswith("\n"):
                    fh.write("\n")
                fh.write(f"# agami secrets + per-user state — never commit\n{line}\n")
    except OSError:
        pass


def _legacy_artifacts_dir() -> Path | None:
    """The custom artifacts dir the OLD `~/.agami/.config` recorded, if any — so the
    migration consolidates into the user's existing model location, not the default."""
    cfg = LEGACY_HOME / ".config"
    if not cfg.exists():
        return None
    try:
        import json
        ad = json.loads(cfg.read_text(encoding="utf-8")).get("artifacts_dir")
        if ad:
            return Path(os.path.expanduser(str(ad))).resolve()
    except (OSError, ValueError):
        pass
    return None


def migrate_legacy_home(art: Path | None = None) -> bool:
    """Move a pre-consolidation `~/.agami/` into `<artifacts_dir>/local/`, once.

    When `art` is None, consolidates into the artifacts dir the old config recorded
    (preserving a custom location), else the current/default one. Idempotent: a no-op
    if there's no legacy home or `local/` already exists. Leaves a tombstone behind.
    """
    if not LEGACY_HOME.is_dir():
        return False
    if art is None:
        art = _legacy_artifacts_dir() or artifacts_dir()
    dest = local_dir(art)
    if dest.exists():
        # Already consolidated. But a stale/cached process (e.g. an old pre-consolidation Desktop
        # MCP copy) can RESURRECT `~/.agami/.config` — the live config lives in `dest`, nothing
        # reads the legacy one, so sweep it back to an inert tombstone instead of letting it linger
        # and look authoritative.
        stale = LEGACY_HOME / ".config"
        if stale.exists():
            try:
                stale.unlink()
            except OSError:
                pass
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        LEGACY_HOME.rename(dest)
    except OSError:
        # rename() raises EXDEV when the artifacts dir is on a different
        # filesystem than ~/.agami (external drive, separate mount). Fall back
        # to a copy+delete move, which crosses devices.
        import shutil
        shutil.move(str(LEGACY_HOME), str(dest))
    ensure_gitignore(art)
    set_artifacts_dir(art)
    try:
        LEGACY_HOME.mkdir(parents=True, exist_ok=True)
        (LEGACY_HOME / "MOVED.txt").write_text(
            f"agami now keeps everything under your artifacts folder.\n"
            f"This dir's contents moved to: {dest}\n", encoding="utf-8")
    except OSError:
        pass
    return True


def bootstrap() -> Path:
    """Call at the start of every entry point: runs the one-shot legacy migration
    (idempotent — a cheap no-op once done) and returns the resolved artifacts dir."""
    migrate_legacy_home()
    return artifacts_dir()
