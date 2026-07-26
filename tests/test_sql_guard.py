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

The recon / metadata deny-list (`sql_guard.check_no_recon`) is the companion gate —
`version()`, `current_user`, `information_schema`, `pg_*` catalog relations are refused
(`recon`). Its corpus below pins both the must-refuse vectors (no false negatives /
info-leaks) and the must-pass analytics look-alikes (no false positives).

`sql_guard.check_read_only` returns None (safe) or a safety Verdict (rejected);
`sql_guard.check_no_recon` returns None or a `recon` safety Verdict.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

import pytest
import sql_guard
from sql_guard import _MAX_SQL_CHARS, check_no_recon, check_read_only

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


@pytest.mark.parametrize(
    "sql",
    [
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
    ],
)
def test_rejects_non_select(sql: str) -> None:
    assert check_read_only(sql) is not None, f"Expected rejection: {sql!r}"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; SELECT 2",
        "SELECT 1; DROP TABLE x",
        "SELECT 1;DELETE FROM x",  # no whitespace around semicolon
        "SELECT 1; -- second statement after comment\nSELECT 2",
        "WITH a AS (SELECT 1) SELECT * FROM a; DROP TABLE a",
    ],
)
def test_rejects_multi_statement(sql: str) -> None:
    assert check_read_only(sql) is not None, f"Expected rejection: {sql!r}"


@pytest.mark.parametrize("sql", ["", "   ", None, "-- just a comment", "/* only a comment */"])
def test_rejects_empty(sql: Any) -> None:
    assert check_read_only(sql) is not None


# ---------------------------------------------------------------------------
# Reject — known bypasses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        # Comment-inside-string bypass: prior strip-order (comments first) let `--`
        # inside a literal eat through end-of-line and hide an injected `; DROP ...`.
        "SELECT '--'; DROP TABLE x",
        "SELECT 'foo' || '--'; UPDATE t SET a=1",
        "SELECT '-- not a comment' AS s; DELETE FROM t",
        "SELECT '--' ; DROP TABLE x",
    ],
)
def test_rejects_comment_in_string_bypass(sql: str) -> None:
    assert check_read_only(sql) is not None, f"Comment-in-string bypass not caught: {sql!r}"


@pytest.mark.parametrize(
    "sql",
    [
        # Data-modifying CTEs — open with WITH so the opener alone lets them through;
        # the deny-list scan catches the DML keyword.
        "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x",
        "WITH x AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM x",
        "WITH x AS (UPDATE t SET a=1 RETURNING *) SELECT * FROM x",
        "WITH x AS (SELECT 1), y AS (DELETE FROM t RETURNING *) SELECT * FROM x, y",
    ],
)
def test_rejects_data_modifying_ctes(sql: str) -> None:
    assert check_read_only(sql) is not None, f"DML CTE not caught: {sql!r}"


@pytest.mark.parametrize(
    "sql",
    [
        "COMMIT",
        "COMMIT; SELECT 1",
        "ROLLBACK",
        "BEGIN",
        "BEGIN TRANSACTION READ ONLY",
        "SAVEPOINT sp1",
        "RELEASE SAVEPOINT sp1",
        "START TRANSACTION",
        "END",
    ],
)
def test_rejects_transaction_control(sql: str) -> None:
    assert check_read_only(sql) is not None, f"TCL not blocked: {sql!r}"


@pytest.mark.parametrize(
    "sql",
    [
        "SET statement_timeout = 0",
        "SET search_path = pg_catalog, public",
        "RESET ALL",
        "RESET statement_timeout",
        "DISCARD ALL",
        "DISCARD TEMP",
    ],
)
def test_rejects_session_state(sql: str) -> None:
    assert check_read_only(sql) is not None, f"Session-state mutation not blocked: {sql!r}"


