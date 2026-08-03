"""ACE-087 — the result bound REFUSES rather than truncating.

A result larger than `AGAMI_SQL_MAX_ROWS` used to come back trimmed and flagged. With no `ORDER BY`
there is no defined prefix, so what came back was whichever rows the engine emitted first: an
arbitrary sample, presented under `status=ok` as the answer. These tests pin the replacement — a
`resource_limit` refusal carrying no rows — and the two properties that make it correct rather than
merely different: it fires wherever the result came from, and it tells the caller a fix that suits
the statement they actually sent.

The verdict lives at `execute_guarded`, not at `_collect_cursor`, and the difference is the point of
`test_injected_executor_reporting_truncated_refuses`: three kinds of executor set `truncated` and
only one of them goes through a DB-API cursor.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402
import guardrail  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_shape():
    # `_guard_shape` is request-scoped; isolate every test from whatever ran before it.
    execute_sql._guard_shape.set(None)
    yield
    execute_sql._guard_shape.set(None)


class _Executor:
    """Returns a fixed `ExecResult`. Stands in for any of the three producers — the cursor path,
    BigQuery, or a hosted consumer's pooled/RBAC executor — since all three report overflow the
    same way: `ExecResult.truncated`."""

    def __init__(self, *, truncated: bool, rows: int = 3):
        self._result = execute_sql.ExecResult(
            columns=["c"], rows=[(i,) for i in range(rows)], truncated=truncated,
        )

    def execute(self, vetted_sql: str, creds: dict, *, profile: str) -> execute_sql.ExecResult:
        return self._result


@pytest.fixture
def _no_creds(monkeypatch):
    """Skip credential resolution — these tests are about the verdict, not about connecting."""
    monkeypatch.setattr(
        execute_sql, "_load_credentials", lambda profile, org_id: {"type": "sqlite"},
    )


# --- the verdict --------------------------------------------------------------------------------


def test_overflow_refuses_with_no_data(_no_creds):
    env = execute_sql.execute_guarded(
        "SELECT c FROM t", "acme", None, executor=_Executor(truncated=True), no_safety=True,
    )

    assert env.status == "refused"
    assert env.refusal.rule == guardrail.RULE_RESOURCE_LIMIT
    # `undetermined`, not `unsafe`: a bound we imposed is not a property of the statement. The rule
    # is single-valued in REASON_FOR_RULE, so this is the same reason the timeout carries.
    assert env.refusal.reason == "undetermined"
    # The rows the executor was holding do not come back. `Envelope.__post_init__` makes this
    # structural rather than a convention, and this asserts the structure was actually reached.
    assert env.data is None


def test_a_result_within_the_bound_still_returns(_no_creds):
    env = execute_sql.execute_guarded(
        "SELECT c FROM t", "acme", None, executor=_Executor(truncated=False), no_safety=True,
    )

    assert env.status == "ok"
    assert env.data.rows == [(0,), (1,), (2,)]


def test_injected_executor_reporting_truncated_refuses(_no_creds):
    """The reason the verdict is not in `_collect_cursor`.

    `ports.Executor` is the seam a hosted consumer overrides with a pooled / per-user-RBAC /
    SSH-tunnel executor. It never touches a DB-API cursor, so a check inside `_collect_cursor`
    would return this result as `ok` with a silently trimmed body — on the deployment where it
    matters most. BigQuery is the same story for a different reason: no DB-API cursor exists there
    either. Both report overflow the only way the contract has, and this asserts the chokepoint
    reads it."""
    injected = _Executor(truncated=True)
    assert not hasattr(injected, "cursor")  # nothing cursor-shaped anywhere in this path

    env = execute_sql.execute_guarded(
        "SELECT c FROM t", "acme", None, executor=injected, no_safety=True,
    )

    assert env.status == "refused"
    assert env.refusal.rule == guardrail.RULE_RESOURCE_LIMIT


def test_the_refusal_names_the_configured_ceiling(monkeypatch, _no_creds):
    """The ceiling is a deployment setting, not a data value, so it may be named — ACE-038 already
    puts the timeout in its detail on that reasoning. A bound the caller cannot see is one it
    cannot plan around."""
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "250")

    env = execute_sql.execute_guarded(
        "SELECT c FROM t", "acme", None, executor=_Executor(truncated=True), no_safety=True,
    )

    assert "250" in env.refusal.detail


# --- the remediation ----------------------------------------------------------------------------


def test_the_aggregate_remediation_never_mentions_limit():
    """The one direction that produces a wrong answer.

    `LIMIT` on a grouped result drops groups, and a partial breakdown reads exactly like a complete
    one — no row is wrong, the total is. That is worse for the caller than the refusal they just
    got. The text does not warn against `LIMIT`; it does not contain the token at all, because the
    reader is usually an LLM and negation is what an LLM follows least reliably."""
    execute_sql._guard_shape.set("aggregate")

    remediation = execute_sql._resource_limit_refusal(None).remediation

    assert "LIMIT" not in remediation.upper()
    assert "grouping" in remediation


def test_the_listing_remediation_asks_for_order_by_alongside_limit():
    """`LIMIT` without `ORDER BY` still returns an arbitrary subset — emission order is not a
    promise — so the `ORDER BY` is what turns "some rows" into "the rows you asked for"."""
    execute_sql._guard_shape.set("listing")

    remediation = execute_sql._resource_limit_refusal(None).remediation

    assert "LIMIT" in remediation
    assert "ORDER BY" in remediation


def test_the_shapes_give_different_remediations():
    execute_sql._guard_shape.set("listing")
    listing = execute_sql._resource_limit_refusal(None).remediation
    execute_sql._guard_shape.set("aggregate")
    aggregate = execute_sql._resource_limit_refusal(None).remediation

    assert listing != aggregate


def test_with_no_shape_the_remediation_makes_limit_conditional():
    """Two real paths reach here with nothing parsed: the vendored plugin mirror, which cannot
    import `semantic_model.runtime` at all, and `no_safety=True`, which skips the pass that sets
    the shape. Guessing "listing" would hand an aggregate caller the one instruction that corrupts
    their answer, so the text hands the fork back to the caller, who does know which they wrote."""
    execute_sql._guard_shape.set(None)

    remediation = execute_sql._resource_limit_refusal(None).remediation

    assert "LIMIT" in remediation
    # Conditional, never an instruction on its own.
    assert "if it is a plain row listing" in remediation.lower()


def test_every_remediation_is_contract_valid():
    """`Refusal.__post_init__` rejects an empty detail or remediation, so a shape whose text was
    missing would raise at construction rather than reach a caller. This walks all three."""
    for shape in (None, "listing", "aggregate"):
        execute_sql._guard_shape.set(shape)
        refusal = execute_sql._resource_limit_refusal(None)
        assert refusal.detail.strip()
        assert refusal.remediation.strip()


# --- the shape predicate ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("SELECT a FROM t", "listing"),
        ("SELECT DISTINCT a FROM t", "listing"),
        # A window function returns one row per input row, so it is a listing and `LIMIT` is the
        # right advice for it — `exp.Window` is deliberately not `exp.Group`.
        ("SELECT a, row_number() OVER (ORDER BY b) FROM t", "listing"),
        # A bare aggregate returns one row and cannot reach the bound; classified, not special-cased.
        ("SELECT count(*) FROM t", "listing"),
        ("SELECT a, count(*) FROM t GROUP BY a", "aggregate"),
        ("SELECT a, sum(x) FROM t GROUP BY ROLLUP(a)", "aggregate"),
        # Grouped inside a CTE, plain outer select: the result is still a breakdown.
        ("WITH g AS (SELECT a, count(*) c FROM t GROUP BY a) SELECT * FROM g", "aggregate"),
        # One grouped arm is enough. Looking anywhere rather than at the outermost select is what
        # makes ambiguity resolve toward the text that never says `LIMIT`.
        ("SELECT a FROM t UNION SELECT b, count(*) FROM u GROUP BY b", "aggregate"),
    ],
)
def test_statement_shape(sql, expected):
    sqlglot = pytest.importorskip("sqlglot")
    from semantic_model import runtime as RT

    class _Ctx:
        tree = None

    ctx = _Ctx()
    ctx.tree = sqlglot.parse_one(sql)
    assert RT.statement_shape(ctx) == expected


def test_statement_shape_is_none_without_a_tree():
    from semantic_model import runtime as RT

    class _Ctx:
        tree = None

    assert RT.statement_shape(None) is None       # sqlglot absent -> no GuardContext at all
    assert RT.statement_shape(_Ctx()) is None     # SQL did not parse -> no tree
