"""The enumeration sentinel: a refusal echoes, but never enumerates.

The locked rule, in two halves:

  * **Echo is allowed.** An identifier the caller put in its own statement may be named back to it.
    That discloses nothing it did not already send, and it is what makes a refusal actionable —
    "column(s) not in the semantic model: ref_no" tells the caller which of its own names to fix.
  * **Enumeration is not.** The declared surface must never be listed. A refusal that names the
    alternatives ("declared tables here are: orders, customers, …") turns every refusal into a
    schema-listing endpoint, reachable by anyone who can send one deliberately-wrong statement —
    which is the recon surface a later slice exists to close.

**This file is lock-in, not a fix.** All five gates are echo-only today; that was verified when they
were converted, and it is asserted here so a later reword cannot quietly turn a refusal into a
listing. Unlike the rest of this spec's tests, the property this one pins is expected to hold on
`446cc20` (the pre-conversion base) as well — and it does. It was checked by running this file's
model, canaries, vectors and scanner against a materialized `446cc20` tree, driving all five rules
through all four routes below and scanning the 20 refusal bodies the base produced: all clean. (The
file cannot be *collected* at that commit — `guardrail` does not exist there and the tool edge
speaks `{"error": {kind, remediation}}` rather than the Envelope — so the base run drops only the
two shape assertions, `status == "refused"` and `refusal.rule == …`, and keeps the scanner
verbatim.) A future failure here is therefore a live disclosure bug, not a conversion regression.

**What this file covers.** Every field of the serialized tool-edge body, for the five refusal rules
and for a `failed` — across both surfaces and both execution paths. `failure.message` is in scope
because it is part of that body: it was the one field the scanner never saw, and it is where a
PostgreSQL `HINT: Perhaps you meant to reference the column "orders.internal_ref".` arrives, which
is enumeration reaching the caller through the operational channel rather than the guardrail one.

**What it does NOT cover, in two different senses.**

*Not yet fixed.* Driver text is relayed to `failure.message` unsanitized, so a driver that
volunteers a declared name puts it in front of the caller. That is measured here rather than
assumed: `test_a_driver_hint_enumerates_the_model_until_ace039_lands` drives exactly that shape and
is `xfail(strict=True)`. Sanitizing driver text is the error-hardening slice's job (ACE-039), not
this one's — and the strict marker means the day it lands, that test flips green and says so.
Everything this slice *does* author into `failure.message` is value-free: the guarded path's
catch-all message is a fixed string, and the forked path no longer relays unstructured child stderr.

*Not fixable here.* The table- and column-scope details confirm "this identifier is not in the
model", which is a one-bit membership oracle per probed identifier: a caller willing to send N
statements learns N bits about the declared surface. That is inherent to the existing design, is
carried across verbatim by this work, and is deliberately unchanged here. It is prior art for the
recon slice (`guardrail.RULE_RECON` is already declared and unassigned for it). A reader who takes
"no enumeration" to mean "no inference" has read this file too generously: it bounds what a refusal
*states*, not what a persistent caller can *infer*.

The model is defined in this file rather than imported, for the same reason
`test_ace035_gate_verdict_parity.py` copies its fixtures: a sentinel whose canary can be
re-pointed by an edit to another test file is not a sentinel.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402
import guardrail  # noqa: E402
import tools  # noqa: E402
from store import Store  # noqa: E402

BASE_URL = "https://your-host.example.com"
SIGNING_SECRET = "x" * 40
PROFILE = "acme"

# Every name the model below declares: the datasource, the subject area, both tables and every
# column. A refusal may repeat one of these ONLY if the caller's own statement contained it.
DECLARED_NAMES = (
    "Shop", "sales",
    "orders", "id", "amount", "internal_ref",
    "ledger_archive", "entry_code",
)

# The canaries: declared, and referenced by NO statement any test in this file sends. They cannot
# reach a refusal by being echoed, so if one appears there it came from the model — which is the
# definition of an enumeration. `test_the_canaries_are_real` keeps them honest.
CANARIES = ("ledger_archive", "internal_ref", "entry_code")


@pytest.fixture(autouse=True)
def _isolate():
    """`_INJECTED_EXECUTOR` is a process global — `create_app()` sets it, and the in-process route
    sets it — so it must not leak between tests."""
    execute_sql._max_rows_override.set(None)
    tools.set_injected_executor(None)
    yield
    execute_sql._max_rows_override.set(None)
    tools.set_injected_executor(None)


def _write_model(root: Path) -> None:
    """A two-table model. Shaped like S5's disk-model fixture, plus the canaries.

    `ledger_archive` (a whole table, with its own `entry_code` column) and `orders.internal_ref` are
    declared and never queried. They are what a "helpful" refusal would reach for — "did you mean
    one of these?" — and they are exactly what must never appear.

    `orders.amount` is declared here and deliberately absent from the warehouse the fixture builds,
    which is how the `failed` vector reaches a real database and is rejected by it. Every other
    statement in this file is refused before execution, so for those the warehouse is never touched.
    """
    import yaml

    (root / "subject_areas" / "sales" / "tables").mkdir(parents=True)
    (root / "datasource.yaml").write_text(
        yaml.safe_dump({"datasource": "Shop", "version": 1,
                        "subject_areas": ["subject_areas/sales"]})
    )
    (root / "subject_areas" / "sales" / "subject_area.yaml").write_text(
        yaml.safe_dump({"name": "sales", "tables": [
            {"storage_connection": "c", "schema": "public", "table": "orders"},
            {"storage_connection": "c", "schema": "public", "table": "ledger_archive"}]})
    )
    (root / "subject_areas" / "sales" / "tables" / "orders.yaml").write_text(
        yaml.safe_dump({
            "name": "orders", "schema": "public", "storage_connection": "c", "grain": ["id"],
            "description": "orders",
            "columns": [
                {"name": "id", "type": "integer", "primary_key": True},
                {"name": "amount", "type": "integer"},
                {"name": "internal_ref", "type": "string"},
            ],
        })
    )
    (root / "subject_areas" / "sales" / "tables" / "ledger_archive.yaml").write_text(
        yaml.safe_dump({
            "name": "ledger_archive", "schema": "public", "storage_connection": "c",
            "grain": ["id"], "description": "ledger archive",
            "columns": [
                {"name": "id", "type": "integer", "primary_key": True},
                {"name": "entry_code", "type": "string"},
            ],
        })
    )


@pytest.fixture
def declared(tmp_path, monkeypatch):
    """A resolvable model under profile `acme`, plus a real warehouse behind it.

    The four scope/safety rules run against the model. The warehouse exists for the `failed` vector
    alone: it has `orders`, but only the `id` column, so a statement the gates all pass is rejected
    by the database itself — the operational channel, reached for real rather than by stubbing an
    executor. Its table carries none of the canaries, so anything a canary-scan finds in a failure
    body came from the model rather than from the database.
    """
    artifacts = tmp_path / "artifacts"
    _write_model(artifacts / PROFILE)

    warehouse = tmp_path / "warehouse.db"
    con = sqlite3.connect(warehouse)
    con.execute("CREATE TABLE orders (id INTEGER)")
    con.commit()
    con.close()

    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(artifacts))
    monkeypatch.setenv("DATASOURCE_URL__ACME", f"sqlite:///{warehouse}")
    # Local, not hosted: the disk model is the one the gates use. (`model_unavailable` needs the
    # hosted signal and gets its own fixture.)
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    monkeypatch.delenv("AGAMI_ORG_ID", raising=False)
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE_URL)
    monkeypatch.setenv("AGAMI_SIGNING_SECRET", SIGNING_SECRET)
    return SimpleNamespace(artifacts=artifacts, warehouse=warehouse)


@pytest.fixture
def unavailable(tmp_path, monkeypatch):
    """Hosted, with NO model resolvable for the requested profile — but another datasource sitting
    right next to it on disk.

    That neighbour is the canary for this rule. There is no declared surface to enumerate when no
    model resolved, so the disclosure a `model_unavailable` refusal could make is a different one:
    "no model for acme — the datasources I did find are: ledger_archive". Naming the profile
    directory after the canary makes that leak a failing assertion rather than a code review.
    """
    artifacts = tmp_path / "artifacts"
    _write_model(artifacts / "ledger_archive")  # a neighbour datasource; `acme` has nothing
    app_db_path = tmp_path / "app.db"
    app_db = "sqlite://" + str(app_db_path)
    store = Store.connect(app_db)
    store.run_migrations()
    store.close()

    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(artifacts))
    monkeypatch.setenv("AGAMI_DB_URL", app_db)  # the hosted signal: fail closed, don't run unguarded
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    monkeypatch.delenv("AGAMI_ORG_ID", raising=False)
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE_URL)
    monkeypatch.setenv("AGAMI_SIGNING_SECRET", SIGNING_SECRET)
    return SimpleNamespace(artifacts=artifacts, app_db_path=app_db_path)


# ---------------------------------------------------------------------------
# The four routes — both surfaces, both execution paths
# ---------------------------------------------------------------------------


def _route_in_process(sql: str, profile: str = PROFILE) -> dict:
    """The in-process executor: `execute_guarded` runs in THIS process and the gate's own object
    reaches `_emit` unserialized."""
    tools.set_injected_executor(execute_sql.BUILTIN_EXECUTOR)
    return json.loads(tools.tool_execute_sql({"sql": sql, "datasource": profile}))


def _route_fork(sql: str, profile: str = PROFILE) -> dict:
    """The subprocess fork: the child writes the refusal to stderr and the parent rebuilds it
    through `Refusal`. A different route to the same object, so it is worth its own column."""
    tools.set_injected_executor(None)
    return json.loads(tools.tool_execute_sql({"sql": sql, "datasource": profile}))


def _route_stdio(sql: str, profile: str = PROFILE) -> dict:
    """The stdio transport, for real: `python -m mcp_harness` over JSON-RPC on stdin."""
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "execute_sql", "arguments": {"sql": sql, "datasource": profile}}},
    ]
    proc = subprocess.run(
        [sys.executable, "-m", "mcp_harness"],
        input="".join(json.dumps(m) + "\n" for m in messages),
        capture_output=True, text=True, timeout=180, env={**os.environ},
    )
    replies = {
        m.get("id"): m
        for m in (json.loads(line) for line in proc.stdout.splitlines() if line.strip())
    }
    assert 2 in replies, proc.stderr
    return json.loads(replies[2]["result"]["content"][0]["text"])


def _route_http(sql: str, profile: str = PROFILE) -> dict:
    """The HTTP transport, for real: `TestClient` over `create_app()`'s authenticated `/mcp`."""
    pytest.importorskip("starlette")
    pytest.importorskip("mcp")
    pytest.importorskip("jwt")
    import mcp_http
    from oauth_server import issue_jwt
    from starlette.testclient import TestClient

    headers = {
        "Authorization": f"Bearer {issue_jwt('jordan@example.com')}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    with TestClient(mcp_http.create_app()) as client:
        init = client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "1"}}})
        session = init.headers.get("mcp-session-id")
        headers2 = {**headers, **({"mcp-session-id": session} if session else {})}
        client.post("/mcp", headers=headers2,
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        resp = client.post("/mcp", headers=headers2, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "execute_sql", "arguments": {"sql": sql, "datasource": profile}}})
    assert resp.status_code == 200, resp.text
    return json.loads(resp.json()["result"]["content"][0]["text"])


