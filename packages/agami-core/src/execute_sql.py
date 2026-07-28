#!/usr/bin/env python3
"""
Tier 3 — Python execution helper.

Reads <artifacts_dir>/local/credentials (INI), opens a connection to the configured
database via the appropriate Python driver, runs ONE SQL statement, and
writes the result as RFC 4180 CSV to stdout. Stdlib + driver-only.

The agami skill calls this when it detects tier=python in <artifacts_dir>/local/.config
(meaning native CLI tools are unavailable but the relevant Python driver
is importable). Connect-side and query-database both shell out to:

    python3 scripts/execute_sql.py --profile <profile> --sql-file <path>

The --sql-file form is preferred over --sql so SQL containing quotes,
backticks, or `$` doesn't get mangled by the shell.

Credentials resolve env-first then file: a DSN in DATASOURCE_URL[__<PROFILE>] (the
self-host channel), else <artifacts_dir>/local/credentials. Connects ONLY to the
host/port that resolution yields. Never substitutes localhost. Never asks for
credentials. Hard exits with a clear message if neither source has them.

Drivers (install only what you need):
    pip install psycopg2-binary             # Postgres / Redshift
    pip install pymysql                     # MySQL
    pip install snowflake-connector-python  # Snowflake
    pip install google-cloud-bigquery       # BigQuery
    # SQLite uses the stdlib `sqlite3` module — no install needed.

Exit codes:
    0  — success, CSV on stdout
    1  — SQL rejected by the read-only guard (kind="permission" JSON on stderr)
    2  — usage / config error (missing credentials, bad profile, etc.)
    3  — driver missing for the configured db type
    4  — connection / authentication failed
    5  — SQL execution error (syntax, unknown column, etc.)
"""

from __future__ import annotations

import argparse
import configparser
import csv
import json
import logging
import os
import stat
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import agami_paths
from guardrail import Refusal, Verdict, policy

if TYPE_CHECKING:
    # ``Executor`` is the 5th port; imported only for type-checkers. At runtime ``execute_sql`` never
    # imports ``ports`` (it ships in the stdlib-lean plugin mirror without it), so the annotation on
    # ``execute_guarded`` stays a lazy string (``from __future__ import annotations``).
    from ports import Executor

# Credentials + config now live under <artifacts_dir>/local/ (the consolidated,
# gitignored replacement for ~/.agami). The path is stable regardless of migration
# timing — bootstrap() just moves the files into it. See agami_paths.
CREDENTIALS_PATH = agami_paths.credentials_path()
CONFIG_PATH = agami_paths.config_path()
ALLOWED_PERMS = (0o600, 0o400)


def _resolve_default_profile() -> str:
    """Pick the default profile when --profile isn't passed and AGAMI_PROFILE is unset.

    Resolution order:
      1. AGAMI_PROFILE env var
      2. active_profile field in <artifacts_dir>/local/.config
      3. The literal string "default" (legacy fallback)
    """
    env = os.environ.get("AGAMI_PROFILE")
    if env:
        return env
    if CONFIG_PATH.exists():
        try:
            import json as _json

            cfg = _json.loads(CONFIG_PATH.read_text())
            active = cfg.get("active_profile")
            if isinstance(active, str) and active:
                return active
        except (OSError, ValueError):
            pass
    return "default"


def _err(msg: str, *, code: int = 2) -> int:
    sys.stderr.write(f"{msg}\n")
    return code


@dataclass(frozen=True)
class ExecResult:
    """What an executor returns: columns + rows with **native Python types preserved** (ints,
    Decimals, datetimes, ``None``), not stringified. Serializing to text — CSV for the subprocess
    wire, JSON at the MCP-tool edge — is the *caller's* single, final step, so an in-process
    executor never pays a serialize→re-parse round-trip and never loses a type or confuses NULL
    with "". ``truncated`` mirrors the ``fetchmany(cap + 1)`` bound: True when the result was capped.

    This lives here (not in ``ports``) because ``execute_sql`` ships in the stdlib-lean plugin
    mirror, which does not include ``ports``; ``ports.Executor`` references it under TYPE_CHECKING.
    """

    columns: list[str]
    rows: list[tuple]
    truncated: bool = False
    # ACE-041: the DECLARED "table.column" refs of any OUTPUT columns redacted to REDACTION_TOKEN by
    # the data-protection mask plan (empty for an unmasked result). The redacted VALUES already live
    # in `rows`; this carries the `applied[{mask}]` note both surfaces record. Immutable default `()`.
    masked_columns: tuple[str, ...] = ()


# ACE-041: the string a masked sensitive value is replaced with in every row of a masked output
# column. A single named constant so the token is defined once for the redactor and the tests.
# ACE-041: token confirmed as "***" (author, 2026-07-13).
REDACTION_TOKEN = "***"


@dataclass(frozen=True)
class MaskPlan:
    """The post-execution redaction the data-protection gate proved safe: which OUTPUT column
    positions to redact (`indices`, 0-based, aligned with the result's columns and every set-op arm)
    and the DECLARED "table.column" refs those positions carry (`columns`, for the `applied[{mask}]`
    note). Built by ``_model_safety`` only when ``guardrail.policy`` returns ``mask`` (every offending
    projection is a deterministically-maskable 1:1 image of one output column), carried to
    ``execute_guarded`` which applies it to ``result.rows``. Frozen — one plan per guarded call."""

    indices: tuple[int, ...]
    columns: tuple[str, ...]
    token: str = REDACTION_TOKEN


class ExecutorError(Exception):
    """A connect / credential / run failure raised by the built-in executor. Carries the exact
    stderr message and exit code the subprocess CLI emits, so ``main`` reproduces today's bytes and
    the in-process caller gets a catchable error instead of a process exit. Replaces the old
    ``return _err(...)`` returns inside the per-engine run functions."""

    def __init__(self, msg: str, *, code: int) -> None:
        super().__init__(msg)
        self.msg = msg
        self.code = code


class GuardRefused(Exception):
    """A guard refusal short-circuiting the executor. Carries the typed ``Refusal`` (the shared
    guardrail shape) so BOTH callers build the SAME envelope from it: the subprocess ``main`` emits
    ``Envelope.refused(refusal)`` JSON to stderr, and the in-process MCP handler returns it directly.
    No stderr round-trip — so a model-safety refusal keeps its structured detail on both paths."""

    def __init__(self, refusal: Refusal, *, code: int) -> None:
        super().__init__()
        self.refusal = refusal
        self.code = code


def _env_token(profile: str) -> str:
    """The env-var suffix for a datasource: the profile id upper-cased with every non-alphanumeric char
    folded to `_` (so `sales-pg` → `SALES_PG`, used as `DATASOURCE_URL__SALES_PG`)."""
    return "".join(c if c.isalnum() else "_" for c in profile).upper()


def _env_datasource_dsn(profile: str, org_id: str = "local") -> str | None:
    """A warehouse DSN supplied via the environment for `(org_id, profile)`, or None.

    Env var forms, checked in order:
      1. <ORG>_DATASOURCE_URL__<PROFILE> — per-(tenant, datasource). Both tokens are the
         id upper-cased with every non-alphanumeric char folded to `_` (so org `acme`,
         profile `sales-pg` → ACME_DATASOURCE_URL__SALES_PG). This is how a multi-tenant
         deployment gives each tenant its own warehouse for a same-named datasource.
      2. <ORG>_DATASOURCE_URL — the org's single-datasource default.
      3. DATASOURCE_URL__<PROFILE>, then 4. DATASOURCE_URL — the ORG-LESS forms, tried
         ONLY for org `local` (the single-tenant default). A NAMED tenant (org != 'local')
         never falls back to these: a forgotten tenant var must not silently point that
         tenant at the shared warehouse. It returns None here, and the caller surfaces the
         usual "no credentials" error.

    This is the container / self-host credential channel (cf. how the model reads
    from Postgres when configured, else the file): env carries no file mode and is
    inherited by this subprocess, so it sidesteps the mounted-secret + chmod-600
    problems the file has under a container uid that doesn't own it.

    Scope / gotchas (deliberately minimal — the file remains the fuller channel):
      - A DSN carries the same expressiveness as the file's `url = ...` field, so the
        env channel supports the schemes `_parse_dsn` handles (postgres / redshift /
        mysql / snowflake / bigquery / sqlite). A warehouse type without a DSN scheme
        (databricks, oracle, sqlserver, trino, duckdb) still uses the per-field file.
      - The value is stripped (secret stores / `.env` / `$(cat file)` commonly append a
        trailing newline, which would otherwise mis-parse), and an empty-or-whitespace-only
        value is treated as unset (falls through to the next source) — set the var to a real
        DSN to take effect; don't set it to "" expecting to *disable* one.
      - Tokens fold every non-alphanumeric char to `_`, so ids differing only in
        punctuation (`sales-pg` vs `sales.pg`) map to the same var — name orgs/profiles
        distinctly.
    """
    org_token, profile_token = _env_token(org_id), _env_token(profile)
    names = [f"{org_token}_DATASOURCE_URL__{profile_token}", f"{org_token}_DATASOURCE_URL"]
    if org_id == "local":
        # Single-tenant default keeps the historical org-less vars; a named tenant does NOT.
        names += [f"DATASOURCE_URL__{profile_token}", "DATASOURCE_URL"]
    for name in names:
        val = os.environ.get(name)
        if val and val.strip():
            return val.strip()  # match the file path's per-field .strip()
    return None


