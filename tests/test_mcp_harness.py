"""
Tests for mcp_harness (packages/agami-core/src/mcp_harness.py) — the local stdio MCP server.

Two contracts anchor this file:

  1. **Read-only guarantee.** The server exposes SQL execution to an LLM, so
     `check_read_only` MUST reject anything that isn't a single SELECT /
     WITH...SELECT — including the Postgres `WITH ... DELETE` data-modifying-CTE
     edge and comment-hidden mutations.

  2. **Privacy invariant.** The server is part of the local-first story: it must
     contain NO network primitives. If you add a feature that needs the network,
     it does not belong in this file (that is the hosted product, by design).

The JSON-RPC handshake is exercised end-to-end via subprocess with no DB
required (initialize / tools/list / list_datasources).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# mcp_harness lives in the installed agami-core package — import it directly, no sys.path.
# SERVER is the on-disk module file, used only for the source-level no-network scan.
SERVER = REPO_ROOT / "packages" / "agami-core" / "src" / "mcp_harness.py"

# The tool impls + helpers live in the shared registry module (tools); mcp_harness is the
# thin stdio adapter, exercised over the protocol via subprocess below.
from tools import (
    _classify_exit,
    _distill_for_llm,
    _resolve_receipt,
    _resolve_units,
    check_read_only,
    resolve_artifacts_dir,
    resolve_profile,
)


def test_distill_strips_human_scaffolding_for_llm_context():
    # datasource.md is injected into the model's prompt; the human-only HTML comment
    # scaffolding ("auto-generated", "edit freely") is noise the LLM shouldn't see — the
    # MCP path must strip it to match the query skill's read path.
    raw = (
        "# About this database\n\n"
        "<!-- Auto-generated SUMMARY (only because no org context was provided).\n"
        "     Edit freely: what the company is. -->\n\n"
        "**acme** — 8 tables across 1 subject area.\n\n"
        "## Key terminology\n- **MRR** — monthly recurring revenue\n"
    )
    out = _distill_for_llm(raw)
    assert "<!--" not in out and "Auto-generated" not in out and "Edit freely" not in out
    assert "**acme** — 8 tables across 1 subject area." in out
    assert "**MRR** — monthly recurring revenue" in out
    assert "\n\n\n" not in out          # collapsed the blank lines the comment left behind
    assert _distill_for_llm(None) == "" and _distill_for_llm("") == ""


# --- Read-only guard --------------------------------------------------------

def test_plain_select_is_allowed():
    assert check_read_only("SELECT 1") is None
    assert check_read_only("select count(*) from orders") is None


def test_with_select_is_allowed():
    assert check_read_only("WITH t AS (SELECT 1 AS x) SELECT x FROM t") is None


def test_trailing_semicolon_tolerated():
    assert check_read_only("SELECT 1;") is None


def test_leading_paren_select_allowed():
    assert check_read_only("(SELECT 1) UNION (SELECT 2)") is None


def test_ddl_rejected():
    for sql in ("DROP TABLE users", "ALTER TABLE t ADD c int", "TRUNCATE t", "CREATE TABLE t (id int)"):
        assert check_read_only(sql) is not None, sql


def test_dml_rejected():
    for sql in ("INSERT INTO t VALUES (1)", "UPDATE t SET x=1", "DELETE FROM t"):
        assert check_read_only(sql) is not None, sql


def test_multi_statement_rejected():
    assert check_read_only("SELECT 1; SELECT 2") is not None
    assert check_read_only("SELECT 1; DROP TABLE t") is not None


def test_data_modifying_cte_rejected():
    # Postgres allows `WITH x AS (...) DELETE ...` — this MUST be blocked even
    # though it starts with WITH.
    assert check_read_only("WITH x AS (SELECT id FROM t) DELETE FROM t USING x") is not None


def test_comment_hidden_mutation_rejected():
    # A mutation smuggled behind a comment-stripped multi-statement must fail.
    assert check_read_only("SELECT 1 -- harmless\n; DELETE FROM t") is not None


def test_identifier_with_keyword_substring_allowed():
    # Column names like created_date / updated_at / is_deleted contain DDL/DML
    # substrings but are NOT whole-word matches → must still be allowed.
    assert check_read_only("SELECT created_date, updated_at, is_deleted FROM t") is None


def test_empty_rejected():
    assert check_read_only("   ") is not None
    assert check_read_only("-- just a comment") is not None


# --- Resolution order -------------------------------------------------------

def test_resolve_profile_explicit_wins(monkeypatch):
    monkeypatch.setenv("AGAMI_PROFILE", "fromenv")
    assert resolve_profile("explicit") == "explicit"


def test_resolve_profile_env_over_config(monkeypatch):
    monkeypatch.setenv("AGAMI_PROFILE", "fromenv")
    assert resolve_profile() == "fromenv"


def test_resolve_artifacts_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    assert resolve_artifacts_dir() == tmp_path


def test_resolve_units_maps_result_columns(monkeypatch, tmp_path):
    # the MCP execute_sql response formats currency deterministically by resolving
    # result columns -> the model's column/metric `unit`
    import pytest
    pytest.importorskip("pydantic")
    yaml = pytest.importorskip("yaml")
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    p = tmp_path / "p"
    (p / "datasources" / "c").mkdir(parents=True)
    (p / "subject_areas" / "s" / "tables").mkdir(parents=True)
    (p / "subject_areas" / "s" / "metrics").mkdir(parents=True)
    (p / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "p", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/s"]}))
    (p / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"}))
    (p / "subject_areas" / "s" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "s", "tables": [{"storage_connection": "c", "schema": "public", "table": "loans"}]}))
    (p / "subject_areas" / "s" / "tables" / "loans.yaml").write_text(yaml.safe_dump({
        "name": "loans", "schema": "public", "storage_connection": "c", "grain": ["id"], "description": "l",
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "amount", "type": "decimal", "unit": "INR"}]}))
    (p / "subject_areas" / "s" / "metrics" / "total_outstanding.yaml").write_text(yaml.safe_dump({
        "name": "total_outstanding", "calculation": "sum", "bindings": {"PostgreSQL": "SUM(loans.amount)"},
        "unit": "INR", "source_tables": ["loans"], "confidence": "inferred", "review_state": "unreviewed"}))
    # traces the SQL: SUM(amount) inherits amount's INR; COUNT(*) is not currency
    got = _resolve_units("p", "SELECT SUM(amount) AS total_outstanding, COUNT(*) AS cnt FROM loans")
    assert got.get("total_outstanding") == "INR" and "cnt" not in got
    assert _resolve_units("p", "SELECT amount FROM loans").get("amount") == "INR"


def test_resolve_units_degrades_to_empty_without_model(monkeypatch, tmp_path):
    # pure-stdlib guarantee: no model on disk -> {} (numbers still format, just no symbol)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    assert _resolve_units("nonexistent", "SELECT 1") == {}


# --- Exit-code classification ----------------------------------------------

def test_classify_exit_mapping():
    assert _classify_exit(3) == "driver_missing"
    assert _classify_exit(4) == "auth"
    assert _classify_exit(5) == "syntax"
    assert _classify_exit(2) == "dsn"
    assert _classify_exit(99) == "other"


# --- Privacy invariant ------------------------------------------------------

def test_server_makes_no_network_calls():
    """The local server must not import or use any network primitive."""
    src = SERVER.read_text()
    forbidden = ("import socket", "import http", "import urllib", "urllib.request",
                 "requests.", "httpx", "import ftplib", "smtplib", "websocket")
    hits = [tok for tok in forbidden if tok in src]
    assert not hits, f"mcp_harness.py must stay network-free; found: {hits}"


# --- Protocol handshake (no DB required) ------------------------------------

def _rpc_exchange(messages: list[dict]) -> list[dict]:
    stdin = "".join(json.dumps(m) + "\n" for m in messages)
    proc = subprocess.run(
        [sys.executable, "-m", "mcp_harness"],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ},
    )
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def test_initialize_and_tools_list():
    out = _rpc_exchange([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ])
    by_id = {m.get("id"): m for m in out}

    init = by_id[1]["result"]
    assert init["protocolVersion"] == "2025-06-18"  # echoes client's version
    assert init["serverInfo"]["name"] == "agami"
    # The lean playbook signposts the trust flow so a Desktop client surfaces it (soft layer;
    # the hard guarantees are enforced in code). Don't let these silently drop out.
    instr = init["instructions"].lower()
    for token in ("examples-first", "receipt", "review_state"):
        assert token in instr, token

    # The MCP surface is exactly the 4 product tools on stdio (mirrored by HTTP). The granular
    # traversal tools / identify_entity / pre_flight_check / save_correction are deliberately NOT
    # MCP tools (subsumed / folded / skill-operations) — asserted here so the omission can't
    # silently regress. See tests/test_tools_registry.py for the cross-transport assertion.
    tools = {t["name"] for t in by_id[2]["result"]["tools"]}
    assert tools == {
        "list_datasources", "get_datasource_schema",
        "get_prompt_examples", "execute_sql",
    }


def test_execute_sql_rejects_mutation_over_protocol():
    out = _rpc_exchange([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "execute_sql", "arguments": {"sql": "DELETE FROM users"}}},
    ])
    by_id = {m.get("id"): m for m in out}
    payload = json.loads(by_id[2]["result"]["content"][0]["text"])
    assert payload["status"] == "refused"
    assert payload["refusal"]["rule"] == "read_only"
    assert payload["refusal"]["reason"] == "unsafe"
    assert payload["refusal"]["remediation"].strip()


def test_unknown_method_returns_error():
    out = _rpc_exchange([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 9, "method": "does/not/exist"},
    ])
    by_id = {m.get("id"): m for m in out}
    assert by_id[9]["error"]["code"] == -32601


# ---------------------------------------------------------------------------
# Trust receipt + corrections through the MCP — the SAME engine the skill uses,
# so the query→receipt→correct loop is identical in Claude Desktop and Claude Code.
# ---------------------------------------------------------------------------


def _write_rich_model(tmp_path):
    """A model with an UNREVIEWED metric + an UNREVIEWED join — the trust signals the
    receipt must surface. Returns (artifacts_dir, profile, root)."""
    import subprocess
    yaml = __import__("yaml")
    p = tmp_path / "p"
    (p / "datasources" / "c").mkdir(parents=True)
    (p / "subject_areas" / "s" / "tables").mkdir(parents=True)
    (p / "subject_areas" / "s" / "metrics").mkdir(parents=True)
    (p / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "p", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/s"]}))
    (p / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"}))
    (p / "subject_areas" / "s" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "s", "tables": [{"storage_connection": "c", "schema": "public", "table": "orders"},
                                {"storage_connection": "c", "schema": "public", "table": "customers"}]}))
    (p / "subject_areas" / "s" / "tables" / "orders.yaml").write_text(yaml.safe_dump({
        "name": "orders", "schema": "public", "storage_connection": "c", "grain": ["id"], "description": "o",
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "customer_id", "type": "integer"},
                    {"name": "amount", "type": "decimal", "description": "net revenue",
                     "description_source": "ai_unvalidated"}]}))
    (p / "subject_areas" / "s" / "tables" / "customers.yaml").write_text(yaml.safe_dump({
        "name": "customers", "schema": "public", "storage_connection": "c", "grain": ["id"], "description": "c",
        "columns": [{"name": "id", "type": "integer", "primary_key": True}]}))
    (p / "subject_areas" / "s" / "metrics" / "revenue.yaml").write_text(yaml.safe_dump({
        "name": "revenue", "calculation": "sum of order amount", "bindings": {"PostgreSQL": "SUM(amount)"},
        "source_tables": ["orders"], "confidence": "proposed", "review_state": "unreviewed"}))
    (p / "subject_areas" / "s" / "relationships.yaml").write_text(yaml.safe_dump({
        "relationships": [{"from_table": "orders", "from_column": "customer_id",
                           "to_table": "customers", "to_column": "id", "from_schema": "public",
                           "to_schema": "public", "relationship": "many_to_one",
                           "confidence": "inferred", "review_state": "unreviewed"}]}))
    subprocess.run(["git", "-C", str(p), "init", "-q"])
    subprocess.run(["git", "-C", str(p), "add", "-A"])
    subprocess.run(["git", "-C", str(p), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "-m", "init"])
    return tmp_path, "p", p


SQL = "SELECT c.id, SUM(amount) AS total FROM orders o JOIN customers c ON o.customer_id = c.id GROUP BY c.id"


def test_mcp_receipt_surfaces_unapproved_metric_and_join(monkeypatch, tmp_path):
    import pytest
    pytest.importorskip("pydantic"); pytest.importorskip("sqlglot")
    art, profile, _ = _write_rich_model(tmp_path)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))
    r = _resolve_receipt(profile, SQL)
    assert r is not None
    # the unapproved metric is surfaced WITH its review_state (drives the approve/change banner)
    rev = next(m for m in r["metrics"] if m["name"] == "revenue")
    assert rev["review_state"] == "unreviewed"
    # the unreviewed join is warned about; the ai-written column is flagged as an assumption
    assert any("unreviewed join" in w for w in r["warnings"])
    assert any(a["column"].endswith("orders.amount") for a in r["assumptions"])


def test_mcp_receipt_equals_shared_assembler(monkeypatch, tmp_path):
    """Golden parity: the MCP receipt IS the shared assembler's output — no divergent
    second implementation. (model_version is the only MCP-added field.)"""
    import pytest
    pytest.importorskip("pydantic"); pytest.importorskip("sqlglot")
    art, profile, root = _write_rich_model(tmp_path)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))
    from semantic_model import loader as L
    from semantic_model import runtime as RT
    shared = RT.assemble_receipt(L.load_datasource(root), SQL)
    mcp = _resolve_receipt(profile, SQL)
    for key in ("tables_used", "relationships", "metrics", "assumptions", "warnings"):
        assert mcp[key] == shared[key], key


# save_correction is no longer an MCP tool (it's a skill operation, off both transports), so its
# MCP-tool tests were removed. The curate engine it used is still covered by tests/test_semantic_model_curate.py.
