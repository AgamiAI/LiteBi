"""The end-to-end harness the safety corpus runs on: the transports, and the file-served path.

Two things live here and nothing else.

**The routes.** A route takes `(sql, profile)` and returns the parsed tool-edge body — the JSON a
caller actually receives. `in_process` calls the tool directly with the built-in executor injected;
`http` goes through `mcp_http.create_app()`'s authenticated `/mcp` over `TestClient`, which is the
transport the hosted deployment serves; `stdio` runs `python -m mcp_harness` as a real child
process, which is the transport a local client launches. They are shaped after the four-route
matrix in
`tests/test_ace035_no_enumeration.py` and deliberately copied rather than imported: a harness whose
transports can be re-pointed by an edit to another test file is not a harness, and that file's
routes are bound to its own model fixture.

**The file-served model path.** `AGAMI_ARTIFACTS_DIR` for the model and `DATASOURCE_URL__<PROFILE>`
for the warehouse — the two axes are orthogonal and this module wires the file/SQLite corner of
them. Both sides are derived from `safety.corpus.SCHEMA`, so the semantic model and the physical
tables cannot describe different columns; a governed vector that returns rows is returning the
rows this module seeded, against the model the gates read.

The model is written RICH rather than minimal — a declared relationship, an unreviewed metric, an
AI-written column description, a declared filter and a row estimate — because the governed vectors
assert that every receipt section is present, and a model that declares nothing produces sections
that are present but say nothing. The point of a receipt assertion is to reach the assembler's real
output, not its empty case.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

TESTS_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = TESTS_ROOT.parent
SRC = REPO_ROOT / "packages" / "agami-core" / "src"
for _path in (TESTS_ROOT, Path(__file__).resolve().parent, SRC):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import itdeps  # noqa: E402

# The HTTP transport's dependencies, declared HERE and at MODULE scope. They used to be three
# `pytest.importorskip` calls inside `route_http`, which is a call-time gate: with `mcp` or `PyJWT`
# absent — one edit to an install line away, both live in the `server` extra — every vector on every
# path raised `Skipped` as it ran. The session reported `53 passed, 297 skipped`, exited 0, and the
# collection sentinel was perfectly satisfied because the items had still been COLLECTED. At module
# scope the same absence removes the items instead, which is the only place a count can see it.
itdeps.importorfail("starlette", "mcp", "jwt", sentinel=itdeps.E2E_REQUIRED)

import execute_sql  # noqa: E402
import tools  # noqa: E402

from safety.corpus import DB_PATH_ENGINE, FILE_PATH_ENGINE, SCHEMA  # noqa: E402

PROFILE = "acme"
AREA = "sales"
# The engine the model DECLARES, and the one the warehouse actually is. They have to agree or the
# chokepoint refuses every vector with `engine_mismatch` before any gate under test runs. Both names
# come from the corpus, which is where the vectors' own `engines` pins are compared against them.
ENGINE = FILE_PATH_ENGINE
PG_ENGINE = DB_PATH_ENGINE

# The deployment ceiling the availability vectors are driven under, derived from the seed data so
# it cannot go stale: `orders` seeds three rows, so a ceiling one below that is over-run by the
# plain projection, and the cross join (three orders x two customers) over-runs it further.
LOW_ROW_CAP = len(SCHEMA["orders"]["rows"]) - 1

# A `TestClient` needs an issuer and a signing secret to mint the bearer the route carries. Both
# are fixture values for a throwaway in-process app; neither reaches a real deployment.
BASE_URL = "https://your-host.example.com"
SIGNING_SECRET = "x" * 40

# The seed data is written in the warehouse's own types; the model declares the semantic ones.
_MODEL_TYPE = {"INTEGER": "integer", "REAL": "decimal", "TEXT": "string"}

# The one declared filter, on `orders`. `{alias}` binds per table REFERENCE, so the same
# declaration reads `o.…` where the statement aliases and `orders.…` where it does not. It changes
# no statement — the injector that used to rewrite SQL is gone — it only gives the `tables` section
# something real to account for.
DECLARED_FILTER = "{alias}.customer_id IS NOT NULL"
ROW_ESTIMATE = 1200
ROWS_AS_OF = "2026-01-01T00:00:00Z"


def write_model(root: Path, engine: str = ENGINE) -> None:
    """Write the disk YAML for `SCHEMA` under `root` (a profile directory).

    `engine` is the storage type the model DECLARES. It is a parameter rather than the module
    constant because the DB path's warehouse is Postgres and the declared engine has to match the
    engine the credentials connect to — a model that still said SQLite would be refused with
    `engine_mismatch` before a single gate under test ran, and every vector would come back with the
    same wrong verdict.
    """
    import yaml

    tables = root / "subject_areas" / AREA / "tables"
    tables.mkdir(parents=True)
    (root / "subject_areas" / AREA / "metrics").mkdir(parents=True)

    (root / "datasource.yaml").write_text(
        yaml.safe_dump(
            {
                "datasource": "Shop",
                "version": 1,
                "storage_connections": [{"name": "c", "storage_type": engine}],
                "subject_areas": [f"subject_areas/{AREA}"],
            }
        )
    )
    (root / "subject_areas" / AREA / "subject_area.yaml").write_text(
        yaml.safe_dump(
            {
                "name": AREA,
                "tables": [
                    {"storage_connection": "c", "schema": "public", "table": name}
                    for name in SCHEMA
                ],
            }
        )
    )

    for name, spec in SCHEMA.items():
        columns = []
        for column, sqlite_type in spec["columns"]:
            declared: dict[str, object] = {"name": column, "type": _MODEL_TYPE[sqlite_type]}
            if column == "id":
                declared["primary_key"] = True
            if column in spec["sensitive"]:
                # A model FACT, and the reason it is declared here: a sensitive column is
                # projectable, so a vector reading one must come back `ok`. Declaring the flag is
                # what makes that a real assertion rather than one about a column nothing marked.
                declared["sensitive"] = True
            if column == "amount":
                # An AI-written meaning, so the `assumptions` section has something to carry.
                declared["description"] = "net revenue"
                declared["description_source"] = "ai_unvalidated"
            columns.append(declared)

        table: dict[str, object] = {
            "name": name,
            "schema": "public",
            "storage_connection": "c",
            "grain": ["id"],
            "description": name,
            "columns": columns,
        }
        if name == "orders":
            table["default_filters"] = [DECLARED_FILTER]
            table["performance_hints"] = {
                "estimated_row_count": ROW_ESTIMATE,
                "estimated_row_count_at": ROWS_AS_OF,
            }
        (tables / f"{name}.yaml").write_text(yaml.safe_dump(table))

    # An UNREVIEWED metric and an UNREVIEWED join: the two trust signals the receipt surfaces, and
    # what gives the `columns`, `joins` and `aggregates` sections real items to report on.
    (root / "subject_areas" / AREA / "metrics" / "revenue.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "revenue",
                "calculation": "sum of order amount",
                "bindings": {engine: "SUM(amount)"},
                "source_tables": ["orders"],
                "confidence": "proposed",
                "review_state": "unreviewed",
            }
        )
    )
    (root / "subject_areas" / AREA / "relationships.yaml").write_text(
        yaml.safe_dump(
            {
                "relationships": [
                    {
                        "from_table": "orders",
                        "from_column": "customer_id",
                        "to_table": "customers",
                        "to_column": "id",
                        "from_schema": "public",
                        "to_schema": "public",
                        "relationship": "many_to_one",
                        "confidence": "inferred",
                        "review_state": "unreviewed",
                    }
                ]
            }
        )
    )


def seed_warehouse(path: Path) -> None:
    """Create and seed the SQLite warehouse `SCHEMA` describes."""
    con = sqlite3.connect(path)
    try:
        for name, spec in SCHEMA.items():
            columns = ", ".join(
                f"{column} {sqlite_type}" for column, sqlite_type in spec["columns"]
            )
            con.execute(f"CREATE TABLE {name} ({columns})")
            placeholders = ", ".join("?" for _ in spec["columns"])
            con.executemany(f"INSERT INTO {name} VALUES ({placeholders})", spec["rows"])
        con.commit()
    finally:
        con.close()


def build_file_path(tmp_path: Path, monkeypatch) -> SimpleNamespace:
    """Wire the file-served model path and return where its two halves landed.

    Local, not hosted: `AGAMI_DB_URL` is removed, so the model is the one on disk and the gates
    read it. Setting it would flip `execute_sql._hosted()` and change what a missing model does,
    which is the DB-served path's own axis rather than a variation of this one.
    """
    artifacts = tmp_path / "artifacts"
    write_model(artifacts / PROFILE)

    warehouse = tmp_path / "warehouse.db"
    seed_warehouse(warehouse)

    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(artifacts))
    monkeypatch.setenv(f"DATASOURCE_URL__{PROFILE.upper()}", f"sqlite:///{warehouse}")
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE_URL)
    monkeypatch.setenv("AGAMI_SIGNING_SECRET", SIGNING_SECRET)
    # `AGAMI_PROFILE` is scrubbed with the rest because the stdio child inherits this environment
    # wholesale: `tools._resolve_profile` reads it, and a developer who happens to have it exported
    # would have the child answer for a profile no vector named.
    for name in (
        "AGAMI_DB_URL",
        "APP_DATABASE_URL",
        "AGAMI_ORG_ID",
        "AGAMI_PROFILE",
        "AGAMI_SQL_MAX_ROWS",
    ):
        monkeypatch.delenv(name, raising=False)
    # Not a per-session budget: only the availability vectors want a tightened bound and they set
    # their own, so a stall on a loaded runner cannot turn an expected verdict into a timeout.
    monkeypatch.delenv("AGAMI_SQL_TIMEOUT_S", raising=False)

    reset_injected_executor()
    return SimpleNamespace(artifacts=artifacts, warehouse=warehouse, profile=PROFILE)


def reset_injected_executor() -> None:
    """`tools._INJECTED_EXECUTOR` is a process global — both routes set it — so it must not leak
    between vectors."""
    tools.set_injected_executor(None)


# ---------------------------------------------------------------------------
# The DB-served model path, on a Postgres warehouse reached as the read-only role
# ---------------------------------------------------------------------------
#
# TWO axes change here and they are orthogonal, so they are named separately rather than folded into
# one "DB path" word:
#
#   * where the MODEL comes from — disk YAML (`build_file_path`) or the app database
#     (`build_db_path`, via `model_deploy.deploy_one` and read back by `model_store.load_datasource`);
#   * what the WAREHOUSE is — a SQLite file, or the Postgres container connected as `agami_ro`.
#
# They are moved together deliberately: the served deployment this corpus is about is the one that
# has both, and running the corpus under it is what proves the verdicts do not depend on either.
# What must NOT be folded in is the consequence: `AGAMI_DB_URL` is also what `execute_sql._hosted()`
# reads, so setting it changes fail-closed behaviour for `model_unavailable` by design. That class is
# asserted per path rather than held to the identical-verdicts claim.

# Matching `tests/test_postgres_timeout_integration.py`: host/port/user/db default to the compose
# fixture's values, and the password is the opt-in switch with no default, so no test password for a
# writing identity lives in the source.
PG_HOST = os.environ.get("AGAMI_IT_PG_HOST", "127.0.0.1")
PG_PORT = os.environ.get("AGAMI_IT_PG_PORT", "55432")
PG_USER = os.environ.get("AGAMI_IT_PG_USER", "agami_test")
PG_PASSWORD = os.environ.get("AGAMI_IT_PG_PASSWORD", "")

# The read-only role's own credentials. Unlike the owner's password above these DO default, because
# the role is created by `fixtures/postgres-readonly-grants.sql` with a value that only exists inside
# a throwaway local container — the same standing as the `agami_test_pw` already in the compose file.
# Overridable for a cluster that made the role by hand.
PG_RO_USER = os.environ.get("AGAMI_IT_PG_RO_USER", "agami_ro")
PG_RO_PASSWORD = os.environ.get("AGAMI_IT_PG_RO_PASSWORD", "agami_ro_pw")

# The database the grants fixture creates for the corpus. A constant, not an env var: the fixture
# creates it under this name, so a second spelling would only ever be wrong.
PG_DATABASE = "corpus"

# The switch that decides whether the DB-backed half runs at all. The same variable the existing
# Postgres integration tests use, so one password turns them all on.
PG_ENABLED = bool(PG_PASSWORD)

# The only hosts `seed_postgres` will issue `DROP TABLE` against. `AGAMI_IT_PG_HOST` is an ordinary
# environment variable, and the seeder drops and recreates `orders` and `customers` at whatever it
# points at — two of the most ordinary table names there are. Someone with that variable exported
# for another purpose, or a copy-pasted invocation carrying a real hostname, would have a test suite
# quietly destroy two tables on a server nobody meant to touch. The compose fixture is a container
# on this machine, so a loopback address is the whole of what this ever legitimately needs.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def pg_dsn(user: str, password: str) -> str:
    """The DSN for `user` against the corpus database."""
    return f"postgresql://{user}:{password}@{PG_HOST}:{PG_PORT}/{PG_DATABASE}"


def pg_readonly_dsn() -> str:
    """The DSN the SERVER is given: the least-privilege role, and nothing else."""
    return pg_dsn(PG_RO_USER, PG_RO_PASSWORD)


def seed_postgres() -> None:
    """Create and seed the Postgres warehouse `SCHEMA` describes, as the OWNER.

    Connected as the owner rather than the read-only role for the obvious reason and one that is
    easy to miss: this is also what exercises the `ALTER DEFAULT PRIVILEGES` line in the grants
    fixture. The tables are created after the grants ran, so the role can only read them if that
    line worked, and a fixture that seeded through some privileged back door would hide its absence.

    `SCHEMA`'s column types are written in SQLite's spelling and reused verbatim — `INTEGER`, `REAL`
    and `TEXT` are all Postgres types too, so the one declaration really does describe both
    warehouses rather than two that happen to line up.
    """
    import psycopg2

    # Refused rather than skipped: a run pointed at a non-loopback host has been misconfigured, and
    # the safe response to "I am about to DROP TABLE somewhere unexpected" is to stop, loudly.
    assert PG_HOST in LOOPBACK_HOSTS, (
        f"AGAMI_IT_PG_HOST is {PG_HOST!r}; this fixture drops and recreates tables and will only "
        f"do that against a local container ({', '.join(sorted(LOOPBACK_HOSTS))})"
    )

    conn = psycopg2.connect(pg_dsn(PG_USER, PG_PASSWORD))
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            for name, spec in SCHEMA.items():
                columns = ", ".join(f"{column} {type_}" for column, type_ in spec["columns"])
                cur.execute(f"DROP TABLE IF EXISTS {name}")
                cur.execute(f"CREATE TABLE {name} ({columns})")
                placeholders = ", ".join(["%s"] * len(spec["columns"]))
                cur.executemany(f"INSERT INTO {name} VALUES ({placeholders})", spec["rows"])
    finally:
        conn.close()


def build_db_path(tmp_path: Path, monkeypatch) -> SimpleNamespace:
    """Wire the DB-served model path against the Postgres warehouse, and return where it landed.

    The model is written to disk once, deployed into the app database, and then left where nothing
    reads it: `AGAMI_ARTIFACTS_DIR` points at an EMPTY directory for the run. So a model that failed
    to deploy does not quietly fall back to disk and pass — the disk has nothing to fall back to, and
    every vector would refuse with `model_unavailable` instead. The read-back below turns that from a
    consequence a reader has to trace into an assertion at the point of setup.
    """
    import model_deploy
    import model_store
    from store import Store
    from tools import resolved_org_id

    served = tmp_path / "artifacts-empty"
    served.mkdir()
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(served))
    monkeypatch.delenv("AGAMI_ORG_ID", raising=False)

    staging = tmp_path / "staging"
    write_model(staging / PROFILE, engine=PG_ENGINE)

    app_db_url = "sqlite://" + str(tmp_path / "app.db")
    store = Store.connect(app_db_url)
    try:
        store.run_migrations()
        model_deploy.deploy_one(store, PROFILE, staging / PROFILE)
        # The read-back the whole path rests on, made where a failure still names its cause. `org_id`
        # is the resolver both sides share: `deploy_one` stamps rows with `resolved_org_id()` and
        # `execute_sql._resolve_guard_model` reads them back with it, so a mismatch here would be a
        # served deployment that finds no model for any tenant.
        served_model = model_store.load_datasource(store, PROFILE, org_id=resolved_org_id())
        assert served_model is not None, "the model did not come back out of the app database"
    finally:
        store.close()

    monkeypatch.setenv("AGAMI_DB_URL", app_db_url)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    monkeypatch.setenv(f"DATASOURCE_URL__{PROFILE.upper()}", pg_readonly_dsn())
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE_URL)
    monkeypatch.setenv("AGAMI_SIGNING_SECRET", SIGNING_SECRET)
    for name in ("AGAMI_PROFILE", "AGAMI_SQL_MAX_ROWS", "AGAMI_SQL_TIMEOUT_S"):
        monkeypatch.delenv(name, raising=False)

    reset_injected_executor()
    return SimpleNamespace(
        app_db_url=app_db_url, artifacts=served, dsn=pg_readonly_dsn(), profile=PROFILE
    )


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------


def route_in_process(sql: str, profile: str = PROFILE) -> dict:
    """`execute_guarded` runs in THIS process and the gate's own object reaches the serializer."""
    tools.set_injected_executor(execute_sql.BUILTIN_EXECUTOR)
    return json.loads(tools.tool_execute_sql({"sql": sql, "datasource": profile}))


