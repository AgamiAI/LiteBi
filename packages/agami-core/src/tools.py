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
    same executor the skills use), which runs the fan/chasm pre-flight + default_filters safety
    pass; the semantic model is read from `<artifacts_dir>/<profile>/`.
"""

from __future__ import annotations

import csv
import functools
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextvars import ContextVar
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths & config resolution (mirrors execute_sql.py / file-layout.md exactly)
# ---------------------------------------------------------------------------
import agami_paths

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
    "metric `calculation`/`bindings` verbatim. (3) execute_sql (safety + default_filters run "
    "inside it). (4) Read the returned `receipt`: SHOW the user `receipt.warnings` and any "
    "`receipt.metrics` whose review_state != 'approved' — joins/metrics they haven't signed off; "
    "never hide them. Don't refuse on an unreviewed metric — answer and warn.\n"
    "PII: a column marked `sensitive: true` restricts OUTPUT, not the query — you MAY "
    "COUNT/COUNT(DISTINCT)/filter/GROUP BY/JOIN on it, but never SELECT its raw per-row values. "
    "'unique emails' → COUNT(DISTINCT email). To disambiguate identical labels, project the "
    "non-sensitive id. (execute_sql enforces this and errors on a raw sensitive projection.)\n"
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


def check_read_only(sql: str) -> str | None:
    """Return None if the SQL is a safe single read-only statement, else a reason string.

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
    """Lazily load the semantic model for a profile, producing an `Organization`. Two backends
    behind one seam: when AGAMI_DB_URL is set the hosted server reads it from the DB; otherwise the
    local skill reads the YAML files (unchanged). Raises a clear error if the model deps (pydantic)
    aren't importable or there's no model for the profile."""
    from store import Store  # stdlib-light; psycopg2/sqlite imported lazily inside

    store = Store.from_env()
    if store is not None:
        from model_store import load_organization as _load_db

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
    if not (root / "org.yaml").exists():
        raise FileNotFoundError(
            f"No semantic model at {root}/org.yaml. Run the agami-connect skill to "
            f"introspect this database."
        )
    return L.load_organization(root)


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


def _model_version(profile: str) -> str | None:
    """The model-version pin the receipt records — the newest model version. Served from the DB
    when AGAMI_DB_URL is set (no file read), else the newest snapshot dir name (a content hash, what
    the local skill reads). None if absent/unavailable (execute_sql stays usable)."""
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