ROUTES = {
    "in_process": _route_in_process,
    "fork": _route_fork,
    "stdio": _route_stdio,
    "http": _route_http,
}


# ---------------------------------------------------------------------------
# The sentinel
# ---------------------------------------------------------------------------

# One statement per rule, chosen so the rule under test is the FIRST gate to fire:
#   read_only    — a denied keyword, refused at the tool edge before a profile is resolved
#   table_scope  — an undeclared table (runs before the star ban and the column gate)
#   select_star  — a declared table, projected with `*`
#   column_scope — a declared table, an undeclared column
# `audit_trail` and `ref_no` are the caller's own inventions, so echoing them back is legitimate;
# nothing the MODEL declares appears in any of them except `orders`.
VECTORS = (
    (guardrail.RULE_READ_ONLY, "DELETE FROM orders"),
    (guardrail.RULE_TABLE_SCOPE, "SELECT ref_no FROM audit_trail"),
    (guardrail.RULE_SELECT_STAR, "SELECT * FROM orders"),
    (guardrail.RULE_COLUMN_SCOPE, "SELECT ref_no FROM orders"),
)

UNAVAILABLE_SQL = "SELECT id FROM orders"

# The `failed` vector: `amount` is declared, so every gate passes it, and it is absent from the
# warehouse, so the database rejects it. This is the operational channel — `failure.message` rather
# than `refusal.detail` — and it was outside the sentinel's scope entirely until now, which mattered
# because `failure.message` is the field nothing in this repo sanitizes.
FAILED_SQL = "SELECT amount FROM orders"