def stdio_child_env() -> dict:
    """The environment `route_stdio` hands its child, and the one line that decides WHICH CHECKOUT
    the child is.

    **`PYTHONPATH` is load-bearing and it was missing.** The parent reaches this branch's executor
    through the `sys.path` insertion at the top of this module; a child process inherits no such
    thing, so with a bare environment it resolved `mcp_harness` and `execute_sql` from whatever
    `agami-core` happened to be pip-installed. On the machine this was found on that was a DIFFERENT
    worktree's checkout — so "one verdict across transports" was comparing this branch's HTTP answer
    against another branch's stdio answer. A divergence introduced here would have been invisible,
    and an unrelated branch's regression could have failed this suite. The child now reads the same
    source tree the parent imported, by construction.

    A function rather than a literal inside the call because
    `test_suite_integrity.py` asserts on it: a guard nobody can inspect is a guard nobody can watch
    fail.
    """
    return {**os.environ, "PYTHONPATH": str(SRC)}


def route_stdio(sql: str, profile: str = PROFILE) -> dict:
    """The stdio transport, for real: `python -m mcp_harness` over JSON-RPC on stdin.

    A genuine child process, not an in-process shim — which is the point, because the model root,
    the warehouse DSN and the row ceiling all reach it the way they reach a deployed server, through
    the environment. `build_file_path` sets them with `monkeypatch.setenv`, so `os.environ` already
    carries them and the child inherits the same view the parent's HTTP app reads.

    A process per call is why the corpus does not run whole on this route; see
    `safety.corpus.STDIO_SUBSET`.
    """
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "execute_sql", "arguments": {"sql": sql, "datasource": profile}},
        },
    ]
    proc = subprocess.run(
        [sys.executable, "-m", "mcp_harness"],
        input="".join(json.dumps(message) + "\n" for message in messages),
        capture_output=True,
        text=True,
        timeout=180,
        env=stdio_child_env(),
    )
    replies = {
        message.get("id"): message
        for message in (json.loads(line) for line in proc.stdout.splitlines() if line.strip())
    }
    # `stderr` is the diagnostic channel, so it is what says WHY when the call never came back —
    # without it a child that died on import reads as a bare KeyError.
    assert 2 in replies, proc.stderr
    return json.loads(replies[2]["result"]["content"][0]["text"])


