#!/usr/bin/env python3
"""
The shared MCP tool registry + implementations — one impl, both transports.

`TOOLS` (name → {handler, description, inputSchema}) is the single source both the stdio
entrypoint (`mcp_harness`) and the HTTP entrypoint (`mcp_http`) advertise, so a client sees the
same surface and behavior whether it connects over stdio (Claude Desktop) or HTTP (claude.ai).

The surface is the **4 product tools**: `list_datasources`, `get_datasource_schema` (adaptive),
`get_prompt_examples`, `execute_sql`.

Design constraints (match the rest of agami):
  - The execute_sql tool is pure-stdlib. The model-backed tools import the
    `semantic_model` package (Pydantic) lazily and surface a clear "install the model deps" error
    if it's absent — so execution still works on a bare install.
  - **No data leaves the machine.** SQL is executed locally by shelling out to `execute_sql` (the
    same executor the skills use), which runs the scope gates and reports the fan/chasm and
    aggregation findings on the receipt; the model is read from `<artifacts_dir>/<profile>/`.
"""

from __future__ import annotations

import csv
import functools
import io
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from contextvars import ContextVar, Token
from dataclasses import asdict
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths & config resolution (mirrors execute_sql.py / file-layout.md exactly)
# ---------------------------------------------------------------------------
import agami_paths

# The guardrail contract is stdlib-only by construction, so importing it here costs a bare install
# nothing — unlike `sql_guard`, which is still imported lazily inside `check_read_only`. `Envelope`,
# `Failure` and `Refusal` are CONSTRUCTED at this layer (the tool edge owns the outcomes that never
# reach the execution chokepoint), so they cannot be TYPE_CHECKING-only the way `Refusal` was.
from guardrail import (
    PRE_MODEL_RULES,
    RECEIPT_BEFORE_MODEL,
    RECEIPT_BUILD_FAILED,
    RECEIPT_NO_MODEL,
    RULE_AUDIT_UNAVAILABLE,
    Envelope,
    Failure,
    Receipt,
    Refusal,
    receipt_from_assembled,
    undetermined_receipt,
)

# The tool edge's logger. The audit write is best-effort — it must never break an answer — but
# "best-effort" is not "silent": a permanently broken sink has to be distinguishable from a working
# one, or the audit trail can be absent for weeks with nothing to notice it by. Everything that is
# swallowed here is logged at WARNING with its exception instead.
_LOG = logging.getLogger(__name__)

# Secrets + per-user state live under <artifacts_dir>/local/. Re-resolved after bootstrap() in main().
AGAMI_LOCAL = agami_paths.local_dir()
CREDENTIALS_PATH = agami_paths.credentials_path()
CONFIG_PATH = agami_paths.config_path()
QUERY_LOG = agami_paths.query_log_path()
TOOL_CALL_LOG = AGAMI_LOCAL / "tool_calls.jsonl"

SERVER_NAME = "agami"


def server_version() -> str:
    """Best-effort version: the AGAMI_VERSION env override, else the installed package metadata.
    Shared by both transports' serverInfo."""
    env_v = os.environ.get("AGAMI_VERSION")
    if env_v:
        return env_v
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("agami-core")
    except PackageNotFoundError:
        return "0.0.0"


# Client-facing usage guidance, surfaced by both transports. Describes the 4-tool flow;
# no save_correction (that's a skill operation, not on the MCP surface).
SERVER_INSTRUCTIONS = (
    "agami local datasource agent. The NL→SQL intelligence runs on your side; these tools provide "
    "the local semantic model + curated examples and execute SQL locally. All execution is local.\n"
    "Flow: (1) list_datasources, then get_datasource_schema for the datasource the question "
    "touches (it sizes itself — pass a `query` to focus metrics, `dataset_names` for full table "
    "detail). (2) Examples-first — call get_prompt_examples and mirror the closest match; use "
    "metric `calculation`/`bindings` verbatim. (3) execute_sql (the safety pass runs inside it; "
    "a table's declared `default_filters` are NOT applied — write one into the SQL yourself if "
    "the question needs it). (4) Read the returned `receipt`. It is on EVERY status, and it is "
    "five sections — columns, tables, joins, aggregates, assumptions — each `{items, "
    "undetermined}`. SHOW the user: any join in `receipt.joins.items` whose review_state != "
    "'approved', and any metric in `receipt.columns.items[].metric` (the entries whose `column` is "
    "null) whose review_state != 'approved' — joins/metrics they haven't signed off; never hide "
    "them. Don't refuse on an unreviewed metric — answer and warn. ALSO read "
    "`receipt.tables.items[].filters`: one entry per declared `default_filters` of that table "
    "reference, each `{expr, status}` with status applied / omitted / undetermined, scoped by the "
    "sibling `scope` field ('main', 'cte:<name>', 'subquery') because a filter satisfied inside a "
    "CTE body is not satisfied for the statement reading it. Nothing applied them for you, so an "
    "`omitted` or `undetermined` one is a real gap between what the org means by that table and "
    "what the answer counted — tell the user, and re-run with the filter written in if the "
    "question wanted it. ALSO surface each section's "
    "`undetermined` sentence when it is non-null: it says what that section did NOT establish, so "
    "an empty `items` with a marker means NOT CHECKED while an empty `items` with a null marker "
    "means checked and clean. Reporting only `items` turns 'not checked' back into 'nothing "
    "wrong', which is the one reading the receipt exists to prevent.\n"
    "PII: a column marked `sensitive: true` is the model author asking you to handle it with care. "
    "Prefer COUNT/COUNT(DISTINCT)/filter/GROUP BY/JOIN over projecting raw per-row values — "
    "'unique emails' → COUNT(DISTINCT email) — and to disambiguate identical labels, project the "
    "non-sensitive id. Nothing stops you: this is judgement, not a gate, and the receipt reports "
    "which sensitive columns an answer projected. Project them when the question genuinely needs "
    "them and say that you did. A column that must not be readable at all is not in the model, and "
    "a statement naming it is refused as out-of-scope.\n"
    "Activity log: on EVERY tool call (not just execute_sql), pass a `thread_id` (one per conversation, "
    "reused across all its calls) and a `correlation_id` (one per user question/turn, reused across the "
    "calls answering it, fresh when they ask something new), plus `user_question` (the user's question "
    "VERBATIM — keep it the SAME across the calls answering it; on execute_sql your own refinement goes "
    "in `raw_query`, never in `user_question`) — so a deployment admin sees the whole conversation, and "
    "within it 'user asked X → agent made N calls'. Best-effort; omit if unknown."
)


def bootstrap_paths() -> None:
    """Re-resolve the module-level paths at startup. agami_paths.bootstrap() also runs a one-time
    migration of any *legacy* ~/.agami install into the current <artifacts_dir>/local layout — new
    installs never create ~/.agami, so the migration is a no-op once there's nothing to move (it's
    a backward-compat shim, safe to drop in a later cleanup). Every entrypoint calls this so the
    paths reflect the resolved (possibly migrated) artifacts dir."""
    global AGAMI_LOCAL, CREDENTIALS_PATH, CONFIG_PATH, QUERY_LOG, TOOL_CALL_LOG
    agami_paths.bootstrap()
    AGAMI_LOCAL = agami_paths.local_dir()
    CREDENTIALS_PATH = agami_paths.credentials_path()
    CONFIG_PATH = agami_paths.config_path()
    QUERY_LOG = agami_paths.query_log_path()
    TOOL_CALL_LOG = AGAMI_LOCAL / "tool_calls.jsonl"


def _load_config() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except (OSError, ValueError):
            pass
    return {}


def resolve_profile(explicit: str | None = None) -> str:
    """Resolution order: explicit arg → AGAMI_PROFILE → .config.active_profile → 'default'."""
    if explicit:
        return explicit
    env = os.environ.get("AGAMI_PROFILE")
    if env:
        return env
    active = _load_config().get("active_profile")
    if isinstance(active, str) and active:
        return active
    return "default"


def resolve_artifacts_dir() -> Path:
    """Resolution order: AGAMI_ARTIFACTS_DIR → ~/.config/agami/path pointer → default
    ~/agami-artifacts. (The pointer, not .config, holds the location now — so there's no
    chicken-and-egg: .config itself lives under <artifacts_dir>/local/.)"""
    return agami_paths.artifacts_dir()


def _credentials_sections() -> dict[str, dict[str, str]]:
    """Parse <artifacts_dir>/local/credentials (INI) into {profile: {field: value}}. Empty on any error."""
    if not CREDENTIALS_PATH.exists():
        return {}
    import configparser

    cfg = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    try:
        cfg.read(CREDENTIALS_PATH)
    except configparser.Error:
        return {}
    out: dict[str, dict[str, str]] = {}
    for section in cfg.sections():
        out[section] = {
            k: (v.strip() if isinstance(v, str) else v) for k, v in cfg[section].items()
        }
    return out


def _db_type_for(profile: str, creds: dict[str, dict[str, str]]) -> str:
    sect = creds.get(profile, {})
    t = sect.get("type", "")
    if not t and sect.get("url"):
        # Map a DSN scheme → the datasource `type` label (display only; execution is execute_sql's
        # job). Covers the DBs agami advertises; an unknown scheme passes through verbatim.
        scheme = sect["url"].split("://", 1)[0].split("+", 1)[0].lower()
        t = {
            "postgresql": "postgres",
            "postgres": "postgres",
            "mysql": "mysql",
            "mariadb": "mysql",
            "redshift": "redshift",
            "snowflake": "snowflake",
            "bigquery": "bigquery",
            "bq": "bigquery",
            "sqlite": "sqlite",
            "mssql": "sqlserver",
            "sqlserver": "sqlserver",
            "oracle": "oracle",
            "databricks": "databricks",
            "trino": "trino",
            "presto": "trino",
            "duckdb": "duckdb",
        }.get(scheme, scheme)
    return t


# ---------------------------------------------------------------------------
# Read-only SQL guard (mirrors shared/sql-generation-rules.md → Safety Rules)
# ---------------------------------------------------------------------------


def check_read_only(sql: str) -> Refusal | None:
    """Return None if the SQL is a safe single read-only statement, else the gate's `Refusal`.

    Thin fail-fast wrapper over the shared `sql_guard` — the SAME gate the executor
    (`execute_sql.py`) enforces — so the stdio server, the HTTP/OAuth server, the
    agami-query skill, and cron all reject writes / DDL / dangerous functions
    identically. Blocking here also avoids spawning the executor subprocess for a
    query that would be rejected anyway.
    """
    import sql_guard

    return sql_guard.check_read_only(sql)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _load_org(profile: str):
    """Lazily load the semantic model for a profile, producing a `Datasource`. Two backends
    behind one seam: when AGAMI_DB_URL is set the hosted server reads it from the DB; otherwise the
    local skill reads the YAML files (unchanged). Raises a clear error if the model deps (pydantic)
    aren't importable or there's no model for the profile."""
    from store import Store  # stdlib-light; psycopg2/sqlite imported lazily inside

    store = Store.from_env()
    if store is not None:
        from model_store import load_datasource as _load_db

        try:
            org = _load_db(store, profile, org_id=_current_org_id())
        finally:
            store.close()
        if org is None:
            raise FileNotFoundError(
                f"No semantic model in the database for datasource {profile!r}. Load it from YAML "
                f"with the deploy's model loader."
            )
        return org

    from semantic_model import loader as L  # may raise ImportError (pydantic)

    root = resolve_artifacts_dir() / profile
    if not (root / "datasource.yaml").exists():
        raise FileNotFoundError(
            f"No semantic model at {root}/datasource.yaml. Run the agami-connect skill to "
            f"introspect this database."
        )
    return L.load_datasource(root)