_MATRIX = [(rule, sql, route) for rule, sql in VECTORS for route in ROUTES]
_MATRIX_IDS = [f"{rule}-{route}" for rule, _, route in _MATRIX]


def _mentions(text: str, name: str) -> bool:
    """Whole-word, case-insensitive. Substring matching would be unusable: the static prose says
    "every column must be named", which contains a declared column called `name` and means nothing
    of the sort."""
    return re.search(rf"\b{re.escape(name)}\b", text, re.IGNORECASE) is not None


def _assert_echo_only(body: dict, sql: str) -> None:
    """The whole rule, over the whole serialized tool-edge body — not just `detail`.

    The body is what a caller actually receives, so `refusal.detail`, `refusal.remediation`,
    `refusal.reason`, `refusal.rule` and every other key are all in scope at once. Scanning the
    serialized form is also why this cannot be satisfied by moving a leak from one field to another.
    """
    text = json.dumps(body)
    for canary in CANARIES:
        assert not _mentions(text, canary), f"canary {canary!r} leaked into: {text}"
    for name in DECLARED_NAMES:
        if _mentions(sql, name):
            continue  # the caller sent it; naming it back discloses nothing it did not already have
        assert not _mentions(text, name), f"declared name {name!r} leaked into: {text}"


@pytest.mark.parametrize(("rule", "sql", "route"), _MATRIX, ids=_MATRIX_IDS)
def test_no_declared_name_the_caller_did_not_send_reaches_a_refusal(declared, rule, sql, route):
    """Five rules — four here, `model_unavailable` below — across both surfaces and both execution
    paths.

    The fork column is not redundant with in-process: the child serializes the refusal to stderr and
    the parent rebuilds it, so a leak could be introduced by either half. (`read_only` is the one
    exception — the tool edge fast-fails it before any fork, so its two in-process-ish columns agree
    by construction; the child's own read-only refusal crossing the wire is covered by
    `tests/test_ace035_read_only_refusal.py::test_parent_reconstructs_the_child_refusal`.)
    """
    body = ROUTES[route](sql)

    assert body["status"] == "refused", body
    assert body["refusal"]["rule"] == rule, body
    _assert_echo_only(body, sql)