@functools.lru_cache(maxsize=None)
def resolved_org_id() -> str:
    """The single-tenant deployment org id, resolved once per process (F14 / ACE-056; relocated by
    F15 / ACE-067). Precedence: ``AGAMI_ORG_ID`` env override -> the id in the deployment-level record
    (``organization.yaml``, via ``org_record.load_org_record``) -> the LEGACY per-profile id found by
    scanning the artifacts dir (``loader.deployment_org_id`` — a pre-record deployment) -> ``"local"``.
    The SAME function backs both the deploy-time stamp (``model_deploy._default_org``) and the serve-time
    resolver, so a deployment writes and reads its rows under one identical id.

    F15 relocates the id's home from each profile's ``org.yaml`` up into the one root record, so the
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
        # Legacy fallback: a pre-record deployment still keeps its id in each profile's org.yaml.
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
    Reuses one Organization across the loads within a query AND across queries, until the model version
    changes; a cache miss falls back to a fresh `_load_org`."""
    version = _model_version(profile)  # cheap: one DB row / dir listing, not a full model load
    if version is None:
        # No version to detect a model change by (e.g. file mode with no snapshot) — don't cache,
        # so we can never serve a stale model. The DB-backed server always has a version.
        return _load_org(profile)
    org_id = _current_org_id()
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
    ORGANIZATION.md, USER_MEMORY.md, the deployment ``OrgRecord``, and the company narrative. Under the DB
    backend all of it is read on a SINGLE connection — this is a hot tool path, so open ``Store`` once, not
    per-source; with no DB configured it falls back to file reads (a DB deploy reads no files at runtime).
    Returns ``(org_md, user_md, record | None, company_md)``; missing pieces come back empty/``None`` so the
    two-level composition degrades cleanly."""
    from store import Store

    store = Store.from_env()
    if store is not None:
        from model_store import load_memory, load_organization_record

        try:
            mem = load_memory(store, profile, org_id=org_id)  # per-datasource ORGANIZATION.md + USER_MEMORY.md
            record = load_organization_record(store, org_id)  # the deployment company record
            company = load_memory(store, "", org_id=org_id)  # company narrative rides the datasource='' row
        finally:
            store.close()
        return mem.get("organization") or "", mem.get("user"), record, (company.get("organization") or "")

    from semantic_model import org_record as OR

    art = resolve_artifacts_dir()
    return (
        _read_text(art / profile / "ORGANIZATION.md") or "",
        _read_text(art / "USER_MEMORY.md"),
        OR.load_org_record(art),
        _read_text(OR.narrative_path(art)) or "",
    )


def _resolve_receipt(profile: str, sql: str) -> dict | None:
    """The FULL trust receipt (tables / relationships / metrics+review_state / assumptions
    / warnings) for this query — the SAME assembler the skill uses, so the 'what did this
    touch / what's unapproved' panel is identical in Claude Code and Claude Desktop.
    Returns None only if the model deps aren't importable (execute_sql stays usable)."""
    try:
        org = get_cached_org(profile)
        from semantic_model import runtime as RT

        return RT.assemble_receipt(org, sql, model_version=_model_version(profile))
    except Exception:
        return None


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
                "loader (model_deploy scans <artifacts_dir>/*/org.yaml).",
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
                "model_present": (pdir / "org.yaml").exists(),
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
    """Strip the human-only scaffolding from a context doc (ORGANIZATION.md / USER_MEMORY.md)
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
    Plus ORGANIZATION.md / USER_MEMORY.md domain context.
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
    # Domain context = the human's ORGANIZATION.md narrative + the model-DERIVED summary
    # (subject areas, conventions, decoded glossary) assembled fresh from the structured model.
    # Source (ORGANIZATION.md / USER_MEMORY.md text) comes from the DB under the DB backend, files
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
    return {
        2: "dsn",  # config / missing credentials / bad profile
        3: "driver_missing",
        4: "auth",  # connect / auth failed (also network)
        5: "syntax",  # SQL execution error
    }.get(code, "other")


def _executor_truncated(stderr: str | None) -> bool:
    """True if execute_sql flagged a bounded-fetch truncation (ACE-038/044). The executor emits a
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
    execution_ms: int, args: dict[str, Any],
) -> str:
    """Shape a successful result (units + exact-render markdown + trust receipt), log the execution
    through the single sink, and return the tool JSON. Shared by both execution paths — the subprocess
    fork and the in-process executor — so a **successful** query returns the identical result envelope
    whichever ran it. (A guard *refusal* is not yet identical across paths: the subprocess surfaces
    execute_sql's stderr JSON as the remediation, while the in-process path returns a clean generic
    refusal with the structured detail in the server log. Full structured-refusal parity needs
    `_model_safety` to *return* its envelope — it currently writes it to stderr, pinned by the
    fail-closed guard tests — so it's tracked as a follow-up, not folded into this seam.)"""
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
        # Trust receipt — provenance + anything unapproved this answer used. Same assembler
        # the agami-query skill renders, so Desktop gets the same trust panel. Clients should
        # surface receipt.warnings and any receipt.metrics whose review_state != "approved"
        # (offer to approve/correct via the save_correction tool).
        "receipt": _resolve_receipt(profile, sql),
    }

    # Log the execution through the single chokepoint: the DB sink when AGAMI_DB_URL is set (one
    # query_executions row), else the local jsonl the skills use. Best-effort either way.
    _record_query(
        {
            "ts": _now_iso(),
            "profile": profile,
            "question": args.get("raw_query"),
            "sql": sql,
            "row_count": len(data_rows),
            "source": "mcp_server",
        }
    )
    return json.dumps(result, indent=2, default=str)


