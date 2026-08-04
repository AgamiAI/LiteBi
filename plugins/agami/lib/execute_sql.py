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
    1  — refused by a guard. `main` writes the contract `{"refusal": {…}}` as a single JSON object
         on stderr, and nothing else — every branch is converted now. Four once wrote their own
         `{"error": {…}}` or plain-text diagnostic line BEFORE it, which made the stream two lines
         and readable only by a line-scanning parser: the default-filter and auto-rewrite notices
         went with the rewrites they announced, and the fan/chasm and sensitive-column
         diagnostics went with the refusals they explained. Parsers should still key off the
         `"refusal"` KEY rather than the code or the line count.
    2  — usage / config error (missing credentials, bad profile, etc.)
    3  — driver missing for the configured db type
    4  — connection / authentication failed
    5  — SQL execution error (syntax, unknown column, etc.)
    6  — an unanticipated error inside the guarded path (`Failure.kind == "other"`). It has a code of
         its own precisely so it does not borrow one: with `other` falling to the generic 2, a parent
         reading the exit code back reported an internal break as a datasource-configuration problem
         (`dsn`), and only on the fork transport, while the identical break in-process said `other`.
    7  — the statement referenced a column the database does not have
    8  — the statement referenced a table the database does not have
    9  — the connection's role lacks SELECT on a referenced object
    10 — the database was unreachable mid-statement (connection refused / reset)

Codes 7-10 exist for the same reason 6 does. The child classifies a driver error at the chokepoint
and then `main` collapses that classification to an exit code, so a kind without a code of its own is
a kind the fork silently loses: all four landed on the `other` default and a parent read them back as
`other`, while the identical error in-process reported the real kind. The in-process tests passed and
the DEFAULT surface was wrong (ACE-039).

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
    PRE_MODEL_RULES,
    RECEIPT_BEFORE_MODEL,
    RECEIPT_BUILD_FAILED,
    RECEIPT_NO_MODEL,
    RECEIPT_NO_RUNTIME,
    RULE_AUDIT_UNAVAILABLE,
    RULE_ENGINE_MISMATCH,
    RULE_MODEL_UNAVAILABLE,
    RULE_RESOURCE_LIMIT,
    Envelope,
    Failure,
    FailureKind,
    Receipt,
    Refusal,
    Status,
    receipt_from_assembled,
    refuse,
    undetermined_receipt,
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

# A logger of its OWN for raw driver text, and the reason is a leak rather than tidiness (ACE-039).
# This module never calls `basicConfig`, so in a forked child a record on `_LOG` falls through to
# `logging.lastResort` and is written to STDERR — and `tools._child_failure_message` relays the
# child's whole stderr into `failure.message` for any classified exit code. Logging the raw text on
# `_LOG` would therefore hand it straight back to the caller on the DEFAULT surface, undoing the
# sanitization in the same breath. (The catch-all beneath the classifier escapes this only because
# its `exc_info=True` trips the traceback guard, at the cost of degrading its message.)
#
# `main` silences this logger at CLI entry, so the forked child emits nothing here and the column
# stays NULL. In-process and hosted, it propagates to the server's root logger as intended.
_RAW_LOG = logging.getLogger(__name__ + ".raw")

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
    with "".

    ``truncated`` mirrors the ``fetchmany(cap + 1)`` bound: True when a (cap+1)th row existed. It is
    an INTERNAL carrier, not a caller-visible fact — ``execute_guarded`` reads it and refuses, so a
    True never reaches an ``ok`` envelope and the rows beside it are discarded. Every executor must
    set it, including an injected one: it is the only channel this contract has for saying "there
    was more", and a consumer that leaves it False on a bounded read presents a partial result as a
    whole one.

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
    # The four kinds the error-hardening slice makes reachable (ACE-039). Each needs a code of its
    # own for exactly the reason 6 does, and the failure mode is worse because it is silent: the
    # child classifies the driver error correctly at the chokepoint, `main` collapses the kind to an
    # exit code, and a kind with no code falls to the `other` default. In-process the caller saw
    # `column_not_found`; through the fork — the DEFAULT transport — it saw `other`, and no test that
    # exercised only the in-process path could tell. `_child_failure_message` compounds it: it
    # relays the child's sanitized text only for a code in this table, so an unmapped code also
    # replaced the message with the generic unexpected-failure text.
    7: "column_not_found",
    8: "table_not_found",
    9: "permission",  # the ROLE lacks SELECT; distinct from `auth`, where the credentials failed
    10: "network",
}

# The inverse, for ``main`` turning a ``Failure`` back into today's exit code. Every failure
# ``execute_guarded`` can produce is either an ``ExecutorError`` whose code is one of the classified
# ones above or the catch-all ``other``, and all of them have their own code — so the round-trip is
# exact for every case this module can reach, in both directions.
#
# The default now covers exactly ONE kind: ``timeout``, which is minted at the tool edge by the
# subprocess supervisor when a child never returns, and so never reaches ``main`` to be encoded.
# That is the whole remaining gap, and it is a gap by construction rather than by omission — a
# supervisor that stops an unresponsive child cannot attribute the kill to the statement, which is
# why it is a failure rather than a ``resource_limit`` refusal (guardrail contract §3).
#
# It is 6 rather than 2 for the same reason ``other`` has its own code: an unmapped kind is something
# we could not classify, which is what 6 says, and never a config error.
FAILURE_KIND_TO_EXIT: dict[str, int] = {kind: code for code, kind in EXIT_TO_FAILURE_KIND.items()}
_DEFAULT_FAILURE_EXIT = 6

# --- Error sanitization (ACE-039) -------------------------------------------
#
# A raw driver error is an enumeration channel. PostgreSQL's `HINT: Perhaps you meant to reference
# the column "orders.internal_ref"` names a DECLARED column the caller never sent, which is the
# model leaking through the operational channel rather than the refusal channel. Measured before
# this landed, by a `strict=True` xfail in tests/test_ace035_no_enumeration.py.
#
# WHOSE TEXT IS IT. Codes 2 and 3 are authored by this module — the credential remediation naming
# DATASOURCE_URL, the chmod-600 fix, the `pip install` lines — and are relayed deliberately, because
# they tell an operator what to do and contain nothing the database said. Codes 4 and 5 are the
# twenty f-string sites that interpolate the driver's own exception. The module's docstring table
# already draws exactly this line, so the discriminator is documented rather than invented.
_AUTHORED_EXIT_CODES = frozenset({2, 3})

# One value-free sentence per kind. NO REMEDIATION: `Failure` has no such field, and that absence is
# what lets a caller tell a decision of ours from the database's outcome. Where the reference
# implementation's remediation carried real information it is folded into the message, which is
# already value-free.
#
# There is deliberately no `timeout` entry. A per-statement deadline is a `resource_limit` REFUSAL
# (ACE-038 owns it, classified from a watchdog flag rather than a driver string), and the only
# `timeout` failure is the subprocess supervisor's kill at the tool edge, which never reaches here.
_ERROR_MESSAGES: dict[str, str] = {
    "syntax": "The generated SQL was not valid for this database. Re-run the query.",
    "column_not_found": (
        "The statement referenced a column this database does not have. If the model was built "
        "against an older schema, re-introspect the datasource."
    ),
    "table_not_found": (
        "The statement referenced a table this database does not have. If the model was built "
        "against an older schema, re-introspect the datasource."
    ),
    "permission": (
        "The database refused to read an object this statement referenced. The connection's role "
        "needs SELECT on it."
    ),
    "auth": "The database rejected the connection's credentials.",
    "network": "The database was unreachable.",
    "dsn": "The datasource host or path could not be resolved.",
    "driver_missing": "The database driver is not installed on the server.",
}