def _resolve_units(profile: str, sql: str) -> dict[str, str]:
    """Best-effort {result-column -> unit} by **tracing the SQL** (so `SUM(amount) AS
    total` inherits amount's currency, not just bare-name matches). Returns {} if the
    model deps (pydantic/sqlglot) aren't installed — execute_sql stays pure-stdlib;
    numbers still format exactly via units.py, just without a currency symbol."""
    try:
        org = get_cached_org(profile)
        from semantic_model import runtime as RT

        return RT.resolve_result_units(org, sql)
    except Exception:
        return {}


# What ONE tool call may resolve more than once and must not resolve more than once: the model
# version pin. Keyed inside by profile, and scoped to a request rather than to the process, for two
# different reasons that both matter.
#
# **Cost.** `_model_version` is not free where it counts. Hosted, it opens a fresh unpooled psycopg2
# connection, queries and closes it — and a single `ok` call reached it five times (once inside each
# of the three `get_cached_org` calls, twice more directly), so one query cost up to five new Postgres
# connections for one unchanging string.
#
# **Correctness.** A version that changes mid-request would otherwise let one answer be described by
# two different model versions. Resolving once per request makes "the receipt pins the model this
# answer used" true by construction rather than by the two reads happening to race the same way.
#
# The assembled receipt was memoized here too, because the legacy flat dict and the Envelope's typed
# `Receipt` were assembled separately from the same statement. There is one receipt now and one
# consumer of it, so the memo has nothing left to absorb and the property it delivered — one
# assembly per request — is pinned by a test on both routes instead of by a cache that hides a
# second call site.
#
# A ContextVar rather than a module global because tool handlers run on parallel worker threads
# (`mcp_http` off-loads them), and it holds `None` outside a scope so a caller that never opens one
# (a direct handler call, a test) behaves exactly as it did before this cache existed.
_request_cache: ContextVar[dict[str, Any] | None] = ContextVar(
    "agami_request_cache", default=None
)


def begin_request_cache() -> Token[dict[str, Any] | None]:
    """Open the per-request resolve-once scope. Reset with the returned token in a `finally`, so one
    request's model version and assembled receipt can never describe the next one."""
    return _request_cache.set({})


def end_request_cache(token: Token[dict[str, Any] | None]) -> None:
    """Close the scope opened by `begin_request_cache`. Separate from the opener so the pairing is
    visible at the call site, the same shape as `set_call_source` / `reset_call_source`."""
    _request_cache.reset(token)


def _request_cached(key: Any, resolve: Callable[[], Any]) -> Any:
    """`resolve()`, once per request per key. Outside a scope it is a plain call-through."""
    cache = _request_cache.get()
    if cache is None:
        return resolve()
    if key not in cache:
        cache[key] = resolve()
    return cache[key]


def _model_version(profile: str) -> str | None:
    """The model-version pin the receipt records — the newest model version. Served from the DB
    when AGAMI_DB_URL is set (no file read), else the newest snapshot dir name (a content hash, what
    the local skill reads). None if absent/unavailable (execute_sql stays usable).

    Resolved at most ONCE per request per profile: on the hosted path this opens a database
    connection, and the callers within one query are several (`get_cached_org` in each model
    consumer, plus the receipt's own pin)."""
    return _request_cached(("model_version", profile), lambda: _resolve_model_version(profile))


def _resolve_model_version(profile: str) -> str | None:
    """`_model_version` without the per-request memo — the actual lookup, split out so the memo has
    something to wrap and so a caller that genuinely wants a fresh read has one to call."""
    from store import Store

    store = Store.from_env()
    if store is not None:
        from model_store import newest_model_version

        try:
            return newest_model_version(store, profile, org_id=_current_org_id())
        except Exception:
            return None
        finally:
            store.close()
    try:
        from semantic_model import snapshot as SN

        return SN.newest_version(resolve_artifacts_dir() / profile)
    except Exception:
        return None


# The org id for the current request's tool calls (ACE-045). The HTTP server sets this per request from
# the OrgResolver-resolved org; unset (stdio / single-tenant) it falls back to AGAMI_ORG_ID / "local".
_current_org_ctx: ContextVar[str | None] = ContextVar("agami_current_org_id", default=None)

# What drove the current tool call, recorded on every activity-log row. The MCP transport is the only
# caller in this package, so the default is the value this log has always carried; an embedder that
# dispatches tool handlers itself sets this for the duration of a call so its rows are distinguishable
# from transport traffic. Unset, every row reads exactly as it did before this seam existed.
#
# It lives in this module rather than `contracts` because the base install is stdlib-lean
# (`dependencies = []`) and `contracts` needs pydantic — which is why every import of it here is
# function-level. The read path imports this constant the same lazy way.
DEFAULT_CALL_SOURCE = "mcp_server"
_source_ctx: ContextVar[str] = ContextVar("agami_call_source", default=DEFAULT_CALL_SOURCE)


def current_call_source() -> str:
    """What drove this tool call — `DEFAULT_CALL_SOURCE` unless an embedder scoped it to something else."""
    return _source_ctx.get()


def set_call_source(source: str) -> Token[str]:
    """Scope the recorded source for the calls made under it. Reset with the returned token in a
    `finally`, so a caller that dispatches handlers directly cannot leak its label into later work."""
    return _source_ctx.set(source)


def reset_call_source(token: Token[str]) -> None:
    """Undo `set_call_source`. Separate from the setter so the pairing is visible at the call site."""
    _source_ctx.reset(token)


@functools.lru_cache(maxsize=None)
def resolved_org_id() -> str:
    """The single-tenant deployment org id, resolved once per process (F14 / ACE-056; relocated by
    F15 / ACE-067). Precedence: ``AGAMI_ORG_ID`` env override -> the id in the deployment-level record
    (``organization.yaml``, via ``org_record.load_org_record``) -> the LEGACY per-profile id found by
    scanning the artifacts dir (``loader.deployment_org_id`` — a pre-record deployment) -> ``"local"``.
    The SAME function backs both the deploy-time stamp (``model_deploy._default_org``) and the serve-time
    resolver, so a deployment writes and reads its rows under one identical id.

    F15 relocates the id's home from each profile's ``datasource.yaml`` up into the one root record, so the
    org owns its own identity; the per-profile scan is kept only as the legacy fallback for a deployment
    that has a per-profile id but no record yet (the id is lifted into a record on the next onboard).
    The artifacts-dir scope (not one 'active' profile) is preserved: a deploy with ``AGAMI_PROFILE``
    unset and the model under a named profile still resolves the deployment id.

    Memoized: at most one resolve per process (this sits on the per-request path via ``_current_org_id``).
    Tests that vary env/profile must call ``.cache_clear()``."""
    env = os.environ.get("AGAMI_ORG_ID", "").strip()
    if env:
        return env
    try:
        from semantic_model import loader as L  # lazy: keeps tools import light + avoids a cycle
        from semantic_model import org_record as OR

        art = resolve_artifacts_dir()
        record = OR.load_org_record(art)  # F15: the record is the home of the id
        # Legacy fallback: a pre-record deployment still keeps its id in each profile's datasource.yaml.
        oid = record.org_id if record is not None else L.deployment_org_id(art)
    except Exception:
        oid = None  # missing/legacy record or absent model deps -> single-tenant default
    return oid or "local"


def _current_org_id() -> str:
    """The org id to scope this process's model cache by: the request's resolved org when the HTTP server
    set it (per-request under a multi-tenant resolver), else the process-wide minted/`local` id."""
    return _current_org_ctx.get() or resolved_org_id()


# Public alias: a consumer's tool handler needs to know the org its call resolved to (to scope its own
# store), and the request never reaches the handler — only this contextvar does.
current_org_id = _current_org_id


def _credential_org_id() -> str:
    """The org that selects WAREHOUSE CREDENTIALS — deliberately NOT always the row-scoping org.

    `execute_sql` is fail-closed: a NAMED tenant never falls back to the shared, org-less
    `DATASOURCE_URL[__<PROFILE>]` vars, so one tenant can't silently borrow another's warehouse. That
    rule keys on the single-tenant sentinel `"local"`. F14 makes a single-tenant deployment's org a
    minted uuid — it is still ONE deployment using ITS OWN credentials — so the deployment's own id must
    keep behaving like the sentinel here, or every existing `DATASOURCE_URL`-based deploy would lose its
    credentials the moment an id is minted.

    So: the deployment's own resolved id (no `AGAMI_ORG_ID` naming a tenant) maps to `"local"`; anything
    else — an explicitly-named `AGAMI_ORG_ID`, or a tenant a multi-tenant resolver picked per request —
    is passed through unchanged and stays fail-closed."""
    org = _current_org_id()
    if not os.environ.get("AGAMI_ORG_ID", "").strip() and org == resolved_org_id():
        return "local"
    return org


# Per-process semantic-model cache (ACE-045). The long-lived server loads the whole model 2-3x per query
# (_resolve_units + _resolve_receipt) and re-loads it every query; caching serves it warm across queries and
# users. Keyed (org_id, datasource, model_version): org-scoped so a multi-tenant server never serves one org's
# model to another, and invalidated when the model version bumps. The execute_sql subprocess is a fresh process
# per query and does NOT share this (its win is the Slice-1 GuardContext, not a cross-query cache).
_ORG_CACHE: dict[tuple[str, str, "str | None"], Any] = {}
# Tool handlers now run in parallel worker threads (mcp_http off-loads them), so this process-global
# cache is genuinely concurrent. The lock keeps a miss provably race-free: the fast path (a hit) stays
# lock-free, and only concurrent misses serialize — one loads, the rest double-check and reuse it,
# which also avoids the eviction loop mutating the dict while another thread iterates it.
_ORG_CACHE_LOCK = threading.Lock()


def get_cached_org(profile: str):
    """Load the semantic model for `profile`, cached per process and keyed (org, datasource, version).
    Reuses one Datasource across the loads within a query AND across queries, until the model version
    changes; a cache miss falls back to a fresh `_load_org`."""
    version = _model_version(profile)  # cheap: one DB row / dir listing, not a full model load
    org_id = _current_org_id()
    if version is None:
        # No version to detect a model change by (e.g. file mode with no snapshot) — so nothing may
        # be cached ACROSS requests, or we could serve a stale model. The DB-backed server always has
        # a version and never takes this branch.
        #
        # WITHIN one request it is cached anyway, and that is not the same tradeoff: the consumers
        # of the model in a single query (the unit resolver and the receipt assembler) are describing
        # ONE answer, so serving them separately-loaded models buys no freshness and costs repeated
        # full loads of the same YAML. Before this, an unversioned local install paid one per
        # consumer on every query.
        return _request_cached(("org", org_id, profile), lambda: _load_org(profile))
    key = (org_id, profile, version)
    cached = _ORG_CACHE.get(key)  # fast path: a hit is a single atomic dict.get, no lock
    if cached is not None:
        return cached
    with _ORG_CACHE_LOCK:
        cached = _ORG_CACHE.get(key)  # double-check: another thread may have loaded it meanwhile
        if cached is not None:
            return cached
        org = _load_org(profile)
        # Drop any stale (org, datasource) entry at a previous version so the cache stays bounded.
        for stale in [k for k in _ORG_CACHE if k[0] == org_id and k[1] == profile and k != key]:
            del _ORG_CACHE[stale]
        _ORG_CACHE[key] = org
        return org


def _context_sources(profile: str, org_id: str) -> "tuple[str, str | None, Any, str]":
    """Every piece of domain-context text the served schema needs, read in ONE place: the per-datasource
    datasource.md, USER_MEMORY.md, the deployment ``OrgRecord``, and the company narrative. Under the DB
    backend all of it is read on a SINGLE connection — this is a hot tool path, so open ``Store`` once, not
    per-source; with no DB configured it falls back to file reads (a DB deploy reads no files at runtime).
    Returns ``(datasource_md, user_md, record | None, company_md)``; missing pieces come back empty/``None`` so the
    two-level composition degrades cleanly."""
    from store import Store

    store = Store.from_env()
    if store is not None:
        from model_store import load_memory, load_organization_record

        try:
            mem = load_memory(store, profile, org_id=org_id)  # per-datasource datasource.md + USER_MEMORY.md
            record = load_organization_record(store, org_id)  # the deployment company record
            company = load_memory(store, "", org_id=org_id)  # company narrative rides the datasource='' row
        finally:
            store.close()
        return mem.get("datasource") or "", mem.get("user"), record, (company.get("datasource") or "")

    from semantic_model import org_record as OR

    art = resolve_artifacts_dir()
    return (
        _read_text(art / profile / "datasource.md") or "",
        _read_text(art / "USER_MEMORY.md"),
        OR.load_org_record(art),
        _read_text(OR.narrative_path(art)) or "",
    )


