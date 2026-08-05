"""ACE-101's off path: what a deployment that has turned the semantic-model pass off actually gets.

The switch (`AGAMI_GOVERNANCE_ENFORCED`, hosted only, default OFF) exists because the 4b/4c gates
refuse on facts about OUR parser and OUR model resolution rather than about the caller's statement, so
a dialect drift or a model that will not resolve refuses every query on a server until an operator
intervenes. Turning them off is a supported posture. What it must never do is let a receipt, an
Envelope or an audit row describe checks that did not run.

Two properties, and the second is the one that is silent when wrong:

  * the gates really are off, on both halves of the pass (the scope gates, and the engine-mismatch
    check that lives in a second `if not no_safety:` block below them);
  * and NOTHING claims otherwise. Every section of the receipt says why it establishes nothing, both
    receipt builders say it identically for one call (REQ-002), and the audit row carries the same
    sentence rather than a tidier one.

`tests/test_ace051_fail_closed.py` is the idiom source: hosted is simulated with a REACHABLE app
database (`sqlite://…`), never an unreachable URL. That is load-bearing since ACE-097 made the served
audit write a gate above everything else here: an unreachable URL turns every call into
`audit_unavailable` and each test below would then be measuring that instead of its subject.

The complementary file is `test_ace101_4a_invariance.py`, which asserts what the switch may NOT reach.
Everything here is synthetic: a fabricated `Shop` datasource over `orders`, one undeclared `secrets`
table, and a SQLite file for a warehouse.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402
import guardrail  # noqa: E402
import tools  # noqa: E402
from store import Store  # noqa: E402

PROFILE = "acme"
AREA = "sales"

# A statement the model declares whole, so the only thing that can change its verdict is the switch.
DECLARED_SQL = "SELECT id, status FROM orders"
# The 4b vector: a table the WAREHOUSE has and the MODEL does not declare. Both halves matter. Without
# the warehouse table the flag-off arm would come back `failed` (the database rejecting a name) rather
# than `ok`, and the test would pass for the wrong reason.
UNDECLARED_SQL = "SELECT id FROM secrets"

# Every spelling the switch accepts, and every spelling it must not. The affirmative set is small and
# closed on purpose; the negative set carries the ones an operator actually types.
_ON_SPELLINGS = ("1", "true", "yes", "on", "TRUE", "Yes", "  on  ", "On\n")
_OFF_SPELLINGS = ("", "   ", "0", "false", "FALSE", "no", "off", "ture", "2", "true1", "enabled")


def _write_model(root: Path, *, engine: str = "SQLite") -> None:
    """The model this file governs against: one subject area, one declared table.

    `engine` is the storage type the model DECLARES, and it is a parameter because one test needs it to
    disagree with the credentials on purpose. Everywhere else it matches the SQLite warehouse below, or
    the engine-mismatch gate would refuse every statement before the gate under test could answer.
    """
    import yaml

    (root / "subject_areas" / AREA / "tables").mkdir(parents=True)
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
                "tables": [{"storage_connection": "c", "schema": "public", "table": "orders"}],
            }
        )
    )
    (root / "subject_areas" / AREA / "tables" / "orders.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "orders",
                "schema": "public",
                "storage_connection": "c",
                "grain": ["id"],
                "description": "orders",
                "columns": [
                    {"name": "id", "type": "integer", "primary_key": True},
                    {"name": "status", "type": "string"},
                ],
            }
        )
    )


def _seed_warehouse(path: Path) -> None:
    """The warehouse: the declared table, and one the model says nothing about."""
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE orders (id INTEGER, status TEXT)")
        con.executemany("INSERT INTO orders VALUES (?, ?)", [(1, "paid"), (2, "open")])
        con.execute("CREATE TABLE secrets (id INTEGER)")
        con.execute("INSERT INTO secrets VALUES (7)")
        con.commit()
    finally:
        con.close()


def _build(tmp_path: Path, monkeypatch, *, engine: str) -> SimpleNamespace:
    """A hosted deployment whose model resolves perfectly, from BOTH sources.

    The model is written to the artifacts directory AND deployed into the app database from that same
    directory, so the two sources cannot describe different tables. Both are needed and for different
    readers: `tools._load_org` does NOT fall back to disk when a database is configured (it raises), so
    the fork-path receipt needs the deployed copy, while the one test that drops hosted mode entirely
    needs the copy on disk.

    A model that resolves is the whole point of the fixture. With the switch off the interesting
    failure is a receipt that reads CLEAN, and a receipt can only read clean if the model was there to
    build one from. A fixture with no model would produce an undetermined receipt for the ordinary
    reason and every assertion below would pass while proving nothing.
    """
    import model_deploy
    import model_store

    artifacts = tmp_path / "artifacts"
    _write_model(artifacts / PROFILE, engine=engine)

    warehouse = tmp_path / "warehouse.db"
    _seed_warehouse(warehouse)

    app_db = "sqlite://" + str(tmp_path / "app.db")
    monkeypatch.setenv("AGAMI_DB_URL", app_db)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    monkeypatch.delenv("AGAMI_ORG_ID", raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(artifacts))
    monkeypatch.setenv(f"DATASOURCE_URL__{PROFILE.upper()}", f"sqlite:///{warehouse}")
    monkeypatch.delenv("AGAMI_SQL_MAX_ROWS", raising=False)
    # The deploy stamps rows with `resolved_org_id()` and the serve path reads them back with it, so
    # the memo has to be cleared AFTER the environment above is in place or the write and the read
    # could disagree about which org this deployment is.
    tools.resolved_org_id.cache_clear()

    store = Store.connect(app_db)
    try:
        store.run_migrations()
        model_deploy.deploy_one(store, PROFILE, artifacts / PROFILE)
        # Asserted at the point of setup rather than left to surface as a puzzling refusal later: a
        # model that did not come back out of the database would make every receipt below undetermined
        # for a reason that has nothing to do with this spec.
        assert (
            model_store.load_datasource(store, PROFILE, org_id=tools.resolved_org_id()) is not None
        ), "the model did not come back out of the app database"
    finally:
        store.close()

    tools.set_injected_executor(None)
    return SimpleNamespace(app_db=app_db, artifacts=artifacts, warehouse=warehouse)


@pytest.fixture()
def deployment(tmp_path, monkeypatch) -> SimpleNamespace:
    """The ordinary hosted deployment: the model declares the engine its credentials speak."""
    return _build(tmp_path, monkeypatch, engine="SQLite")


@pytest.fixture()
def mismatched_deployment(tmp_path, monkeypatch) -> SimpleNamespace:
    """The same deployment with one operator error in it: the model declares an engine the
    credentials do not connect to. That is what `_engine_mismatch` exists to catch, and it is the half
    of the pass that lives BELOW `_model_safety` in a second `if not no_safety:` block."""
    return _build(tmp_path, monkeypatch, engine="PostgreSQL")


def _enforced(monkeypatch, on: bool) -> None:
    """Put the switch in one of its two postures.

    The off posture DELETES the variable rather than writing a false spelling, because deleting it is
    the posture that ships (the default is off) and because `tests/conftest.py` pins it on for the
    whole suite. Writing `"false"` would exercise the parser; deleting exercises the default.
    """
    if on:
        monkeypatch.setenv("AGAMI_GOVERNANCE_ENFORCED", "true")
    else:
        monkeypatch.delenv("AGAMI_GOVERNANCE_ENFORCED", raising=False)


def _run(sql: str):
    """One statement through the real chokepoint with the real executor."""
    return execute_sql.execute_guarded(
        sql, PROFILE, AREA, executor=execute_sql.BUILTIN_EXECUTOR
    )


def _undetermined(receipt: guardrail.Receipt) -> set[str | None]:
    return {getattr(receipt, name).undetermined for name in guardrail.Receipt.SECTIONS}


def _assert_governance_disabled_receipt(receipt: guardrail.Receipt) -> None:
    """Every section says the pass is off, and none of them is quietly clean.

    Both halves are asserted. A section with the reason set AND items filled in would be a receipt
    reporting findings from checks that never ran, which is a worse failure than a missing sentence.
    """
    assert _undetermined(receipt) == {guardrail.RECEIPT_GOVERNANCE_DISABLED}
    assert all(getattr(receipt, name).items == () for name in guardrail.Receipt.SECTIONS)


# ---------------------------------------------------------------------------
# The gates
# ---------------------------------------------------------------------------


def test_the_switch_off_lets_an_undeclared_table_through(deployment, monkeypatch):
    """One statement, both postures, so the difference is attributable to the switch and nothing else.

    The enforcing arm is not decoration: without it the flag-off `ok` could come from a model that
    silently declared `secrets`, or from a gate that was already inert for an unrelated reason, and the
    test would pass while measuring neither.
    """

    def verdict(enforced: bool):
        _enforced(monkeypatch, enforced)
        return _run(UNDECLARED_SQL)

    off = verdict(enforced=False)
    assert off.status == "ok", off.refusal or off.failure
    assert off.data.rows == [(7,)]  # it really reached the warehouse, rather than short-circuiting

    on = verdict(enforced=True)
    assert on.status == "refused"
    assert on.refusal.rule == guardrail.RULE_TABLE_SCOPE
    assert on.refusal.reason == guardrail.REASON_FOR_RULE[guardrail.RULE_TABLE_SCOPE]


def test_the_switch_off_disarms_the_engine_mismatch_check_too(mismatched_deployment, monkeypatch):
    """The second half of the pass, which needed its own copy of the condition to be reached.

    `_engine_mismatch` sits below `_model_safety` in a separate `if not no_safety:` block, so scoping
    the switch to that function alone would have left this gate refusing every statement on a
    deployment that had turned the pass off. It is the same class of finding as the gates above it: our
    configuration disagreeing with itself, not anything the caller's statement did.
    """

    def verdict(enforced: bool):
        _enforced(monkeypatch, enforced)
        return _run(DECLARED_SQL)

    off = verdict(enforced=False)
    assert off.status == "ok", off.refusal or off.failure

    on = verdict(enforced=True)
    assert on.status == "refused"
    assert on.refusal.rule == guardrail.RULE_ENGINE_MISMATCH
    assert on.refusal.reason == guardrail.REASON_FOR_RULE[guardrail.RULE_ENGINE_MISMATCH]


def test_the_local_path_is_untouched_by_the_switch(deployment, monkeypatch):
    """The switch is read only when `_hosted()` is true, so a laptop, the plugin and every OSS install
    behave exactly as they do with the variable absent.

    Both app-database variables are removed, because `_hosted()` reads either one and leaving the
    second set would have this test quietly running the hosted path it claims to have left.
    """
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    _enforced(monkeypatch, False)
    assert execute_sql._hosted() is False

    refused = _run(UNDECLARED_SQL)
    assert refused.status == "refused"
    assert refused.refusal.rule == guardrail.RULE_TABLE_SCOPE

    # And the gates are genuinely running rather than refusing everything: a declared statement still
    # answers, which is what makes the refusal above attributable to the scope gate.
    allowed = _run(DECLARED_SQL)
    assert allowed.status == "ok", allowed.refusal or allowed.failure


# ---------------------------------------------------------------------------
# The receipt, on both builders
# ---------------------------------------------------------------------------


def test_the_switch_off_leaves_no_clean_section_on_the_in_process_receipt(deployment, monkeypatch):
    """The chokepoint's own builder.

    Its trap is `_guard_model`: the pass returns before it can set the context var, so the builder's
    `org is None` branch would answer "no semantic model could be resolved" about a deployment whose
    model resolves perfectly. The branch is therefore the FIRST statement of the builder, and this is
    the assertion that keeps it there: placed one line lower, past the runtime import, the answer on a
    vendored install becomes `RECEIPT_NO_RUNTIME` instead. Both are false causes stated confidently.
    """
    _enforced(monkeypatch, False)

    _assert_governance_disabled_receipt(
        execute_sql._receipt_for(DECLARED_SQL, PROFILE, bounded=False)
    )
    # The bounded assembler is chosen by every non-ok outcome, and the branch above ignores the flag,
    # so both spellings have to answer the same way.
    _assert_governance_disabled_receipt(
        execute_sql._receipt_for(DECLARED_SQL, PROFILE, bounded=True)
    )


def test_the_switch_off_leaves_no_clean_section_on_the_forked_receipt(deployment, monkeypatch):
    """The fork parent's builder, which is the dangerous half of the pair.

    Its twin above reaches a branch that reports a FALSE cause; this one does not read `_guard_model`
    at all. It resolves the model itself, and on a deployment with the pass off that resolve SUCCEEDS,
    so left alone it would assemble a full receipt describing checks that never ran. The fork is the
    DEFAULT path, so that clean-looking receipt is what most callers would have received.

    The enforcing arm runs first and is the whole evidence for that claim: it proves this builder can
    and does assemble a populated receipt for this statement on this deployment, which is exactly what
    the off arm must then not do.
    """
    _enforced(monkeypatch, True)
    control = tools._resolve_receipt(PROFILE, DECLARED_SQL)
    assert guardrail.RECEIPT_GOVERNANCE_DISABLED not in _undetermined(control)
    assert any(getattr(control, name).items for name in guardrail.Receipt.SECTIONS), (
        "the control assembled nothing, so the off arm below would pass without the guard existing"
    )

    _enforced(monkeypatch, False)
    _assert_governance_disabled_receipt(tools._resolve_receipt(PROFILE, DECLARED_SQL))


def test_the_two_receipt_builders_describe_one_call_the_same_way(deployment, monkeypatch):
    """REQ-002: one call, one account of it, whichever process assembled the account.

    The two builders live in different modules and run in different processes (the chokepoint's
    in-process, the parent's on the far side of a fork, because the child's Envelope is destroyed at
    the boundary). Two switch branches is two chances for them to drift, and a caller has no way to
    tell which one answered.
    """
    _enforced(monkeypatch, False)

    in_process = execute_sql._receipt_for(DECLARED_SQL, PROFILE, bounded=True)
    forked = tools._resolve_receipt(PROFILE, DECLARED_SQL, bounded=True)

    assert forked == in_process
    _assert_governance_disabled_receipt(forked)


def test_the_audit_row_carries_the_same_receipt_the_caller_got(deployment, monkeypatch):
    """Principle 7c one layer down: what the caller was told is what the record keeps.

    A row that recorded a tidier receipt than the answer carried would turn "nobody looked" back into
    "nothing wrong" for the reviewer, which is the whole failure this family of reasons exists to
    prevent. No new column and no schema change is involved: `tools._record_execution` already stores
    the receipt verbatim, so the assertion is a read-back of what shipped.
    """
    _enforced(monkeypatch, False)

    envelope = _run(DECLARED_SQL)
    assert envelope.status == "ok", envelope.refusal or envelope.failure
    tools._emit(envelope, sql=DECLARED_SQL, execution_ms=None, profile=PROFILE)

    store = Store.connect(deployment.app_db)
    try:
        rows = store.query("SELECT id, status, receipt FROM query_executions ORDER BY ts")
    finally:
        store.close()

    assert len(rows) == 1
    recorded = json.loads(rows[0]["receipt"])
    # Compared after a JSON round-trip of the Envelope's own receipt, not against `asdict` directly:
    # `ReceiptSection.items` is a tuple in the type and a list once serialized, so a raw comparison
    # would fail on the encoding rather than on the content, which is not what this pins.
    assert recorded == json.loads(json.dumps(asdict(envelope.receipt), default=str))
    assert {recorded[name]["undetermined"] for name in guardrail.Receipt.SECTIONS} == {
        guardrail.RECEIPT_GOVERNANCE_DISABLED
    }


# ---------------------------------------------------------------------------
# The variable itself
# ---------------------------------------------------------------------------


def test_the_switch_is_off_for_anything_it_does_not_recognize(monkeypatch):
    """The parse, and the direction that matters is the default.

    Unset is OFF because the switch has to be able to ship before the gates have met real traffic; a
    typo is OFF because the alternative surprises an operator who believed they had turned enforcement
    ON, and that is the only one of the two mistakes with a security consequence. `ture` is in the
    negative set for exactly that reason rather than as a joke.
    """
    monkeypatch.delenv("AGAMI_GOVERNANCE_ENFORCED", raising=False)
    assert execute_sql._governance_enforced() is False, "unset must be off"

    for spelling in _ON_SPELLINGS:
        monkeypatch.setenv("AGAMI_GOVERNANCE_ENFORCED", spelling)
        assert execute_sql._governance_enforced() is True, spelling

    for spelling in _OFF_SPELLINGS:
        monkeypatch.setenv("AGAMI_GOVERNANCE_ENFORCED", spelling)
        assert execute_sql._governance_enforced() is False, spelling


def test_the_switch_is_read_on_every_call_rather_than_cached_at_import(deployment, monkeypatch):
    """An operator flips the variable on a running deployment and the next request obeys it.

    Asserted on the VERDICT and not only on the predicate, because a cached read would be invisible in
    the predicate if the cache happened to be per-call anyway. The module identity is pinned around
    both calls so the change cannot be attributed to a reimport picking up a fresh module-level
    constant, which is the mechanism this is ruling out.
    """
    module = sys.modules["execute_sql"]

    _enforced(monkeypatch, False)
    first = _run(UNDECLARED_SQL)

    _enforced(monkeypatch, True)
    second = _run(UNDECLARED_SQL)

    assert sys.modules["execute_sql"] is module, "the answer changed because of a reimport"
    assert first.status == "ok", first.refusal or first.failure
    assert second.status == "refused"
    assert second.refusal.rule == guardrail.RULE_TABLE_SCOPE