def _classify_db_error(text: str, code: int) -> FailureKind:
    """Classify a driver error into a `FailureKind`, from text that is never relayed.

    Classifying FROM driver text is not the same as returning it: the output is one of a fixed set
    of labels, and the caller receives `_ERROR_MESSAGES[kind]` rather than anything the database
    said. The raw text is captured server-side only.

    Order matters and two positions are load-bearing:

    * **Cancellation is checked early and lands on `other`.** Every cancellation signature was ceded
      to ACE-038, whose rule is that a deadline is classified from a signal rather than a string.
      But simply DELETING the arm does not leave these statements unclassified — it leaves them
      mis-classified: "canceling statement due to statement timeout" contains "timed out" and would
      fall to `network`, or, failing that, to the exit-5 prior and out as `syntax`. An
      unattributable server-side cancellation is honestly `other`.
    * **`table_not_found` precedes `syntax`**, because Snowflake prefixes an unknown object with
      "SQL compilation error" and would otherwise be read as a syntax error.

    `"timed out"` is deliberately NOT a `network` needle. A driver-level connect or login timeout is
    what the executor already reports as `auth` (exit 4) and stays there; adding the needle would
    silently move it and contradict the contract's account of what `timeout` means.
    """
    lowered = (text or "").lower()

    def has(*needles: str) -> bool:
        return any(needle in lowered for needle in needles)

    if code == 3 or has("no module named", "modulenotfounderror", "command not found"):
        return "driver_missing"
    # Ten engines spell an authorization failure ten ways, and getting this wrong is not cosmetic:
    # a `permission` failure tells the operator to GRANT, while `auth` tells them to re-credential
    # and `syntax` makes the skill auto-retry the identical statement twice.
    if has(
        "permission denied",
        "insufficient_privileges",
        "insufficient privileges",  # Oracle ORA-01031, a space rather than an underscore
        "command denied",
        "insufficient_access_or_readonly",
        "permission was denied",  # SQL Server
        "access denied for user",  # MySQL 1044/1045 — narrower than the bare needle in `auth`
    ) or ("access denied" in lowered and has("table", "dataset", "cannot select")):
        # BigQuery and Trino both open with "Access Denied:", which the `auth` arm's bare
        # "access denied" would otherwise swallow into a credentials problem.
        return "permission"
    if has(
        "canceling statement",
        "statement_timeout",
        "query was canceled",
        "querycanceled",
        # MySQL 2013, whose actual text is "Lost connection to MySQL server DURING QUERY". The
        # reference's needle read "lost connection during query" and so never matched it. Kept
        # specific on purpose: a bare "lost connection" also covers "…at 'reading initial
        # communication packet'", which is a genuine network failure and not a cancellation.
        "lost connection to mysql server during query",
    ):
        return "other"
    if has(
        "no such column",
        "unknown column",
        "undefinedcolumn",
        "invalid identifier",
        "invalid_field",
        "invalid column name",  # SQL Server 207
        "cannot be resolved",  # Trino, Databricks (UNRESOLVED_COLUMN)
        "unrecognized name",  # BigQuery
        "unresolved_column",
    ) or ("column" in lowered and has("does not exist", "not found")):
        return "column_not_found"
    if has("no such table", "undefinedtable", "invalid object name") or (
        has("relation", "table", "object") and has("does not exist", "doesn't exist", "not found")
    ):
        return "table_not_found"
    if has("syntax error", "syntaxerror", "compilation error", "error in your sql syntax"):
        return "syntax"
    if has(
        "could not translate host",
        "name or service not known",
        "getaddrinfo",
        "unknown mysql server host",
        "can't connect",
        "no such file or directory",
    ):
        return "dsn"
    if has("connection refused", "connection reset", "could not connect", "wrong_version_number"):
        return "network"
    if has(
        "password authentication failed",
        "no pg_hba",
        "incorrect username or password",
        # NOT a bare "access denied": BigQuery, Trino, SQL Server and MySQL 1044 all use that
        # wording for an AUTHORIZATION failure on an object, which the arm above now claims.
        "authentication failed",
        "login failed",
    ):
        return "auth"
    return EXIT_TO_FAILURE_KIND.get(code, "other")


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
        # And the connection close is guarded for the same reason, one line down: an exception raised
        # inside a `finally` REPLACES the one propagating through it, so an unguarded close here would
        # destroy the marker the `except _ResourceLimit: raise` above just re-raised — and the caller
        # would read a bound we imposed as an unclassified server break.
        try:
            conn.close()
        except Exception:
            pass
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
        # Guarded because an exception raised inside a `finally` REPLACES the one propagating through
        # it, which would destroy the marker re-raised just above. MySQL is the most exposed of the
        # ten: its cancel is `_force_close()`, which deliberately destroys the socket that this
        # `close()` then tries to write COM_QUIT to — so on exactly the timeout path this is the close
        # most likely to raise.
        try:
            conn.close()
        except Exception:
            pass
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
        except Exception:
            # The ONE code-2 site that interpolated a third-party exception. google-auth's text can
            # carry the absolute path of the service-account key, and code 2 is the band this module
            # relays verbatim on the strength of having authored it — so an interpolated exception
            # here made "codes 2 and 3 are ours" true only approximately (ACE-039). Authored prose
            # out, the original to the server log.
            _LOG.error("BigQuery service-account credentials failed to load", exc_info=True)
            raise ExecutorError(
                "BigQuery credentials could not be loaded. Check `service_account_path` for this "
                "profile in <artifacts_dir>/local/credentials — the file must exist and be a valid "
                "service-account key.",
                code=2,
            )

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
        # Guarded like every other engine's: an exception raised inside a `finally` REPLACES the one
        # propagating through it, so an unguarded close would silently convert the refusal above into
        # an unclassified failure.
        try:
            conn.close()
        except Exception:
            pass
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


_DEFAULT_MAX_ROWS = 1000  # rows a result may hold before the transfer bound refuses it

# The raw driver text of the failure this call classified, for the audit row only (ACE-039). A
# ContextVar for the same reason as the cap above: request-scoped, so concurrent in-process handlers
# cannot read each other's. `execute_guarded` CLEARS it on entry, which is the load-bearing half —
# without that, a call that succeeds after one that failed would attribute the earlier call's error
# to itself, and the audit row would name a statement that never produced it.
#
# It carries the text out-of-band rather than on the Envelope on purpose: `Failure` is `{kind,
# message}` and stays that way, because a raw field on the contract is a raw field somebody
# eventually serializes.
_last_error_detail: ContextVar[str | None] = ContextVar("_last_error_detail", default=None)

# The classified outcome of THIS call, for the tool-call recorder (ACE-098). `tools.record_tool_call`
# derived `success` / `error_kind` by `json.loads`-ing the serialized body and reading
# `refusal["rule"]` out of it — so the tool_calls row's account of WHY a call failed depended on our
# own wire shape holding still, which is exactly what principle 7's re-derivation bar cannot rest on.
#
# It has to travel out-of-band because the tool handler returns a `str`: by the time the transport
# runs its `finally`, the Envelope is gone and a serialized body is all there is. Same mechanism and
# same reasoning as `_last_error_detail` directly above, including the clear-on-entry — a verdict
# left behind by an earlier call must never label this one — and the same reason it is a ContextVar
# rather than a module global: the in-process path runs tool handlers on worker threads.
#
# **Read by the TRANSPORT, out of a context it owns.** That is not a detail — a ContextVar set
# inside `async_offload.run_blocking`'s worker thread is invisible to the caller and to every other
# worker, because anyio hands the thread a COPY of the context. Verified rather than assumed: a var
# set in one `run_blocking` reads back `None` both in the request task and in a second
# `run_blocking`. So `mcp_http` runs the handler inside its own `contextvars.copy_context()` and
# reads the result out of that object afterwards. A plain "set here, read there" would silently
# record nothing on the one surface that records tool calls at all.
#
# `(status, rule, row_count)`, not the whole Envelope: the transport needs to say whether the call
# succeeded, which gate stopped it, and how many rows came back. Putting a contract object in here
# invites somebody to serialize it from the transport instead of from the one place that owns the
# wire shape.
_last_outcome: ContextVar[tuple[str, str | None, int | None] | None] = ContextVar(
    "_last_outcome", default=None
)

# The semantic model `_model_safety` resolved for THIS call, so the receipt describes the model the
# gates actually consulted rather than one re-resolved a moment later — and so the hosted path loads
# it ONCE per query rather than twice (`_resolve_guard_model` is a full DB or disk load and is not
# cached at this layer). A ContextVar for the same reason as the two above: the in-process path runs
# tool handlers on worker threads, so a module global would race. `execute_guarded` clears it on
# entry, which is the load-bearing half — a model left behind by an earlier call must never describe
# this one.
#
# `_model_safety` cannot simply return it. Its `(sql, verdict)` two-tuple is unpacked by
# `execute_guarded` and by the enumeration sentinel, and widening the contract of the safety pass to
# carry a value only the receipt wants is a worse trade than this carrier.
_guard_model: ContextVar[Any | None] = ContextVar("_guard_model", default=None)

# The shape `_model_safety` read off THIS call's parsed statement — "aggregate", "listing", or None
# when there was no tree to read. Carried for exactly one consumer: the result-bound refusal, which
# must not tell an aggregate caller to add a `LIMIT` (ACE-087).
#
# It is a carrier for the same reason `_guard_model` is, plus one this module cannot get around: the
# classification needs sqlglot and this module ships in the stdlib-only vendored mirror, so the
# verdict site literally cannot compute it. `semantic_model.runtime.statement_shape` does it where
# the tree already is, and the answer travels as a plain string.
#
# None is a real, reachable answer rather than an error, and by more routes than the obvious one:
# the vendored mirror has no `runtime` to import, `no_safety=True` skips the pass entirely, and a
# local install returns from `_model_safety` before the classify line when the model package is
# absent or no model has been built yet. All of them get the shape-neutral remediation, which is why
# that third text is not a defensive branch — it is the answer for every deployment without a model.
_guard_shape: ContextVar[str | None] = ContextVar("_guard_shape", default=None)


