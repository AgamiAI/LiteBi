"""The guardrail audit trail — every execute_sql result (ok or refused) writes one row keyed by
the response Envelope's `audit_id`, at the shared chokepoint so BOTH surfaces are covered.

Also checks the jsonl fallback (no datastore) and that a logging failure never breaks the tool.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import tools  # noqa: E402
from store import Store  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    url = "sqlite://" + str(tmp_path / "audit.db")
    monkeypatch.setenv("AGAMI_DB_URL", url)
    s = Store.connect(url)
    s.run_migrations()
    s.close()
    return url


def _audit_rows(url):
    s = Store.connect(url)
    rows = s.query("SELECT * FROM guardrail_audit ORDER BY ts")
    s.close()
    return rows


class _FakeProc:
    """A successful executor run: RFC-4180 CSV on stdout (header + one row), clean stderr."""

    returncode = 0
    stdout = "n\n5\n"
    stderr = ""


def test_migration_creates_the_table(db):
    assert _audit_rows(db) == []  # table exists, empty


def test_refused_query_writes_a_refused_audit_row(db):
    # A write is rejected by the read-only gate before any subprocess — the refusal still audits.
    resp = json.loads(
        tools.tool_execute_sql(
            {"sql": "DELETE FROM t", "datasource": "sales", "correlation_id": "turn-1"}
        )
    )
    assert resp["status"] == "refused" and resp["refusal"]["kind"] == "permission"

    (row,) = _audit_rows(db)
    assert row["audit_id"] == resp["audit_id"]  # the row the Envelope points at
    assert row["status"] == "refused"
    assert row["refusal_kind"] == "permission"
    assert row["sql"] == "DELETE FROM t"
    assert row["datasource"] == "sales"
    assert row["correlation_id"] == "turn-1"
    assert row["row_count"] is None
    assert (
        row["error_detail"] is None
    )  # a guardrail refusal has a clean reason — no raw detail stored


def test_ok_query_writes_an_ok_audit_row(db, monkeypatch):
    monkeypatch.setattr(tools.subprocess, "run", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(tools, "_resolve_units", lambda *a: {})
    monkeypatch.setattr(tools, "_resolve_receipt", lambda *a: None)

    resp = json.loads(tools.tool_execute_sql({"sql": "SELECT n FROM t", "datasource": "sales"}))
    assert resp["status"] == "ok" and resp["data"]["row_count"] == 1

    (row,) = _audit_rows(db)
    assert row["audit_id"] == resp["audit_id"]
    assert row["status"] == "ok"
    assert row["refusal_kind"] is None
    assert row["sql"] == "SELECT n FROM t"
    assert row["row_count"] == 1


def test_operational_error_puts_raw_in_audit_not_in_envelope(db, monkeypatch):
    # A DB execution error: the RAW driver text (schema / column / value names) must be captured in
    # the audit row's error_detail (for the operator) but NEVER surface in the Envelope refusal.
    class _ErrProc:
        returncode = 5
        stdout = ""
        stderr = "permission denied for table salaries"

    monkeypatch.setattr(tools.subprocess, "run", lambda *a, **k: _ErrProc())

    resp = json.loads(tools.tool_execute_sql({"sql": "SELECT x FROM t", "datasource": "sales"}))
    assert resp["status"] == "refused"
    assert resp["refusal"]["kind"] == "permission"
    assert "salaries" not in json.dumps(
        resp
    )  # the raw object name is absent from the whole response

    (row,) = _audit_rows(db)
    assert row["audit_id"] == resp["audit_id"]
    assert row["refusal_kind"] == "permission"
    assert "salaries" in (row["error_detail"] or "")  # but IS captured server-side for the operator


def test_recon_query_writes_a_recon_audit_row(db):
    # A recon query is refused before any subprocess — the recon refusal audits with kind=recon and,
    # being a guardrail refusal, stores no raw error_detail.
    resp = json.loads(tools.tool_execute_sql({"sql": "SELECT current_user", "datasource": "sales"}))
    assert resp["status"] == "refused" and resp["refusal"]["kind"] == "recon"

    (row,) = _audit_rows(db)
    assert row["audit_id"] == resp["audit_id"]
    assert row["refusal_kind"] == "recon"
    assert row["error_detail"] is None


def test_jsonl_fallback_carries_error_detail_on_operational_failure(tmp_path, monkeypatch):
    # The default OSS deployment (no AGAMI_DB_URL) writes audit to the local jsonl — an operational
    # failure's RAW text must land there (and only there), not in the response.
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    log = tmp_path / "guardrail_audit.jsonl"
    monkeypatch.setattr(tools, "GUARDRAIL_AUDIT_LOG", log)

    class _ErrProc:
        returncode = 5
        stdout = ""
        stderr = "permission denied for table salaries"

    monkeypatch.setattr(tools.subprocess, "run", lambda *a, **k: _ErrProc())

    resp = json.loads(tools.tool_execute_sql({"sql": "SELECT x FROM t", "datasource": "sales"}))
    assert resp["status"] == "refused" and "salaries" not in json.dumps(resp)

    rec = json.loads(log.read_text().splitlines()[0])
    assert "salaries" in (rec.get("error_detail") or "")


def test_audit_insert_degrades_on_pre_013_schema(tmp_path):
    # New code against an un-migrated DB (guardrail_audit WITHOUT error_detail): the audit row must
    # SURVIVE (minus the raw detail), not be silently dropped by the best-effort recorder.
    from contracts import GuardrailAuditRecord  # noqa: PLC0415
    from model_store import DbActivitySink  # noqa: PLC0415

    s = Store.connect("sqlite://" + str(tmp_path / "old.db"))
    s.execute(
        "CREATE TABLE guardrail_audit (audit_id TEXT PRIMARY KEY, ts TEXT NOT NULL, datasource TEXT, "
        "status TEXT NOT NULL, refusal_kind TEXT, sql TEXT, row_count INTEGER, execution_ms INTEGER, "
        "correlation_id TEXT, source TEXT)"  # the pre-013 10-column schema
    )
    s.commit()

    rec = GuardrailAuditRecord(
        audit_id="a1",
        ts="2026-07-12T00:00:00Z",
        status="refused",
        refusal_kind="syntax",
        error_detail="permission denied for table salaries",
    )
    DbActivitySink(s).record_guardrail_audit(rec)  # must NOT raise and must NOT drop the row

    rows = s.query("SELECT * FROM guardrail_audit")
    s.close()
    assert len(rows) == 1 and rows[0]["audit_id"] == "a1" and rows[0]["refusal_kind"] == "syntax"
    assert "error_detail" not in rows[0]  # column absent; row still written without the raw detail


def test_audit_insert_does_not_retry_on_a_non_schema_error():
    # Copilot review: the pre-013 fallback fires ONLY for the missing error_detail column. Any OTHER
    # insert failure must propagate on the FIRST attempt — a blind retry would mask a real DB error
    # (e.g. a deadlock / constraint violation) AND silently drop error_detail. Pin: one execute call,
    # no rollback, the original error re-raised.
    from model_store import DbActivitySink  # noqa: PLC0415

    class _FakeStore:
        def __init__(self):
            self.execute_calls = 0
            self.rolled_back = False

        def execute(self, *_a, **_k):
            self.execute_calls += 1
            raise RuntimeError("deadlock detected")  # NOT a missing-column signature

        def rollback(self):
            self.rolled_back = True

        def commit(self):
            pass

    class _Rec:
        audit_id = "a"
        ts = "t"
        datasource = None
        status = "ok"
        refusal_kind = None
        sql = None
        row_count = None
        execution_ms = None
        correlation_id = None
        source = "x"
        error_detail = "raw driver text"

    store = _FakeStore()
    with pytest.raises(RuntimeError, match="deadlock"):
        DbActivitySink(store).record_guardrail_audit(_Rec())
    assert store.execute_calls == 1  # first insert only — NO fallback retry on a non-schema error
    assert store.rolled_back is False  # rollback belongs to the pre-013 path, never reached here


def test_jsonl_fallback_when_no_datastore(tmp_path, monkeypatch):
    # No AGAMI_DB_URL → the audit row lands in the local jsonl instead of the DB.
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    log = tmp_path / "guardrail_audit.jsonl"
    monkeypatch.setattr(tools, "GUARDRAIL_AUDIT_LOG", log)

    resp = json.loads(tools.tool_execute_sql({"sql": "DROP TABLE t", "datasource": "sales"}))
    assert resp["status"] == "refused"

    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["audit_id"] == resp["audit_id"]
    assert rec["status"] == "refused" and rec["refusal_kind"] == "permission"


def test_audit_write_is_best_effort_and_never_raises(tmp_path, monkeypatch):
    # A failing write (here: the jsonl append blows up) is swallowed inside the recorder, so the
    # tool that calls it through _finish can never be broken by a logging failure.
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(tools, "_append_jsonl", _boom)
    tools._record_guardrail_audit({"audit_id": "x", "ts": "t", "status": "ok"})  # must not raise


def test_audit_sink_failure_warns_exactly_once(tmp_path, monkeypatch, capsys):
    # Best-effort must not be SILENT: a PERSISTENTLY unwritable sink emits a one-time warning so a dead
    # audit trail is observable to an operator — but only ONCE (not a line per query), and never with
    # raw sql/driver text, and never breaking the tool.
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.setattr(tools, "_append_jsonl", lambda *_a, **_k: False)  # log dir unwritable

    for i in range(3):
        tools._record_guardrail_audit({"audit_id": str(i), "ts": "t", "status": "ok"})  # never raises

    err = capsys.readouterr().err
    assert err.count("guardrail audit not recorded") == 1  # warned ONCE across 3 failures, not per-call
    assert "SELECT" not in err and "status" not in err  # value-free: no sql / record contents leaked


def test_audit_db_sink_error_warns_once(tmp_path, monkeypatch, capsys):
    # The DB-sink branch (exception, not a False return) is also surfaced once — type only, no message.
    url = "sqlite://" + str(tmp_path / "audit.db")
    monkeypatch.setenv("AGAMI_DB_URL", url)

    def _boom_store(*_a, **_k):
        raise RuntimeError("permission denied for table guardrail_audit on host db-42.internal")

    monkeypatch.setattr(tools, "_append_jsonl", lambda *_a, **_k: True)
    import store as _store

    monkeypatch.setattr(_store.Store, "from_env", staticmethod(_boom_store))

    tools._record_guardrail_audit({"audit_id": "a", "ts": "t", "status": "ok"})
    tools._record_guardrail_audit({"audit_id": "b", "ts": "t", "status": "ok"})

    err = capsys.readouterr().err
    assert err.count("guardrail audit not recorded (sink error)") == 1
    assert "db-42.internal" not in err  # value-free: the driver message never rides the warning
