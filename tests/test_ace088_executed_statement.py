"""SC-6: the receipt describes the statement that RAN, not the statement the caller sent.

`_model_safety` may hand back a statement that is not the one it was given, and `execute_guarded`
rebinds its local `sql` to whatever comes back. The `ok` receipt is built from that rebound value, so
the IN-PROCESS path already describes what ran, and the job here is to measure it.

Measuring it needs a statement whose executed form differs from its received form — and the guard is
in the middle of subtracting every mechanism that produced one. ACE-042 deleted
`apply_default_filters`, which used to AND each in-scope table's declared filter into the WHERE; the
fan/chasm pre-flight's `auto_rewrite` branch, which drops a fan-out join and takes a whole table with
it, is the last one standing and ACE-093 deletes that too. A test founded on either is a test a later
slice quietly makes vacuous, which is exactly what happened to
`test_the_receipt_describes_the_statement_that_ran` when ACE-042 landed: it went on asserting a
precondition that had stopped being true.

So the divergence the in-process test measures is now its OWN. It wraps `_model_safety` and
substitutes a statement that drops a table AND adds a column — the two directions the two real
rewrites moved a statement between them — and no future subtraction can take that divergence away,
because the test owns it. The property under test is unchanged: the receipt describes the string the
executor was handed. A synthetic seam is also the only honest way left to state the property at all,
which is what ACE-093's byte-identity criterion already implies.

The fork path cannot be measured that way, and that is the whole point of the xfail below. `tools`
runs `python -m execute_sql` as a subprocess and rebuilds the Envelope from the child's exit code and
stderr, so the receipt the child assembled is destroyed at the process boundary; the parent assembles
its own from the only statement it holds, which is the one the CALLER sent. A monkeypatch in this
process does not cross the fork, so the only divergence that reaches the child is a REAL one. The
xfail is therefore founded on the surviving `auto_rewrite` branch and on nothing else, and it is
deleted by the slice that deletes the branch.

**The gap is `ok`-only, and that is not a narrowing of the property but a consequence of where the
full receipt now lives.** Every NON-ok receipt is built from the statement the caller SENT, on both
paths and on purpose: whatever the guard rewrites a statement INTO is the guard's own text, so a
refusal built from the rebound string can name a table the caller never wrote. So the two paths agree
by construction on `refused` and `failed`, and `ok` is the one status left where the receipt is asked
to describe what executed. It has no top-level `receipt` on the wire yet, so both measurements below
read `Envelope.receipt` through a spy on `_emit` — which is where the contract states the property
anyway.
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

# The one statement a REAL rewrite still changes, which is what the fork tests need. It aggregates a
# measure on the ONE side of a declared one-to-many and touches the many side nowhere but the ON
# clause, which is exactly the shape the pre-flight auto-rewrites rather than refuses. The rewrite
# happens inside the child too, so it is the only divergence that survives a process boundary — and
# ACE-093 deletes it.
FAN_SQL = "SELECT SUM(o.total) FROM orders o JOIN order_items i ON i.order_id = o.id"

# The synthetic pair the in-process test owns, and the reason it outlives the rewrites. `RECEIVED_SQL`
# projects from the many side rather than aggregating across it, so the pre-flight has nothing to
# rewrite and the real safety pass hands it back untouched; the `diverged` seam then substitutes
# `EXECUTED_SQL` for it on the way to the executor. The substitution drops a table and adds a column,
# which is what `auto_rewrite` and the deleted `apply_default_filters` did between them.
RECEIVED_SQL = "SELECT o.total, i.qty FROM orders o JOIN order_items i ON i.order_id = o.id"
EXECUTED_SQL = "SELECT o.total FROM orders o WHERE o.deleted_at IS NULL"

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
            columns=["total"], rows=[(7,)], truncated=False
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
    """A two-table model that arms the fan rewrite and declares every name the two statements use.

    Shaped after `test_semantic_model_cli.py::_model`, which is the fixture the suite already uses to
    drive `auto_rewrite`, plus the schema and the disk layout `test_ace035_no_enumeration.py` proves
    resolves end to end behind a real warehouse. Copied rather than imported, for the reason
    `test_ace035_gate_verdict_parity.py` gives: the fixture is the spec of what each assertion here
    means, so it must not be re-pointed by an edit to another test file.

    `order_items` is the MANY side of a declared many-to-one, and `FAN_SQL` reads it nowhere outside
    the ON clause, so the pre-flight drops that join instead of refusing. `orders.deleted_at` is
    declared but nothing applies it: it is the column the synthetic seam adds, and it has to be in
    the model for the receipt to resolve it to `public.orders.deleted_at` rather than report it
    against no table at all.
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
def shop(tmp_path, monkeypatch):
    """The model above under profile `acme`, plus a real warehouse behind it.

    The warehouse holds `orders` and the three columns the model declares for it, and no
    `order_items` at all. That asymmetry is what the fork tests run on: the pre-flight's rewrite
    drops `order_items` from `FAN_SQL`, so what the child actually executes is a statement the
    warehouse can serve and both routes reach `ok` — the one status whose receipt is asked to describe
    what executed. Once ACE-093 deletes that rewrite, executed and received converge on a statement
    naming `order_items`, both routes come back `failed`, and both receipts are the bounded receipt of
    the same received string — equal, which is exactly the alarm the strict xfail below exists to
    raise.
    """
    artifacts = tmp_path / "artifacts"
    _write_model(artifacts / PROFILE)

    warehouse = tmp_path / "warehouse.db"
    con = sqlite3.connect(warehouse)
    con.execute("CREATE TABLE orders (id INTEGER, total NUMERIC, deleted_at TIMESTAMP)")
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


