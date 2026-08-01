"""The raw driver error is kept where only an operator can read it.

The caller receives a classified value-free sentence, so sanitizing without capturing would
leave an operator debugging a real customer failure with nothing at all. The detail goes to
`query_executions.error_detail` — a column on the row that already exists, keyed by the same
`audit_id` the caller was handed, because the record is 1:1 with the execution.

NULL is a claim, not a gap: it means the chokepoint holding the raw text and the recorder
writing the row were not in one process. On the forked stdio surface that is by design.

The sharpest test here is `test_the_forked_child_leaks_nothing_through_its_logger`. Logging
raw text in the child is not obviously safe: this module never calls `basicConfig`, so a
record falls through to `logging.lastResort` and lands on stderr, and the parent relays the
child's stderr into `failure.message`. Handing the text back to the caller on the default
surface would have undone the whole slice, in the very code meant to protect it.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys

import execute_sql
import pytest
import tools
import yaml

PROFILE = "acme"
PG_ERROR = (
    'Postgres execution error: column "amount" does not exist\n'
    'HINT:  Perhaps you meant to reference the column "orders.internal_ref".'
)


class _Raiser:
    def execute(self, vetted_sql, creds, *, profile):  # noqa: ANN001, ANN201
        raise execute_sql.ExecutorError(PG_ERROR, code=5)


def _write_model(root) -> None:
    """`orders` declared with `id` and `amount`; the warehouse below has only `id`.

    That gap is what lets a statement pass every gate and be rejected by the DATABASE, which is
    the branch these tests need — and it has to be a real model on disk rather than a stubbed
    `_model_safety`, because the forked child is a fresh process and inherits no monkeypatch.
    """
    (root / "subject_areas" / "sales" / "tables").mkdir(parents=True)
    (root / "datasource.yaml").write_text(
        yaml.safe_dump({"datasource": "Shop", "version": 1,
                        "subject_areas": ["subject_areas/sales"]})
    )
    (root / "subject_areas" / "sales" / "subject_area.yaml").write_text(
        yaml.safe_dump({"name": "sales", "tables": [
            {"storage_connection": "c", "schema": "public", "table": "orders"}]})
    )
    (root / "subject_areas" / "sales" / "tables" / "orders.yaml").write_text(
        yaml.safe_dump({
            "name": "orders", "schema": "public", "storage_connection": "c", "grain": ["id"],
            "description": "orders",
            "columns": [
                {"name": "id", "type": "integer", "primary_key": True},
                {"name": "amount", "type": "integer"},
            ],
        })
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    """An app database to audit into, a model on disk, and a real sqlite warehouse."""
    app_db = tmp_path / "app.db"
    from store import Store

    store = Store.connect(f"sqlite:///{app_db}")
    store.run_migrations()
    store.close()

    artifacts = tmp_path / "artifacts"
    _write_model(artifacts / PROFILE)

    warehouse = tmp_path / "warehouse.db"
    conn = sqlite3.connect(warehouse)
    conn.execute("CREATE TABLE orders (id INTEGER)")
    conn.commit()
    conn.close()

    monkeypatch.setenv("AGAMI_DB_URL", f"sqlite:///{app_db}")
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(artifacts))
    monkeypatch.setenv(f"DATASOURCE_URL__{PROFILE.upper()}", f"sqlite:///{warehouse}")
    return {"app_db": app_db, "artifacts": artifacts}


def _rows(app_db):
    conn = sqlite3.connect(app_db)
    try:
        return conn.execute(
            "SELECT id, status, error_detail FROM query_executions ORDER BY ts"
        ).fetchall()
    finally:
        conn.close()


def test_the_migration_adds_the_column(env):
    conn = sqlite3.connect(env["app_db"])
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(query_executions)")]
    finally:
        conn.close()
    assert "error_detail" in cols


def test_the_in_process_path_records_the_raw_detail(env):
    """Where the chokepoint and the recorder share a process, the column is populated."""
    tools.set_injected_executor(_Raiser())
    try:
        body = json.loads(tools.tool_execute_sql({"sql": "SELECT id FROM orders", "datasource": PROFILE}))
    finally:
        tools.set_injected_executor(None)

    rows = _rows(env["app_db"])
    assert len(rows) == 1
    audit_id, status, detail = rows[0]
    assert status == "failed"
    assert audit_id == body["audit_id"], "the row's key is the id the caller was handed"
    # The operator gets the whole thing, including the HINT that names a declared column.
    assert "internal_ref" in detail
    # The caller gets none of it.
    assert "internal_ref" not in body["failure"]["message"]
    assert body["failure"]["kind"] == "column_not_found"


def test_error_detail_never_appears_in_the_emitted_json(env):
    """The column is an audit field, not a response field. Neither the key nor the value."""
    tools.set_injected_executor(_Raiser())
    try:
        body = json.loads(tools.tool_execute_sql({"sql": "SELECT id FROM orders", "datasource": PROFILE}))
    finally:
        tools.set_injected_executor(None)

    serialized = json.dumps(body)
    assert "error_detail" not in serialized
    assert "internal_ref" not in serialized
    assert "HINT" not in serialized


def test_a_successful_call_records_no_detail(env):
    """The ContextVar is cleared on entry, so a previous failure cannot attach to a later row.

    Without that clear, the second row here would carry the first call's driver text — an audit
    row naming an error the statement it records never produced.
    """
    tools.set_injected_executor(_Raiser())
    try:
        tools.tool_execute_sql({"sql": "SELECT id FROM orders", "datasource": PROFILE})
    finally:
        tools.set_injected_executor(None)

    tools.set_injected_executor(execute_sql.BUILTIN_EXECUTOR)
    try:
        tools.tool_execute_sql({"sql": "SELECT id FROM orders", "datasource": PROFILE})
    finally:
        tools.set_injected_executor(None)

    rows = _rows(env["app_db"])
    assert len(rows) == 2
    statuses = {status: detail for _, status, detail in rows}
    assert "internal_ref" in statuses["failed"]
    assert statuses["ok"] is None


def test_a_forked_call_does_not_inherit_an_earlier_in_process_detail(env):
    """The stale-carrier bug, which shipped silently until a test happened to order two calls.

    `execute_guarded` clears the carrier on entry, but on the fork the PARENT never calls it —
    it spawns a child and then records the row itself. So a forked call following an
    in-process failure in the same server process read the earlier call's driver text and wrote
    it onto the later call's audit row: an operator would be debugging a statement against an
    error it never produced. The reset therefore belongs at the tool edge, where both paths
    begin, not inside the one path that happens to pass through the chokepoint.
    """
    tools.set_injected_executor(_Raiser())
    try:
        tools.tool_execute_sql({"sql": "SELECT id FROM orders", "datasource": PROFILE})
    finally:
        tools.set_injected_executor(None)

    # Now a FORKED call whose statement the database also rejects, in the same process.
    tools.tool_execute_sql({"sql": "SELECT amount FROM orders", "datasource": PROFILE})

    rows = _rows(env["app_db"])
    assert len(rows) == 2
    forked_detail = rows[1][2]
    assert forked_detail is None, "the forked row inherited the in-process call's driver text"


def test_the_stored_detail_is_bounded(env):
    """A driver error with a full HINT / CONTEXT / parameter dump is unbounded.

    015's argument applies verbatim: a failed statement must not become a way to grow the store.
    """
    huge = "x" * 50_000

    class _Huge:
        def execute(self, vetted_sql, creds, *, profile):  # noqa: ANN001, ANN201
            raise execute_sql.ExecutorError(huge, code=5)

    tools.set_injected_executor(_Huge())
    try:
        tools.tool_execute_sql({"sql": "SELECT id FROM orders", "datasource": PROFILE})
    finally:
        tools.set_injected_executor(None)

    _, _, detail = _rows(env["app_db"])[0]
    assert len(detail) == tools.AUDIT_ERROR_DETAIL_MAX_CHARS


def test_the_forked_child_leaks_nothing_through_its_logger(env):
    """THE hazard this slice's design exists to avoid, driven as a real subprocess.

    `execute_sql` never calls `basicConfig`, so a record on the module logger falls through to
    `logging.lastResort` and is written to stderr — and `tools._child_failure_message` relays
    the child's stderr into `failure.message` for any classified exit code. A raw-detail log on
    the ordinary logger would therefore return the driver text to the caller on the DEFAULT
    surface. `_RAW_LOG` is silenced for the CLI lifetime instead, so the child emits nothing.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "execute_sql", "--profile", PROFILE,
         "--sql", "SELECT amount FROM orders"],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "AGAMI_ARTIFACTS_DIR": str(env["artifacts"])},
    )
    assert proc.returncode == execute_sql.FAILURE_KIND_TO_EXIT["column_not_found"]
    # The whole stream, not just the message: anything here is relayed to the caller.
    assert "amount" not in proc.stderr
    assert "audit detail" not in proc.stderr
    assert proc.stderr.strip() == execute_sql._ERROR_MESSAGES["column_not_found"]


def test_the_forked_path_records_null(env):
    """NULL is the claim that the two halves were in different processes."""
    body = json.loads(
        tools.tool_execute_sql({"sql": "SELECT amount FROM orders", "datasource": PROFILE})
    )
    assert body["status"] == "failed"
    assert body["failure"]["kind"] == "column_not_found"

    rows = _rows(env["app_db"])
    assert len(rows) == 1
    assert rows[0][2] is None
    assert "amount" not in body["failure"]["message"]


def test_migrations_are_re_runnable(tmp_path):
    """Forward-only, and the ledger makes a second run a no-op rather than an error."""
    from store import Store

    url = f"sqlite:///{tmp_path / 'twice.db'}"
    for _ in range(2):
        store = Store.connect(url)
        store.run_migrations()
        store.close()

    conn = sqlite3.connect(tmp_path / "twice.db")
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(query_executions)")]
    finally:
        conn.close()
    assert cols.count("error_detail") == 1