def route_http(sql: str, profile: str = PROFILE) -> dict:
    """The HTTP transport, for real: `TestClient` over `create_app()`'s authenticated `/mcp`.

    No dependency guard here: it is at module scope, where a missing transport removes the vectors
    rather than skipping them one at a time after they have been counted.
    """
    import mcp_http
    from oauth_server import issue_jwt
    from starlette.testclient import TestClient

    headers = {
        "Authorization": f"Bearer {issue_jwt('jordan@example.com')}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    with TestClient(mcp_http.create_app()) as client:
        init = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"},
                },
            },
        )
        session = init.headers.get("mcp-session-id")
        headers2 = {**headers, **({"mcp-session-id": session} if session else {})}
        client.post(
            "/mcp", headers=headers2, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        resp = client.post(
            "/mcp",
            headers=headers2,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "execute_sql", "arguments": {"sql": sql, "datasource": profile}},
            },
        )
    assert resp.status_code == 200, resp.text
    return json.loads(resp.json()["result"]["content"][0]["text"])


# The keys a tool-edge body carries ONLY when the statement ran and produced a result. A refusal
# that carried any of them would be handing back a partial answer under a status that says there is
# none — "refused, and the rows came back anyway" is the highest-consequence failure this corpus can
# catch, so the list lives here, next to `verdict`, and both the per-vector driver and the
# availability test read it rather than each keeping its own copy.
RESULT_KEYS = ("data", "columns", "rows", "row_count", "markdown", "units")