@pytest.fixture()
def diverged(monkeypatch) -> None:
    """Make the statement that reaches the executor differ from the one the caller sent, by this
    file's own hand rather than by a production rewrite.

    The seam is `_model_safety`, because that is where the hand-off actually happens:
    `execute_guarded` rebinds its local `sql` to the statement that comes back and builds the `ok`
    receipt from the rebound value. Wrapping it — rather than replacing it — is deliberate and
    load-bearing twice over. The real pass still runs, so it still publishes the resolved model that
    `_receipt_for` reads out of `_guard_model`; and a statement it REFUSES keeps its own string, so
    the seam can never turn a refusal into an execution. Only the statement of a cleared call is
    diverted.

    Owning the divergence here is what makes the test outlive the guard's rewrites. ACE-042 already
    deleted `apply_default_filters` and ACE-093 deletes the fan-join `auto_rewrite`; a test that
    borrowed either one would go quiet the day it went away, and one of them did exactly that.
    """
    real = execute_sql._model_safety

    def _rewriting_model_safety(
        sql: str, profile: str, area: str | None
    ) -> tuple[str, execute_sql.Refusal | int | None]:
        vetted, verdict = real(sql, profile, area)
        return (EXECUTED_SQL if verdict is None else vetted), verdict

    monkeypatch.setattr(execute_sql, "_model_safety", _rewriting_model_safety)


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


def _both_routes(monkeypatch, sql: str) -> tuple[dict, dict, list[dict]]:
    """Run `sql` down both routes and return `(in_process_body, forked_body, [receipts])`.

    The receipts are read off the `Envelope` through a spy on `_emit`, not off the wire: the `ok`
    body carries no top-level `receipt` yet, because `_emit` builds it as `{"status": "ok",
    **payload}` and `payload` already has a legacy `"receipt"` key the chart template reads. The
    contract states the property on the TYPE, which is what this measures.
    """
    seen: list = []
    real = tools._emit
    monkeypatch.setattr(tools, "_emit", lambda env, **kw: (seen.append(env), real(env, **kw))[1])

    tools.set_injected_executor(execute_sql.BUILTIN_EXECUTOR)
    in_process = _tool_out(sql)
    tools.set_injected_executor(None)
    forked = _tool_out(sql)

    return in_process, forked, [asdict(env.receipt) for env in seen]


# ---------------------------------------------------------------------------
# The in-process path: the receipt describes what the executor was handed
# ---------------------------------------------------------------------------