def _run_in_process(
    sql: str, profile: str, area: str | None, max_rows: int | None, executor: Any
) -> tuple[list, list, bool] | dict:
    """Run through the in-process executor behind the shared guarded envelope (no subprocess, no CSV
    round-trip). Returns ``(columns, data_rows, truncated)`` on success, or an error dict on a guard
    refusal / execution failure — the same error shape the subprocess branch produces.

    Rows are textualized to match the subprocess CSV wire (``None`` → ``""``, else ``str``) so the
    two paths return observably identical JSON. Native-typed rows are a deliberately deferred decision
    (see the AH-012 spec); flipping this one coercion is the follow-up once that's settled."""
    import execute_sql

    # The per-call cap rides execute_sql's `_max_rows_override` ContextVar (ACE-028) — request-scoped,
    # so concurrent in-process queries with different caps don't stomp each other. Set/reset via the
    # token (the `_current_org_ctx` pattern); `run_blocking` copied this request's context into the
    # worker thread, so the set is isolated to this call.
    cap_token = execute_sql._max_rows_override.set(max_rows)
    try:
        result = execute_sql.execute_guarded(
            sql, profile, area, executor=executor, org_id=_credential_org_id()
        )
    except execute_sql.GuardRefused as refusal:
        # A read-only refusal (envelope present) is already caught by tool_execute_sql's upstream
        # check_read_only fast-fail, so in practice only the model-safety branch (envelope None) is
        # reached here; both are handled for defence-in-depth.
        if refusal.envelope is not None:
            return {"error": refusal.envelope["error"]}
        # A model-safety refusal wrote its structured detail to the server log (stderr); surface a
        # clean refusal here. (The subprocess path instead surfaces that stderr JSON as remediation —
        # the not-yet-identical refusal envelope tracked as a follow-up.)
        return {"error": {"kind": "permission",
                          "remediation": "Query refused by the semantic-model safety pass "
                                         "(see server logs for the specific rule)."}}
    except execute_sql.ExecutorError as exc:
        return {"error": {"kind": _classify_exit(exc.code), "remediation": exc.msg}}
    except SystemExit as exc:
        # Defence-in-depth. The known credential/DSN failures now raise ExecutorError (handled above,
        # carrying their detailed message), so this net catches only a residual/future sys.exit deep
        # in a driver — ensuring an in-process query can never take down the host; it becomes a
        # fail-closed tool error instead.
        code = exc.code if isinstance(exc.code, int) else 2
        return {"error": {"kind": _classify_exit(code),
                          "remediation": "Datasource configuration error."}}
    finally:
        execute_sql._max_rows_override.reset(cap_token)

    columns = list(result.columns)
    data_rows = [["" if v is None else str(v) for v in row] for row in result.rows]
    truncated = result.truncated
    if max_rows is not None and len(data_rows) > max_rows:  # backstop, matches the subprocess branch
        data_rows = data_rows[:max_rows]
        truncated = True
    return columns, data_rows, truncated