def _load_credentials(profile: str, org_id: str = "local") -> dict[str, str]:
    """Resolve credentials for `(org_id, profile)`, env-first then the file.

    Source order:
      1. A DSN from the environment (<ORG>_DATASOURCE_URL[__<PROFILE>], and for org 'local'
         also the org-less DATASOURCE_URL forms) — the self-host / multi-tenant channel;
         parsed by `_parse_dsn`, no file read, no chmod gate. See `_env_datasource_dsn` for
         the exact precedence + the fail-closed rule for named tenants.
      2. <artifacts_dir>/local/credentials (the local-plugin default), where within
         the selected profile a `url = ...` DSN (merged with per-field overrides) or
         per-field host / port / user / password / database / type / sslmode is read.

    The env is an added *source*, not a fork: the file path — and its chmod-600 gate
    (never on a command line) — is unchanged, and is skipped only when a deployment
    opts into the env var (and so has no file to protect).
    """
    dsn = _env_datasource_dsn(profile, org_id)
    if dsn:
        return _parse_dsn(dsn)

    if not CREDENTIALS_PATH.exists():
        raise ExecutorError(
            f"No warehouse credentials for profile [{profile}]. Set DATASOURCE_URL "
            f"(or DATASOURCE_URL__{_env_token(profile)}) "
            "in the environment, or create <artifacts_dir>/local/credentials via the agami `init` skill.\n"
            "Never type credentials into chat — they belong in the environment or the file.",
            code=2,
        )

    # chmod check: refuse if too permissive. POSIX only — Windows file modes don't
    # map to Unix permission bits (NTFS ACLs guard the file; a stat() there reports
    # ~0o666, which would wrongly trip this gate and block the credentials read).
    if os.name == "posix":
        mode = stat.S_IMODE(CREDENTIALS_PATH.stat().st_mode)
        if mode not in ALLOWED_PERMS:
            raise ExecutorError(
                f"<artifacts_dir>/local/credentials must be chmod 600 (currently {oct(mode)[2:]})\n"
                f"Run: chmod 600 <artifacts_dir>/local/credentials",
                code=2,
            )

    # IMPORTANT: enable inline-comment stripping for both `#` and `;`. Without
    # this, a credentials line like `account = xy12345  # locator + region`
    # parses as `xy12345  # locator + region` (the comment becomes part of the
    # value), which then gets fed to Snowflake/Postgres/MySQL as a junk
    # hostname/account and the connection hangs or fails confusingly.
    cfg = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    cfg.read(CREDENTIALS_PATH)
    if profile not in cfg:
        raise ExecutorError(
            f"Profile [{profile}] not found in <artifacts_dir>/local/credentials. "
            f"Sections present: {cfg.sections()}",
            code=2,
        )

    section = {k: (v.strip() if isinstance(v, str) else v) for k, v in cfg[profile].items()}

    # Accept the friendlier `service_account` / `credentials_path` spellings in the
    # per-field form too — the BigQuery executor reads `service_account_path`, and the
    # DSN parser already treats all three as equivalent. Without this, a per-field
    # `service_account = ...` would be silently ignored (falling back to ADC).
    for alias in ("service_account", "credentials_path"):
        if section.get(alias) and not section.get("service_account_path"):
            section["service_account_path"] = section[alias]

    # If the profile has `url = ...` (e.g. a Supabase / Neon / RDS DSN), parse it
    # and merge with any per-field overrides (sslmode, etc.) defined alongside.
    if "url" in section and section["url"]:
        from_dsn = _parse_dsn(section["url"])
        # Per-field values in the same section override DSN values, except for
        # `url` itself which we drop from the output.
        merged = dict(from_dsn)
        for k, v in section.items():
            if k == "url":
                continue
            merged[k] = v
        return merged

    return section


# Schemes we accept. Strip "+driver" suffixes (e.g. postgresql+asyncpg, postgres+psycopg2).
_POSTGRES_SCHEMES = {"postgres", "postgresql"}
_MYSQL_SCHEMES = {"mysql", "mariadb"}
_REDSHIFT_SCHEMES = {"redshift"}  # speaks Postgres wire protocol; port 5439, SSL required
_SNOWFLAKE_SCHEMES = {"snowflake"}  # native CLI (snowsql) + snowflake-connector-python
_BIGQUERY_SCHEMES = {
    "bigquery",
    "bq",
}  # google-cloud-bigquery — auth via service-account JSON or ADC


def _parse_dsn(dsn: str) -> dict[str, str]:
    """Parse a database DSN into a credentials dict.

    Supported schemes (with or without `+driver` suffix):
      postgresql://, postgres://, postgresql+asyncpg://, postgresql+psycopg2://,
      postgresql+psycopg://, postgres+asyncpg:// — all map to type=postgres.
      mysql://, mariadb://, mysql+pymysql:// — all map to type=mysql.
      sqlite:///absolute/path/to.db — maps to type=sqlite.
      redshift://user:pass@cluster.region.redshift.amazonaws.com:5439/db — type=redshift
      snowflake://user:pass@account.region.cloud/database/schema?warehouse=wh&role=r
        — type=snowflake. The path is `/database` or `/database/schema`. Query
        params (warehouse, role, application, authenticator) are carried over.

    Cloud Postgres providers (Supabase, Neon, RDS, etc.) frequently use the
    SQLAlchemy-style `postgresql+asyncpg://...` form. We accept it.

    Query-string parameters on the DSN (e.g. `?sslmode=require`) are merged
    into the output dict — useful for SSL settings.
    """
    u = urllib.parse.urlparse(dsn)
    raw_scheme = u.scheme.lower()

    # Strip "+driver" suffix: "postgresql+asyncpg" → "postgresql"
    base_scheme = raw_scheme.split("+", 1)[0]

    if base_scheme in _POSTGRES_SCHEMES:
        db_type = "postgres"
        default_port = 5432
    elif base_scheme in _REDSHIFT_SCHEMES:
        # Redshift speaks Postgres wire protocol → reuse postgres execution path.
        # The only thing that's different is the default port (5439 vs 5432) and
        # that SSL is required by default.
        db_type = "redshift"
        default_port = 5439
    elif base_scheme in _MYSQL_SCHEMES:
        db_type = "mysql"
        default_port = 3306
    elif base_scheme in _SNOWFLAKE_SCHEMES:
        db_type = "snowflake"
        default_port = 443  # Snowflake is HTTPS-only; port not used by snowsql/connector
    elif base_scheme in _BIGQUERY_SCHEMES:
        # BigQuery URLs follow the SQLAlchemy-bigquery convention:
        #   bigquery://<project>             — default dataset comes from creds
        #   bigquery://<project>/<dataset>   — set the default dataset
        # Query params may carry: service_account, location.
        # No host:port — BigQuery is HTTPS-only via the Google Cloud REST API.
        project = u.hostname or ""
        path_parts = (u.path or "").lstrip("/").split("/") if u.path else []
        out = {
            "type": "bigquery",
            "project": project,
        }
        if path_parts and path_parts[0]:
            out["dataset"] = path_parts[0]
        if u.query:
            for k, v in urllib.parse.parse_qsl(u.query):
                key = k.lower()
                # `service_account` and `credentials_path` both map to the
                # file path of the JSON service-account key.
                if key in ("credentials_path", "service_account"):
                    out["service_account_path"] = v
                else:
                    out[key] = v
        return out
    elif base_scheme == "sqlite":
        # sqlite:///absolute/path or sqlite:relative/path
        path = dsn[len("sqlite://") :]
        if path.startswith("/"):
            path = path[1:] if path[1:2] == "/" else path  # handle `sqlite:////abs`
        # Trailing path normalization
        result = {"type": "sqlite", "path": path or u.path.lstrip("/")}
        return result
    else:
        raise ExecutorError(
            f"Unsupported scheme {raw_scheme!r}. "
            f"Supported: postgresql[+driver], postgres[+driver], redshift, "
            f"mysql[+driver], mariadb, snowflake, sqlite.",
            code=2,
        )

    # Snowflake's URL is account-shaped, not host:port. The "hostname" portion
    # of `snowflake://user:pw@xy12345.us-east-1.aws/MYDB/PUBLIC` is the account
    # identifier, and the path holds DATABASE[/SCHEMA].
    if db_type == "snowflake":
        path_parts = (u.path or "").lstrip("/").split("/")
        out = {
            "type": "snowflake",
            "account": u.hostname or "",
            "user": urllib.parse.unquote(u.username or ""),
            "password": urllib.parse.unquote(u.password or ""),
            "database": path_parts[0] if path_parts and path_parts[0] else "",
        }
        if len(path_parts) > 1 and path_parts[1]:
            out["schema"] = path_parts[1]
        # Carry warehouse, role, application, authenticator from query params
        if u.query:
            for k, v in urllib.parse.parse_qsl(u.query):
                out[k.lower()] = v
        return out

    out: dict[str, str] = {
        "type": db_type,
        "host": u.hostname or "",
        "port": str(u.port or default_port),
        "user": urllib.parse.unquote(u.username or ""),
        "password": urllib.parse.unquote(u.password or ""),
        "database": (u.path or "").lstrip("/"),
    }

    # Merge any query-string params (e.g. ?sslmode=require)
    if u.query:
        for k, v in urllib.parse.parse_qsl(u.query):
            out[k.lower()] = v

    # Redshift defaults: SSL required if not explicitly set
    if db_type == "redshift" and "sslmode" not in out:
        out["sslmode"] = "require"

    return out


