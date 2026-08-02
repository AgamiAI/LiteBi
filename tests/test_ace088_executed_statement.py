"""SC-6: the receipt describes the statement that RAN, not the statement the caller sent.

`_model_safety` may hand back a statement that is not the one it was given, and `execute_guarded`
rebinds its local `sql` to whatever comes back. Two mechanisms rewrite it today, and one statement
drives both:

  * the fan/chasm pre-flight's `auto_rewrite` branch drops the fan-out join, so a whole table and
    the declared relationship reaching it leave the statement (deleted by ACE-093);
  * `apply_default_filters` ANDs each in-scope table's declared filter into the WHERE, so a column
    the caller never named enters it (deleted by ACE-042, which ACE-093 depends on).

Every receipt `execute_guarded` builds is built from the rebound value, so the IN-PROCESS path
already describes what ran. `test_the_receipt_describes_the_statement_that_ran` asserts that against
the string a spy executor actually received, and it would fail loudly if the receipt were ever
rebuilt from the caller's string instead.

The fork path cannot do the same thing. `tools` runs `python -m execute_sql` as a subprocess and
rebuilds the Envelope from the child's exit code and stderr, so the receipt the child assembled is
destroyed at the process boundary; the parent assembles its own from the only statement it holds,
which is the one the CALLER sent. After a rewrite the two paths therefore describe two different
statements. That gap is pinned below as a strict xfail rather than left as prose, because a gap
nobody measures is a gap nobody closes.
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
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402
import tools  # noqa: E402

PROFILE = "acme"
AREA = "sales"

# One statement, both rewrites. It aggregates a measure on the ONE side of a declared one-to-many
# and touches the many side nowhere but the ON clause, which is exactly the shape the pre-flight
# auto-rewrites rather than refuses; and `orders` declares a default filter, which is applied to
# whatever the pre-flight left behind.
RECEIVED_SQL = "SELECT SUM(o.total) FROM orders o JOIN order_items i ON i.order_id = o.id"

# The two names that separate the executed statement from the received one, in opposite directions.
# `order_items` is in the caller's statement and gone from the executed one; `deleted_at` is in the
# executed one and absent from the caller's. Either name alone would let a receipt built from the
# wrong string pass half the assertions below, so both are asserted.
DROPPED_TABLE = "order_items"
INJECTED_COLUMN = "deleted_at"


class _SpyExecutor:
    """Records what reached the connect-and-run step, so a test can assert on the exact string the
    executor was handed rather than on a string it reconstructed itself."""

    def __init__(self, result: execute_sql.ExecResult | None = None):
        self.calls: list[tuple[str, dict, str]] = []
        self._result = result or execute_sql.ExecResult(
            columns=["sum"], rows=[(7,)], truncated=False
        )

    def execute(self, vetted_sql: str, creds: dict, *, profile: str) -> execute_sql.ExecResult:
        self.calls.append((vetted_sql, creds, profile))
        return self._result


@pytest.fixture(autouse=True)
def _isolate():
    """`_max_rows_override` is a request-scoped ContextVar and `_INJECTED_EXECUTOR` a process
    global. The fork test below needs the latter to be `None` for a real subprocess to run at all,
    so leaking an executor into it would silently turn it into a second in-process test."""
    execute_sql._max_rows_override.set(None)
    tools.set_injected_executor(None)
    yield
    execute_sql._max_rows_override.set(None)
    tools.set_injected_executor(None)


def _write_model(root: Path) -> None:
    """A two-table model that arms both rewrites at once.

    Shaped after `test_semantic_model_cli.py::_model`, which is the fixture the suite already uses
    to drive `auto_rewrite` and `default_filters`, plus the schema and the disk layout
    `test_ace035_no_enumeration.py` proves resolves end to end behind a real warehouse. Copied
    rather than imported, for the reason `test_ace035_gate_verdict_parity.py` gives: the fixture is
    the spec of what each assertion here means, so it must not be re-pointed by an edit to another
    test file.

    `order_items` is the MANY side of a declared many-to-one, and the statement reads it nowhere
    outside the ON clause, so the pre-flight drops that join instead of refusing. `orders` declares
    a default filter over `deleted_at`, which is then ANDed into whatever the pre-flight left.
    """
    import yaml

    (root / "subject_areas" / AREA / "tables").mkdir(parents=True)
    (root / "datasource.yaml").write_text(
        yaml.safe_dump({"datasource": "Shop", "version": 1,
                        "subject_areas": [f"subject_areas/{AREA}"]})
    )
    (root / "subject_areas" / AREA / "subject_area.yaml").write_text(
        yaml.safe_dump({"name": AREA, "tables": [
            {"storage_connection": "c", "schema": "public", "table": "orders"},
            {"storage_connection": "c", "schema": "public", "table": "order_items"}]})
    )
    (root / "subject_areas" / AREA / "tables" / "orders.yaml").write_text(
        yaml.safe_dump({
            "name": "orders", "schema": "public", "storage_connection": "c", "grain": ["id"],
            "description": "orders",
            "default_filters": ["{alias}.deleted_at IS NULL"],
            "columns": [
                {"name": "id", "type": "integer", "primary_key": True},
                {"name": "deleted_at", "type": "timestamp"},
                {"name": "total", "type": "decimal"},
            ],
        })
    )
    (root / "subject_areas" / AREA / "tables" / "order_items.yaml").write_text(
        yaml.safe_dump({
            "name": "order_items", "schema": "public", "storage_connection": "c", "grain": ["id"],
            "description": "order items",
            "columns": [
                {"name": "id", "type": "integer", "primary_key": True},
                {"name": "order_id", "type": "integer"},
                {"name": "qty", "type": "integer"},
            ],
        })
    )
    (root / "subject_areas" / AREA / "relationships.yaml").write_text(
        yaml.safe_dump({"relationships": [{
            "from_table": "order_items", "from_column": "order_id",
            "to_table": "orders", "to_column": "id",
            "from_schema": "public", "to_schema": "public",
            "relationship": "many_to_one", "confidence": "confirmed",
            "review_state": "approved", "signed_off_by": "you@example.com",
            "signed_off_role": "data_owner", "signed_off_at": "2026-01-01T00:00:00Z"}]})
    )


@pytest.fixture()
def rewritten(tmp_path, monkeypatch):
    """The model above under profile `acme`, plus a real warehouse behind it.

    The warehouse declares `orders` and NOTHING else: no `deleted_at` column and no `order_items`
    table. That is deliberate and it is what keeps the fork test below honest through the specs that
    delete the rewrites. Today the child runs the rewritten statement and SQLite rejects the
    injected `deleted_at`; once both rewrites are gone the child runs the caller's statement and
    SQLite rejects the missing `order_items`. Either way the outcome is `failed`, which is the
    status whose receipt is on the wire, so the test keeps measuring the receipt rather than
    changing shape underneath itself.
    """
    artifacts = tmp_path / "artifacts"
    _write_model(artifacts / PROFILE)

    warehouse = tmp_path / "warehouse.db"
    con = sqlite3.connect(warehouse)
    con.execute("CREATE TABLE orders (id INTEGER, total NUMERIC)")
    con.commit()
    con.close()

    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(artifacts))
    monkeypatch.setenv("DATASOURCE_URL__ACME", f"sqlite:///{warehouse}")
    # Local, not hosted: the disk model is the one the gates read and the one the receipt is built
    # against. A stray inherited app-database URL would flip both onto the hosted branch.
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    monkeypatch.delenv("AGAMI_ORG_ID", raising=False)
    monkeypatch.delenv("AGAMI_SQL_TIMEOUT_S", raising=False)
    return SimpleNamespace(artifacts=artifacts, warehouse=warehouse)


def _column_names(items) -> list[str]:
    """The qualified column names a receipt's `columns` section carries, dropping the entries that
    are a statement-level metric match rather than a column (those have no owning column today)."""
    return [item["column"] for item in items if item["column"]]


def _tool_out(sql: str) -> dict:
    """One `tool_execute_sql` call, deserialized. Which execution path serves it is decided by
    `_INJECTED_EXECUTOR`, which each caller below sets for itself."""
    return json.loads(
        tools.tool_execute_sql({"sql": sql, "datasource": PROFILE, "area": AREA})
    )


# ---------------------------------------------------------------------------
# The in-process path: the receipt describes what the executor was handed
# ---------------------------------------------------------------------------


def test_the_receipt_describes_the_statement_that_ran(rewritten):
    """SC-6, on the path that can hold it today.

    The statement the executor receives is not the statement the caller sent: the pre-flight has
    dropped the fan-out join and the default filter has been ANDed in. `execute_guarded` rebinds
    `sql` to that string before it builds anything, so the receipt reports a table set, a join set
    and a column set that only the EXECUTED string produces.

    Each of the three assertions fails if the receipt is ever rebuilt from the caller's string, and
    they fail in different directions: the caller's statement names a table the executed one does
    not, reaches a declared relationship the executed one does not, and lacks a column the executed
    one has. A receipt built from the wrong string cannot satisfy any of them, let alone all three.
    """
    spy = _SpyExecutor()
    env = execute_sql.execute_guarded(RECEIVED_SQL, PROFILE, AREA, executor=spy)

    assert env.status == "ok", env
    executed = spy.calls[0][0]
    # Both rewrites really fired, in opposite directions, so the two strings genuinely differ.
    assert DROPPED_TABLE in RECEIVED_SQL and DROPPED_TABLE not in executed
    assert INJECTED_COLUMN not in RECEIVED_SQL and INJECTED_COLUMN in executed

    # The dropped table is gone from the receipt, so the receipt is not describing the caller's
    # FROM/JOIN list.
    assert [item["ref"] for item in env.receipt.tables.items] == ["orders"]
    # And with it the declared relationship, whose other endpoint is no longer in scope. This is the
    # loudest of the three: a join the answer did not traverse must not be reported as one it did.
    assert env.receipt.joins.items == ()
    # The injected predicate's column is in the receipt, so the receipt is not describing the
    # caller's WHERE clause either.
    assert _column_names(env.receipt.columns.items) == [
        "public.orders.deleted_at",
        "public.orders.total",
    ]


# ---------------------------------------------------------------------------
# The fork path: the parent describes the statement it was given
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "The fork path builds the receipt in the PARENT, which only ever holds the statement the "
        "caller sent, while the child executes the rewritten one. ACE-093 deletes the fan-join "
        "rewrite and ACE-042 (which it depends on) deletes the default-filter injection, making "
        "executed == received by construction; this marker is deleted by that slice."
    ),
)
def test_the_forked_receipt_describes_the_statement_the_child_ran(rewritten):
    """The half of SC-6 that cannot hold until the rewrites are gone, measured rather than asserted
    in prose. Closed by ACE-093 (with ACE-042, which it depends on).

    Both routes are real: the in-process one runs `execute_guarded` in this process behind the
    built-in executor, and the forked one actually spawns `python -m execute_sql` and rebuilds the
    Envelope from what the child wrote. Both reach the same warehouse and both come back `failed`,
    which is the status whose receipt is on the wire today.

    The property asserted is that the two paths describe the same statement, and it is the fork half
    of "the receipt describes the executed statement" because the in-process half is anchored by
    `test_the_receipt_describes_the_statement_that_ran` directly above: that test pins the
    in-process receipt to the string the executor was handed, so a fork receipt equal to it is a
    fork receipt describing what ran. Stating it as a parity check rather than as a literal table
    list is what lets this marker do its job: when the rewrites go, executed and received converge
    and this assertion starts passing, and a strict xfail that passes is a CI error, which is
    exactly the alarm it is here to raise.

    It fails today because the parent describes the received statement: its receipt still names
    `order_items` and the relationship reaching it, and still has no `deleted_at`.
    """
    tools.set_injected_executor(execute_sql.BUILTIN_EXECUTOR)
    in_process = _tool_out(RECEIVED_SQL)

    tools.set_injected_executor(None)
    forked = _tool_out(RECEIVED_SQL)

    assert in_process["status"] == forked["status"] == "failed", (in_process, forked)
    assert forked["receipt"] == in_process["receipt"]


def test_the_two_paths_reach_the_same_outcome_for_the_same_statement(rewritten):
    """The precondition the xfail above rests on, asserted separately so a change that stops either
    route from reaching a `failed` outcome is a plain failure here rather than an xfail that keeps
    passing for a reason nobody intended.

    It also pins that the receipt is on the wire at all on that status, and that it is the contract
    type's own five-section shape rather than the flat legacy dict the ok payload still nests.
    """
    tools.set_injected_executor(execute_sql.BUILTIN_EXECUTOR)
    in_process = _tool_out(RECEIVED_SQL)

    tools.set_injected_executor(None)
    forked = _tool_out(RECEIVED_SQL)

    assert in_process["status"] == forked["status"] == "failed", (in_process, forked)
    for body in (in_process, forked):
        assert set(body["receipt"]) == {"model_version", *execute_sql.Receipt.SECTIONS}
    # And the divergence is real rather than an artifact of comparing two empty receipts: the
    # in-process one carries the facts the executed statement produces.
    assert [item["ref"] for item in in_process["receipt"]["tables"]["items"]] == ["orders"]
    assert asdict(execute_sql.undetermined_receipt("x")) != in_process["receipt"]