# The argument-validation outcome: there is no statement, so there is nothing a receipt could be
# about. Distinct from every reason in `guardrail`, each of which is a statement whose receipt could
# not be built. It lives here because the tool edge is the only layer that can reach it — the
# chokepoint is never called without a statement.
RECEIPT_NO_STATEMENT = (
    "No statement was supplied, so there was nothing to establish anything about."
)


def _resolve_receipt(profile: str, sql: str, *, bounded: bool = False) -> Receipt:
    """The `Envelope.receipt` for a statement this process did NOT run through the chokepoint.

    The fork path needs this. `tools` runs `python -m execute_sql` as a subprocess and rebuilds the
    Envelope from the child's exit code and stderr, so the receipt the child assembled is destroyed
    at the process boundary — on exactly the refused and failed outcomes. Building it here is what
    keeps the two paths saying the same thing about the same statement, without changing the wire
    format between parent and child (ACE-035 declined to scope that).

    `bounded` picks the echo-bounded assembler, and EVERY non-ok outcome asks for it — the same rule
    the chokepoint's `_receipt_for` states and for the same reason: a `failed` body is reachable for a
    name the model declares and the warehouse does not have, so it is a disclosure channel of its own
    rather than a subset of `ok`'s.

    A build that raises returns an `undetermined` receipt, never `None`. A receipt that could not be
    built is a fact the caller can act on ("the model deps are not installed here"); `None` was an
    absence the caller could only read as silence. The two failure reasons are kept apart here
    exactly as the chokepoint keeps them apart, and they are the chokepoint's own constants: a caller
    on the default fork path would otherwise never see the actionable one, which is this spec's own
    defect one layer down.

    CLOSED GAP, and this is where it lived. `sql` here is the statement the CALLER sent, which is the
    only one this side of the fork has: `_model_safety` runs in the child, so anything it rewrote
    rebound the child's local before it executed and this described a statement that did not run.

    It closed by subtraction rather than by plumbing the rewritten statement back across the wire.
    The default-filter injection went first, then the fan/chasm auto-rewrite, and nothing rewrites a
    statement now — the string below is the one the child executes, byte for byte, and
    tests/test_ace093_byte_identity.py asserts that at the driver hand-off. The parity this depended
    on is measured in tests/test_ace088_executed_statement.py, which carried it as a `strict=True`
    xfail until the last rewrite went.

    Keep the invariant in mind before adding one back: a rewrite anywhere below the fork makes this
    receipt describe the wrong statement again, and the fork path has no channel to learn about it.
    """
    try:
        org = get_cached_org(profile)
    except Exception:
        # No model for this datasource, or no model deps at all. Both are ordinary states for a bare
        # local install rather than faults, so they are not server-log events — the fact travels to
        # the caller on the receipt itself, which is the whole point of returning one. Logged without
        # `exc_info`: a traceback here would carry the resolved artifacts path into the server log
        # for an outcome that is not an error at all.
        _LOG.debug("no model for datasource %r; receipt undetermined", profile)
        return undetermined_receipt(RECEIPT_NO_MODEL)
    try:
        from semantic_model import runtime as RT

        assemble = RT.assemble_refusal_receipt if bounded else RT.assemble_receipt
        return receipt_from_assembled(
            assemble(org, sql, model_version=_model_version(profile))
        )
    except Exception:
        # A model that loaded and an assembler that then broke IS a fault, and the operator is the
        # only one who can act on it. Same split, and the same two log levels, as `_receipt_for`.
        _LOG.error("could not assemble the receipt for datasource %r", profile, exc_info=True)
        return undetermined_receipt(RECEIPT_BUILD_FAILED)


def _refusal_receipt(profile: str, sql: str, refusal: Refusal) -> Receipt:
    """The receipt a REFUSED Envelope carries on this side of the fork, chosen by which rule fired.

    The twin of `execute_sql._refusal_receipt`, consulting the same `PRE_MODEL_RULES` set, so one
    refusal reads one way whichever process decided it. It is also what keeps a refusal the cheap
    path: a `read_only` or `recon` verdict is reached without a model, so building its receipt must
    not load one — which on the hosted server is a fresh unpooled connection per refusal, on the one
    outcome an attacker triggers at will.
    """
    if refusal.rule in PRE_MODEL_RULES:
        return undetermined_receipt(RECEIPT_BEFORE_MODEL)
    return _resolve_receipt(profile, sql, bounded=True)


def tool_list_datasources(_args: dict[str, Any]) -> str:
    """Analog of Ask Agami `list_organizations`: enumerate the datasources this deployment serves.

    Two backends behind one seam, exactly like `_load_org` / `_load_memory`: a served deployment
    (AGAMI_DB_URL set) reads the models from the store — the credentials file never ships to the
    container — while the local skill reads the on-disk profiles. Before this seam existed, the
    tool only ever read the credentials file, so it reported "no datasources" on every self-hosted
    server even with a model deployed and querying fine (get_datasource_schema / execute_sql work
    because they already went through the store)."""
    active = resolve_profile()
    from store import Store  # stdlib-light; the DB driver is imported lazily inside

    store = Store.from_env()
    if store is not None:
        try:
            from model_store import list_datasources, model_table_counts

            org_id = _current_org_id()
            counts = model_table_counts(store, org_id=org_id)  # one grouped query, not per-datasource
            out = [
                {
                    "datasource": ds,
                    "database_type": _served_db_type(ds),
                    "table_count": counts.get(ds, 0),
                    "model_present": True,
                    "is_active": ds == active,
                }
                for ds in list_datasources(store, org_id=org_id)
                if ds  # defensive: only real, named datasources (never an empty name)
            ]
        finally:
            store.close()
        if out:
            return json.dumps({"datasources": out, "active_datasource": active}, indent=2)
        return json.dumps(
            {
                "datasources": [],
                "note": "No models deployed to this server yet. Load one with the deploy's model "
                "loader (model_deploy scans <artifacts_dir>/*/datasource.yaml).",
            },
            indent=2,
        )

    # Local skill path: enumerate the credentials-file profiles + their on-disk models.
    creds = _credentials_sections()
    artifacts = resolve_artifacts_dir()
    out = []
    for profile in sorted(creds.keys()):
        pdir = artifacts / profile
        table_count = 0
        if pdir.is_dir():
            table_count = (
                sum(1 for _ in (pdir / "subject_areas").glob("*/tables/*.yaml"))
                if (pdir / "subject_areas").is_dir()
                else 0
            )
        out.append(
            {
                "datasource": profile,
                "database_type": _db_type_for(profile, creds),
                "table_count": table_count,
                "model_present": (pdir / "datasource.yaml").exists(),
                "is_active": profile == active,
            }
        )
    if not out:
        return json.dumps(
            {
                "datasources": [],
                "note": "No profiles found in your credentials file. Run the agami-connect skill first.",
            },
            indent=2,
        )
    return json.dumps({"datasources": out, "active_datasource": active}, indent=2)


def _served_db_type(datasource: str) -> str:
    """Best-effort database-type label for a served datasource. The store holds the model, not the
    warehouse credentials, so derive the type from the `DATASOURCE_URL[__<datasource>]` scheme the
    executor already resolves. Display-only ("" when no DSN is set — the model still serves)."""
    from execute_sql import _env_datasource_dsn  # sibling module; no import cycle

    dsn = _env_datasource_dsn(datasource)
    return _db_type_for(datasource, {datasource: {"url": dsn}}) if dsn else ""


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def _distill_for_llm(text: str | None) -> str:
    """Strip the human-only scaffolding from a context doc (datasource.md / USER_MEMORY.md)
    before it goes into the model's prompt. These files serve two readers: a human editing
    them (who wants the `<!-- edit freely … -->` prompts) and the LLM reading them as query
    context (for whom those prompts are noise — or worse, a "this was auto-generated" aside it
    might distrust). The skill strips comments on its read path; the MCP must match, or Claude
    Desktop sees the raw scaffolding on every query. Drops HTML comments + collapses the blank
    lines they leave behind."""
    if not text:
        return ""
    out = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


# --- get_datasource_schema adaptive sizing ---------------------------------
# A full semantic model can be enormous and overwhelm the client's context. `mode="auto"` picks
# an initial verbosity by **subject-area count** (agami-core's primary unit); the char budget is
# the hard backstop that downgrades one rung at a time (full→summary→index) even for an explicit
# `mode="full"`, so a single tool result can't blow the context window.
_AUTO_FULL_MAX_AREAS = 12  # <= this -> full
_AUTO_SUMMARY_MAX_AREAS = 50  # <= this -> summary; above -> index
_SCHEMA_CHAR_BUDGET = 60_000  # decoded len(json.dumps(...)) ceiling (~15K tokens)
_SCHEMA_MODE_DOWNGRADE = {"full": "summary", "summary": "index", "index": None}
_LARGE_TABLE_ROWS = 1_000_000  # tables at/above this surface in `large_tables` in every mode

# Metric ranking (lexical, no embeddings): exact/substring hits ("strong") are always kept; the
# weak token-overlap tail needs >= this coverage and is capped at top-K. This only decides which
# metrics get FULL detail inline — `get_datasource_schema` ALWAYS returns `metric_index` (every
# metric's name + one-liner), so a metric that matches nothing is never hidden: the client sees it
# exists and can pull it by name via `metric_names`. The stopwords carry no metric-identity signal,
# so they're dropped from the weak token-overlap path only (exact/substring still match them).
_METRIC_MATCH_TOP_K = 10
_METRIC_MATCH_MIN_COVERAGE = 0.6
_METRIC_MATCH_STOPWORDS = frozenset(
    {
        "per",
        "to",
        "by",
        "of",
        "the",
        "a",
        "an",
        "and",
        "in",
        "for",
        "on",
        "vs",
        "average",
        "avg",
        "mean",
        "rate",
        "ratio",
        "total",
        "number",
        "num",
        "count",
        "percentage",
        "percent",
        "pct",
    }
)
_METRIC_WORD_RE = re.compile(r"[a-z0-9]+")


def _auto_mode_for(area_count: int) -> str:
    """Pick the initial verbosity for mode='auto' by subject-area count."""
    if area_count <= _AUTO_FULL_MAX_AREAS:
        return "full"
    if area_count <= _AUTO_SUMMARY_MAX_AREAS:
        return "summary"
    return "index"


def _norm_phrase(s: str | None) -> str:
    """Lowercase + word-tokenize + single-space join (case/underscore/punct collapse away)."""
    return " ".join(_METRIC_WORD_RE.findall((s or "").lower()))


def _content_tokens(s: str | None) -> set[str]:
    """Non-stopword tokens with naive plural folding — the token-overlap path only."""
    out: set[str] = set()
    for t in _METRIC_WORD_RE.findall((s or "").lower()):
        if t in _METRIC_MATCH_STOPWORDS:
            continue
        out.add(t[:-1] if len(t) > 3 and t.endswith("s") else t)
    return out


