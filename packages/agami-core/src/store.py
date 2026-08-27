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


def _is_read(sql: str) -> bool:
    """Whether a statement can be assumed to write nothing.

    **Deliberately one keyword, and deliberately conservative.** This is not a SQL parser and must
    not become one — the repo has exactly one of those and a second would be a second thing to keep
    correct. It decides one thing: may a reconnect drop this statement without losing data?

    Only a bare `SELECT` qualifies. `WITH` is excluded even though most CTEs are reads, because
    Postgres allows a data-modifying statement inside one and a wrong answer here is silent. Anything
    unrecognised counts as a write, so the failure direction is a refused reconnect rather than a
    lost row.

    The word boundary is not pedantry: a prefix match alone reads any identifier beginning with those
    six letters as a query, and being wrong in that direction is the one direction that loses data.
    """
    head = sql.lstrip()
    if head[:6].upper() != "SELECT":
        return False
    after = head[6:7]
    return after == "" or not (after.isalnum() or after == "_")


class Store:
    """A DB-API connection + its dialect, with portable execute/query helpers.

    SQL is authored once with `?` placeholders and adapted per dialect; rows come back as plain
    dicts on every backend (built from cursor.description), so callers never branch on the backend.
    """

    def __init__(self, conn: Any, dialect: str, *, url: str = "") -> None:
        self.conn = conn
        self.dialect = dialect  # "sqlite" | "postgres"
        # Kept so a connection the server reaped can be reopened — see `execute`. Empty for SQLite
        # and for a hand-built Store, and an empty url simply means no reconnect is attempted.
        self._url = url
        # Whether an uncommitted WRITE is outstanding. **This is what makes the reconnect safe**,
        # and it is the whole reason this flag exists: see `_reconnected`.
        self._mid_transaction = False
        # Whether something holds state that belongs to this connection rather than to a transaction
        # — today only the migration advisory lock. A new connection would not have it.
        self._session_state_held = False

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

            return cls(psycopg2.connect(url), "postgres", url=url)
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

    def _dead_connection_errors(self) -> tuple:
        """The exceptions that *may* mean "this connection is gone", or nothing on SQLite.

        Both are needed and they arrive at different moments. `OperationalError` is raised the first
        time a statement meets a socket the server has closed — so it comes out of `cursor.execute`.
        Every call after that gets `InterfaceError` from `conn.cursor()`, because the driver has by
        then marked the connection closed. A deployment that handled only the second would recover
        from every failure except the one that starts each outage.

        Catching them is not the same as acting on them — see `_is_dead`.
        """
        if self.dialect != "postgres":
            return ()
        import psycopg2

        return (psycopg2.InterfaceError, psycopg2.OperationalError)

    def _is_dead(self, exc: Exception) -> bool:
        """Whether that exception really means the connection is gone.

        **`OperationalError` is a broad base class** — a cancelled query, a statement timeout, a full
        disk all raise it, on a connection that is perfectly healthy. Retrying blindly on any of them
        would re-run the statement, and for the first write of a transaction (where the guard below
        correctly permits a reconnect) that means the row lands **twice**. A retry is only safe when
        there is nothing left to retry *on*, so the driver's own view decides: psycopg2 sets
        `conn.closed` non-zero once the connection is unusable.

        `InterfaceError` needs no such test. It is raised by `conn.cursor()`, which the driver only
        refuses when the connection is already closed.
        """
        import psycopg2

        if isinstance(exc, psycopg2.InterfaceError):
            return True
        return bool(getattr(self.conn, "closed", 0))

    def _reconnected(self) -> bool:
        """Reopen the connection, unless doing so would be unsafe or impossible.

        **Refuses mid-transaction, and that refusal is the point of this method.** Several callers
        write two rows that must land together — a role change and its audit line, a credential and
        the record of who set it. If the connection dies between the two statements, the first is
        already lost, and silently reconnecting would let the second commit **alone**: an audit line
        for a grant that never happened, which is precisely the property the audit line exists to
        provide. A caller mid-transaction must see the failure and roll back.

        Between statements there is nothing uncommitted to lose, which is the ordinary case — a read,
        or the first statement of a unit of work — and that is where an hour-idle connection is
        reaped in practice.

        **It also refuses while session-scoped state is held.** A Postgres advisory lock belongs to a
        connection, not to a transaction, so reconnecting silently continues without it. During
        migrations that would let two instances apply the same files at once — the exact thing the
        lock exists to prevent, reintroduced by the mechanism meant to make things more reliable.
        """
        if self._mid_transaction or self._session_state_held or not self._url:
            return False
        import psycopg2

        try:
            self.conn = psycopg2.connect(self._url)
        except Exception:  # noqa: BLE001
            return False
        return True

    def execute(self, sql: str, params: tuple = ()) -> Any:
        try:
            return self._execute_once(sql, params)
        except self._dead_connection_errors() as exc:
            if not self._is_dead(exc):
                raise
            # A connection idle long enough gets closed by the server, and nothing tells the process.
            # Without this the instance is poisoned for as long as it lives: every request afterwards
            # fails in milliseconds, having attempted no round trip at all.
            if not self._reconnected():
                raise
        return self._execute_once(sql, params)

    def _execute_once(self, sql: str, params: tuple = ()) -> Any:
        cur = self.conn.cursor()
        # **No params argument at all when there are none**, and that is not a tidy-up.
        # psycopg2 treats a non-None `params` as a request to interpolate, so it reads `%` in the SQL
        # as a placeholder marker — and a literal percent is every `LIKE 'thing%'` ever written. With
        # `params=()` such a statement raises `IndexError: tuple index out of range` on Postgres and
        # runs fine on SQLite, whose driver never inspects the string.
        #
        # That divergence is the dangerous part rather than the crash: the suite runs on SQLite and
        # deployments run on Postgres, so the first `LIKE` anybody writes passes every test and fails
        # in production, with an IndexError that points nowhere near the SQL. Skipping the argument
        # makes psycopg2 skip the interpolation, so one statement means one thing on both engines.
        if params:
            cur.execute(self._adapt(sql), params)
        else:
            cur.execute(self._adapt(sql))
        # **Only a write marks the transaction, and only after it has actually run.**
        #
        # What the guard in `_reconnected` protects is uncommitted *writes*: reconnecting after one
        # would let a later statement commit without it. A read leaves nothing to lose, so it must
        # not set the flag — and the difference is not cosmetic. The path this whole change exists
        # for reads five tables in a row and never commits, because there is nothing to commit. If a
        # read marked the transaction, that caller would reconnect once and then be refused for the
        # life of the process: the fix would not fix the bug. A test pins exactly that.
        if not _is_read(sql):
            self._mid_transaction = True
        return cur

    def query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        cur = self.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def commit(self) -> None:
        self.conn.commit()
        self._mid_transaction = False

    def rollback(self) -> None:
        """Abandon the open transaction. Callers reached past this into `store.conn.rollback()`
        before it existed; going through here is what keeps the reconnect guard honest, because a
        connection left marked mid-transaction never reconnects again."""
        self.conn.rollback()
        self._mid_transaction = False

    def close(self) -> None:
        self.conn.close()
        self._mid_transaction = False

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
            # **No reconnect until this is released.** The lock belongs to the connection, not to a
            # transaction, so a new connection would not hold it — and a reconnect part-way through
            # would let a second instance apply the same files concurrently, which is the one thing
            # this lock exists to stop.
            self._session_state_held = True
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
                    self.rollback()
                    self.execute("SELECT pg_advisory_unlock(?)", (_MIGRATION_LOCK_KEY,))
                    self.commit()
                self._session_state_held = False
            raise
        if locked:
            # Success: release the lock and let an unexpected unlock failure SURFACE — silently holding the
            # lock would hang the next instance on pg_advisory_lock.
            self.execute("SELECT pg_advisory_unlock(?)", (_MIGRATION_LOCK_KEY,))
            self.commit()
            self._session_state_held = False
        return ran

    def _run_script(self, sql: str) -> None:
        """Run a multi-statement SQL script. SQLite needs executescript; psycopg2 runs a multi-
        statement string in one execute."""
        if self.dialect == "sqlite":
            self.conn.executescript(sql)
        else:
            self.conn.cursor().execute(sql)