def _comparable_rows(body: dict) -> tuple | None:
    """The row payload, order-independent.

    Left out entirely before, with a fair rationale: pinning row ORDER would pin something no engine
    promised, and none of these statements carries an `ORDER BY` that makes the order total. But
    dropping the payload means a path returning DIFFERENT rows of the same shape — same columns,
    same count — compares equal, and "the two paths agree" is the entire claim two files make with
    this function. Sorting is what keeps the rationale and closes the hole: each row is serialized
    to one string and the strings are sorted, so the comparison is over the multiset of rows and
    over nothing else. `default=str` because a warehouse is free to hand back a `Decimal` or a date
    where the other hands back a float, and the shape of the comparison is what matters here, not
    the driver's Python type.
    """
    rows = body.get("rows")
    if rows is None:
        return None
    return tuple(sorted(json.dumps(row, default=str, sort_keys=True) for row in rows))


def verdict(body: dict) -> tuple:
    """The DECISION, stripped of everything a run is free to differ on.

    Two dimensions compare bodies with this — one transport against another, one model/warehouse
    path against another — and both want the same answer to the same question: did the chokepoint
    decide the same thing? So it is defined once here rather than twice in the files that ask.

    `audit_id` is per call (two runs are two executions and each writes its own row), `execution_ms`
    is a clock, and `sql` and `markdown` are echoes. What must not differ is the status, the rule and
    reason when refused, and the answer itself when not — the columns, the count, and the rows as an
    unordered multiset (see `_comparable_rows`).
    """
    refusal = body.get("refusal") or {}
    return (
        body["status"],
        refusal.get("rule"),
        refusal.get("reason"),
        tuple(body["columns"]) if "columns" in body else None,
        body.get("row_count"),
        _comparable_rows(body),
    )