def _all_metrics(org) -> dict[str, tuple[Any, str | None]]:
    """Map a unique key -> (metric, area) for every metric (subject-area + cross-area). The key is
    the metric name, disambiguated by area on a collision so two areas sharing a metric name are
    BOTH kept (the never-hide contract: every metric must appear in metric_index)."""
    out: dict[str, tuple[Any, str | None]] = {}

    def _add(m, area: str | None) -> None:
        key = m.name
        if key in out:  # name collision across areas — disambiguate, keep both
            key = f"{m.name} ({area})" if area else f"{m.name} (cross-area)"
        out[key] = (m, area)

    for sa in org.subject_areas:
        for m in sa.metrics:
            _add(m, sa.name)
    for m in getattr(org, "cross_subject_area_metrics", []):
        _add(m, None)
    return out


def _match_metrics(query: str | None, metrics: dict[str, tuple[Any, str | None]]) -> list[str]:
    """Lexically rank metrics against `query` -> matched names. Strong (exact/substring) hits are
    never dropped by the cap; the cap bounds only the weak token-overlap tail. [] if no match."""
    q_norm = _norm_phrase(query)
    if not q_norm:
        return []
    q_tokens = _content_tokens(query)
    scored: list[tuple[float, bool, str]] = []
    for name, (m, _area) in metrics.items():
        # Match on the metric's real name (not the possibly area-disambiguated dict key).
        cand_phrases = [m.name.replace("_", " "), m.description or ""] + list(m.other_names or [])
        cand_norms = [c for c in (_norm_phrase(p) for p in cand_phrases) if c]
        score, strong = 0.0, False
        for cn in cand_norms:
            if cn == q_norm:
                score, strong = max(score, 100.0), True
            elif cn in q_norm or q_norm in cn:
                score, strong = max(score, 60.0), True
        if q_tokens:
            cand_tokens: set[str] = set()
            for cn in cand_norms:
                cand_tokens |= _content_tokens(cn)
            if cand_tokens:
                coverage = len(q_tokens & cand_tokens) / len(q_tokens)
                if coverage >= _METRIC_MATCH_MIN_COVERAGE:
                    score = max(score, 20.0 * coverage)
        if score > 0:
            scored.append((score, strong, name))
    if not scored:
        return []
    scored.sort(key=lambda t: (-t[0], t[2]))
    strong_hits = [n for _, st, n in scored if st]
    result = list(dict.fromkeys(strong_hits + [n for _, _, n in scored]))
    return result[: max(_METRIC_MATCH_TOP_K, len(strong_hits))]


def _metric_full(m, area: str | None) -> dict[str, Any]:
    return {
        "name": m.name,
        "area": area,
        "description": m.description,
        "calculation": m.calculation,
        "other_names": list(m.other_names or []),
        "review_state": m.review_state,
    }


def _large_tables(org) -> dict[str, int]:
    out: dict[str, int] = {}
    for sa in org.subject_areas:
        for t in sa.tables_defined:
            ph = t.performance_hints
            rc = ph.estimated_row_count if ph else None
            if rc and rc >= _LARGE_TABLE_ROWS:
                out[t.name] = rc
    return out


def _table_contexts(org, table_names: list[str], L, index=None) -> dict[str, Any]:
    """Full get_table_context for the named tables, grouped back into a {name: ctx} map. `index`
    (from L.build_table_index) resolves tables in O(1) instead of a per-table linear scan (ACE-047)."""
    area_of = {t.name: sa.name for sa in org.subject_areas for t in sa.tables_defined}
    by_area: dict[str | None, list[str]] = {}
    for t in table_names:
        by_area.setdefault(area_of.get(t), []).append(t)
    contexts: dict[str, Any] = {}
    for area, tbls in by_area.items():
        ctx = L.get_table_context(
            org,
            tbls,
            area=area,
            include=["default_filters", "relationships", "caveats", "value_transforms", "metrics"],
            index=index,
        )
        contexts.update(ctx.get("tables", {}))
    return contexts


def _schema_payload(
    org, profile: str, mode: str, matched: list[str], metrics: dict[str, tuple[Any, str | None]], L,
    index=None,
) -> dict[str, Any]:
    """Build the structured schema payload at the given verbosity. `metric_index` + `large_tables`
    are always present (the never-hide net); `metrics` carries FULL detail for the matched set, or
    every metric in `full` with no query."""
    result: dict[str, Any] = {
        "datasource": profile,
        "organization": org.description or None,
        "mode": mode,
        "cross_area_relationships": [
            {
                "from": r.from_subject_area,
                "to": r.to_subject_area,
                "for_questions_about": r.for_questions_about,
            }
            for r in org.cross_subject_area_relationships
        ],
        "metric_index": {n: (m.description or n) for n, (m, _a) in metrics.items()},
        "large_tables": _large_tables(org),
    }
    if mode == "index":
        result["subject_areas"] = [
            {"name": sa.name, "description": sa.description, "table_count": len(sa.tables)}
            for sa in org.subject_areas
        ]
    else:  # summary or full — areas carry their table list (name + one-line description)
        result["subject_areas"] = [
            {
                "name": sa.name,
                "description": sa.description,
                "default_time_window": sa.default_time_window,
                "tables": [
                    {"name": t.name, "description": t.description} for t in sa.tables_defined
                ],
            }
            for sa in org.subject_areas
        ]
    if mode == "full":
        result["tables"] = _table_contexts(
            org, [t.name for sa in org.subject_areas for t in sa.tables_defined], L, index=index
        )
    # metrics in full: the matched set (a query/metric_names limits them); else every metric in
    # full mode (back-compat); else none (rely on metric_index).
    selected = matched if matched else (list(metrics) if mode == "full" else [])
    result["metrics"] = [
        _metric_full(metrics[n][0], metrics[n][1]) for n in selected if n in metrics
    ]
    return result


def tool_get_datasource_schema(args: dict[str, Any]) -> str:
    """Return the semantic model for a datasource, **sized to fit the client's context**.

    `mode="auto"` (default) picks verbosity by subject-area count (full <=12, summary <=50, index
    51+); a hard ~60K-char budget then downgrades one rung at a time (full→summary→index) even for
    an explicit `mode="full"`, setting `truncated=true`. `dataset_names=[...]` returns full
    `get_table_context` for the named tables (an explicit scope is respected — no downgrade).
    `query="<question>"` lexically ranks metrics so the client never ingests the whole catalog;
    `metric_index` (name->description for EVERY metric) + `large_tables` are always present.
    Plus datasource.md / USER_MEMORY.md domain context.
    """
    profile = resolve_profile(args.get("datasource"))
    try:
        org = get_cached_org(profile)
    except FileNotFoundError as e:
        return json.dumps({"error": {"kind": "not_found", "remediation": str(e)}}, indent=2)
    except ImportError:
        return json.dumps(
            {
                "error": {
                    "kind": "driver_missing",
                    "remediation": "semantic model deps not installed. Run: pip install -r "
                    "plugins/agami/scripts/semantic_model/requirements.txt",
                }
            },
            indent=2,
        )

    from semantic_model import loader as L

    requested = args.get("dataset_names") or []
    requested_mode = (args.get("mode") or "auto").lower()
    metrics = _all_metrics(org)

    if requested:
        # Explicit table scope — full detail for the named tables, no budget downgrade. Build the
        # O(1) name→table index so this resolves each table by lookup, not a per-table rescan
        # (scalability-audit finding P12).
        wanted = [str(n).split(".")[-1] for n in requested]
        result: dict[str, Any] = {
            "datasource": profile,
            "organization": org.description or None,
            "mode": "full",
            "requested_mode": requested_mode,
            "tables": _table_contexts(org, wanted, L, index=L.build_table_index(org)),
            "metric_index": {n: (m.description or n) for n, (m, _a) in metrics.items()},
            "large_tables": _large_tables(org),
        }
    else:
        explicit = [n for n in (args.get("metric_names") or []) if n in metrics]
        matched = list(dict.fromkeys(explicit + _match_metrics(args.get("query"), metrics)))
        mode = (
            _auto_mode_for(len(org.subject_areas)) if requested_mode == "auto" else requested_mode
        )
        if mode not in _SCHEMA_MODE_DOWNGRADE:
            mode = "summary"
        # Only full mode assembles the per-table `tables` block (the sole index consumer), and the
        # loop only ever DOWNGRADES from full — so build the index iff we start at full, else a
        # wide model that starts in `mode="index"` (the case this optimizes) would pay a wasted
        # O(tables) index build it never uses.
        index = L.build_table_index(org) if mode == "full" else None
        truncated = False
        while True:
            result = _schema_payload(org, profile, mode, matched, metrics, L, index=index)
            if len(json.dumps(result, default=str)) <= _SCHEMA_CHAR_BUDGET:
                break
            nxt = _SCHEMA_MODE_DOWNGRADE[mode]
            if nxt is None:
                # At the floor (index) and STILL over budget — the inline `metrics` (full detail
                # for matched/all metrics) is the remaining bulk. Shed it; `metric_index` still
                # lists every metric by name, so nothing is hidden — the client requests specifics
                # via `metric_names`. Flag truncated so the overflow is never silent (C1/C3).
                truncated = True
                if result.get("metrics"):
                    result["metrics"] = []
                break
            mode, truncated = nxt, True
        result["requested_mode"] = requested_mode
        if truncated:
            result["truncated"] = True
            result["next_action"] = (
                "Response was downgraded to fit the context budget. Request "
                "specific tables via `dataset_names` or focus metrics with `query`."
            )

    parts = [json.dumps(result, indent=2, default=str)]
    # Domain context = the human's datasource.md narrative + the model-DERIVED summary
    # (subject areas, conventions, decoded glossary) assembled fresh from the structured model.
    # Source (datasource.md / USER_MEMORY.md text) comes from the DB under the DB backend, files
    # otherwise — so a DB-only deploy reads no files at runtime.
    from semantic_model import org_draft as _OD

    # Two-level context: the shared COMPANY block from the deployment record + this datasource's own
    # narrative + derived summary. All the text is read on ONE DB connection (see _context_sources). No
    # record ⇒ compose_org_context degrades to the single-level output, so a deployment without a record
    # is unaffected.
    org_md_raw, user_md_raw, record, company_md = _context_sources(profile, _current_org_id())
    domain_context = _OD.compose_org_context(
        record, [org], company_narrative=company_md, source_narratives=[org_md_raw]
    )
    if domain_context:
        parts.append(f"\n## Domain context\n{domain_context}")
    user_mem = _distill_for_llm(user_md_raw)
    if user_mem:
        parts.append(f"\n## USER_MEMORY.md (cross-database preferences)\n{user_mem}")
    return "\n".join(parts)


def tool_get_prompt_examples(args: dict[str, Any]) -> str:
    """Ask Agami `get_prompt_examples`: the few-shot library.

    DB serving (hosted, AGAMI_DB_URL set): scope to the datasource, rank by word-overlap on
    `query`, and cap to `top_k` within a char budget — so a large library (e.g. accumulated
    corrections) never floods the context. Local serving (files): returns the curated examples.yaml
    verbatim (small; the client reads YAML directly), `query`/`top_k` accepted for parity.
    """
    profile = resolve_profile(args.get("datasource"))

    from store import Store

    store = Store.from_env()
    if store is not None:
        from model_store import select_examples

        # honour an explicit top_k=0 (caller wants none); only default when absent/None
        top_k = args.get("top_k")
        top_k = 10 if top_k is None else int(top_k)
        try:
            examples = select_examples(
                store,
                profile,
                query=args.get("query"),
                area=args.get("area"),
                top_k=top_k,
                org_id=_current_org_id(),
            )
        finally:
            store.close()
        return json.dumps(
            {"datasource": profile, "examples": examples, "count": len(examples)},
            indent=2,
            default=str,
        )

    artifacts = resolve_artifacts_dir()
    ex_dir = artifacts / profile / "prompt_examples"
    blocks: list[str] = []
    if ex_dir.is_dir():
        for ex_file in sorted(ex_dir.glob("*/examples.yaml")):
            text = _read_text(ex_file)
            if text and text.strip():
                area = ex_file.parent.name
                blocks.append(f"## subject area: {area}\n```yaml\n{text}\n```")
    if not blocks:
        return json.dumps(
            {
                "examples": [],
                "note": f"No examples under {ex_dir}/<area>/examples.yaml. "
                f"Corrections saved via agami-save-correction will appear here.",
            },
            indent=2,
        )
    header = (
        f"# Few-shot NL→SQL examples for datasource '{profile}'  (source: {ex_dir})\n"
        f"# Each block is one subject area's curated library. Match on the question, "
        f"then reuse the tagged tables/columns/SQL shape.\n"
    )
    return header + "\n" + "\n\n".join(blocks) + "\n"