def _require(creds: dict[str, str], *fields: str) -> None:
    """Raise ``ExecutorError`` (not ``sys.exit``) when a required credential field is missing, so the
    same check is safe in-process (a bad profile can't kill the server) and the subprocess ``main``
    still surfaces the identical stderr message + exit code 2."""
    missing = [f for f in fields if not creds.get(f)]
    if missing:
        raise ExecutorError(
            f"Credentials profile is missing required fields: {missing}. "
            f"Edit <artifacts_dir>/local/credentials and add them.",
            code=2,
        )


def _run_postgres(creds: dict[str, str], sql: str) -> ExecResult:
    try:
        import psycopg2  # type: ignore
    except ImportError:
        raise ExecutorError("psycopg2 not installed. Run: pip install psycopg2-binary", code=3)
    _require(creds, "host", "port", "user", "password", "database")
    try:
        conn = psycopg2.connect(
            host=creds["host"],
            port=int(creds["port"]),
            user=creds["user"],
            password=creds["password"],
            dbname=creds["database"],
            sslmode=creds.get("sslmode", "prefer"),
            connect_timeout=10,
        )
    except Exception as e:
        raise ExecutorError(f"Postgres connect failed: {e}", code=4)
    try:
        with conn:
            # Genuine server-side cancel: the backend aborts the statement itself after the deadline.
            # Set per-transaction via SET LOCAL — NOT the libpq `options` startup parameter, which a
            # transaction-mode pooler (Supabase Supavisor, PgBouncer) can reject at connect. SET LOCAL
            # is transaction-scoped, which those poolers pass through cleanly. The watchdog's
            # conn.cancel() (a pg_cancel request) is the client-initiated backstop.
            with conn.cursor() as setup:
                setup.execute(f"SET LOCAL statement_timeout = {_timeout_ms()}")
            # A server-side (named) cursor so the row cap bounds TRANSFER, not just what we write:
            # psycopg2's default client-side cursor buffers the ENTIRE result before we can fetchmany,
            # so a runaway result would still be pulled whole. The named cursor streams from the
            # server in bounded batches (ACE-038). Read-only SELECTs (the only thing the guard admits)
            # are exactly what a server-side cursor supports.
            cur = conn.cursor(name="agami_bounded")  # not `with`: a cancelled txn makes CLOSE raise
            cur.itersize = _resolve_row_cap() + 1  # server fetch batch = the bounded window
            try:
                result = _run_bounded(lambda: _exec(cur, sql), conn.cancel)
            finally:
                try:
                    cur.close()
                except Exception:
                    pass  # a cancelled query leaves the txn aborted; closing the server-side cursor
                    # would raise and mask _ResourceLimit — swallow so the timeout refusal survives
    except _ResourceLimit:
        raise  # timed out; `with conn` rolled back — execute_guarded maps it to a resource_limit refusal
    except Exception as e:
        raise ExecutorError(f"Postgres execution error: {e}", code=5)
    finally:
        conn.close()
    return result


def _run_mysql(creds: dict[str, str], sql: str) -> ExecResult:
    try:
        import pymysql  # type: ignore
    except ImportError:
        raise ExecutorError("pymysql not installed. Run: pip install pymysql", code=3)
    _require(creds, "host", "port", "user", "password", "database")
    secs = _resolve_timeout_s()
    try:
        conn = pymysql.connect(
            host=creds["host"],
            port=int(creds["port"]),
            user=creds["user"],
            password=creds["password"],
            database=creds["database"],
            charset="utf8mb4",
            connect_timeout=10,
            # The reliable client-side bound: a socket read/write blocked past the deadline raises on
            # its own. Closing the connection from the watchdog thread does NOT unblock a recv() on
            # Linux (that needs shutdown(), which pymysql's close() skips), so this — not the close —
            # is what guarantees MySQL/MariaDB can't hang past the deadline.
            read_timeout=secs,
            write_timeout=secs,
            autocommit=True,
        )
    except Exception as e:
        raise ExecutorError(f"MySQL connect failed: {e}", code=4)
    # Native server-side timeout. MySQL 5.7.8+ spells it max_execution_time (ms); MariaDB spells it
    # max_statement_time (seconds). Try both — each is a no-op on the other dialect (a swallowed
    # unknown-variable error) — so whichever the server is gets a genuine server-side abort.
    for stmt in (
        f"SET SESSION max_execution_time={secs * 1000}",
        f"SET SESSION max_statement_time={secs}",
    ):
        try:
            with conn.cursor() as _c:
                _c.execute(stmt)
        except Exception:
            pass  # not this dialect; read_timeout still bounds the client
    try:
        cur = conn.cursor()  # not `with` — the watchdog may close conn, so avoid a close-on-close
        result = _run_bounded(lambda: _exec(cur, sql), lambda: _safe_close(conn))
    except _ResourceLimit:
        raise
    except Exception as e:
        raise ExecutorError(f"MySQL execution error: {e}", code=5)
    finally:
        _safe_close(conn)
    return result


def _run_snowflake(creds: dict[str, str], sql: str) -> ExecResult:
    """Tier-3 path for Snowflake using snowflake-connector-python."""
    try:
        import snowflake.connector  # type: ignore
    except ImportError:
        raise ExecutorError(
            "snowflake-connector-python not installed. "
            "Run: pip install snowflake-connector-python",
            code=3,
        )
    _require(creds, "account", "user")
    if not (creds.get("password") or creds.get("authenticator")):
        raise ExecutorError(
            "Snowflake profile is missing 'password' or 'authenticator'. "
            "Add one to <artifacts_dir>/local/credentials.",
            code=2,
        )
    conn_kwargs: dict[str, Any] = {
        "account": creds["account"],
        "user": creds["user"],
        "client_session_keep_alive": False,
        "login_timeout": 15,
        # Native server-side statement timeout (seconds) — Snowflake aborts the query itself.
        "session_parameters": {"STATEMENT_TIMEOUT_IN_SECONDS": _resolve_timeout_s()},
    }
    for k in ("password", "warehouse", "database", "schema", "role", "authenticator"):
        if creds.get(k):
            conn_kwargs[k] = creds[k]
    try:
        conn = snowflake.connector.connect(**conn_kwargs)
    except Exception as e:
        raise ExecutorError(f"Snowflake connect failed: {e}", code=4)
    try:
        cur = conn.cursor()
        result = _run_bounded(lambda: _exec(cur, sql), lambda: _safe_close(conn))
    except _ResourceLimit:
        raise
    except Exception as e:
        raise ExecutorError(f"Snowflake execution error: {e}", code=5)
    finally:
        _safe_close(conn)
    return result


def _run_bigquery(creds: dict[str, str], sql: str) -> ExecResult:
    """Tier-3 path for BigQuery using google-cloud-bigquery.

    Required: `project`. One of: `service_account_path` (path to a JSON key
    file), OR no auth at all (falls back to Application Default Credentials —
    `gcloud auth application-default login`). Optional: `dataset` (sets the
    default dataset so unqualified table refs resolve), `location` (e.g. `US`,
    `EU`, `asia-northeast1`).
    """
    try:
        from google.cloud import bigquery  # type: ignore
        from google.oauth2 import service_account  # type: ignore
    except ImportError:
        raise ExecutorError(
            "google-cloud-bigquery not installed. "
            "Run: pip install google-cloud-bigquery",
            code=3,
        )

    _require(creds, "project")
    project = creds["project"]
    sa_path = creds.get("service_account_path")
    location = creds.get("location") or None

    client_kwargs: dict[str, Any] = {"project": project}
    if location:
        client_kwargs["location"] = location

    if sa_path:
        sa_path_expanded = os.path.expanduser(sa_path)
        if not os.path.exists(sa_path_expanded):
            raise ExecutorError(
                f"service_account_path '{sa_path}' doesn't exist. "
                f"Point at the JSON key file you downloaded from GCP.",
                code=2,
            )
        # Defensive chmod check — service-account JSON contains a private key.
        try:
            mode = stat.S_IMODE(os.stat(sa_path_expanded).st_mode)
            if mode not in ALLOWED_PERMS:
                sys.stderr.write(
                    f"Warning: service_account_path '{sa_path}' has permissions "
                    f"{oct(mode)} — should be 0600. The file contains a private key.\n"
                )
        except Exception:
            pass
        try:
            creds_obj = service_account.Credentials.from_service_account_file(sa_path_expanded)
            client_kwargs["credentials"] = creds_obj
        except Exception as e:
            raise ExecutorError(f"BigQuery credentials load failed: {e}", code=2)

    try:
        client = bigquery.Client(**client_kwargs)
    except Exception as e:
        raise ExecutorError(f"BigQuery client init failed: {e}", code=4)

    # If `dataset` was set, prefix unqualified table references via the
    # default_dataset job config so the SQL can omit `<project>.<dataset>.`
    # `job_timeout_ms` is the native server-side job timeout — BigQuery ends the job itself.
    job_config_kwargs: dict[str, Any] = {"job_timeout_ms": _timeout_ms()}
    if creds.get("dataset"):
        try:
            job_config_kwargs["default_dataset"] = f"{project}.{creds['dataset']}"
        except Exception:
            pass

    cap = _resolve_row_cap()
    job_box: dict[str, Any] = {}
    started = time.monotonic()
    timeout_s = _resolve_timeout_s()

    def _bq_cancel() -> None:
        job = job_box.get("job")
        if job is not None:
            job.cancel()  # genuine server-side job cancel (the client-initiated backstop to job_timeout_ms)

    try:
        with _deadline(_bq_cancel, timeout_s) as fired:
            try:
                job = client.query(sql, job_config=bigquery.QueryJobConfig(**job_config_kwargs))
                job_box["job"] = job
                # BigQuery has no DB-API cursor, so it can't funnel through `_collect_cursor`; apply the
                # same bounded-fetch cap here. `max_results=cap+1` bounds what the API returns (transfer),
                # and the (cap+1)th row flags truncation — the never-silent guarantee holds for BigQuery too.
                results = job.result(max_results=cap + 1)  # waits for completion; raises on error
            except Exception:
                if _deadline_hit(fired, started, timeout_s):
                    raise _ResourceLimit from None
                raise
    except _ResourceLimit:
        raise  # execute_guarded maps a fired BigQuery deadline to a resource_limit refusal
    except Exception as e:
        raise ExecutorError(f"BigQuery execution error: {e}", code=5)

    if not results.schema:
        return ExecResult(columns=[], rows=[], truncated=False)
    columns = [f.name for f in results.schema]
    ncols = len(results.schema)
    rows: list[tuple] = []
    truncated = False
    for row in results:
        if len(rows) >= cap:
            truncated = True
            break
        rows.append(tuple(row[i] for i in range(ncols)))
    return ExecResult(columns=columns, rows=rows, truncated=truncated)


