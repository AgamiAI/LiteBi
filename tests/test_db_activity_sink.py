"""The DB write path — execute_sql logs to the DB when AGAMI_DB_URL is set (Slice D).

`DbActivitySink` conforms to the `ports.ActivitySink` Protocol by shape (no inheritance) and is
backend-agnostic (one class — SQLite here, Postgres in prod). The local jsonl path is unchanged
when AGAMI_DB_URL is unset.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")  # the contract records are pydantic

import tools  # noqa: E402
from model_store import DbActivitySink  # noqa: E402
from ports import ActivitySink  # noqa: E402
from store import Store  # noqa: E402


def _fresh_db(tmp_path) -> str:
    url = "sqlite://" + str(tmp_path / "agami.db")
    s = Store.connect(url)
    s.run_migrations()
    s.close()
    return url


def test_db_sink_conforms_to_activity_sink_port():
    # structural conformance — has the methods; verified via the runtime_checkable Protocol
    assert isinstance(DbActivitySink(Store.connect("sqlite://")), ActivitySink)


def test_record_query_writes_one_row(tmp_path, monkeypatch):
    url = _fresh_db(tmp_path)
    monkeypatch.setenv("AGAMI_DB_URL", url)
    tools._record_query(
        {
            "id": "7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f",
            "ts": "2026-06-25T00:00:00Z",
            "profile": "main",
            "question": "how many orders?",
            "sql": "SELECT count(*) FROM orders",
            "row_count": 1,
            "source": "mcp_server",
            "status": "ok",
        }
    )
    # a fresh connection (a "second instance") reads it
    s = Store.connect(url)
    rows = s.query(
        "SELECT id, datasource, question, sql, row_count, source, status, reason, rule "
        "FROM query_executions"
    )
    s.close()
    assert rows == [
        {
            # The caller's id lands as the row's primary key — the sink no longer mints (and throws
            # away) one of its own, which is what made the row unreferenceable.
            "id": "7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f",
            "datasource": "main",
            "question": "how many orders?",
            "sql": "SELECT count(*) FROM orders",
            "row_count": 1,
            "source": "mcp_server",
            "status": "ok",
            # An `ok` row has no rule to record: only a refusal is a decision of ours.
            "reason": None,
            "rule": None,
        }
    ]


def test_record_query_is_best_effort_on_db_error(tmp_path, monkeypatch, caplog):
    # AGAMI_DB_URL points at a DB with NO migrations applied, so the INSERT into query_executions
    # fails. _record_query must swallow it — a logging failure can't break a successful query — but
    # it must SAY so: a permanently broken sink and a working one have to be distinguishable.
    url = "sqlite://" + str(tmp_path / "empty.db")
    Store.connect(url).close()  # create the file; no tables
    monkeypatch.setenv("AGAMI_DB_URL", url)
    with caplog.at_level("WARNING", logger="tools"):
        tools._record_query(
            {  # must NOT raise
                "id": "0a1b2c3d4e5f60718293a4b5c6d7e8f9",
                "ts": "2026-06-25T00:00:00Z",
                "profile": "main",
                "question": "q",
                "sql": "SELECT 1",
                "row_count": 1,
                "source": "mcp_server",
                "status": "ok",
            }
        )
    assert [r.levelname for r in caplog.records] == ["WARNING"]