def _classify_exit(code: int) -> str:
    """The `Failure.kind` for a CLI exit code — a one-line delegate to `execute_sql`, which owns the
    exit-code contract (it documents the codes and is the only place that produces them), so the tool
    edge cannot drift a second copy of the table."""
    from execute_sql import EXIT_TO_FAILURE_KIND

    return EXIT_TO_FAILURE_KIND.get(code, "other")


def _stderr_refusal(returncode: int, stderr: str | None) -> dict | None:
    """The refusal a forked executor wrote to stderr, reconstructed — or None when this exit is not
    one.

    Exit 1 is the guard's code but not exclusively the read-only guard's: the semantic-model
    branches still exit 1 after writing today's `{"error": …}` line. Keying off the `"refusal"` key
    rather than off the code alone is what lets both shapes share the exit without either being
    reinterpreted as the other.

    The payload is rebuilt through `Refusal` rather than passed along as a raw dict, so the
    contract's invariants (a known reason, a non-empty detail and remediation) are re-checked on
    THIS side of the process boundary. A child that somehow emitted a malformed refusal therefore
    falls back to the generic error path instead of being relayed as a valid one.

    `AttributeError` is in the caught set alongside `TypeError`/`ValueError` because a non-string
    field (`{"detail": 3}`) reaches `__post_init__`'s `.strip()` and raises it — a third way the same
    malformed payload can fail, and the one that would escape a fallback this docstring promises."""
    if returncode != 1:
        return None
    from guardrail import Refusal

    for line in (stderr or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("refusal"), dict):
            continue
        try:
            return asdict(Refusal(**payload["refusal"]))
        except (AttributeError, TypeError, ValueError):
            return None
    return None


def _child_failure_message(returncode: int, stderr: str | None) -> str:
    """The `Failure.message` for a forked executor that exited non-zero and did **not** write a
    structured refusal — either the child's own classified diagnostic, or a generic stand-in.

    Relayed only when BOTH hold:

      * the exit code is one the child's CLI contract produces from a `Failure`
        (`execute_sql.EXIT_TO_FAILURE_KIND` — 2/3/4/5/6). Those are the codes `main` reaches by
        writing `env.failure.message`, so the text is something the child classified and chose. Any
        other code — a Python-level crash exiting 1, a signal, a code we do not know — means the
        child never got that far, so its stderr is whatever happened to be on the way out.
      * the text carries no Python traceback. Belt and braces on the rule above, and the concrete
        thing that used to land here: before `execute_guarded` was made total, a malformed
        credentials file raised `configparser.MissingSectionHeaderError` out of the child, and the
        parent put the whole traceback — absolute filesystem paths included — into `failure.message`,
        which is a field the caller is shown.

    What is deliberately still relayed is the child's *authored* config-error text ("No warehouse
    credentials for profile […]. Set DATASOURCE_URL…"). That is the remediation a misconfigured user
    needs, and it is the same text the in-process path surfaces from `ExecutorError.msg` — the two
    paths agreeing on it is a property this slice exists to keep.

    **The sanitized band is RECONSTRUCTED, never relayed (ACE-039).** For every code whose message
    the child derives from `_ERROR_MESSAGES`, the parent can rebuild that exact sentence from the
    exit code alone, so relaying stderr for those codes buys nothing and carries real risk: stderr
    is a *shared* stream, and the child writes to it before the failure line. The concrete case was
    `_model_safety`'s `[agami] applied default_filters: …` notice — it put a declared row-level
    predicate, which the caller never sent, into `failure.message` on the DEFAULT transport, while
    the in-process path returned the clean sentence. (That notice is gone: ACE-042 deleted the
    injection it announced, and the `[agami] auto-corrected …` notice that replaced it as the
    example went the same way with the rewrite it announced. What still writes to the child's stderr
    is anything a library logs: both surviving diagnostic lines went too, when ACE-094 deleted the
    refusals that wrote them. The reconstruction below is not thereby unnecessary — a library
    writing to the shared stream has exactly the same reach, and it is the mechanism rather than
    any one notice that keeps the caller's answer clean. The traceback guard only ever caught the
    two `exc_info=True` sites.)
    """
    from execute_sql import (
        _AUTHORED_EXIT_CODES,
        _ERROR_MESSAGES,
        EXIT_TO_FAILURE_KIND,
        UNEXPECTED_FAILURE_MESSAGE,
    )

    text = (stderr or "").strip()
    # Rebuild rather than relay. The child produced this sentence from the kind, so the kind is all
    # the parent needs, and the child's stream never touches the caller's answer.
    kind = EXIT_TO_FAILURE_KIND.get(returncode)
    if returncode not in _AUTHORED_EXIT_CODES and kind in _ERROR_MESSAGES:
        return _ERROR_MESSAGES[kind]
    classified = returncode in EXIT_TO_FAILURE_KIND
    # `Traceback (most recent call last):` is the stable header of every Python traceback, and a
    # `  File "…", line N` frame is the line that carries the absolute path. Either one is enough.
    has_traceback = "Traceback (most recent call last):" in text or '\n  File "' in f"\n{text}"
    if text and classified and not has_traceback:
        return text
    if text:
        # Not for the caller, but not lost either: the raw stream is exactly what an operator needs
        # to debug a child that died in a way it could not classify.
        _LOG.error(
            "forked executor exited %s with no relayable diagnostic; raw stderr: %s",
            returncode, text,
        )
    return UNEXPECTED_FAILURE_MESSAGE


def _executor_truncated(stderr: str | None) -> bool:
    """True if execute_sql flagged a bounded-fetch truncation (ACE-044). The executor emits a
    non-error `{"truncated": {"row_cap": N}}` line on stderr alongside any other notices; scan for it."""
    for line in (stderr or "").splitlines():
        line = line.strip()
        if line.startswith("{") and '"truncated"' in line:
            try:
                if isinstance(json.loads(line).get("truncated"), dict):
                    return True
            except ValueError:
                pass
    return False


# The composition-root executor (AH-012). ``None`` (the default) means "fork the execute_sql
# subprocess" — the byte-identical local/single-user path. A consumer injects a ``ports.Executor``
# via ``create_app(adapters=…)`` to run execution IN-PROCESS behind the same guard (no fork, native
# rows). Process-global on purpose: the executor is a composition-root singleton, not per-request.
_INJECTED_EXECUTOR: Any | None = None


def set_injected_executor(executor: Any | None) -> None:
    """Register (or clear) the composition-root executor. Called once by ``mcp_http.create_app`` from
    ``adapters.executor``; ``None`` keeps the default subprocess path. Validates the shape at
    registration so a malformed adapter fails fast at app construction, not as an ``AttributeError``
    at query time."""
    global _INJECTED_EXECUTOR
    if executor is not None:
        import ports

        if not isinstance(executor, ports.Executor):  # runtime_checkable: has execute(...)
            raise TypeError(
                "injected executor must satisfy ports.Executor "
                "(an execute(vetted_sql, creds, *, profile) method)"
            )
    _INJECTED_EXECUTOR = executor


def _finalize_execution(
    columns: list, data_rows: list, truncated: bool, *, profile: str, sql: str,
    execution_ms: int,
) -> str:
    """Shape a successful result (units + exact-render markdown) and return the result JSON. Shared
    by both execution paths — the subprocess fork and the in-process executor — so a successful query
    returns the identical payload whichever ran it.

    This is the **`ok` payload only**, and it is frozen: `_emit` merges `status`, `receipt` and
    `audit_id` onto what this returns and is the only thing that serializes a tool response. It no
    longer builds a receipt of its own. It used to nest one here — the flat assembler output, under
    `data.receipt` — beside the Envelope's typed `guardrail.Receipt`, so one answer carried two
    descriptions of itself that were free to disagree and only the nested one reached an `ok`
    caller. There is one receipt now, it hangs off the Envelope, and `_emit` puts it on all three
    statuses.

    It no longer writes the audit row either. This function only ever runs on success, so hanging the
    write here is precisely why a refusal left no trace; the write moved to `_emit`, which every
    outcome passes through. Shaping a payload and recording an execution were two jobs in one place —
    and the `args` parameter went with the write, since the tool arguments were only ever read for
    the log."""
    # Deterministic, exact rendering — so the numbers a user verifies don't depend on
    # how the host LLM chooses to format them. `markdown` is the table to display
    # verbatim; `rows` stays raw (exact CSV values) for charting / programmatic use.
    unit_map = _resolve_units(profile, sql)
    try:
        from semantic_model import units  # stdlib-only; safe even without model deps

        markdown = units.format_table(columns, data_rows, unit_map)
    except Exception:
        markdown = None

    result = {
        "columns": columns,
        "rows": data_rows,
        "row_count": len(data_rows),
        "truncated": truncated,
        "units": unit_map,
        "markdown": markdown,  # exact, full numbers (currency symbol + grouping) — render as-is
        "sql": sql,
        "execution_ms": execution_ms,
    }
    return json.dumps(result, indent=2, default=str)


def _envelope(
    status: str,
    *,
    receipt: Receipt,
    data: Any | None = None,
    refusal: Refusal | None = None,
    failure: Failure | None = None,
) -> Envelope:
    """The ONE place the tool edge constructs an `Envelope`, and the one place it mints an
    `audit_id`.

    `receipt` is MANDATORY, which is what actually delivers the property this docstring used to
    claim: an outcome with no receipt to hand must return one that SAYS so, rather than the
    empty-and-silent default a consumer would read as "checked, found nothing". Every call site
    already passes one, so the fallback was unreachable — and its reason named the wrong cause for
    most of the outcomes that could have reached it.

    The execution chokepoint (`execute_sql.execute_guarded`) mints its own for everything that
    reaches it; this covers the outcomes that never do — a malformed argument, the read-only
    fast-fail, the subprocess supervisor timeout, and the fork path, whose child's id is
    deliberately not on the wire. The id minted here is the one that gets RECORDED: `_emit` — the
    single consumer of what this returns — writes it as `query_executions.id`, so the id a caller
    reads back off the answer is the primary key of its own audit row."""
    return Envelope(
        status=status,
        data=data,
        refusal=refusal,
        failure=failure,
        receipt=receipt,
        audit_id=uuid.uuid4().hex,
    )