def _resolve_row_cap() -> int:
    """Effective result-row cap. `AGAMI_SQL_MAX_ROWS` is the operator-configurable DEPLOYMENT cap
    (default 1000 when unset) — an operator owns their availability tradeoff and may set it higher OR
    lower than 1000; it is NOT a hard 1000 ceiling. A missing/invalid/zero env value falls back to
    1000.

    The operator is the only voice here. A per-call override used to be able to lower it, and it went
    with the trim (ACE-087): the one thing a caller might know better than the deployment — that it
    wants MORE rows — is the thing a lowering-only override structurally could not express, and a
    caller that wants 200 rows says so in the statement, where the intent is legible to everything
    downstream."""
    raw = os.environ.get("AGAMI_SQL_MAX_ROWS", "").strip()
    cap = int(raw) if raw.isdigit() else _DEFAULT_MAX_ROWS
    if cap <= 0:
        cap = _DEFAULT_MAX_ROWS  # "0" / "00" → the default, never an empty result
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
# How many worker threads `_execute_bounded` may have given up on at once, process-wide. That layer
# holds nothing it can cancel, so every expiry abandons a thread which still occupies a pooled
# connection and, server-side, a running statement. On the built-in path the inner watchdog fires ten
# seconds earlier, so reaching the outer bound means a cancel already failed — rare, and an honest
# cost. On an INJECTED executor there is no inner watchdog at all, so every slow statement abandons
# one, and because the caller's own thread is freed at the bound the abandonments accumulate with no
# ceiling of their own: the anyio worker limiter used to supply that ceiling by blocking the caller,
# and bounding the wait removed it. The cap restores one explicitly. 8 is deliberately far below both
# that former limiter and a typical warehouse connection pool, so a saturated executor loses a
# minority of the pool to work nobody is waiting for rather than taking the datasource away from the
# whole organization.
_MAX_ABANDONED_WORKERS = 8
_abandoned_lock = threading.Lock()
_abandoned_workers = 0


def _resolve_timeout_s() -> int:
    """Effective per-statement timeout, in whole seconds. `AGAMI_SQL_TIMEOUT_S` is the
    operator-configurable DEPLOYMENT budget (default 30 when unset) — an operator owns their
    availability tradeoff and may set it higher OR lower than 30. A missing or non-positive value
    falls back to the default.

    **The environment is the ONLY source, deliberately.** A request-scoped override would outrank it
    in the parent and be invisible to a forked child, which re-resolves from `os.environ` alone — so
    the supervisor bound the parent derives could sit BELOW the budget the child actually enforces
    and fire first, inverting the ordered family the whole design rests on. One source, readable on
    both sides of the fork, makes that inversion unrepresentable rather than merely unlikely.

    Unlike `_resolve_row_cap`, a value that is PRESENT and does not survive to become the budget is
    logged at warning before the fallback. That covers `45.5` and `30s`, which cannot be read at all,
    and equally `-5` and `0`, which can be read and are then declined: an operator who wrote either
    asked for something specific, and a deployment quietly running 30 instead is exactly the
    invisible degradation the warning exists against. The warning goes to the module logger and never
    to stderr, because the subprocess transport parses stderr and an extra line there would break
    that contract."""
    raw = os.environ.get("AGAMI_SQL_TIMEOUT_S", "").strip()
    digits = raw[1:] if raw.startswith("-") else raw  # a leading minus is a value, not a typo
    # `isdecimal`, not `isdigit`: the latter admits `²` and `①`, which `int()` then refuses — turning
    # a misconfigured deployment into a ValueError raised out of this resolver, at a call site (the
    # fork path's supervisor bound) that sits outside any handler.
    written = int(raw) if digits.isdecimal() else None
    timeout_s = written if written is not None and written > 0 else _DEFAULT_TIMEOUT_S
    if raw and timeout_s != written:
        _LOG.warning(
            "AGAMI_SQL_TIMEOUT_S=%r is not a usable whole number of seconds; falling back to %ds.",
            raw,
            _DEFAULT_TIMEOUT_S,
        )
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


class _ExecutorSaturated(_ResourceLimit):
    """Raised INSTEAD of starting a statement, when the abandoned-worker cap is already reached.

    A sibling of `_OuterBoundExpired` under the same parent for the same reason — one handler, one
    refusal — and distinguishable for the same reason too: this statement did not run long, it did
    not run at all. What we observed is the executor, not the statement, so the sentence the caller
    reads must not blame the query it just sent.
    """


# The marker's message, single-sourced because every engine raises it. It is diagnostic text, not
# caller-facing: the refusal `execute_guarded` builds re-resolves the budget and writes its own
# detail, so nothing a caller reads comes from here.
_OUTLIVED_BUDGET = "the statement outlived its per-statement budget"
# The outer marker's message, and diagnostic in the same way — it names the executor rather than the
# statement, because at this layer the statement is not the thing we observed.
_OUTLIVED_OUTER_BOUND = "the executor outlived the outer bound around it"
# The saturation marker's message, diagnostic in the same way again.
_EXECUTOR_SATURATED = "the executor already has its limit of abandoned calls outstanding"

# The remediation names only what would make THIS statement executable: on the served path the caller
# is an assistant with no shell and no deployment, so naming the environment variable would be advice
# it cannot take, addressed to someone who is not reading.
_NARROW_IT = ("Narrow the time range, reduce the grouping, or add a selective filter, "
              "then run it again.")

# The transfer bound's remediation, keyed by the shape `_guard_shape` carries. Three entries rather
# than one, because the right fix genuinely differs and the wrong one is not merely unhelpful:
#
#   * A LISTING can be bounded. `LIMIT` without `ORDER BY` still returns an arbitrary subset — the
#     engine's emission order is not a promise — so the ORDER BY is what turns "some rows" into "the
#     rows you asked for", and it is named first for that reason.
#   * An AGGREGATE cannot. `LIMIT` on a grouped result silently drops groups, and a partial breakdown
#     reads exactly like a complete one: no row is wrong, the total is. That is a worse outcome than
#     the refusal the caller just got. The REMEDIATION does not warn against `LIMIT`; it does not
#     mention it at all. The reader here is usually an LLM, negation is what an LLM follows least
#     reliably, and "do not add a LIMIT" puts the token in front of it either way. Absence is the
#     stronger control, and it is also what makes the acceptance check a plain one.
#
#     Scoped to the remediation deliberately: the shared `detail` says "the {N}-row limit", and that
#     stays. It is prose naming the ceiling, not an instruction to write a clause — the same thing
#     ACE-038's timeout detail does with its seconds — and the remediation is the field a caller acts
#     on. Stripping the word from the detail too would cost the caller the one number they need.
#   * None means nothing parsed the statement — the vendored mirror has no `runtime` to call, and
#     `no_safety=True` skips the pass that sets the shape. Guessing "listing" here would hand an
#     aggregate caller the one instruction that corrupts their answer, so this text hands the fork
#     back to the caller, who does know which they wrote, and makes `LIMIT` conditional on it.
# The keys are exactly `runtime.statement_shape`'s return domain, and the lookup below is direct
# rather than defaulted on purpose: a fifth shape added there without a text here should fail loudly
# in the first test that runs it, not quietly serve the neutral wording and let a shape ship with no
# advice of its own. Adding a shape means adding an entry.
_RESULT_BOUND_REMEDIATION = {
    "listing": "Bound the result with LIMIT, and add an ORDER BY so the rows you get back are the "
               "ones you meant to ask for.",
    "aggregate": "Narrow the grouping, or add a selective filter, so the whole breakdown fits within "
                 "the row cap, then run it again.",
    None: "Add a selective filter to narrow the result. If it is a plain row listing rather than an "
          "aggregate, you can instead bound it with LIMIT and an ORDER BY.",
}