@pytest.mark.parametrize("route", list(ROUTES), ids=list(ROUTES))
def test_the_model_unavailable_refusal_lists_no_datasource_and_names_no_infrastructure(
    unavailable, route
):
    """The fifth rule. Two things it must not say, and they are different things.

    It must not enumerate what it DID find — the neighbouring datasource on disk. And it must not
    name where it looked: no filesystem path, no DSN, no hostname. The second is why both
    remediations on this rule are authored as static prose at their construction sites rather than
    assembled from the resolution attempt.

    Prior art, not duplicated here: `tests/test_ace051_fail_closed.py` pins the specific-value form
    of the infrastructure rule for the DB-source branch — no host, no password, no probed artifacts
    path — both in-process and one process boundary out on the wire. This asserts the general form,
    across all four routes.
    """
    body = ROUTES[route](UNAVAILABLE_SQL)

    assert body["status"] == "refused", body
    assert body["refusal"]["rule"] == guardrail.RULE_MODEL_UNAVAILABLE, body
    _assert_echo_only(body, UNAVAILABLE_SQL)
    _assert_no_infrastructure(body["refusal"], whole_body=json.dumps(body), env=unavailable)


@pytest.mark.parametrize("route", list(ROUTES), ids=list(ROUTES))
def test_a_failed_envelope_enumerates_nothing_either(declared, route):
    """A refusal is not the only body that reaches the caller, and `failure.message` is not scanned
    by anything else.

    A statement the gates all pass and the database then rejects produces the other channel — and
    that channel is the one this repo does not sanitize, so leaving it out of the sentinel meant the
    property was asserted where it was already true and unasserted where it was not. sqlite names
    only the column the caller sent, so this is green; the shape that is not is pinned below.
    """
    body = ROUTES[route](FAILED_SQL)

    assert body["status"] == "failed", body
    _assert_echo_only(body, FAILED_SQL)


