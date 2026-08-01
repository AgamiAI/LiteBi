"""A database error is classified from driver text that never crosses the boundary.

The failure channel was the one place the model could still leak. A guardrail refusal is
built from static prose and echoes only what the caller sent; `failure.message` was relayed
from the driver verbatim, and PostgreSQL volunteers declared column names in a `HINT`.

Classifying FROM driver text is not the same as returning it. The output is one of a fixed
set of labels and the caller receives `_ERROR_MESSAGES[kind]`, so the caller still learns
enough to act (a missing column is not a syntax error) while learning nothing about the
schema.

Two properties here are easy to get wrong and silent when wrong:

* **The authored/driver split.** Codes 2 and 3 are text this module wrote and must keep being
  relayed, or an operator loses the one line telling them to set `DATASOURCE_URL`. Codes 4
  and 5 carry the driver's exception and must not be.
* **Fork parity.** The child classifies correctly and encodes the kind as an exit code. A
  test that only exercises the in-process path cannot see the default surface being wrong.
"""

from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import subprocess
import sys

import execute_sql
import guardrail
import pytest
import tools

PROFILE = "acme"

# (driver text, exit code, expected kind). The text is real driver output, lower-cased matching
# aside, because the classifier's whole job is to read what engines actually emit.
_DRIVER_ERRORS = [
    ('column "amount" does not exist\nHINT:  Perhaps you meant "orders.internal_ref".', 5, "column_not_found"),
    ("psycopg2.errors.UndefinedColumn: no such column: secret_col", 5, "column_not_found"),
    ("Unknown column 'ssn' in 'field list'", 5, "column_not_found"),
    ('relation "payroll" does not exist', 5, "table_not_found"),
    ("no such table: internal_ledger", 5, "table_not_found"),
    ("Object 'FINANCE.SALARIES' does not exist or not authorized.", 5, "table_not_found"),
    ("permission denied for table employee_comp", 5, "permission"),
    ("SELECT command denied to user 'app'@'%' for table 'payroll'", 5, "permission"),
    ('syntax error at or near "SELCT"', 5, "syntax"),
    ("You have an error in your SQL syntax; check the manual", 5, "syntax"),
    ("could not translate host name \"warehouse.internal\" to address", 4, "dsn"),
    ("connection refused", 4, "network"),
    ("password authentication failed for user \"analytics\"", 4, "auth"),
]

# Ceded to ACE-038: a deadline is classified from a watchdog signal, never a driver string.
_CANCELLATIONS = [
    "canceling statement due to statement timeout",
    "ERROR:  canceling statement due to user request",
    "query was canceled",
    "psycopg2.extensions.QueryCanceledError",
    "Lost connection to MySQL server during query",
]


class _Raiser:
    """An adapter that fails the way a real one does: an ExecutorError, no special flag."""

    def __init__(self, message: str, code: int) -> None:
        self._message, self._code = message, code

    def execute(self, vetted_sql, creds, *, profile):  # noqa: ANN001, ANN201
        raise execute_sql.ExecutorError(self._message, code=self._code)


@pytest.fixture
def warehouse(tmp_path, monkeypatch):
    """A real sqlite datasource both processes can reach, so the fork path is drivable."""
    db = tmp_path / "warehouse.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE orders (id INTEGER)")
    conn.commit()
    conn.close()
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setenv(f"DATASOURCE_URL__{PROFILE.upper()}", f"sqlite:///{db}")
    monkeypatch.setattr(execute_sql, "_model_safety", lambda s, p, a: (s, None))
    return tmp_path


@pytest.mark.parametrize(("text", "code", "kind"), _DRIVER_ERRORS, ids=lambda v: str(v)[:44])
def test_each_kind_is_classified_from_text_that_never_crosses(text, code, kind, warehouse):
    env = execute_sql.execute_guarded(
        "SELECT id FROM orders", PROFILE, None, executor=_Raiser(text, code), no_safety=True
    )
    assert env.status == "failed"
    assert env.failure.kind == kind
    assert env.failure.message == execute_sql._ERROR_MESSAGES[kind]
    # The point of the slice: no fragment of the driver's text survives into the message.
    for token in ("amount", "internal_ref", "secret_col", "ssn", "payroll", "SALARIES",
                  "employee_comp", "internal_ledger", "SELCT", "warehouse.internal", "analytics"):
        assert token not in env.failure.message


@pytest.mark.parametrize("text", _CANCELLATIONS, ids=lambda s: s[:44])
def test_no_cancellation_signature_produces_timeout(text, warehouse):
    """Ceded to ACE-038, and NOT by deleting the arm.

    Deleting it would not leave these unclassified, it would leave them mis-classified: the
    text contains "timed out" or "lost connection", which earlier drafts routed to `network`,
    and failing that it falls to the exit-5 prior and reads as `syntax`. An unattributable
    server-side cancellation is honestly `other`.
    """
    env = execute_sql.execute_guarded(
        "SELECT id FROM orders", PROFILE, None, executor=_Raiser(text, 5), no_safety=True
    )
    assert env.failure.kind != "timeout"
    assert env.failure.kind == "other"


