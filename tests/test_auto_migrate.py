"""The server applies pending migrations on first DB open (idempotent, locked, fail-closed).

Covers the read helper (`Store.run_migrations`: the Postgres advisory-lock bracketing, idempotency, and
fail-closed-on-error) and the startup wiring (`mcp_http` lifespan applies them before serving + propagates).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("starlette")
pytest.importorskip("mcp")
pytest.importorskip("argon2")

PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import mcp_http  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402
from store import Store  # noqa: E402

BASE = "https://your-host.example.com"
SECRET = "x" * 40
ADMIN_USER = "admin@example.com"
ADMIN_PW = "admin-password-localtest"


def _write_migration(d: Path, name: str, sql: str) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(sql)


# --- run_migrations: idempotency + fail-closed (real SQLite) ------------------


def test_run_migrations_applies_then_is_idempotent(tmp_path):
    mig = tmp_path / "m"
    _write_migration(mig, "001_demo.sql", "CREATE TABLE demo_x (a INTEGER);")
    s = Store.connect("sqlite://" + str(tmp_path / "db.sqlite"))
    first = s.run_migrations(mig)
    second = s.run_migrations(mig)  # nothing pending now
    rows = s.query("SELECT id FROM schema_migrations")
    s.close()
    assert first == ["001_demo.sql"]
    assert second == []  # idempotent — a re-run applies nothing
    assert [r["id"] for r in rows] == ["001_demo.sql"]


def test_failing_migration_raises_and_is_not_recorded(tmp_path):
    import sqlite3

    mig = tmp_path / "m"
    _write_migration(mig, "001_bad.sql", "CREATE TABLE oops (")  # invalid SQL
    s = Store.connect("sqlite://" + str(tmp_path / "db.sqlite"))
    with pytest.raises(sqlite3.OperationalError):  # the raise comes from APPLYING the bad migration
        s.run_migrations(mig)
    applied = {r["id"] for r in s.query("SELECT id FROM schema_migrations")}
    leaked = s.query("SELECT name FROM sqlite_master WHERE type='table' AND name='oops'")
    s.close()
    assert "001_bad.sql" not in applied  # fail-closed: a failed migration is never recorded
    assert not leaked  # and no partial DDL leaked from the half-run script


def test_missing_core_migrations_root_raises(tmp_path):
    # A missing core root globs to NOTHING, so the server would boot on an empty schema with no error
    # at all. Fail loudly instead — this is the silent-empty-schema mode the in-package path removes.
    s = Store.connect("sqlite://" + str(tmp_path / "db.sqlite"))
    with pytest.raises(ValueError, match="core migration root is not a directory"):
        s.run_migrations(tmp_path / "does_not_exist")
    tables = {r["name"] for r in s.query("SELECT name FROM sqlite_master WHERE type='table'")}
    s.close()
    assert "schema_migrations" not in tables  # guard runs before any DDL


def test_default_core_migrations_root_lives_inside_the_package(tmp_path):
    # The default root must resolve NEXT TO store.py so it works identically from an installed wheel
    # and from a checkout. Resolving out of the tree (e.g. parents[3]) still passes in a checkout but
    # silently yields nothing once installed — exactly the failure this pins down.
    import store as store_mod

    pkg_dir = Path(store_mod.__file__).resolve().parent
    assert store_mod.MIGRATIONS_DIR.is_relative_to(pkg_dir)  # in the package, not the repo tree
    assert list(store_mod.MIGRATIONS_DIR.glob("*.sql"))  # and it actually ships migrations


# --- the advisory lock (dialect-branch, no real Postgres needed) -------------


class _Conn:
    """Stub psycopg2 connection — records whether the failed-migration rollback ran."""

    def __init__(self) -> None:
        self.rolled_back = False

    def rollback(self) -> None:
        self.rolled_back = True


class _Recorder:
    """A minimal stand-in for Store that records the SQL `run_migrations` issues, so we can assert the
    advisory-lock bracketing per dialect (and the error-path cleanup) without a live Postgres."""

    def __init__(self, dialect: str, *, fail_on_script: bool = False) -> None:
        self.dialect = dialect
        self.sql: list[str] = []
        self.conn = _Conn()
        self._fail = fail_on_script

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.sql.append(sql)

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        self.sql.append(sql)
        return []  # nothing applied yet

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        """`run_migrations` rolls back through `Store` rather than reaching into `store.conn`.

        Changed when the reconnect guard landed: `Store.rollback` is what clears the flag saying an
        uncommitted write is outstanding, and a rollback that bypassed it would leave the connection
        marked mid-transaction forever, so it could never reconnect again.
        """
        self.conn.rollback()

    def _run_script(self, sql: str) -> None:
        if self._fail:
            raise RuntimeError("bad migration")


def test_postgres_brackets_the_apply_with_a_session_advisory_lock(tmp_path):
    mig = tmp_path / "m"
    _write_migration(mig, "001_demo.sql", "CREATE TABLE demo_x (a INTEGER);")
    rec = _Recorder("postgres")
    Store.run_migrations(rec, mig)  # call the unbound method with our recorder as self
    # SESSION lock (survives the per-migration commits), not the xact variant — taken first, freed last.
    assert rec.sql[0] == "SELECT pg_advisory_lock(?)"
    assert "pg_advisory_xact_lock" not in " | ".join(rec.sql)
    assert rec.sql[-1] == "SELECT pg_advisory_unlock(?)"
    assert "pg_advisory_unlock" not in " | ".join(rec.sql[:-1])  # unlock only at the very end
    assert not rec.conn.rolled_back  # success path doesn't roll back (only the error path does)


def test_sqlite_takes_no_advisory_lock(tmp_path):
    mig = tmp_path / "m"
    _write_migration(mig, "001_demo.sql", "CREATE TABLE demo_x (a INTEGER);")
    rec = _Recorder("sqlite")
    Store.run_migrations(rec, mig)
    assert not any("pg_advisory" in s for s in rec.sql)  # single-writer — no lock
    assert not rec.conn.rolled_back  # sqlite skips the whole locked-cleanup block


def test_postgres_error_path_rolls_back_then_unlocks_without_masking(tmp_path):
    # A failing migration must propagate the REAL error (not a masking "transaction aborted"), roll the
    # aborted txn back, and still release the lock — see the run_migrations finally.
    mig = tmp_path / "m"
    _write_migration(mig, "001_demo.sql", "CREATE TABLE demo_x (a INTEGER);")
    rec = _Recorder("postgres", fail_on_script=True)
    with pytest.raises(RuntimeError, match="bad migration"):  # original error, not masked
        Store.run_migrations(rec, mig)
    assert rec.conn.rolled_back  # cleared the aborted state before unlocking
    assert rec.sql[-1] == "SELECT pg_advisory_unlock(?)"  # lock still released on the error path


# --- the startup wiring (lifespan applies on open, fail-closed) --------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    url = "sqlite://" + str(tmp_path / "startup.db")  # FRESH, deliberately NOT pre-migrated
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("AGAMI_DB_URL", url)
    monkeypatch.setenv("AGAMI_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("AGAMI_ADMIN_USERNAME", ADMIN_USER)
    monkeypatch.setenv("AGAMI_ADMIN_PASSWORD", ADMIN_PW)
    for v in ("AGAMI_OIDC_GOOGLE_CLIENT_ID", "AGAMI_OIDC_GOOGLE_CLIENT_SECRET"):
        monkeypatch.delenv(v, raising=False)
    return url


def test_startup_applies_pending_migrations(env):
    # Entering the TestClient runs the lifespan = the server's startup. The DB starts EMPTY; after startup
    # the real migrations must be applied (the footgun fix — no manual migrate step).
    with TestClient(mcp_http.build_app()):
        pass
    s = Store.connect(env)
    applied = s.query("SELECT id FROM schema_migrations")
    has_tool_calls = s.query(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='tool_calls'"
    )
    s.close()
    assert applied, "startup applied no migrations"  # at least one ran
    assert has_tool_calls, "a migrated table is missing after startup"


def test_startup_is_fail_closed_on_migration_error(env, monkeypatch):
    # A failing migration must abort startup, not be swallowed — the lifespan propagates the error.
    def _boom(self, migrations_dir=None):
        raise RuntimeError("migration blew up")

    monkeypatch.setattr(Store, "run_migrations", _boom)
    with pytest.raises(RuntimeError, match="migration blew up"):
        with TestClient(mcp_http.build_app()):
            pass


# --- migration overlays (extra roots layered on top of migrations/core) ------


def test_no_overlay_keeps_bare_core_ids(tmp_path):
    # Default path (no overlay) is byte-identical to today: core ids are bare filenames.
    core = tmp_path / "core"
    _write_migration(core, "001_core.sql", "CREATE TABLE core_a (a INTEGER);")
    s = Store.connect("sqlite://" + str(tmp_path / "db.sqlite"))
    ran = s.run_migrations(core, overlay_dirs=[])
    s.close()
    assert ran == ["001_core.sql"]  # no namespace prefix on core


def test_overlay_applies_core_then_overlay_and_is_idempotent(tmp_path):
    core = tmp_path / "core"
    ov = tmp_path / "ov_a"
    _write_migration(core, "001_core.sql", "CREATE TABLE core_b (a INTEGER);")
    _write_migration(ov, "001_a.sql", "CREATE TABLE t_a (b INTEGER);")
    s = Store.connect("sqlite://" + str(tmp_path / "db.sqlite"))
    first = s.run_migrations(core, overlay_dirs=[ov])
    second = s.run_migrations(core, overlay_dirs=[ov])  # nothing pending now
    ids = {r["id"] for r in s.query("SELECT id FROM schema_migrations")}
    s.close()
    assert first == ["001_core.sql", "ov_a:001_a.sql"]  # core first, then overlay
    assert second == []  # idempotent
    assert {"001_core.sql", "ov_a:001_a.sql"} <= ids


def test_same_filename_in_core_and_overlay_does_not_collide(tmp_path):
    # Core and an overlay both ship 001_init.sql — namespaced overlay ids keep them distinct on the pk.
    core = tmp_path / "core"
    ov = tmp_path / "ov_b"
    _write_migration(core, "001_init.sql", "CREATE TABLE core_init (a INTEGER);")
    _write_migration(ov, "001_init.sql", "CREATE TABLE ov_init (b INTEGER);")
    s = Store.connect("sqlite://" + str(tmp_path / "db.sqlite"))
    ran = s.run_migrations(core, overlay_dirs=[ov])
    tables = {r["name"] for r in s.query("SELECT name FROM sqlite_master WHERE type='table'")}
    s.close()
    assert ran == ["001_init.sql", "ov_b:001_init.sql"]  # both applied, distinct ids
    assert {"core_init", "ov_init"} <= tables  # both DDLs actually ran (no skip-on-collision)


def test_overlay_added_after_core_resumes(tmp_path):
    # An overlay registered AFTER core was already migrated lands on the next run (resume-safe).
    core = tmp_path / "core"
    ov = tmp_path / "ov_c"
    _write_migration(core, "001_core.sql", "CREATE TABLE core_c (a INTEGER);")
    s = Store.connect("sqlite://" + str(tmp_path / "db.sqlite"))
    s.run_migrations(core, overlay_dirs=[])  # core only, first
    _write_migration(ov, "001_c.sql", "CREATE TABLE t_c (b INTEGER);")
    ran = s.run_migrations(core, overlay_dirs=[ov])  # core skipped, overlay is new
    s.close()
    assert ran == ["ov_c:001_c.sql"]


def test_overlays_apply_in_the_given_order(tmp_path):
    core = tmp_path / "core"
    ov1 = tmp_path / "ov_1"
    ov2 = tmp_path / "ov_2"
    _write_migration(core, "001_core.sql", "CREATE TABLE core_d (a INTEGER);")
    _write_migration(ov1, "001_one.sql", "CREATE TABLE one (a INTEGER);")
    _write_migration(ov2, "001_two.sql", "CREATE TABLE two (a INTEGER);")
    s = Store.connect("sqlite://" + str(tmp_path / "db.sqlite"))
    ran = s.run_migrations(core, overlay_dirs=[ov1, ov2])
    s.close()
    assert ran == ["001_core.sql", "ov_1:001_one.sql", "ov_2:001_two.sql"]  # core → ov1 → ov2


def test_register_migration_overlay_is_used_by_no_arg_run(tmp_path, monkeypatch):
    # The mcp_http lifespan calls run_migrations() with no args — a registered overlay must apply.
    import store as store_mod

    monkeypatch.setattr(store_mod, "_MIGRATION_OVERLAYS", [])  # isolate the module global
    core = tmp_path / "core"
    ov = tmp_path / "ov_reg"
    _write_migration(core, "001_core.sql", "CREATE TABLE core_e (a INTEGER);")
    _write_migration(ov, "001_reg.sql", "CREATE TABLE reg (a INTEGER);")
    store_mod.register_migration_overlay(ov)
    store_mod.register_migration_overlay(ov)  # dedup: registering twice is a no-op
    s = Store.connect("sqlite://" + str(tmp_path / "db.sqlite"))
    ran = s.run_migrations(core)  # overlay_dirs defaults to the registered list
    s.close()
    assert store_mod._MIGRATION_OVERLAYS == [ov.resolve()]  # deduped + normalized on registration
    assert ran == ["001_core.sql", "ov_reg:001_reg.sql"]  # the registered overlay applied


def test_duplicate_overlay_namespace_raises(tmp_path):
    # Two overlay roots with the SAME basename share a namespace → fail fast, no partial apply.
    core = tmp_path / "core"
    a_ov = tmp_path / "a" / "ov"
    b_ov = tmp_path / "b" / "ov"
    _write_migration(core, "001_core.sql", "CREATE TABLE core_f (a INTEGER);")
    _write_migration(a_ov, "001_a.sql", "CREATE TABLE t_a (a INTEGER);")
    _write_migration(b_ov, "001_b.sql", "CREATE TABLE t_b (a INTEGER);")
    s = Store.connect("sqlite://" + str(tmp_path / "db.sqlite"))
    with pytest.raises(ValueError, match="share a directory name"):
        s.run_migrations(core, overlay_dirs=[a_ov, b_ov])
    tables = {r["name"] for r in s.query("SELECT name FROM sqlite_master WHERE type='table'")}
    s.close()
    assert "t_a" not in tables and "t_b" not in tables  # nothing applied
    assert "schema_migrations" not in tables  # guard runs before the tracking table is created


def test_empty_overlay_namespace_raises(tmp_path):
    # An overlay root with an empty directory name (a filesystem root) would produce BARE ids that
    # collide with core — reject it before touching the filesystem.
    core = tmp_path / "core"
    _write_migration(core, "001_core.sql", "CREATE TABLE core_g (a INTEGER);")
    root_like = Path(tmp_path.anchor)  # .name == "" (filesystem root)
    assert root_like.name == ""  # precondition
    s = Store.connect("sqlite://" + str(tmp_path / "db.sqlite"))
    with pytest.raises(ValueError, match="empty directory name"):
        s.run_migrations(core, overlay_dirs=[root_like])
    s.close()


def test_nonexistent_overlay_root_raises(tmp_path):
    # A registered overlay that isn't a directory would silently apply nothing (glob yields empty);
    # fail fast instead so the misconfiguration is loud.
    core = tmp_path / "core"
    _write_migration(core, "001_core.sql", "CREATE TABLE core_h (a INTEGER);")
    s = Store.connect("sqlite://" + str(tmp_path / "db.sqlite"))
    with pytest.raises(ValueError, match="not a directory"):
        s.run_migrations(core, overlay_dirs=[tmp_path / "does_not_exist"])
    s.close()


def test_register_overlay_normalizes_path(tmp_path, monkeypatch):
    # Different spellings of the same directory dedupe on registration (normalized via resolve()),
    # so they don't slip through as duplicate roots the namespace guard would then reject.
    import store as store_mod

    monkeypatch.setattr(store_mod, "_MIGRATION_OVERLAYS", [])
    ov = tmp_path / "a" / "ov"
    _write_migration(ov, "001_x.sql", "CREATE TABLE t_x (a INTEGER);")
    alt = tmp_path / "a" / "ov" / ".." / "ov"  # different spelling, same dir after resolve()
    store_mod.register_migration_overlay(ov)
    store_mod.register_migration_overlay(alt)
    assert store_mod._MIGRATION_OVERLAYS == [ov.resolve()]  # collapsed to one
