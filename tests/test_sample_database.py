"""Smoke tests for the shipped sample database (plugins/agami/samples/store).

Guards the no-database onboarding path (agami-connect Phase 0s):
  * seed.sql builds deterministically via the stdlib builder (no sqlite3 CLI),
  * the prebuilt model loads + validates,
  * the headline demo behaves — the fan-trap query is REFUSED and the
    chasm-trap query is REFUSED by the pre-flight, and the correct
    revenue-by-category query returns the expected, frozen numbers.

If any of these break, the launch demo is broken — so this runs in CI.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DIR = REPO_ROOT / "plugins" / "agami" / "samples" / "store"
MODEL_DIR = SAMPLE_DIR / "model"
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))
sys.path.insert(0, str(SAMPLE_DIR))

import build_sample  # noqa: E402
from semantic_model import loader as L  # noqa: E402
from semantic_model import runtime as RT  # noqa: E402
from semantic_model import validator as V  # noqa: E402

# Expected, frozen counts — the dataset is 100% deterministic.
EXPECTED_ROWS = {
    "categories": 8,
    "products": 64,
    "customers": 500,
    "orders": 4000,
    "order_items": 10000,
    "payments": 3800,
    "refunds": 400,
    "plans": 5,
    "subscriptions": 400,
    "invoices": 2899,
}


@pytest.fixture(scope="module")
def db(tmp_path_factory) -> sqlite3.Connection:
    """Build the sample DB via the STDLIB builder (no sqlite3 CLI — CI-portable)."""
    out = tmp_path_factory.mktemp("sample") / "store.db"
    method = build_sample.build(out, prefer_cli=False)
    assert method == "stdlib"
    conn = sqlite3.connect(str(out))
    yield conn
    conn.close()


def test_build_is_deterministic(tmp_path):
    """Two stdlib builds produce byte-identical files (no random(), no rounding drift)."""
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    build_sample.build(a, prefer_cli=False)
    build_sample.build(b, prefer_cli=False)
    assert a.read_bytes() == b.read_bytes()


@pytest.mark.parametrize("table,expected", EXPECTED_ROWS.items())
def test_row_counts(db, table, expected):
    assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == expected


def test_customer_names_are_unique(db):
    """Every customer has a distinct full_name (regression guard: an earlier seed
    indexed first+last both on n%20, yielding only ~20 names across 500 customers,
    which made 'top customers' show repeated names)."""
    distinct = db.execute("SELECT COUNT(DISTINCT full_name) FROM customers").fetchone()[0]
    assert distinct == 500


def test_full_name_is_not_sensitive():
    """full_name is the display label for a customer — it must be queryable; only
    email/phone are sensitive."""
    org = L.load_datasource(MODEL_DIR)
    cols = {c.name: c for area in org.subject_areas for t in area.tables_defined for c in t.columns if t.name == "customers"}
    assert cols["full_name"].sensitive is False
    assert cols["email"].sensitive is True
    assert cols["phone"].sensitive is True


def test_model_validates():
    org = L.load_datasource(MODEL_DIR)
    res = V.validate(org)
    assert res.ok, res.errors


def test_context_surfaces_row_counts():
    """The answer receipt's '≈N rows' provenance reads performance_hints.estimated_row_count
    from the assembled context. Regression guard for the 'rows unknown' bug, where the
    context/bundle include-list dropped performance_hints even though the model had it."""
    org = L.load_datasource(MODEL_DIR)
    # the compound context fetch (default include)
    ctx = L.get_table_context(org, ["subscriptions", "plans"], area="agami-example")
    assert ctx["tables"]["subscriptions"]["performance_hints"]["estimated_row_count"] == 400
    assert ctx["tables"]["plans"]["performance_hints"]["estimated_row_count"] == 5
    # and the subject-area bundle the traversal uses
    bundle = L.get_subject_area_bundle(org, "agami-example")
    assert bundle["tables"]["orders"]["performance_hints"]["estimated_row_count"] == 4000


def test_revenue_metric_is_signed_off():
    """The committed model ships signed-off metrics, so demo answers carry no
    'not reviewed' warning."""
    org = L.load_datasource(MODEL_DIR)
    metrics = {m.name: m for area in org.subject_areas for m in area.metrics}
    assert "revenue" in metrics
    assert metrics["revenue"].review_state == "approved"


def test_fan_trap_is_refused():
    """Summing the order-grain total across the line-item join double-counts —
    the pre-flight must refuse it. This is the headline demo."""
    org = L.load_datasource(MODEL_DIR)
    sql = (
        "SELECT cat.name, SUM(o.total_amount) FROM orders o "
        "JOIN order_items oi ON oi.order_id = o.id "
        "JOIN products p ON p.id = oi.product_id "
        "JOIN categories cat ON cat.id = p.category_id GROUP BY cat.name"
    )
    pf = RT.pre_flight_check(sql, org)
    assert pf.risk == "fan_trap"
    assert pf.action == "refuse"


def test_chasm_trap_is_refused():
    org = L.load_datasource(MODEL_DIR)
    sql = (
        "SELECT c.id, SUM(o.total_amount), SUM(s.id) FROM customers c "
        "JOIN orders o ON o.customer_id = c.id "
        "JOIN subscriptions s ON s.customer_id = c.id GROUP BY c.id"
    )
    pf = RT.pre_flight_check(sql, org)
    assert pf.risk == "chasm_trap"
    assert pf.action == "refuse"


def test_execute_sql_refuses_fan_trap_cleanly(tmp_path):
    """End-to-end through execute_sql.py (the path agami-query uses): the fan-trap
    query must be REFUSED with the helpful JSON, NOT crash. Regression guard for the
    missing `import json` that made every Python-tier pre-flight refusal a NameError.
    """
    import json
    import os
    import subprocess

    # Wire a temp profile exactly as Phase 0s does.
    art = tmp_path / "artifacts"
    (art / "local" / "samples").mkdir(parents=True)
    db_path = art / "local" / "samples" / "store.db"
    build_sample.build(db_path, prefer_cli=False)
    (art / "local" / "credentials").write_text(
        f"[agami-example]\ntype = sqlite\npath = {db_path}\n", encoding="utf-8"
    )
    (art / "local" / ".config").write_text(
        json.dumps({"active_profile": "agami-example", "artifacts_dir": str(art)}), encoding="utf-8"
    )
    import shutil
    shutil.copytree(MODEL_DIR, art / "agami-example")

    fan_trap = (
        "SELECT cat.name, SUM(o.total_amount) FROM orders o "
        "JOIN order_items oi ON oi.order_id = o.id "
        "JOIN products p ON p.id = oi.product_id "
        "JOIN categories cat ON cat.id = p.category_id GROUP BY cat.name"
    )
    env = {**os.environ, "AGAMI_ARTIFACTS_DIR": str(art)}
    proc = subprocess.run(
        [sys.executable, "-m", "execute_sql",
         "--profile", "agami-example", "--area", "agami-example", "--sql", fan_trap],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 1, proc.stderr
    assert "preflight_refused" in proc.stderr
    assert "Traceback" not in proc.stderr and "NameError" not in proc.stderr


def test_correct_revenue_by_category(db):
    """The guard-safe (line-item grain) revenue query returns the frozen answer."""
    rows = db.execute(
        "SELECT cat.name, ROUND(SUM(oi.quantity * oi.unit_price), 2) AS revenue "
        "FROM order_items oi "
        "JOIN products p ON p.id = oi.product_id "
        "JOIN categories cat ON cat.id = p.category_id "
        "GROUP BY cat.name ORDER BY revenue DESC"
    ).fetchall()
    assert len(rows) == 8
    assert rows[0][0] == "Home & Kitchen"
    assert round(rows[0][1]) == 1360276


# ---------------------------------------------------------------------------
# Sensitive-column (PII) projection guard — enforced in execute_sql's shared
# safety pass, so the skill, the MCP server, and cron all protect PII identically.
# ---------------------------------------------------------------------------

def test_sensitive_projection_refuses_raw():
    """Projecting a raw sensitive value (bare, aliased, via *, or via MIN/MAX) is refused."""
    org = L.load_datasource(MODEL_DIR)
    for sql in [
        "SELECT email FROM customers",
        "SELECT * FROM customers",
        "SELECT c.email FROM customers c JOIN orders o ON o.customer_id = c.id",
        "SELECT MIN(email) FROM customers",
        "SELECT email AS contact FROM customers",
    ]:
        assert RT.check_sensitive_projection(sql, org).action == "refuse", sql


def test_sensitive_projection_allows_aggregate_filter_and_nonsensitive():
    """COUNT/COUNT(DISTINCT), WHERE-only use, and non-sensitive columns are allowed."""
    org = L.load_datasource(MODEL_DIR)
    for sql in [
        "SELECT COUNT(DISTINCT email) FROM customers",
        "SELECT COUNT(email) FROM customers",
        "SELECT id, full_name FROM customers",
        "SELECT country, COUNT(*) FROM customers WHERE email LIKE '%@x.com' GROUP BY country",
        "SELECT * FROM (SELECT id, full_name FROM customers)",
    ]:
        assert RT.check_sensitive_projection(sql, org).action == "allow", sql


def test_sensitive_projection_refuses_set_operation_arm():
    """A sensitive column projected in ANY set-operation arm is refused. A UNION parses
    to exp.SetOperation (not exp.Select), so a gate that only inspects the root SELECT
    would let `SELECT id … UNION SELECT email …` slip PII straight past the gate."""
    org = L.load_datasource(MODEL_DIR)
    for sql in [
        "SELECT id FROM customers UNION SELECT email FROM customers",
        "SELECT full_name FROM customers UNION ALL SELECT email FROM customers",
        "SELECT id FROM customers INTERSECT SELECT email FROM customers",
        "(SELECT id FROM customers) UNION (SELECT email FROM customers)",
    ]:
        assert RT.check_sensitive_projection(sql, org).action == "refuse", sql


def test_sensitive_projection_allows_union_of_nonsensitive():
    """A set operation whose every arm projects only non-sensitive columns still passes —
    the arm-walking fix must not over-refuse a clean UNION."""
    org = L.load_datasource(MODEL_DIR)
    sql = "SELECT id FROM customers UNION SELECT country FROM customers"
    assert RT.check_sensitive_projection(sql, org).action == "allow", sql


@pytest.fixture
def wired_artifacts(tmp_path):
    """A temp artifacts dir wired to the sample exactly as Phase 0s does."""
    import json
    import os
    import shutil
    art = tmp_path / "artifacts"
    (art / "local" / "samples").mkdir(parents=True)
    db_path = art / "local" / "samples" / "store.db"
    build_sample.build(db_path, prefer_cli=False)
    creds = art / "local" / "credentials"
    creds.write_text(f"[agami-example]\ntype = sqlite\npath = {db_path}\n", encoding="utf-8")
    os.chmod(creds, 0o600)  # execute_sql refuses world-readable credentials
    (art / "local" / ".config").write_text(
        json.dumps({"active_profile": "agami-example", "artifacts_dir": str(art)}), encoding="utf-8")
    shutil.copytree(MODEL_DIR, art / "agami-example")
    return art


def _run_execute_sql(art, sql):
    import os
    import subprocess
    return subprocess.run(
        [sys.executable, "-m", "execute_sql",
         "--profile", "agami-example", "--area", "agami-example", "--sql", sql],
        capture_output=True, text=True, env={**os.environ, "AGAMI_ARTIFACTS_DIR": str(art)})


def test_execute_sql_refuses_raw_pii_both_paths(wired_artifacts):
    """End-to-end through execute_sql.py — the path BOTH the skill and the MCP server
    use — a raw PII projection is refused with a structured error, not leaked."""
    proc = _run_execute_sql(wired_artifacts, "SELECT full_name, email FROM customers LIMIT 5")
    assert proc.returncode == 1, proc.stderr
    assert "sensitive_columns" in proc.stderr
    assert "@example.com" not in proc.stdout  # no raw email leaked
    assert "Traceback" not in proc.stderr


def test_execute_sql_allows_pii_aggregate(wired_artifacts):
    """COUNT(DISTINCT email) runs and returns the count — sensitive cols restrict output, not the query."""
    proc = _run_execute_sql(wired_artifacts, "SELECT COUNT(DISTINCT email) AS n FROM customers")
    assert proc.returncode == 0, proc.stderr
    assert "500" in proc.stdout


def test_customer_spend_is_skewed(db):
    """Total spend per customer follows a long tail — the #1 customer clearly
    outspends the 5th — so 'top customers' isn't a near-tie (regression guard:
    a uniform order assignment gave every customer ~8 orders and near-identical
    spend, e.g. three customers tied to the cent)."""
    rows = db.execute(
        "SELECT ROUND(SUM(o.total_amount), 2) AS spend "
        "FROM customers c JOIN orders o ON o.customer_id = c.id "
        "GROUP BY c.id ORDER BY spend DESC LIMIT 5"
    ).fetchall()
    spends = [r[0] for r in rows]
    assert len(set(spends)) == 5, f"top-5 spends should be distinct, got {spends}"
    assert spends[0] >= 2 * spends[4], f"expected a clear long tail, got {spends}"