def _emit(
    env: Envelope,
    *,
    sql: str | None,
    execution_ms: int | None,
    profile: str | None = None,
    args: dict[str, Any] | None = None,
    max_rows: int | None = None,
) -> str:
    """Serialize ONE `Envelope` to the tool-edge JSON — the single serializer `tool_execute_sql`
    returns through, whichever path produced the Envelope.

    Collapsing the six previous `json.dumps` return sites into this one is the point of the slice: a
    refusal now reads the same whether the guard ran in-process or in a forked child, because there
    is only one place that decides how a refusal reads.

    Per status:
      * `ok`      — `_finalize_execution`'s frozen payload with `status`, `receipt` and `audit_id`
                    merged on. Its rows are textualized here (`None` → `""`, else `str`) so both
                    paths emit the same JSON, and the `max_rows` backstop is applied here too.
      * `refused` — `{status, refusal, sql?, execution_ms?, receipt, audit_id}`.
      * `failed`  — `{status, failure, sql?, execution_ms?, receipt, audit_id}`.

    The receipt is attached AFTER the branch, once, for all three — so `Envelope.receipt` is the
    only receipt a caller can read and there is no per-status decision about whether it appears. It
    rode the two non-ok bodies only while the `ok` payload owned a `"receipt"` key of its own (the
    flat legacy dict), because splatting the payload would have silently overwritten one of the two.
    That key is gone.

    `None` fields are omitted rather than emitted as explicit `null`, so a response never carries a
    key that says nothing (the argument-validation path, for instance, has no `sql` to report).

    This is also where the audit row is written — see `_record_execution`. Both branches below fall
    through to ONE record call and ONE `json.dumps`, so "exactly one row per tool call, on every
    outcome" is a property of the control flow rather than of six call sites staying in step."""
    if env.status == "ok":
        columns = list(env.data.columns)
        rows = [["" if v is None else str(v) for v in row] for row in env.data.rows]
        truncated = env.data.truncated
        # Backstop only: the executor already caps at the source (ACE-044) and flags it. This
        # catches a result that slipped past that bound, and marks it truncated rather than
        # presenting a trimmed result as complete.
        if max_rows is not None and len(rows) > max_rows:
            rows, truncated = rows[:max_rows], True
        payload = json.loads(
            _finalize_execution(
                columns, rows, truncated,
                profile=profile or "", sql=sql or "", execution_ms=execution_ms or 0,
            )
        )
        body: dict[str, Any] = {"status": "ok", **payload}
        row_count = payload["row_count"]
    else:
        body = {"status": env.status}
        if env.refusal is not None:
            body["refusal"] = asdict(env.refusal)
        if env.failure is not None:
            body["failure"] = asdict(env.failure)
        if sql is not None:
            body["sql"] = sql
        if execution_ms is not None:
            body["execution_ms"] = execution_ms
        # A refused or failed call returned no rows at all — which is not the same fact as a query
        # that ran and matched nothing, but the row's `status` is what carries that distinction.
        row_count = 0

    # Outside the branch, so every status carries the Envelope's receipt and exactly one of them.
    body["receipt"] = asdict(env.receipt)
    body["audit_id"] = env.audit_id

    _record_execution(env, sql=sql, profile=profile, args=args, row_count=row_count)
    return json.dumps(body, indent=2, default=str)


# The most of a caller's statement that reaches the audit store. Deliberately far below the guard's
# own 50,000-character cap (`sql_guard._MAX_SQL_CHARS`), because the two bounds answer different
# questions: the guard's decides what may RUN, this one decides what is worth KEEPING. Since a
# refusal now writes a row, an oversized statement is refused *for being oversized* and its whole
# body was still persisted — so an authenticated caller could grow the store without ever reaching
# the warehouse. 8,000 characters holds any statement a person or a model actually writes (a long
# generated SELECT is a few KB), and the verdict columns, not the blob, are what make the row useful.
AUDIT_SQL_MAX_CHARS = 8_000

# The raw driver error is unbounded in a way the statement is not: a PostgreSQL failure can carry a
# HINT, a CONTEXT chain and every bound parameter. 015's argument applies verbatim — a failed
# statement must not become a way to grow the store — and this is the operator's diagnostic, so a
# couple of thousand characters is the whole of what is worth keeping.
AUDIT_ERROR_DETAIL_MAX_CHARS = 2_000


def _bounded_audit_sql(sql: str) -> tuple[str, bool]:
    """The statement as it will be stored, plus whether it had to be cut.

    The flag matters as much as the cut: a truncated statement that does not say so is a statement a
    reviewer would read as the whole thing, and re-running it would not reproduce the decision.
    """
    if len(sql) <= AUDIT_SQL_MAX_CHARS:
        return sql, False
    return sql[:AUDIT_SQL_MAX_CHARS], True


def _record_execution(
    env: Envelope,
    *,
    sql: str | None,
    profile: str | None,
    args: dict[str, Any] | None,
    row_count: int,
) -> None:
    """Write the ONE audit row for this call, from the ONE Envelope about to be serialized.

    Called from `_emit`, the single tool-edge serializer, so a row lands for `ok`, `refused` **and**
    `failed`. It used to hang off `_finalize_execution`, which runs only on success — so the audit
    trail recorded the queries that worked and silently dropped every decision we made against one.

    `id=env.audit_id` makes the row's primary key the same id the answer carried back, so a caller
    can find the record of its own execution with no join and no second uuid to reconcile.

    `reason` and `rule` are read off `env.refusal` — the contract object — rather than re-parsed out
    of the serialized body: the typed fields are already here, and a later change to the wire shape
    then cannot quietly empty the audit columns.

    `sql` is BOUNDED before it is stored — see `AUDIT_SQL_MAX_CHARS`.
    """
    refusal = env.refusal
    if refusal is not None and refusal.rule == RULE_AUDIT_UNAVAILABLE:
        # The ONE outcome that writes no row, and the only exemption there is (ACE-097). This
        # refusal means the store could not be opened, so writing a row to say so is the same write
        # failing a second time — and on the hosted path a failing write now raises, which would
        # turn the tidy fail-closed refusal into an unhandled exception at the serializer. Every
        # other outcome, refusals included, still records.
        #
        # Keyed on the RULE rather than on a flag threaded down from the gate: the rule is the fact,
        # a flag would be a second way to say it, and the two could disagree.
        return
    stored_sql, sql_truncated = _bounded_audit_sql(sql or "")
    # The RAW driver text, for the operator only — the caller's `failure.message` is the classified
    # value-free sentence and stays that way. Present only when the chokepoint ran in THIS process:
    # across the fork the child sanitizes and the parent records, so the raw text never crosses and
    # this is None. `execute_guarded` clears the var on entry, so a stale detail cannot attach to a
    # later call.
    from execute_sql import _last_error_detail

    raw_detail = _last_error_detail.get()
    _record_query(
        {
            "id": env.audit_id,
            "error_detail": (
                raw_detail[:AUDIT_ERROR_DETAIL_MAX_CHARS] if raw_detail else None
            ),
            "ts": _now_iso(),
            "profile": profile or "",
            "question": (args or {}).get("raw_query"),
            "sql": stored_sql,
            "sql_truncated": sql_truncated,
            "row_count": row_count,
            # Same provenance the tool-call row carries, so the two logs can't disagree about what
            # drove one execution. Unset, this is the value it has always been.
            "source": current_call_source(),
            "status": env.status,
            "reason": refusal.reason if refusal is not None else None,
            "rule": refusal.rule if refusal is not None else None,
        }
    )


def _run_in_process(
    sql: str, profile: str, area: str | None, max_rows: int | None, executor: Any
) -> Envelope:
    """Run through the in-process executor behind the shared guarded envelope (no subprocess, no CSV
    round-trip) and return the `Envelope` it produced — unmodified.

    This function no longer decides anything. It used to collapse every semantic-model refusal into
    a single `{"kind": "permission", "remediation": "…see server logs…"}`, which is the exact bug
    the guardrail contract exists to fix: the in-process caller could not tell a table-scope refusal
    from a column-scope one, while the forked caller could. Now the rule the gate chose travels all
    the way to the caller on both paths."""
    import execute_sql

    # The per-call cap rides execute_sql's `_max_rows_override` ContextVar (ACE-028) — request-scoped,
    # so concurrent in-process queries with different caps don't stomp each other. Set/reset via the
    # token (the `_current_org_ctx` pattern); `run_blocking` copied this request's context into the
    # worker thread, so the set is isolated to this call.
    cap_token = execute_sql._max_rows_override.set(max_rows)
    try:
        return execute_sql.execute_guarded(
            sql, profile, area, executor=executor, org_id=_credential_org_id()
        )
    except SystemExit:
        # Defence-in-depth. The known credential/DSN failures become a `failed` Envelope inside
        # `execute_guarded` (carrying their detailed message), so this net catches only a
        # residual/future sys.exit deep in a driver — ensuring an in-process query can never take
        # down the host; it becomes a fail-closed `failed` Envelope instead. The exit code is
        # deliberately ignored: a driver's exit status is not this module's exit-code contract, and
        # every reachable case is a datasource-configuration problem.
        return _envelope("failed", failure=Failure(
            kind="dsn", message="Datasource configuration error.",
        ), receipt=_resolve_receipt(profile, sql, bounded=True))
    finally:
        execute_sql._max_rows_override.reset(cap_token)


def tool_execute_sql(args: dict[str, Any]) -> str:
    """Local analog of Ask Agami `execute_sql`: run a read-only SELECT locally.

    Routes through the sibling execute_sql.py (Tier-3 Python executor) so all
    DB types are handled identically and nothing but the rows leaves the
    process. Enforces the same read-only guarantee as the hosted connector.

    Two execution paths behind the same guard: the default forks the execute_sql subprocess
    (isolation, byte-identical local/single-user); an injected executor (AH-012) runs in-process with
    native rows. Every outcome on either path becomes ONE `Envelope` and is serialized by `_emit`, so
    a caller sees the same shape — and, for a refusal, the same rule and the same remediation —
    whichever path ran.

    The whole body runs inside the per-request resolve-once scope, opened here because this is the
    one point both paths pass through, and closed in a `finally` for the same reason
    `_max_rows_override` is: a value left behind would describe the next call.
    """
    cache_token = begin_request_cache()
    try:
        return _tool_execute_sql(args)
    finally:
        end_request_cache(cache_token)


