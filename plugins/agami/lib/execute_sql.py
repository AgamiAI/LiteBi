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
    1  — refused by a guard. `main` always writes the contract `{"refusal": {…}}` as a single JSON
         object on stderr. The four unconverted semantic-model branches (fan/chasm pre-flight,
         auto-rewrite notice, sensitive columns, default-filter notice) additionally write their own
         `{"error": {…}}` / plain-text diagnostic line BEFORE it, so for those the stream is two
         lines rather than one. Parsers key off the `"refusal"` KEY, not the code and not the line
         count; the extra line goes away when those branches are subtracted.
    2  — usage / config error (missing credentials, bad profile, etc.)
    3  — driver missing for the configured db type
    4  — connection / authentication failed
    5  — SQL execution error (syntax, unknown column, etc.)
    6  — an unanticipated error inside the guarded path (`Failure.kind == "other"`). It has a code of
         its own precisely so it does not borrow one: with `other` falling to the generic 2, a parent
         reading the exit code back reported an internal break as a datasource-configuration problem
         (`dsn`), and only on the fork transport, while the identical break in-process said `other`.

`EXIT_TO_FAILURE_KIND` / `FAILURE_KIND_TO_EXIT` below are that table in code — this module owns the
exit-code contract because it documents it here and is the only place that produces it.
"""

from __future__ import annotations

import argparse
import configparser
import contextlib
import csv
import json
import logging
import os
import stat
import sys
import threading
import urllib.parse
import uuid
from collections.abc import Callable, Iterator
from contextvars import ContextVar, copy_context
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import agami_paths
from guardrail import (
    RULE_MODEL_SAFETY,
    RULE_MODEL_UNAVAILABLE,
    RULE_RESOURCE_LIMIT,
    Envelope,
    Failure,
    FailureKind,
    Refusal,
    Status,
    refuse,
)

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

_LOG = logging.getLogger(__name__)

# What a caller is told when something we did not anticipate broke. Deliberately generic and
# value-free: the raw text of an unanticipated exception is the one string in this module nobody has
# read, so it may carry a traceback, an absolute path, a DSN or a row value. It goes to the server
# log; the caller gets this. `other` is the failure kind, because an unclassified break is exactly
# what that member is for.
UNEXPECTED_FAILURE_MESSAGE = (
    "The statement could not be run because of an unexpected error on the server. "
    "The details are in the server log."
)


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


class ExecutorError(Exception):
    """A connect / credential / run failure raised by the built-in executor. Carries the exact
    stderr message and exit code the subprocess CLI emits, so ``main`` reproduces today's bytes and
    the in-process caller gets a catchable error instead of a process exit. Replaces the old
    ``return _err(...)`` returns inside the per-engine run functions."""

    def __init__(self, msg: str, *, code: int) -> None:
        super().__init__(msg)
        self.msg = msg
        self.code = code


# The CLI exit-code contract in code, in both directions — the module docstring's table, executable.
# Note what is NOT here: no exception type carries a refusal out of this module. A refusal is
# *returned* inside an ``Envelope``, never raised. Leaving a second transport in place next to the
# Envelope is how the fork path and the in-process path drifted into different answers in the first
# place, so there is exactly one way out.
EXIT_TO_FAILURE_KIND: dict[int, FailureKind] = {
    2: "dsn",             # usage / config error (missing credentials, bad profile)
    3: "driver_missing",  # no driver installed for the configured db type
    4: "auth",            # connect / authentication failed
    5: "syntax",          # SQL execution error
    # The catch-all needs a code of its OWN, not the generic 2. ``execute_guarded``'s catch-all
    # produces ``other`` for anything nobody anticipated (a malformed credentials file raising
    # ``configparser.MissingSectionHeaderError``, an adapter's own exception type), and while that
    # mapped to 2 the parent read it back as ``dsn`` — so an internal break was reported to the
    # caller as a datasource-configuration problem, and only on the fork transport.
    6: "other",
}

# The inverse, for ``main`` turning a ``Failure`` back into today's exit code. Every failure
# ``execute_guarded`` can produce is either an ``ExecutorError`` whose code is one of the four
# classified ones above or the catch-all ``other``, and all five have their own code — so the
# round-trip is exact for every case this module can reach, in both directions. The default covers
# only the kinds nothing produces yet (``column_not_found``, ``table_not_found``, ``network``) plus
# ``timeout``, which is minted at the tool edge by the subprocess supervisor and so never reaches
# ``main``. It is 6 rather than 2 for the same reason ``other`` has its own code: an unmapped kind is
# something we could not classify, which is what 6 says, and never a config error.
FAILURE_KIND_TO_EXIT: dict[str, int] = {kind: code for code, kind in EXIT_TO_FAILURE_KIND.items()}
_DEFAULT_FAILURE_EXIT = 6


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
_REDSHIFT_SCHEMES = {"redshift"}        # speaks Postgres wire protocol; port 5439, SSL required
_SNOWFLAKE_SCHEMES = {"snowflake"}      # native CLI (snowsql) + snowflake-connector-python
_BIGQUERY_SCHEMES = {"bigquery", "bq"}  # google-cloud-bigquery — auth via service-account JSON or ADC


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
        path = dsn[len("sqlite://"):]
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
    timeout_s = _resolve_timeout_s()
    # Bound outside the try so the `finally` can close it whether or not the declare got that far.
    cur = None
    try:
        # `with conn` is the TRANSACTION, not the connection: leaving it by an exception rolls back,
        # which is why it stays even though the cursor no longer lives inside a `with` of its own.
        with conn:
            # `SET LOCAL statement_timeout` is the native server-side backstop. It is
            # TRANSACTION-scoped, so it has to run on THIS transaction and before the named cursor
            # declares — a regular client-side cursor, because a named cursor may only ever run the
            # one query it was declared for. The libpq `options` startup parameter would be the
            # other way to set it and is deliberately not used: a transaction-mode connection pooler
            # can reject an unknown startup parameter, which would break the connect outright rather
            # than bound the statement.
            with conn.cursor() as bound_cur:
                bound_cur.execute(
                    "SET LOCAL statement_timeout = %s",
                    ((timeout_s + _NATIVE_BOUND_SKEW_S) * 1000,),  # the setting is in milliseconds
                )
            # A server-side (named) cursor so the row cap bounds TRANSFER, not just what we write:
            # psycopg2's default client-side cursor buffers the ENTIRE result before we can fetchmany,
            # so a runaway result would still be pulled whole. The named cursor streams from the
            # server in bounded batches. Read-only SELECTs (the only thing the guard admits)
            # are exactly what a server-side cursor supports.
            cur = conn.cursor(name="agami_bounded")
            cur.itersize = _resolve_row_cap() + 1  # server fetch batch = the bounded window
            # `connection.cancel()` is psycopg2's cancel: it opens a second connection and sends the
            # libpq cancel request, so it is safe to call from the watchdog thread while this one is
            # blocked in the driver.
            with _deadline(conn.cancel, timeout_s) as expired:
                try:
                    cur.execute(sql)
                    result = _collect_cursor(cur)
                except Exception:
                    if expired.is_set():
                        raise _ResourceLimit(_OUTLIVED_BUDGET)
                    raise
            if expired.is_set():
                raise _ResourceLimit(_OUTLIVED_BUDGET)
    except _ResourceLimit:
        raise
    except Exception as e:
        raise ExecutorError(f"Postgres execution error: {e}", code=5)
    finally:
        # The named cursor is closed HERE, by hand, rather than by a `with` around it. Closing one
        # sends `CLOSE agami_bounded` to the server, and when we are unwinding on the timeout marker
        # the transaction is already aborted, so that statement raises in turn — from inside
        # `__exit__`, where a raised exception REPLACES the one being propagated. The refusal would
        # reach the chokepoint as an ordinary driver error and be reported as a failure. Swallowing
        # it costs nothing: by this point the transaction has ended and the server-side portal is
        # gone with it, so the close is a courtesy rather than the thing that frees the resource.
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        conn.close()
    return result


def _run_mysql(creds: dict[str, str], sql: str) -> ExecResult:
    try:
        import pymysql  # type: ignore
    except ImportError:
        raise ExecutorError("pymysql not installed. Run: pip install pymysql", code=3)
    _require(creds, "host", "port", "user", "password", "database")
    try:
        conn = pymysql.connect(
            host=creds["host"],
            port=int(creds["port"]),
            user=creds["user"],
            password=creds["password"],
            database=creds["database"],
            charset="utf8mb4",
            connect_timeout=10,
            autocommit=True,
        )
    except Exception as e:
        raise ExecutorError(f"MySQL connect failed: {e}", code=4)
    timeout_s = _resolve_timeout_s()
    try:
        with conn.cursor() as cur:
            # pymysql's connection has NO `cancel()`. Its two cancel-shaped methods are `close()`,
            # which sends COM_QUIT down the very socket the blocked statement owns — and so can
            # itself block on a connection that is already stuck — and `_force_close()`, which
            # closes the socket outright. Only the second one actually unblocks a statement in
            # flight, so it is named here explicitly rather than reached for by shape.
            with _deadline(conn._force_close, timeout_s) as expired:
                try:
                    cur.execute(sql)
                    result = _collect_cursor(cur)
                except Exception:
                    if expired.is_set():
                        raise _ResourceLimit(_OUTLIVED_BUDGET)
                    raise
            if expired.is_set():
                raise _ResourceLimit(_OUTLIVED_BUDGET)
    except _ResourceLimit:
        raise
    except Exception as e:
        raise ExecutorError(f"MySQL execution error: {e}", code=5)
    finally:
        conn.close()
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
    # Resolved before the connect, because the native backstop is a SESSION parameter and so has to
    # be handed to the connect call itself.
    timeout_s = _resolve_timeout_s()
    conn_kwargs: dict[str, Any] = {
        "account": creds["account"],
        "user": creds["user"],
        "client_session_keep_alive": False,
        "login_timeout": 15,
        # Snowflake's native server-side bound, set behind our watchdog by the usual skew so the
        # watchdog wins and the flag stays the only classification. On a warehouse the backstop is
        # worth more than elsewhere: a statement nobody is waiting for still bills credits.
        "session_parameters": {
            "STATEMENT_TIMEOUT_IN_SECONDS": timeout_s + _NATIVE_BOUND_SKEW_S,
        },
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

        def cancel_snowflake_statement() -> None:
            """Ask Snowflake to abort the statement this cursor is running.

            Neither the connection nor the cursor has a `cancel()` — verified against the connector's
            own source, where the only public abort is `SnowflakeCursor.abort_query(qid)` and the
            connection's cancel helpers are private. The query id is what identifies the statement to
            abort, and it exists only once the statement has been submitted; before that there is
            nothing to abort and the session parameter above is what bounds the call.
            """
            qid = cur.sfqid
            if qid:
                cur.abort_query(qid)

        with _deadline(cancel_snowflake_statement, timeout_s) as expired:
            try:
                cur.execute(sql)
                result = _collect_cursor(cur)
            except Exception:
                if expired.is_set():
                    raise _ResourceLimit(_OUTLIVED_BUDGET)
                raise
        if expired.is_set():
            raise _ResourceLimit(_OUTLIVED_BUDGET)
    except _ResourceLimit:
        raise
    except Exception as e:
        raise ExecutorError(f"Snowflake execution error: {e}", code=5)
    finally:
        try:
            conn.close()
        except Exception:
            pass
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
            creds_obj = service_account.Credentials.from_service_account_file(
                sa_path_expanded
            )
            client_kwargs["credentials"] = creds_obj
        except Exception as e:
            raise ExecutorError(f"BigQuery credentials load failed: {e}", code=2)

    try:
        client = bigquery.Client(**client_kwargs)
    except Exception as e:
        raise ExecutorError(f"BigQuery client init failed: {e}", code=4)

    timeout_s = _resolve_timeout_s()
    # `job_timeout_ms` is BigQuery's native server-side bound, and on this engine it is the ONLY
    # bound: there is no watchdog cancel here, deliberately. BigQuery hands out no connection or
    # cursor to cancel, and the call that blocks is `job.result()`, which is reached only AFTER
    # `client.query()` has returned — so at the instant a watchdog would fire there is nothing in
    # hand to stop. Stated plainly, and accepted rather than papered over: on BigQuery a client-side
    # stall comes back as a `failed` envelope, not a `resource_limit` refusal. The query itself is
    # still stopped by the bound below, which is what keeps a runaway from scanning on unattended.
    # The usual skew is kept so this engine's number matches every other engine's.
    job_config_kwargs: dict[str, Any] = {
        "job_timeout_ms": (timeout_s + _NATIVE_BOUND_SKEW_S) * 1000,
    }
    # If `dataset` was set, prefix unqualified table references via the
    # default_dataset job config so the SQL can omit `<project>.<dataset>.`
    if creds.get("dataset"):
        try:
            job_config_kwargs["default_dataset"] = f"{project}.{creds['dataset']}"
        except Exception:
            pass

    cap = _resolve_row_cap()
    try:
        job_config = bigquery.QueryJobConfig(**job_config_kwargs)
        job = client.query(sql, job_config=job_config)
        # BigQuery has no DB-API cursor, so it can't funnel through `_collect_cursor`; apply the
        # same bounded-fetch cap here. `max_results=cap+1` bounds what the API returns (transfer),
        # and the (cap+1)th row flags truncation — the never-silent guarantee holds for BigQuery too.
        results = job.result(max_results=cap + 1)  # waits for completion; raises on error
    except _ResourceLimit:
        # Nothing in this engine raises the marker today, and this clause is still not optional: the
        # rule is that NO engine may relabel it as a driver error, and the clause below would. It is
        # what makes the rule checkable across all ten engines rather than nine.
        raise
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
    """Tier-3 path for SQLite using the stdlib `sqlite3` module.

    The first engine to run under the per-statement deadline. `sqlite3.Connection.interrupt()` is a
    genuine cancel — it stops the statement mid-scan from another thread — so this engine can prove
    the whole refusal contract in-process, with no network and no fixture warehouse.
    """
    import sqlite3  # always available in stdlib
    _require(creds, "path")
    path = os.path.expanduser(creds["path"])
    try:
        conn = sqlite3.connect(path)
    except Exception as e:
        raise ExecutorError(f"SQLite connect failed: {e}", code=4)
    # Resolved ONCE for the call, so the budget the watchdog enforces and the number the refusal
    # quotes cannot be two different values.
    timeout_s = _resolve_timeout_s()
    try:
        cur = conn.cursor()
        # The deadline covers the FETCH as well as the execute. `_collect_cursor` pulls `cap + 1`
        # rows in a single `fetchmany`, and on a cursor that streams its result that pull is where
        # the scan actually happens — a clock that stopped at `execute` would bound the cheap half
        # of the work and leave the expensive half unbounded.
        with _deadline(conn.interrupt, timeout_s) as expired:
            try:
                cur.execute(sql)
                result = _collect_cursor(cur)
            except Exception:
                # A cancelled statement raises `sqlite3.OperationalError("interrupted")`, whose text
                # is not a classification we would want to key on. The FLAG is the classification,
                # and it is the ONLY one: `_deadline` sets it before the cancel lands, so an error
                # raised while it is unset is the database's own — however late it arrives.
                if expired.is_set():
                    raise _ResourceLimit(_OUTLIVED_BUDGET)
                raise
        # Checked after the watchdog is disarmed, so the flag is final. A cancel can also land
        # between the two calls above, or just as the second returns, and leave nothing to raise:
        # the budget still elapsed, so the outcome is still a refusal rather than a result gathered
        # past it.
        if expired.is_set():
            raise _ResourceLimit(_OUTLIVED_BUDGET)
    except _ResourceLimit:
        # Ahead of the catch-all below on purpose: our own marker must reach `execute_guarded`
        # intact. Wrapped in an `ExecutorError` it would become a `failed`/`syntax` envelope, which
        # is the database's outcome rather than the bound we imposed.
        raise
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
            server=creds["host"], port=int(creds.get("port", 1433)),
            user=creds["user"], password=creds["password"],
            database=creds.get("database", ""), login_timeout=15,
        )
    except Exception as e:
        raise ExecutorError(f"SQL Server connect failed: {e}", code=4)
    timeout_s = _resolve_timeout_s()
    try:
        cur = conn.cursor()
        # pymssql's DB-API `Connection` has no `cancel()` — verified against pymssql 2.3, whose
        # connection exposes only close/commit/cursor/rollback/bulk_copy, and whose cursor exposes
        # none either. The cancel lives one layer down, on the `_mssql.MSSQLConnection` that
        # connection wraps, reachable only as the private `_conn`; that object's `cancel()` sends the
        # TDS attention packet, which is the thing that actually stops a statement in flight.
        with _deadline(conn._conn.cancel, timeout_s) as expired:
            try:
                cur.execute(sql)
                result = _collect_cursor(cur)
            except Exception:
                if expired.is_set():
                    raise _ResourceLimit(_OUTLIVED_BUDGET)
                raise
        if expired.is_set():
            raise _ResourceLimit(_OUTLIVED_BUDGET)
    except _ResourceLimit:
        raise
    except Exception as e:
        raise ExecutorError(f"SQL Server execution error: {e}", code=5)
    finally:
        try:
            conn.close()
        except Exception:
            pass
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
        dsn = oracledb.makedsn(creds["host"], int(creds.get("port", 1521)),
                               service_name=creds["service_name"])
    try:
        conn = oracledb.connect(user=creds["user"], password=creds["password"], dsn=dsn)
    except Exception as e:
        raise ExecutorError(f"Oracle connect failed: {e}", code=4)
    timeout_s = _resolve_timeout_s()
    try:
        cur = conn.cursor()
        # `oracledb.Connection.cancel()` breaks out of the call in progress on that connection —
        # verified against python-oracledb 3.x, where it is on the CONNECTION and not on the cursor.
        with _deadline(conn.cancel, timeout_s) as expired:
            try:
                cur.execute(sql)
                result = _collect_cursor(cur)
            except Exception:
                if expired.is_set():
                    raise _ResourceLimit(_OUTLIVED_BUDGET)
                raise
        if expired.is_set():
            raise _ResourceLimit(_OUTLIVED_BUDGET)
    except _ResourceLimit:
        raise
    except Exception as e:
        raise ExecutorError(f"Oracle execution error: {e}", code=5)
    finally:
        try:
            conn.close()
        except Exception:
            pass
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
            server_hostname=creds["host"], http_path=creds["http_path"],
            access_token=creds["token"],
        )
    except Exception as e:
        raise ExecutorError(f"Databricks connect failed: {e}", code=4)
    timeout_s = _resolve_timeout_s()
    try:
        cur = conn.cursor()
        # The cancel is on the CURSOR here, not the connection — verified against
        # databricks-sql-connector 4.x, whose `Cursor.cancel()` posts a cancel for the operation that
        # cursor is running while its connection has none.
        with _deadline(cur.cancel, timeout_s) as expired:
            try:
                cur.execute(sql)
                result = _collect_cursor(cur)
            except Exception:
                if expired.is_set():
                    raise _ResourceLimit(_OUTLIVED_BUDGET)
                raise
        if expired.is_set():
            raise _ResourceLimit(_OUTLIVED_BUDGET)
    except _ResourceLimit:
        raise
    except Exception as e:
        raise ExecutorError(f"Databricks execution error: {e}", code=5)
    finally:
        try:
            conn.close()
        except Exception:
            pass
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
            host=creds["host"], port=int(creds.get("port", 8080)), user=creds["user"],
            catalog=creds.get("catalog"), schema=creds.get("schema"),
            http_scheme="https" if creds.get("password") else "http", auth=auth,
        )
    except Exception as e:
        raise ExecutorError(f"Trino connect failed: {e}", code=4)
    timeout_s = _resolve_timeout_s()
    try:
        cur = conn.cursor()
        # Trino's cancel is on the CURSOR — verified against the trino client, whose
        # `Cursor.cancel()` sends the coordinator a DELETE for the query that cursor started.
        with _deadline(cur.cancel, timeout_s) as expired:
            try:
                cur.execute(sql)
                result = _collect_cursor(cur)
            except Exception:
                if expired.is_set():
                    raise _ResourceLimit(_OUTLIVED_BUDGET)
                raise
        if expired.is_set():
            raise _ResourceLimit(_OUTLIVED_BUDGET)
    except _ResourceLimit:
        raise
    except Exception as e:
        raise ExecutorError(f"Trino execution error: {e}", code=5)
    finally:
        try:
            conn.close()
        except Exception:
            pass
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
    timeout_s = _resolve_timeout_s()
    try:
        # `conn.interrupt()` is DuckDB's cancel, and it is on the connection — which is just as well,
        # because on this engine there is no cursor to reach for until the execute has returned:
        # `conn.execute` HANDS BACK THE CONNECTION ITSELF, so `cur` below IS `conn`. The deadline
        # therefore has to be armed around the execute as well as the fetch, and on an in-process
        # engine the execute is where the scan happens.
        with _deadline(conn.interrupt, timeout_s) as expired:
            try:
                cur = conn.execute(sql)
                result = _collect_cursor(cur)
            except Exception:
                if expired.is_set():
                    raise _ResourceLimit(_OUTLIVED_BUDGET)
                raise
        if expired.is_set():
            raise _ResourceLimit(_OUTLIVED_BUDGET)
    except _ResourceLimit:
        raise
    except Exception as e:
        raise ExecutorError(f"DuckDB execution error: {e}", code=5)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return result


_DEFAULT_MAX_ROWS = 1000  # rows materialized per result before truncation
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


_DEFAULT_TIMEOUT_S = 30  # wall-clock seconds one statement may run before the watchdog cancels it
# How far BEHIND our own watchdog a NATIVE server-side bound is set, on the three engines that have
# one. The skew is the whole point: our watchdog fires at the budget, the engine's own bound five
# seconds later, so the watchdog always wins the race and its Event stays the SOLE classification
# signal. Reverse the order and a server-side kill would beat us to the statement, the flag would be
# clear, and the refusal would come back as an ordinary database failure. The native bound is a
# backstop for the one case the watchdog cannot cover — our process dying with a statement in
# flight, which would otherwise leave the engine scanning for nobody.
_NATIVE_BOUND_SKEW_S = 5
# How far behind the watchdog the OUTER bound sits — the one `execute_guarded` puts around
# `executor.execute` itself, so an INJECTED executor is bounded too. The four bounds are one ordered
# family resolved from the same budget, innermost first:
#
#     watchdog (timeout_s) < native (+5) < outer (+10) < supervisor (+60)
#
# and the order is the contract. The inner watchdog must always win, because it is the only layer
# that can attribute the stop to the statement and hand the caller the precise refusal; every layer
# behind it is a backstop for a failure the layer in front of it cannot see. Collapse the order and
# a bound we imposed comes back as something else — a native server-side kill reads as an ordinary
# database error, and a supervisor kill reads as `failed`/`timeout` naming nothing the caller can
# act on. Both are strictly worse answers to the same event.
_OUTER_BOUND_SKEW_S = 10
# The OUTERMOST bound: how far behind the watchdog the subprocess supervisor in `tools` stops waiting
# for a forked child. It lives here, next to its three siblings, so the whole family is derived from
# one resolver and one place — the supervisor was a hardcoded 240s, which for any statement budget
# approaching it became the FIRST bound to fire and inverted the order. Its slack is large because it
# bounds a whole process (interpreter start, model load, credential resolution, connect) rather than
# a statement.
_SUPERVISOR_SKEW_S = 60
# Per-call timeout, the twin of `_max_rows_override` and request-scoped for the same reason: once the
# HTTP server runs execution in-process, concurrent handlers run in worker threads that each copy the
# context, so one request's budget can never stomp another's. In the subprocess/CLI (one process, one
# thread) it behaves exactly as a module global would.
_timeout_override: ContextVar[int | None] = ContextVar("_timeout_override", default=None)


def _resolve_timeout_s() -> int:
    """Effective per-statement timeout, in whole seconds. `AGAMI_SQL_TIMEOUT_S` is the
    operator-configurable DEPLOYMENT budget (default 30 when unset) — an operator owns their
    availability tradeoff and may set it higher OR lower than 30. A missing or non-positive value
    falls back to the default.

    Unlike `_resolve_row_cap`, a value that is PRESENT but unparseable is logged at warning before
    the fallback. An operator who wrote `45.5` or `30s` asked for something specific and silently
    running 30 instead is how a misconfiguration survives a whole deployment unnoticed. The warning
    goes to the module logger and never to stderr, because the subprocess transport parses stderr and
    an extra line there would break that contract."""
    raw = os.environ.get("AGAMI_SQL_TIMEOUT_S", "").strip()
    digits = raw[1:] if raw.startswith("-") else raw  # a leading minus is a value, not a typo
    if raw and not digits.isdigit():
        _LOG.warning(
            "AGAMI_SQL_TIMEOUT_S=%r is not a whole number of seconds; falling back to %ds.",
            raw,
            _DEFAULT_TIMEOUT_S,
        )
    timeout_s = int(raw) if digits.isdigit() else _DEFAULT_TIMEOUT_S
    if timeout_s <= 0:
        timeout_s = _DEFAULT_TIMEOUT_S  # "0" / "-5" → the default, never an instantly-expired budget
    override = _timeout_override.get()
    if override is not None and override > 0:
        timeout_s = override  # a caller that resolved its own budget outranks the deployment default
    return timeout_s


class _ResourceLimit(Exception):
    """Raised when our own watchdog fired, so a cancelled statement unwinds the engine function the
    same way any other failure does and the surrounding transaction rolls back. It is an internal
    marker only: it is always caught and translated inside this module and never crosses the tool
    boundary."""


class _OuterBoundExpired(_ResourceLimit):
    """Raised when the OUTER bound expired: the executor never returned and we stopped waiting.

    A subclass, so the one `except _ResourceLimit` handler in `execute_guarded` produces exactly one
    refusal for either layer and no second one can be minted. It stays distinguishable because the
    two events are not the same event: the watchdog CANCELLED a statement it holds a connection to,
    while this layer only abandoned its wait — so the sentence the caller reads differs, and saying
    "cancelled" here would be false.
    """


# The marker's message, single-sourced because every engine raises it. It is diagnostic text, not
# caller-facing: the refusal `execute_guarded` builds re-resolves the budget and writes its own
# detail, so nothing a caller reads comes from here.
_OUTLIVED_BUDGET = "the statement outlived its per-statement budget"
# The outer marker's message, and diagnostic in the same way — it names the executor rather than the
# statement, because at this layer the statement is not the thing we observed.
_OUTLIVED_OUTER_BOUND = "the executor outlived the outer bound around it"


@contextlib.contextmanager
def _deadline(cancel: Callable[[], None], timeout_s: float) -> Iterator[threading.Event]:
    """Arm a watchdog that calls `cancel` if the wrapped block outlives `timeout_s`, and yield the
    `threading.Event` that says whether it fired.

    The event is set BEFORE `cancel` runs. Order matters: whoever catches the driver error that the
    cancellation provokes must be able to read an already-set flag and attribute the failure to us
    rather than to the database. A `cancel` that raises is swallowed and logged, because some drivers
    raise when cancelled from a thread other than the one running the statement, and an exception
    escaping a timer thread is both unhandleable by the caller and invisible in the result."""
    fired = threading.Event()

    def fire() -> None:
        fired.set()
        try:
            cancel()
        except Exception as exc:
            _LOG.warning("Cancelling the statement after its timeout expired failed: %s", exc)

    timer = threading.Timer(timeout_s, fire)
    timer.daemon = True  # a hung cancel must never hold the interpreter open at shutdown
    timer.start()
    try:
        yield fired
    finally:
        timer.cancel()  # a block that finished on time disarms the watchdog before it can fire


def _flag_truncated(cap: int) -> None:
    """Signal a bounded-fetch truncation to the caller — a non-error `{"truncated": …}` marker on
    stderr (distinct from the guards' `{"error": …}`), so a truncated result is never mistaken for a
    complete one (ACE-044). Shared by every engine's materialization path. One write so the
    marker is always a single line, even if other notices surround it."""
    sys.stderr.write(json.dumps({"truncated": {"row_cap": cap}}) + "\n")


def _collect_cursor(cur: Any) -> ExecResult:
    """Fetch at most the row cap from a DB-API cursor into an ``ExecResult`` with **native types**.
    `fetchmany(cap + 1)` — never `fetchall` — so a huge result can't be buffered whole; a (cap+1)th
    row means the result was truncated. The SQL itself is untouched (no injected LIMIT). This is the
    single bounded-fetch implementation both the CSV wire (`_write_cursor_csv`) and the in-process
    executor path share, so the row cap is enforced once, identically, for every caller.

    Fetch FIRST, then read ``cur.description``: a psycopg2 **server-side (named) cursor** — which the
    Postgres/Redshift path uses to bound transfer — reports ``description is None`` until the
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


def _emit_result_csv(result: ExecResult) -> None:
    """Serialize an ``ExecResult`` to stdout as CSV — the subprocess/CLI wire. Byte-for-byte what the
    old inline cursor→CSV writer produced: header row then data rows, and a truncation marker on
    stderr when capped. This is the *single, final* text serialization for the fork path; the
    in-process path skips it and returns the native rows straight to the tool edge."""
    if not result.columns:  # cursor had no description → wrote nothing (e.g. a non-row statement)
        return
    writer = csv.writer(sys.stdout)
    writer.writerow(result.columns)
    for row in result.rows:
        writer.writerow(row)
    if result.truncated:
        _flag_truncated(_resolve_row_cap())


def _write_cursor_csv(cur: Any) -> None:
    """Collect the bounded result and write it to stdout as CSV — the per-engine sink the subprocess
    path uses. Kept as the thin composition ``_emit_result_csv(_collect_cursor(cur))`` so the fetch
    bound and the CSV shape stay single-sourced (and the existing bounded-fetch tests still pin it)."""
    _emit_result_csv(_collect_cursor(cur))


def _hosted() -> bool:
    """The served (hosted) path is signalled by a configured database — the same signal
    `tools._load_org` / `Store.from_env` use. On it, a missing model is a safety failure (fail
    closed); locally (no DB) a not-yet-built model legitimately means 'no model yet'."""
    return bool(os.environ.get("AGAMI_DB_URL") or os.environ.get("APP_DATABASE_URL"))


def _resolve_guard_model(profile: str):
    """Resolve the semantic model for the safety pass, mirroring `tools._load_org` (ACE-051): from
    the DB when one is configured (hosted — the `/artifacts` disk mount may be absent), else the
    on-disk YAML (local). Returns a `Datasource` or None if neither is available.

    The DB import is lazy AND env-guarded on purpose: the local executor runs from a stdlib-lean
    mirror that does not ship `store`/`model_store`, so we only reach for them when a DB is set.
    Any DB-load failure degrades to disk rather than crashing the executor."""
    from semantic_model import loader as L

    # Any load failure below degrades to the next source (DB → disk → None), silently: a freeform
    # error line here would (a) leak DB connection details from the exception and (b) precede the
    # JSON refusal `_model_safety` emits when both sources are absent on hosted, breaking the
    # single-JSON-object contract callers parse. The observable signal is the fail-closed refusal
    # itself, not a diagnostic line.
    if _hosted():
        try:
            from model_store import load_datasource as _load_db
            from store import Store
            from tools import _current_org_id

            store = Store.from_env()
            if store is not None:
                try:
                    # Scoped to the REQUEST's org, exactly as `tools._load_org` does. Letting this
                    # default to 'local' looked right while `model_deploy._default_org()` was also
                    # `AGAMI_ORG_ID or "local"`, but F14/F15 moved the write side onto the deployment's
                    # resolved id — so rows land under that id and a 'local' read finds nothing. On a
                    # multi-tenant server it finds nothing for EVERY tenant, and the fail-closed rule
                    # below then refuses every query. `_default_org`'s own docstring states the contract
                    # this restores: the write and read org "MUST agree".
                    org = _load_db(store, profile, org_id=_current_org_id())
                finally:
                    store.close()
                if org is not None:
                    return org
        except Exception:
            pass  # DB unreachable/misconfigured -> fall through to disk

    root = Path(os.environ.get("AGAMI_ARTIFACTS_DIR") or (Path.home() / "agami-artifacts")) / profile
    if (root / "datasource.yaml").exists():
        try:
            return L.load_datasource(root)
        except Exception:
            pass  # unparseable/absent on disk -> None (hosted then fails closed)
    return None


def _write_refusal(refusal: Refusal) -> None:
    """Write a guard refusal to stderr in the ONE shape every caller parses — the single wire-writer,
    called only from ``main``.

    One JSON object, on one line: ``{"refusal": {reason, rule, detail, remediation}}`` — the wire
    shape S2 established, which ``tools._stderr_refusal`` rebuilds through ``Refusal`` on the parent
    side of the process boundary. Nothing else may be written alongside it: several callers (and the
    fail-closed suite) parse the WHOLE stderr stream as a single object, so a second line of
    diagnostics would not merely be noisy, it would make the refusal unreadable. The four
    unconverted ``_model_safety`` branches still write such a line before this one — for those the
    stream is two lines and only the line-scanning parser can read it, which is one more reason they
    are being subtracted.
    """
    json.dump({"refusal": asdict(refusal)}, sys.stderr)
    sys.stderr.write("\n")


def _model_safety(sql: str, profile: str, area: str | None) -> tuple[str, Refusal | int | None]:
    """Semantic-model safety pass before execution: fan-trap / chasm-trap pre-flight
    + default_filters auto-application, over a model resolved from the DB (hosted) or disk (local).

    Returns ``(sql_to_run, verdict)``. ``verdict`` is ``None`` to continue, a ``Refusal`` from one of
    the five converted branches, or — from the four unconverted ones — today's bare exit code, which
    the caller wraps in the interim ``model_safety`` rule. Inert (returns the SQL unchanged) when the
    model package isn't importable, or — on the LOCAL path only — when there is no model yet. On the
    HOSTED path a model that can't be resolved fails closed (refuses), never runs unguarded (ACE-051).

    The five converted branches — both ``model_unavailable`` sites and the three scope gates — write
    NOTHING: they hand the contract object back and ``execute_guarded`` puts it in the Envelope, so
    the in-process caller sees the same rule the forked one does. The fan/chasm pre-flight,
    sensitive-column and default-filter branches below still write today's ``{"error": …}`` / plain
    text and return today's int, because those become receipt facts rather than refusals and
    converting them here would pre-empt that decision.
    """
    try:
        from semantic_model import runtime as RT
    except Exception:
        # The model package (pydantic) isn't importable, so the guards can't run at all. On the
        # hosted served path that is the same "can't guarantee safety" condition as a missing model
        # — fail closed. Locally it stays a no-op (a bare install legitimately has no model). (The
        # sqlglot-unavailable / unparseable-SQL degrade-to-allow is a distinct fail-open owned by
        # ACE-037, not closed here.)
        if _hosted():
            # `remediation` is authored here rather than carried across: this branch had no
            # `suggestion` at all, and the contract makes an unactionable refusal a construction
            # error. It names an operator action and no DSN, path or hostname — the same
            # value-free rule the detail already follows, and what the single-clean-JSON test pins.
            return sql, refuse(
                RULE_MODEL_UNAVAILABLE,
                detail="semantic-model package not importable; refusing to run unguarded on the "
                       "hosted server",
                remediation="Install the semantic-model dependencies on the server and retry.",
            )
        # The non-hosted twin: a bare local install legitimately has no model package, so this is a
        # silent no-op rather than a refusal. Not a branch to convert — there is nothing to refuse.
        return sql, None  # local: model package not available -> no-op

    org = _resolve_guard_model(profile)
    if org is None:
        if _hosted():
            # Fail closed: a served query with no resolvable model must be refused, never run with
            # the fan/chasm/scope/PII guards silently off. `remediation` is authored here for the
            # same reason as the branch above, and is likewise free of any resolved path or DSN —
            # "checked DB and disk" names the two SOURCES, never where either one lives.
            return sql, refuse(
                RULE_MODEL_UNAVAILABLE,
                detail="no semantic model could be resolved (checked DB and disk); refusing to run "
                       "unguarded on the hosted server",
                remediation="Build or deploy a semantic model for this datasource, then retry.",
            )
        # The non-hosted twin again: locally a not-yet-built model is expected, so no refusal.
        return sql, None  # local: no model yet -> no-op (unchanged)

    # Build the shared guard context ONCE — parse the SQL + build each model index a single
    # time — and thread it through the battery below, instead of every guard re-parsing and
    # rebuilding its index (audit P2 / ACE-045). Behaviour-preserving: a guard given `ctx`
    # returns the same verdict as one that builds its own.
    ctx = RT.build_guard_context(sql, org)

    # Table-scope guard — a query may only reference tables the semantic model
    # declares; any other table in the connected database is refused. Runs FIRST
    # so the fan/chasm and sensitive checks below only evaluate in-scope tables.
    # Each gate returns the contract object itself and this layer only hands it back — there is no
    # second place where a refusal's reason, rule or wording could be chosen, and no serialization
    # between the gate and the Envelope for the two paths to disagree about.
    ts = RT.check_table_scope(sql, org, ctx=ctx)
    if ts is not None:
        return sql, ts

    # SELECT * ban — force every projected column to be named, so the column-scope
    # guard below can check what is actually returned (and nothing hides behind *).
    star = RT.check_no_select_star(sql, ctx=ctx)
    if star is not None:
        return sql, star

    # Column-scope guard — a column that binds to a declared table must be one that
    # table declares (a hallucinated column, or a physical column the model excluded).
    cs = RT.check_column_scope(sql, org, ctx=ctx)
    if cs is not None:
        return sql, cs

    pf = RT.pre_flight_check(sql, org, ctx=ctx)
    if pf.risk and pf.action == "refuse":
        json.dump({"error": {"kind": "preflight_refused", "risk": pf.risk,
                             "reason": pf.reason, "suggestion": pf.suggestion,
                             "triggering_joins": pf.triggering_joins}}, sys.stderr)
        sys.stderr.write("\n")
        return sql, 1
    if pf.risk and pf.action == "auto_rewrite" and pf.rewritten_sql:
        sys.stderr.write(f"[agami] auto-corrected {pf.risk}: ran rewritten SQL. {pf.reason}\n")
        sql = pf.rewritten_sql
        ctx = RT.build_guard_context(sql, org)  # SQL changed -> refresh the shared context

    # Sensitive-column (PII) guard — refuse to PROJECT raw sensitive values. Same
    # deterministic chokepoint as the fan/chasm pre-flight, so the agami-query skill,
    # the local MCP server, and cron all protect PII identically (not just whichever
    # path happened to read a prose rule). Aggregates / filters / joins are allowed.
    sens = RT.check_sensitive_projection(sql, org, ctx=ctx)
    if sens.action == "refuse":
        json.dump({"error": {"kind": "sensitive_columns", "columns": sens.columns,
                             "reason": sens.reason, "suggestion": sens.suggestion}}, sys.stderr)
        sys.stderr.write("\n")
        return sql, 1

    new_sql, applied = RT.apply_default_filters(sql, org, area=area, ctx=ctx)
    if applied:
        sys.stderr.write(f"[agami] applied default_filters: {applied}\n")
        sql = new_sql
    return sql, None


# ---------------------------------------------------------------------------
# Executor seam (AH-012): one guarded envelope, a swappable connect-and-run step
# ---------------------------------------------------------------------------
#
# `execute_guarded` is the single execution chokepoint: guard -> resolve datasource ->
# executor.execute(vetted_sql) -> return ONE Envelope. The built-in executor (`BUILTIN_EXECUTOR`) is
# the default connect-per-query path, unchanged; a consumer injects its own `ports.Executor`
# (pooled / RBAC / tunnelled) *behind* the same guard — no fork of the guard, per REQ-002/REQ-014.
# The subprocess `main` and the in-process MCP handler both go through `execute_guarded`, so the
# guard is applied identically and can't be bypassed. The per-engine `_execute_<db>` CSV wrappers
# below are the subprocess/CLI adapter (they emit CSV + return an exit code); `_run_<db>` is the
# shared connect-and-run that returns native rows to either caller.


def _emit_or_err(run: Callable[[], ExecResult]) -> int:
    """Subprocess/CLI adapter over a ``_run_<db>`` function: write its result to stdout as CSV and
    return exit code 0, or translate an ``ExecutorError`` into the stderr message + exit code the CLI
    contract documents (byte-identical to what the old ``_execute_<db>`` emitted)."""
    try:
        _emit_result_csv(run())
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


def _envelope(
    status: Status,
    *,
    data: ExecResult | None = None,
    refusal: Refusal | None = None,
    failure: Failure | None = None,
) -> Envelope:
    """The ONE place ``execute_guarded`` constructs an ``Envelope``, and the one place this module
    mints an ``audit_id``.

    Funnelling all five outcomes through here is what makes "every path returns exactly one
    Envelope" a property of the code rather than a claim in a docstring: a new outcome cannot reach
    a caller without passing the contract's own present-iff check in ``Envelope.__post_init__``.

    ``uuid4().hex`` keeps the vendored plugin slice stdlib-only. When the caller is the in-process
    tool edge, this id is the one it reports. When the caller is ``main`` in a forked child, the id
    goes nowhere: this module writes no audit row, and it keeps the id OFF the wire — the
    refusal/failure JSON stays exactly the shape the parent's parser expects, with no ``audit_id``
    key — so the parent mints the one that gets recorded rather than there being two ids for one
    query. A directly-invoked ``python -m execute_sql`` therefore carries an id nothing records,
    which is correct: nothing audits that path today.
    """
    return Envelope(
        status=status,
        data=data,
        refusal=refusal,
        failure=failure,
        audit_id=uuid.uuid4().hex,
    )


def _execute_bounded(
    executor: Executor, sql: str, creds: dict[str, str], *, profile: str
) -> ExecResult:
    """Run ``executor.execute`` under the OUTER bound and return what it returned.

    This is the layer that makes "no executor escapes the limit" true. The inner deadline lives
    inside the BUILT-IN executor's engine functions, so before this an INJECTED executor — the
    hosted connection-reuse path — ran with no per-statement bound at all, and even on the built-in
    path BigQuery has no watchdog (it has no connection object to cancel), so a client-side stall
    there was unbounded too. Both are bounded here, and deliberately by the same layer: applying
    this only to non-built-in executors would leave the BigQuery hole open while looking closed.

    **The mechanism is a worker thread, because this layer holds nothing it can cancel.** An
    arbitrary ``Executor`` exposes one method and no connection, so the only thing this code owns is
    its own WAIT — it starts the call on a daemon thread and joins with the budget.

    **The cost is a leaked worker, and it is real.** On expiry the thread is still inside the
    driver call, and it stays there: nothing here cancels it, and it may hold a database connection
    (and, on the server side, a running statement) until that call returns on its own. It is a
    daemon so it cannot hold the interpreter open at exit, and that is the whole of the mitigation.
    This is a bound on how long a CALLER waits, not a promise that the work stopped — which is
    exactly why the inner watchdog, the layer that really can cancel, is set to fire first.

    Exceptions cross the thread boundary with their ORIGINAL type, re-raised here. That is what
    keeps the handlers in ``execute_guarded`` correct: ``_ResourceLimit`` still reaches the refusal
    branch and ``ExecutorError`` still reaches the classified one, rather than every executor
    failure arriving as a worker that finished having produced nothing. ``BaseException`` is caught
    rather than ``Exception`` for the same reason: a driver's deep ``sys.exit`` raises ``SystemExit``,
    which ``tools._run_in_process`` nets one layer up, and swallowing it in a worker thread would
    turn that fail-closed answer into a silent hang until the bound expired.

    The call runs inside a copy of the CALLER's context. A new thread starts with an empty one, so
    without this the request-scoped ``_timeout_override`` / ``_max_rows_override`` would read as
    unset inside the worker and every in-process call would silently fall back to the deployment
    defaults.
    """
    timeout_s = _resolve_timeout_s()
    outcome: dict[str, Any] = {}
    ctx = copy_context()

    def call() -> None:
        try:
            outcome["result"] = ctx.run(executor.execute, sql, creds, profile=profile)
        except BaseException as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=call, name="agami-bounded-execute", daemon=True)
    worker.start()
    worker.join(timeout_s + _OUTER_BOUND_SKEW_S)
    if worker.is_alive():
        raise _OuterBoundExpired(_OUTLIVED_OUTER_BOUND)
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]