@pytest.mark.parametrize(
    "sql",
    [
        "LISTEN channel1",
        "NOTIFY channel1, 'payload'",
        "UNLISTEN channel1",
        "LOCK TABLE accounts IN ACCESS EXCLUSIVE MODE",
        "PREPARE plan1 AS SELECT 1",
        "DEALLOCATE plan1",
        "DEALLOCATE ALL",
        "WITH x AS (LISTEN ch) SELECT 1",  # deny-list catches it mid-statement
    ],
)
def test_rejects_pubsub_lock_prepared(sql: str) -> None:
    assert check_read_only(sql) is not None, f"pub/sub | lock | prepared not blocked: {sql!r}"


@pytest.mark.parametrize(
    "sql",
    [
        # `FOR UPDATE` / `FOR NO KEY UPDATE` are caught by the `UPDATE` keyword deny
        # (which runs before the row-lock rule) — still rejected, just with the
        # keyword message. `FOR SHARE` / `FOR KEY SHARE` fall through to the row-lock rule.
        "SELECT * FROM accounts FOR UPDATE",
        "SELECT id FROM accounts WHERE id = 1 FOR SHARE",
        "SELECT * FROM accounts FOR NO KEY UPDATE",
        "SELECT id FROM accounts FOR KEY SHARE OF accounts",
    ],
)
def test_rejects_row_level_locks(sql: str) -> None:
    assert check_read_only(sql) is not None, f"Row-level lock not blocked: {sql!r}"


@pytest.mark.parametrize(
    "sql", ["SELECT id FROM accounts FOR SHARE", "SELECT id FROM t FOR KEY SHARE"]
)
def test_row_lock_rule_names_the_lock(sql: str) -> None:
    # These use SHARE (not a deny keyword) so the row-lock rule is what fires.
    v = check_read_only(sql)
    assert v is not None and "lock" in v.detail.lower(), v


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * INTO new_table FROM users",
        "SELECT id, email INTO archive_users FROM users WHERE deleted_at IS NOT NULL",
        "SELECT 1 INTO scratch FROM dual",
    ],
)
def test_rejects_select_into_write_path(sql: str) -> None:
    v = check_read_only(sql)
    assert v is not None, f"SELECT INTO not blocked: {sql!r}"
    assert "INTO" in v.detail


@pytest.mark.parametrize(
    "sql",
    [
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
    ],
)
def test_rejects_dangerous_functions(sql: str) -> None:
    assert check_read_only(sql) is not None, f"Dangerous function not blocked: {sql!r}"


def test_rejects_over_length_cap() -> None:
    payload = "SELECT 1, " + ("a, " * 30_000) + "1"
    assert len(payload) > _MAX_SQL_CHARS
    v = check_read_only(payload)
    assert v is not None
    assert "50000" in v.detail or "caps" in v.detail


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


@pytest.mark.parametrize(
    "sql",
    [
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
    ],
)
def test_red_team_quoted_dangerous_fn_bypass(sql: str) -> None:
    assert check_read_only(sql) is not None, f"Quoted-fn bypass NOT blocked: {sql!r}"


@pytest.mark.parametrize(
    "sql",
    [
        # Block comments between keywords must not defeat `\b` word boundaries —
        # comments are stripped to SPACE, not empty.
        "SELECT 1 FROM users WHERE id IN (SELECT/**/pg_sleep(10))",
        "SELECT 1 FROM users WHERE x = (SELECT/**/pg_read_file('/etc/passwd'))",
        'SELECT 1 FROM users WHERE id IN (SELECT "pg_sleep"(10))',
        "SELECT 1 INTO/**/new_table FROM users",
    ],
)
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


@pytest.mark.parametrize(
    "sql",
    [
        # Deny-list keywords separated by Unicode whitespace must STILL reject.
        "SELECT\t1\tINTO\tnew_table FROM users",
        "SELECT" + chr(0x00A0) + "1" + chr(0x00A0) + "INTO" + chr(0x00A0) + "new_table FROM users",
    ],
)
def test_red_team_unicode_whitespace_does_not_bypass_deny(sql: str) -> None:
    assert check_read_only(sql) is not None, f"Unicode-whitespace INTO bypass: {sql!r}"