# The PostgreSQL text a real deployment gets for the same statement, verbatim in shape. `amount` is
# declared and missing from the table; PG reports that and then volunteers the nearest column it
# does have, which here is a canary — a name the caller never sent and could not otherwise learn.
_PG_HINT_ERROR = (
    'Postgres execution error: column "amount" does not exist\n'
    "LINE 1: SELECT amount FROM orders\n"
    "               ^\n"
    'HINT:  Perhaps you meant to reference the column "orders.internal_ref".'
)


class _PostgresLikeExecutor:
    def execute(self, vetted_sql, creds, *, profile):
        raise execute_sql.ExecutorError(_PG_HINT_ERROR, code=5)


@pytest.mark.xfail(
    strict=True,
    reason="ACE-039 owns sanitizing driver text; until it lands, a driver HINT reaches the caller "
           "through failure.message. Measured rather than assumed — this flips green when it lands.",
)
def test_a_driver_hint_enumerates_the_model_until_ace039_lands(declared):
    """The known gap, driven rather than described.

    PostgreSQL routinely appends `HINT: Perhaps you meant to reference the column "…"` to an
    undefined-column error, and that hint names a column of the table — a declared name the caller
    did not send. It arrives on `failure.message`, which is relayed from the driver verbatim: the
    guardrail refuses to enumerate, and then the operational channel does it anyway.

    Deliberately NOT fixed here. Classifying and sanitizing driver text across ten engines is the
    error-hardening slice's whole job, and a partial regex in this slice would look like coverage
    while missing the engines nobody thought of. So the vector stays, marked `strict` so it cannot
    rot in either direction: if it starts passing, ACE-039 has landed and this marker must go; if
    the assertion changes shape, it fails loudly.

    One route is enough. The fork path relays the same child-classified text for the same exit code,
    so this pins the field, not the transport.
    """
    tools.set_injected_executor(_PostgresLikeExecutor())
    body = json.loads(tools.tool_execute_sql({"sql": FAILED_SQL, "datasource": PROFILE}))

    assert body["status"] == "failed", body
    _assert_echo_only(body, FAILED_SQL)


def _assert_no_infrastructure(refusal: dict, *, whole_body: str, env) -> None:
    """No filesystem path, no DSN, no hostname — asserted on the two authored fields.

    `/` is banned outright in these two, which the other rules could not accept (their remediations
    legitimately name the `/agami-model` command). That is the point: this rule's refusal is emitted
    from a code path that has a resolved path and a connection string in hand, so the absence of a
    separator is the cheapest proof that neither was interpolated in.
    """
    authored = f"{refusal['detail']} {refusal['remediation']}"
    assert "/" not in authored and "\\" not in authored, authored  # no filesystem path
    assert "://" not in authored, authored  # no DSN
    assert not re.search(r"\b(?:localhost|\d{1,3}(?:\.\d{1,3}){3})\b", authored), authored  # no host
    # And the two concrete locations this run actually probed are absent from the whole response.
    assert str(env.artifacts) not in whole_body
    assert str(env.app_db_path) not in whole_body


def test_the_unimportable_package_refusal_names_no_infrastructure(unavailable, monkeypatch):
    """`model_unavailable` has TWO construction sites and this is the other one.

    `tests/test_ace051_fail_closed.py::test_hosted_fail_closed_when_model_package_unimportable`
    proves this branch fails closed; it does not check what the refusal says. Driven in-process
    because forcing the import to fail is a monkeypatch, not an environment — the branch's output is
    a static string, so one route is enough to pin it.
    """
    import builtins

    real_import = builtins.__import__

    def _boom(name, _globals=None, _locals=None, fromlist=(), level=0):
        if name == "semantic_model" and fromlist and "runtime" in fromlist:
            raise ImportError("forced: semantic_model.runtime unavailable")
        return real_import(name, _globals, _locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _boom)
    _, verdict = execute_sql._model_safety(UNAVAILABLE_SQL, PROFILE, None)
    monkeypatch.undo()  # restore the import hook before touching json/re again

    assert isinstance(verdict, guardrail.Refusal)
    assert verdict.rule == guardrail.RULE_MODEL_UNAVAILABLE
    refusal = {"detail": verdict.detail, "remediation": verdict.remediation}
    _assert_no_infrastructure(refusal, whole_body=json.dumps(refusal), env=unavailable)
    for canary in CANARIES:
        assert not _mentions(json.dumps(refusal), canary), canary