def execute_guarded(
    sql: str,
    profile: str,
    area: str | None,
    *,
    executor: Executor,
    org_id: str | None = None,
    no_safety: bool = False,
) -> Envelope:
    """The un-bypassable guarded envelope — the single execution chokepoint (REQ-002/REQ-014).

    In fixed order: read-only / dangerous-SQL guard (the hard security gate — NOT bypassable via
    ``no_safety``, which skips only the semantic-model pass, never write/RCE/DoS protection) ->
    semantic-model safety pass (fan/chasm pre-flight + scope + PII + ``default_filters`` rewrite) ->
    resolve the datasource -> ``executor.execute(vetted_sql, …)``. The executor only ever receives
    SQL both guards have passed.

    **Every path returns exactly one ``Envelope``, and this function is TOTAL** — nothing is raised
    out of here for a caller to interpret, so the subprocess ``main`` and the in-process MCP handler
    cannot disagree about what happened. The seven outcomes:

      * the read-only gate refuses            -> ``refused`` carrying that gate's ``Refusal``
      * ``_model_safety`` returns a Refusal   -> ``refused`` carrying it verbatim
      * ``_model_safety`` returns an int      -> ``refused`` carrying the interim ``model_safety``
      * either time bound fired               -> ``refused`` carrying ``resource_limit``
      * ``executor.execute`` raises           -> ``failed`` carrying a classified ``Failure``
      * anything else raises                  -> ``failed``/``other``, generic message, raw to the log
      * the statement ran                     -> ``ok`` carrying the ``ExecResult``

    The whole body is inside the try, and the catch-all is what makes "exactly one Envelope" a
    property rather than a claim. It was neither before: ``_model_safety`` sat outside the try
    entirely, ``_load_credentials`` was inside it but only ``ExecutorError`` was caught (a malformed
    or duplicate-section credentials file raises ``configparser.Error``), an injected executor
    raising anything else escaped, and an executor returning ``None`` made ``_envelope("ok", …)``
    raise out of ``Envelope.__post_init__``. Each of those propagated past the chokepoint to
    ``tools._run_in_process``, which catches only ``SystemExit`` — so the caller got an exception
    instead of an Envelope AND no audit row was written, because the row is written by the
    serializer the exception skipped. That matters most for the hosted path: ``ports.Executor``
    exists so a consumer can inject a pooled / per-user-RBAC / SSH-tunnel executor, and a pooled
    executor raising its own ``PoolError`` must not be able to leave the chokepoint silently.

    ``SystemExit`` and ``KeyboardInterrupt`` are not ``Exception`` and deliberately still escape: a
    process being torn down is not a query outcome to report.

    ``_load_credentials`` sits INSIDE the try deliberately, so a bad profile / missing DSN becomes a
    ``failed``/``dsn`` Envelope carrying its detailed message rather than escaping as an exception
    the two callers would each have to translate. The row cap rides the request-scoped
    ``_max_rows_override`` ContextVar the caller sets."""
    try:
        import sql_guard

        refusal = sql_guard.check_read_only(sql)
        if refusal is not None:
            return _envelope("refused", refusal=refusal)
        if not no_safety:
            sql, verdict = _model_safety(sql, profile, area)
            if isinstance(verdict, Refusal):
                return _envelope("refused", refusal=verdict)
            if verdict is not None:
                # One of the four unconverted branches: it wrote its own diagnostic to the server
                # log and handed back only an exit code, so this is the most we can say without
                # inventing a rule for it. The interim RULE_MODEL_SAFETY exists precisely so this
                # path still returns an Envelope — without it the signature would have to be
                # `Envelope | int` and the "exactly one Envelope per path" property would be
                # literally false. Both this constant and this branch go away when those branches
                # are subtracted.
                return _envelope("refused", refusal=refuse(
                    RULE_MODEL_SAFETY,
                    detail="the semantic-model safety pass refused this statement",
                    remediation="Check the server log for the rule that fired, then adjust the "
                                "query.",
                ))
        creds = _load_credentials(profile, org_id or "local")
        # Bounded at the CHOKEPOINT, so the limit reaches every executor rather than only the
        # built-in one whose engines carry the inner watchdog. See `_execute_bounded` for the
        # mechanism and for the leaked worker it costs on expiry.
        result = _execute_bounded(executor, sql, creds, profile=profile)
        # Inside the try on purpose: an executor that returns `None` (or anything else the contract
        # does not accept) fails the present-iff check in `Envelope.__post_init__`, and that is a
        # broken adapter, not a reason for the chokepoint to raise at its caller.
        return _envelope("ok", data=result)
    except _ResourceLimit as exc:
        # AHEAD of both handlers below: `_ResourceLimit` is an `ExecutorError` sibling, not a
        # subclass, but the catch-all would swallow it and report a bound WE imposed as an
        # unclassified server break. This is the per-statement bound the contract reserves
        # `resource_limit` for — its subject IS the statement, so "narrow it and run it again" is a
        # fix we can honestly name, unlike the supervisor's kill of a child that never returned.
        #
        # ONE handler for both bounds, and so exactly one refusal per call however many layers were
        # armed. When the inner watchdog fired, its marker is what arrives here — the outer layer's
        # join returned long before its own budget and has nothing to add, so the inner refusal is
        # the answer and cannot be overwritten by a later one.
        #
        # The budget is re-resolved rather than carried on the marker: the marker stays a plain
        # exception, and nothing between the engine call and here can change the env var or the
        # request-scoped ContextVar the resolver reads, so it is the same number the watchdog used.
        timeout_s = _resolve_timeout_s()
        # The configured number belongs in the detail — it is a deployment setting, not a data
        # value, and a bound the caller cannot see is one it cannot plan around.
        if isinstance(exc, _OuterBoundExpired):
            # The outer layer holds no connection, so it stopped WAITING rather than stopping the
            # statement. "Cancelled" would be a claim we cannot make: the worker is still inside the
            # driver call and the statement may still be running. The caller is told what we
            # actually know, and the number quoted is the bound that actually elapsed.
            detail = (
                f"The executor did not return within the {timeout_s + _OUTER_BOUND_SKEW_S}s limit "
                "and the query was abandoned."
            )
        else:
            detail = f"The statement ran longer than the {timeout_s}s limit and was cancelled."
        return _envelope("refused", refusal=refuse(
            RULE_RESOURCE_LIMIT,
            detail=detail,
            # The remediation names only what would make THIS statement executable: on the served
            # path the caller is an assistant with no shell and no deployment, so naming the
            # environment variable would be advice it cannot take, addressed to someone who is not
            # reading.
            remediation="Narrow the time range, reduce the grouping, or add a selective filter, "
                        "then run it again.",
        ))
    except ExecutorError as exc:
        # The classified branch: `msg` is authored by this module (a missing driver, a connect
        # failure, the credential-resolution remediation naming DATASOURCE_URL) or relayed from the
        # driver, and both callers already surface it. Sanitizing DRIVER text is a separate, larger
        # job (ACE-039) and is deliberately not attempted here.
        return _envelope("failed", failure=Failure(
            kind=EXIT_TO_FAILURE_KIND.get(exc.code, "other"), message=exc.msg,
        ))
    except Exception:
        # Unanticipated, so unreadable: nobody has vetted what this exception's text contains, and
        # `configparser.MissingSectionHeaderError` alone carries the absolute path of the
        # credentials file. The raw text and its stack go to the server log; the caller gets the
        # generic message. Logged at ERROR rather than WARNING because reaching here means a bug or
        # a broken adapter, not a user mistake.
        _LOG.error("unhandled error in the guarded execution path", exc_info=True)
        return _envelope("failed", failure=Failure(
            kind="other", message=UNEXPECTED_FAILURE_MESSAGE,
        ))


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
    p.add_argument("--area", default=None,
                   help="Subject area for the semantic-model safety pass (pre-flight + default_filters).")
    p.add_argument("--no-safety", action="store_true",
                   help="Skip the semantic-model pre-flight / default_filters pass.")
    p.add_argument("--max-rows", type=int, default=None,
                   help="Lower the row cap for this call (never raises it). Effective cap = "
                        "min(this, AGAMI_SQL_MAX_ROWS) — the env is the deployment cap, default 1000.")
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

    # Route through the single guarded envelope with the built-in executor: guard -> model-safety ->
    # resolve -> connect-and-run, returning ONE Envelope the switch below renders to the subprocess
    # wire. Same guard, same verdicts, same connect-per-query behaviour as before — the split just
    # makes the connect-and-run step swappable in-process (AH-012). The guard is the hard security
    # gate for EVERY caller (both MCP servers, the agami-query skill, cron), NOT bypassable via
    # --no-safety (which skips only the semantic-model pass, never write/RCE/DoS protection).
    env = execute_guarded(
        sql, profile, args.area, executor=BUILTIN_EXECUTOR, no_safety=args.no_safety
    )

    # The single print site: a three-way switch on the ONE Envelope `execute_guarded` returned.
    # Nothing else in this module writes to stdout or decides an exit code, so the CLI contract
    # lives in one readable place instead of being spread across every `except` in the call graph.
    if env.status == "refused":
        # One JSON object, on one line, and nothing else — `tools._stderr_refusal` and the
        # fail-closed suite parse this whole stream.
        _write_refusal(env.refusal)
        return 1
    if env.status == "failed":
        # Deliberately NOT JSON: the bare message + the classified exit code are today's documented
        # CLI contract, which the agami-query skill's error classifier and `semantic_model.cli`
        # already read. Restructuring it would buy nothing in this slice.
        sys.stderr.write(f"{env.failure.message}\n")
        return FAILURE_KIND_TO_EXIT.get(env.failure.kind, _DEFAULT_FAILURE_EXIT)
    _emit_result_csv(env.data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
