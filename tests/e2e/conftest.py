"""Shared e2e fixtures for the F9 safety corpus (ACE-040): the two transport surfaces and the two
model paths, wired so the corpus asserts the same Envelope regardless of surface or path."""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

import harness  # noqa: E402  (tests/e2e is on sys.path during collection)

from safety.corpus import SCHEMA  # noqa: E402

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

    # 1) demo datasource tables in public (owner-seeded), from the single-sourced SCHEMA — through
    #    the shared per-engine seeder (ACE-082), so Postgres and every other engine build the
    #    physical fixture the same way and a DDL-spelling difference lives in ONE table.
    harness.seed_schema(
        lambda sql, params=None: cur.execute(sql, params), "PostgreSQL", drop_first=True
    )

    # 2) the SELECT-only role the app connects to the datasource as.
    create_ro_role(cur, sc["dbname"])

    # 3) the DB-served model: migrate the app schema + write the org into it (the hosted model source).
    super_dsn = (
        f"postgresql://{sc['user']}:{sc['password']}@{sc['host']}:{sc['port']}/{sc['dbname']}"
    )
    # The datasource this env reads is Postgres (DATASOURCE_URL__ACME below), so the model must
    # declare Postgres: the guard parses in the model's engine and refuses when that is not the
    # engine the credentials connect to.
    harness.seed_db_model(super_dsn, ds="acme", storage_type="PostgreSQL")

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


# ── ACE-082: the per-engine EXECUTION fixtures (real engines, containerized) ───────────────────
# ACE-079 proved the guard's VERDICT on all eleven engines, but with a spy executor and no
# database — the vetted statement was re-parsed by sqlglot, never accepted by an engine. These
# fixtures close that half: the same corpus, through the same tool, against a real server.
#
# Each engine follows `pg_admin`'s contract: absent connection settings SKIP, unless
# AGAMI_IT_<ENGINE>_REQUIRED is set — in the CI job that owns an engine's evidence, an
# unavailable engine FAILS, because pytest exits 0 when everything skips and a silently-skipped
# job would claim per-engine coverage while proving nothing. "Unavailable" deliberately includes a
# MISSING DRIVER, not just an unreachable server: these drivers are supplied only by `--with` flags
# on the CI `uvx` line, so dropping one in a future edit is the likeliest way to empty a job, and
# `pytest.importorskip` would turn exactly that into a green run.
#
# Like the Postgres fixtures above, these DROP/CREATE shared table names in a shared database, so
# they are SERIAL-ONLY — do not run this dir under pytest-xdist (`-n`) without per-worker table
# names, or workers would race. The current invocation is serial.


def _engine_creds(engine: str, default_port: str) -> dict[str, str]:
    """AGAMI_IT_<ENGINE>_* connection settings. Defaults target the local containers; CI overrides."""
    e = engine.upper()
    return {
        "host": os.environ.get(f"AGAMI_IT_{e}_HOST", "127.0.0.1"),
        "port": os.environ.get(f"AGAMI_IT_{e}_PORT", default_port),
        "user": os.environ.get(f"AGAMI_IT_{e}_USER", ""),
        "password": os.environ.get(f"AGAMI_IT_{e}_PASSWORD", ""),
        "database": os.environ.get(f"AGAMI_IT_{e}_DB", "shop"),
    }


def _unavailable(engine: str) -> Callable[[str], NoReturn]:
    """`pytest.fail` in the job that owns this engine's evidence, else `pytest.skip`."""
    return pytest.fail if os.environ.get(f"AGAMI_IT_{engine.upper()}_REQUIRED") else pytest.skip


def _require_driver(engine: str, module: str):
    """Import an engine's driver, honouring the REQUIRED contract.

    Deliberately not `pytest.importorskip`: that skips unconditionally, which in the job that owns
    this engine's evidence would report success for a run that executed nothing.
    """
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        _unavailable(engine)(f"{module} not installed, so {engine} proves nothing ({exc})")


def _write_credentials(art: Path, profile: str, fields: dict[str, str], monkeypatch) -> None:
    """Write <artifacts_dir>/local/credentials for engines with no DSN scheme (SQLServer, DuckDB).

    Both surfaces must read the SAME file: the stdio subprocess re-resolves the path from
    AGAMI_ARTIFACTS_DIR in `execute_sql.main()`, but the in-process HTTP surface captured
    CREDENTIALS_PATH at import — so the module constant is repointed here too. That is a
    test-ordering artifact, not a product gap: a deployed server has its credentials file
    before it boots.
    """
    import execute_sql

    local = art / "local"
    local.mkdir(parents=True, exist_ok=True)
    path = local / "credentials"
    body = f"[{profile}]\n" + "".join(f"{k} = {v}\n" for k, v in fields.items())
    path.write_text(body)
    path.chmod(0o600)  # the executor refuses a more permissive credentials file
    monkeypatch.setattr(execute_sql, "CREDENTIALS_PATH", path)


def _engine_model_env(art: Path, engine: str, monkeypatch) -> None:
    """File-served model declaring the engine's storage type — what the guard parses every statement
    in, and what the executor is then reconciled against. The storage type is read from the engine's
    `EngineFixture` rather than restated, so the model and the physical fixture cannot disagree."""
    (art / "acme").mkdir(parents=True, exist_ok=True)
    harness.write_disk_model(art / "acme", storage_type=harness.ENGINE_FIXTURES[engine].storage_type)
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))
    monkeypatch.setenv("AGAMI_PROFILE", "acme")
    monkeypatch.delenv("AGAMI_SQL_UNSCOPABLE_POSTURE", raising=False)