def _resource_limit_refusal(exc: _ResourceLimit | None) -> Refusal:
    """The ONE `resource_limit` refusal, built from whichever of the four bounds stopped the call.

    Single-sourced because two entries into the engines lead here — the guarded chokepoint and the
    subprocess/CLI adapter — so neither can drift into wording the other does not have.

    `exc is None` is the fourth bound, and the only one that is not a time bound: the result-transfer
    ceiling (ACE-087). It arrives with no marker because there is nothing to raise — overflow is a
    flag on a result the executor already returned — so the absence of an exception IS the signal.

    What this deliberately no longer preserves is indistinguishability. The three time bounds still
    read alike, but a transfer refusal names rows where a timeout names seconds, and its remediation
    is shaped by the statement. That is the point of it: "add a LIMIT" and "narrow the grouping" are
    different fixes, and a caller who gets the wrong one is worse off than one who gets neither. The
    invariant that survives is one rule with one emit site, not one sentence.

    The budget is re-resolved rather than carried: nothing between the engine call and here can
    change the environment the resolvers read, so both `_resolve_timeout_s` and `_resolve_row_cap`
    return the same number the bound itself used. The configured number belongs in the detail — it
    is a deployment setting, not a data value, and a bound the caller cannot see is one it cannot
    plan around.
    """
    if exc is None:
        # The transfer bound. No rows come back with this: an unordered prefix of a larger result is
        # an arbitrary sample, and returning it under a `status=ok` is a wrong answer wearing a right
        # one's clothes.
        detail = (
            f"The result exceeded the {_resolve_row_cap()}-row limit, so it was not returned."
        )
        return refuse(RULE_RESOURCE_LIMIT, detail=detail,
                      remediation=_RESULT_BOUND_REMEDIATION[_guard_shape.get()])
    timeout_s = _resolve_timeout_s()
    if isinstance(exc, _ExecutorSaturated):
        # Nothing ran, so nothing about THIS statement is the finding. Saying "your query was too
        # slow" here would be false and would send the caller off simplifying a statement that is
        # very possibly fine.
        detail = (
            "The executor is saturated: too many earlier calls have not returned, so this "
            "statement was not started."
        )
        remediation = "Wait for the calls already in flight to finish, then run it again."
    elif isinstance(exc, _OuterBoundExpired):
        # The outer layer holds no connection, so it stopped WAITING rather than stopping the
        # statement. "Cancelled" would be a claim we cannot make: the worker is still inside the
        # driver call and the statement may still be running. The caller is told what we actually
        # know, and the number quoted is the bound that actually elapsed.
        detail = (
            f"The executor did not return within the {timeout_s + _OUTER_BOUND_SKEW_S}s limit "
            "and the query was abandoned."
        )
        remediation = _NARROW_IT
    else:
        detail = f"The statement ran longer than the {timeout_s}s limit and was cancelled."
        remediation = _NARROW_IT
    return refuse(RULE_RESOURCE_LIMIT, detail=detail, remediation=remediation)


@contextlib.contextmanager
def _deadline(cancel: Callable[[], None], timeout_s: float) -> Iterator[threading.Event]:
    """Arm a watchdog that calls `cancel` if the wrapped block outlives `timeout_s`, and yield the
    `threading.Event` that says whether it fired.

    The event is set BEFORE `cancel` runs. Order matters: whoever catches the driver error that the
    cancellation provokes must be able to read an already-set flag and attribute the failure to us
    rather than to the database. A `cancel` that raises is swallowed and logged, because some drivers
    raise when cancelled from a thread other than the one running the statement, and an exception
    escaping a timer thread is both unhandleable by the caller and invisible in the result.

    **The disarm is a JOIN, not a request.** `threading.Timer.cancel()` only sets the timer's internal
    `finished` Event, so a `fire` that already passed its own check runs to completion regardless —
    setting the flag and delivering a cancel AFTER this block has returned. Both consequences are
    real: every engine re-reads the flag once the block has exited and would read a stale `False`, and
    a cancel arriving late lands on a connection the engine has moved on from, which on a pooled one
    is by then someone else's statement. The lock plus the flag close that: a `fire` that loses the
    race neither sets the Event nor calls `cancel`, and a `fire` that wins holds the lock across the
    cancel, so the disarm waits for it. The wait is bounded by the layer outside this one — a cancel
    that hangs is what the outer bound around the whole executor call exists to survive."""
    fired = threading.Event()
    lock = threading.Lock()
    # Assigned only below, in the enclosing scope, and only read inside `fire` — so the closure sees
    # the current value with no `nonlocal` and no mutable box.
    disarmed = False

    def fire() -> None:
        with lock:
            if disarmed:
                return  # the block already returned; there is nothing of ours left to stop
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
        # Claim the race under the lock FIRST, then ask the timer to stand down. `Timer.cancel()` is
        # not a join, so doing it the other way round leaves a window between this block returning
        # and the flag being set, in which a `fire` that has already expired can still take the lock
        # and mark a statement that actually completed. Taking the lock first closes that window: a
        # `fire` already holding the lock is waited for, and every other `fire` observes `disarmed`.
        with lock:
            disarmed = True
        timer.cancel()  # belt and braces, for a timer that has not started running yet