def _tool_execute_sql(args: dict[str, Any]) -> str:
    """`tool_execute_sql`'s body, inside the per-request scope its caller opens."""
    # Clear the raw-detail carrier for THIS call, at the one point both paths pass through.
    # `execute_guarded` also clears it on entry, but that only covers the in-process path — on the
    # fork the parent never calls it, so without this a forked call would record the driver text
    # left behind by an earlier IN-PROCESS failure in the same server process. Two paths, one
    # ContextVar, so the reset belongs where the call begins rather than where one of them does.
    from execute_sql import _last_error_detail

    _last_error_detail.set(None)

    sql = args.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        # An argument the caller got wrong, before any gate or database is involved: not a refusal
        # (we decided nothing about a statement — there is no statement) and `other` is the
        # catch-all kind for exactly this.
        return _emit(
            _envelope("failed", failure=Failure(
                kind="other", message="Pass a non-empty `sql` string.",
            ), receipt=undetermined_receipt(RECEIPT_NO_STATEMENT)),
            sql=None,
            execution_ms=None,
        )

    # Resolved BEFORE the read-only gate so the refusal it may produce is audited against the
    # datasource it was aimed at. `resolve_profile` reads an argument, an env var and a local config
    # file — it opens no connection and touches no credential, so a mutation still never reaches the
    # warehouse; what it no longer does is land in the audit trail with an empty `datasource`.
    profile = resolve_profile(args.get("datasource"))

    refusal = check_read_only(sql)
    if refusal is not None:
        # The read-only fast-fail: the same gate `execute_guarded` runs, applied here so a mutation
        # never even resolves credentials, forks, or consults a semantic model. Same rule, same
        # remediation as the deeper call would give — and now the same audit row, too. The receipt is
        # the pre-model one for the same reason: `read_only` is in `PRE_MODEL_RULES`, so there is
        # nothing a model could have been asked about this statement, and asking one anyway would put
        # a fresh unpooled database round-trip on the cheapest outcome an attacker can trigger at will.
        return _emit(
            _envelope("refused", refusal=refusal,
                      receipt=_refusal_receipt(profile, sql, refusal)),
            sql=sql, execution_ms=None, profile=profile, args=args,
        )

    max_rows = args.get("max_rows")
    try:
        max_rows = int(max_rows) if max_rows is not None else None
    except (TypeError, ValueError):
        max_rows = None
    if max_rows is not None:
        max_rows = max(1, min(max_rows, 10_000))

    area = str(args["area"]) if args.get("area") else None

    # In-process path (AH-012): a consumer injected an executor, so run behind the shared guarded
    # envelope with no subprocess and no CSV round-trip. Falls through to the subprocess fork below
    # when no executor is injected (the default) — that path stays byte-identical.
    if _INJECTED_EXECUTOR is not None:
        started = time.monotonic()
        env = _run_in_process(sql, profile, area, max_rows, _INJECTED_EXECUTOR)
        execution_ms = int((time.monotonic() - started) * 1000)
        return _emit(
            env, sql=sql, execution_ms=execution_ms,
            profile=profile, args=args, max_rows=max_rows,
        )

    # The model safety pass (fan/chasm pre-flight + scope + PII) runs inside execute_sql.py;
    # pass the subject area so the gates scope to the right one.
    # Route through the unified executor as a module (the package is installed alongside
    # this harness), so the read-only safety pass + logging run once.
    cmd = [sys.executable, "-m", "execute_sql", "--profile", profile, "--sql", sql]
    if args.get("area"):
        cmd += ["--area", str(args["area"])]
    if max_rows is not None:
        # ACE-044: cap at the source so the executor's bounded fetch (fetchmany(cap+1)) never
        # materializes the whole result — not a client-side trim after the fact.
        cmd += ["--max-rows", str(max_rows)]

    # The supervisor's bound is the OUTERMOST of the four time bounds, and it is DERIVED from the
    # same resolver every inner layer reads rather than being a number of its own. A fixed 240s
    # inverted that order for any statement budget approaching it: the supervisor fired FIRST, so a
    # statement we could have cancelled and refused precisely came back instead as a
    # `failed`/`timeout` naming nothing the caller can act on. Imported lazily for the same
    # reason `_run_in_process` does it.
    #
    # Resolved HERE and enforced on a child that re-resolves for itself, which only works because the
    # resolver reads the environment and nothing else: the child inherits `os.environ` (no `env=`
    # below) and therefore reaches the identical number. A request-scoped override would be the one
    # thing that could break that — it would outrank the environment on this side of the fork and be
    # invisible on the other, so a parent bound of 65s could sit against a child budget of 300s and
    # fire first, inverting the order this whole family exists to hold. There is deliberately no such
    # override; `_resolve_timeout_s` documents why, and a test pins that the budget keeps exactly one
    # configuration surface.
    import execute_sql

    supervisor_timeout_s = execute_sql._resolve_timeout_s() + execute_sql._SUPERVISOR_SKEW_S

    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=supervisor_timeout_s,
        )
    except subprocess.TimeoutExpired:
        # A `failed`/`timeout`, NOT a `refused`/`resource_limit` — and the reason is what we can
        # OBSERVE, not who acted. The bound is ours, but all it tells us is that the child never came
        # back; it does not say the STATEMENT ran long. The child may have hung in connect, in
        # credential resolution or in loading the model, and on any of those "narrow the query" is
        # advice pointing at the wrong thing. A refusal must name a fix, so a kill we cannot attribute
        # is not one. (Guardrail contract §3: an unresponsive executor is `failed`. The executor's own
        # per-statement deadline IS a refusal and carries `RULE_RESOURCE_LIMIT`; it can attribute the
        # cancel to the statement because it is the thing it cancelled. The two bounds coexist, and
        # this one must not borrow the other's rule.)
        return _emit(
            _envelope("failed", failure=Failure(
                kind="timeout",
                message="The executor did not respond within the supervisor's bound and was stopped.",
            ), receipt=_resolve_receipt(profile, sql, bounded=True)),
            sql=sql,
            execution_ms=None,
            profile=profile,
            args=args,
        )
    execution_ms = int((time.monotonic() - started) * 1000)

    if proc.returncode != 0:
        # A structured refusal crosses the process boundary as a refusal, not as raw stderr text
        # stuffed into a remediation field — the fork path and the in-process path must agree on
        # what the caller sees, and only the child knows which rule fired. Rebuilding through
        # `Refusal` here is the second contract check (the first is inside `_stderr_refusal`); it
        # costs nothing and keeps the Envelope's payload a real contract object, never a loose dict.
        refusal = _stderr_refusal(proc.returncode, proc.stderr)
        if refusal is not None:
            rebuilt = Refusal(**refusal)
            env = _envelope("refused", refusal=rebuilt,
                            receipt=_refusal_receipt(profile, sql, rebuilt))
        else:
            # Not raw stderr: see `_child_failure_message` for which of the two the caller gets and
            # why. This field is shown to the caller, so a traceback must never reach it.
            env = _envelope("failed", failure=Failure(
                kind=_classify_exit(proc.returncode),
                message=_child_failure_message(proc.returncode, proc.stderr),
            ), receipt=_resolve_receipt(profile, sql, bounded=True))
        # `profile` and `args` travel with the non-ok outcomes too. Omitting them wrote the audit row
        # with `datasource=''` and `question=NULL` on exactly the rows a reviewer of a refusal most
        # needs them on — and only on the fork path, which is the default, so the two paths disagreed
        # about the record of the same decision.
        return _emit(
            env, sql=sql, execution_ms=execution_ms, profile=profile, args=args,
        )

    # Parse the RFC-4180 CSV emitted on stdout. The executor caps at the source (ACE-044) and
    # flags it on stderr; carry that flag so a truncated result is never presented as complete. The
    # `max_rows` backstop is applied by `_emit`, the same one the in-process path gets.
    reader = csv.reader(io.StringIO(proc.stdout))
    rows_all = list(reader)
    columns = rows_all[0] if rows_all else []
    data_rows = rows_all[1:] if len(rows_all) > 1 else []

    from execute_sql import ExecResult

    env = _envelope("ok", data=ExecResult(
        columns=columns,
        rows=[tuple(r) for r in data_rows],
        truncated=_executor_truncated(proc.stderr),
    ), receipt=_resolve_receipt(profile, sql))
    return _emit(
        env, sql=sql, execution_ms=execution_ms,
        profile=profile, args=args, max_rows=max_rows,
    )


