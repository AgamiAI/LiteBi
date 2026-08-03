"""ACE-093 — the statement handed to the driver is the statement the caller sent, byte for byte.

`runtime._drop_fanout_joins` parsed the caller's statement, removed the JOINs a fan trap ran
through, and returned `tree.sql()`. `_model_safety` swapped that string in and rebuilt the guard
context around it, so the database received a statement chosen by us. Principle 1 forbids it
outright; principle 6 says why it was the wrong shape of answer even where the analysis was right,
since whether a multiplied total is a bug depends on the question this layer never sees.

It was the last such mechanism. ACE-042 deleted the default-filter injector before it, and with both
gone the property below is assertable for the first time: not merely "we do not inject", but
"executed == received", which is stronger and decidable in one comparison.

**Byte, not semantic.** The comparison is `==` on the strings. That is the only reading a test can
decide without a second parser, and it forbids round-tripping a statement through sqlglot on the way
to the driver even where the round trip changes nothing meaningful — which is the actual defect,
since `tree.sql()` normalises quoting and whitespace whether or not a join was dropped.
"""

from __future__ import annotations

import ast
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402
from semantic_model import runtime as rt  # noqa: E402

PROFILE = "acme"
AREA = "sales"


# --- fixtures ---------------------------------------------------------------


def _write_model(root: Path) -> None:
    """`orders` and `order_items`, related many-to-one so a fan trap is reachable.

    Every column each statement below names is declared, because the column-scope gate is
    fail-closed: an undeclared name refuses before the executor is reached and the byte comparison
    would then be vacuous rather than false.
    """
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