@pytest.mark.parametrize(
    "sql",
    [
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
    ],
)
def test_red_team_dollar_quoted_stacking_blocked(sql: str) -> None:
    assert check_read_only(sql) is not None, f"Dollar-quote stacking NOT blocked: {sql!r}"


@pytest.mark.parametrize(
    "sql",
    [
        # MySQL/MariaDB `--` is a comment ONLY when followed by whitespace; `--0` is
        # `- -0`, so blanking it PG-style would hide the stacked `;DROP`. Refuse the
        # dialect-ambiguous form.
        "SELECT 1--0;DROP TABLE users",
        "SELECT 1--x\nUNION SELECT 2",
        # MySQL executable comments run their body as live SQL server-side.
        "SELECT 1/*!;DROP TABLE t*/",
        "SELECT 1/*!50000 ;DROP TABLE t*/",
        "SELECT * FROM t /*!UNION SELECT * FROM secrets*/",
    ],
)
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


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; SET statement_timeout = 0; SELECT 2",
        "SELECT pg_sleep(10) FROM dual",
        "WITH x AS (LISTEN ch) SELECT 1",
        "SELECT 1 FROM (SELECT NOTIFY ch1, 'p' AS y) x",
    ],
)
def test_red_team_stacked_keywords(sql: str) -> None:
    assert check_read_only(sql) is not None, f"Stacked attack not blocked: {sql!r}"


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
# Recon / metadata deny-list — check_no_recon. A false NEGATIVE here is a
# shipped info-leak (server fingerprint / schema enumeration across the LLM boundary);
# a false POSITIVE breaks a legitimate analytics query. Both are pinned exhaustively.
# (These queries pass check_read_only — recon is a separate, distinct `recon` gate.)
# ---------------------------------------------------------------------------

_RECON_REFUSE = [
    # paren fns / pg
    "SELECT version()",
    "SELECT current_database()",
    "SELECT current_schemas(true)",
    # paren fns / mysql
    "SELECT database()",
    "SELECT connection_id()",
    "SELECT system_user()",
    # paren fns / snowflake
    "SELECT current_account()",
    "SELECT current_warehouse()",
    "SELECT current_version()",
    # niladic (no-paren special values)
    "SELECT current_user",
    "SELECT session_user",
    "SELECT current_catalog",
    "SELECT current_schema",
    "SELECT current_role",
    "SELECT id FROM t WHERE owner = current_user",
    # schema-qualified catalog access
    "SELECT * FROM information_schema.tables",
    "SELECT column_name FROM information_schema.columns",
    "SELECT * FROM pg_catalog.pg_class",
    "SELECT * FROM account_usage.query_history",
    "SELECT * FROM mysql.user",
    # bare catalog relations
    "SELECT * FROM pg_tables",
    "SELECT relname FROM pg_class",
    "SELECT * FROM pg_stat_activity",
    "SELECT usename FROM pg_user",
    "SELECT * FROM pg_settings",
    "SELECT * FROM pg_roles",
    # catalog DATA / password / stats relations — caught by the pg_ prefix, NOT the explicit list
    "SELECT rolname, rolpassword FROM pg_authid",
    "SELECT most_common_vals, histogram_bounds FROM pg_stats WHERE tablename = 'salaries'",
    "SELECT query FROM pg_stat_statements",
    "SELECT * FROM pg_statistic",
    "SELECT typname FROM pg_type",
    "SELECT * FROM pg_constraint",
    "SELECT * FROM pg_stat_user_tables",
    "SELECT srvname FROM pg_foreign_server",
    # catalog-DDL-dump / object-existence-probe / topology FUNCTIONS
    "SELECT pg_get_viewdef('v')",
    "SELECT pg_get_functiondef('f'::regprocedure)",
    "SELECT has_table_privilege('secret_table', 'SELECT')",
    "SELECT to_regclass('secret_table')",
    "SELECT inet_server_addr()",
    "SELECT inet_client_addr()",
    "SELECT pg_relation_size('t')",
    "SELECT pg_backend_pid()",
    # redshift system tables (all four reserved prefixes) + pg_-prefixed redshift catalog helpers
    "SELECT * FROM stl_query",
    "SELECT * FROM svv_table_info",
    "SELECT * FROM stv_sessions",
    "SELECT * FROM svl_statementtext",
    "SELECT * FROM pg_table_def",
    "SELECT * FROM pg_user_info",
    # mysql secondary recon schemas
    "SELECT * FROM performance_schema.threads",
    "SELECT * FROM sys.session",
    # mysql system variables
    "SELECT @@version",
    "SELECT @@hostname",
    "SELECT @@datadir",
    "SELECT @@basedir",
    # quoted-identifier bypass — _neutralize unwraps the quotes, so the recon token re-forms
    'SELECT "version"()',
    'SELECT "pg_class" FROM t',
    # bypass attempts — neutralization must still catch them
    "SELECT /*c*/ version()",
    "SELECT VERSION()",
    "SELECT  version  ()",
    "select * from Information_Schema.Tables",
    "SELECT id FROM t UNION SELECT version()",
    "SELECT * FROM (SELECT * FROM pg_tables) s",
]