def _now_iso() -> str:
    # Avoid Date.now-style nondeterminism concerns: use UTC wall clock here is fine
    # (this is a long-running server process, not a replayed workflow).
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append_jsonl(path: Path, record: dict[str, Any]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
        return True
    except OSError:
        return False


def _audit_is_load_bearing() -> bool:
    """Whether a failed audit write must break the call — the served deployment, and only it.

    Reads `execute_sql._hosted()` rather than re-testing the environment here, so the question "is
    this a served deployment?" has ONE answer in the codebase. The same function decides whether
    `execute_guarded` runs the pre-execution reachability check, and the two must not be able to
    disagree: a deployment that fails closed at the gate but shrugs at the write would refuse
    healthy calls and quietly lose the record on the broken ones.
    """
    from execute_sql import _hosted

    return _hosted()


def _record_query(rec: dict[str, Any]) -> None:
    """Log a query execution through the DB sink (AGAMI_DB_URL) or the local jsonl.

    **Served: the write is load-bearing and its failure is raised** (ACE-097, principle 7). An answer
    that reached the caller with no record of the statement that produced it is the thing the
    principle forbids, and a warning is not a substitute: the operator reads it hours later, the
    caller acted on the answer immediately. `execute_guarded` establishes the store is reachable
    BEFORE executing, so this raise is the narrow residual — the store died between that check and
    this write — rather than the common path.

    **Local stays best-effort**, and unchanged. `governance-principles.md` scopes the principles to
    the served deployment; here there is no store and this writes jsonl, so a read-only artifacts
    directory would otherwise stop a laptop answering. A swallowed failure is still LOGGED at WARNING
    with its traceback, never passed silently: a sink broken for a month must not look identical to a
    working one.

    The WHOLE body sits inside the try, `Store.from_env()` included. Constructing the store can
    itself raise — a malformed DSN, an uninstalled driver — and with that call outside the try the
    exception escaped onto the success path even locally, breaking every query the deployment logged
    rather than every log it wrote. That shape is what now lets the served path re-raise deliberately
    from one place instead of leaking from an arbitrary one."""
    try:
        from store import Store

        # Stamped ABOVE the branch, so the field is on the record whichever sink takes it. It used to
        # be set inside the DB branch only, which left the jsonl rows without the `org_id` the format
        # spec documents for them — the field was declared for both sinks and written by one.
        rec.setdefault("org_id", _current_org_id())  # the calling tenant

        store = Store.from_env()
        if store is None:
            # The local (no-database) path. `_append_jsonl` swallows its own OSError and reports it
            # as a False return, which was being discarded — the same invisibility as the bare
            # `except: pass` below, so it gets the same treatment.
            if not _append_jsonl(QUERY_LOG, rec):
                _LOG.warning("query-execution audit write to %s failed", QUERY_LOG)
            return
        try:
            from contracts import QueryExecutionRecord
            from model_store import DbActivitySink

            DbActivitySink(store).record_query_execution(QueryExecutionRecord(**rec))
        finally:
            store.close()
    except Exception:
        if _audit_is_load_bearing():
            # Served: no record, no answer. Raised rather than logged, and deliberately not wrapped
            # in a friendlier exception — this escapes past `_emit`, so the caller gets a transport
            # error and the operator gets the traceback. Converting it into a tidy refusal Envelope
            # is not available: that Envelope would itself need an audit row, written through this
            # same broken sink.
            raise
        # Local: no SQL, no question, no org — the record's own fields are the caller's data, and
        # this line goes to the server log. The exception and the stack say where the write broke.
        _LOG.warning("query-execution audit write failed; the answer is unaffected", exc_info=True)


def record_tool_call(
    *,
    name: str,
    arguments: dict[str, Any] | None,
    result_text: str | None,
    execution_ms: int | None,
    actor: str | None,
    raised: bool = False,
    source: str | None = None,
    thread_id: str | None = None,
    correlation_id: str | None = None,
    user_question: str | None = None,
    success: bool | None = None,
    row_count: int | None = None,
    error_kind: str | None = None,
    org_id: str | None = None,
) -> None:
    """Record one MCP tool call to the activity log (the transport calls this for **every** tool). The
    audit-grade fields are server-observed; `success`/`row_count`/`error_kind` are derived from the
    result (a tool returns a refusal or failure body without raising, so `raised` alone would see
    almost nothing). The self-report fields (`user_question`/`agent_query`/`thread_id`) are whatever
    Claude supplied — may be None.
    **Best-effort locally; load-bearing on the served path** (ACE-097). Locally a logging failure
    must not break the tool and is warned about. Served, principle 7 makes the row part of the call:
    the write's failure is raised, and the tool call fails with it.

    The trailing parameters are an **override seam for an embedder that dispatches tool handlers
    itself** rather than through this package's transport. Each defaults to `None`, meaning *derive it
    the way this function always has* — so a caller that passes none of them gets byte-identical rows.

    Two groups, for two different reasons:

    - `source` / `thread_id` / `correlation_id` / `user_question` replace values that are otherwise read
      out of `arguments`, i.e. **self-reported by the model**. A caller that observed them directly can
      state them authoritatively, and the model can no longer influence how its own calls are grouped.
    - `success` / `row_count` / `error_kind` replace values otherwise **derived here by parsing
      `result_text`**. A caller that already classified the outcome passes it rather than handing over a
      result body to be re-parsed — and, more importantly, one that has *no* body to hand over can still
      record a failure. Without these, an outcome this function cannot see defaults to success, which is
      the one direction an audit log must never fail in. They are applied **as a group, and forced to
      be coherent**: state any one and the derived trio is replaced wholesale; an `error_kind` with no
      explicit `success` reads as a failure rather than defaulting to success; and a success never
      carries an error kind. So no combination of these arguments can write a row that says
      "succeeded" beside an error. **`raised=True` outranks all of them** — a call that threw is a
      failure whatever the caller passes, though it may still be given a more specific kind than the
      generic `"exception"`.
    - `org_id` replaces the tenant otherwise stamped downstream from this process's context. A caller
      that read the tenant at the point which actually scoped the work states it, rather than leaving it
      to be re-read later from a context that may no longer be the same one. The fallback when that
      read finds nothing is the deployment-wide org, and for an audit row that is the wrong direction
      to fail in.
    """
    args = arguments or {}
    derived_success, derived_row_count, derived_error_kind = True, None, None
    if raised:
        derived_success, derived_error_kind = False, "exception"
    else:
        try:
            parsed = json.loads(result_text) if result_text else None
            if isinstance(parsed, dict):
                # Two body shapes reach this sink. `execute_sql` speaks the guardrail Envelope
                # (`status` + `refusal`/`failure`); the model-backed tools still return the older
                # `{"error": {kind, remediation}}`. All three must mark the call unsuccessful —
                # reading only one of them would silently log blocked or failed queries as
                # successes.
                failure, refusal = parsed.get("failure"), parsed.get("refusal")
                if parsed.get("status") == "failed" and isinstance(failure, dict):
                    derived_success = False
                    derived_error_kind = failure.get("kind") or "error"
                elif parsed.get("status") == "refused" and isinstance(refusal, dict):
                    # A refusal is `success=0`, which is what it was before the Envelope: the body
                    # then was `{"error": {"kind": "permission", …}}` and this sink read the `error`
                    # key. The Envelope moved the verdict into `refusal`, and reading only `failure`
                    # would have flipped every blocked query to `success=1` — so any dashboard
                    # counting blocked queries off `tool_calls.success` would silently go to zero on
                    # deploy. `error_kind` is the rule the gate chose (`table_scope`,
                    # `column_scope`, …), strictly more informative than the single `permission`
                    # the old body could say. "Successful" here means the caller's request was
                    # carried out, not that the server behaved correctly; a refusal is the server
                    # behaving correctly AND the request not being carried out.
                    derived_success = False
                    derived_error_kind = refusal.get("rule") or "refused"
                elif isinstance(parsed.get("error"), dict):
                    derived_success = False
                    derived_error_kind = parsed["error"].get("kind") or "error"
                derived_row_count = parsed.get("row_count")
        except (ValueError, TypeError):
            pass
    if success is not None or row_count is not None or error_kind is not None:
        # Stating any one of the three replaces all three — and the result is forced to be coherent,
        # because an audit row that says "succeeded" beside an error kind is worse than either fact
        # alone. Two rules do that: naming an `error_kind` IS a statement of failure even with no
        # explicit flag (otherwise the outcome would default to success and reintroduce exactly the
        # row this seam exists to prevent), and a success cannot carry an error kind, so a caller that
        # states both loses the kind rather than writing the contradiction.
        derived_success = success if success is not None else error_kind is None
        derived_error_kind = None if derived_success else error_kind
        derived_row_count = row_count
    if raised:
        # A raise outranks every override. `raised` is not a classification the caller is offering —
        # it is a fact this function was told about what the tool actually did, and no argument can
        # make a call that threw into a successful one. Without this, passing something as innocuous
        # as `row_count` alongside `raised=True` erased the exception and logged a success.
        # A more specific kind than the generic "exception" is still welcome, so a stated one stands
        # — read from the parameter rather than the derived value, because a caller who contradicts
        # themselves (`raised=True` with `success=True`) has already had the derived kind cleared by
        # the success rule above. Their success claim loses; their diagnosis has no reason to.
        derived_success = False
        derived_error_kind = error_kind or derived_error_kind or "exception"
    rec: dict[str, Any] = {
        "ts": _now_iso(),
        "tool_name": name,
        "source": current_call_source() if source is None else source,
        "actor": actor,
        "datasource": args.get("datasource"),
        "sql": args.get("sql"),
        "row_count": derived_row_count if isinstance(derived_row_count, int) else None,
        "execution_ms": execution_ms,
        "success": derived_success,
        "error_kind": derived_error_kind,
        "user_question": user_question if user_question is not None else args.get("user_question"),
        "agent_query": args.get("raw_query"),  # the existing arg is the agent's framing of the query
        "thread_id": thread_id if thread_id is not None else args.get("thread_id"),
        "correlation_id": (  # the turn (one user question)
            correlation_id if correlation_id is not None else args.get("correlation_id")
        ),
    }
    if org_id is not None:
        # Set rather than left absent, so `_record_tool_call`'s `setdefault` keeps it instead of
        # re-reading the tenant from this process's context later.
        rec["org_id"] = org_id
    _record_tool_call(rec)


def _record_tool_call(rec: dict[str, Any]) -> None:
    """Write a tool-call record through the DB sink (AGAMI_DB_URL) or the local jsonl.

    Served: the failure is raised, for the reason `_record_query` gives. Local: swallowed, and now
    logged rather than passed silently — the `except Exception: pass` this replaced was the quieter
    of the two swallows this spec closes, and the reason neither had been closed before is that they
    masked each other exactly: this one hid inside the transport's, and the transport's had nothing
    to catch once this one stopped raising. Removing either alone was a PR with no observable
    change, which is indistinguishable from not having done the work."""
    try:
        from store import Store

        store = Store.from_env()
        if store is None:
            _append_jsonl(TOOL_CALL_LOG, rec)
            return
        try:
            from contracts import ToolCallRecord
            from model_store import DbActivitySink

            rec.setdefault("org_id", _current_org_id())  # stamp the calling tenant onto the log row
            DbActivitySink(store).record_tool_call(ToolCallRecord(**rec))
        finally:
            store.close()
    except Exception:
        if _audit_is_load_bearing():
            raise
        _LOG.warning("tool-call audit write failed; the answer is unaffected", exc_info=True)


# ---------------------------------------------------------------------------
# Tool registry (name → (handler, description, inputSchema))
# ---------------------------------------------------------------------------

# The self-reported grouping ids — the same on EVERY tool, so the admin activity log can reconstruct
# the conversation (thread ▸ turn ▸ call) the MCP server never sees. Defined once and spread into each
# schema's `properties` so the wording can't drift between tools; SERVER_INSTRUCTIONS tells Claude to
# pass them on every call. All best-effort (omit if unknown).
_THREAD_ID_PROP = {
    "type": "string",
    "description": "A short id you generate ONCE per conversation and reuse on every tool call in it "
    "— lets the admin group a conversation's calls into one session.",
}
_CORRELATION_ID_PROP = {
    "type": "string",
    "description": "A short id you generate ONCE per USER QUESTION (a turn) and reuse on every call "
    "you make answering THAT question — lets the admin see 'user asked X → agent made N calls'. "
    "Start a fresh one when the user asks something new.",
}
_USER_QUESTION_PROP = {
    "type": "string",
    "description": "The user's question, VERBATIM, that this call helps answer — recorded so an admin "
    "sees what was actually asked. Keep it the SAME across the calls answering one question.",
}

TOOLS: dict[str, dict[str, Any]] = {
    "list_datasources": {
        "handler": tool_list_datasources,
        "description": (
            "List the local agami datasources (credential profiles) and whether each has a "
            "semantic model. Local analog of the hosted list_organizations. Call this first "
            "when the datasource is not yet known; the others accept an optional `datasource`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "user_question": _USER_QUESTION_PROP,
                "thread_id": _THREAD_ID_PROP,
                "correlation_id": _CORRELATION_ID_PROP,
            },
            "additionalProperties": False,
        },
    },
    "get_datasource_schema": {
        "handler": tool_get_datasource_schema,
        "description": (
            "Fetch the local semantic model for a datasource, sized to fit context. `mode=auto` "
            "(default) picks verbosity by subject-area count (full/summary/index) under a char "
            "budget; `dataset_names=[...]` returns full get_table_context (columns scoped by "
            "expose_column_groups, default_filters, relationships, caveats, value_transforms, "
            "metrics) for the named tables; `query` ranks metrics so you don't ingest the whole "
            "catalog (`metric_index` lists every metric regardless). Plus datasource.md / "
            "USER_MEMORY.md context. Use metric `calculation`/`bindings` VERBATIM."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "datasource": {
                    "type": "string",
                    "description": "Profile name; defaults to the active profile.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["auto", "full", "summary", "index"],
                    "description": "Verbosity; default auto (sized by subject-area count + char budget).",
                },
                "dataset_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tables to pull full field-level detail for (an explicit scope, no downgrade).",
                },
                "query": {
                    "type": "string",
                    "description": "The user's NL question — lexically ranks metrics.",
                },
                "metric_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Return full detail for these named metrics.",
                },
                "user_question": _USER_QUESTION_PROP,
                "thread_id": _THREAD_ID_PROP,
                "correlation_id": _CORRELATION_ID_PROP,
            },
            "additionalProperties": False,
        },
    },
    "get_prompt_examples": {
        "handler": tool_get_prompt_examples,
        "description": (
            "Fetch the curated few-shot NL→SQL examples for a datasource (one block per subject "
            "area, from prompt_examples/<area>/examples.yaml). Use before generating SQL to ground "
            "dialect and house style; match on the question, then reuse the tagged tables/columns/SQL."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "datasource": {
                    "type": "string",
                    "description": "Profile name; defaults to the active profile.",
                },
                "query": {
                    "type": "string",
                    "description": "The user's NL question (context only).",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Accepted for hosted-parity; not applied locally.",
                },
                "user_question": _USER_QUESTION_PROP,
                "thread_id": _THREAD_ID_PROP,
                "correlation_id": _CORRELATION_ID_PROP,
            },
            "additionalProperties": False,
        },
    },
    "execute_sql": {
        "handler": tool_execute_sql,
        "description": (
            "Execute a single read-only SELECT / WITH...SELECT against the local datasource and "
            "return {columns, rows, row_count, truncated, sql, execution_ms}. SELECT-only is "
            "enforced: DML/DDL/multi-statement come back as {status:'refused', refusal:{reason, "
            "rule, detail, remediation}} — relay the remediation, it says how to get an answer. "
            "Runs entirely locally via execute_sql.py — no data leaves the machine. "
            # The declared-filter clause. Spec ids stay in comments like this one — this string
            # ships to every client, and an id only resolves inside the spec repo.
            "A table's declared `default_filters` are NOT applied to your SQL — if a filter "
            "matters to the question, write it into the statement yourself. The receipt then "
            "reports which ones you did: `receipt.tables.items[].filters` carries one "
            "`{expr, status}` per declared filter of that reference, status applied / omitted / "
            "undetermined, per REFERENCE and scoped by the sibling `scope` field."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "One SELECT or WITH...SELECT statement."},
                "datasource": {
                    "type": "string",
                    "description": "Profile name; defaults to the active profile.",
                },
                "area": {
                    "type": "string",
                    "description": "Subject area — scopes the fan/chasm pre-flight and the scope/PII gates.",
                },
                "raw_query": {
                    "type": "string",
                    "description": "Your (the agent's) framing of THIS sub-query — recorded for the "
                    "admin activity log. Your refinement goes here, NOT in user_question.",
                },
                "user_question": {
                    "type": "string",
                    "description": "The user's ORIGINAL question, VERBATIM. Keep it the SAME across every "
                    "query you run to answer one question — do not replace it with your refinement (that "
                    "goes in raw_query). Recorded so an admin sees what was actually asked.",
                },
                "thread_id": _THREAD_ID_PROP,
                "correlation_id": _CORRELATION_ID_PROP,
                "max_rows": {"type": "integer", "description": "Row cap (clamped 1–10000)."},
            },
            "required": ["sql"],
            "additionalProperties": False,
        },
    },
}


def register(
    name: str,
    handler: Callable[[dict[str, Any]], str],
    description: str,
    inputSchema: dict[str, Any],
) -> None:
    """Add a tool to the shared TOOLS registry — the supported consumer extension point.

    Raises on a duplicate name so a consumer can't silently shadow a core tool (e.g. execute_sql).
    Note create_app merges a consumer's extra tools over a *copy* of TOOLS; register() mutates the
    module global directly (the stdio path uses it), so its dup-guard is the safety net either way."""
    if name in TOOLS:
        raise ValueError(f"tool {name!r} is already registered")
    TOOLS[name] = {"handler": handler, "description": description, "inputSchema": inputSchema}