def _drop_corpus_tables(cur) -> None:
    """Best-effort teardown. Tolerates a table that was never created — the next seed re-runs
    DROP ... IF EXISTS first, so a failure here cannot leave stale rows behind."""
    for name in SCHEMA:
        try:
            cur.execute(f"DROP TABLE IF EXISTS {name}")
        except Exception:
            pass


@pytest.fixture
def mysql_safety_env(tmp_path, monkeypatch):
    """MySQL 8 — the engine that matters most here: backticks are MySQL's NATIVE identifier quote,
    so this is where a wrongly-dialected parse and a real execution can genuinely disagree."""
    unavailable = _unavailable("MySQL")
    pymysql = _require_driver("MySQL", "pymysql")
    c = _engine_creds("MySQL", "53307")
    if not c["password"]:
        unavailable("set AGAMI_IT_MYSQL_PASSWORD to run the MySQL safety corpus")
    try:
        conn = pymysql.connect(
            host=c["host"],
            port=int(c["port"]),
            user=c["user"],
            password=c["password"],
            database=c["database"],
            autocommit=True,
            connect_timeout=10,
        )
    except Exception as exc:
        unavailable(f"no reachable MySQL ({exc})")
    # Everything after the connect lives in the try, so a failure while seeding closes the
    # connection instead of leaking one per test.
    try:
        cur = conn.cursor()
        harness.seed_schema(
            lambda sql, params=None: cur.execute(sql, params), "MySQL", drop_first=True
        )
        art = tmp_path / "art"
        _engine_model_env(art, "MySQL", monkeypatch)
        monkeypatch.setenv(
            "DATASOURCE_URL__ACME",
            f"mysql://{c['user']}:{c['password']}@{c['host']}:{c['port']}/{c['database']}",
        )
        yield
    finally:
        _drop_corpus_tables(conn.cursor())
        conn.close()


@pytest.fixture
def sqlserver_safety_env(tmp_path, monkeypatch):
    """SQL Server 2022 — the only bracket-quoting engine, and the worst measured divergence:
    under a generic parse `SELECT TOP n [col] FROM [tbl]` resolves to no tables and `TOP` as a
    column, so every scope gate finds nothing to object to."""
    unavailable = _unavailable("SQLServer")
    pymssql = _require_driver("SQLServer", "pymssql")
    c = _engine_creds("SQLServer", "11433")
    if not c["password"]:
        unavailable("set AGAMI_IT_SQLSERVER_PASSWORD to run the SQL Server safety corpus")
    connect = dict(
        server=c["host"],
        port=int(c["port"]),
        user=c["user"],
        password=c["password"],
        autocommit=True,
        login_timeout=15,
    )
    try:
        boot = pymssql.connect(**connect)
    except Exception as exc:
        unavailable(f"no reachable SQL Server ({exc})")
    try:  # the fixture database may not exist yet; create it before connecting into it
        boot.cursor().execute(
            f"IF DB_ID('{c['database']}') IS NULL CREATE DATABASE [{c['database']}]"
        )
    finally:
        boot.close()

    conn = pymssql.connect(database=c["database"], **connect)
    try:
        cur = conn.cursor()
        harness.seed_schema(
            lambda sql, params=None: cur.execute(sql, params), "SQLServer", drop_first=True
        )
        art = tmp_path / "art"
        _engine_model_env(art, "SQLServer", monkeypatch)
        # SQL Server has no DSN scheme (`_env_datasource_dsn` handles postgres/mysql/sqlite/…), so
        # the per-field credentials file is the supported channel. Clear the env DSN or it would win.
        monkeypatch.delenv("DATASOURCE_URL__ACME", raising=False)
        _write_credentials(
            art,
            "acme",
            {
                "type": harness.ENGINE_FIXTURES["SQLServer"].credential_type,
                "host": c["host"],
                "port": c["port"],
                "user": c["user"],
                "password": c["password"],
                "database": c["database"],
            },
            monkeypatch,
        )
        yield
    finally:
        _drop_corpus_tables(conn.cursor())
        conn.close()


@pytest.fixture
def duckdb_safety_env(tmp_path, monkeypatch):
    """DuckDB in-process — no server, no container. Included because it is cheap and because the
    corpus previously claimed DuckDB coverage it did not have.

    Its only availability risk is the driver, so it goes through the same REQUIRED contract: the
    `integration-duckdb` job is this engine's ONLY evidence, and a skipped run there would be a
    green job that proved nothing."""
    _require_driver("DuckDB", "duckdb")
    db = tmp_path / "shop.duckdb"
    harness.seed_duckdb(db)
    art = tmp_path / "art"
    _engine_model_env(art, "DuckDB", monkeypatch)
    # DuckDB has no DSN scheme either — same per-field credentials channel as SQL Server.
    monkeypatch.delenv("DATASOURCE_URL__ACME", raising=False)
    _write_credentials(
        art,
        "acme",
        {"type": harness.ENGINE_FIXTURES["DuckDB"].credential_type, "path": str(db)},
        monkeypatch,
    )
    yield


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