# ---------------------------------------------------------------------------
# Keeping the sentinel honest
# ---------------------------------------------------------------------------


def test_the_canaries_are_real(tmp_path):
    """A canary that the model does not declare, or that a test statement happens to send, proves
    nothing — and would do so silently. So: every name in `DECLARED_NAMES` really is in the YAML the
    fixture writes (the constant cannot go stale), every canary is one of them, and no statement any
    test here sends mentions one.
    """
    _write_model(tmp_path / PROFILE)
    model_text = "\n".join(
        p.read_text() for p in sorted((tmp_path / PROFILE).rglob("*.yaml"))
    )
    for name in DECLARED_NAMES:
        assert _mentions(model_text, name), f"{name!r} is not actually declared by the fixture"

    sent = [sql for _, sql in VECTORS] + [UNAVAILABLE_SQL, FAILED_SQL]
    for canary in CANARIES:
        assert canary in DECLARED_NAMES, canary
        for sql in sent:
            assert not _mentions(sql, canary), (canary, sql)


def test_the_scanner_can_go_red():
    """Twenty green rows are worth nothing if the scanner cannot fail.

    A `_mentions` broken by a well-meaning simplification — dropping the `\\b` anchors, or the
    `IGNORECASE` — would leave every assertion above vacuously true and the sentinel silently
    disarmed. So: three shapes an "enumerating" refusal would take must each trip it.
    """
    sql = "SELECT ref_no FROM audit_trail"
    honest = {
        "status": "refused",
        "refusal": {
            "reason": "out_of_scope", "rule": "table_scope",
            "detail": "query references table(s) not in the semantic model: audit_trail",
            "remediation": "Add the table to the model, or remove it from the query.",
        },
        "sql": sql,
        "audit_id": "0" * 32,
    }
    _assert_echo_only(honest, sql)  # the shape actually shipped: clean

    for leak in (
        "declared tables here: orders, ledger_archive",  # the classic "did you mean" listing
        "did you mean orders.internal_ref?",  # a single column is an enumeration of one
        "try ENTRY_CODE instead",  # case must not be an escape hatch
    ):
        enumerating = json.loads(json.dumps(honest))
        enumerating["refusal"]["remediation"] = leak
        with pytest.raises(AssertionError):
            _assert_echo_only(enumerating, sql)


def test_echoing_the_callers_own_identifier_is_allowed():
    """The other half of the rule, pinned so a later tightening cannot ban the echo by accident.

    A gate that could not name the offending identifier would be telling the caller "something in
    your statement is wrong" — unactionable, and the contract makes an unactionable refusal a
    construction error. `orders` here came from the caller, so repeating it discloses nothing.
    """
    sql = "SELECT * FROM orders"
    body = {
        "status": "refused",
        "refusal": {
            "reason": "out_of_scope", "rule": "select_star",
            "detail": "query uses SELECT * on orders — every column must be named",
            "remediation": "List the columns of orders explicitly instead of '*'.",
        },
        "sql": sql,
        "audit_id": "0" * 32,
    }
    _assert_echo_only(body, sql)


def test_every_rule_and_every_route_is_covered():
    """The matrix is the claim; an accidentally-thinned one would still be green."""
    assert {rule for rule, _ in VECTORS} | {guardrail.RULE_MODEL_UNAVAILABLE} == {
        guardrail.RULE_READ_ONLY,
        guardrail.RULE_TABLE_SCOPE,
        guardrail.RULE_SELECT_STAR,
        guardrail.RULE_COLUMN_SCOPE,
        guardrail.RULE_MODEL_UNAVAILABLE,
    }
    assert set(ROUTES) == {"in_process", "fork", "stdio", "http"}
    assert len(_MATRIX) == len(VECTORS) * len(ROUTES) == 16
    # The `failed` channel is covered by its own matrix rather than this one, because it has no
    # rule. Its vector must not be one of these: a statement that a gate refuses would report on the
    # refusal channel a second time and leave `failure.message` unscanned again, which is exactly
    # the hole being closed.
    assert FAILED_SQL not in {sql for _, sql in VECTORS} and FAILED_SQL != UNAVAILABLE_SQL
