"""Shared e2e fixtures for the F9 safety corpus (ACE-040): the two transport surfaces and the two
model paths, wired so the corpus asserts the same Envelope regardless of surface or path."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

import harness  # noqa: E402  (tests/e2e is on sys.path during collection)

BASE = "https://demo.example.com"


@pytest.fixture
def presence_auth(monkeypatch):
    """HTTP bearer-presence mode: PUBLIC_BASE_URL set, no signing secret → 'Bearer present' works.
    Harmless for the stdio surface (the subprocess inherits the env but doesn't gate on the bearer)."""
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    monkeypatch.delenv("AGAMI_SIGNING_SECRET", raising=False)


@pytest.fixture(params=["stdio", "http"])
def surface(request, presence_auth):
    """Parametrize a test across BOTH transports; yields the driver `(sql, datasource=, max_rows=)`."""
    return harness.SURFACES[request.param]


@pytest.fixture
def file_safety_env(tmp_path, monkeypatch):
    """The FILE-served model path: an on-disk model (AGAMI_ARTIFACTS_DIR) + a seeded SQLite datasource
    (via the DATASOURCE_URL__<PROFILE> env DSN, which both surfaces inherit). No AGAMI_DB_URL ⇒ local
    (not hosted), so the guards resolve the model from disk. Default (enforce) unscopable posture."""
    art = tmp_path / "art"
    (art / "acme").mkdir(parents=True)
    harness.write_disk_model(art / "acme")
    db = tmp_path / "shop.db"
    harness.seed_sqlite(db)

    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))
    monkeypatch.setenv("AGAMI_PROFILE", "acme")
    monkeypatch.setenv("DATASOURCE_URL__ACME", "sqlite:///" + str(db))
    monkeypatch.delenv(
        "AGAMI_SQL_UNSCOPABLE_POSTURE", raising=False
    )  # enforce (the shipped default)
    yield


# ── the DB-served model path (Postgres-in-Docker) — env-gated, skips without a reachable PG ────
# The corpus's "both paths" proof: the SAME cases run against a Postgres-SERVED model + a Postgres
# datasource, so file-served and DB-served can't silently diverge. Opt-in via AGAMI_IT_PG_PASSWORD
# (the integration-pg CI job / `docker compose -f tests/integration/docker-compose.yml up postgres`).
# NOTE: these fixtures DROP/CREATE the shared global role `agami_ro` and shared tables in the single
# `shop` DB, so they are SERIAL-ONLY — do not run this dir under pytest-xdist (`-n`) without per-worker
# role/table names, or workers would race. The current invocation is serial.
# The read-only role's password is DERIVED from the (test-only) PG password env, never a hardcoded
# literal — the fixture Postgres is an ephemeral localhost CI service, and the role-floor tests
# PRIVILEGES, not auth (the CI service uses trust auth, so this value is not verified anyway).
_RO_PASSWORD = "ro_" + os.environ.get("AGAMI_IT_PG_PASSWORD", "local")


def pg_super_creds() -> dict:
    """Superuser creds for the fixture Postgres (host/port/user/db default to the compose fixture)."""
    return {
        "host": os.environ.get("AGAMI_IT_PG_HOST", "127.0.0.1"),
        "port": int(os.environ.get("AGAMI_IT_PG_PORT", "55432")),
        "user": os.environ.get("AGAMI_IT_PG_USER", "agami_test"),
        "password": os.environ.get("AGAMI_IT_PG_PASSWORD", ""),
        "dbname": os.environ.get("AGAMI_IT_PG_DB", "shop"),
    }


@pytest.fixture
def pg_admin():
    """An autocommit superuser connection to the fixture Postgres. Normally SKIPS (never fails) when
    no AGAMI_IT_PG_PASSWORD is set or no Postgres is reachable, so the DB-free test job is unaffected.

    BUT when AGAMI_IT_PG_REQUIRED is set (the integration-pg CI job sets it), an unavailable DB FAILS
    instead of skips — this job carries the ONLY proof of the role-floor + file/db parity + DB-served
    model, and pytest exits 0 when everything skips, so a service race / env rename / driver hiccup
    would otherwise turn the F9 done-bar gate green while proving nothing."""
    psycopg2 = pytest.importorskip("psycopg2")
    # In the required job, a missing DB is a hard failure — an all-skip must NOT pass as green.
    unavailable = pytest.fail if os.environ.get("AGAMI_IT_PG_REQUIRED") else pytest.skip
    if not os.environ.get("AGAMI_IT_PG_PASSWORD"):
        unavailable("set AGAMI_IT_PG_PASSWORD to run the Postgres safety-corpus / role-floor tests")
    sc = pg_super_creds()
    try:
        conn = psycopg2.connect(connect_timeout=10, **sc)
    except Exception as exc:  # unreachable DB → skip locally, FAIL in the required CI job
        unavailable(f"no reachable Postgres ({exc})")
    conn.autocommit = True
    try:
        yield psycopg2, conn, sc
    finally:
        conn.close()


