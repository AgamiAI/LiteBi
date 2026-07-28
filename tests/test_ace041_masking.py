"""ACE-041 slice 3 — the behaviour flip: a *maskable* sensitive projection is no longer refused,
it is EXECUTED and the sensitive OUTPUT column is redacted to a token post-execution; an
*untraceable* sensitive projection still refuses (fail-closed). This pins the whole chain:

  guard classifies (runtime.check_sensitive_projection) -> _model_safety builds a data_protection
  Verdict + runs guardrail.policy -> execute_guarded applies the mask plan to result.rows -> the
  tool edge records `applied[{mask}]` on BOTH surfaces.

Also covers the slice-3 case-fold fix (`SELECT SSN` / `SELECT "SSN"` now match a lower-case declared
`ssn`, consistent with the table/column-scope gates) and the unparseable fail-closed decision (this
branch has NO ACE-037 upstream unscopable gate, so the PII path itself refuses an unparseable
statement when the model declares PII).

Synthetic, generic names only — `customers`, `orders`, `x`, `AcmeCorp`.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import execute_sql  # noqa: E402
from semantic_model import models as m  # noqa: E402
from semantic_model import runtime as rt  # noqa: E402


def _org() -> "m.Datasource":
    """`customers` has three sensitive columns (`ssn`, `email`, decimal `account_balance`);
    `orders` is non-sensitive for joins and cross-datasource set-operation arms; `x` carries a column
    literally named `ssn` that is NOT sensitive (the same-name decoy)."""
    customers = m.Table(
        name="customers", schema="public", storage_connection="c", grain=["id"],
        columns=[
            m.Column(name="id", type="integer"),
            m.Column(name="name", type="string"),
            m.Column(name="ssn", type="string", sensitive=True),
            m.Column(name="email", type="string", sensitive=True),
            m.Column(name="account_balance", type="decimal", sensitive=True),
        ],
    )
    orders = m.Table(
        name="orders", schema="public", storage_connection="c", grain=["id"],
        columns=[m.Column(name="id", type="integer"), m.Column(name="cust_id", type="integer")],
    )
    x = m.Table(
        name="x", schema="public", storage_connection="c", grain=["ssn"],
        columns=[m.Column(name="id", type="integer"), m.Column(name="ssn", type="string")],
    )
    sa = m.SubjectArea(name="area", description="d", tables_defined=[customers, orders, x])
    return m.Datasource(
        datasource="AcmeCorp", version=1,
        storage_connections=[m.StorageConnection(name="c", storage_type="SQLite")],
        subject_areas=[sa],
    )


class _SpyExecutor:
    """Records calls and returns a canned ``ExecResult`` — so a test can assert the executor RAN and
    what it returned was redacted downstream (not blocked)."""

    def __init__(self, result: "execute_sql.ExecResult"):
        self.calls: list[tuple] = []
        self._result = result

    def execute(self, vetted_sql: str, creds: dict, *, profile: str) -> "execute_sql.ExecResult":
        self.calls.append((vetted_sql, creds, profile))
        return self._result


@pytest.fixture
def guarded(monkeypatch):
    """Wire ``execute_guarded`` to run the REAL ``_model_safety`` over ``_org()`` with dummy creds, so
    a test drives the whole guard->policy->redact chain with only the executor faked."""
    monkeypatch.setattr(execute_sql, "_resolve_guard_model", lambda profile: _org())
    monkeypatch.setattr(execute_sql, "_load_credentials", lambda p, org_id="local": {"type": "sqlite", "path": ":memory:"})

    def _run(sql: str, spy: "_SpyExecutor", **kw):
        return execute_sql.execute_guarded(sql, "acme", None, executor=spy, **kw)

    return _run


def _R(columns, rows, truncated=False):
    return execute_sql.ExecResult(columns=list(columns), rows=[tuple(r) for r in rows], truncated=truncated)


@pytest.fixture(autouse=True)
def _reset_injected_executor():
    """Isolate the process-global injected executor across tests (the tool-edge tests below register
    a spy; leaving it set would pollute a later test). Additive — clears before and after each test."""
    import tools

    tools.set_injected_executor(None)
    yield
    tools.set_injected_executor(None)


# ---------------------------------------------------------------------------
# execute_guarded: a maskable projection PROCEEDS and the value is redacted.
# ---------------------------------------------------------------------------


def test_maskable_projection_redacts_the_output_value_and_does_not_block(guarded):
    spy = _SpyExecutor(_R(["ssn"], [("111-22-3333",), ("999-88-7777",)]))
    result = guarded("SELECT ssn FROM customers", spy)
    assert spy.calls, "the query must PROCEED to the executor, not be blocked"
    assert result.rows == [(execute_sql.REDACTION_TOKEN,), (execute_sql.REDACTION_TOKEN,)]
    assert result.columns == ["ssn"]
    assert result.masked_columns == ("customers.ssn",)


def test_only_the_sensitive_output_column_is_redacted(guarded):
    spy = _SpyExecutor(_R(["name", "ssn"], [("Alice", "111"), ("Bob", "222")]))
    result = guarded("SELECT name, ssn FROM customers", spy)
    # name (index 0, non-sensitive) is untouched; ssn (index 1) is redacted.
    tok = execute_sql.REDACTION_TOKEN
    assert result.rows == [("Alice", tok), ("Bob", tok)]
    assert result.masked_columns == ("customers.ssn",)


def test_masking_holds_on_a_row_capped_result(guarded):
    # The redaction runs on the ALREADY-capped rows and preserves the truncated flag (composes with
    # the existing cap, does not fight it).
    spy = _SpyExecutor(_R(["ssn"], [("a",), ("b",)], truncated=True))
    result = guarded("SELECT ssn FROM customers", spy)
    tok = execute_sql.REDACTION_TOKEN
    assert result.rows == [(tok,), (tok,)]
    assert result.truncated is True


def test_two_sensitive_columns_both_redacted(guarded):
    spy = _SpyExecutor(_R(["ssn", "email"], [("111", "a@x.io")]))
    result = guarded("SELECT ssn, email FROM customers", spy)
    tok = execute_sql.REDACTION_TOKEN
    assert result.rows == [(tok, tok)]
    assert result.masked_columns == ("customers.email", "customers.ssn")


def test_join_query_masks_the_sensitive_column_and_returns(guarded):
    spy = _SpyExecutor(_R(["ssn"], [("111",)]))
    result = guarded(
        "SELECT c.ssn FROM customers c JOIN orders o ON o.cust_id = c.id", spy
    )
    assert spy.calls
    assert result.rows == [(execute_sql.REDACTION_TOKEN,)]


# ---------------------------------------------------------------------------
# Untraceable / star projections still REFUSE (fail-closed), executor never runs.
# ---------------------------------------------------------------------------


def test_untraceable_projection_still_refuses(guarded):
    spy = _SpyExecutor(_R(["s"], [("x",)]))
    with pytest.raises(execute_sql.GuardRefused) as ei:
        guarded("SELECT UPPER(ssn) FROM customers", spy)
    assert ei.value.refusal.kind == "sensitive_columns"
    assert spy.calls == []  # buried value cannot be masked -> refused before the executor


def test_star_projection_still_refuses(guarded):
    spy = _SpyExecutor(_R(["a"], [("x",)]))
    with pytest.raises(execute_sql.GuardRefused) as ei:
        guarded("SELECT * FROM customers", spy)
    # SELECT * is caught by the star-ban safety gate FIRST (before the sensitive gate) — still refused.
    assert spy.calls == []
    assert ei.value.refusal.kind in ("select_star", "sensitive_columns")


def test_partially_maskable_union_fails_closed(guarded):
    # One arm buries ssn in UPPER(...) -> not every offending projection is maskable -> uncertain ->
    # reject (fail closed). The executor never runs.
    spy = _SpyExecutor(_R(["s"], [("x",)]))
    with pytest.raises(execute_sql.GuardRefused) as ei:
        guarded("SELECT UPPER(ssn) FROM customers UNION ALL SELECT id FROM customers", spy)
    assert ei.value.refusal.kind == "sensitive_columns"
    assert spy.calls == []


# ---------------------------------------------------------------------------
# Allowed queries are not masked at all (COUNT / WHERE / non-sensitive).
# ---------------------------------------------------------------------------


def test_count_of_sensitive_is_not_masked(guarded):
    spy = _SpyExecutor(_R(["n"], [(5,)]))
    result = guarded("SELECT COUNT(ssn) FROM customers", spy)
    assert result.rows == [(5,)]  # untouched
    assert result.masked_columns == ()


def test_nonsensitive_projection_is_not_masked(guarded):
    spy = _SpyExecutor(_R(["name"], [("Alice",), ("Bob",)]))
    result = guarded("SELECT name FROM customers WHERE ssn = :x", spy)
    assert result.rows == [("Alice",), ("Bob",)]
    assert result.masked_columns == ()


# ---------------------------------------------------------------------------
# Case-fold fix (ACE-041 slice 3): the sensitive-name match is now case-insensitive.
# ---------------------------------------------------------------------------


def test_uppercase_unquoted_sensitive_column_is_now_masked(guarded):
    spy = _SpyExecutor(_R(["SSN"], [("111",)]))
    result = guarded("SELECT SSN FROM customers", spy)
    assert spy.calls  # proceeds
    assert result.rows == [(execute_sql.REDACTION_TOKEN,)]
    assert result.masked_columns == ("customers.ssn",)


def test_quoted_uppercase_sensitive_column_is_now_masked(guarded):
    spy = _SpyExecutor(_R(["SSN"], [("111",)]))
    result = guarded('SELECT "SSN" FROM customers', spy)
    assert result.rows == [(execute_sql.REDACTION_TOKEN,)]
    assert result.masked_columns == ("customers.ssn",)


def test_mixed_case_sensitive_column_is_now_masked(guarded):
    spy = _SpyExecutor(_R(["Ssn"], [("111",)]))
    result = guarded("SELECT Ssn FROM customers", spy)
    assert result.rows == [(execute_sql.REDACTION_TOKEN,)]
    assert result.masked_columns == ("customers.ssn",)


def test_same_named_nonsensitive_column_on_other_table_still_not_flagged(guarded):
    # `x.ssn` is literally named `ssn` but NOT sensitive on `x` — projecting it (any case) is allowed.
    spy = _SpyExecutor(_R(["ssn"], [("v",)]))
    result = guarded("SELECT SSN FROM x", spy)
    assert result.rows == [("v",)]  # untouched
    assert result.masked_columns == ()


# ---------------------------------------------------------------------------
# Unparseable fail-closed (this branch has no upstream ACE-037 unscopable gate):
# an unparseable statement + a model that declares PII is REFUSED, never run.
# ---------------------------------------------------------------------------


def test_unparseable_with_declared_pii_fails_closed(guarded, monkeypatch):
    # Force sqlglot to fail to parse (tree=None) while it is otherwise available, so we exercise the
    # PARSE-FAILURE fail-closed branch (distinct from sqlglot-unavailable, which stays a fail-open).
    # NOTE (governance-branch merge of ACE-037 + ACE-041): the scopability gate (ACE-037) runs BEFORE
    # the PII gate and also fails closed on an unparseable query, so a declared-PII model — which
    # always has declared tables — is refused as `unscopable_sql` first. The invariant this test pins
    # is unchanged (unparseable + declared PII => refused fail-closed, executor never runs); only the
    # refusal *kind* moves from `sensitive_columns` to `unscopable_sql`. The PII parse-failure check
    # remains in _model_safety as defense-in-depth (it fires if the scopable gate is ever disabled).
    # The guard battery parses through `_parse_reporting` (tree + why-it-failed) and
    # `_parse_sql` (tree only), so both are stubbed to report a parse failure.
    monkeypatch.setattr(rt, "_parse_reporting", lambda sql, dialect=None: (None, "forced"))
    monkeypatch.setattr(rt, "_parse_sql", lambda sql, dialect=None: None)
    spy = _SpyExecutor(_R(["ssn"], [("111",)]))
    with pytest.raises(execute_sql.GuardRefused) as ei:
        guarded("SELECT ssn FROM customers", spy)
    assert ei.value.refusal.kind == "unscopable_sql"
    assert spy.calls == []


# ---------------------------------------------------------------------------
# _model_safety returns a typed (sql, Refusal|None, MaskPlan|None).
# ---------------------------------------------------------------------------


def test_model_safety_returns_a_mask_plan_for_a_maskable_projection(monkeypatch):
    monkeypatch.setattr(execute_sql, "_resolve_guard_model", lambda profile: _org())
    sql, refusal, plan = execute_sql._model_safety("SELECT id, ssn FROM customers", "acme", None)
    assert refusal is None
    assert plan is not None
    assert plan.indices == (1,)
    assert plan.columns == ("customers.ssn",)
    assert plan.token == execute_sql.REDACTION_TOKEN


def test_model_safety_rejects_an_untraceable_projection(monkeypatch):
    monkeypatch.setattr(execute_sql, "_resolve_guard_model", lambda profile: _org())
    sql, refusal, plan = execute_sql._model_safety("SELECT UPPER(ssn) FROM customers", "acme", None)
    assert plan is None
    assert refusal is not None and refusal.kind == "sensitive_columns"


def test_model_safety_allows_a_clean_query_with_no_plan(monkeypatch):
    monkeypatch.setattr(execute_sql, "_resolve_guard_model", lambda profile: _org())
    sql, refusal, plan = execute_sql._model_safety("SELECT name FROM customers", "acme", None)
    assert refusal is None and plan is None


# ---------------------------------------------------------------------------
# The subprocess metadata channel: _emit_result_csv flags masked columns on stderr, and
# tools._executor_masked parses that line.
# ---------------------------------------------------------------------------


def test_emit_result_csv_flags_masked_columns_on_stderr(capsys):
    result = execute_sql.ExecResult(
        columns=["ssn"], rows=[("***",)], truncated=False, masked_columns=("customers.ssn",)
    )
    execute_sql._emit_result_csv(result)
    err = capsys.readouterr().err
    lines = [json.loads(x) for x in err.splitlines() if x.strip().startswith("{")]
    masked = [d["masked"] for d in lines if "masked" in d]
    assert masked == [["customers.ssn"]]


def test_tools_executor_masked_parses_the_stderr_marker():
    import tools

    stderr = 'some notice\n{"masked": ["customers.ssn", "customers.email"]}\n'
    assert tools._executor_masked(stderr) == ["customers.ssn", "customers.email"]
    assert tools._executor_masked("nothing here") == []


# ---------------------------------------------------------------------------
# tool_execute_sql (in-process, injected executor): masked VALUES in `data`, `applied[{mask}]` note.
# ---------------------------------------------------------------------------


def test_tool_execute_sql_masks_value_and_records_applied(monkeypatch):
    import tools

    monkeypatch.setattr(tools, "resolve_profile", lambda ds: "acme")
    monkeypatch.setattr(execute_sql, "_resolve_guard_model", lambda profile: _org())
    monkeypatch.setattr(execute_sql, "_load_credentials", lambda p, org_id="local": {"type": "sqlite", "path": ":memory:"})

    spy = _SpyExecutor(_R(["name", "ssn"], [("Alice", "111-22-3333")]))
    tools.set_injected_executor(spy)
    out = json.loads(tools.tool_execute_sql({"sql": "SELECT name, ssn FROM customers", "datasource": "acme"}))

    assert out["status"] == "ok"  # NOT refused — the query ran
    assert spy.calls  # the injected executor ran behind the guard
    assert out["data"]["rows"] == [["Alice", execute_sql.REDACTION_TOKEN]]  # ssn redacted, name intact
    assert {"mask": "customers.ssn"} in out["applied"]


def test_tool_execute_sql_untraceable_pii_is_refused_no_data(monkeypatch):
    import tools

    monkeypatch.setattr(tools, "resolve_profile", lambda ds: "acme")
    monkeypatch.setattr(execute_sql, "_resolve_guard_model", lambda profile: _org())
    monkeypatch.setattr(execute_sql, "_load_credentials", lambda p, org_id="local": {"type": "sqlite", "path": ":memory:"})

    spy = _SpyExecutor(_R(["s"], [("x",)]))
    tools.set_injected_executor(spy)
    out = json.loads(tools.tool_execute_sql({"sql": "SELECT UPPER(ssn) FROM customers", "datasource": "acme"}))

    assert out["status"] == "refused"
    assert out["refusal"]["kind"] == "sensitive_columns"
    assert "data" not in out
    assert spy.calls == []


# ---------------------------------------------------------------------------
# Cross-position set operations (ACE-041 review, finding 1): a sensitive column that appears at a
# DIFFERENT output position in another arm forces that position to be redacted too — each merged
# output column carries a raw SSN in SOME arm's rows, so masking every such position is correct AND
# necessary. Two fixes are pinned here: (a) the masking behaviour (no raw SSN survives on either
# surface), and (b) the enriched `applied`/`masked_columns` note that names EVERY masked position.
# ---------------------------------------------------------------------------


# Each row carries a raw SSN in a DIFFERENT position, mirroring how a real DB merges the arms
# (arm 1 → (ssn, name); arm 2 → (name, ssn)). The output labels come from the first arm: (ssn, name).
_CROSS_POSITION_ROWS = [("111-22-3333", "Alice"), ("Bob", "999-88-7777")]


@pytest.mark.parametrize("op", ["UNION ALL", "UNION", "INTERSECT", "EXCEPT"])
def test_cross_position_set_op_redacts_every_position_and_leaks_no_ssn(guarded, op):
    # `SELECT ssn, name … <op> SELECT name, ssn …`: output column 0 holds an SSN in arm 1's rows and
    # column 1 holds an SSN in arm 2's rows, so BOTH output positions must be (and are) redacted.
    spy = _SpyExecutor(_R(["ssn", "name"], _CROSS_POSITION_ROWS))
    result = guarded(f"SELECT ssn, name FROM customers {op} SELECT name, ssn FROM customers", spy)
    tok = execute_sql.REDACTION_TOKEN
    assert spy.calls  # the query PROCEEDS (maskable), it is not refused
    assert result.rows == [(tok, tok), (tok, tok)]  # every position redacted
    # No raw SSN survives at ANY output position, on the in-process (native rows) surface.
    flat = [v for row in result.rows for v in row]
    assert "111-22-3333" not in flat and "999-88-7777" not in flat
    # Enriched note: the declared ref for column 0, plus column 1's output label (`name`) — the extra
    # positionally-aligned position that carries raw SSN in arm 2 and is not named by a declared ref.
    assert result.masked_columns == ("customers.ssn", "name")


def test_cross_position_union_redacts_no_ssn_on_the_csv_surface(guarded, capsys):
    # The subprocess wire (CSV on stdout + `{"masked": …}` on stderr) must also carry no raw SSN and
    # the enriched note. Run the guarded chain, then serialize the masked result the way the fork does.
    spy = _SpyExecutor(_R(["ssn", "name"], _CROSS_POSITION_ROWS))
    result = guarded("SELECT ssn, name FROM customers UNION ALL SELECT name, ssn FROM customers", spy)
    execute_sql._emit_result_csv(result)
    captured = capsys.readouterr()
    assert "***" in captured.out
    assert "111-22-3333" not in captured.out and "999-88-7777" not in captured.out
    masked = [json.loads(x)["masked"] for x in captured.err.splitlines() if '"masked"' in x]
    assert masked == [["customers.ssn", "name"]]  # the enriched note rides the stderr metadata channel


def test_cross_position_partial_only_names_the_masked_position(guarded):
    # `SELECT ssn, id FROM customers UNION ALL SELECT cust_id, id FROM orders`: only output column 0
    # holds a sensitive value (ssn in arm 1; cust_id in arm 2 is NOT sensitive), so only column 0 is
    # redacted. The whole column 0 is redacted (arm 2's non-sensitive cust_id is collateral, which is
    # correct — no raw SSN can survive), and the note names just the one declared masked column.
    spy = _SpyExecutor(_R(["ssn", "id"], [("111-22-3333", 1), (42, 2)]))
    result = guarded("SELECT ssn, id FROM customers UNION ALL SELECT cust_id, id FROM orders", spy)
    tok = execute_sql.REDACTION_TOKEN
    assert result.rows == [(tok, 1), (tok, 2)]  # column 0 redacted; column 1 (id) untouched
    flat = [v for row in result.rows for v in row]
    assert "111-22-3333" not in flat
    assert result.masked_columns == ("customers.ssn",)  # only the one masked position, its declared ref


def test_cross_position_enriched_note_reaches_the_tool_edge_applied(monkeypatch):
    # The enriched note surfaces on the in-process tool surface's `applied[{mask}]` list too: both the
    # declared ref AND the extra output-label position are recorded, so the receipt is accurate.
    import tools

    monkeypatch.setattr(tools, "resolve_profile", lambda ds: "acme")
    monkeypatch.setattr(execute_sql, "_resolve_guard_model", lambda profile: _org())
    monkeypatch.setattr(execute_sql, "_load_credentials", lambda p, org_id="local": {"type": "sqlite", "path": ":memory:"})

    spy = _SpyExecutor(_R(["ssn", "name"], _CROSS_POSITION_ROWS))
    tools.set_injected_executor(spy)
    out = json.loads(tools.tool_execute_sql(
        {"sql": "SELECT ssn, name FROM customers UNION ALL SELECT name, ssn FROM customers",
         "datasource": "acme"}
    ))

    assert out["status"] == "ok"
    assert {"mask": "customers.ssn"} in out["applied"]
    assert {"mask": "name"} in out["applied"]  # the extra positionally-aligned position is named


def test_single_column_note_is_unchanged_by_the_enrichment(guarded):
    # Regression guard for spec success criterion 1: the normal single-arm case still reports EXACTLY
    # the declared ref — the enrichment must not add the output label when it already names the column.
    spy = _SpyExecutor(_R(["ssn"], [("111-22-3333",)]))
    result = guarded("SELECT ssn FROM customers", spy)
    assert result.masked_columns == ("customers.ssn",)


def test_mask_note_helper_is_accurate_and_deterministic():
    # Unit-pin the note builder directly. Single declared column whose label matches -> just the ref.
    assert execute_sql._mask_note(("customers.ssn",), (0,), ["ssn"]) == ("customers.ssn",)
    # Aliased single column (label differs from the bare name) -> the label is added alongside the ref,
    # which is fail-safe for a receipt (it never drops a masked position, only ever adds disclosure).
    assert execute_sql._mask_note(("customers.ssn",), (0,), ["taxid"]) == ("customers.ssn", "taxid")
    # Cross-position set op: column 0 named by the declared ref, column 1 named by its output label.
    assert execute_sql._mask_note(("customers.ssn",), (0, 1), ["ssn", "name"]) == ("customers.ssn", "name")
    # An index past the output width simply contributes nothing (fail-safe, no crash).
    assert execute_sql._mask_note(("customers.ssn",), (0, 9), ["ssn"]) == ("customers.ssn",)


# ---------------------------------------------------------------------------
# CO-PROJECTION FAIL-CLOSED — the mask verdict must be a WHOLE-OUTPUT proof.
#
# `all_maskable` quantifies over the projections the DETECTOR FOUND, which proves "every
# offending projection we saw is maskable" — not "no sensitive value reaches the output".
# Under the old refuse-only behaviour a detector miss leaked only when NOTHING was detected,
# because anything detected blocked the whole query. Once a detected-and-maskable projection
# started letting the query RUN, one seen column began unblocking the entire result set — so
# every co-projected value the name-based detector missed came back raw, next to a `***` that
# makes it look like masking worked.
#
# These are the co-projection forms of the alias-lineage gap below. The standalone forms are
# genuinely pre-existing; these are NOT — each returned a raw SSN only after the refuse->mask
# flip. Refusing is the correct outcome: when the output cannot be proven clean, fail closed.
# ---------------------------------------------------------------------------

_CO_PROJECTION_LEAK = [
    # a detected `ssn` beside the same column laundered through a derived table
    "SELECT ssn, t.ssn FROM customers, (SELECT ssn FROM customers) t",
    # ...through a CTE with a rename
    "WITH q AS (SELECT ssn AS z FROM customers) SELECT c.ssn, q.z FROM customers c, q",
]


@pytest.mark.parametrize("sql", _CO_PROJECTION_LEAK)
def test_co_projected_unprovable_output_is_refused_not_partially_masked(guarded, sql):
    """A raw sensitive value must never ride along beside a masked one.

    Each of these ran and returned `('***', '111-22-3333')` before the whole-output proof:
    one column redacted, its twin in the clear.
    """
    spy = _SpyExecutor(_R(["ssn", "ssn_2"], [("111-22-3333", "111-22-3333")]))
    with pytest.raises(execute_sql.GuardRefused) as ei:
        guarded(sql, spy)
    assert ei.value.refusal.kind == "sensitive_columns"
    assert not spy.calls, "an unprovable output must be refused BEFORE the executor runs"


@pytest.mark.parametrize(
    "sql,columns,row",
    [
        ("SELECT ssn FROM customers", ["ssn"], ("111-22-3333",)),
        ("SELECT c.ssn FROM customers c", ["ssn"], ("111-22-3333",)),
        ('SELECT "SSN" FROM customers', ["SSN"], ("111-22-3333",)),
        ("SELECT name, ssn FROM customers", ["name", "ssn"], ("Alice", "111-22-3333")),
        ("SELECT ssn FROM customers UNION ALL SELECT ssn FROM customers", ["ssn"],
         ("111-22-3333",)),
        # Case-mismatched alias: `FROM customers C` + `c.ssn`. ACE-041 slice 3 (`4bd39bb`) folded
        # the COLUMN name; the table ALIAS lookup was the other half. Once it resolves, this is an
        # ordinary provable projection, so it masks rather than refusing — which restores the
        # intended ACE-041 outcome. Before the alias fold, `SELECT C.ssn FROM customers c`
        # returned the raw value with no mask at all.
        ("SELECT ssn, c.ssn FROM customers C", ["ssn", "ssn_2"],
         ("111-22-3333", "111-22-3333")),
        ("SELECT C.ssn FROM customers c", ["ssn"], ("111-22-3333",)),
    ],
)
def test_provable_output_still_masks(guarded, sql, columns, row):
    """The whole-output proof must not collapse ACE-041 back into refuse-everything.

    Every projection here resolves to a declared table, so the output IS provable and the
    feature works as specified: the query runs and the sensitive column is redacted.
    """
    # The fixture mirrors each query's real projection shape, with the raw secret ONLY in cells the
    # engine would fill with a sensitive value. That is what makes "no raw value survives"
    # assertable — a fixture that puts the secret in every cell can only ever check that a token
    # appeared SOMEWHERE, which passes even when the plan masks the wrong column.
    spy = _SpyExecutor(_R(columns, [tuple(row)]))
    result = guarded(sql, spy)
    assert spy.calls, f"a provable maskable projection must still RUN: {sql!r}"
    assert result.masked_columns, f"a provable projection must be MASKED, not passed through: {sql!r}"
    flat = [v for r in result.rows for v in r]
    assert execute_sql.REDACTION_TOKEN in flat, f"must be masked: {sql!r}"
    assert "111-22-3333" not in flat, f"raw value survived: {result.rows!r} for {sql!r}"
    assert execute_sql.REDACTION_TOKEN in [v for row in result.rows for v in row]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(ssn) FROM customers",
        "SELECT COUNT(*) FROM customers WHERE ssn IS NOT NULL",
        "SELECT COUNT(*) FROM customers c, (SELECT id FROM orders) o WHERE c.ssn IS NOT NULL",
    ],
)
def test_aggregate_or_filter_use_returns_before_the_proof(guarded, sql):
    """Aggregate/filter use of a sensitive column stays allowed, derived table or not.

    Note what this does and does NOT cover. Every case here ends at `action="allow"`, so it
    returns BEFORE the whole-output proof is ever consulted — it pins the early-return path, not
    the proof. The over-refusal direction (a query that DOES project a sensitive column and also
    contains unrelated nesting) is pinned separately by
    `test_unrelated_nesting_still_masks`, which is the test that would catch the proof
    being widened back out.
    """
    spy = _SpyExecutor(_R(["n"], [(3,)]))
    result = guarded(sql, spy)
    assert spy.calls, f"aggregate/filter use must not be refused: {sql!r}"
    assert result.rows == [(3,)]


# ---------------------------------------------------------------------------
# KNOWN LIMITATION (ACE-041 review, finding 2): a sensitive column RENAMED inside a derived-table /
# CTE body escapes both mask and refuse, because `_output_selects` excludes subquery/CTE bodies and
# the sensitive match is name-based with no alias/lineage tracking. This is PRE-EXISTING (not
# introduced by ACE-041) in its STANDALONE form — nothing sensitive is detected at all, so the
# whole-output proof above never engages.
#
# Its CO-PROJECTION form WAS new, and is closed above — but closed by REFUSAL, not by detection.
# The detector still never sees the laundered column (it reports only the plainly-projected
# `customers.ssn`); what stops the leak is that an arm drawing on a derived table or a CTE cannot
# be proven, so the statement is refused. That coupling matters: narrowing the opaque-source test
# in `_output_lineage_provable` would silently re-open these leaks while every test here still
# passes. `test_opaque_output_source_is_still_refused` is what pins it.
# These xfail tests assert the DESIRED secure behaviour (the outer projection
# is masked OR refused); they xfail today and will flip to xpass when a future alias-lineage fix
# lands — that flip is the tracking signal. Do NOT "fix" them by weakening the assertion. See the
# `# ACE-041 known limitation` comment in semantic_model/runtime.py::_output_selects.
# ---------------------------------------------------------------------------

_ALIAS_LINEAGE_BYPASS = [
    "WITH t AS (SELECT ssn AS s FROM customers) SELECT s FROM t",
    "SELECT z FROM (SELECT ssn AS z FROM customers) q",
]


@pytest.mark.xfail(
    reason="ACE-041 known limitation: no alias/lineage tracking through derived-table/CTE bodies; "
    "pre-existing in the sensitive-projection gate. Tracked as follow-up spec ACE-062.",
    strict=False,
)
@pytest.mark.parametrize("sql", _ALIAS_LINEAGE_BYPASS)
def test_alias_rename_through_derived_body_should_be_masked_or_refused(guarded, sql):
    # DESIRED secure behaviour: the outer projection of the renamed sensitive column is either masked
    # or the whole query is refused. Today it is neither (the guard allows it and the raw value flows
    # straight through), so this xfails; a future lineage fix flips it to xpass.
    spy = _SpyExecutor(_R(["out"], [("111-22-3333",)]))
    try:
        result = guarded(sql, spy)
    except execute_sql.GuardRefused:
        return  # refusing the query is a secure (desired) outcome
    # It ran: the renamed sensitive value MUST be redacted and the raw value MUST NOT survive.
    flat = [v for row in result.rows for v in row]
    assert execute_sql.REDACTION_TOKEN in flat
    assert "111-22-3333" not in flat


# ---------------------------------------------------------------------------
# Corpus gaps (ACE-041 review, finding 3): cases that previously passed only by probe — now asserted.
# ---------------------------------------------------------------------------


def test_numeric_sensitive_column_masks_on_both_surfaces(guarded, capsys):
    # A numeric (typed) sensitive column projected bare is redacted to the token with no crash, on the
    # in-process surface AND the CSV wire (the token replaces the value regardless of its Python type).
    spy = _SpyExecutor(_R(["account_balance"], [(1234.56,), (9876,)]))
    result = guarded("SELECT account_balance FROM customers", spy)
    tok = execute_sql.REDACTION_TOKEN
    assert result.rows == [(tok,), (tok,)]
    assert result.masked_columns == ("customers.account_balance",)
    # Subprocess/CSV surface: the token is serialized, never the raw number.
    execute_sql._emit_result_csv(result)
    out = capsys.readouterr().out
    assert "***" in out
    assert "1234.56" not in out and "9876" not in out


def test_null_value_at_a_masked_index_becomes_the_token(guarded):
    # A NULL (None) at a masked index is redacted deterministically to the token, exactly like any
    # other value — the redactor replaces by position, not by inspecting the value.
    spy = _SpyExecutor(_R(["ssn"], [(None,), ("111-22-3333",)]))
    result = guarded("SELECT ssn FROM customers", spy)
    tok = execute_sql.REDACTION_TOKEN
    assert result.rows == [(tok,), (tok,)]  # the NULL row and the value row both become the token
    assert result.masked_columns == ("customers.ssn",)


def test_null_at_masked_index_textualizes_to_the_token_in_process(monkeypatch):
    # On the in-process tool edge, a masked NULL surfaces as the token (not "" — it was replaced before
    # the None→"" textualization), so a redacted NULL is indistinguishable from a redacted value.
    import tools

    monkeypatch.setattr(tools, "resolve_profile", lambda ds: "acme")
    monkeypatch.setattr(execute_sql, "_resolve_guard_model", lambda profile: _org())
    monkeypatch.setattr(execute_sql, "_load_credentials", lambda p, org_id="local": {"type": "sqlite", "path": ":memory:"})

    spy = _SpyExecutor(_R(["ssn"], [(None,)]))
    tools.set_injected_executor(spy)
    out = json.loads(tools.tool_execute_sql({"sql": "SELECT ssn FROM customers", "datasource": "acme"}))
    assert out["data"]["rows"] == [[execute_sql.REDACTION_TOKEN]]


def test_empty_result_with_maskable_projection_records_mask_and_no_rows(guarded):
    # An empty result set through a maskable projection: the query proceeds, returns zero rows with no
    # error, and the mask note is STILL recorded (asserting the actual behaviour — the receipt names
    # the column that WOULD have been redacted even though no value was present).
    spy = _SpyExecutor(_R(["ssn"], []))
    result = guarded("SELECT ssn FROM customers", spy)
    assert spy.calls
    assert result.rows == []
    assert result.masked_columns == ("customers.ssn",)


def test_empty_result_still_records_applied_mask_on_the_tool_edge(monkeypatch):
    import tools

    monkeypatch.setattr(tools, "resolve_profile", lambda ds: "acme")
    monkeypatch.setattr(execute_sql, "_resolve_guard_model", lambda profile: _org())
    monkeypatch.setattr(execute_sql, "_load_credentials", lambda p, org_id="local": {"type": "sqlite", "path": ":memory:"})

    spy = _SpyExecutor(_R(["ssn"], []))
    tools.set_injected_executor(spy)
    out = json.loads(tools.tool_execute_sql({"sql": "SELECT ssn FROM customers", "datasource": "acme"}))
    assert out["status"] == "ok"
    assert out["data"]["rows"] == [] and out["data"]["row_count"] == 0
    assert {"mask": "customers.ssn"} in out["applied"]  # the note is recorded even with zero rows


# ---------------------------------------------------------------------------
# A REAL subprocess fork end-to-end: `python -m execute_sql` over an on-disk model + a sqlite
# datasource, exercising the true fork path (main -> execute_guarded -> BUILTIN_EXECUTOR -> sqlite ->
# _emit_result_csv). Asserts `***` in the emitted CSV, the raw value absent, and the `{"masked": …}`
# stderr line present — the genuine subprocess wire, not a monkeypatched main().
# ---------------------------------------------------------------------------

def _write_disk_model(root: Path) -> None:
    """Write a minimal v2 semantic-model tree declaring `customers.ssn` sensitive — the layout
    `semantic_model.loader.load_datasource` (and `execute_sql._resolve_guard_model`) read from
    disk. Mirrors tests/test_semantic_model_loader.py::test_disk_round_trip, plus the sensitive flag."""
    import yaml

    (root / "datasources" / "c").mkdir(parents=True)
    (root / "subject_areas" / "area" / "tables").mkdir(parents=True)
    (root / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "AcmeCorp", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/area"],
    }))
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "SQLite", "storage_config": {}}))
    (root / "subject_areas" / "area" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "area", "description": "d",
        "tables": [{"storage_connection": "c", "schema": "public", "table": "customers"}],
    }))
    (root / "subject_areas" / "area" / "tables" / "customers.yaml").write_text(yaml.safe_dump({
        "name": "customers", "schema": "public", "storage_connection": "c", "grain": ["id"],
        "description": "one line",
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "ssn", "type": "string", "sensitive": True}],
    }))
    (root / "subject_areas" / "area" / "relationships.yaml").write_text(
        yaml.safe_dump({"relationships": []}))


def test_real_subprocess_fork_masks_ssn_in_the_emitted_csv(tmp_path):
    pytest.importorskip("yaml")
    # On-disk model (declares customers.ssn sensitive) + a real sqlite datasource with raw SSNs.
    art = tmp_path / "art"
    _write_disk_model(art / "acme")
    db = tmp_path / "customers.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE customers (id INTEGER, ssn TEXT)")
    con.executemany("INSERT INTO customers VALUES (?, ?)", [(1, "111-22-3333"), (2, "999-88-7777")])
    con.commit()
    con.close()

    # Env-first credentials (a sqlite DSN) avoid the chmod-600 file gate; AGAMI_ARTIFACTS_DIR points the
    # model resolver at the on-disk tree; the DB-url vars are cleared so `_hosted()` is False (disk model).
    env = {**os.environ,
           "AGAMI_ARTIFACTS_DIR": str(art),
           "DATASOURCE_URL__ACME": f"sqlite:///{db}",
           "PYTHONPATH": str(PKG_SRC) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    env.pop("AGAMI_DB_URL", None)
    env.pop("APP_DATABASE_URL", None)

    proc = subprocess.run(
        [sys.executable, "-m", "execute_sql", "--profile", "acme", "--sql", "SELECT ssn FROM customers"],
        capture_output=True, text=True, env=env, timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    # The emitted CSV carries the token, never the raw SSNs.
    assert "***" in proc.stdout
    assert "111-22-3333" not in proc.stdout and "999-88-7777" not in proc.stdout
    # The `{"masked": …}` metadata line is present on stderr, naming the declared masked column.
    masked = [json.loads(x)["masked"] for x in proc.stderr.splitlines()
              if x.strip().startswith("{") and '"masked"' in x]
    assert masked == [["customers.ssn"]]


# ---------------------------------------------------------------------------
# WHOLE-OUTPUT PROOF — adversarial matrix (PR #155 review).
#
# The proof answers one question: could the detector have seen EVERY value reaching the output?
# Two failure directions, and both must be pinned, because a fix for either one silently breaks
# the other:
#
#   * UNSOUND  — `output_provable` True while a sensitive value reaches the output unseen. That is
#     the `('***', raw)` receipt this whole feature exists to prevent, so it is the direction that
#     must never regress. Caused by the detector RESOLVING A QUALIFIER TO THE WRONG TABLE.
#   * OVER-BROAD — the proof withholds itself for a construct that cannot contribute an output
#     value at all (a WHERE/HAVING subquery, a CTE nothing selects from), refusing an ordinary
#     query that ACE-041 is supposed to mask.
#
# The fixtures below put the raw secret ONLY in cells the engine would really fill with a sensitive
# value, and a distinct sentinel elsewhere, so "no raw value survives" is directly assertable
# instead of being inferred from the presence of a token somewhere in the row.
# ---------------------------------------------------------------------------

_SECRET = "111-22-3333"
_SAFE = "not-sensitive"


def _assert_no_raw_value(guarded, sql, columns, row):
    """Run `sql` and assert the raw sensitive value never reaches the caller.

    Either outcome is secure: refusing the statement, or running it with every sensitive cell
    redacted. What is NOT acceptable is a raw secret in the result — with or without a `***`
    beside it, which is the partial-mask receipt that reads as "this row was protected".
    """
    spy = _SpyExecutor(_R(columns, [tuple(row)]))
    try:
        result = guarded(sql, spy)
    except execute_sql.GuardRefused:
        return  # fail-closed is a secure outcome
    flat = [v for r in result.rows for v in r]
    assert _SECRET not in flat, f"raw sensitive value survived: {result.rows!r} for {sql!r}"


# An alias declared in a subquery must not capture a qualifier belonging to the OUTER query. The
# detector resolves `t.ssn` through its alias->table map; if the map is built over the whole tree,
# the inner `FROM orders t` overwrites the outer `t -> customers` and the outer `t.ssn` is judged a
# column of `orders` (which declares no `ssn`), so it is never detected. Every qualifier still
# "resolves", so the proof reports provable and the query masks ONE column and returns the other raw.
_ALIAS_SHADOWING_LEAK = [
    # `EXISTS (...)` parses to exp.Exists wrapping a bare Select — NO exp.Subquery node — so a
    # derived-table check keyed on that node type never fires here.
    ("SELECT c.ssn, t.ssn FROM customers c, customers t WHERE EXISTS (SELECT 1 FROM orders t)",
     ["ssn", "ssn_2"], (_SECRET, _SECRET)),
    ("SELECT c.ssn, t.ssn FROM customers c, customers t WHERE NOT EXISTS (SELECT 1 FROM x t)",
     ["ssn", "ssn_2"], (_SECRET, _SECRET)),
    # Same root cause with no co-projection to unblock the query: the single projection resolves to
    # the wrong table, nothing is detected at all, and the raw value flows straight through.
    ("SELECT t.ssn FROM customers t WHERE EXISTS (SELECT 1 FROM orders t)",
     ["ssn"], (_SECRET,)),
    # A JOIN body, not a WHERE body — same shadowing, different clause.
    ("SELECT c.ssn, t.ssn FROM customers c JOIN customers t ON t.id = c.id "
     "WHERE EXISTS (SELECT 1 FROM x t)", ["ssn", "ssn_2"], (_SECRET, _SECRET)),
]


@pytest.mark.parametrize("sql,columns,row", _ALIAS_SHADOWING_LEAK)
def test_subquery_alias_must_not_shadow_an_outer_qualifier(guarded, sql, columns, row):
    _assert_no_raw_value(guarded, sql, columns, row)


# A quoted alias must not capture an unquoted qualifier that the ENGINE folds onto a different
# table. `FROM x "AB", customers ab` + `AB.ssn`: Postgres folds the unquoted `AB` to `ab` and reads
# `customers`, but an exact-match-first lookup binds it to the quoted `"AB"` -> `x`, whose `ssn` is
# not declared sensitive. When two aliases in scope fold together the binding is genuinely
# ambiguous, and an ambiguous binding must fail closed, not pick one.
_QUOTED_ALIAS_COLLISION = [
    ('SELECT c.ssn, AB.ssn FROM customers c, x "AB", customers ab',
     ["ssn", "ssn_2"], (_SECRET, _SECRET)),
    ('SELECT AB.ssn FROM x "AB", customers ab', ["ssn"], (_SECRET,)),
]


@pytest.mark.parametrize("sql,columns,row", _QUOTED_ALIAS_COLLISION)
def test_case_colliding_aliases_fail_closed(guarded, sql, columns, row):
    _assert_no_raw_value(guarded, sql, columns, row)


# The proof must engage ONLY for constructs that can actually put a value in the output. A subquery
# in WHERE/HAVING/ORDER BY yields a filter, not a column; a CTE nothing selects from contributes
# nothing at all. Refusing these buys no safety and rolls back the feature for ordinary analytics
# SQL, which is what LLM-generated queries look like.
_MASKABLE_DESPITE_UNRELATED_NESTING = [
    # WHERE subquery — the archetypal false refusal.
    ("SELECT ssn FROM customers WHERE id IN (SELECT cust_id FROM orders)", ["ssn"], (_SECRET,)),
    ("SELECT c.ssn FROM customers c WHERE c.id IN (SELECT cust_id FROM orders)", ["ssn"], (_SECRET,)),
    ("SELECT ssn FROM customers c WHERE EXISTS (SELECT 1 FROM orders o WHERE o.cust_id = c.id)",
     ["ssn"], (_SECRET,)),
    # HAVING / ORDER BY subqueries.
    ("SELECT ssn FROM customers GROUP BY ssn HAVING COUNT(*) > (SELECT COUNT(*) FROM orders)",
     ["ssn"], (_SECRET,)),
    ("SELECT ssn FROM customers ORDER BY (SELECT COUNT(*) FROM orders)", ["ssn"], (_SECRET,)),
    # A CTE that no output arm selects from cannot launder anything.
    ("WITH unused AS (SELECT id FROM orders) SELECT ssn FROM customers", ["ssn"], (_SECRET,)),
    # An ordinary two-table join with an unqualified projection. A bare column is matched by NAME
    # against the sensitive set, which does not depend on which table it binds to, so ambiguity here
    # cannot hide a sensitive value — `_sensitive_col_ref` already falls back to the folded bare name.
    ("SELECT name, ssn FROM customers JOIN orders ON customers.id = orders.cust_id",
     ["name", "ssn"], (_SAFE, _SECRET)),
    ("SELECT ssn FROM customers a, customers b", ["ssn"], (_SECRET,)),
]


@pytest.mark.parametrize("sql,columns,row", _MASKABLE_DESPITE_UNRELATED_NESTING)
def test_unrelated_nesting_still_masks(guarded, sql, columns, row):
    """Nesting that cannot reach the output must not downgrade a maskable projection to a refusal."""
    spy = _SpyExecutor(_R(columns, [tuple(row)]))
    result = guarded(sql, spy)
    assert spy.calls, f"must RUN, not refuse: {sql!r}"
    flat = [v for r in result.rows for v in r]
    assert execute_sql.REDACTION_TOKEN in flat, f"must be masked: {sql!r}"
    assert _SECRET not in flat, f"raw value survived: {result.rows!r} for {sql!r}"


# The narrowing above must not re-open what the whole-output proof closed. Each of these puts an
# output-bearing arm behind a body the detector does not walk, so the statement must still refuse.
_OUTPUT_BEARING_OPAQUE_SOURCE = [
    # derived table in FROM
    "SELECT ssn, t.ssn FROM customers, (SELECT ssn FROM customers) t",
    # CTE actually selected from by the output arm
    "WITH q AS (SELECT ssn AS z FROM customers) SELECT c.ssn, q.z FROM customers c, q",
    # a set-operation arm whose source is opaque, the other arm clean
    "SELECT ssn FROM customers UNION ALL SELECT z FROM (SELECT ssn AS z FROM customers) q",
    "WITH q AS (SELECT ssn AS z FROM customers) SELECT ssn FROM customers UNION ALL SELECT z FROM q",
    # nested two deep (no `*` — the star ban would refuse that one first, for a different reason)
    "SELECT ssn, t.z FROM customers, (SELECT z FROM (SELECT ssn AS z FROM customers) i) t",
    # a scalar subquery in the PROJECTION list can contribute an output value, and a rename inside
    # it defeats the name-based match, so it stays opaque even though it is not a FROM source
    "SELECT ssn, (SELECT z FROM (SELECT ssn AS z FROM customers) i LIMIT 1) AS leaked FROM customers",
]


@pytest.mark.parametrize("sql", _OUTPUT_BEARING_OPAQUE_SOURCE)
def test_opaque_output_source_is_still_refused(guarded, sql):
    spy = _SpyExecutor(_R(["a", "b"], [(_SECRET, _SECRET)]))
    with pytest.raises(execute_sql.GuardRefused) as ei:
        guarded(sql, spy)
    assert ei.value.refusal.kind == "sensitive_columns"
    assert not spy.calls, "an unprovable output must be refused BEFORE the executor runs"