@pytest.mark.parametrize("sql", _RECON_REFUSE)
def test_recon_refused(sql: str) -> None:
    v = check_no_recon(sql)
    assert v is not None, f"RECON NOT BLOCKED (false negative / info-leak): {sql!r}"
    assert v.rule == "recon", v


_RECON_PASS = [
    # bare recon token as a column / alias — only the ()-call form is recon
    "SELECT version FROM releases",
    "SELECT version AS v FROM app_releases",
    "SELECT current_database FROM config",
    # suffix on a niladic/paren token — the word boundary protects these (NOT pg_-prefixed)
    "SELECT current_user_id FROM audit",
    "SELECT session_user_count FROM stats",
    "SELECT versions.id FROM versions",
    # deliberately-allowed bare niladic synonyms (too common as column names) — only their ()-form is recon
    "SELECT user FROM accounts",
    "SELECT schema, name FROM migrations",
    "SELECT database FROM connections",
    # qualified column — the (?<!\\.) lookbehind (niladic AND relation) lets these pass
    "SELECT t.current_user FROM team t",
    "SELECT o.pg_class FROM orders o",
    "SELECT s.pg_tables FROM settings s",
    # bare schema name without a dot
    "SELECT information_schema FROM catalog_meta",
    # recon token inside a string literal (neutralized away)
    "SELECT id FROM t WHERE note = 'see pg_tables for detail'",
    "SELECT 'version()' AS label FROM t",
    "SELECT id FROM t WHERE u = 'current_user'",
    # recon token inside a comment (neutralized away)
    "SELECT id FROM t -- current_user audit",
    "SELECT id /* pg_stat_activity */ FROM t",
    # control analytics that must never trip the gate
    "SELECT date_trunc('month', created_at), count(*) FROM orders GROUP BY 1",
    "SELECT u.id, o.amount FROM users u JOIN orders o ON o.user_id = u.id",
    "WITH v AS (SELECT max(created_at) mx FROM events) SELECT * FROM v",
    "SELECT count(*) OVER (PARTITION BY region) FROM sales",
    "SELECT d::date FROM generate_series('2026-01-01'::date, '2026-12-31'::date, INTERVAL '1 day') AS d",
]


