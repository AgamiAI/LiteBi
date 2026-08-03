"""
Tests for `sql_guard` — the hardened read-only / dangerous-SQL gate shared by the
stdio server, the HTTP/OAuth server, the agami-query skill, and cron.

Two things are pinned here:

  1. **Security.** Every write / DDL / transaction-control / session-state /
     dangerous-function / multi-statement / comment-or-quote-bypass vector is
     rejected — including the vectors that historically bypassed naive guards
     (comment-in-string, PG-quoted function names, comment-welded word boundaries).

  2. **No false positives.** A large corpus of the analytics SQL an assistant emits
     every day MUST pass. Over-tightening the deny-list silently degrades every
     query, so this corpus is the primary safety net.

Bare `pg_catalog` / `information_schema` / environment-introspection blocking is a
deferred follow-up (see `test_known_deferred_gaps_currently_pass` for the pinned
current behavior).

`sql_guard.check_read_only` returns None (safe) or a `guardrail.Refusal` (rejected).

Every reject corpus below is a module-level constant rather than an inline literal in its
`@pytest.mark.parametrize`. That is what lets `test_ace035_read_only_refusal.py` assert the refusal
contract over the WHOLE corpus by importing it, instead of copying ~150 strings into a second file
where the two would drift apart the first time a vector is added here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

import pytest
from guardrail import RULE_READ_ONLY
from sql_guard import _MAX_SQL_CHARS, _REMEDIATION, _neutralize, check_read_only

# ---------------------------------------------------------------------------
# Accept — valid single read-only statements
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select 1",  # case-insensitive
        "  SELECT 1  ",  # leading/trailing whitespace
        "SELECT 1;",  # one trailing semicolon allowed
        "SELECT 1 ; ",  # trailing semicolon with whitespace
        "WITH a AS (SELECT 1) SELECT * FROM a",  # CTE
        "(SELECT 1) UNION (SELECT 2)",  # parenthesized set operation
        "-- comment\nSELECT 1",  # leading line comment
        "/* block comment */ SELECT 1",  # leading block comment
        "/* multi\nline\ncomment */SELECT 1",  # multi-line block comment
        "SELECT 'a;b' FROM dual",  # semicolon inside string literal
        "SELECT 1 /* ;DROP TABLE x; */ FROM dual",  # semicolons inside comment
        "SELECT 1 -- ; DROP TABLE x",  # semicolon inside line comment
        # Identifiers whose substrings overlap DML/DDL keywords — the `\b`
        # word-boundary in the deny-list must NOT false-positive on these.
        "SELECT updated_at FROM users",
        "SELECT deleted_at, updated_at FROM accounts",
        "SELECT * FROM deleted_records",
        "SELECT drop_count FROM stats",
        "SELECT * FROM events WHERE event_name = 'user_created'",
        "SELECT * FROM jsonb_call_data WHERE id = 1",
    ],
)
def test_accepts_valid_selects(sql: str) -> None:
    assert check_read_only(sql) is None, f"Expected pass, got rejection: {sql!r}"


@pytest.mark.parametrize(
    "sql",
    [
        # Identifiers overlapping the expanded deny-list keywords.
        "SELECT committed_at FROM events",  # COMMIT
        "SELECT rollback_count FROM stats",  # ROLLBACK
        "SELECT begin_date, end_date FROM bookings",  # BEGIN / END
        "SELECT set_id FROM datasets",  # SET
        "SELECT reset_count FROM users",  # RESET
        "SELECT discard_pile FROM games",  # DISCARD
        "SELECT lock_version FROM accounts",  # LOCK
        "SELECT prepared_at FROM orders",  # PREPARE
        "SELECT pinto_color FROM cars",  # 'into' substring
        "SELECT intolerance_level FROM patients",  # starts with 'into'
        "SELECT set_config_id FROM audit",  # not a set_config( call
        "SELECT pg_advisory_lock_id FROM custom_table",  # column, not a call
        "SELECT a.id FROM accounts a JOIN contacts c ON a.id = c.account_id",  # aliases
    ],
)
def test_accepts_identifiers_with_keyword_substrings(sql: str) -> None:
    assert check_read_only(sql) is None, (
        f"Legit identifier-with-keyword-substring rejected: {sql!r}"
    )


@pytest.mark.parametrize(
    "sql",
    [
        # `CASE ... END` — END must NOT be in the deny-list.
        "SELECT CASE WHEN status = 'open' THEN 1 ELSE 0 END AS is_open FROM tickets",
        "SELECT name, CASE x WHEN 1 THEN 'a' WHEN 2 THEN 'b' ELSE 'c' END AS bucket FROM t",
        "SELECT SUM(CASE WHEN region = 'NA' THEN amount END) FROM orders",
        "SELECT CASE WHEN a > 0 THEN CASE WHEN b > 0 THEN 'pp' ELSE 'pn' END ELSE 'n' END FROM t",
    ],
)
def test_accepts_case_when_end(sql: str) -> None:
    assert check_read_only(sql) is None, f"CASE...END false-positived: {sql!r}"


# ---------------------------------------------------------------------------
# Reject — non-SELECT (DML / DDL)
# ---------------------------------------------------------------------------


REJECT_NON_SELECT = [
    "INSERT INTO x VALUES (1)",
    "UPDATE x SET a = 1",
    "DELETE FROM x",
    "DROP TABLE x",
    "TRUNCATE TABLE x",
    "ALTER TABLE x ADD COLUMN y INT",
    "CREATE TABLE x (a INT)",
    "COPY x FROM '/etc/passwd'",
    "GRANT SELECT ON x TO public",
    "REVOKE ALL ON x FROM public",
    "VACUUM x",
    "MERGE INTO x USING y ON x.a = y.a",
    "CALL my_proc()",
]


@pytest.mark.parametrize("sql", REJECT_NON_SELECT)
def test_rejects_non_select(sql: str) -> None:
    assert check_read_only(sql) is not None, f"Expected rejection: {sql!r}"


REJECT_MULTI_STATEMENT = [
    "SELECT 1; SELECT 2",
    "SELECT 1; DROP TABLE x",
    "SELECT 1;DELETE FROM x",  # no whitespace around semicolon
    "SELECT 1; -- second statement after comment\nSELECT 2",
    "WITH a AS (SELECT 1) SELECT * FROM a; DROP TABLE a",
]


@pytest.mark.parametrize("sql", REJECT_MULTI_STATEMENT)
def test_rejects_multi_statement(sql: str) -> None:
    assert check_read_only(sql) is not None, f"Expected rejection: {sql!r}"


REJECT_EMPTY = ["", "   ", None, "-- just a comment", "/* only a comment */"]


@pytest.mark.parametrize("sql", REJECT_EMPTY)
def test_rejects_empty(sql: Any) -> None:
    assert check_read_only(sql) is not None


# ---------------------------------------------------------------------------
# Reject — known bypasses
# ---------------------------------------------------------------------------


REJECT_COMMENT_IN_STRING = [
    # Comment-inside-string bypass: prior strip-order (comments first) let `--`
    # inside a literal eat through end-of-line and hide an injected `; DROP ...`.
    "SELECT '--'; DROP TABLE x",
    "SELECT 'foo' || '--'; UPDATE t SET a=1",
    "SELECT '-- not a comment' AS s; DELETE FROM t",
    "SELECT '--' ; DROP TABLE x",
]


@pytest.mark.parametrize("sql", REJECT_COMMENT_IN_STRING)
def test_rejects_comment_in_string_bypass(sql: str) -> None:
    assert check_read_only(sql) is not None, f"Comment-in-string bypass not caught: {sql!r}"


REJECT_HASH_COMMENT = [
    # `#` starts a line comment in MySQL/MariaDB and is an operator character in PostgreSQL
    # (bit-string XOR, geometric intersection), so the same bytes are a comment on one engine
    # and live SQL on another. This lexer has no dialect, so the form is refused rather than
    # read one of the two ways — the same call the bare `--x` case makes.
    "SELECT a FROM t # DROP TABLE t",       # comment body reads as DML on the non-comment engines
    "SELECT a FROM t # note; more",         # comment body hides a statement separator
    "SELECT a FROM t #no space",            # MySQL needs no space after `#`
    "SELECT a FROM t # trailing note",      # inert on both readings, still ambiguous
    "SELECT a # b FROM t",                  # the PostgreSQL operator reading — refused too
    "SELECT a FROM t\n# DROP TABLE t",      # comment on its own line
]


@pytest.mark.parametrize("sql", REJECT_HASH_COMMENT)
def test_rejects_hash_comment_as_dialect_ambiguous(sql: str) -> None:
    """Every `#` outside a literal is refused, and always for the ambiguity rather than for
    whatever its body happened to spell.

    The bug this closes: `#` was not a comment to the neutralizer, so the body reached the
    deny-list and the statement-separator check. `# DROP TABLE t` came back as "keyword 'DROP' is
    not allowed" and `# note; more` as "multiple statements are not allowed" — both refusals of
    valid MySQL, and both naming a fix that would not have helped, since neither the DROP nor the
    second statement was ever going to run.
    """
    refusal = check_read_only(sql)
    assert refusal is not None, f"ambiguous `#` form not caught: {sql!r}"
    assert refusal.rule == RULE_READ_ONLY
    assert refusal.detail == (
        "'#' is a comment in MySQL but an operator in PostgreSQL, so the text after it "
        "cannot be read the same way on both"
    )
    assert refusal.remediation == _REMEDIATION["hash_line_comment"]


@pytest.mark.parametrize(
    "sql",
    [
        # A `#` inside a literal is data, not syntax. The scan consumes the literal in its own
        # branch before the `#` branch can see it, so this must keep passing — the fix above must
        # not become a ban on the character.
        "SELECT '#' AS h FROM t",
        "SELECT a FROM t WHERE b = '# not a comment; really'",
        "SELECT a FROM t WHERE b = 'DROP TABLE t # nope'",
        'SELECT "col#name" FROM t',  # and inside a quoted identifier
    ],
)
def test_hash_inside_a_literal_is_still_data(sql: str) -> None:
    assert check_read_only(sql) is None, f"`#` in a literal must not be refused: {sql!r}"


REJECT_DATA_MODIFYING_CTES = [
    # Data-modifying CTEs — open with WITH so the opener alone lets them through;
    # the deny-list scan catches the DML keyword.
    "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x",
    "WITH x AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM x",
    "WITH x AS (UPDATE t SET a=1 RETURNING *) SELECT * FROM x",
    "WITH x AS (SELECT 1), y AS (DELETE FROM t RETURNING *) SELECT * FROM x, y",
]


@pytest.mark.parametrize("sql", REJECT_DATA_MODIFYING_CTES)
def test_rejects_data_modifying_ctes(sql: str) -> None:
    assert check_read_only(sql) is not None, f"DML CTE not caught: {sql!r}"


REJECT_TRANSACTION_CONTROL = [
    "COMMIT",
    "COMMIT; SELECT 1",
    "ROLLBACK",
    "BEGIN",
    "BEGIN TRANSACTION READ ONLY",
    "SAVEPOINT sp1",
    "RELEASE SAVEPOINT sp1",
    "START TRANSACTION",
    "END",
]


@pytest.mark.parametrize("sql", REJECT_TRANSACTION_CONTROL)
def test_rejects_transaction_control(sql: str) -> None:
    assert check_read_only(sql) is not None, f"TCL not blocked: {sql!r}"


REJECT_SESSION_STATE = [
    "SET statement_timeout = 0",
    "SET search_path = pg_catalog, public",
    "RESET ALL",
    "RESET statement_timeout",
    "DISCARD ALL",
    "DISCARD TEMP",
]


@pytest.mark.parametrize("sql", REJECT_SESSION_STATE)
def test_rejects_session_state(sql: str) -> None:
    assert check_read_only(sql) is not None, f"Session-state mutation not blocked: {sql!r}"


REJECT_PUBSUB_LOCK_PREPARED = [
    "LISTEN channel1",
    "NOTIFY channel1, 'payload'",
    "UNLISTEN channel1",
    "LOCK TABLE accounts IN ACCESS EXCLUSIVE MODE",
    "PREPARE plan1 AS SELECT 1",
    "DEALLOCATE plan1",
    "DEALLOCATE ALL",
    "WITH x AS (LISTEN ch) SELECT 1",  # deny-list catches it mid-statement
]


@pytest.mark.parametrize("sql", REJECT_PUBSUB_LOCK_PREPARED)
def test_rejects_pubsub_lock_prepared(sql: str) -> None:
    assert check_read_only(sql) is not None, f"pub/sub | lock | prepared not blocked: {sql!r}"


REJECT_ROW_LEVEL_LOCKS = [
    # `FOR UPDATE` / `FOR NO KEY UPDATE` are caught by the `UPDATE` keyword deny
    # (which runs before the row-lock rule) — still rejected, just with the
    # keyword message. `FOR SHARE` / `FOR KEY SHARE` fall through to the row-lock rule.
    "SELECT * FROM accounts FOR UPDATE",
    "SELECT id FROM accounts WHERE id = 1 FOR SHARE",
    "SELECT * FROM accounts FOR NO KEY UPDATE",
    "SELECT id FROM accounts FOR KEY SHARE OF accounts",
]


@pytest.mark.parametrize("sql", REJECT_ROW_LEVEL_LOCKS)
def test_rejects_row_level_locks(sql: str) -> None:
    assert check_read_only(sql) is not None, f"Row-level lock not blocked: {sql!r}"


REJECT_NAMED_ROW_LOCK = ["SELECT id FROM accounts FOR SHARE", "SELECT id FROM t FOR KEY SHARE"]


@pytest.mark.parametrize("sql", REJECT_NAMED_ROW_LOCK)
def test_row_lock_rule_names_the_lock(sql: str) -> None:
    # These use SHARE (not a deny keyword) so the row-lock rule is what fires.
    refusal = check_read_only(sql)
    assert refusal is not None and "lock" in refusal.detail.lower(), refusal


REJECT_SELECT_INTO = [
    "SELECT * INTO new_table FROM users",
    "SELECT id, email INTO archive_users FROM users WHERE deleted_at IS NOT NULL",
    "SELECT 1 INTO scratch FROM dual",
]


@pytest.mark.parametrize("sql", REJECT_SELECT_INTO)
def test_rejects_select_into_write_path(sql: str) -> None:
    refusal = check_read_only(sql)
    assert refusal is not None, f"SELECT INTO not blocked: {sql!r}"
    assert "INTO" in refusal.detail


REJECT_DANGEROUS_FUNCTIONS = [
        # Time-wasters / file I/O / OS exec / remote SQL / process control.
        "SELECT pg_sleep(10)",
        "SELECT pg_sleep_for('10s')",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT pg_read_binary_file('/etc/passwd')",
        "SELECT pg_ls_dir('/var/lib/postgresql')",
        "SELECT pg_stat_file('/etc/passwd')",
        "SELECT pg_read_server_files('/')",
        "SELECT pg_write_server_files('/tmp/x', 'data')",
        "SELECT lo_export(12345, '/tmp/leak')",
        "SELECT lo_import('/etc/passwd')",
        "SELECT dblink('host=evil.example.com', 'select 1')",
        "SELECT dblink_exec('host=evil.example.com', 'drop table x')",
        "SELECT pg_terminate_backend(123)",
        "SELECT pg_cancel_backend(123)",
        "SELECT pg_reload_conf()",
        # Sequence mutation — real writes that open with SELECT.
        "SELECT setval('users_id_seq', 100)",
        "SELECT nextval('order_seq')",
        # Server / replication / stats control.
        "SELECT pg_stat_reset()",
        "SELECT pg_stat_reset_shared('bgwriter')",
        "SELECT pg_stat_statements_reset()",
        "SELECT pg_switch_wal()",
        "SELECT pg_create_restore_point('x')",
        "SELECT pg_drop_replication_slot('s')",
        "SELECT pg_replication_slot_advance('s', '0/0')",
        # Post-audit additions.
        "SELECT set_config('statement_timeout', '0', false)",
        "SELECT current_setting('statement_timeout')",
        "SELECT pg_advisory_lock(1)",
        "SELECT pg_advisory_xact_lock(1)",
        "SELECT pg_advisory_unlock(1)",
        "SELECT pg_advisory_unlock_all()",
        "SELECT query_to_xml('SELECT * FROM pg_tables', false, false, '')",
        "SELECT query_to_xmlschema('SELECT 1', false, false, '')",
        "SELECT query_to_json('SELECT 1')",
        "SELECT cursor_to_xml('foo', 1, false, false, '')",
        "SELECT pg_rotate_logfile()",
        "SELECT pg_logfile_rotate()",
        "SELECT copy_program('cat /etc/passwd')",
]


@pytest.mark.parametrize("sql", REJECT_DANGEROUS_FUNCTIONS)
def test_rejects_dangerous_functions(sql: str) -> None:
    assert check_read_only(sql) is not None, f"Dangerous function not blocked: {sql!r}"


def over_length_payload() -> str:
    """SQL past the length cap. A function rather than a constant so the ~50KB string is built only
    by the tests that need it (here and the refusal-contract corpus), not at every import."""
    return "SELECT 1, " + ("a, " * 30_000) + "1"


def test_rejects_over_length_cap() -> None:
    payload = over_length_payload()
    assert len(payload) > _MAX_SQL_CHARS
    refusal = check_read_only(payload)
    assert refusal is not None
    assert "50000" in refusal.detail or "caps" in refusal.detail


def test_length_cap_exact_boundary() -> None:
    """At exactly `_MAX_SQL_CHARS` accept; one over must reject. Off-by-one guard."""
    at_cap = "SELECT 1" + (" " * (_MAX_SQL_CHARS - len("SELECT 1")))
    assert len(at_cap) == _MAX_SQL_CHARS
    assert check_read_only(at_cap) is None

    over_cap = at_cap + " "
    assert len(over_cap) == _MAX_SQL_CHARS + 1
    assert check_read_only(over_cap) is not None


# ---------------------------------------------------------------------------
# Adversarial / red-team
# ---------------------------------------------------------------------------


REJECT_QUOTED_DANGEROUS_FN = [
        # PG-quoted identifier for a dangerous function name. The gate must strip
        # the `"` chars (not the contents) so `"pg_sleep"(10)` reduces to `pg_sleep(10)`.
        'SELECT "pg_sleep"(10)',
        'SELECT "pg_sleep" ( 10 )',
        'SELECT "PG_SLEEP"(10)',
        'SELECT "Pg_Sleep"(10)',
        'SELECT pg_catalog."pg_sleep"(10)',
        'SELECT "pg_catalog".pg_sleep(10)',
        'SELECT "pg_catalog"."pg_sleep"(10)',
        "SELECT \"pg_read_file\"('/etc/passwd')",
        'SELECT "pg_terminate_backend"(123)',
        "SELECT \"set_config\"('statement_timeout', '0', false)",
        "SELECT \"dblink\"('host=evil', 'select 1')",
        'SELECT "pg_advisory_lock"(1)',
        "SELECT \"query_to_xml\"('SELECT 1', false, false, '')",
]


@pytest.mark.parametrize("sql", REJECT_QUOTED_DANGEROUS_FN)
def test_red_team_quoted_dangerous_fn_bypass(sql: str) -> None:
    assert check_read_only(sql) is not None, f"Quoted-fn bypass NOT blocked: {sql!r}"


REJECT_WELDED_QUOTED_DANGEROUS_FN = [
        # WELDED quoted identifier — no whitespace between the preceding keyword and the
        # opening `"`. A delimited identifier is self-delimiting in SQL (the quote IS the
        # token boundary), so these are valid statements the engine happily runs; verified
        # on PostgreSQL 16 (`SELECT"pg_read_file"('/tmp/x')` returns the file contents).
        # Dropping the quotes without re-supplying a separator fuses two tokens into one
        # (`FROM"pg_class"` -> `FROMpg_class`), which destroys the `\b` anchor every
        # deny-list pattern relies on and silently blinds the gate.
        "SELECT\"pg_read_file\"('/etc/passwd')",
        "SELECT*FROM\"pg_read_file\"('/etc/passwd')",
        'SELECT"pg_sleep"(10)',
        'SELECT"dblink"(\'host=evil\', \'select 1\')',
        'SELECT"pg_terminate_backend"(123)',
        'SELECT"set_config"(\'statement_timeout\', \'0\', false)',
        'SELECT 1 FROM"pg_class"WHERE"pg_sleep"(10) IS NULL',
        # The weld can also appear mid-statement, after a non-keyword word char.
        'SELECT a FROM t WHERE b="pg_sleep"(1)',
        # The CLOSING quote delimits too, so a keyword can hide on the trailing side.
        # `SELECT ... INTO <table>` is a write that opens with SELECT, which is exactly
        # why INTO is in the DML/DDL list; `\bINTO\b` does not match `xINTO`. Verified on
        # PostgreSQL 16: each of these creates a table holding the source rows.
        'SELECT "x"INTO evil FROM t',
        'SELECT"x"INTO evil FROM t',
        'SELECT t."x"INTO evil FROM t',
        'SELECT 1 AS"a"INTO evil',
        'WITH c AS (SELECT 1 AS "x")SELECT "x"INTO evil FROM c',
        # Row-lock rule, same root cause. FOR UPDATE survives only because bare UPDATE is
        # independently in the DML list; FOR SHARE has no such backstop.
        'SELECT * FROM t AS"a"FOR SHARE',
        'SELECT * FROM t AS"a"FOR KEY SHARE',
]


@pytest.mark.parametrize("sql", REJECT_WELDED_QUOTED_DANGEROUS_FN)
def test_red_team_welded_quoted_dangerous_fn_bypass(sql: str) -> None:
    """A quoted identifier welded to the preceding token must not escape the gate.

    Regression for the `_neutralize` weld: every pre-existing case in the corpus above
    happens to carry a space before the `"`, so the missing separator was invisible.
    """
    assert check_read_only(sql) is not None, f"Welded quoted-fn bypass NOT blocked: {sql!r}"


@pytest.mark.parametrize(
    "sql",
    [
        # The separator must be re-supplied ONLY where the quote was actually delimiting
        # two word chars. A blanket space would break qualified names — `t."col"` must stay
        # `t.col`, not become `t. col` — so these legitimate reads must still pass.
        'SELECT t."current_user" FROM t',
        'SELECT "order id" FROM orders',
        'SELECT "name", "email" FROM customers',
        'SELECT c."email" FROM customers c',
        'SELECT "schema"."table" FROM "schema"."table"',
        'SELECT a."b" FROM x a',
    ],
)
def test_welded_fix_does_not_over_block_legitimate_quoted_identifiers(sql: str) -> None:
    """The weld fix must not turn a qualified quoted column into a false positive."""
    assert check_read_only(sql) is None, f"Legitimate quoted identifier wrongly blocked: {sql!r}"


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        # A separator is re-supplied ONLY where the quote actually separated two word
        # chars — so a qualified name survives as ONE token.
        ('SELECT t."current_user" FROM t', "SELECT t.current_user FROM t"),
        ('SELECT "schema"."table" FROM "schema"."table"', "SELECT schema.table FROM schema.table"),
        ('SELECT c."email" FROM customers c', "SELECT c.email FROM customers c"),
        # ...and IS re-supplied on both sides where it was separating word chars.
        ('SELECT * FROM"pg_class"', "SELECT * FROM pg_class"),
        ('SELECT "x"INTO evil FROM t', "SELECT x INTO evil FROM t"),
        ('SELECT 1 AS"a"INTO evil', "SELECT 1 AS a INTO evil"),
    ],
)
def test_neutralize_preserves_token_structure(sql: str, expected: str) -> None:
    """Pin the *shape* of the neutralized text, not just the gate's yes/no.

    Every rule here is `\\b`-anchored, so a blanket separator on both sides would block the
    same attacks and pass the same negatives — the gate-level tests alone cannot tell the
    two designs apart, and a future refactor could silently swap one for the other. What a
    blanket space would change is token *structure*: `t."col"` would become `t. col`, two
    tokens where the statement meant one. Any rule that reasons about qualification (a
    pattern anchored on a preceding `.`, say) would then read a qualified column as a bare
    identifier. Asserting the neutralized string keeps that decision checkable.

    The assertion compares `.text` rather than the bare return value: ACE-039 gave
    `_neutralize` a second output (the quoted-identifier spans the recon gate's niladic
    matcher consults) and folded the caller's `.strip()` inside, so one coordinate frame
    exists instead of three. A deliberate contract change, not a weakening — the six cases
    and the property they pin are untouched.
    """
    assert _neutralize(sql).text == expected


REJECT_COMMENT_BREAKS_GATE = [
    # Block comments between keywords must not defeat `\b` word boundaries —
    # comments are stripped to SPACE, not empty.
    "SELECT 1 FROM users WHERE id IN (SELECT/**/pg_sleep(10))",
    "SELECT 1 FROM users WHERE x = (SELECT/**/pg_read_file('/etc/passwd'))",
    'SELECT 1 FROM users WHERE id IN (SELECT "pg_sleep"(10))',
    "SELECT 1 INTO/**/new_table FROM users",
]


@pytest.mark.parametrize("sql", REJECT_COMMENT_BREAKS_GATE)
def test_red_team_comment_breaks_gate(sql: str) -> None:
    assert check_read_only(sql) is not None, f"Comment-bypass NOT blocked: {sql!r}"


@pytest.mark.parametrize(
    "sql",
    [
        # Legitimate SELECT with Unicode / non-space whitespace separators must PASS
        # (`\s` is Unicode-aware for str patterns in Python's re).
        "SELECT" + chr(0x00A0) + "1",  # NBSP
        "SELECT" + chr(0x2028) + "1",  # line separator
        "SELECT\t*\nFROM\nusers",
        "SELECT\r\n*\r\nFROM\r\nusers",
    ],
)
def test_red_team_unicode_whitespace_accepts_valid(sql: str) -> None:
    assert check_read_only(sql) is None, f"Unicode-whitespace SELECT should pass: {sql!r}"


REJECT_UNICODE_WHITESPACE_DENY = [
    # Deny-list keywords separated by Unicode whitespace must STILL reject.
    "SELECT\t1\tINTO\tnew_table FROM users",
    "SELECT" + chr(0x00A0) + "1" + chr(0x00A0) + "INTO" + chr(0x00A0) + "new_table FROM users",
]


@pytest.mark.parametrize("sql", REJECT_UNICODE_WHITESPACE_DENY)
def test_red_team_unicode_whitespace_does_not_bypass_deny(sql: str) -> None:
    assert check_read_only(sql) is not None, f"Unicode-whitespace INTO bypass: {sql!r}"


REJECT_DOLLAR_QUOTED_STACKING = [
        # Dollar-quote statement stacking. A `'` inside a `$$...$$` / `$tag$...$tag$`
        # body used to desync the single-quote stripper and smuggle a real second
        # statement past the multi-statement check. The lexer-faithful scan
        # neutralizes the whole dollar body, so the injected `;` stays visible and is
        # blocked. (Regression: arbitrary statement execution regardless of DB role.)
        r"SELECT $$'$$ ; DROP TABLE users -- '",
        r"SELECT $tag$'$tag$ ; DELETE FROM accounts -- '",
        r"SELECT $$won't$$ ; CREATE TABLE evil(x int) -- '",
        # Numeric-tag `$1$` is not a real PG dollar-quote (tags can't start with a
        # digit), so the raw payload is a DB syntax error — but the scan still treats
        # any `$…$` span as opaque, so a `'` inside can't desync it and expose the `;`.
        r"SELECT $1$'$1$ ; DROP TABLE users -- '",
        # An UNTERMINATED `$tag$` opener must not blank to EOF and swallow the trailing
        # `; DROP ...` (a fail-open the `$…$`-as-opaque broadening introduced): with no
        # matching close tag, the `;` stays visible and trips the guard.
        r"SELECT 1 AS $tag$; DROP TABLE users",
        r"SELECT 1 $$x ; DROP TABLE users",
        # A `$$` that OPENS inside a line comment must not be treated as a real
        # dollar-quote and swallow the statement that follows the newline.
        "SELECT 1 --$$\n;DROP TABLE x--$$",
        # A DO-block is procedural, not a SELECT — rejected on the opening-keyword check.
        "DO $$ BEGIN DELETE FROM users; END $$",
]


@pytest.mark.parametrize("sql", REJECT_DOLLAR_QUOTED_STACKING)
def test_red_team_dollar_quoted_stacking_blocked(sql: str) -> None:
    assert check_read_only(sql) is not None, f"Dollar-quote stacking NOT blocked: {sql!r}"


REJECT_MYSQL_COMMENT_LEXING = [
    # MySQL/MariaDB `--` is a comment ONLY when followed by whitespace; `--0` is
    # `- -0`, so blanking it PG-style would hide the stacked `;DROP`. Refuse the
    # dialect-ambiguous form.
    "SELECT 1--0;DROP TABLE users",
    "SELECT 1--x\nUNION SELECT 2",
    # MySQL executable comments run their body as live SQL server-side.
    "SELECT 1/*!;DROP TABLE t*/",
    "SELECT 1/*!50000 ;DROP TABLE t*/",
    "SELECT * FROM t /*!UNION SELECT * FROM secrets*/",
]


@pytest.mark.parametrize("sql", REJECT_MYSQL_COMMENT_LEXING)
def test_red_team_mysql_comment_lexing_blocked(sql: str) -> None:
    assert check_read_only(sql) is not None, f"MySQL comment bypass NOT blocked: {sql!r}"


@pytest.mark.parametrize(
    "sql",
    [
        # The whitespace-led `--` comment is a comment in BOTH dialects — must pass.
        "SELECT 1 -- 0;DROP\n",
        "SELECT 1 --\tvalue FROM t",
        # A plain `/* ... */` block (not `/*!`) stays a normal comment.
        "SELECT 1 /* note */ FROM t",
    ],
)
def test_unambiguous_comments_still_pass(sql: str) -> None:
    assert check_read_only(sql) is None, f"Legit comment wrongly rejected: {sql!r}"


REJECT_STACKED_KEYWORDS = [
    "SELECT 1; SET statement_timeout = 0; SELECT 2",
    "SELECT pg_sleep(10) FROM dual",
    "WITH x AS (LISTEN ch) SELECT 1",
    "SELECT 1 FROM (SELECT NOTIFY ch1, 'p' AS y) x",
]


@pytest.mark.parametrize("sql", REJECT_STACKED_KEYWORDS)
def test_red_team_stacked_keywords(sql: str) -> None:
    assert check_read_only(sql) is not None, f"Stacked attack not blocked: {sql!r}"


def _every_reject_list(namespace: dict[str, Any]) -> list[Any]:
    """Every `REJECT_*` list in `namespace`, flattened, in declaration order.

    Built by scanning rather than hand-unioned. The union was 19 `*REJECT_…` lines a contributor had
    to remember to extend, and it is what makes the exhaustive-remediation claim in
    `test_ace035_read_only_refusal.py` true — so a new `REJECT_FOO` list left out of it did not fail
    anything, it silently shrank the corpus that file believes it is proving the contract over. The
    only existing guard was a `len > 120` floor, which a single dropped list of four vectors clears
    comfortably.

    `dict.fromkeys` de-duplicates while preserving order: a vector that legitimately appears in two
    categories should be parametrized once, and pytest ids must stay unique.
    """
    return list(
        dict.fromkeys(
            sql
            for name, value in namespace.items()
            if name.startswith("REJECT_") and name != "REJECT_CORPUS" and isinstance(value, list)
            for sql in value
        )
    )


# Every rejected statement in this file, in one list. `test_ace035_read_only_refusal.py` imports it
# to assert the refusal contract holds for the whole corpus rather than for a sampled few — so a new
# vector added to any list above is automatically held to the contract too. The over-length payload
# is appended there (it is generated, not a literal).
#
# Assembled here, at the bottom of the reject section, so it sees every list declared above it.
# `test_the_corpus_is_every_reject_list_in_this_module` re-runs the scan after the WHOLE module has
# loaded, which is what catches the remaining hole: a `REJECT_FOO` declared *below* this line.
REJECT_CORPUS: list[Any] = _every_reject_list(globals())


def test_the_corpus_is_every_reject_list_in_this_module() -> None:
    """The corpus, re-derived after the whole module has loaded, must equal the one assembled above.

    That is the only way to see a `REJECT_*` list declared BELOW the assembly line — the scan cannot
    include what does not exist yet, and a contributor appending a new category at the end of the
    file is the likeliest version of this mistake. A test rather than a lazy property because the
    corpus is consumed at import time by `@pytest.mark.parametrize`.
    """
    missing = [sql for sql in _every_reject_list(globals()) if sql not in REJECT_CORPUS]
    assert missing == [], (
        "a REJECT_* list is declared below REJECT_CORPUS and is not in it; move it above the "
        f"assembly line. Vectors dropped: {missing!r}"
    )


def test_the_corpus_scanner_finds_the_lists_and_only_the_lists() -> None:
    """The scanner is what makes the corpus self-maintaining, so it has to be shown working.

    Three shapes at once: a new `REJECT_*` list is picked up, a non-`REJECT_` list is not, and a
    duplicated vector appears once (pytest ids must stay unique).
    """
    scanned = _every_reject_list({
        "REJECT_ONE": ["a", "b"],
        "REJECT_TWO": ["b", "c"],  # 'b' is also in REJECT_ONE
        "ACCEPT_ONE": ["SELECT 1"],
        "REJECT_CORPUS": ["never"],  # the assembly itself is excluded by name
        "REJECT_NOT_A_LIST": "a",
    })
    assert scanned == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# False-positive guard — the analytics SQL an assistant emits every day. A regex
# regression that broke any of these would silently degrade every query.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        # ----- Time-series rollups -----
        "SELECT date_trunc('day', created_at) AS d, COUNT(*) FROM orders GROUP BY d ORDER BY d DESC LIMIT 30",
        "SELECT date_trunc('month', o.created_at), SUM(o.amount) FROM orders o GROUP BY 1",
        "SELECT EXTRACT(YEAR FROM created_at) AS yr, EXTRACT(MONTH FROM created_at) AS mo, COUNT(*) FROM events GROUP BY yr, mo",
        "SELECT EXTRACT(DOW FROM o.created_at) AS dow, COUNT(*) FROM orders o GROUP BY dow",
        # ----- Conditional aggregation -----
        "SELECT SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END) FROM invoices",
        "SELECT COUNT(*) FILTER (WHERE status = 'open') FROM tickets",
        "SELECT status, COUNT(*) AS n FROM tickets GROUP BY status",
        # ----- Window functions -----
        "SELECT id, ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY created_at DESC) AS rn FROM orders",
        "SELECT id, RANK() OVER (ORDER BY revenue DESC) FROM accounts",
        "SELECT customer_id, amount, SUM(amount) OVER (PARTITION BY customer_id ORDER BY created_at ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cumulative FROM orders",
        # ----- CTEs -----
        "WITH monthly AS (SELECT date_trunc('month', created_at) AS m, COUNT(*) AS n FROM orders GROUP BY 1) SELECT * FROM monthly ORDER BY m",
        "WITH t1 AS (SELECT id FROM users), t2 AS (SELECT id FROM accounts) SELECT * FROM t1 UNION ALL SELECT * FROM t2",
        # ----- Recursive CTE -----
        "WITH RECURSIVE org_tree(id, parent_id, depth) AS (SELECT id, parent_id, 0 FROM departments WHERE parent_id IS NULL UNION ALL SELECT d.id, d.parent_id, t.depth+1 FROM departments d JOIN org_tree t ON d.parent_id = t.id) SELECT * FROM org_tree",
        # ----- Multi-join -----
        "SELECT u.email, COUNT(o.id) FROM users u LEFT JOIN orders o ON u.id = o.customer_id GROUP BY u.email HAVING COUNT(o.id) > 5",
        "SELECT a.name, c.email FROM accounts a JOIN contacts c ON a.id = c.account_id LEFT JOIN opportunities o ON o.account_id = a.id",
        # ----- Set operations -----
        "SELECT id FROM users WHERE active UNION SELECT id FROM admins",
        "SELECT id FROM customers EXCEPT SELECT customer_id FROM churned_customers",
        "SELECT product_id FROM orders INTERSECT SELECT product_id FROM returns",
        # ----- String / array / JSON functions -----
        "SELECT REGEXP_REPLACE(email, '@.*', '') FROM users",
        "SELECT data->>'name' FROM events WHERE data ? 'id'",
        "SELECT array_agg(DISTINCT status) FROM tickets",
        "SELECT jsonb_array_length(items) FROM orders",
        # ----- Casts & arithmetic -----
        "SELECT CAST(amount AS DECIMAL(10,2)) FROM invoices",
        "SELECT amount::numeric * 1.18 AS amount_with_tax FROM orders",
        "SELECT NULLIF(email, '') FROM users",
        "SELECT COALESCE(phone, mobile, 'unknown') FROM contacts",
        # ----- Subqueries -----
        "SELECT * FROM users WHERE id IN (SELECT user_id FROM admin_users WHERE active)",
        "SELECT name, (SELECT COUNT(*) FROM orders o WHERE o.user_id = u.id) AS order_count FROM users u",
        "SELECT id FROM users WHERE EXISTS (SELECT 1 FROM orders WHERE orders.user_id = users.id)",
        # ----- Date/time functions -----
        "SELECT NOW(), CURRENT_DATE, CURRENT_TIMESTAMP",
        "SELECT created_at + INTERVAL '7 days' FROM events",
        "SELECT created_at AT TIME ZONE 'UTC' FROM events",
        # ----- Aliases that overlap reserved-substring patterns -----
        "SELECT u.created_at, u.updated_at, u.deleted_at FROM users u",
        "SELECT 1 AS dropped, 2 AS truncated FROM dual",
        "SELECT prepared_count, committed_at, rollback_total FROM stats",
        # ----- Comments mid-query (legit) -----
        "SELECT id /* primary key */, email FROM users",
        "SELECT id, /* date created */ created_at FROM users",
        "-- top-of-file comment\nSELECT 1",
        "SELECT 1 -- trailing comment",
        "/* block */ SELECT /* inline */ 1 /* end */",
        # ----- PG-quoted column names that are legit -----
        'SELECT "user_count" FROM stats',
        'SELECT "order date", "total amount" FROM legacy_orders',
        # ----- LATERAL joins -----
        "SELECT u.id, latest.created_at FROM users u, LATERAL (SELECT created_at FROM orders WHERE user_id = u.id ORDER BY created_at DESC LIMIT 1) latest",
        # ----- DISTINCT ON -----
        "SELECT DISTINCT ON (customer_id) customer_id, created_at, amount FROM orders ORDER BY customer_id, created_at DESC",
        # ----- generate_series -----
        "SELECT d::date FROM generate_series('2026-01-01'::date, '2026-12-31'::date, INTERVAL '1 day') AS d",
        # ----- Dollar-quoted string CONSTANTS (a value, not executable code). The
        # keywords/`;` inside are inert data; blocking these was an old false positive. -----
        "SELECT $$plain label$$ AS note FROM stats",
        "SELECT $tag$O'Brien$tag$ AS name",
        "SELECT $$multi\nline\ntext$$ AS body FROM docs",
        # ----- Positional parameters ($1, $2) are NOT dollar-quote openers -----
        "SELECT id, name FROM users WHERE id = $1",
        "SELECT * FROM orders WHERE customer_id = $1 AND status = $2",
    ],
)
def test_false_positive_guard_legitimate_analytics_sql(sql: str) -> None:
    assert check_read_only(sql) is None, f"FALSE POSITIVE — legit analytics SQL rejected: {sql!r}"


# ---------------------------------------------------------------------------
# Deferred-scope pins — behaviors intentionally NOT hardened in this pass, so a
# future change that adds them is a conscious decision (and updates this test).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        # Bare `pg_catalog` / `information_schema` probing and environment-introspection
        # keywords/functions are NOT blocked by this gate (schema-scoping is a deferred
        # follow-up — see the plan). They currently PASS. When the follow-up lands, move
        # these into a reject test.
        "SELECT * FROM pg_tables",
        "SELECT * FROM information_schema.tables",
        "SELECT current_user",
        "SELECT current_database()",
    ],
)
def test_known_deferred_gaps_currently_pass(sql: str) -> None:
    assert check_read_only(sql) is None, (
        f"Expected this deferred-scope query to pass the read-only gate for now: {sql!r}"
    )


# ---------------------------------------------------------------------------
# Chokepoint enforcement — the guard is wired into execute_sql.py::main and is
# NOT bypassable via --no-safety (that flag only skips the semantic-model pass).
# This is the regression that closes the direct-`python -m execute_sql` gap.
# ---------------------------------------------------------------------------


def _run_executor(sql: str, tmp_path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "execute_sql", "--profile", "nonexistent", "--sql", sql, *extra],
        capture_output=True,
        text=True,
        timeout=60,
        # Isolate artifacts dir so bootstrap() never touches the real home dir.
        env={**os.environ, "AGAMI_ARTIFACTS_DIR": str(tmp_path)},
    )


@pytest.mark.parametrize("extra", [(), ("--no-safety",)])
def test_executor_blocks_dangerous_sql_even_with_no_safety(tmp_path, extra) -> None:
    """A write/DDL must be rejected by the executor BEFORE credentials are loaded,
    regardless of --no-safety. Proves the hard gate is at the shared chokepoint."""
    proc = _run_executor("DROP TABLE secrets", tmp_path, *extra)
    assert proc.returncode != 0, proc.stdout
    # The guard emits the contract Refusal as one JSON object on stderr.
    payload = None
    for line in proc.stderr.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except ValueError:
                continue
    assert payload is not None, f"no JSON refusal on stderr; got: {proc.stderr!r}"
    assert payload["refusal"]["rule"] == RULE_READ_ONLY, payload


def test_executor_dangerous_function_blocked(tmp_path) -> None:
    proc = _run_executor("SELECT pg_read_file('/etc/passwd')", tmp_path)
    assert proc.returncode != 0
    assert json.loads(proc.stderr.strip())["refusal"]["rule"] == RULE_READ_ONLY


def test_executor_lets_valid_select_past_the_gate(tmp_path) -> None:
    """A valid SELECT is NOT rejected by the read-only gate — it proceeds to the
    credential step and fails there instead (proving the gate didn't block it)."""
    proc = _run_executor("SELECT 1", tmp_path)
    # It should fail (no such profile / credentials), but NOT with a refusal from the guard.
    assert '"refusal"' not in proc.stderr, (
        f"valid SELECT was wrongly blocked by the read-only gate: {proc.stderr!r}"
    )