def _reset_ro_role(cur) -> None:
    """Drop the read-only role + any grants it holds, tolerating 'does not exist' (setup + teardown)."""
    for stmt in (
        "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM agami_ro",
        "REVOKE ALL ON SCHEMA public FROM agami_ro",
        "DROP OWNED BY agami_ro",
        "DROP ROLE IF EXISTS agami_ro",
    ):
        try:
            cur.execute(stmt)
        except Exception:  # role/grant may not exist yet (autocommit conn → no aborted-txn carry)
            pass


def create_ro_role(cur, dbname: str) -> None:
    """(Re)create the SELECT-only `agami_ro` role + grants — verbatim from readonly-grants.md."""
    _reset_ro_role(cur)
    cur.execute("CREATE ROLE agami_ro LOGIN PASSWORD %s", (_RO_PASSWORD,))
    cur.execute(f"GRANT CONNECT ON DATABASE {dbname} TO agami_ro")
    cur.execute("GRANT USAGE ON SCHEMA public TO agami_ro")
    cur.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO agami_ro")
    cur.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO agami_ro")


@pytest.fixture
def db_safety_env(pg_admin, tmp_path, monkeypatch):
    """The DB-served model path: the model is written to the app DB (hosted → model_store), the demo
    datasource tables live in the same Postgres, and the app connects to them as the read-only role."""
    psycopg2, conn, sc = pg_admin
    cur = conn.cursor()

    # 1) demo datasource tables in public (owner-seeded), from the single-sourced SCHEMA.
    from safety.corpus import SCHEMA

    for name, spec in SCHEMA.items():
        cur.execute(f"DROP TABLE IF EXISTS {name} CASCADE")
        pg_types = {"INTEGER": "integer", "REAL": "double precision", "TEXT": "text"}
        ddl = ", ".join(f"{c} {pg_types[t]}" for c, t in spec["columns"])
        cur.execute(f"CREATE TABLE {name} ({ddl})")
        placeholders = ", ".join("%s" for _ in spec["columns"])
        for row in spec["rows"]:
            cur.execute(f"INSERT INTO {name} VALUES ({placeholders})", row)

    # 2) the SELECT-only role the app connects to the datasource as.
    create_ro_role(cur, sc["dbname"])

    # 3) the DB-served model: migrate the app schema + write the org into it (the hosted model source).
    super_dsn = (
        f"postgresql://{sc['user']}:{sc['password']}@{sc['host']}:{sc['port']}/{sc['dbname']}"
    )
    harness.seed_db_model(super_dsn, ds="acme")

    ro_dsn = f"postgresql://agami_ro:{_RO_PASSWORD}@{sc['host']}:{sc['port']}/{sc['dbname']}"
    monkeypatch.setenv("AGAMI_DB_URL", super_dsn)  # hosted → model + audit from the app DB
    monkeypatch.setenv("DATASOURCE_URL__ACME", ro_dsn)  # datasource read as the read-only role
    monkeypatch.setenv("AGAMI_PROFILE", "acme")
    monkeypatch.setenv(
        "AGAMI_ARTIFACTS_DIR", str(tmp_path / "no_disk")
    )  # DB is the only model source
    monkeypatch.delenv("AGAMI_SQL_UNSCOPABLE_POSTURE", raising=False)
    try:
        yield
    finally:
        for name in SCHEMA:
            try:
                cur.execute(f"DROP TABLE IF EXISTS {name} CASCADE")
            except Exception:
                pass
        _reset_ro_role(cur)


@pytest.fixture
def pg_ro_conn(pg_admin):
    """A RAW connection AS the read-only role, plus one seeded table — for the role-floor test. This
    connection bypasses the app layer entirely (no tool_execute_sql / no app read-only gate), so a
    write reaching it is stopped by the DATABASE itself — the primary control, proven independent of
    the app gate."""
    psycopg2, conn, sc = pg_admin
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS agami_floor CASCADE")
    cur.execute("CREATE TABLE agami_floor (id integer, label text)")
    cur.execute("INSERT INTO agami_floor VALUES (1, 'a'), (2, 'b')")
    create_ro_role(cur, sc["dbname"])
    ro = psycopg2.connect(
        host=sc["host"],
        port=sc["port"],
        user="agami_ro",
        password=_RO_PASSWORD,
        dbname=sc["dbname"],
        connect_timeout=3,
    )
    try:
        yield psycopg2, ro
    finally:
        ro.close()
        try:
            cur.execute("DROP TABLE IF EXISTS agami_floor CASCADE")
        except Exception:
            pass
        _reset_ro_role(cur)