def test_a_connect_timeout_is_still_auth(warehouse):
    """The needle removal, locked from this side.

    Porting the reference's `network` needles verbatim would have added "timed out" and
    silently moved every driver connect timeout from `auth` to `network`, contradicting the
    contract's account of what each kind means.
    """
    env = execute_sql.execute_guarded(
        "SELECT id FROM orders", PROFILE, None,
        executor=_Raiser("connection to server timed out", 4), no_safety=True,
    )
    assert env.failure.kind == "auth"


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("No warehouse credentials for profile [acme]. Set DATASOURCE_URL…", 2),
        ("pymysql not installed. Run: pip install pymysql", 3),
    ],
)
def test_text_this_module_authored_is_still_relayed(text, code, warehouse):
    """The other half of the split, and the half a blanket sanitizer would break.

    These name an operator action and contain nothing the database said. Sanitizing them
    would leave an operator with a classified kind and no idea what to do about it.
    """
    env = execute_sql.execute_guarded(
        "SELECT id FROM orders", PROFILE, None, executor=_Raiser(text, code), no_safety=True
    )
    assert env.failure.message == text


def test_a_failure_carries_no_remediation(warehouse):
    """Enforced by the type, not by discipline — `Failure` has no such field.

    That absence is what lets a caller tell a decision of ours (which always names its fix)
    from the database's outcome (which we can only relay).
    """
    env = execute_sql.execute_guarded(
        "SELECT id FROM orders", PROFILE, None,
        executor=_Raiser('column "x" does not exist', 5), no_safety=True,
    )
    assert set(env.failure.__dict__ if hasattr(env.failure, "__dict__") else
               env.failure._asdict() if hasattr(env.failure, "_asdict") else
               {"kind": env.failure.kind, "message": env.failure.message}) == {"kind", "message"}
    assert not hasattr(env.failure, "remediation")


def test_the_same_driver_error_yields_the_same_kind_in_process_and_forked(warehouse, monkeypatch):
    """FORK PARITY — the criterion S2's exit codes exist to make satisfiable.

    Before the exit-code table was widened, this reported `column_not_found` in-process and
    `other` through the fork, and no in-process-only test could see it. The statement asks for
    a column the sqlite warehouse does not have, so the database itself produces the error on
    both routes; nothing is stubbed on the fork side.
    """
    sql = "SELECT missing_column FROM orders"

    tools.set_injected_executor(execute_sql.BUILTIN_EXECUTOR)
    try:
        in_process = json.loads(tools.tool_execute_sql({"sql": sql, "datasource": PROFILE}))
    finally:
        tools.set_injected_executor(None)

    forked = json.loads(tools.tool_execute_sql({"sql": sql, "datasource": PROFILE}))

    assert in_process["status"] == forked["status"] == "failed"
    assert in_process["failure"]["kind"] == forked["failure"]["kind"] == "column_not_found"
    assert in_process["failure"]["message"] == forked["failure"]["message"]
    # The response echoes the caller's own `sql` back, which discloses nothing it did not
    # send. What must not appear is the driver's text, so scan the message specifically.
    assert "missing_column" not in forked["failure"]["message"]
    assert "no such column" not in forked["failure"]["message"].lower()


def test_the_forked_child_writes_no_raw_driver_text_to_stderr(warehouse):
    """Driven as a real subprocess, because the leak this guards is a transport property.

    The parent relays the child's stderr into `failure.message` for any classified code, so
    anything the child prints there reaches the caller. The child must print the sanitized
    line and nothing else.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "execute_sql", "--profile", PROFILE,
         "--sql", "SELECT missing_column FROM orders"],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "AGAMI_ARTIFACTS_DIR": str(warehouse)},
        cwd=str(pathlib.Path(execute_sql.__file__).parent),
    )
    assert proc.returncode == guardrail_exit_for("column_not_found")
    assert "missing_column" not in proc.stderr
    assert proc.stderr.strip() == execute_sql._ERROR_MESSAGES["column_not_found"]


def guardrail_exit_for(kind: str) -> int:
    return execute_sql.FAILURE_KIND_TO_EXIT[kind]


def test_every_classified_kind_has_a_message() -> None:
    """A kind the classifier can return but the message table lacks would fall back to the
    generic unexpected-failure text, which reads as a bug rather than a database error."""
    classifiable = set(guardrail._FAILURE_KINDS) - {"timeout", "other"}
    assert classifiable == set(execute_sql._ERROR_MESSAGES)