def _run_sqlite(creds: dict[str, str], sql: str) -> ExecResult:
    import sqlite3  # always available in stdlib

    _require(creds, "path")
    path = os.path.expanduser(creds["path"])
    try:
        conn = sqlite3.connect(path)
    except Exception as e:
        raise ExecutorError(f"SQLite connect failed: {e}", code=4)
    try:
        cur = conn.cursor()
        result = _run_bounded(lambda: _exec(cur, sql), conn.interrupt)
    except _ResourceLimit:
        raise  # timed out — execute_guarded maps it to a resource_limit refusal
    except Exception as e:
        raise ExecutorError(f"SQLite execution error: {e}", code=5)
    finally:
        conn.close()
    return result


def _run_sqlserver(creds: dict[str, str], sql: str) -> ExecResult:
    """Tier-3 path for SQL Server / Azure SQL using pymssql."""
    try:
        import pymssql  # type: ignore
    except ImportError:
        raise ExecutorError("pymssql not installed. Run: pip install pymssql", code=3)
    _require(creds, "host", "user", "password")
    try:
        conn = pymssql.connect(
            server=creds["host"],
            port=int(creds.get("port", 1433)),
            user=creds["user"],
            password=creds["password"],
            database=creds.get("database", ""),
            login_timeout=15,
            timeout=_resolve_timeout_s(),  # native per-query timeout (seconds)
        )
    except Exception as e:
        raise ExecutorError(f"SQL Server connect failed: {e}", code=4)
    try:
        cur = conn.cursor()
        result = _run_bounded(lambda: _exec(cur, sql), lambda: _safe_close(conn))
    except _ResourceLimit:
        raise
    except Exception as e:
        raise ExecutorError(f"SQL Server execution error: {e}", code=5)
    finally:
        _safe_close(conn)
    return result


def _run_oracle(creds: dict[str, str], sql: str) -> ExecResult:
    """Tier-3 path for Oracle using python-oracledb (thin mode — no client libs)."""
    try:
        import oracledb  # type: ignore
    except ImportError:
        raise ExecutorError("python-oracledb not installed. Run: pip install oracledb", code=3)
    _require(creds, "user", "password")
    dsn = creds.get("dsn") or creds.get("url")
    if not dsn:
        _require(creds, "host", "service_name")
        dsn = oracledb.makedsn(
            creds["host"], int(creds.get("port", 1521)), service_name=creds["service_name"]
        )
    try:
        conn = oracledb.connect(user=creds["user"], password=creds["password"], dsn=dsn)
    except Exception as e:
        raise ExecutorError(f"Oracle connect failed: {e}", code=4)
    try:
        conn.call_timeout = _timeout_ms()  # native round-trip timeout (ms)
    except Exception:
        pass  # older driver / mode; the wall-clock watchdog still bounds the query
    try:
        cur = conn.cursor()
        result = _run_bounded(lambda: _exec(cur, sql), lambda: _safe_close(conn))
    except _ResourceLimit:
        raise
    except Exception as e:
        raise ExecutorError(f"Oracle execution error: {e}", code=5)
    finally:
        _safe_close(conn)
    return result


def _run_databricks(creds: dict[str, str], sql: str) -> ExecResult:
    """Tier-3 path for Databricks SQL warehouses using databricks-sql-connector."""
    try:
        from databricks import sql as dbsql  # type: ignore
    except ImportError:
        raise ExecutorError(
            "databricks-sql-connector not installed. Run: pip install databricks-sql-connector",
            code=3,
        )
    _require(creds, "host", "http_path", "token")
    try:
        conn = dbsql.connect(
            server_hostname=creds["host"],
            http_path=creds["http_path"],
            access_token=creds["token"],
        )
    except Exception as e:
        raise ExecutorError(f"Databricks connect failed: {e}", code=4)
    try:
        with conn.cursor() as _c:  # best-effort native server-side bound (seconds; 0 = no timeout)
            _c.execute(f"SET STATEMENT_TIMEOUT = {_resolve_timeout_s()}")
    except Exception:
        pass  # older runtime without the config; cur.cancel() below is the cross-thread cancel
    try:
        cur = conn.cursor()
        # cur.cancel() is a genuine server-side cancel of the running statement.
        result = _run_bounded(lambda: _exec(cur, sql), cur.cancel)
    except _ResourceLimit:
        raise
    except Exception as e:
        raise ExecutorError(f"Databricks execution error: {e}", code=5)
    finally:
        _safe_close(conn)
    return result


def _run_trino(creds: dict[str, str], sql: str) -> ExecResult:
    """Tier-3 path for Trino / Presto using the trino python client."""
    try:
        import trino  # type: ignore
    except ImportError:
        raise ExecutorError("trino not installed. Run: pip install trino", code=3)
    _require(creds, "host", "user")
    try:
        auth = None
        if creds.get("password"):
            auth = trino.auth.BasicAuthentication(creds["user"], creds["password"])
        conn = trino.dbapi.connect(
            host=creds["host"],
            port=int(creds.get("port", 8080)),
            user=creds["user"],
            catalog=creds.get("catalog"),
            schema=creds.get("schema"),
            http_scheme="https" if creds.get("password") else "http",
            auth=auth,
            # Native server-side cap on total query runtime.
            session_properties={"query_max_run_time": f"{_resolve_timeout_s()}s"},
        )
    except Exception as e:
        raise ExecutorError(f"Trino connect failed: {e}", code=4)
    try:
        cur = conn.cursor()
        # cur.cancel() cancels the running Trino query server-side.
        result = _run_bounded(lambda: _exec(cur, sql), cur.cancel)
    except _ResourceLimit:
        raise
    except Exception as e:
        raise ExecutorError(f"Trino execution error: {e}", code=5)
    finally:
        _safe_close(conn)
    return result


def _run_duckdb(creds: dict[str, str], sql: str) -> ExecResult:
    """Tier-3 path for DuckDB using the duckdb python module (file or in-memory)."""
    try:
        import duckdb  # type: ignore
    except ImportError:
        raise ExecutorError("duckdb not installed. Run: pip install duckdb", code=3)
    path = creds.get("path") or creds.get("database") or ":memory:"
    try:
        conn = duckdb.connect(path, read_only=True)
    except Exception as e:
        raise ExecutorError(f"DuckDB open failed: {e}", code=4)
    try:
        # DuckDB runs the query in conn.execute() and returns the streaming source; conn.interrupt()
        # aborts an in-flight query (in-process, thread-safe) — same shape as SQLite.
        result = _run_bounded(lambda: conn.execute(sql), conn.interrupt)
    except _ResourceLimit:
        raise
    except Exception as e:
        raise ExecutorError(f"DuckDB execution error: {e}", code=5)
    finally:
        _safe_close(conn)
    return result


_DEFAULT_MAX_ROWS = 1000  # rows materialized per result before truncation (ACE-038)
# Per-call cap from --max-rows (ACE-044). A ContextVar, not a plain global, so it is REQUEST-SCOPED
# once the HTTP server runs execution in-process (ACE-028): concurrent handlers run in worker threads
# (`run_blocking` copies the context per call, like `_current_org_ctx`), so each request's cap is
# isolated and can't stomp another's. In the subprocess/CLI (one process, one thread) it behaves
# exactly as the old module global did.
_max_rows_override: ContextVar[int | None] = ContextVar("_max_rows_override", default=None)


def _resolve_row_cap() -> int:
    """Effective result-row cap. `AGAMI_SQL_MAX_ROWS` is the operator-configurable DEPLOYMENT cap
    (default 1000 when unset) — an operator owns their availability tradeoff and may set it higher OR
    lower than 1000; it is NOT a hard 1000 ceiling. A per-call `--max-rows` can only LOWER it for a
    single call (cap = min(env, --max-rows)). A missing/invalid/zero env value falls back to 1000."""
    raw = os.environ.get("AGAMI_SQL_MAX_ROWS", "").strip()
    cap = int(raw) if raw.isdigit() else _DEFAULT_MAX_ROWS
    if cap <= 0:
        cap = _DEFAULT_MAX_ROWS  # "0" / "00" → the default, never an empty result
    override = _max_rows_override.get()
    if override is not None and override > 0:
        cap = min(cap, override)
    return cap