@pytest.mark.parametrize("sql", _RECON_PASS)
def test_recon_gate_no_false_positive(sql: str) -> None:
    assert check_no_recon(sql) is None, (
        f"FALSE POSITIVE — legit query blocked by recon gate: {sql!r}"
    )


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT "current_user"',  # bare quoted niladic — _neutralize unwraps the quotes
        "SELECT x AS pg_tables FROM t",  # alias deliberately named a reserved catalog relation
        # A bare, unqualified user identifier literally prefixed `pg_` — the reserved-prefix rule
        # refuses it (Postgres reserves the `pg_` object-name prefix). Qualified `t.pg_foo` passes.
        "SELECT pg_tables_archived FROM meta",
        "SELECT pg_class_history FROM audit_log",
    ],
)
def test_recon_accepted_residual_false_positives(sql: str) -> None:
    # Regex without AST binding can't distinguish these from a real recon reference; all are rare +
    # convention-violating and documented in sql_guard. Pinned so a future change that flips one is a
    # conscious decision.
    assert check_no_recon(sql) is not None, sql


def test_recon_sets_drive_the_regexes() -> None:
    # A new engine builtin is a one-line SET edit — the compiled regexes derive from these sets (SC4).
    # Loop over EVERY member so the corpus is self-updating: adding/removing a set entry is auto-tested,
    # and no member can silently stop matching.
    for name in sql_guard._RECON_PAREN_FNS:
        assert check_no_recon(f"SELECT {name}()").rule == "recon", name
    for name in sql_guard._RECON_NILADIC:
        assert check_no_recon(f"SELECT {name}").rule == "recon", name
    for name in sql_guard._RECON_BARE_RELATIONS:
        assert check_no_recon(f"SELECT * FROM {name}").rule == "recon", name
    for name in sql_guard._RECON_SCHEMAS:
        assert check_no_recon(f"SELECT * FROM {name}.x").rule == "recon", name
    # the pg_ reserved-prefix catch-all covers catalog relations NOT in the explicit list
    for name in ("pg_statistic", "pg_authid", "pg_stats", "pg_stat_statements", "pg_type"):
        assert check_no_recon(f"SELECT * FROM {name}").rule == "recon", name


def test_recon_fails_closed_on_unparseable_input() -> None:
    # A form _neutralize can't disambiguate (a bare `--x` comment) → check_no_recon fails CLOSED (a
    # recon verdict), not open. In the normal flow check_read_only refuses it first; this pins the
    # defense-in-depth for a standalone / reordered caller.
    v = check_no_recon("SELECT 1--x")
    assert v is not None and v.rule == "recon", v


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
    # The guard emits a JSON envelope with kind=permission to stderr.
    envelope = None
    for line in proc.stderr.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                envelope = json.loads(line)
            except ValueError:
                continue
    assert envelope is not None, f"no JSON refusal envelope on stderr; got: {proc.stderr!r}"
    assert envelope["refusal"]["kind"] == "permission", envelope


def test_executor_dangerous_function_blocked(tmp_path) -> None:
    proc = _run_executor("SELECT pg_read_file('/etc/passwd')", tmp_path)
    assert proc.returncode != 0
    assert "permission" in proc.stderr


def test_executor_recon_query_blocked(tmp_path) -> None:
    # The recon gate fires at the shared chokepoint (execute_sql.py::main, which both transports
    # call), refusing BEFORE credentials are loaded — proving both surfaces inherit it (SC3).
    proc = _run_executor("SELECT current_user", tmp_path)
    assert proc.returncode != 0, proc.stdout
    envelope = None
    for line in proc.stderr.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                envelope = json.loads(line)
            except ValueError:
                continue
    assert envelope is not None, f"no JSON refusal envelope on stderr; got: {proc.stderr!r}"
    assert envelope["refusal"]["kind"] == "recon", envelope


def test_executor_lets_valid_select_past_the_gate(tmp_path) -> None:
    """A valid SELECT is NOT rejected by the read-only gate — it proceeds to the
    credential step and fails there instead (proving the gate didn't block it)."""
    proc = _run_executor("SELECT 1", tmp_path)
    # It should fail (no such profile / credentials), but NOT with a read-only
    # permission rejection from the guard.
    assert '"kind": "permission"' not in proc.stderr, (
        f"valid SELECT was wrongly blocked by the read-only gate: {proc.stderr!r}"
    )
