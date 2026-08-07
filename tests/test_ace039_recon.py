"""The recon gate: metadata FUNCTIONS are refused, catalog RELATIONS are the model's job.

Three corpora, because this gate has three ways to be wrong and only one of them is the
obvious one.

**Deny** — the functions, niladic keywords and register-type casts that must refuse. Every
vector here is verified to pass `check_read_only` first, so it genuinely proves the recon
gate rather than borrowing another gate's refusal.

**Collide** — names on BOTH this list and the dangerous-function list. They refuse as
`read_only`, because that gate runs first and owns the label for what it names. Asserting it
pins the order rather than leaving it to whoever reads the chokepoint next (principle 9).

**Pass** — the false-positive bar. Over-refusal is the safe direction, which is exactly why
it needs a criterion: the one regression this gate has caused in real use was an
over-refusal, found by a user rather than a test.

The corpora are deliberately NOT named `REJECT_*`. `test_sql_guard.py` auto-scans that prefix
into a corpus that `test_ace035_read_only_refusal.py` holds to `rule == read_only`, so a
`REJECT_RECON` list here would be silently adopted by that contract and break it.
"""

from __future__ import annotations

import guardrail
import pytest
from sql_guard import check_no_recon, check_read_only

# One vector per member of every deny set, so a name added without a vector is visible.
RECON_DENY = [
    # Server / session / account metadata, call form.
    "SELECT version()",
    "SELECT current_database()",
    "SELECT current_schemas(true)",
    "SELECT connection_id()",
    "SELECT system_user()",
    "SELECT current_account()",
    "SELECT current_warehouse()",
    "SELECT inet_server_addr()",
    "SELECT inet_client_port()",
    # Niladic keywords — the same primitive without the parens.
    "SELECT current_user",
    "SELECT session_user",
    "SELECT current_catalog",
    "SELECT current_schema",
    "SELECT current_role",
    # The privilege family, enumerated.
    "SELECT has_table_privilege('orders', 'SELECT')",
    "SELECT has_column_privilege('orders', 'id', 'SELECT')",
    "SELECT has_schema_privilege('public', 'USAGE')",
    # Call families.
    "SELECT pg_get_viewdef('v')",
    "SELECT to_regclass('orders')",
    "SELECT to_regprocedure('now()')",
    # Register-type casts, both spellings. The cast errors when the object is absent, so it
    # answers "does this exist?" without naming the object in a FROM clause.
    "SELECT 'secret_table'::regclass",
    "SELECT 'x'::regproc",
    "SELECT 'x'::regnamespace",
    "SELECT CAST('secret_table' AS regclass)",
    # SCHEMA-QUALIFIED, the spelling an arm anchored straight after `::` never sees. Valid
    # PostgreSQL, same oracle, and it names no relation for the model gate to bite on.
    "SELECT 'secret_table'::pg_catalog.regclass",
    "SELECT CAST('secret_table' AS pg_catalog.regproc)",
    'SELECT \'x\'::"pg_catalog"."regclass"',
    # A quoted CALL is still a call — quoting suppresses the niladic matcher, never a call
    # matcher. Without the paren/niladic union this one slips both.
    'SELECT "current_schema"()',
    'SELECT "version"()',
    # Riding inside an otherwise legitimately-scoped query, which is the shape that makes
    # this gate necessary: every model gate passes it.
    "SELECT o.id, version() FROM orders o",
    "SELECT o.id FROM orders o WHERE o.name = current_user",
]

# On the dangerous-function list too. `check_read_only` runs first and owns these.
RECON_COLLIDES_WITH_READ_ONLY = [
    "SELECT current_setting('data_directory')",
    "SELECT set_config('a', 'b', false)",
    "SELECT pg_sleep(10)",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT pg_ls_dir('/')",
    "SELECT pg_stat_file('/etc/passwd')",
    "SELECT pg_terminate_backend(1)",
    "SELECT pg_cancel_backend(1)",
    "SELECT pg_reload_conf()",
]

# Must pass BOTH gates. Each is a plausible statement against a real declared model.
RECON_PASSES = [
    # A declared column whose name collides with a niladic keyword. Quoted, it is an
    # identifier; this is the false positive the span tracking exists to fix.
    'SELECT "current_user" FROM audit_log',
    'SELECT "session_user", id FROM audit_log',
    # Qualified, it was already fine — pinned so the lookbehind is not lost in a refactor.
    "SELECT t.current_user FROM t",
    "SELECT o.version FROM orders o",
    # A user-defined function whose name sits between `has_` and `_privilege`. The wildcard
    # matched this; the enumerated list does not.
    "SELECT has_active_privilege(user_id) FROM memberships",
    "SELECT has_billing_privilege(1) FROM t",
    # `reg*` as an ordinary alias or column, not a cast.
    "SELECT id AS regclass_id FROM t",
    "SELECT regclass_name FROM t",
    # Ordinary columns that merely start with a reserved-looking prefix.
    "SELECT reltuples FROM stats",
    "SELECT pg_status FROM t",
    # A recon token mentioned inside a literal or comment cannot reach the matcher, and
    # equally must not false-trip it.
    "SELECT 'current_user' AS label FROM t",
    "SELECT id FROM t /* current_user is not called here */",
]