def _flag_truncated(cap: int) -> None:
    """Signal a bounded-fetch truncation to the caller — a non-error `{"truncated": …}` marker on
    stderr (distinct from the guards' `{"error": …}`), so a truncated result is never mistaken for a
    complete one (ACE-038/044). Shared by every engine's materialization path. One write so the
    marker is always a single line, even if other notices surround it."""
    sys.stderr.write(json.dumps({"truncated": {"row_cap": cap}}) + "\n")


def _collect_cursor(cur: Any) -> ExecResult:
    """Fetch at most the row cap from a DB-API cursor into an ``ExecResult`` with **native types**.
    `fetchmany(cap + 1)` — never `fetchall` — so a huge result can't be buffered whole; a (cap+1)th
    row means the result was truncated. The SQL itself is untouched (no injected LIMIT). This is the
    single bounded-fetch implementation both the CSV wire (`_write_cursor_csv`) and the in-process
    executor path share, so the row cap is enforced once, identically, for every caller.

    Fetch FIRST, then read ``cur.description``: a psycopg2 **server-side (named) cursor** — which the
    Postgres/Redshift path uses to bound transfer (ACE-038) — reports ``description is None`` until the
    first fetch, so reading it beforehand would drop EVERY row of a real Postgres result. Client-side
    cursors (sqlite/mysql/…) set ``description`` at execute, so fetch-first is equally correct there."""
    cap = _resolve_row_cap()
    fetched = cur.fetchmany(cap + 1)
    if cur.description is None:
        # A statement with no result set. The read-only guard admits only SELECT/WITH…SELECT (which
        # always have a result set), so this is defensive; emit nothing, matching the old sink.
        return ExecResult(columns=[], rows=[], truncated=False)
    columns = [d[0] for d in cur.description]
    truncated = len(fetched) > cap
    return ExecResult(columns=columns, rows=[tuple(r) for r in fetched[:cap]], truncated=truncated)


def _flag_masked(columns: tuple[str, ...]) -> None:
    """Signal which OUTPUT columns were PII-redacted to the subprocess caller — a non-error
    ``{"masked": [...]}`` marker on stderr, the SAME metadata channel as ``{"truncated": …}`` (not a
    new protocol). The redacted VALUES already ride the CSV on stdout; this line carries only the
    ``applied[{mask}]`` note the parent records on the Envelope (ACE-041 slice 3)."""
    sys.stderr.write(json.dumps({"masked": list(columns)}) + "\n")


def _emit_result_csv(result: ExecResult) -> None:
    """Serialize an ``ExecResult`` to stdout as CSV — the subprocess/CLI wire. Byte-for-byte what the
    old inline cursor→CSV writer produced: header row then data rows, and a truncation marker on
    stderr when capped. This is the *single, final* text serialization for the fork path; the
    in-process path skips it and returns the native rows straight to the tool edge. When the rows were
    PII-masked, the redacted VALUES are already in ``result.rows`` (masked in ``execute_guarded``
    before this call), so the CSV carries them; a ``{"masked": …}`` stderr marker names the columns."""
    if not result.columns:  # cursor had no description → wrote nothing (e.g. a non-row statement)
        return
    writer = csv.writer(sys.stdout)
    writer.writerow(result.columns)
    for row in result.rows:
        writer.writerow(row)
    if result.truncated:
        _flag_truncated(_resolve_row_cap())
    if result.masked_columns:
        _flag_masked(result.masked_columns)


def _write_cursor_csv(cur: Any) -> None:
    """Collect the bounded result and write it to stdout as CSV — the per-engine sink the subprocess
    path uses. Kept as the thin composition ``_emit_result_csv(_collect_cursor(cur))`` so the fetch
    bound and the CSV shape stay single-sourced (and the existing bounded-fetch tests still pin it)."""
    _emit_result_csv(_collect_cursor(cur))


# ---------------------------------------------------------------------------
# Per-statement timeout — the availability guarantee
#
# A generated query can hang or exhaust the DB (recursive CTE, cartesian bomb, unbounded scan). The
# read-only role is not mandated to carry a role-level statement_timeout, so this app-layer bound is
# the SOLE availability guarantee: a wall-clock watchdog cancels the in-flight query after
# AGAMI_SQL_TIMEOUT_S (default 30) — the universal backstop for every engine — layered under each
# engine's native statement timeout where it has one (the primary, genuine DB-side cancel). A fired
# deadline emits a `resource_limit` refusal; the query is killed, no result is returned.
# ---------------------------------------------------------------------------

_DEFAULT_TIMEOUT_S = 30  # per-statement wall-clock timeout, overridable by AGAMI_SQL_TIMEOUT_S


