"""SC-6: the receipt describes the statement that RAN, not the statement the caller sent.

`_model_safety` may hand back a statement that is not the one it was given, and `execute_guarded`
rebinds its local `sql` to whatever comes back. The `ok` receipt is built from that rebound value, so
the IN-PROCESS path already describes what ran, and the job here is to measure it.

Measuring it needs a statement whose executed form differs from its received form, and no production
mechanism produces one any more. ACE-042 deleted `apply_default_filters`, which used to AND each
in-scope table's declared filter into the WHERE; the fan/chasm pre-flight's `auto_rewrite` branch,
which dropped a fan-out join and took a whole table with it, was the last one standing and it is gone
too. A test founded on either is a test a later slice quietly makes vacuous, which is exactly what
happened to `test_the_receipt_describes_the_statement_that_ran` when ACE-042 landed: it went on
asserting a precondition that had stopped being true.

So the divergence the in-process test measures is its OWN. It wraps `_model_safety` and substitutes a
statement that drops a table AND adds a column — the two directions the two real rewrites moved a
statement between them — and no future subtraction can take that divergence away, because the test
owns it. The property under test is unchanged: the receipt describes the string the executor was
handed. A synthetic seam is now the only honest way to state the property at all, since executed and
received are otherwise the same string by construction.

The fork path cannot use that seam. `tools` runs `python -m execute_sql` as a subprocess and rebuilds
the Envelope from the child's exit code and stderr, so the receipt the child assembled is destroyed at
the process boundary; the parent assembles its own from the only statement it holds, which is the one
the CALLER sent. A monkeypatch in this process does not cross the fork. While a real rewrite existed
the two therefore described different statements, and that gap was carried here as a `strict=True`
xfail; it closed by subtraction rather than by plumbing, and the two fork tests below now assert the
parity directly.

**The property is `ok`-only, and that is not a narrowing of it but a consequence of where the full
receipt lives.** Every NON-ok receipt is built from the statement the caller SENT, on both paths and
on purpose: whatever a guard rewrites a statement INTO is the guard's own text, so a refusal built
from a rebound string could name a table the caller never wrote. The two paths therefore agree by
construction on `refused` and `failed`, and `ok` is the one status left where the receipt is asked to
describe what executed. Both measurements below read `Envelope.receipt` through a spy on `_emit`,
which is where the contract states the property.
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

# A genuine fan trap: it aggregates a measure on the ONE side of a declared one-to-many and touches
# the many side nowhere but the ON clause. That shape used to be auto-rewritten rather than refused,
# which made it the one divergence that survived a process boundary and the reason the fork test
# below was an xfail. It is REFUSED now, so it no longer reaches an executed statement at all — the
# fork tests moved to `RECEIVED_SQL`, and what this statement proves is refusal parity across the
# fork, in tests/test_ace093_byte_identity.py.
FAN_SQL = "SELECT SUM(o.total) FROM orders o JOIN order_items i ON i.order_id = o.id"

# The statement that clears the pre-flight untouched, which two different tests need for two reasons.
# It projects from the many side rather than aggregating across it, so there is no trap to refuse and
# the real safety pass hands it back unchanged.
#
#   - the in-process test pairs it with `EXECUTED_SQL` through the `diverged` seam, which substitutes
#     one for the other on the way to the executor: a synthetic divergence that drops a table and adds
#     a column, which is what `auto_rewrite` and the deleted `apply_default_filters` did between them;
#   - the two fork tests run it with NO seam, because a monkeypatch does not cross a process boundary.
#     Both routes execute it as written and their receipts must agree.
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
    """A two-table model that arms the fan detector and declares every name the statements below use.

    Shaped after `test_semantic_model_cli.py::_model`, plus the schema and the disk layout
    `test_ace035_no_enumeration.py` proves resolves end to end behind a real warehouse. Copied rather
    than imported, for the reason `test_ace035_gate_verdict_parity.py` gives: the fixture is the spec
    of what each assertion here means, so it must not be re-pointed by an edit to another test file.

    `order_items` is the MANY side of a declared many-to-one, which is what makes `FAN_SQL` a fan trap
    and therefore a refusal. `orders.deleted_at` is declared but nothing applies it: it is the column
    the synthetic seam adds, and it has to be in the model for the receipt to resolve it to
    `public.orders.deleted_at` rather than report it
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

    The warehouse holds both declared tables, so `FAN_SQL` runs as written and both routes reach
    `ok` — the one status whose receipt is asked to describe what executed.

    It used to hold `orders` alone. That asymmetry was load-bearing while the fan-join rewrite
    existed: the rewrite dropped `order_items` from `FAN_SQL`, so only the rewritten statement was
    servable, and the in-process receipt (built from the rewritten string) named one table while the
    forked receipt (built in the parent from the caller's string) named two. That divergence is what
    the strict xfail measured. The rewrite is gone, so both routes now execute the statement the
    caller sent; without `order_items` in the warehouse both would fail, and two bounded receipts of
    the same received string would compare equal for a reason that has nothing to do with the
    property under test. Adding the table keeps the comparison meaningful: the receipts are equal
    because both describe the same executed statement, which is the property itself.
    """
    artifacts = tmp_path / "artifacts"
    _write_model(artifacts / PROFILE)

    warehouse = tmp_path / "warehouse.db"
    con = sqlite3.connect(warehouse)
    con.execute("CREATE TABLE orders (id INTEGER, total NUMERIC, deleted_at TIMESTAMP)")
    con.execute("CREATE TABLE order_items (id INTEGER, order_id INTEGER, qty INTEGER)")
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

    Owning the divergence here is what makes the test outlive the guard's rewrites. Both real ones
    are now gone — `apply_default_filters` and then the fan-join `auto_rewrite` — and a test that
    borrowed either would have gone quiet the day it went away, as one of them did.
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

    The receipts are read off the `Envelope` through a spy on `_emit` rather than off the wire
    because the contract states the property on the TYPE, which is what this measures. The two are
    the same object now — `_emit` serializes `Envelope.receipt` onto every status, and the legacy
    `"receipt"` key inside the `ok` payload that used to shadow it is gone — so reading it here is a
    matter of measuring the property where it is declared, not of routing around the wire.
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


def test_the_forked_receipt_describes_the_statement_the_child_ran(shop, monkeypatch):
    """The fork half of "the receipt describes the executed statement", which held only by
    subtraction and now holds.

    This carried a `strict=True` xfail until the fan-join rewrite was deleted. The gap it measured
    was structural: `tools` runs `python -m execute_sql` as a subprocess and rebuilds the Envelope
    from the child's exit code and stderr, so the receipt the child assembled is destroyed at the
    process boundary and the parent assembles its own from the only statement it holds, which is the
    one the CALLER sent. While anything rewrote the statement in the child, that receipt described
    something that did not run — and since the flat legacy receipt inside the `ok` payload is gone,
    it was the ONLY description an `ok` caller on this path got.

    It closed by subtraction rather than by plumbing the child's receipt back across the wire.
    ACE-042 deleted the default-filter injection and the fan-join rewrite went the same way, so the
    parent's only statement and the child's executed statement are now the same string by
    construction. Nothing was added to the fork path to make this pass.

    Run with NO seam, deliberately. A monkeypatch lives in this process and the child is another one,
    so the synthetic divergence `test_the_receipt_describes_the_statement_that_ran` owns cannot reach
    what the child executes. Nothing else can either, which is the point: the assertion is that two
    independently assembled receipts of the same statement agree, and it has teeth because
    reintroducing any rewrite would make the in-process receipt describe the rewritten string while
    the parent's still describes the received one.

    It ran on `FAN_SQL` while that statement was auto-rewritten. It cannot now: a fan trap is refused,
    so it never produces the `ok` outcome this measures. `RECEIVED_SQL` clears the pre-flight
    untouched and names both declared tables, so both routes execute it and both receipts describe it.

    Stated as a parity check rather than a literal table list. The in-process receipt is built from
    whatever the executor was handed, which the test directly above pins as a property of the
    chokepoint; a fork receipt equal to it is therefore a fork receipt describing what ran.
    """
    in_process, forked, receipts = _both_routes(monkeypatch, RECEIVED_SQL)

    assert in_process["status"] == forked["status"] == "ok", (in_process, forked)
    assert receipts[1] == receipts[0]


def test_the_two_paths_reach_the_same_outcome_for_the_same_statement(shop, monkeypatch):
    """The precondition the xfail above rests on, asserted separately so a change that stops either
    route from reaching an `ok` outcome is a plain failure here rather than an xfail that keeps
    passing for a reason nobody intended.

    It also pins that the receipt is the contract type's own five-section shape, and that the parity
    above is not an artifact of comparing two receipts that establish nothing.

    The table list is asserted literally here rather than as a parity check, so a change that made
    BOTH receipts describe the wrong statement would fail here even though the parity assertion above
    would still pass. Both name the caller's own FROM/JOIN list, because that is now also the executed
    one — while the rewrite existed these two lines differed, which is what the xfail measured.
    """
    in_process, forked, receipts = _both_routes(monkeypatch, RECEIVED_SQL)

    assert in_process["status"] == forked["status"] == "ok", (in_process, forked)
    for receipt in receipts:
        assert set(receipt) == {"model_version", *execute_sql.Receipt.SECTIONS}
    for receipt in receipts:
        assert [item["ref"] for item in receipt["tables"]["items"]] == ["orders", DROPPED_TABLE]
    assert asdict(execute_sql.undetermined_receipt("x")) != receipts[0]
