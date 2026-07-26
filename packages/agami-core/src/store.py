"""Backend-portable store — the thin DB layer the hosted server serves from.

A shared/multi-instance server can't keep state in local files, so the model + prompt examples are
served from a database and query logs are written to it. The backend is **Postgres in
production** (cloud-neutral, networked, multi-instance-safe), but the schema + queries are kept
**portable** so the same code runs on **SQLite** — which is what the test suite uses (no DB service
needed in CI) and is fine for a small single-instance self-host.

This module is the only place that knows a backend dialect. Everything above it (the model loader,
the activity sink, example serving) writes one set of SQL with `?` placeholders and uses dict rows,
and `Store` adapts to whichever backend `AGAMI_DB_URL` selects:

    sqlite://                      → in-memory (tests)
    sqlite:///abs/path/agami.db    → a file (self-host)
    postgresql://user:pw@host/db   → Postgres (production; needs the [server] extra)

Migrations are ordered `migrations/core/NNN_*.sql` applied idempotently via a `schema_migrations`
tracking table — re-running only applies new files.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# The migrations ship INSIDE the package (see pyproject's packages/package-data), so this resolves
# next to this module — identically from an installed wheel and from a checkout. Resolving it out of
# the tree (e.g. parents[3]) silently yields nothing once installed: glob on a missing dir returns [].
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations" / "core"

# A fixed (non-secret) key for the Postgres session advisory lock that serializes concurrent
# migration runs — see run_migrations. The digits spell "AGAMI" in hex; any stable bigint works.
_MIGRATION_LOCK_KEY = 0x4147414D49


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Extra migration roots layered on top of migrations/core. A consumer registers its root here at
# boot so the mcp_http lifespan's `run_migrations()` (no args) applies it — no call-site edit.
_MIGRATION_OVERLAYS: list[Path] = []


def register_migration_overlay(path: Path) -> None:
    """Register an extra migration root, applied AFTER migrations/core in registration order.

    Lets a consumer overlay its own tables without editing core's tree. Each overlay root must
    have a distinct, non-empty directory name — the name namespaces its tracking ids so an overlay
    file can't collide with a core file on the schema_migrations primary key; `run_migrations`
    raises on an empty or duplicated overlay name."""
    # Normalize first so different spellings of one dir (relative/absolute/symlinked) dedupe here
    # rather than slipping through as duplicate roots that the run_migrations namespace guard then
    # rejects. resolve() doesn't require the path to exist (strict=False); run_migrations checks that.
    path = path.resolve()
    if path not in _MIGRATION_OVERLAYS:
        _MIGRATION_OVERLAYS.append(path)


class Store:
    """A DB-API connection + its dialect, with portable execute/query helpers.

    SQL is authored once with `?` placeholders and adapted per dialect; rows come back as plain
    dicts on every backend (built from cursor.description), so callers never branch on the backend.
    """

    def __init__(self, conn: Any, dialect: str) -> None:
        self.conn = conn
        self.dialect = dialect  # "sqlite" | "postgres"

    # --- construction -------------------------------------------------------

    @classmethod
    def connect(cls, url: str) -> Store:
        if url.startswith("sqlite://"):
            import sqlite3

            rest = url[len("sqlite://") :]
            path = ":memory:" if rest in ("", ":memory:") else rest
            conn = sqlite3.connect(path)
            conn.execute("PRAGMA foreign_keys = ON")
            return cls(conn, "sqlite")
        if url.startswith(("postgresql://", "postgres://")):
            import psycopg2  # in the [server] extra; only needed for the Postgres backend

            return cls(psycopg2.connect(url), "postgres")
        raise ValueError(
            f"Unsupported AGAMI_DB_URL scheme: {url.split('://', 1)[0]!r} "
            "(expected sqlite:// or postgresql://)"
        )

    @classmethod
    def from_env(cls) -> Store | None:
        """Open the store named by the DB env var, or None when unset (the local file path is used).

        `AGAMI_DB_URL` is canonical; `APP_DATABASE_URL` is accepted as an alias for the common
        cloud-platform convention (Cloud Run/ACA/Heroku-style `*_DATABASE_URL`). Canonical wins if
        both are set, so a deliberate override is unambiguous."""
        import os

        url = (
            os.environ.get("AGAMI_DB_URL", "").strip()
            or os.environ.get("APP_DATABASE_URL", "").strip()
        )
        return cls.connect(url) if url else None

    # --- portable SQL -------------------------------------------------------

    def _adapt(self, sql: str) -> str:
        # Our SQL never contains a literal '?'; Postgres wants %s placeholders.
        return sql if self.dialect == "sqlite" else sql.replace("?", "%s")

    def execute(self, sql: str, params: tuple = ()) -> Any:
        cur = self.conn.cursor()
        cur.execute(self._adapt(sql), params)
        return cur

    def query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        cur = self.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def close(self) -> None:
        self.conn.close()

    # --- migrations ---------------------------------------------------------

    def run_migrations(
        self, migrations_dir: Path | None = None, overlay_dirs: list[Path] | None = None
    ) -> list[str]:
        """Apply un-applied `NNN_*.sql` in order; return the tracking ids newly applied. Idempotent.

        Applies `migrations/core` first, then any overlay roots in order — a consumer layers
        its own tables on top of core without editing core's tree. `overlay_dirs` overrides the
        registered overlays (`register_migration_overlay`); pass `[]` to force core-only. Core ids stay
        the bare filename (so already-migrated DBs are byte-identical); overlay ids are namespaced by
        their root's name, so a core and an overlay file with the same name can't collide on the pk.
        Overlay roots must have distinct, non-empty directory names — this raises `ValueError` otherwise.

        On Postgres a **session advisory lock** brackets the read-applied + apply so that when several
        instances boot together (e.g. Cloud Run) exactly one migrates and the rest wait, then re-read the
        applied set and skip what's done — otherwise two instances could both run a migration's DDL or
        collide on the `schema_migrations` primary key. SQLite is single-writer, so the lock is a no-op.
        A failing migration propagates (fail-closed: a half-migrated schema must not serve)."""
        migrations_dir = migrations_dir or MIGRATIONS_DIR
        # Same failure mode the overlays guard against, but for CORE — and far worse: a missing core root
        # globs to nothing, so the server would boot on an EMPTY schema with no error at all. Fail loudly.
        if not migrations_dir.is_dir():
            raise ValueError(f"core migration root is not a directory: {migrations_dir}")
        overlays = overlay_dirs if overlay_dirs is not None else list(_MIGRATION_OVERLAYS)
        # A registered overlay that doesn't exist (or isn't a directory) would silently apply nothing —
        # `glob` on a bad path yields an empty iterator — so a misconfigured overlay would be skipped
        # with no signal. Fail fast so the misconfig is loud rather than an unmigrated schema.
        not_dirs = [str(root) for root in overlays if not root.is_dir()]
        if not_dirs:
            raise ValueError(f"overlay migration root is not a directory: {not_dirs}")
        # Overlay tracking ids are namespaced by the root's directory name. Fail fast (before any lock
        # or DDL) if a name is empty — an empty name falls back to a BARE id that collides with core —
        # or duplicated across overlays — two roots would then map distinct files to the same id. Left
        # unchecked, either surfaces mid-apply as a schema_migrations pk violation or a skipped migration.
        namespaces = [root.name for root in overlays]
        if "" in namespaces:
            raise ValueError(
                "overlay migration root has an empty directory name; give each overlay a distinct name"
            )
        dupes = sorted({n for n in namespaces if namespaces.count(n) > 1})
        if dupes:
            raise ValueError(
                f"overlay migration roots share a directory name: {dupes}; names must be distinct"
            )
        # (namespace, root): core is un-namespaced (bare ids, backwards-compatible); each overlay is
        # namespaced by its directory name so its ids can't collide with core's on the pk.
        roots = [("", migrations_dir)] + [(root.name, root) for root in overlays]
        # pg_advisory_lock (session-level, NOT released by commit) — must use the session variant so the
        # per-migration commits in the loop below don't drop it mid-apply.
        locked = self.dialect == "postgres"
        if locked:
            self.execute("SELECT pg_advisory_lock(?)", (_MIGRATION_LOCK_KEY,))
        try:
            self.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (id TEXT PRIMARY KEY, applied_at TEXT)"
            )
            self.commit()
            applied = {r["id"] for r in self.query("SELECT id FROM schema_migrations")}
            ran: list[str] = []
            for namespace, root in roots:
                for path in sorted(root.glob("*.sql")):
                    mid = f"{namespace}:{path.name}" if namespace else path.name
                    if mid in applied:
                        continue
                    self._run_script(path.read_text())
                    self.execute(
                        "INSERT INTO schema_migrations (id, applied_at) VALUES (?, ?)",
                        (mid, _now_iso()),
                    )
                    self.commit()
                    ran.append(mid)
        except Exception:
            if locked:
                # A failed migration leaves the psycopg2 connection in an aborted-transaction state, so roll
                # back FIRST so the unlock can run; suppress cleanup errors here so the REAL migration error
                # is what propagates (the lock also frees on connection close as a backstop).
                with contextlib.suppress(Exception):
                    self.conn.rollback()
                    self.execute("SELECT pg_advisory_unlock(?)", (_MIGRATION_LOCK_KEY,))
                    self.commit()
            raise
        if locked:
            # Success: release the lock and let an unexpected unlock failure SURFACE — silently holding the
            # lock would hang the next instance on pg_advisory_lock.
            self.execute("SELECT pg_advisory_unlock(?)", (_MIGRATION_LOCK_KEY,))
            self.commit()
        return ran

    def _run_script(self, sql: str) -> None:
        """Run a multi-statement SQL script. SQLite needs executescript; psycopg2 runs a multi-
        statement string in one execute."""
        if self.dialect == "sqlite":
            self.conn.executescript(sql)
        else:
            self.conn.cursor().execute(sql)