@pytest.mark.parametrize("sql", RECON_DENY, ids=lambda s: s[:48])
def test_a_recon_function_is_refused(sql: str) -> None:
    assert check_read_only(sql) is None, (
        "vector is caught by the read-only gate first, so it does not prove the recon gate"
    )
    refusal = check_no_recon(sql)
    assert refusal is not None, f"recon not refused: {sql!r}"
    assert refusal.rule == guardrail.RULE_RECON
    assert refusal.reason == "unsafe"
    assert refusal.detail.strip()
    assert refusal.remediation.strip()


@pytest.mark.parametrize("sql", RECON_COLLIDES_WITH_READ_ONLY, ids=lambda s: s[:48])
def test_a_name_on_both_lists_refuses_as_read_only(sql: str) -> None:
    """The order is fixed, so the label is deterministic (principle 9).

    Both gates would refuse these; the chokepoint runs `check_read_only` first, so the caller
    always sees the same rule for the same statement rather than one that depends on which
    list was consulted.
    """
    refusal = check_read_only(sql)
    assert refusal is not None
    assert refusal.rule == guardrail.RULE_READ_ONLY


@pytest.mark.parametrize("sql", RECON_PASSES, ids=lambda s: s[:48])
def test_no_false_positives(sql: str) -> None:
    assert check_read_only(sql) is None, f"read-only gate over-refused: {sql!r}"
    assert check_no_recon(sql) is None, f"recon gate over-refused: {sql!r}"


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT "current_user',                      # runs to EOF
        'SELECT "a\\"b" , current_user FROM t',       # MySQL re-opens a runaway identifier
        'SELECT o.id FROM orders o WHERE o.n = "x current_user',
    ],
    ids=["eof", "mysql_backslash", "tail"],
)
def test_an_unterminated_quote_does_not_suppress_the_keyword(sql: str) -> None:
    """The under-refusal direction, which is the only one that matters here.

    An unterminated `"` swallows the rest of the statement into the identifier buffer. If that
    produced a span, the niladic matcher would treat every keyword inside it as quoted and skip
    it. Reachable rather than theoretical: MySQL's default sql_mode treats `"` as a STRING
    delimiter with backslash escapes, so the second vector is a statement MySQL runs and answers
    with CURRENT_USER() while the neutralizer reads a runaway identifier.

    No closing delimiter means no span, so nothing is skipped and the gate refuses.
    """
    refusal = check_no_recon(sql)
    assert refusal is not None, f"an unterminated quote suppressed the keyword: {sql!r}"
    assert refusal.rule == guardrail.RULE_RECON


def test_a_quoted_bare_word_is_an_identifier_but_a_quoted_call_is_a_call() -> None:
    """The exact boundary of the false-positive fix, in one place.

    Quoting suppresses the NILADIC matcher only. A quoted name with a trailing `(` is still a
    call, and treating it otherwise is the hole the paren/niladic union closes.
    """
    assert check_no_recon('SELECT "current_user" FROM audit_log') is None
    assert check_no_recon('SELECT "current_schema"()').rule == guardrail.RULE_RECON


def test_a_model_may_declare_a_schema_named_sys() -> None:
    """The model is the authority on relations, so a declared schema is queryable.

    Bare schema names are not on this list precisely so that a schema the model declares is a
    schema the caller may query, whatever it happens to be called.
    """
    assert check_no_recon("SELECT id FROM sys.user_group") is None
    assert check_no_recon("SELECT u.id FROM sys.sys_user u JOIN sys.sys_group g ON g.id = u.gid") is None


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT relname FROM pg_class",
        "SELECT table_name FROM information_schema.tables",
        "SELECT name FROM sqlite_master",
    ],
)
def test_catalog_relations_are_not_this_gates_job(sql: str) -> None:
    """Relations belong to `semantic_model.runtime.check_table_scope`, not here.

    They are tables the model does not declare, which is a 4b out-of-scope refusal from a gate
    that was already built. Denying them here as well was redundant where that gate runs, and
    where it does not run it was harmful: matching bare schema names refused a datasource whose
    `sys` schema holds ordinary user tables.

    Residual, stated: on the vendored plugin layout `runtime` is absent by construction, so
    catalog relations have no gate there at all.
    """
    assert check_no_recon(sql) is None


def test_an_unreadable_statement_is_undetermined_not_recon() -> None:
    """A statement we cannot read is not one we caught fingerprinting the server.

    It fails closed either way; only the label and the fix change. Unreachable at the
    chokepoint, where `check_read_only` runs the same neutralizer and refuses first — so this
    is asserted as a standalone call, which is the only place it can be observed.
    """
    refusal = check_no_recon("SELECT 1 --x\nFROM t")
    assert refusal is not None
    assert refusal.rule == guardrail.RULE_UNPARSEABLE
    assert refusal.reason == "undetermined"


def test_an_empty_statement_is_left_to_the_read_only_gate() -> None:
    """It owns that rejection and its remediation; two gates answering is two answers."""
    assert check_no_recon("") is None
    assert check_no_recon("   ") is None
    assert check_no_recon(None) is None


def test_the_refusal_echoes_the_caller_s_own_token_and_enumerates_nothing() -> None:
    """A refusal that lists what IS allowed is a schema-listing endpoint."""
    refusal = check_no_recon("SELECT o.id, version() FROM orders o")
    assert "version" in refusal.detail
    for leak in ("orders", "customers", "public", "information_schema"):
        assert leak not in refusal.remediation