class _SpyExecutor:
    """Records the exact string the executor was handed.

    Shaped after `test_ace088_executed_statement.py::_SpyExecutor`, and copied rather than imported
    for the reason that file gives: the fixture is the spec of what the assertions mean, so it must
    not be re-pointed by an edit to another test file.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, str]] = []

    def execute(self, vetted_sql: str, creds: dict, *, profile: str) -> execute_sql.ExecResult:
        self.calls.append((vetted_sql, creds, profile))
        return execute_sql.ExecResult(columns=["n"], rows=[(1,)], truncated=False)


@pytest.fixture()
def shop(tmp_path, monkeypatch):
    """The model above plus a real warehouse holding both tables."""
    artifacts = tmp_path / "artifacts"
    _write_model(artifacts / PROFILE)

    warehouse = tmp_path / "warehouse.db"
    con = sqlite3.connect(warehouse)
    con.execute("CREATE TABLE orders (id INTEGER, total NUMERIC)")
    con.execute("CREATE TABLE order_items (id INTEGER, order_id INTEGER, qty INTEGER)")
    con.commit()
    con.close()

    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(artifacts))
    monkeypatch.setenv("DATASOURCE_URL__ACME", f"sqlite:///{warehouse}")
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    monkeypatch.delenv("AGAMI_ORG_ID", raising=False)
    monkeypatch.delenv("AGAMI_SQL_TIMEOUT_S", raising=False)
    return SimpleNamespace(artifacts=artifacts, warehouse=warehouse)


# --- SC-2: the executed statement equals the received one -------------------
#
# Each of these clears every gate, which is what makes the comparison mean something: a refused
# statement never reaches the executor, so `spy.calls` would be empty and an `==` over it would pass
# by never running. `test_a_refused_statement_never_reaches_the_executor` pins that distinction.
#
# Chosen for the ways a re-serialiser changes a string without changing its meaning. `tree.sql()`
# would have normalised every one of them.
BYTE_IDENTICAL = {
    "plain": "SELECT id FROM orders",
    "trailing line comment": "SELECT id FROM orders -- keep this comment",
    "leading line comment": "-- why this query exists\nSELECT id FROM orders",
    "block comment mid-statement": "SELECT /* inline */ id FROM orders",
    "irregular whitespace": "SELECT\t\tid\n\n   FROM    orders",
    "trailing newline": "SELECT id FROM orders\n",
    "lowercase keywords": "select id from orders",
    "double-quoted identifiers": 'SELECT "id" FROM "orders"',
    # A quoting style the default sqlglot dialect does not read as an identifier. It still reaches
    # the executor, so the property has to hold for it too — arguably most of all, since this is
    # exactly the statement a re-serialiser would mangle beyond recognition.
    "bracket-quoted identifiers": "SELECT id FROM [orders]",
    "joined, no aggregate": "SELECT o.total, i.qty FROM orders o JOIN order_items i ON i.order_id = o.id",
}


@pytest.mark.parametrize("sql", BYTE_IDENTICAL.values(), ids=BYTE_IDENTICAL.keys())
def test_the_executed_statement_equals_the_received_one(shop, sql):
    """The bytes handed to the driver, compared to the bytes the caller sent.

    Asserted at the executor rather than at `_model_safety`'s return, because the executor is the
    port boundary: it is where every path ends up, and a rewrite anywhere between the tool argument
    and it would show up here regardless of which layer introduced it.
    """
    spy = _SpyExecutor()
    env = execute_sql.execute_guarded(sql, PROFILE, AREA, executor=spy)

    assert env.status == "ok", env
    assert len(spy.calls) == 1
    assert spy.calls[0][0] == sql


def test_a_refused_statement_never_reaches_the_executor(shop):
    """The anti-vacuity guard for the battery above.

    Every assertion up there is `executed == received` over a list that is empty when a gate
    refuses, so an equality that never runs would look identical to one that holds. This pins that a
    refusal really does produce no call, which is what makes a non-empty `spy.calls` above evidence
    rather than an assumption.
    """
    spy = _SpyExecutor()
    env = execute_sql.execute_guarded("SELECT id FROM ghost_table", PROFILE, AREA, executor=spy)

    assert env.status == "refused", env
    assert spy.calls == []


def test_prepare_hands_back_the_statement_it_was_given(shop, tmp_path):
    """`sm prepare` is the OTHER execution path, and the byte-identity claim has to cover it.

    The query skill calls it on every tier and runs whatever `sql` comes back, so a rewrite here ran
    on psql and mysql without ever passing through `execute_sql`. It returned `rewritten_sql` for an
    aggregation-only fan trap until this slice; now it echoes or refuses.
    """
    root = shop.artifacts / PROFILE
    for label, sql in BYTE_IDENTICAL.items():
        sql_file = tmp_path / "stmt.sql"
        sql_file.write_text(sql)
        proc = subprocess.run(
            [sys.executable, "-m", "semantic_model.cli", "prepare", str(root),
             "--area", AREA, "--sql-file", str(sql_file)],
            capture_output=True, text=True, cwd=PKG_SRC,
        )
        assert proc.returncode == 0, (label, proc.stderr)
        assert json.loads(proc.stdout)["sql"] == sql, label


# --- SC-1: the rewrite and its residue are gone -----------------------------


def test_the_rewriter_is_gone_not_wrapped():
    """Deleted, not left as an empty shell returning `None`. A surviving helper is a re-entry point
    for the next person who decides one safe transform is worth it."""
    assert not hasattr(rt, "_drop_fanout_joins")
    assert "_drop_fanout_joins" not in rt.__all__
    # The two helpers that existed only to decide whether a trap was rewrite-eligible.
    assert not hasattr(rt, "_has_raw_non_grouped_columns")
    assert not hasattr(rt, "_tables_referenced_outside_from")


def test_the_result_carries_no_statement_and_no_rewrite_action():
    """`PreFlightResult` described a rewrite in three places: the action value, the statement it had
    authored, and the statement it started from. All three are gone, and `as_dict` is what `sm
    preflight` serialises, so this pins that command's JSON contract too."""
    fields = set(rt.PreFlightResult.__dataclass_fields__)
    assert "rewritten_sql" not in fields
    assert "original_sql" not in fields

    result = rt.PreFlightResult(None, "allow", reason="x")
    assert set(result.as_dict()) == {"risk", "action", "reason", "suggestion", "triggering_joins"}


def test_the_preflight_signature_has_no_rewrite_switch():
    """`allow_rewrite` distinguished a top-level SELECT from a set-operation arm, and the only thing
    it decided was rewrite-eligibility. With no rewrite the two are analysed identically, and a
    surviving parameter would be a switch with one position."""
    import inspect

    assert "allow_rewrite" not in inspect.signature(rt._preflight_select).parameters


# The trees a shipped surface is assembled from. `plugins/agami/lib` is the vendored regeneration of
# `packages/agami-core/src`, drift-checked by `dev.py check`; it is scanned anyway, because a scan
# that trusted the drift check would go quiet the day someone edited the copy directly.
_SCAN_ROOTS = (
    PKG_SRC,
    REPO_ROOT / "plugins" / "agami" / "lib",
    REPO_ROOT / "plugins" / "agami" / "skills",
    REPO_ROOT / "plugins" / "agami" / "shared",
    REPO_ROOT / "docs",
)