def _collect_cursor(cur: Any) -> ExecResult:
    """Fetch at most the row cap from a DB-API cursor into an ``ExecResult`` with **native types**.
    `fetchmany(cap + 1)` — never `fetchall` — so a huge result can't be buffered whole; a (cap+1)th
    row means there was more, and `execute_guarded` turns that into a refusal (ACE-087). The rows
    below the cap are still returned here rather than dropped on the spot: this function reports what
    it found and the chokepoint decides, which is what keeps the decision in one place. The SQL
    itself is untouched (no injected LIMIT). Every DB-API engine shares this one implementation, so
    the bound is applied once, identically.

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
    """Serialize an ``ExecResult`` to stdout as CSV — the subprocess/CLI wire: header row then data
    rows. This is the *single, final* text serialization for the fork path; the in-process path
    skips it and returns the native rows straight to the tool edge.

    Nothing is written to stderr any more. A `{"truncated": …}` marker used to ride there beside the
    rows; a result that overflows never reaches this function now, because ``main`` branches to
    ``_write_refusal`` before it (ACE-087). That also restores a property ``_write_refusal``'s
    docstring asserts and the marker quietly broke: stderr carries one JSON object or nothing."""
    if not result.columns:  # cursor had no description → wrote nothing (e.g. a non-row statement)
        return
    writer = csv.writer(sys.stdout)
    writer.writerow(result.columns)
    for row in result.rows:
        writer.writerow(row)


def _hosted() -> bool:
    """The served (hosted) path is signalled by a configured database — the same signal
    `tools._load_org` / `Store.from_env` use. On it, a missing model is a safety failure (fail
    closed); locally (no DB) a not-yet-built model legitimately means 'no model yet'."""
    return bool(os.environ.get("AGAMI_DB_URL") or os.environ.get("APP_DATABASE_URL"))


def _audit_store_reachable() -> bool:
    """Whether the audit store can be opened right now — the pre-execution half of principle 7
    (ACE-097).

    Recording happens at the tool edge, AFTER the statement ran: `tools._record_execution` is called
    from `tools._emit`, the serializer. So closing the swallows there can only turn a lost record
    into a raised exception on a statement that already reached the customer's database. Refusing
    *instead of* executing needs the question asked before execution, and this is that question.

    `Store.from_env()` is the probe rather than a DSN parse because it is not a parse:
    `Store.connect` calls `psycopg2.connect` / `sqlite3.connect` eagerly, so constructing one either
    reaches the database or raises. It is also the identical call `_record_query` will make, which
    is what makes the answer relevant rather than merely adjacent — a probe that opens a different
    connection than the writer would is a probe of something else.

    **Costs one connect per served call.** Deliberate and measured against the alternatives: a cached
    health flag is stale in exactly the direction that matters (it says yes while the store is
    already gone), and anything cheaper does not establish reachability at all. The deployment
    already pays a comparable per-request open in `tools._model_version`; pooling the pair is
    ACE-028's, not this slice's.

    Local is always True and never opens anything. `governance-principles.md` scopes the principles
    to the served deployment, and locally there is no store to reach: `_record_query` writes jsonl,
    and a read-only artifacts directory must not stop a laptop from answering.
    """
    if not _hosted():
        return True
    try:
        from store import Store

        store = Store.from_env()
    except Exception:
        # Every way the store can be unopenable produces the same refusal, because there is only one
        # thing a caller can do about any of them. But the CAUSE has to land somewhere, or this
        # reintroduces the defect the spec exists to remove in a new place: a bad DSN scheme, an
        # unreachable host and a missing driver would all read as one opaque refusal and an operator
        # would have nothing to work from. Server log, with the traceback — the same treatment
        # `_record_query` gives a swallowed write, and for the same reason.
        #
        # `_RAW_LOG`, not `_LOG`, and for BOTH of that logger's reasons. The exception text carries
        # the host, the port and the DSN scheme — raw text, the operator's and never the caller's
        # (ACE-039). And this module never calls `basicConfig`, so a record on `_LOG` falls through
        # to `logging.lastResort` and lands on STDERR, which on the CLI/fork path is a wire carrying
        # exactly one JSON object: a diagnostic line there does not merely add noise, it makes the
        # refusal unparseable and the parent loses it entirely. `main` silences `_RAW_LOG` for the
        # child's lifetime, so the child stays clean while in-process and hosted still get the cause
        # on the server's root logger — which is where the operator being told to "restore the audit
        # database" actually reads it.
        _RAW_LOG.warning("audit store is not reachable; refusing to execute unrecorded",
                         exc_info=True)
        return False
    if store is None:
        # Reachable, not defensive: `_hosted()` tests the variable for truthiness while `from_env`
        # strips it, so a whitespace-only `AGAMI_DB_URL` lands exactly here. A configured-but-empty
        # store is a misconfiguration, and on a security gate a misconfiguration reads as
        # unreachable, never as fine.
        _RAW_LOG.warning("the audit store url is set but empty; treating it as unreachable")
        return False
    store.close()
    return True


def _engine_mismatch(profile: str, creds: dict) -> "Refusal | None":
    """Refuse when the model's declared engine is not the engine the credentials connect to.

    Silent when either side is absent or unmapped, and that is not laziness on the second one: the
    executor rejects an unusable credential type itself, with a message about the credential, and
    refusing here as well would relabel a plain configuration error as a governance refusal that
    misdescribes it. A missing declaration is already refused upstream by the readability gate.

    `getattr` throughout because this file is vendored into the plugin while runtime.py resolves
    from the separately-versioned installed package: an older runtime skips the check rather than
    raising AttributeError.
    """
    try:
        from semantic_model import runtime as RT
    except Exception:
        return None
    engines_disagree = getattr(RT, "engines_disagree", None)
    dialect_of = getattr(RT, "_dialect_of", None)
    if engines_disagree is None or dialect_of is None:
        return None
    org = _resolve_guard_model(profile)
    if org is None:
        return None
    if not engines_disagree(dialect_of(org)[0], creds.get("type", "")):
        return None
    return refuse(
        RULE_ENGINE_MISMATCH,
        detail="the datasource's declared engine is not the engine its credentials connect to, so "
               "the statement was checked against the wrong SQL grammar",
        remediation="Make the model's storage_connections[].storage_type match the engine the "
                    "credentials point at.",
    )


def _disk_model_root(profile: str) -> Path | None:
    """The on-disk model root for `profile`, or None when no model is declared there.

    Stdlib-only and free of any `semantic_model` import, deliberately: the fail-closed branch in
    `_model_safety` that calls this runs on the interpreter where that package is absent, and
    `_resolve_guard_model` below — the other caller — is unreachable there, since its own first
    statement imports `semantic_model.loader`.

    One definition rather than two copies of the same expression, because the two callers must agree
    about whether a model exists. If they drifted apart, the package-less path would refuse where the
    supported path would not have enforced (a false positive that breaks a working install) or stay
    silent where it would have (the hole ACE-071 closes).
    """
    root = Path(os.environ.get("AGAMI_ARTIFACTS_DIR") or (Path.home() / "agami-artifacts")) / profile
    return root if (root / "datasource.yaml").exists() else None


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

    root = _disk_model_root(profile)
    if root is not None:
        try:
            return L.load_datasource(root)
        except Exception:
            pass  # unparseable/absent on disk -> None (hosted then fails closed)
    return None


def _write_refusal(refusal: Refusal) -> None:
    """Write a guard refusal to stderr in the ONE shape every caller parses — the single wire-writer,
    called from ``main`` and from the per-engine CSV adapter, which is the other entry into the
    engines.

    One JSON object, on one line: ``{"refusal": {reason, rule, detail, remediation}}`` — the wire
    shape S2 established, which ``tools._stderr_refusal`` rebuilds through ``Refusal`` on the parent
    side of the process boundary. Nothing else may be written alongside it: several callers (and the
    fail-closed suite) parse the WHOLE stderr stream as a single object, so a second line of
    diagnostics would not merely be noisy, it would make the refusal unreadable. Nothing writes one
    now: four branches once did, and all four are gone — the default-filter and auto-rewrite
    notices with the rewrites they announced, the fan/chasm and sensitive-column diagnostics with
    the refusals they explained. The stream is a single object on every path.
    """
    json.dump({"refusal": asdict(refusal)}, sys.stderr)
    sys.stderr.write("\n")


def _model_safety(sql: str, profile: str, area: str | None) -> tuple[str, Refusal | None]:
    """Semantic-model safety pass before execution: the scope gates, over a model resolved from the
    DB (hosted) or disk (local).

    Every branch now returns a `Refusal` or nothing. The bare `int` this used to be able to return
    existed for two branches that wrote their own diagnostic to stderr and handed back an exit code
    with no rule attached — the fan/chasm pre-flight and the sensitive-column gate. Both are gone,
    so the interim `RULE_MODEL_SAFETY` that let `execute_guarded` turn that int into an Envelope is
    gone too, and "exactly one Envelope per path" holds without a placeholder rule standing in.

    A table's declared ``default_filters`` are NOT applied here: ACE-042 deleted the injection,
    because it authored SQL and mis-scoped it on any CTE. What replaced it is a REPORT rather than
    an edit — ``runtime.assemble_receipt`` decides, per table reference, which of that reference's
    declared filters the statement applied and which it omitted, and puts the answer on
    ``tables.items[].filters``. So a declared filter still never changes the statement this pass
    hands on, and the caller is no longer left to infer whether it was satisfied.

    Returns ``(sql_to_run, verdict)``. ``verdict`` is ``None`` to continue or a ``Refusal`` from
    whichever gate chose it — there is no third thing, so every refusal names its own rule. Inert
    (returns the SQL unchanged) only when no model is DECLARED for the profile: a local install with
    nothing built yet has no declared surface, so no gate has anything to enforce. Every other
    "cannot guarantee safety" state refuses — on the HOSTED path when the model cannot be resolved or
    the package/parser is missing (ACE-051), and on the LOCAL path when the guards cannot be imported
    while a model does exist on disk (ACE-071).

    Every branch writes NOTHING: it hands the contract object back and ``execute_guarded`` puts it
    in the Envelope, so the in-process caller sees the same rule the forked one does. The fan/chasm
    pre-flight and the sensitive-column check were the two exceptions, writing their own diagnostic
    and returning a bare int. Both became receipt facts, so neither is here at all now.
    """
    try:
        from semantic_model import runtime as RT
    except Exception:
        # The model package (pydantic) isn't importable, so the guards can't run at all. On the
        # hosted served path that is the same "can't guarantee safety" condition as a missing model
        # — fail closed. Locally it stays a no-op (a bare install legitimately has no model). The
        # two siblings of this branch are below: an unresolvable model, and an absent SQL parser.
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
        # The local twin, in two halves (ACE-071). This path is the VENDORED slice — the one
        # `python3 -m execute_sql` resolves, which ships no `runtime.py` — so table scope, the star
        # ban and column scope cannot run here at all. `_receipt_for` already concedes exactly that,
        # returning `undetermined_receipt(RECEIPT_NO_RUNTIME)`, and principle 4c makes undetermined a
        # refusal. The gap was never missing information: we published the conclusion and then
        # executed anyway.
        #
        # So: refuse when a model is DECLARED for this profile, because the user has a declared
        # surface and believes it is being enforced, and 4b reach is exactly what is unchecked here.
        # `_hosted()` is deliberately not consulted — both interpreters in question are local, and
        # what separates them is which one can import the guards, not where they run.
        if _disk_model_root(profile) is not None:
            # Value-free, like its hosted siblings: it names no resolved path, no profile directory,
            # no DSN. The remediation names the supported invocation, which is the whole actionable
            # content — the model is fine, the interpreter is the problem.
            return sql, refuse(
                RULE_MODEL_UNAVAILABLE,
                detail="the semantic-model package is not importable on this interpreter, so table "
                       "and column scope could not run against the model declared for this profile",
                remediation="Run this through the interpreter that has the semantic-model package "
                            "installed (the project virtualenv), then retry.",
            )
        # And stay inert with no model on disk. A bare install between `pip install` and its first
        # `agami-connect` has no declared surface, so there is nothing 4b could be exceeded against
        # and nothing 4c could be undetermined about. Refusing here would break every user in that
        # window to close a hole that is not open.
        return sql, None  # local: no model declared -> no-op

    org = _resolve_guard_model(profile)
    # Published for the receipt builder before any gate can refuse, so a refusal's receipt is built
    # against the same model the gate that fired read. See `_guard_model`.
    _guard_model.set(org)
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

    # No parser, no guard — the third member of the family above, and the last of them to be closed.
    # Every gate below opens with `if not _HAVE_SQLGLOT: return None`, so a hosted server whose
    # sqlglot import failed resolves a model, reports itself guarded, and then runs the statement
    # with table scope, column scope, the star ban and the readability gate all silently inert. It is
    # the same "cannot guarantee safety" condition as the two branches above and takes the same rule:
    # a deployment-state fact that says nothing about the statement, so no re-emission fixes it.
    #
    # The local twin stays a no-op for the third time, and for the same reason — a bare install
    # legitimately has no sqlglot, and refusing there would break every local user to close a hole
    # that only exists on a served path.
    if _hosted() and not getattr(RT, "_HAVE_SQLGLOT", True):
        return sql, refuse(
            RULE_MODEL_UNAVAILABLE,
            detail="the SQL parser is not installed, so no guard could read the statement; "
                   "refusing to run unguarded on the hosted server",
            remediation="Install the semantic-model dependencies on the server and retry.",
        )

    # Build the shared guard context ONCE — parse the SQL + build each model index a single
    # time — and thread it through the battery below, instead of every guard re-parsing and
    # rebuilding its index (audit P2 / ACE-045). Behaviour-preserving: a guard given `ctx`
    # returns the same verdict as one that builds its own.
    ctx = RT.build_guard_context(sql, org)
    # Published for the result-bound refusal, off the tree that was just parsed rather than a second
    # one. Set here and not at the chokepoint because the chokepoint cannot import sqlglot — see
    # `_guard_shape`. Stays None when `ctx` is None (no sqlglot) or the statement did not parse.
    _guard_shape.set(RT.statement_shape(ctx))

    # Readability gate — refuse a statement the guard cannot read in this datasource's own grammar,
    # BEFORE any gate below is asked to judge it. Each gate below degrades to allow when it has no
    # tree, so an unreadable statement reaches all three looking clean: on a backtick-quoting engine
    # the old generic parse returned no tables and no columns, and table scope, the star ban and
    # column scope each found nothing to object to while the statement read whatever it liked.
    # `getattr` because this file is vendored into the plugin while runtime.py resolves from the
    # separately-versioned installed package, so a newer plugin meeting an older runtime skips the
    # gate instead of raising AttributeError.
    check_readable = getattr(RT, "check_readable", None)
    if check_readable is not None:
        unreadable = check_readable(sql, org, ctx=ctx)
        if unreadable is not None:
            return sql, unreadable

    # Scopability gate — the readable statement that still cannot be checked. It runs between the
    # readability gate and the scope gates because that is exactly the gap it fills: the gate above
    # asks whether we could read the statement, the gates below ask what the statement reaches, and
    # a table function is readable while reaching something neither one can name. Table scope skips
    # an empty-name table so that a CTE reference passes, and a table function arrives as precisely
    # that node, so it went through both. Same `getattr` reason as the gate above.
    check_scopable = getattr(RT, "check_scopable", None)
    if check_scopable is not None:
        unscopable = check_scopable(sql, org, ctx=ctx)
        if unscopable is not None:
            return sql, unscopable

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

    # The fan/chasm pre-flight and the sensitive-projection check used to sit here and REFUSE. Both
    # are gone from this function, and neither analysis is: they run in `RT.assemble_receipt`, where
    # what they find rides on the answer as a fact.
    #
    # Correctness cannot be a refusal. Whether a join multiplies the rows an aggregate is computed
    # from is derivable from the statement and the model, but whether that multiplication is a BUG
    # depends on the question, and the question never reaches this frame — the same statement is
    # wrong for order revenue and right for line-item exposure. Refusing it here made a judgement on
    # the caller's behalf, in the one place least equipped to make it.
    #
    # The sensitive-projection refusal went for a different reason: it was an access policy, and we
    # hold none of our own. A column that must not be readable is not declared, and the two scope
    # gates above already refuse any statement reaching it — that is 4b, and it needs no help.
    #
    # What is left here is 4a and 4b, which is the whole of what principle 4 permits.

    # No branch rebinds `sql` anywhere in this function, and the guards above all run against what
    # the caller actually sent. tests/test_ace093_byte_identity.py asserts that at the driver.
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
# guard is applied identically and can't be bypassed. `_run_<db>` is the shared connect-and-run that
# returns native rows to either caller.
#
# There is deliberately no second route into `_run_<db>`. A family of per-engine `_execute_<db>` CSV
# wrappers used to sit here, reached through `_emit_or_err`, and by the time it was deleted (ACE-087)
# nothing in production called them: `main` reaches the engines through `execute_guarded` and the
# built-in executor. They mattered because they still TRIMMED and flagged, with no refusal and no
# receipt, so anything wired back onto them would have gone round the chokepoint while looking like
# it went through it. Keeping the single chokepoint true structurally, rather than by nobody
# happening to call the alternative, is worth more than the wrappers were.


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
    satisfies the port by shape (method-style, like the other three ports). Stateless — one shared
    ``BUILTIN_EXECUTOR`` instance."""

    def execute(self, vetted_sql: str, creds: dict[str, str], *, profile: str) -> ExecResult:
        return _builtin_execute(vetted_sql, creds, profile=profile)


