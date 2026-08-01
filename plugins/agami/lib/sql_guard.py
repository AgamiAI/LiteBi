"""
Read-only / dangerous-SQL guard — the single source of truth for "is this SQL
safe to run against the user's database?".

This gate runs at the shared executor chokepoint (`execute_sql.py::main`) and as a
fail-fast pre-check in the MCP tool layer (`tools.check_read_only`), so the stdio
server, the HTTP/OAuth server, the agami-query skill, and cron are all protected
identically — not just whichever path happened to read a prose rule.

It is defense in depth at the application layer; the underlying connection is *also*
expected to run under a read-only role. Postgres / Redshift are the primary concern
(the dangerous functions below are Postgres server-side primitives), but the checks
are neutral enough to be safe across the other supported engines.

`check_read_only(sql)` returns `None` when the SQL is a single safe read-only
statement, else a `guardrail.Refusal` carrying `rule=read_only`, the rejection text as
`detail`, and the fix for that specific rejection as `remediation`. Callers relay the
whole object rather than re-deriving a shape of their own.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from guardrail import RULE_READ_ONLY, RULE_RECON, RULE_UNPARSEABLE, Refusal, refuse

# Hard cap on SQL length. Prevents a compromised client from POSTing a multi-MB
# SQL blob that takes the parser / planner / this gate down a slow path. Real
# analytics SQL fits in ~10KB; 50KB is conservative.
_MAX_SQL_CHARS = 50_000

# One remediation per rejection, gathered here so a reviewer can audit every fix this gate hands
# back on a single screen. They are deliberately NOT one shared string: shortening a 60,000-character
# statement and dropping a `pg_read_file` call are different actions, and a generic "fix your SQL"
# would make the contract's mandatory-remediation rule vacuous — satisfied in form, useless in fact.
#
# Every entry is static prose. That is the property that keeps a refusal from becoming a
# schema-listing endpoint: a remediation built by interpolating what IS allowed would enumerate the
# declared surface to a caller who has not been granted it. The only caller-specific text any
# rejection carries lives in `detail`, and only ever echoes a token the caller itself sent.
_REMEDIATION: dict[str, str] = {
    "empty": "Send a SELECT or WITH…SELECT statement.",
    "too_long": (
        f"Shorten the statement — the cap is {_MAX_SQL_CHARS:,} characters, "
        "and analytics SQL fits well under it."
    ),
    "bare_line_comment": "Put a space after `--`, or use a `/* … */` block comment.",
    "mysql_exec_comment": (
        "Remove the `/*! … */` executable comment; a plain `/* … */` comment is fine."
    ),
    "multi_statement": "Send exactly one statement.",
    "not_a_select": "Rewrite as a read-only SELECT or WITH…SELECT.",
    "denied_keyword": (
        "Rewrite as a read-only SELECT — this connection executes no writes, DDL, "
        "transaction control, session state or prepared statements."
    ),
    "row_lock": "Remove the row-lock clause; an analytic read never needs one.",
    "dangerous_function": (
        "Remove the call — server-file, OS, process-control, sleep and remote-SQL "
        "functions are never executed here."
    ),
}

# Opening delimiter of a Postgres / Snowflake / DuckDB dollar-quoted string —
# `$$` or a tagged `$name$`. A positional parameter (`$1`) is NOT an opener (no
# second `$`), so those pass through untouched. `_neutralize` finds the matching
# close tag itself (a backreference can't express "same literal tag" inside the
# single-pass scan cleanly, so the scan does the find).
#
# `\w*` accepts digit-led tags (`$1$`) too, which Postgres itself rejects (a real
# tag follows identifier rules and can't start with a digit). Being STRICTER than
# the grammar here is deliberate: treating any `$…$`-delimited span as an opaque
# literal only ever neutralizes *more*, so it can never hide a token the database
# would execute — it just refuses to let a `$1$`-looking region desync the scan.
_DOLLAR_OPEN_RE = re.compile(r"\$\w*\$")


class _GuardReject(Exception):
    """Raised from the scan when SQL uses a construct whose meaning is
    dialect-ambiguous and therefore cannot be neutralized safely with one lexer
    (see the MySQL comment forms in `_neutralize`). Carries both halves of the
    caller-facing refusal, because the relay in `check_read_only` cannot tell which
    of the two ambiguous forms fired and so cannot pick the right fix on its own.
    """

    def __init__(self, detail: str, remediation: str) -> None:
        self.detail = detail
        self.remediation = remediation
        super().__init__(detail)


class _Neutralized(NamedTuple):
    """The analysis copy of a statement, plus where its quoted identifiers ended up.

    `text` is the neutralized statement, **already stripped**. `quoted` holds one
    `[start, end)` per double-quoted identifier, naming that identifier's CONTENT in
    `text`'s own coordinates — never the re-supplied separators around it.

    **One coordinate frame, deliberately.** The scan re-supplies separator spaces and
    drops delimiters, so input offsets are not output offsets, and stripping afterwards
    would shift them a third time. Doing the strip *inside* this function and reporting
    spans against the stripped result is what makes a span usable without any caller
    reconciling frames. A span in the wrong frame does not fail loudly; it mis-identifies
    a token, so the frame is collapsed to one rather than documented.

    **Only the niladic recon matcher reads `quoted`.** Every other consumer — the
    read-only gate above all — matches `text` with the quotes already dropped, because
    that unwrapping is what keeps `SELECT*FROM"pg_read_file"(...)` visible to a
    `\\b`-anchored pattern. A quoted bare word is unambiguously an identifier; a quoted
    name with a trailing `(` is still a call. That distinction is the consumer's to make,
    which is why this type reports provenance and decides nothing.
    """

    text: str
    quoted: tuple[tuple[int, int], ...]


def _neutralize(sql: str) -> _Neutralized:
    """Blank out comments and string / dollar-quoted literals, and drop the quote
    delimiters of double-quoted identifiers (keeping their content), in a SINGLE
    left-to-right pass so the FIRST-opened construct wins — exactly how the database
    lexer resolves them.

    A stack of independent regex subs (one per construct) CANNOT do this: whichever
    regex runs first is blind to the others, so a `'` inside a `$$...$$` body, or a
    `$$` inside a `-- ...` comment, desyncs it and can smuggle an injected
    `; DROP ...` past the multi-statement check. The scan below never desyncs
    because at each position it commits to whatever opens there and skips to that
    construct's own close. Under-matching (an unterminated literal running to EOF)
    only ever fails *safe* — a stray `;` stays visible and trips the guard.

    Only this analysis copy is transformed; the ORIGINAL sql is what executes.
    Neutralized spans collapse to a single space (never empty — welding tokens like
    `SELECT/**/INTO` -> `SELECTINTO` would defeat the `\\b` word boundaries below).

    Escapes: `''` inside a single-quoted literal and `""` inside a double-quoted
    identifier are treated as doubled-delimiter escapes (standard SQL). Backslash is
    deliberately NOT an escape here — engines disagree (MySQL yes, standard PG no),
    and not honoring it can only stop a literal *early* (fail safe), never late.

    Returns a `_Neutralized` — the stripped text plus the quoted-identifier spans. See
    that type for why the strip happens here rather than at the call site.
    """
    def _last_emitted(chunks: list[str]) -> str:
        """The last character actually emitted, skipping empty chunks.

        Peeking at `chunks[-1]` alone is wrong: a zero-length identifier (`""`) appends an
        empty string, which would hide the real preceding character and drop a separator
        that is needed.
        """
        for chunk in reversed(chunks):
            if chunk:
                return chunk[-1]
        return ""

    out: list[str] = []
    spans: list[tuple[int, int]] = []
    width = 0  # running len("".join(out)), so a span can be recorded as it is emitted

    def emit(chunk: str) -> None:
        nonlocal width
        out.append(chunk)
        width += len(chunk)

    i, n = 0, len(sql)
    while i < n:
        two = sql[i : i + 2]
        if two == "--":  # line comment — ends at CR or LF (PG scanner ends at either)
            # MySQL/MariaDB only treat `--` as a comment when the next char is
            # whitespace/EOL/EOF; `--0` there parses as `- -0`, so blanking it (PG's
            # rule) would hide a following `;DROP`. The two dialects genuinely
            # disagree, so refuse the ambiguous form rather than pick one.
            nxt = sql[i + 2] if i + 2 < n else ""
            if nxt and nxt not in " \t\r\n\f":
                raise _GuardReject(
                    "an inline '--' comment must be followed by whitespace "
                    "(bare '--x' is a comment in Postgres but an operator in MySQL)",
                    _REMEDIATION["bare_line_comment"],
                )
            j = i + 2
            while j < n and sql[j] not in "\r\n":
                j += 1
            emit(" ")
            i = j
        elif two == "/*":  # block comment
            # `/*! ... */` (and versioned `/*!NNNNN ... */`) is a MySQL *executable*
            # comment — the server runs its body as live SQL. Blanking it as an
            # ordinary comment would smuggle whatever it contains past every check.
            if sql[i + 2 : i + 3] == "!":
                raise _GuardReject(
                    "MySQL executable comments ('/*! ... */') are not allowed",
                    _REMEDIATION["mysql_exec_comment"],
                )
            end = sql.find("*/", i + 2)
            i = n if end == -1 else end + 2
            emit(" ")
        elif sql[i] == "'":  # single-quoted string literal
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":  # doubled '' escape
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            emit(" ")
            i = j
        elif sql[i] == '"':  # double-quoted identifier — keep content, drop quotes
            j, buf = i + 1, []
            closed = False
            while j < n:
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':  # doubled "" escape
                        buf.append('"')
                        j += 2
                        continue
                    j += 1
                    closed = True
                    break
                buf.append(sql[j])
                j += 1
            # A pathological identifier like "a;b" reduces to a;b and trips the
            # multi-statement check — a deliberate, safe-direction hardening choice.
            #
            # The quotes are ALSO the token delimiters, on BOTH ends: a delimited
            # identifier is self-delimiting, so `FROM"pg_class"` and `"x"INTO` are both
            # valid SQL the engine runs. Dropping the quotes without re-supplying those
            # boundaries fuses neighbouring tokens into one — `FROM"pg_class"` ->
            # `FROMpg_class`, `"x"INTO` -> `xINTO` — which defeats the `\b` anchors every
            # deny-list pattern below relies on. The gate then stops *seeing* the token
            # rather than allowing it, so it returns no rejection at all. This is the same
            # invariant the docstring states for comments and literals ("never empty");
            # the identifier branch is simply the one place that was not honouring it.
            #
            # Re-supply a boundary only where the quote was actually separating two word
            # chars, so a qualified name stays one token: `t."col"` must neutralize to
            # `t.col`, not `t. col`. That structural fidelity is the neutralizer's job —
            # it removes hiding places without re-tokenizing the statement — and it is
            # pinned by an explicit output test, since no current rule distinguishes the
            # two spellings on its own.
            if _last_emitted(out).isalnum() or _last_emitted(out) == "_":
                emit(" ")
            # Record the span AROUND the content only, between the two separator emits, so
            # `quoted` names the identifier and never the boundary this branch re-supplied.
            start = width
            emit("".join(buf))
            # Record a span ONLY for an identifier whose closing delimiter we actually consumed.
            # An unterminated `"` swallows the rest of the statement into `buf`, and a span over
            # that tail would tell the niladic matcher to skip every keyword inside it — an
            # UNDER-refusal, the one direction this design must never fail in. It is reachable:
            # MySQL's default sql_mode treats `"` as a string delimiter with backslash escapes, so
            # `SELECT "a\"b" , current_user FROM t` re-opens a runaway identifier here while MySQL
            # executes it and returns CURRENT_USER(). No span means no skip, so the gate refuses.
            if closed:
                spans.append((start, width))
            nxt = sql[j] if j < n else ""
            if nxt.isalnum() or nxt == "_":
                emit(" ")
            i = j
        elif sql[i] == "$":  # dollar-quoted string literal ($$...$$ or $tag$...$tag$)
            # Only a `$tag$` with a MATCHING close delimiter is a literal we can blank.
            # An unterminated opener must NOT swallow to EOF — that would hide a trailing
            # `; DROP ...` from the multi-statement check (fail-open). Treating it as a
            # bare `$` instead leaves everything after it visible, so the scan fails safe
            # (the DB rejects an unterminated dollar-quote anyway).
            m = _DOLLAR_OPEN_RE.match(sql, i)
            close = sql.find(m.group(0), m.end()) if m else -1
            if m and close != -1:
                i = close + len(m.group(0))
                emit(" ")
            else:
                emit("$")
                i += 1
        else:
            emit(sql[i])
            i += 1
    raw = "".join(out)
    text = raw.strip()
    lead = len(raw) - len(raw.lstrip())
    limit = len(text)
    # Shift every span into the stripped frame, and DROP any the strip disturbed rather
    # than clamping it. Dropping is the safe direction: a missing span means the niladic
    # matcher does not skip that token, so the gate over-refuses. Clamping a span that ran
    # off either end would instead widen the skipped region and could hide a live keyword.
    kept = tuple(
        (s - lead, e - lead) for s, e in spans if 0 <= s - lead < e - lead <= limit
    )
    return _Neutralized(text, kept)


# Allowed opening keyword. `WITH` covers CTEs whose final clause is a SELECT.
# Leading `(` / whitespace is tolerated so a parenthesized set operation —
# `(SELECT 1) UNION (SELECT 2)` — is still recognized as read-only.
_READ_ONLY_OPEN_RE = re.compile(r"^[\s(]*(?:SELECT|WITH)\b", re.IGNORECASE)

# Deny-list of statement-level keywords that must NOT appear anywhere in the
# stripped (comments + literals removed) SQL.
#   - DML/DDL: writes / schema changes.
#   - TCL: `COMMIT`/`ROLLBACK`/`SAVEPOINT`/`RELEASE` can escape a read-only
#     transaction (a known bypass class for SQL-execution servers).
#     `BEGIN`/`START`/`END` are omitted — the opening-keyword check already rejects
#     anything not starting with SELECT/WITH, and `END` is a false-positive
#     landmine (`CASE ... END`).
#   - Session: `SET`/`RESET`/`DISCARD` corrupt pooled connection state.
#   - Pub/sub + locking: `LISTEN`/`NOTIFY`/`LOCK` aren't analytics primitives.
#   - Prepared: `PREPARE`/`DEALLOCATE` are an alternative query-stacking path.
#   - `INTO`: `SELECT ... INTO new_table` is a write that starts with SELECT, so
#     the opening-keyword check passes it — deny `INTO` to close that write path.
_DML_DDL_KEYWORDS = "INSERT|UPDATE|DELETE|MERGE|TRUNCATE|DROP|ALTER|CREATE|GRANT|REVOKE|COPY|CALL|VACUUM|REINDEX|CLUSTER|EXECUTE|INTO"
_TCL_KEYWORDS = "COMMIT|ROLLBACK|SAVEPOINT|RELEASE"
_SESSION_KEYWORDS = "RESET|DISCARD|SET"
_PUBSUB_LOCK_KEYWORDS = "LISTEN|NOTIFY|UNLISTEN|LOCK"
_PREPARED_KEYWORDS = "PREPARE|DEALLOCATE"
_DENY_KEYWORD_RE = re.compile(
    rf"\b({_DML_DDL_KEYWORDS}|{_TCL_KEYWORDS}|{_SESSION_KEYWORDS}|{_PUBSUB_LOCK_KEYWORDS}|{_PREPARED_KEYWORDS})\b",
    re.IGNORECASE,
)

# Row-level lock clauses inside an otherwise-valid SELECT. `FOR UPDATE`,
# `FOR SHARE`, `FOR NO KEY UPDATE`, `FOR KEY SHARE` — none belong in analytics.
_ROW_LOCK_RE = re.compile(r"\bFOR\s+(UPDATE|SHARE|NO\s+KEY\s+UPDATE|KEY\s+SHARE)\b", re.IGNORECASE)

# Dangerous function calls — these read server files, execute OS commands via
# `COPY ... FROM PROGRAM` (when callable), drain server-side IO, sleep to burn
# worker time, kill other backends, mutate session state via the function path
# that bypasses the `SET` keyword deny, hold session-survival advisory locks, or
# execute a nested SQL string passed as a function arg (the `query_to_xml(text)`
# family). Match against `name(` so identifiers sharing a prefix aren't matched.
_DANGEROUS_FN_RE = re.compile(
    r"\b("
    # Time wasters / DoS
    r"pg_sleep|pg_sleep_for|pg_sleep_until|"
    # Server-side file I/O
    r"pg_read_file|pg_read_binary_file|pg_read_server_files|pg_write_server_files|"
    r"pg_ls_dir|pg_stat_file|pg_ls_logdir|pg_ls_waldir|pg_ls_tmpdir|"
    # Large objects — full set including legacy `loread`/`lowrite` and the
    # open/seek/tell/close API that lets an attacker chain `lo_open` -> `loread`
    # to read arbitrary LO content without using `lo_export`.
    r"lo_export|lo_import|lo_create|lo_unlink|lo_get|lo_put|lo_from_bytea|"
    r"lo_open|lo_read|lo_write|lo_close|lo_lseek|lo_lseek64|lo_tell|lo_tell64|lo_truncate|lo_truncate64|"
    r"loread|lowrite|"
    # Remote SQL execution — `dblink\w*` catches every variant.
    r"dblink\w*|"
    # Shell out via COPY
    r"copy_program|"
    # Sequence mutation — `setval`/`nextval` WRITE (advance / reset a sequence),
    # a real data change that starts with SELECT and so slips the keyword deny.
    r"nextval|setval|"
    # Backend / process control
    r"pg_terminate_backend|pg_cancel_backend|pg_reload_conf|"
    r"pg_rotate_logfile|pg_logfile_rotate|"
    # Server / replication / stats control — reset monitoring counters, force a WAL
    # switch, or drop a replication slot (can break downstream replication). Same
    # side-effecting family as the log/conf calls above. `pg_stat_reset\w*` covers
    # `pg_stat_reset_shared` / `_single_table_counters` / etc.
    r"pg_stat_reset\w*|pg_stat_statements_reset|pg_switch_wal|"
    r"pg_create_restore_point|pg_drop_replication_slot|pg_replication_slot_advance|"
    # Session-state mutation that bypasses the `SET` keyword deny.
    r"set_config|current_setting|"
    # Session-survival advisory locks — survive connection return and can DoS.
    r"pg_advisory_lock|pg_advisory_xact_lock|"
    r"pg_advisory_unlock|pg_advisory_unlock_all|"
    # Nested-SQL execution via XML/JSON conversion — these execute the SQL passed
    # as a string argument server-side, bypassing the outer gate.
    r"query_to_xml|query_to_xmlschema|query_to_json|cursor_to_xml"
    r")\s*\(",
    re.IGNORECASE,
)


def check_read_only(sql: str | None) -> Refusal | None:
    """Return None if `sql` is a single safe read-only statement, else a `Refusal`.

    Every rejection is built with `refuse()` rather than `Refusal(...)`, so the reason comes from
    the contract's pinned table and no step here can classify itself.

    Rejection ladder (each step has its own detail AND its own remediation so the caller can
    correct — see `_REMEDIATION` for why one shared fix would not do):
      0. Empty SQL
      1. SQL longer than `_MAX_SQL_CHARS`
      1b. Dialect-ambiguous comment form (bare `--x`, MySQL `/*! ... */`) — raised
          from `_neutralize` because it can't be neutralized safely with one lexer
      2. Multi-statement (any `;` outside literals/comments, except one trailing `;`)
      3. Doesn't open with SELECT or WITH (leading `(` tolerated)
      4. Contains a forbidden keyword (DML/DDL/TCL/session/pub-sub/lock/prepared/INTO)
      5. Contains a row-level lock clause (`FOR UPDATE` etc.)
      6. Calls a dangerous function (`pg_sleep`, `pg_read_file`, `dblink`, ...)
    """
    if not sql or not sql.strip():
        return refuse(RULE_READ_ONLY, detail="empty statement", remediation=_REMEDIATION["empty"])

    if len(sql) > _MAX_SQL_CHARS:
        return refuse(
            RULE_READ_ONLY,
            detail=(
                f"SQL is {len(sql)} characters; the guard caps at {_MAX_SQL_CHARS}. "
                "Real analytics SQL fits well under this."
            ),
            remediation=_REMEDIATION["too_long"],
        )

    # Blank out comments and string / dollar literals, and unwrap double-quoted
    # identifiers (`"pg_sleep"(10)` -> `pg_sleep(10)`), in one lexer-faithful pass so
    # nothing hidden inside a literal or comment can reach the checks below. See
    # `_neutralize` for why a single scan is required rather than layered regexes.
    try:
        stripped = _neutralize(sql).text
    except _GuardReject as reject:
        return refuse(RULE_READ_ONLY, detail=reject.detail, remediation=reject.remediation)

    # Allow exactly one trailing `;`. Any other `;` indicates a second statement —
    # the classic statement-stacking bypass (`COMMIT; DROP SCHEMA public CASCADE`).
    if stripped.endswith(";"):
        stripped = stripped[:-1].rstrip()
    if not stripped:
        return refuse(RULE_READ_ONLY, detail="empty statement", remediation=_REMEDIATION["empty"])
    if ";" in stripped:
        return refuse(
            RULE_READ_ONLY,
            detail="multiple statements are not allowed — send one SELECT",
            remediation=_REMEDIATION["multi_statement"],
        )

    if not _READ_ONLY_OPEN_RE.match(stripped):
        head = stripped.lstrip("(").split(None, 1)
        head = head[0].upper() if head else "?"
        return refuse(
            RULE_READ_ONLY,
            detail=f"only SELECT / WITH...SELECT is allowed (statement starts with {head})",
            remediation=_REMEDIATION["not_a_select"],
        )

    deny = _DENY_KEYWORD_RE.search(stripped)
    if deny:
        return refuse(
            RULE_READ_ONLY,
            detail=(
                f"keyword '{deny.group(1).upper()}' is not allowed — send a single "
                "SELECT / WITH...SELECT (no DML, DDL, transaction control, session-state, "
                "or prepared statements)"
            ),
            remediation=_REMEDIATION["denied_keyword"],
        )

    if _ROW_LOCK_RE.search(stripped):
        return refuse(
            RULE_READ_ONLY,
            detail="row-level lock clauses (FOR UPDATE / FOR SHARE / ...) are not allowed",
            remediation=_REMEDIATION["row_lock"],
        )

    fn = _DANGEROUS_FN_RE.search(stripped)
    if fn:
        return refuse(
            RULE_READ_ONLY,
            detail=(
                f"function `{fn.group(1)}` is not allowed — server-file / OS / "
                "process-control / sleep / remote-SQL functions are blocked"
            ),
            remediation=_REMEDIATION["dangerous_function"],
        )
    return None


# --- Recon / metadata functions (ACE-039) -----------------------------------
#
# FUNCTIONS ONLY. No schema names, no relation names, no relation-name prefixes, no system
# variables. Catalog RELATIONS are the model's to refuse, not this gate's: `check_table_scope`
# already rejects `pg_class` and `information_schema.tables` as tables the model does not declare,
# which is the same refusal reached by the gate that owns it. The relation half was therefore
# redundant where the model gate runs — and where it does not run, it was actively harmful: matching
# bare schema names on every engine refused a datasource whose `sys` schema holds ordinary user
# tables. A schema the model declares is a schema the caller may query.
#
# The residual is stated rather than hidden: on the vendored plugin layout `semantic_model.runtime`
# is absent by construction (it needs sqlglot and pydantic; the mirror is stdlib-only), so catalog
# relations have no gate there at all. Recon denial protects an operator from a caller who is not
# them, and on that layout the user owns the machine, the credentials and the role.
#
# Niladic keywords and register-type casts are in scope because they are the same primitive in a
# spelling the function matcher never sees: `current_user` is `current_user()` without the parens,
# and `'x'::regclass` is `to_regclass('x')` without them either — the cast errors when the object is
# absent, which is an object-existence oracle that rides inside a fully-scoped query.


def _recon_group(names: frozenset[str]) -> str:
    """Alternation body for a set of names, longest first.

    Longest-first is load-bearing rather than tidy: regex alternation takes the first branch that
    matches at a position, so `current_schema` listed before `current_schemas` would shadow it and
    the longer name would never match as a whole token.
    """
    return "|".join(sorted(names, key=len, reverse=True))


# Server / session / account metadata, matched only in their CALL form.
_RECON_PAREN_FNS = frozenset(
    {
        "version",
        "current_database",
        "current_schemas",  # pg, plural, takes a bool — the niladic `current_schema` is below
        "database",
        "schema",
        "user",  # MySQL user() / system_user() — the CALL form only
        "connection_id",
        "system_user",
        "current_account",  # snowflake
        "current_region",
        "current_version",
        "current_warehouse",
        "inet_server_addr",  # server / client network fingerprint (pg)
        "inet_server_port",
        "inet_client_addr",
        "inet_client_port",
    }
)

# Niladic metadata keywords — the special function spelled without parens. Bare `user`, `schema` and
# `database` are DELIBERATELY absent: they are Postgres niladic synonyms but far too common as
# intended column names, so only their call form (above) is denied. A known, minor
# username-fingerprint residual, taken knowingly in exchange for not refusing ordinary schemas.
_RECON_NILADIC = frozenset(
    {"current_user", "session_user", "current_catalog", "current_schema", "current_role"}
)

# The privilege-check family, enumerated rather than globbed. `has_\w+_privilege` also matched
# `has_active_privilege(...)` — a plausible user-defined function, and an over-refusal is the failure
# mode this list has already produced in real use.
_RECON_PRIVILEGE_FNS = frozenset(
    {
        "has_any_column_privilege",
        "has_column_privilege",
        "has_database_privilege",
        "has_foreign_data_wrapper_privilege",
        "has_function_privilege",
        "has_language_privilege",
        "has_largeobject_privilege",
        "has_parameter_privilege",
        "has_schema_privilege",
        "has_sequence_privilege",
        "has_server_privilege",
        "has_table_privilege",
        "has_tablespace_privilege",
        "has_type_privilege",
    }
)

# Call FAMILIES matched by prefix, each still requiring the trailing `(`. `pg_*` is a namespace ban
# and deliberately overlaps the dangerous-function list: `check_read_only` runs first and owns the
# label for what it names, and this is the backstop underneath — including for builtins that ship
# after this list is written. `to_reg*` resolves a name to an OID, the call form of the casts below.
_RECON_CALL_FAMILIES = (r"pg_\w+", r"to_reg\w+")

# Register types. `'secret'::regclass` errors when the object is absent, so it answers "does this
# exist?" without ever naming it in a FROM clause.
_RECON_REGTYPES = frozenset(
    {
        "regclass",
        "regcollation",
        "regconfig",
        "regdictionary",
        "regnamespace",
        "regoper",
        "regoperator",
        "regproc",
        "regprocedure",
        "regrole",
        "regtype",
    }
)

# A quoted name with a trailing `(` is STILL A CALL, so the paren matcher covers the niladic
# keywords too. Without that union `SELECT "current_schema"()` would be skipped by the niladic
# matcher (it is a quoted span) and missed by the paren matcher (`current_schemas` needs its `s`) —
# a hole the false-positive fix would otherwise open. This union is where the FP rule and the
# deny-list interact, and it is the only place they do.
_RECON_PAREN_RE = re.compile(
    r"\b("
    + _recon_group(_RECON_PAREN_FNS | _RECON_NILADIC | _RECON_PRIVILEGE_FNS)
    + "|"
    + "|".join(_RECON_CALL_FAMILIES)
    + r")\s*\(",
    re.IGNORECASE,
)
# `(?<!\.)` lets a qualified column through (`t.current_user`); the quoted-span check in
# `_recon_niladic_hit` handles the bare quoted spelling (`"current_user"`), which the neutralizer
# has by then unwrapped into something this pattern would otherwise match.
_RECON_NILADIC_RE = re.compile(
    r"(?<!\.)\b(" + _recon_group(_RECON_NILADIC) + r")\b", re.IGNORECASE
)
# Both cast spellings. `::reg*` anchors on the type name because `_neutralize` blanks the literal
# before it, so `'x'::regclass` arrives as ` ::regclass`. The `CAST(x AS reg*)` arm requires the
# closing paren so a column aliased `AS regclass` is not caught.
# The optional `\w+\s*\.\s*` is a SCHEMA QUALIFIER, and it is load-bearing:
# `'secret_table'::pg_catalog.regclass` is the same object-existence oracle in the spelling a
# deny-list that anchors straight after `::` never sees. `_neutralize` has already unwrapped quotes
# by this point, so `"pg_catalog"."regclass"` arrives here as `pg_catalog.regclass` and is covered
# by the same branch. The call matcher tolerated qualification already; this arm did not.
_RECON_CAST_RE = re.compile(
    r"::\s*(?:\w+\s*\.\s*)?(" + _recon_group(_RECON_REGTYPES) + r")\b"
    r"|\bAS\s+(?:\w+\s*\.\s*)?(" + _recon_group(_RECON_REGTYPES) + r")\s*\)",
    re.IGNORECASE,
)

_RECON_REMEDIATION = (
    "Remove the server-metadata call — query only the tables and columns your model declares. "
    "Server version, session identity, privilege probes and object-existence casts are never "
    "executed here."
)


def _recon_niladic_hit(neutral: _Neutralized) -> re.Match[str] | None:
    """The niladic keyword, skipping any occurrence that was a quoted identifier.

    The ONLY consumer of `_Neutralized.quoted`. A double-quoted bare word is unambiguously an
    identifier — `SELECT "current_user" FROM audit_log` reads a column — while the same word
    unquoted is the special function. The neutralizer drops the delimiters (that unwrapping is what
    keeps a welded `"pg_read_file"(...)` visible to the read-only gate), so the distinction survives
    only in the spans it reports.

    Containment must be FULL. A partial overlap means the span and the match disagree about where
    the token is, and the safe reading of a disagreement is not to skip.
    """
    for match in _RECON_NILADIC_RE.finditer(neutral.text):
        if not any(s <= match.start() and match.end() <= e for s, e in neutral.quoted):
            return match
    return None


def check_no_recon(sql: str | None) -> Refusal | None:
    """Return ``None`` when ``sql`` calls no metadata / recon function, else a ``Refusal``.

    Runs immediately after :func:`check_read_only` at the shared executor chokepoint, over the SAME
    neutralized text — comments and literals blanked, quoted identifiers unwrapped — so a recon token
    hidden in a string cannot smuggle past and a legitimate mention inside one cannot false-trip.
    No second parser: this module is regex over `_neutralize`, and the sqlglot pass lives in
    `semantic_model.runtime`, which the vendored mirror cannot import.

    Order is fixed, so the label is deterministic: a name on both this list and the
    dangerous-function list refuses as ``read_only``, because that gate runs first and owns what it
    names (principle 9).
    """
    if not sql or not sql.strip():
        return None  # empty — `check_read_only` owns that rejection, and its remediation

    try:
        neutral = _neutralize(sql)
    except _GuardReject as reject:
        # A statement we cannot read is not a statement we caught fingerprinting the server. It
        # fails closed either way, but as `undetermined`/`unparseable` — labelling it `recon` told
        # the caller it had tried something it had not, and handed it a recon fix for a parse
        # problem. Unreachable at the chokepoint, where `check_read_only` runs the same neutralizer
        # and refuses first; reachable, and covered, as a standalone call.
        return refuse(
            RULE_UNPARSEABLE, detail=reject.detail, remediation=reject.remediation
        )

    match = (
        _RECON_PAREN_RE.search(neutral.text)
        or _recon_niladic_hit(neutral)
        or _RECON_CAST_RE.search(neutral.text)
    )
    if match is None:
        return None

    # Echo the token the caller itself sent, never the alternatives. A refusal that lists what IS
    # allowed is a schema-listing endpoint.
    hit = match.group(0).strip(" .(:")
    return refuse(
        RULE_RECON,
        detail=f"metadata/recon access is not allowed (`{hit}`)",
        remediation=_RECON_REMEDIATION,
    )