_GONE = ("_drop_fanout_joins", "rewritten_sql", "original_sql", "auto_rewrite", "allow_rewrite")


def _python_sources() -> list[Path]:
    return sorted(p for root in _SCAN_ROOTS for p in root.rglob("*.py"))


def test_there_are_sources_to_scan():
    """Guard against a glob that silently matches nothing, which would make the scans below pass by
    never looking at anything."""
    sources = _python_sources()
    assert len(sources) > 20, sources
    assert any(p.name == "runtime.py" for p in sources)


def test_no_code_references_the_deleted_rewrite():
    """Grep-clean, over CODE rather than raw text.

    Comments are excluded on purpose and the exclusion is the point: the deletion left tombstones
    that name what went and say not to re-add it, exactly as ACE-042's did, and a raw-text scan
    would force those to be written in circumlocutions nobody can grep for later. What must not
    survive is a live reference, so the scan reads the token stream with comments stripped.
    """
    import io
    import tokenize

    offenders: list[str] = []
    for path in _python_sources():
        source = path.read_text()
        if not any(name in source for name in _GONE):
            continue
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            if tok.string in _GONE:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{tok.start[0]}: {tok.string}")
    assert not offenders, f"live references to the deleted rewrite: {offenders}"


def test_no_shipped_prose_promises_a_rewrite():
    """The markdown and HTML surfaces are payload: a skill file IS the prompt, loaded verbatim, so a
    line telling the assistant to expect a rewritten statement is a behavioural bug rather than a
    stale comment."""
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{n}: {line.strip()[:80]}"
        for root in _SCAN_ROOTS
        for pattern in ("*.md", "*.html")
        for path in root.rglob(pattern)
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if "auto_rewrite" in line or '"rewritten"' in line or "rewritten: true" in line
    ]
    assert not offenders, f"shipped prose still promises a rewrite: {offenders}"


# --- SC-4: nothing re-serialises a parsed statement onto the execution path --


def test_no_parsed_statement_is_re_serialised_onto_the_execution_path():
    """`.sql()` regenerates a statement from a parsed tree. Doing that anywhere a statement can
    reach the driver is what made byte-identity unassertable, whether or not the regeneration
    changed anything meaningful.

    One call survives, and it is allowlisted by LOCATION rather than by count so that a second one
    appearing in the same function still fails. `pre_flight_check` walks each arm of a set operation
    and passes `arm.sql()` down as the arm's own text — it reaches a `reason` string and nothing
    else, and the statement `pre_flight_check` returns is always the caller's own.
    """
    allowed = {("runtime.py", "pre_flight_check")}
    offenders: list[str] = []

    for path in _python_sources():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Attribute)
                        and inner.func.attr == "sql"
                        and not inner.args):
                    if (path.name, node.name) in allowed:
                        continue
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}:{inner.lineno}: "
                        f"re-serialisation inside {node.name}()"
                    )
    assert not offenders, f"a parsed statement is re-serialised: {offenders}"


# --- SC-3: the trap refuses, on both routes ---------------------------------


FAN_SQL = "SELECT SUM(o.total) FROM orders o JOIN order_items i ON i.order_id = o.id"


def test_the_aggregation_only_fan_trap_is_refused_end_to_end(shop):
    """The shape that used to be rewritten, driven through the whole chokepoint rather than through
    `pre_flight_check` alone.

    It is the aggregation-only case: the many side is touched nowhere but the ON clause, which is
    precisely what made it look safe to rewrite. The caller now gets a refusal and a way forward
    instead of a silently different statement.
    """
    spy = _SpyExecutor()
    env = execute_sql.execute_guarded(FAN_SQL, PROFILE, AREA, executor=spy)

    assert env.status == "refused", env
    assert spy.calls == []


def test_the_refusal_no_longer_offers_a_rewrite(shop):
    """The `reason` used to end "Rewrite would change result shape", which offered a transform that
    no longer exists. Nothing replaced that sentence: the caller's way forward lives in
    `suggestion`, which this slice does not touch, so the reason is free to state only the fact."""
    from semantic_model import loader as L

    org = L.load_datasource(shop.artifacts / PROFILE)
    pf = rt.pre_flight_check(FAN_SQL, org)

    assert pf.risk == "fan_trap" and pf.action == "refuse"
    assert "rewrite" not in pf.reason.lower(), pf.reason
    assert pf.suggestion and "pre-aggregate" in pf.suggestion