BUILTIN_EXECUTOR = _BuiltinExecutor()


def _receipt_for(sql: str, profile: str, *, bounded: bool) -> Receipt:
    """The ``Receipt`` this call's Envelope carries.

    ``bounded`` picks the assembler, and the difference is a security boundary rather than a detail
    level. **Every NON-OK body gets the bounded one**, which echoes only what the caller's own
    statement already disclosed. The rule used to be "every REFUSAL", on the reasoning that a
    ``failed`` body discloses nothing an ``ok`` body would not, because reaching ``failed`` means the
    statement's names passed the scope gates. That reasoning is wrong: a table or column the model
    declares and the physical warehouse does not have can only ever produce ``failed`` — ``ok`` is
    structurally unreachable for it — so ``failed`` is a disclosure channel of its own rather than a
    subset of one. The full receipt therefore rides ``ok`` alone, which is the one status a caller
    cannot provoke without the model and the warehouse already agreeing about every name in it.

    The caller of a non-ok outcome also passes the statement it RECEIVED rather than the one that
    ran: see ``execute_guarded``, which captures it before ``_model_safety`` can rebind the local.
    The ok receipt is the one built from the rebound value, because SC-6 asks it to describe what
    actually executed.

    Degrades rather than crashes, three ways, because every one of them is a real deployment. The
    vendored plugin mirror ships ``semantic_model/__init__.py`` and ``units.py`` and no runtime at
    all, so the guarded import is the same one ``_model_safety`` makes for the same reason; a local
    install legitimately has no model yet; and an assembler that raises must cost the caller its
    receipt, never its answer.
    """
    try:
        from semantic_model import runtime as RT
    except ImportError:
        # The vendored plugin mirror ships `guardrail` and this module but no
        # `semantic_model.runtime` at all, so the import genuinely fails there. That is a fact about
        # the deployment, not a fault, and it is what `RECEIPT_NO_RUNTIME` says.
        return undetermined_receipt(RECEIPT_NO_RUNTIME)
    except Exception:
        # The module IS installed and raised while importing itself, which is a defect. Reporting it
        # as "not available in this deployment" would send an operator looking for a missing install
        # and leave the real error unlogged. Two different facts, and this spec exists to stop them
        # being reported as one.
        _LOG.error("the semantic-model runtime failed to import", exc_info=True)
        return undetermined_receipt(RECEIPT_BUILD_FAILED)
    org = _guard_model.get()
    if org is None:
        return undetermined_receipt(RECEIPT_NO_MODEL)
    try:
        assemble = RT.assemble_refusal_receipt if bounded else RT.assemble_receipt
        return receipt_from_assembled(
            assemble(org, sql, model_version=_receipt_model_version(profile))
        )
    except Exception:
        # Unanticipated, so unreadable — the same posture as the chokepoint's own catch-all: the
        # stack goes to the server log and the caller gets a receipt that says it has nothing.
        _LOG.error("could not assemble the receipt", exc_info=True)
        return undetermined_receipt(RECEIPT_BUILD_FAILED)


def _refusal_receipt(refusal: Refusal, received_sql: str, profile: str) -> Receipt:
    """The receipt a REFUSED Envelope carries, chosen by which rule fired.

    A ``PRE_MODEL_RULES`` refusal is not a receipt that could not be built — it is a receipt there
    was never anything to build. Those gates run above the semantic-model pass, so ``_guard_model``
    is still the ``None`` ``execute_guarded`` cleared on entry, and asking the builder anyway
    produced ``RECEIPT_NO_MODEL``: "no model could be resolved", about a deployment whose model
    resolves perfectly a line later. Naming the rule instead is both truthful and free, and it is the
    same branch ``tools`` takes on the far side of a fork, so one refusal reads one way on both paths.

    Everything else is a refusal a gate reached WITH the model in hand, so it gets the echo-bounded
    receipt built from the statement the caller sent.
    """
    if refusal.rule in PRE_MODEL_RULES:
        return undetermined_receipt(RECEIPT_BEFORE_MODEL)
    return _receipt_for(received_sql, profile, bounded=True)