def test_the_receipt_describes_the_statement_that_ran(shop, diverged):
    """SC-6, on the path that can hold it today.

    The statement the executor receives is not the statement the caller sent: the `diverged` seam has
    handed `execute_guarded` a different one, dropping a table and adding a column the way the guard's
    own rewrites used to. `execute_guarded` rebinds `sql` to that string before it builds anything, so
    the receipt reports a table set, a join set and a column set that only the EXECUTED string
    produces.

    Each of the three assertions fails if the receipt is ever rebuilt from the caller's string, and
    they fail in different directions: the caller's statement names a table the executed one does
    not, reaches a declared relationship the executed one does not, and lacks a column the executed
    one has. A receipt built from the wrong string cannot satisfy any of them, let alone all three.
    """
    spy = _SpyExecutor()
    env = execute_sql.execute_guarded(RECEIVED_SQL, PROFILE, AREA, executor=spy)

    assert env.status == "ok", env
    executed = spy.calls[0][0]
    # The divergence really reached the executor, in both directions, so the two strings genuinely
    # differ — and it reached it through the same rebinding a production rewrite goes through.
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
        "caller sent, while the child executes the rewritten one. ACE-042 has landed, so the "
        "default-filter injection is already gone; the fan-join auto_rewrite is the last mechanism "
        "that still makes the two strings differ, and ACE-093 deletes it — after which executed == "
        "received by construction and this marker is deleted by that slice."
    ),
)
def test_the_forked_receipt_describes_the_statement_the_child_ran(shop, monkeypatch):
    """The half of SC-6 that cannot hold until the last rewrite is gone, measured rather than
    asserted in prose. Closed by ACE-093, which deletes it.

    Founded on the REAL rewrite, and it has to be: a monkeypatched seam lives in this process and the
    child is another one, so the synthetic divergence the in-process test owns cannot reach the
    statement the child executes. `FAN_SQL` is therefore the statement here, and the pre-flight's
    `auto_rewrite` branch is the only mechanism left that changes it on both sides of the fork.

    Both routes are real: the in-process one runs `execute_guarded` in this process behind the
    built-in executor, and the forked one actually spawns `python -m execute_sql` and rebuilds the
    Envelope from what the child wrote. Both reach the same warehouse and both come back `ok`, which
    is the one status whose receipt is asked to describe the executed statement — every non-ok
    receipt is deliberately built from the RECEIVED statement on both paths, so those agree by
    construction and have nothing left to measure.

    The property asserted is that the two paths describe the same statement, and it is the fork half
    of "the receipt describes the executed statement" because the in-process half is anchored by
    `test_the_receipt_describes_the_statement_that_ran` directly above. That test pins a property of
    the chokepoint rather than of one statement — the `ok` receipt is built from whatever the
    executor was handed — so it holds for `FAN_SQL` too, and a fork receipt equal to the in-process
    one is therefore a fork receipt describing what ran. Stating it as a parity check rather than as a
    literal table list is what lets this marker do its job: when the rewrite goes, executed and
    received converge and this assertion starts passing, and a strict xfail that passes is a CI
    error, which is exactly the alarm it is here to raise.

    It fails today because the parent describes the received statement: its receipt still names
    `order_items` and the relationship reaching it, and the in-process one names neither.
    """
    in_process, forked, receipts = _both_routes(monkeypatch, FAN_SQL)

    assert in_process["status"] == forked["status"] == "ok", (in_process, forked)
    assert receipts[1] == receipts[0]


def test_the_two_paths_reach_the_same_outcome_for_the_same_statement(shop, monkeypatch):
    """The precondition the xfail above rests on, asserted separately so a change that stops either
    route from reaching an `ok` outcome is a plain failure here rather than an xfail that keeps
    passing for a reason nobody intended.

    It also pins that the receipt is the contract type's own five-section shape, and that the
    divergence it measures is real rather than an artifact of comparing two receipts that establish
    nothing.
    """
    in_process, forked, receipts = _both_routes(monkeypatch, FAN_SQL)

    assert in_process["status"] == forked["status"] == "ok", (in_process, forked)
    for receipt in receipts:
        assert set(receipt) == {"model_version", *execute_sql.Receipt.SECTIONS}
    # The in-process one carries the facts the EXECUTED statement produces: the dropped table is gone.
    assert [item["ref"] for item in receipts[0]["tables"]["items"]] == ["orders"]
    # The forked one still carries the caller's own FROM/JOIN list, which is the gap.
    assert [item["ref"] for item in receipts[1]["tables"]["items"]] == ["orders", DROPPED_TABLE]
    assert asdict(execute_sql.undetermined_receipt("x")) != receipts[0]