def tool_execute_sql(args: dict[str, Any]) -> str:
    """Local analog of Ask Agami `execute_sql`: run a read-only SELECT locally.

    Routes through the sibling execute_sql.py (Tier-3 Python executor) so all
    DB types are handled identically and nothing but the rows leaves the
    process. Enforces the same read-only guarantee as the hosted connector.

    Two execution paths behind the same guard: the default forks the execute_sql subprocess
    (isolation, byte-identical local/single-user); an injected executor (AH-012) runs in-process with
    native rows. Both funnel through `_finalize_execution`, so a **successful** query's result
    envelope is identical either way (a guard refusal's envelope is not yet identical across paths —
    see `_finalize_execution` and the tracked structured-refusal-parity follow-up).
    """
    sql = args.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        return json.dumps(
            {"error": {"kind": "other", "remediation": "Pass a non-empty `sql` string."}}
        )

    reason = check_read_only(sql)
    if reason is not None:
        return json.dumps(
            {
                "error": {"kind": "permission", "remediation": reason},
                "sql": sql,
            },
            indent=2,
        )

    profile = resolve_profile(args.get("datasource"))
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
        outcome = _run_in_process(sql, profile, area, max_rows, _INJECTED_EXECUTOR)
        execution_ms = int((time.monotonic() - started) * 1000)
        if isinstance(outcome, dict):  # guard refusal / execution error
            return json.dumps({**outcome, "sql": sql, "execution_ms": execution_ms}, indent=2)
        columns, data_rows, truncated = outcome
        return _finalize_execution(
            columns, data_rows, truncated,
            profile=profile, sql=sql, execution_ms=execution_ms, args=args,
        )

    # The model safety pass (fan/chasm pre-flight + default_filters) runs inside
    # execute_sql.py; pass the subject area so default_filters scope correctly.
    # Route through the unified executor as a module (the package is installed alongside
    # this harness), so the read-only safety pass + default_filters + logging run once.
    cmd = [sys.executable, "-m", "execute_sql", "--profile", profile, "--sql", sql]
    if args.get("area"):
        cmd += ["--area", str(args["area"])]
    if max_rows is not None:
        # ACE-044: cap at the source so the executor's bounded fetch (fetchmany(cap+1)) never
        # materializes the whole result — not a client-side trim after the fact.
        cmd += ["--max-rows", str(max_rows)]

    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=240,
        )
    except subprocess.TimeoutExpired:
        return json.dumps(
            {"error": {"kind": "timeout", "remediation": "Query exceeded 240s."}, "sql": sql}
        )
    execution_ms = int((time.monotonic() - started) * 1000)

    if proc.returncode != 0:
        return json.dumps(
            {
                "error": {
                    "kind": _classify_exit(proc.returncode),
                    "remediation": (proc.stderr or "").strip() or "execute_sql.py failed",
                },
                "sql": sql,
                "execution_ms": execution_ms,
            },
            indent=2,
        )

    # Parse the RFC-4180 CSV emitted on stdout.
    reader = csv.reader(io.StringIO(proc.stdout))
    rows_all = list(reader)
    columns = rows_all[0] if rows_all else []
    data_rows = rows_all[1:] if len(rows_all) > 1 else []
    # The executor caps at the source now (ACE-038/044) and flags it on stderr; surface that so a
    # truncated result is never presented as complete. Keep the client-side trim as a backstop.
    truncated = _executor_truncated(proc.stderr)
    if max_rows is not None and len(data_rows) > max_rows:
        data_rows = data_rows[:max_rows]
        truncated = True

    return _finalize_execution(
        columns, data_rows, truncated,
        profile=profile, sql=sql, execution_ms=execution_ms, args=args,
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


def _record_query(rec: dict[str, Any]) -> None:
    """Log a query execution through the DB sink (AGAMI_DB_URL) or the local jsonl. **Best-effort:**
    a logging failure must never break an otherwise-successful query — so the DB path swallows
    errors exactly like the jsonl path, and always closes its connection."""
    from store import Store

    store = Store.from_env()
    if store is None:
        _append_jsonl(QUERY_LOG, rec)
        return
    try:
        from contracts import QueryExecutionRecord
        from model_store import DbActivitySink

        rec.setdefault("org_id", _current_org_id())  # stamp the calling tenant onto the log row
        DbActivitySink(store).record_query_execution(QueryExecutionRecord(**rec))
    except Exception:
        pass  # best-effort: never fail the query because logging failed
    finally:
        store.close()


def record_tool_call(
    *,
    name: str,
    arguments: dict[str, Any] | None,
    result_text: str | None,
    execution_ms: int | None,
    actor: str | None,
    raised: bool = False,
) -> None:
    """Record one MCP tool call to the activity log (the transport calls this for **every** tool). The
    audit-grade fields are server-observed; `success`/`row_count`/`error_kind` are derived from the
    result (execute_sql returns an `{"error": ...}` body on a bad query without raising). The self-report
    fields (`user_question`/`agent_query`/`thread_id`) are whatever Claude supplied — may be None.
    **Best-effort and never raises** — a logging failure must not break the tool."""
    args = arguments or {}
    success, row_count, error_kind = True, None, None
    if raised:
        success, error_kind = False, "exception"
    else:
        try:
            parsed = json.loads(result_text) if result_text else None
            if isinstance(parsed, dict):
                if isinstance(parsed.get("error"), dict):
                    success = False
                    error_kind = parsed["error"].get("kind") or "error"
                row_count = parsed.get("row_count")
        except (ValueError, TypeError):
            pass
    _record_tool_call(
        {
            "ts": _now_iso(),
            "tool_name": name,
            "source": "mcp_server",
            "actor": actor,
            "datasource": args.get("datasource"),
            "sql": args.get("sql"),
            "row_count": row_count if isinstance(row_count, int) else None,
            "execution_ms": execution_ms,
            "success": success,
            "error_kind": error_kind,
            "user_question": args.get("user_question"),
            "agent_query": args.get(
                "raw_query"
            ),  # the existing arg is the agent's framing of the query
            "thread_id": args.get("thread_id"),
            "correlation_id": args.get("correlation_id"),  # the turn (one user question)
        }
    )


def _record_tool_call(rec: dict[str, Any]) -> None:
    """Write a tool-call record through the DB sink (AGAMI_DB_URL) or the local jsonl. Wrapped so the
    whole thing is best-effort — even opening the store can't surface an error to the caller."""
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
        pass  # best-effort: never fail the tool because logging failed


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
            "catalog (`metric_index` lists every metric regardless). Plus ORGANIZATION.md / "
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
            "enforced (DML/DDL/multi-statement rejected with kind='permission'). Runs entirely "
            "locally via execute_sql.py — no data leaves the machine."
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
                    "description": "Subject area — scopes the fan/chasm pre-flight + default_filters safety pass.",
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
