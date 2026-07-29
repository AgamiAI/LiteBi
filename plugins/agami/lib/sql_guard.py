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
statement, else a short human-readable reason string. Callers decide how to wrap it
(the MCP tools attach `kind="permission"`).
"""

from __future__ import annotations

import re

# Hard cap on SQL length. Prevents a compromised client from POSTing a multi-MB
# SQL blob that takes the parser / planner / this gate down a slow path. Real
# analytics SQL fits in ~10KB; 50KB is conservative.
_MAX_SQL_CHARS = 50_000

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
    (see the MySQL comment forms in `_neutralize`). Carries the caller-facing reason.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _neutralize(sql: str) -> str:
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
                    "(bare '--x' is a comment in Postgres but an operator in MySQL)"
                )
            j = i + 2
            while j < n and sql[j] not in "\r\n":
                j += 1
            out.append(" ")
            i = j
        elif two == "/*":  # block comment
            # `/*! ... */` (and versioned `/*!NNNNN ... */`) is a MySQL *executable*
            # comment — the server runs its body as live SQL. Blanking it as an
            # ordinary comment would smuggle whatever it contains past every check.
            if sql[i + 2 : i + 3] == "!":
                raise _GuardReject("MySQL executable comments ('/*! ... */') are not allowed")
            end = sql.find("*/", i + 2)
            i = n if end == -1 else end + 2
            out.append(" ")
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
            out.append(" ")
            i = j
        elif sql[i] == '"':  # double-quoted identifier — keep content, drop quotes
            j, buf = i + 1, []
            while j < n:
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':  # doubled "" escape
                        buf.append('"')
                        j += 2
                        continue
                    j += 1
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
                out.append(" ")
            out.append("".join(buf))
            nxt = sql[j] if j < n else ""
            if nxt.isalnum() or nxt == "_":
                out.append(" ")
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
                out.append(" ")
            else:
                out.append("$")
                i += 1
        else:
            out.append(sql[i])
            i += 1
    return "".join(out)


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


def check_read_only(sql: str | None) -> str | None:
    """Return None if `sql` is a single safe read-only statement, else a reason string.

    Rejection ladder (each step has its own message so the caller can correct):
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
        return "empty statement"

    if len(sql) > _MAX_SQL_CHARS:
        return (
            f"SQL is {len(sql)} characters; the guard caps at {_MAX_SQL_CHARS}. "
            "Real analytics SQL fits well under this."
        )

    # Blank out comments and string / dollar literals, and unwrap double-quoted
    # identifiers (`"pg_sleep"(10)` -> `pg_sleep(10)`), in one lexer-faithful pass so
    # nothing hidden inside a literal or comment can reach the checks below. See
    # `_neutralize` for why a single scan is required rather than layered regexes.
    try:
        stripped = _neutralize(sql).strip()
    except _GuardReject as reject:
        return reject.reason

    # Allow exactly one trailing `;`. Any other `;` indicates a second statement —
    # the classic statement-stacking bypass (`COMMIT; DROP SCHEMA public CASCADE`).
    if stripped.endswith(";"):
        stripped = stripped[:-1].rstrip()
    if not stripped:
        return "empty statement"
    if ";" in stripped:
        return "multiple statements are not allowed — send one SELECT"

    if not _READ_ONLY_OPEN_RE.match(stripped):
        head = stripped.lstrip("(").split(None, 1)
        head = head[0].upper() if head else "?"
        return f"only SELECT / WITH...SELECT is allowed (statement starts with {head})"

    deny = _DENY_KEYWORD_RE.search(stripped)
    if deny:
        return (
            f"keyword '{deny.group(1).upper()}' is not allowed — send a single "
            "SELECT / WITH...SELECT (no DML, DDL, transaction control, session-state, "
            "or prepared statements)"
        )

    if _ROW_LOCK_RE.search(stripped):
        return "row-level lock clauses (FOR UPDATE / FOR SHARE / ...) are not allowed"

    fn = _DANGEROUS_FN_RE.search(stripped)
    if fn:
        return (
            f"function `{fn.group(1)}` is not allowed — server-file / OS / "
            "process-control / sleep / remote-SQL functions are blocked"
        )
    return None