def _resolve_timeout_s() -> int:
    """Per-statement timeout in seconds — `AGAMI_SQL_TIMEOUT_S` (default 30). A missing / invalid /
    non-positive value falls back to the default; there is no "0 = unlimited" (availability is a
    safety obligation — a query is always bounded). Read once at the point of use, like
    `_resolve_row_cap`."""
    raw = os.environ.get("AGAMI_SQL_TIMEOUT_S", "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else _DEFAULT_TIMEOUT_S


@contextmanager
def _deadline(cancel: Callable[[], None], timeout_s: int):
    """Bound the enclosed statement by a wall-clock watchdog: after `timeout_s` seconds, `cancel()`
    interrupts the in-flight query (per driver — `conn.cancel()` / `conn.interrupt()` / `cur.cancel()`),
    which makes the blocked execute/fetch raise. The universal availability backstop so no engine hangs
    past the deadline, even one with no native statement timeout. Yields an Event that is set iff the
    deadline fired (so the caller can tell a timeout from a real error).

    `timeout_s` is passed in (not re-read from the env here) so the watchdog duration and the caller's
    `_deadline_hit` elapsed-vs-timeout classification key off the SAME resolved value — a mid-flight
    change to `AGAMI_SQL_TIMEOUT_S` can't make the two diverge (Copilot review)."""
    fired = threading.Event()

    def _fire() -> None:
        fired.set()
        try:
            cancel()
        except Exception:
            pass  # best-effort cancel; the deadline having fired is what the caller keys on

    timer = threading.Timer(timeout_s, _fire)
    timer.daemon = True
    timer.start()
    try:
        yield fired
    finally:
        timer.cancel()


class _ResourceLimit(Exception):
    """The per-statement deadline fired and the query was cancelled. Raised out through the engine's
    `with conn` / transaction handling so it ROLLS BACK (a cancelled query leaves the txn aborted)
    rather than committing; the engine re-raises it and `execute_guarded` maps it to the typed
    `resource_limit` refusal (one refusal, both surfaces)."""


def _timeout_ms() -> int:
    """The statement timeout in milliseconds — for the engines whose native param takes ms."""
    return _resolve_timeout_s() * 1000


def _safe_close(conn: Any) -> None:
    """Close a connection, swallowing errors — safe to call twice (the watchdog may close it to
    interrupt an in-flight query, then the engine's `finally` closes it again)."""
    try:
        conn.close()
    except Exception:
        pass


def _exec(cur: Any, sql: str) -> Any:
    """Run `sql` on a DB-API cursor and return it as the streaming source (for `_run_bounded`)."""
    cur.execute(sql)
    return cur


def _resource_limit_refusal() -> Refusal:
    """Build the typed `resource_limit` refusal for a query cancelled at the statement deadline. The
    single source of the timeout refusal — `execute_guarded` raises it as a `GuardRefused` so both the
    subprocess wire and the in-process path surface the identical refused Envelope."""
    secs = _resolve_timeout_s()
    return Refusal(
        kind="resource_limit",
        reason=f"the query exceeded the {secs}s statement timeout and was cancelled",
        remediation=f"Narrow the query, or raise AGAMI_SQL_TIMEOUT_S (currently {secs}s).",
    )


def _deadline_hit(fired: threading.Event, started: float, timeout_s: int) -> bool:
    """True if the query was ended by the statement deadline. Primary signal: the watchdog `fired`
    (it sets the flag before it cancels, so a watchdog-driven kill is always flagged). Secondary
    signal: wall-clock elapsed reached the timeout — an engine's NATIVE server timeout (set to the
    same duration) can win the race and raise before the watchdog's callback runs, and without the
    elapsed check that genuine timeout would be mislabeled a generic error. Either signal → the query
    ran to the deadline, so the raise is a `resource_limit`, not a real error."""
    return fired.is_set() or (time.monotonic() - started) >= timeout_s


def _run_bounded(execute: Callable[[], Any], cancel: Callable[[], None]) -> ExecResult:
    """Run a statement under the deadline and return its bounded ``ExecResult``. `execute()` runs the
    SQL and returns the cursor/result to fetch from (a thunk so both the `cur.execute(sql)` engines and
    DuckDB's `conn.execute(sql)` fit). On a deadline hit — the client watchdog fired OR wall-clock
    reached the timeout (whichever mechanism, client cancel or native server timeout, killed the
    query) — it raises `_ResourceLimit`; the engine re-raises it and `execute_guarded` maps it to a
    single `resource_limit` refusal, so no partial result is ever returned. A non-timeout error
    (raised before the deadline) propagates unchanged to the engine's handler."""
    started = time.monotonic()
    timeout_s = _resolve_timeout_s()
    with _deadline(cancel, timeout_s) as fired:
        try:
            cur = execute()
            return _collect_cursor(cur)
        except Exception:
            if _deadline_hit(fired, started, timeout_s):
                raise _ResourceLimit from None
            raise


def _hosted() -> bool:
    """The served (hosted) path is signalled by a configured database — the same signal
    `tools._load_org` / `Store.from_env` use. On it, a missing model is a safety failure (fail
    closed); locally (no DB) a not-yet-built model legitimately means 'no model yet'."""
    return bool(os.environ.get("AGAMI_DB_URL") or os.environ.get("APP_DATABASE_URL"))


def _guard_org_id() -> str:
    """The org the guard-model DB load must scope to — mirrors ``tools._load_org`` (which passes
    ``org_id=_current_org_id()``). Without it, ``model_store.load_datasource`` defaults to
    ``DEFAULT_ORG`` ('local'), so a model seeded under a real minted org (a hosted, DB-only deploy with
    no baked ``/artifacts``) is invisible to the guard → a false ``model_unavailable`` refusal on every
    query. Lazy import + ``AGAMI_ORG_ID`` fallback so the stdlib-lean subprocess mirror still resolves."""
    try:
        from tools import _current_org_id  # mirror the serve path's org scoping

        return _current_org_id()
    except Exception:
        return os.environ.get("AGAMI_ORG_ID", "").strip() or "local"


def _resolve_guard_model(profile: str):
    """Resolve the semantic model for the safety pass, mirroring `tools._load_org` (ACE-051): from
    the DB when one is configured (hosted — the `/artifacts` disk mount may be absent), else the
    on-disk YAML (local). Returns a `Datasource` or None if neither is available.

    The DB/loader imports are lazy AND (for the DB) env-guarded on purpose: the local executor runs
    from a stdlib-lean mirror that does not ship `store`/`model_store`, so we only reach for them when
    a DB is set; and the loader import sits inside the disk-path try so an import failure degrades to
    None (hosted then fails closed) rather than crashing the executor."""
    # Any load failure below degrades to the next source (DB → disk → None), and each is LOGGED (via
    # `logging` → stderr, never stdout, so the single-JSON-object stdout contract callers parse stays
    # intact) with the exception TYPE only — never str(exc), which could leak a DSN. This makes a real
    # resolution bug observable: a lost import once made EVERY disk model fail to load and silently
    # disabled the model-safety guards, indistinguishable from a legitimate "no model yet".
    if _hosted():
        try:
            from model_store import load_datasource as _load_db
            from store import Store

            store = Store.from_env()
            if store is not None:
                try:
                    org = _load_db(store, profile, org_id=_guard_org_id())
                finally:
                    try:
                        store.close()
                    except Exception:
                        pass  # a close error must not discard a model that loaded fine (→ false refusal)
                if org is not None:
                    return org
        except Exception as exc:
            # DB unreachable/misconfigured -> fall through to disk (type only; str(exc) may carry a DSN).
            logging.getLogger(__name__).warning(
                "DB-backed model load failed for profile %r (%s); falling through to disk",
                profile,
                type(exc).__name__,
            )

    root = Path(os.environ.get("AGAMI_ARTIFACTS_DIR") or (Path.home() / "agami-artifacts")) / profile
    if (root / "datasource.yaml").exists():
        try:
            from semantic_model import loader as L

            return L.load_datasource(root)
        except Exception as exc:
            # unparseable/absent on disk, or a loader import/bug -> None (hosted then fails closed).
            logging.getLogger(__name__).warning(
                "disk model at %s failed to load (%s); treating as no model", root, type(exc).__name__
            )
    return None


def _refusal_from_verdict(kind: str, verdict: Verdict) -> Refusal:
    """Build the shared ``Refusal`` from a safety ``Verdict``. A safety verdict always refuses
    (``policy(safety)`` is ``reject`` in every tier), so the guard chain refuses unconditionally on a
    non-None verdict. ``kind`` is the caller-supplied refusal kind (the verdict's ``rule`` names the
    gate, e.g. ``read_only``; the refusal ``kind`` is the outward vocabulary, e.g. ``permission``)."""
    return Refusal(kind=kind, reason=verdict.detail, remediation=verdict.remediation)


def _unscopable_posture() -> str:
    """The unscopable-SQL rollout posture: ``enforce`` (default — fail-closed) or ``warn`` (a
    staged-rollout escape hatch that logs and allows). ``warn`` is never the shipped default; safety
    fails closed. This is an operational rollout knob, NOT the deployment tier — safety has no tier
    variance and enforces in every tier."""
    return os.environ.get("AGAMI_SQL_UNSCOPABLE_POSTURE", "enforce").strip().lower()


def _model_safety(
    sql: str, profile: str, area: str | None
) -> tuple[str, Refusal | None, MaskPlan | None]:
    """Semantic-model safety pass before execution: fan-trap / chasm-trap pre-flight + scope + PII +
    default_filters auto-application, over a model resolved from the DB (hosted) or disk (local).

    Returns ``(sql_to_run, refusal, mask_plan)``:
      - ``refusal`` is None to continue, or a typed ``Refusal`` the caller (``execute_guarded``)
        raises as a ``GuardRefused`` — so both the subprocess and in-process paths build the same
        envelope from it.
      - ``mask_plan`` (ACE-041 slice 3) is None unless the PII gate proved a *maskable* sensitive
        projection: the query then PROCEEDS and ``execute_guarded`` redacts the named output columns
        post-execution. An *untraceable* sensitive projection sets ``refusal`` instead (fail-closed).

    Inert (SQL unchanged, no refusal, no plan) when the model package isn't importable, or — on the
    LOCAL path only — when there is no model yet. On the HOSTED path a model that can't be resolved
    fails closed (refuses), never runs unguarded (ACE-051)."""
    try:
        from semantic_model import runtime as RT
    except Exception:
        # The model package (pydantic) isn't importable, so the guards can't run at all. On the
        # hosted served path that is the same "can't guarantee safety" condition as a missing model
        # — fail closed. Locally it stays a no-op (a bare install legitimately has no model). (The
        # sqlglot-unavailable / unparseable-SQL degrade-to-allow is a distinct fail-open owned by
        # ACE-037, not closed here.)
        if _hosted():
            return sql, Refusal(
                "model_unavailable",
                "semantic-model package not importable; refusing to run unguarded on the "
                "hosted server",
            ), None
        return sql, None, None  # local: model package not available -> no-op

    org = _resolve_guard_model(profile)
    if org is None:
        if _hosted():
            # Fail closed: a served query with no resolvable model must be refused, never run with
            # the fan/chasm/scope/PII guards silently off.
            return sql, Refusal(
                "model_unavailable",
                "no semantic model could be resolved (checked DB and disk); refusing to run "
                "unguarded on the hosted server",
            ), None
        return sql, None, None  # local: no model yet -> no-op (unchanged)

    # Build the shared guard context ONCE — parse the SQL + build each model index a single
    # time — and thread it through the battery below, instead of every guard re-parsing and
    # rebuilding its index (audit P2 / ACE-045). Behaviour-preserving: a guard given `ctx`
    # returns the same verdict as one that builds its own.
    ctx = RT.build_guard_context(sql, org)

    # Ungovernable-engine gate — if the datasource does not determine which engine its SQL
    # runs on, there is no grammar to read the statement in, so no gate below can reach a
    # trustworthy verdict and the statement is refused rather than run blind. This is
    # deliberately ahead of, and not subject to, the unscopable posture switch: the posture
    # is a staged-rollout hatch for queries that cannot be scoped, whereas a datasource that
    # cannot be parsed at all is a configuration fault the operator must fix. Refusing here
    # also keeps the guarantee independent of which gate would otherwise have run.
    if ctx is not None and ctx.dialect is None and ctx.parse_error:
        return sql, Refusal(
            "unscopable_sql",
            ctx.parse_error,
            # No rewrite of the query helps, so this must not invite a retry — it names the
            # one change that makes the datasource governable.
            "Declare the datasource's engine (storage_connections[].storage_type) so its SQL "
            "can be parsed for the right engine, then retry.",
        ), None

    # Scopability gate — refuse a query that can't be fully scoped (unparseable, or a non-`Table`
    # FROM/JOIN source the scope walk can't reject) rather than run it blind. Runs
    # BEFORE the object-scope gates so an unscopable query fails closed instead of reaching their
    # degrade-to-allow branches. Posture `warn` is a staged-rollout escape hatch (logs + allows);
    # the default `enforce` refuses. Safety fails closed regardless of tier.
    scop = RT.check_scopable(sql, org, ctx=ctx)
    if scop is not None:
        if _unscopable_posture() == "warn":
            sys.stderr.write(
                "[agami] unscopable SQL allowed (AGAMI_SQL_UNSCOPABLE_POSTURE=warn): "
                f"{scop.detail}\n"
            )
        else:
            return sql, _refusal_from_verdict("unscopable_sql", scop), None

    # Table-scope guard — a query may only reference tables the semantic model
    # declares; any other table in the connected database is refused. Runs FIRST
    # so the fan/chasm and sensitive checks below only evaluate in-scope tables.
    ts = RT.check_table_scope(sql, org, ctx=ctx)
    if ts is not None:
        return sql, _refusal_from_verdict("table_out_of_scope", ts), None

    # SELECT * ban — force every projected column to be named, so the column-scope
    # guard below can check what is actually returned (and nothing hides behind *).
    star = RT.check_no_select_star(sql, ctx=ctx)
    if star is not None:
        return sql, _refusal_from_verdict("select_star", star), None

    # Column-scope guard — a column that binds to a declared table must be one that
    # table declares (a hallucinated column, or a physical column the model excluded).
    cs = RT.check_column_scope(sql, org, ctx=ctx)
    if cs is not None:
        return sql, _refusal_from_verdict("column_out_of_scope", cs), None

    pf = RT.pre_flight_check(sql, org, ctx=ctx)
    if pf.risk and pf.action == "refuse":
        return sql, Refusal("preflight_refused", pf.reason, pf.suggestion or ""), None
    if pf.risk and pf.action == "auto_rewrite" and pf.rewritten_sql:
        sys.stderr.write(f"[agami] auto-corrected {pf.risk}: ran rewritten SQL. {pf.reason}\n")
        sql = pf.rewritten_sql
        ctx = RT.build_guard_context(sql, org)  # SQL changed -> refresh the shared context

    # Sensitive-column (PII) fail-closed on UNPARSEABLE SQL when the model declares PII (ACE-041
    # slice 3): a statement sqlglot cannot parse cannot be proven free of a raw sensitive projection.
    # Unlike the other model guards (which degrade to allow — that fail-open is ACE-037's to close;
    # ACE-037 is NOT on this branch), the PII path refuses a parse FAILURE outright rather than run a
    # possibly-PII-projecting query. Only a parse failure with sqlglot PRESENT (ctx is not None and
    # ctx.tree is None) is closed here; sqlglot-unavailable (ctx is None) stays the ACE-037-owned
    # fail-open, and a model with no sensitive columns has nothing to protect.
    if ctx is not None and ctx.tree is None and ctx.sensitive_by_table[1]:
        return sql, Refusal(
            "sensitive_columns",
            "the query could not be parsed to verify it does not expose sensitive columns, so it "
            "was refused.",
            "Simplify the SQL so it parses, then retry.",
        ), None

    # Sensitive-column (PII) guard — the deterministic chokepoint that protects PII identically for
    # the agami-query skill, the local MCP server, and cron. A sensitive column may still be counted,
    # filtered, grouped, or joined; only PROJECTING its raw value is offending. When it is, route the
    # gate's verdict through the shared data-protection policy: a *maskable* projection (every
    # offending projection is a clean 1:1 image of one output column) becomes a MASK plan and the
    # query PROCEEDS with those columns redacted post-execution; an *untraceable* projection fails
    # closed to a refusal (mirroring safety's "uncertainty ⇒ reject").
    mask_plan: MaskPlan | None = None
    sens = RT.check_sensitive_projection(sql, org, ctx=ctx)
    if sens.action == "refuse":
        # Two conditions, and BOTH are required. `all_maskable` quantifies over the projections
        # the detector FOUND, so on its own it proves only "every offending projection we saw is
        # maskable" — never "no sensitive value reaches the output". Under the earlier refuse-only
        # behaviour that gap leaked only when nothing was detected (anything detected blocked the
        # whole query); once a maskable projection let the query RUN, one seen column unblocked the
        # entire result set and every co-projected value the name-based detector missed came back
        # raw — beside a `***` implying the row had been protected. `output_provable` is the
        # missing half: the detector could see the whole output, not just part of it.
        all_maskable = bool(sens.projections) and all(
            p.maskable and p.output_index is not None for p in sens.projections
        )
        # `getattr`, not attribute access: this file is VENDORED into the plugin while
        # semantic_model/runtime.py resolves from the separately-versioned installed package, so a
        # newer plugin can meet an older `SensitiveCheckResult`. A missing field then means "this
        # build cannot prove the output", which is exactly the fail-closed answer.
        provable = all_maskable and getattr(sens, "output_provable", False)
        verdict = Verdict(
            cls="data_protection",
            rule="sensitive_projection",
            severity="high",
            certainty="provable" if provable else "uncertain",
            detail=sens.reason,
            remediation=sens.suggestion or "",
        )
        # data_protection enforces in every tier (tier never downgrades it), so policy's default
        # tier is correct here: provable → "mask", anything weaker → "reject" (fail closed).
        if policy(verdict) == "mask":
            indices = tuple(sorted({p.output_index for p in sens.projections}))
            mask_plan = MaskPlan(indices=indices, columns=tuple(sens.columns))
        else:
            return sql, Refusal("sensitive_columns", sens.reason, sens.suggestion or ""), None

    new_sql, applied = RT.apply_default_filters(sql, org, area=area, ctx=ctx)
    if applied:
        # default_filters only add WHERE conditions, never change the projection list, so the mask
        # plan's output indices stay valid against the rewritten SQL's result.
        sys.stderr.write(f"[agami] applied default_filters: {applied}\n")
        sql = new_sql
    return sql, None, mask_plan


# ---------------------------------------------------------------------------
# Executor seam (AH-012): one guarded envelope, a swappable connect-and-run step
# ---------------------------------------------------------------------------
#
# `execute_guarded` is the single execution chokepoint: guard -> resolve datasource ->
# executor.execute(vetted_sql) -> return native rows. The built-in executor (`BUILTIN_EXECUTOR`) is
# the default connect-per-query path, unchanged; a consumer injects its own `ports.Executor`
# (pooled / RBAC / tunnelled) *behind* the same guard — no fork of the guard, per REQ-002/REQ-014.
# The subprocess `main` and the in-process MCP handler both go through `execute_guarded`, so the
# guard is applied identically and can't be bypassed. The per-engine `_execute_<db>` CSV wrappers
# below are the subprocess/CLI adapter (they emit CSV + return an exit code); `_run_<db>` is the
# shared connect-and-run that returns native rows to either caller.


def _emit_or_err(run: Callable[[], ExecResult]) -> int:
    """Subprocess/CLI adapter over a ``_run_<db>`` function: write its result to stdout as CSV and
    return exit code 0, or translate an ``ExecutorError`` into the stderr message + exit code the CLI
    contract documents (byte-identical to what the old ``_execute_<db>`` emitted). A per-statement
    deadline (``_ResourceLimit``) surfaces as the shared ``resource_limit`` refusal on stderr + exit
    code 1 — the same JSON ``execute_guarded``/``main`` emit — and ``run()`` raises it BEFORE returning
    a result, so no partial CSV is written."""
    try:
        _emit_result_csv(run())
    except _ResourceLimit:
        json.dump({"refusal": _resource_limit_refusal().as_dict()}, sys.stderr)
        sys.stderr.write("\n")
        return 1
    except ExecutorError as e:
        return _err(e.msg, code=e.code)
    return 0


def _execute_postgres(creds: dict[str, str], sql: str) -> int:
    return _emit_or_err(lambda: _run_postgres(creds, sql))


def _execute_mysql(creds: dict[str, str], sql: str) -> int:
    return _emit_or_err(lambda: _run_mysql(creds, sql))


def _execute_snowflake(creds: dict[str, str], sql: str) -> int:
    return _emit_or_err(lambda: _run_snowflake(creds, sql))


def _execute_bigquery(creds: dict[str, str], sql: str) -> int:
    return _emit_or_err(lambda: _run_bigquery(creds, sql))


def _execute_sqlite(creds: dict[str, str], sql: str) -> int:
    return _emit_or_err(lambda: _run_sqlite(creds, sql))


def _execute_sqlserver(creds: dict[str, str], sql: str) -> int:
    return _emit_or_err(lambda: _run_sqlserver(creds, sql))


def _execute_oracle(creds: dict[str, str], sql: str) -> int:
    return _emit_or_err(lambda: _run_oracle(creds, sql))


def _execute_databricks(creds: dict[str, str], sql: str) -> int:
    return _emit_or_err(lambda: _run_databricks(creds, sql))


def _execute_trino(creds: dict[str, str], sql: str) -> int:
    return _emit_or_err(lambda: _run_trino(creds, sql))


def _execute_duckdb(creds: dict[str, str], sql: str) -> int:
    return _emit_or_err(lambda: _run_duckdb(creds, sql))


def _builtin_execute(vetted_sql: str, creds: dict[str, str], *, profile: str) -> ExecResult:
    """The built-in connect-and-run: dispatch on the datasource type and return native rows. Same
    per-engine behaviour as before (redshift/supabase ride the Postgres wire); only the row-emit
    moved to the caller. Raises ``ExecutorError`` on an unknown/missing type or a driver/connect/run
    failure. This is what ``BUILTIN_EXECUTOR.execute`` calls."""
    db_type = creds.get("type", "").lower()
    if not db_type:
        raise ExecutorError(f"Credentials profile [{profile}] is missing the 'type' field.", code=2)
    if db_type == "postgres":
        return _run_postgres(creds, vetted_sql)
    if db_type == "redshift":
        # Redshift speaks the Postgres wire protocol; psycopg2 connects fine. `_run_postgres` reads
        # host/port/etc. directly, so the type field doesn't matter — only sslmode defaulting does.
        if "sslmode" not in creds:
            creds = {**creds, "sslmode": "require"}
        return _run_postgres(creds, vetted_sql)
    if db_type == "mysql":
        return _run_mysql(creds, vetted_sql)
    if db_type == "sqlite":
        return _run_sqlite(creds, vetted_sql)
    if db_type == "snowflake":
        return _run_snowflake(creds, vetted_sql)
    if db_type == "bigquery":
        return _run_bigquery(creds, vetted_sql)
    if db_type in ("sqlserver", "mssql"):
        return _run_sqlserver(creds, vetted_sql)
    if db_type == "oracle":
        return _run_oracle(creds, vetted_sql)
    if db_type == "databricks":
        return _run_databricks(creds, vetted_sql)
    if db_type in ("trino", "presto"):
        return _run_trino(creds, vetted_sql)
    if db_type == "duckdb":
        return _run_duckdb(creds, vetted_sql)
    if db_type == "supabase":
        # Supabase is hosted Postgres.
        return _run_postgres(creds, vetted_sql)
    raise ExecutorError(
        f"Unsupported db type {db_type!r}. Supported: postgres, supabase, redshift, "
        f"mysql, sqlite, snowflake, bigquery, sqlserver, oracle, databricks, trino, duckdb.",
        code=2,
    )


class _BuiltinExecutor:
    """The default ``ports.Executor``: wraps the connect-per-query dispatch as an object so it
    satisfies the port by shape (method-style, like the other four ports). Stateless — one shared
    ``BUILTIN_EXECUTOR`` instance."""

    def execute(self, vetted_sql: str, creds: dict[str, str], *, profile: str) -> ExecResult:
        return _builtin_execute(vetted_sql, creds, profile=profile)


BUILTIN_EXECUTOR = _BuiltinExecutor()


def execute_guarded(
    sql: str,
    profile: str,
    area: str | None,
    *,
    executor: Executor,
    org_id: str | None = None,
    no_safety: bool = False,
) -> ExecResult:
    """The un-bypassable guarded envelope — the single execution chokepoint (REQ-002/REQ-014).

    In fixed order: read-only / dangerous-SQL guard -> recon / metadata deny-list (both hard security
    gates — NOT bypassable via ``no_safety``, which skips only the semantic-model pass, never
    write/RCE/DoS protection) -> semantic-model safety pass (fan/chasm pre-flight + scope + PII +
    ``default_filters`` rewrite) -> resolve the datasource -> ``executor.execute(vetted_sql, …)``. The
    executor only ever receives SQL every guard has passed. Raises ``GuardRefused`` carrying the typed
    ``Refusal`` on a refusal, and ``ExecutorError`` on a connect/run failure — so the subprocess
    ``main`` and the in-process MCP handler apply the same guards and build the same envelope. The row
    cap rides the request-scoped ``_max_rows_override`` ContextVar the caller sets.

    ACE-041: when the model-safety pass returns a ``MaskPlan`` (a *maskable* sensitive projection),
    the query runs and the named output columns are redacted here, at the single shared post-execute
    point — so masking covers every ``_run_<db>`` AND any injected ``ports.Executor``, and composes
    with the already-applied row cap (it redacts the capped rows in place)."""
    import sql_guard

    verdict = sql_guard.check_read_only(sql)
    if verdict is not None:
        raise GuardRefused(_refusal_from_verdict("permission", verdict), code=1)
    # Recon / metadata deny-list — refuse server-fingerprinting + system-catalog introspection
    # (version(), current_user, information_schema, pg_* relations) as a distinct `recon` refusal.
    # A hard gate, at the chokepoint so BOTH surfaces get it, and NOT bypassable via no_safety.
    recon = sql_guard.check_no_recon(sql)
    if recon is not None:
        raise GuardRefused(_refusal_from_verdict("recon", recon), code=1)
    mask_plan: MaskPlan | None = None
    if not no_safety:
        sql, refusal, mask_plan = _model_safety(sql, profile, area)
        if refusal is not None:
            raise GuardRefused(refusal, code=1)
    creds = _load_credentials(profile, org_id or "local")
    try:
        result = executor.execute(sql, creds, profile=profile)
        if mask_plan is not None:
            result = _apply_mask_plan(result, mask_plan)
        return result
    except _ResourceLimit:
        # The per-statement deadline fired inside the engine (client watchdog or native server
        # timeout). Map it to the shared refusal HERE — the one place both surfaces funnel through —
        # so the timeout is a `resource_limit` refused Envelope with no partial data, identically for
        # the subprocess wire and the in-process handler.
        raise GuardRefused(_resource_limit_refusal(), code=1) from None


def _mask_note(
    declared: tuple[str, ...], indices: tuple[int, ...], out_columns: list[str]
) -> tuple[str, ...]:
    """The ``applied[{mask}]`` receipt for a redaction — accurate about EVERY masked output position.

    ``declared`` are the model's sensitive "table.column" refs the gate named (``MaskPlan.columns``);
    ``indices`` are the redacted 0-based output positions; ``out_columns`` are the executor's actual
    output labels (``result.columns``), which for a set operation come from its FIRST arm. A single
    sensitive projection redacts exactly one position whose own label IS that sensitive column, so the
    note is just its declared ref (the spec's success criterion 1 — e.g. ``("customers.ssn",)``).

    A CROSS-POSITION set operation (e.g. ``SELECT ssn, name … UNION ALL SELECT name, ssn …``) redacts
    a SECOND output position whose first-arm label is a non-sensitive name (``name``) — necessary,
    because that merged output column carries a raw SSN in the second arm's rows. Naming that extra
    position by its output label keeps the receipt honest: it reflects every redacted position, not
    only the declared columns. A position is "already named by a declared sensitive column" iff its
    output label case-folds to the bare column name of one of the ``declared`` refs; otherwise its
    label is appended. This never DROPS a masked position (fail-safe for a receipt) and never renames
    the single-column case (that position's label equals its declared column's bare name)."""
    declared_bare = {d.rsplit(".", 1)[-1].lower() for d in declared}
    note: list[str] = list(declared)
    for i in sorted(indices):
        if 0 <= i < len(out_columns):
            label = out_columns[i]
            if label.lower() not in declared_bare and label not in note:
                note.append(label)
    return tuple(note)


def _apply_mask_plan(result: ExecResult, plan: MaskPlan) -> ExecResult:
    """Redact the sensitive OUTPUT columns of an executor result post-execution — the SINGLE shared
    masking point for EVERY executor (built-in ``_run_<db>`` and an injected ``ports.Executor``).
    Only the values at the plan's output indices become the token; every other value is untouched,
    and the redacted rows plus the masked column refs are returned in a NEW ``ExecResult`` (it is
    frozen). Runs on the already-row-capped rows, so it composes with the fetch bound rather than
    fighting it. An index outside a row's width simply doesn't match (fail-safe — the value isn't
    there to leak); by construction an output index equals its result-column position, so this holds.

    ``masked_columns`` is the ACCURATE receipt (``_mask_note``): the declared sensitive refs PLUS the
    output label of any extra positionally-aligned position a set operation forced us to redact, so
    the note reflects every masked output position, not only the declared columns (ACE-041 review)."""
    if not plan.indices:
        return result
    mask_set = set(plan.indices)
    redacted_rows = [
        tuple(plan.token if i in mask_set else v for i, v in enumerate(row)) for row in result.rows
    ]
    return ExecResult(
        columns=result.columns,
        rows=redacted_rows,
        truncated=result.truncated,
        masked_columns=_mask_note(plan.columns, plan.indices, result.columns),
    )


def main() -> int:
    # One-shot migration of a legacy <artifacts_dir>/local into <artifacts_dir>/local/, then re-resolve
    # the paths (the migration can set the artifacts-dir pointer to a custom location).
    global CREDENTIALS_PATH, CONFIG_PATH
    agami_paths.bootstrap()
    CREDENTIALS_PATH = agami_paths.credentials_path()
    CONFIG_PATH = agami_paths.config_path()
    p = argparse.ArgumentParser(
        description="Tier-3 Python SQL executor for agami. Reads credentials, runs SQL, emits CSV.",
    )
    p.add_argument(
        "--profile",
        default=None,
        help="Credentials profile to use. Defaults to AGAMI_PROFILE env, then .config.active_profile, then 'default'.",
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--sql", help="SQL statement (use --sql-file for SQL with special characters)")
    src.add_argument("--sql-file", help="Path to a file containing one SQL statement")
    p.add_argument(
        "--area",
        default=None,
        help="Subject area for the semantic-model safety pass (pre-flight + default_filters).",
    )
    p.add_argument(
        "--no-safety",
        action="store_true",
        help="Skip the semantic-model pre-flight / default_filters pass.",
    )
    p.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help="Lower the row cap for this call (never raises it). Effective cap = "
        "min(this, AGAMI_SQL_MAX_ROWS) — the env is the deployment cap, default 1000.",
    )
    args = p.parse_args()

    # Per-call cap (ACE-044); the sink reads it via _resolve_row_cap. No token/reset kept: main() is
    # the one-shot subprocess/CLI entry (one process, one thread), so there's no sibling request to
    # isolate from — unlike the in-process server path, which resets the token in tools._run_in_process.
    _max_rows_override.set(args.max_rows)

    if args.sql_file:
        sql = Path(os.path.expanduser(args.sql_file)).read_text()
    else:
        sql = args.sql

    profile = args.profile or _resolve_default_profile()

    # Route through the single guarded envelope with the built-in executor: guard -> recon deny-list
    # -> model-safety -> resolve -> connect-and-run, returning native rows we then serialize to stdout
    # as CSV (the subprocess wire). The guard is the hard security gate for EVERY caller (both MCP
    # servers, the agami-query skill, cron), NOT bypassable via --no-safety (which skips only the
    # semantic-model pass, never write/RCE/DoS protection).
    try:
        result = execute_guarded(
            sql, profile, args.area, executor=BUILTIN_EXECUTOR, no_safety=args.no_safety
        )
    except GuardRefused as refusal:
        # Emit the shared refusal as one JSON line to stderr — the subprocess wire the MCP tool relays
        # into a refused Envelope, and a direct CLI caller reads alongside the exit code.
        json.dump({"refusal": refusal.refusal.as_dict()}, sys.stderr)
        sys.stderr.write("\n")
        return refusal.code
    except ExecutorError as exc:
        sys.stderr.write(f"{exc.msg}\n")
        return exc.code
    _emit_result_csv(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
