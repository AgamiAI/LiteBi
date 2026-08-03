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


# --- the refusal discloses nothing --------------------------------------------------------------


def test_the_refusal_echoes_nothing_from_the_statement(_no_creds):
    """`Refusal.detail` and `remediation` are value-free by contract: never raw SQL, never a data
    value, and never an enumeration of the declared surface.

    Structurally this refusal cannot leak — its four strings are constants and the only thing
    interpolated is an integer from the environment. Asserted anyway, because "the text happens to
    be static today" is exactly the property a later edit breaks while adding something helpful."""
    env = execute_sql.execute_guarded(
        "SELECT secret_column FROM confidential_table", "acme", None,
        executor=_Executor(truncated=True), no_safety=True,
    )

    text = env.refusal.detail + env.refusal.remediation
    assert "secret_column" not in text
    assert "confidential_table" not in text
    assert "SELECT" not in text


# --- the deletions are deletions ----------------------------------------------------------------


def test_the_trim_is_deleted_not_bypassed():
    """The trim and its plumbing are gone from the modules, not merely unreachable.

    Asserted on the imported modules rather than on their source text, for two reasons. A text scan
    cannot tell code from a comment, and several of these names are still *written about* in
    comments that explain what was removed and why — which is the documentation working, not a
    leftover. And `truncated` on its own is still a live word in `tools`: the schema-sizing tool has
    a flag of that name, the model index has a floor, and the audit row carries `sql_truncated`.
    None of those are this. Naming the symbols avoids both traps.

    The `--max-rows` flag is covered where it is observable —
    `test_ace044_bounded_fetch.py::test_the_fork_command_carries_no_per_call_cap` asserts the parent
    no longer appends it, which is what would break every forked call if it did."""
    import tools

    for module, gone in (
        (execute_sql, "_flag_truncated"),
        (execute_sql, "_max_rows_override"),
        (execute_sql, "_write_cursor_csv"),
        (execute_sql, "_emit_or_err"),
        (tools, "_executor_truncated"),
    ):
        assert not hasattr(module, gone), f"{module.__name__}.{gone} survived the deletion"

    # The ten per-engine CSV wrappers went with `_emit_or_err` — they trimmed and flagged with no
    # refusal, so anything wired back onto them would have gone round the chokepoint. `_run_<db>`,
    # the shared connect-and-run behind the built-in executor, is what stayed.
    for engine in ("postgres", "mysql", "snowflake", "bigquery", "sqlite",
                   "sqlserver", "oracle", "databricks", "trino", "duckdb"):
        assert not hasattr(execute_sql, f"_execute_{engine}")
        assert hasattr(execute_sql, f"_run_{engine}")


# --- the shape actually reaches the refusal ------------------------------------------------------
#
# Everything above sets `_guard_shape` by hand, which tests the builder and nothing else. These two
# drive a real statement through the real model pass, so the whole chain is on the hook: the safety
# pass classifies off the tree it already parsed, publishes the shape, and the chokepoint reads it
# back out to word the refusal. Deleting the one line that publishes it leaves every other test in
# this repo green, which is exactly why these exist.


_AREA = "sales"
_PROFILE = "acme"


@pytest.fixture()
def shop(tmp_path, monkeypatch):
    """The smallest model the safety pass will accept, over a warehouse with two groups in it."""
    yaml = pytest.importorskip("yaml")
    pytest.importorskip("pydantic")
    pytest.importorskip("sqlglot")

    root = tmp_path / "artifacts" / _PROFILE
    (root / "subject_areas" / _AREA / "tables").mkdir(parents=True)
    (root / "datasource.yaml").write_text(
        yaml.safe_dump({"datasource": "Shop", "version": 1,
                        "subject_areas": [f"subject_areas/{_AREA}"]})
    )
    (root / "subject_areas" / _AREA / "subject_area.yaml").write_text(
        yaml.safe_dump({"name": _AREA, "tables": [
            {"storage_connection": "c", "schema": "public", "table": "orders"}]})
    )
    (root / "subject_areas" / _AREA / "tables" / "orders.yaml").write_text(
        yaml.safe_dump({
            "name": "orders", "schema": "public", "storage_connection": "c", "grain": ["id"],
            "description": "orders", "columns": [
                {"name": "id", "type": "integer", "primary_key": True},
                {"name": "grp", "type": "string"},
            ],
        })
    )

    warehouse = tmp_path / "warehouse.db"
    con = __import__("sqlite3").connect(warehouse)
    con.execute("CREATE TABLE orders (id INTEGER, grp TEXT)")
    con.executemany("INSERT INTO orders VALUES (?, ?)", [(i, f"g{i % 4}") for i in range(20)])
    con.commit()
    con.close()

    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("DATASOURCE_URL__ACME", f"sqlite:///{warehouse}")
    for var in ("AGAMI_DB_URL", "APP_DATABASE_URL", "AGAMI_ORG_ID", "AGAMI_SQL_TIMEOUT_S"):
        monkeypatch.delenv(var, raising=False)
    # One row over the cap on both statements below: 20 rows, and 4 groups.
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "3")


def _refuse(sql: str):
    env = execute_sql.execute_guarded(
        sql, _PROFILE, _AREA, executor=execute_sql.BUILTIN_EXECUTOR,
    )
    assert env.status == "refused", env
    assert env.refusal.rule == guardrail.RULE_RESOURCE_LIMIT
    return env.refusal


def test_a_row_listing_is_told_to_bound_it(shop):
    remediation = _refuse("SELECT id FROM orders").remediation

    assert "LIMIT" in remediation and "ORDER BY" in remediation
    # Not the shape-neutral fallback, which also names both but makes them conditional. Without
    # this the test passes whether or not the shape ever reached the refusal.
    assert "if it is a plain row listing" not in remediation.lower()


def test_an_aggregate_is_told_to_narrow_the_grouping(shop):
    """The whole reason the shape is carried at all.

    This statement groups, so the refusal must not tell the caller to add a `LIMIT` — that would
    drop groups and hand back a breakdown that reads as complete. If the safety pass ever stops
    publishing the shape, this refusal silently falls back to the shape-neutral text and this
    assertion is what notices."""
    remediation = _refuse("SELECT grp, COUNT(*) FROM orders GROUP BY grp").remediation

    assert "LIMIT" not in remediation.upper()
    assert "grouping" in remediation


def test_the_tool_surface_advertises_neither_max_rows_nor_truncated():
    """Both halves of what a client can see: the argument it may send and the shape it is promised.

    Read off the live registry rather than the source text, so this cannot pass because a literal
    moved."""
    import tools

    spec = tools.TOOLS["execute_sql"]
    assert "max_rows" not in spec["inputSchema"]["properties"]
    # `additionalProperties: False` already rejects a caller that still sends it, which is the
    # behaviour we want over silently ignoring one.
    assert spec["inputSchema"]["additionalProperties"] is False
    assert "truncated" not in spec["description"]
    assert "max_rows" not in spec["description"]
