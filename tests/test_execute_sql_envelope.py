"""tool_execute_sql assembles the response Envelope from the executor subprocess output — the
executor→tool refusal relay.

The `permission` refusal short-circuits BEFORE the subprocess (in the read-only pre-check), so this
is the only coverage of how a `{"refusal": ...}` stderr line (the model/scope/PII gates) — or a bare
operational failure — becomes a refused Envelope with the right `kind`. A regression in the stderr
parse would silently degrade every model-scope refusal to a generic error; these pin it.
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


class _Proc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture(autouse=True)
def _no_db(monkeypatch, tmp_path):
    # The audit write in _finish is best-effort; point its jsonl fallback at a temp file and ensure
    # no DB is configured, so these stay hermetic.
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.setattr(tools, "GUARDRAIL_AUDIT_LOG", tmp_path / "audit.jsonl")


def _run(monkeypatch, proc: _Proc) -> dict:
    monkeypatch.setattr(tools.subprocess, "run", lambda *a, **k: proc)
    return json.loads(tools.tool_execute_sql({"sql": "SELECT id FROM t"}))


def test_stderr_refusal_line_becomes_a_refused_envelope(monkeypatch):
    stderr = json.dumps(
        {
            "refusal": {
                "kind": "table_out_of_scope",
                "reason": "table foo not in the model",
                "remediation": "add it to the model",
            }
        }
    )
    env = _run(monkeypatch, _Proc(1, stderr=stderr))
    assert env["status"] == "refused"
    assert env["refusal"] == {
        "kind": "table_out_of_scope",
        "reason": "table foo not in the model",
        "remediation": "add it to the model",
    }
    assert "data" not in env  # a refusal carries no data
    assert env["audit_id"]


def test_refusal_line_is_found_among_interleaved_notices(monkeypatch):
    # The executor prints `[agami] …` notices to stderr too; the parser must skip them and still
    # find the refusal line.
    stderr = "[agami] applied default_filters: deleted_at IS NULL\n" + json.dumps(
        {"refusal": {"kind": "sensitive_columns", "reason": "raw PII", "remediation": "aggregate"}}
    )
    env = _run(monkeypatch, _Proc(1, stderr=stderr))
    assert env["status"] == "refused" and env["refusal"]["kind"] == "sensitive_columns"


def test_resource_limit_refusal_discards_partial_stdout(monkeypatch):
    # The per-statement timeout emits a {"refusal": {"kind": "resource_limit"}} line from the
    # executor. Both transports (stdio + HTTP) funnel through tool_execute_sql, so this single relay
    # is the both-surfaces guarantee. Critically: a streaming engine may have flushed a partial CSV
    # (e.g. the header row) to stdout before the cancel — the relay must DISCARD it on the non-zero
    # exit, so a killed query is a refused Envelope with no data. Feed partial stdout to pin that.
    stderr = json.dumps(
        {
            "refusal": {
                "kind": "resource_limit",
                "reason": "the query exceeded the 30s statement timeout and was cancelled",
                "remediation": "Narrow the query, or raise AGAMI_SQL_TIMEOUT_S (currently 30s).",
            }
        }
    )
    env = _run(monkeypatch, _Proc(1, stdout="n\n1\n2\n", stderr=stderr))
    assert env["status"] == "refused"
    assert env["refusal"]["kind"] == "resource_limit"
    assert "data" not in env  # the partial CSV on stdout is discarded — no partial data leaks
    assert env["audit_id"]


@pytest.mark.parametrize(
    "raw,returncode,expected_kind,secret",
    [
        ("permission denied for table salaries", 5, "permission", "salaries"),
        ('relation "hr.employees" does not exist', 5, "table_not_found", "hr.employees"),
        ('column "ssn" does not exist', 5, "column_not_found", "ssn"),
        ('syntax error at or near "SELCT"', 5, "syntax", "SELCT"),
        ("canceling statement due to statement timeout", 5, "timeout", None),
        # opaque exit-5 → the exit-code prior (syntax); the raw (incl. a path) must not leak
        ("driver panic 0xDEADBEEF at /var/lib/pg/secret", 5, "syntax", "/var/lib/pg/secret"),
        # the HIGH-LEAK kinds — raw stderr carries a hostname / username
        ('could not translate host name "internal-db.corp"', 4, "dsn", "internal-db.corp"),
        ("connection refused", 4, "network", None),
        ('password authentication failed for user "admin_svc"', 4, "auth", "admin_svc"),
        # dialect variants — same refined kind from different driver wording
        ("(1054, \"Unknown column 'ssn' in 'field list'\")", 5, "column_not_found", "ssn"),
        ("no such column: ssn", 5, "column_not_found", "ssn"),
        ("no such table: employees", 5, "table_not_found", "employees"),
        # Snowflake: 'object <name> does not exist' must win over its 'compilation error' prefix
        (
            "SQL compilation error: Object 'DB.SCHEMA.SECRETS' does not exist or not authorized",
            5,
            "table_not_found",
            "DB.SCHEMA.SECRETS",
        ),
    ],
)
def test_operational_error_is_classified_and_sanitized(
    monkeypatch, raw, returncode, expected_kind, secret
):
    # An execution error must NOT leak raw driver text (schema / column / value / host / user names)
    # into the response — it is classified into a generic, value-free message. (The raw goes only to
    # the audit trail; see tests/test_guardrail_audit.py::test_operational_error_puts_raw_in_audit_not_in_envelope.)
    env = _run(monkeypatch, _Proc(returncode, stderr=raw))
    assert env["status"] == "refused"
    assert env["refusal"]["kind"] == expected_kind, env
    assert "data" not in env
    if secret is not None:
        assert secret not in json.dumps(env), (
            f"raw token {secret!r} leaked into the envelope: {env}"
        )


@pytest.mark.parametrize(
    "returncode,expected_kind",
    [(2, "dsn"), (3, "driver_missing"), (4, "auth"), (5, "syntax"), (99, "other")],
)
def test_operational_error_exit_code_prior(monkeypatch, returncode, expected_kind):
    # With no recognizable stderr text, classification falls back to the exit-code prior.
    env = _run(monkeypatch, _Proc(returncode, stderr=""))
    assert env["status"] == "refused"
    assert env["refusal"]["kind"] == expected_kind, env