def _receipt_model_version(profile: str) -> str | None:
    """The model-version pin the receipt records, read from ``tools`` rather than resolved here.

    The two execution paths build the receipt in two different processes — the in-process path in
    this module, the fork path in the parent, because the child's Envelope is destroyed at the
    process boundary — and they must pin the SAME version or the same statement against the same
    model comes back with two different receipts. One resolver is the only way to guarantee that.

    ``tools`` imports nothing heavier than the stdlib at module level and this module already
    reaches for it the same way in ``_resolve_guard_model``. It is absent from the vendored plugin
    mirror, which is why this degrades to an unpinned receipt rather than failing: the mirror has no
    runtime to assemble one with either.
    """
    try:
        from tools import _model_version

        return _model_version(profile)
    except Exception:
        return None


def _envelope(
    status: Status,
    *,
    receipt: Receipt,
    data: ExecResult | None = None,
    refusal: Refusal | None = None,
    failure: Failure | None = None,
) -> Envelope:
    """The ONE place ``execute_guarded`` constructs an ``Envelope``, and the one place this module
    mints an ``audit_id``.

    ``receipt`` is MANDATORY, which is what actually delivers the property this docstring used to
    claim: an outcome that has no receipt to hand must return one that SAYS so, rather than the
    empty-and-silent default a consumer would read as "checked, found nothing". While the parameter
    carried a fallback, that promise rested on every call site remembering to pass one — and the
    fallback itself named the wrong cause (``RECEIPT_NO_MODEL``) for the outcomes that could have
    reached it. Every call site already passes one, so requiring it costs nothing and makes the
    omission unrepresentable.

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
        receipt=receipt,
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

    **The cost is an abandoned worker, and it is real.** On expiry the thread is still inside the
    driver call, and it stays there: nothing here cancels it, and it may hold a database connection
    (and, on the server side, a running statement) until that call returns on its own. It is a
    daemon so it cannot hold the interpreter open at exit. This is a bound on how long a CALLER
    waits, not a promise that the work stopped — which is exactly why the inner watchdog, the layer
    that really can cancel, is set to fire first.

    **And the cost is CAPPED, because bounding the wait removed the ceiling that used to bound it.**
    Before this layer existed the same slow call blocked the caller's own worker thread, so the
    host's thread limiter capped how much abandoned work could be in flight and applied backpressure
    to everything behind it. Returning at the bound frees that slot, so on the injected path — where
    there is no inner watchdog and EVERY slow statement abandons one — the abandonments would
    otherwise accumulate without limit until the pool, and with it the datasource, belonged entirely
    to work nobody is waiting for. So the count is capped by ``_MAX_ABANDONED_WORKERS`` and checked
    BEFORE a worker is started: at the cap this refuses immediately rather than starting abandonment
    N+1, and says the executor is saturated rather than blaming a statement that never ran. The slot
    is released when the abandoned worker finally returns, which is the moment the leak actually
    ends.

    Exceptions cross the thread boundary with their ORIGINAL type, re-raised here. That is what
    keeps the handlers in ``execute_guarded`` correct: ``_ResourceLimit`` still reaches the refusal
    branch and ``ExecutorError`` still reaches the classified one, rather than every executor
    failure arriving as a worker that finished having produced nothing. ``BaseException`` is caught
    rather than ``Exception`` for the same reason: a driver's deep ``sys.exit`` raises ``SystemExit``,
    which ``tools._run_in_process`` nets one layer up, and swallowing it in a worker thread would
    turn that fail-closed answer into a silent hang until the bound expired.

    The call runs inside a copy of the CALLER's context, and what that is FOR changed when the
    per-call row cap went (ACE-087). It used to carry ``_max_rows_override`` to ``_resolve_row_cap``
    inside the worker; the cap is the deployment's environment now and needs no carrier. What it
    still carries is the *caller's* request scope — ``tools._current_org_ctx``, the resolve-once
    request cache, the actor and session on the served path — into the one place a consumer's own
    code runs. That is the point of the ``Executor`` seam: a pooled / per-user-RBAC executor picks
    its connection from exactly that context, and a new thread starts with an empty one, so dropping
    the copy would hand every injected executor a blank request to serve.
    """
    global _abandoned_workers

    timeout_s = _resolve_timeout_s()
    outcome: dict[str, Any] = {}
    ctx = copy_context()

    with _abandoned_lock:
        if _abandoned_workers >= _MAX_ABANDONED_WORKERS:
            # Fail closed, and before the work starts: the cheapest moment to say no, and the only
            # one at which saying no still prevents anything.
            raise _ExecutorSaturated(_EXECUTOR_SATURATED)

    def call() -> None:
        global _abandoned_workers
        try:
            outcome["result"] = ctx.run(executor.execute, sql, creds, profile=profile)
        except BaseException as exc:
            outcome["error"] = exc
        finally:
            # `finished` and the slot release are set under the same lock the abandonment takes, so
            # the SLOT ACCOUNTING cannot double-count: a worker that finishes in the sliver between
            # the join timing out and the lock being taken decrements the slot the caller just took.
            #
            # The OUTCOME is not decided that cleanly, and the difference is a known defect — see
            # issue #177. Whichever thread reaches this lock first decides: if the caller wins it
            # reads `finished` unset and raises, so a call whose work had in fact completed is
            # refused. It fails closed, and the built-in path is shielded by the per-statement
            # watchdog firing a full skew earlier, so it is reachable only through an injected
            # executor returning in that same instant. The repair is to signal the end of the WORK
            # separately from this accounting, so the flag the caller reads cannot be delayed by
            # lock contention. Do not "fix" it with `worker.is_alive()`: a worker parked here in
            # its `finally` is still alive, and reads as abandoned in exactly the losing ordering.
            with _abandoned_lock:
                if outcome.get("abandoned"):
                    _abandoned_workers -= 1
                outcome["finished"] = True

    worker = threading.Thread(target=call, name="agami-bounded-execute", daemon=True)
    worker.start()
    worker.join(timeout_s + _OUTER_BOUND_SKEW_S)
    with _abandoned_lock:
        abandoned = not outcome.get("finished")
        if abandoned:
            _abandoned_workers += 1
            outcome["abandoned"] = True
    if abandoned:
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
    semantic-model safety pass (the scope gates; the fan/chasm and PII checks became receipt facts,
    and declared ``default_filters`` are not applied here either — which of them the statement
    satisfied became a receipt fact too, per table reference) ->
    resolve the datasource -> ``executor.execute(vetted_sql, …)``. The executor only ever receives
    SQL both guards have passed.

    **Every path returns exactly one ``Envelope``, and this function is TOTAL** — nothing is raised
    out of here for a caller to interpret, so the subprocess ``main`` and the in-process MCP handler
    cannot disagree about what happened. The six outcomes:

      * the read-only gate refuses            -> ``refused`` carrying that gate's ``Refusal``
      * ``_model_safety`` returns a Refusal   -> ``refused`` carrying it verbatim
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
    the two callers would each have to translate. The row cap is the deployment's alone
    (``AGAMI_SQL_MAX_ROWS``); no caller can lower it for one call."""
    # Clear before anything can set it, so a detail from a PREVIOUS call in this context can never
    # be attributed to this one. The recorder reads it unconditionally; a stale value would put the
    # wrong error text on a row that succeeded.
    _last_error_detail.set(None)
    # Same reason again: a verdict left behind by an earlier call in this context must never label
    # this one. The recorder falls back to parsing the body when this is None, so a stale value is
    # strictly worse than no value — it would be believed.
    _last_outcome.set(None)
    # Same reason, same load-bearing half: a model resolved for an earlier call in this context must
    # never be the one this call's receipt describes.
    _guard_model.set(None)
    # And the same again for the shape. A stale value here is worse than none: it would word this
    # call's refusal for a statement somebody else sent, and the two wordings give opposite advice.
    _guard_shape.set(None)
    # The statement the CALLER sent. Every NON-OK receipt is built from this one, and the rule is
    # about the REBINDING rather than about any particular mechanism: whatever a safety pass rewrites
    # a statement INTO is the guard's own text, so a rebound string can name a table the caller never
    # wrote, and a refusal built from it then describes a statement nobody sent, in model-authored
    # text — precisely the schema listing `tests/test_ace035_no_enumeration.py` exists to prevent.
    # The reproduction came through `apply_default_filters`, which pulled the name straight out of
    # the model's YAML.
    #
    # No pass rewrites anything today: that injector went, then the fan-join auto-rewrite, and
    # `_model_safety` now returns the statement it was given on every path. So this equals the
    # executed statement rather than merely guarding against it, and the capture is kept as the thing
    # that keeps it that way — a future rewrite would have to reintroduce the divergence past a name
    # that says what it is for.
    received_sql = sql
    try:
        # FIRST, above the read-only gate, because principle 7 records "every call ... whether it
        # executed or was refused" — so gating only execution would leave the refusals unrecorded
        # too, and the outcomes most worth reviewing are exactly the ones a reviewer could not find.
        # A `DROP TABLE` therefore comes back `audit_unavailable` rather than `read_only` while the
        # store is down. Nothing is weakened by that ordering: both are refusals, nothing runs, and
        # the security gates below are unreachable only in the state where NOTHING is reachable.
        #
        # Outside the `no_safety` branch on purpose. That flag skips the semantic-model pass and
        # nothing else; an audit guarantee is not a model check, and a caller able to opt out of
        # being recorded is the hole this spec closes.
        if not _audit_store_reachable():
            refusal = refuse(
                RULE_AUDIT_UNAVAILABLE,
                detail="the audit store is not reachable, so this call cannot be recorded",
                remediation="Restore the audit database, then run the statement again.",
            )
            return _envelope("refused", refusal=refusal,
                             receipt=_refusal_receipt(refusal, received_sql, profile))
        import sql_guard

        refusal = sql_guard.check_read_only(sql)
        if refusal is not None:
            return _envelope("refused", refusal=refusal,
                             receipt=_refusal_receipt(refusal, received_sql, profile))
        # Metadata / recon functions, ABOVE the `no_safety` branch on purpose. `no_safety` skips the
        # semantic-model pass and nothing else; server fingerprinting and object-existence probing
        # are a hard gate for the same reason write and RCE protection are. Running second is what
        # makes the label deterministic when a name is on both lists (principle 9).
        refusal = sql_guard.check_no_recon(sql)
        if refusal is not None:
            return _envelope("refused", refusal=refusal,
                             receipt=_refusal_receipt(refusal, received_sql, profile))
        if not no_safety:
            # `_model_safety` returns the statement it will actually run. It returns the one it was
            # given, on every path, since the fan-join auto-rewrite was the last branch that changed
            # it — so this rebinding is now a no-op and the `ok` receipt built below describes the
            # caller's own statement. The assignment stays because the CONTRACT is "run what this
            # returns": reading the return value is what makes a reintroduced rewrite a receipt bug
            # rather than an executed-statement bug.
            sql, verdict = _model_safety(sql, profile, area)
            if isinstance(verdict, Refusal):
                return _envelope("refused", refusal=verdict,
                                 receipt=_refusal_receipt(verdict, received_sql, profile))
        creds = _load_credentials(profile, org_id or "local")
        if not no_safety:
            # The guard picked its grammar from the model's declared engine; the executor picks its
            # driver from these credentials. Two independent pieces of operator configuration with
            # nothing reconciling them, so a mis-declared model has the guard vet a statement in a
            # grammar the database does not speak — this defect again, by a different door.
            # Credentials resolve after the gates by design, so this is the first point at which
            # both are known.
            mismatch = _engine_mismatch(profile, creds)
            if mismatch is not None:
                return _envelope("refused", refusal=mismatch,
                                 receipt=_refusal_receipt(mismatch, received_sql, profile))
        # Bounded at the CHOKEPOINT, so the limit reaches every executor rather than only the
        # built-in one whose engines carry the inner watchdog. See `_execute_bounded` for the
        # mechanism and for the leaked worker it costs on expiry.
        result = _execute_bounded(executor, sql, creds, profile=profile)
        # Inside the try on purpose: an executor that returns `None` (or anything else the contract
        # does not accept) breaks here rather than at the chokepoint's caller — that is a broken
        # adapter, not a reason for `execute_guarded` to stop being total. It used to fail the
        # present-iff check in `Envelope.__post_init__`; since ACE-087 the `truncated` read below
        # reaches it first and raises `AttributeError` instead. Same outcome by the same handler
        # (`failed`/`other`, logged, audit row written), one line earlier.
        # The transfer bound, checked BEFORE the `ok` envelope can be built (ACE-087). It sits here
        # rather than in `_collect_cursor` because that function is only one of three ways a result
        # arrives: BigQuery has no DB-API cursor and bounds itself, and an injected `ports.Executor`
        # — the pooled / per-user-RBAC seam a hosted consumer supplies — reaches neither. All three
        # set `truncated` on the ExecResult, so reading it here is the one place that covers them.
        #
        # The rows are dropped rather than returned. `_envelope("refused", …)` carries no data by
        # construction, which is the whole point: what the executor holds is whichever rows the
        # engine emitted first, and with no ORDER BY that is an arbitrary sample of the real result.
        if result.truncated:
            return _envelope("refused", refusal=_resource_limit_refusal(None),
                             receipt=_receipt_for(received_sql, profile, bounded=True))
        # The one receipt built from the REBOUND statement: `ok` is the status SC-6 asks to describe
        # what actually executed, and it is also the one a caller cannot provoke without every name
        # in it having already satisfied the scope gates and the warehouse.
        return _envelope("ok", data=result,
                         receipt=_receipt_for(sql, profile, bounded=False))
    except _ResourceLimit as exc:
        # AHEAD of both handlers below: `_ResourceLimit` is an `ExecutorError` sibling, not a
        # subclass, but the catch-all would swallow it and report a bound WE imposed as an
        # unclassified server break. This is the per-statement bound the contract reserves
        # `resource_limit` for — its subject IS the statement, so "narrow it and run it again" is a
        # fix we can honestly name, unlike the supervisor's kill of a child that never returned.
        #
        # ONE handler for all three bounds, and so exactly one refusal per call however many layers
        # were armed. When the inner watchdog fired, its marker is what arrives here — the outer
        # layer's join returned long before its own budget and has nothing to add, so the inner
        # refusal is the answer and cannot be overwritten by a later one.
        return _envelope("refused", refusal=_resource_limit_refusal(exc),
                         receipt=_receipt_for(received_sql, profile, bounded=True))
    except ExecutorError as exc:
        # Two kinds of text arrive here and only one of them is ours (ACE-039).
        #
        # Codes 2 and 3 are AUTHORED by this module — "No warehouse credentials for profile […]",
        # the chmod-600 fix, the `pip install` line. They name an operator action and contain
        # nothing the database said, so they are relayed verbatim exactly as before.
        if exc.code in _AUTHORED_EXIT_CODES:
            return _envelope("failed", failure=Failure(
                kind=EXIT_TO_FAILURE_KIND.get(exc.code, "other"), message=exc.msg,
            ), receipt=_receipt_for(received_sql, profile, bounded=True))
        # Codes 4 and 5 carry the driver's own exception, interpolated at one of the twenty
        # per-engine sites. That text is an enumeration channel: a PostgreSQL HINT names declared
        # columns the caller never sent. Classify FROM it, return a fixed sentence INSTEAD of it.
        kind = _classify_db_error(exc.msg, exc.code)
        # Keep the raw text for the operator, on two channels that reach two different places. The
        # ContextVar is read by `tools._record_execution` when the recorder shares this process, and
        # `_RAW_LOG` is the server-side trail everywhere else. Neither is the caller's channel.
        _last_error_detail.set(exc.msg)
        _RAW_LOG.error("database error (audit detail): %s", exc.msg)
        return _envelope("failed", failure=Failure(
            kind=kind, message=_ERROR_MESSAGES.get(kind, UNEXPECTED_FAILURE_MESSAGE),
        ), receipt=_receipt_for(received_sql, profile, bounded=True))
    except Exception:
        # Unanticipated, so unreadable: nobody has vetted what this exception's text contains, and
        # `configparser.MissingSectionHeaderError` alone carries the absolute path of the
        # credentials file. The raw text and its stack go to the server log; the caller gets the
        # generic message. Logged at ERROR rather than WARNING because reaching here means a bug or
        # a broken adapter, not a user mistake.
        _LOG.error("unhandled error in the guarded execution path", exc_info=True)
        return _envelope("failed", failure=Failure(
            kind="other", message=UNEXPECTED_FAILURE_MESSAGE,
        ), receipt=_receipt_for(received_sql, profile, bounded=True))


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
                   help="Subject area for the semantic-model safety pass (pre-flight + scope + PII).")
    p.add_argument("--no-safety", action="store_true",
                   help="Skip the semantic-model safety pass.")
    args = p.parse_args()

    # Silence the raw-detail logger for the whole CLI/child lifetime. Without this it falls through
    # to `logging.lastResort`, which writes to stderr — and the parent relays the child's stderr into
    # `failure.message`, so the raw driver text this module just took OUT of the caller's answer
    # would arrive back in it (ACE-039). The child therefore records no detail at all and the audit
    # column stays NULL, which is exactly what NULL means on that column: the chokepoint and the
    # recorder were not in one process.
    _RAW_LOG.addHandler(logging.NullHandler())
    _RAW_LOG.propagate = False

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
