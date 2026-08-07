"""The floor under every other test in this directory: the database itself refusing to be written.

Everything else the corpus proves is a decision OUR code makes. This file proves the one control
that does not depend on our code being correct, or present, or reached. The connection is opened
directly with the driver, as the least-privilege role the server is configured with, and the writes
are issued on it. `execute_guarded` is not called. `tool_execute_sql` is not called. No transport, no
gate, no envelope — nothing from the application is in the path at all, which is precisely the
condition the read-only role exists to survive.

There is no in-app bypass to reach for even if that were the shape wanted: `no_safety=True` skips
only the model-scope pass, and `sql_guard.check_read_only` runs ahead of it and is not bypassable. So
"with the app gate out of the loop" can only mean a raw connection, and that is what this is.

**A role-floor test that passes for the wrong reason proves nothing, and there are three ways to.**
The role might not exist; the connection might have failed; the table might be absent. Each would
make every write raise and every assertion pass, while establishing exactly nothing about
privileges. So:

  * the same connection must first SELECT the seeded rows successfully — a negative control, and the
    reason each case opens its own connection rather than sharing one;
  * the connected identity is asserted to be the read-only role, not the owner the fixture seeds as;
  * the error is read, never merely caught. A write must fail with SQLSTATE 42501,
    `insufficient_privilege` — a permission decision. `42P01`, "relation does not exist", would be
    the absent-table impostor and it fails here.

**And the cases carry the `role_floor` marker, which is what puts them inside the sentinel.** They
were outside it: the DB job's only count was of `db_path` vectors, and this file's items carry
none, so deleting the file, renaming its test or thinning `WRITES` left the job green with the
single most load-bearing claim in the spec unrun. The retired `pytest -k "db_path or role"`
selector at least matched on "role"; replacing it with a count that never mentioned the role floor
made the coverage narrower, not wider. `tests/e2e/conftest.py::EXPECTED_ROLE_FLOOR_VECTORS` is the
number this file's five statements are held to, and it deliberately lives over there so that
deleting this file cannot delete the expectation with it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import itdeps  # noqa: E402

# The DB path's driver, required rather than skipped when the run declared it carries this evidence.
# `pytest.importorskip` would take a lost driver and report the whole proof green.
itdeps.importorfail("psycopg2")

import harness  # noqa: E402

if not harness.PG_ENABLED:
    # A skip locally, a FAILURE in the job that declared it carries this evidence. The password is
    # as much a prerequisite as the driver is, and a single deleted line in the workflow used to
    # take the whole DB half down to a module-level skip and an exit code of 0.
    itdeps.skip_or_fail(
        "set AGAMI_IT_PG_PASSWORD to run the Postgres role floor against the compose fixture",
        module_level=True,
    )

import psycopg2  # noqa: E402

# The ways to change or destroy data, one per statement class. `TRUNCATE` and `DROP` are here and
# not only `INSERT`/`UPDATE`/`DELETE` because a grant that stopped DML and left DDL alone would pass
# a three-statement test while leaving the table droppable.
#
# `create` is the one that is not about `orders` at all, and it closes a different gap: the five
# above prove the role cannot modify an EXISTING declared table, which is not the same claim as "the
# role cannot make new objects". A role that can create its own table in the schema the model
# declares can materialize whatever it can read, wherever it likes, and every statement above would
# still be refused. On PostgreSQL 16 this is refused by the server's own default — `CREATE ON SCHEMA
# public` is no longer granted to `PUBLIC` — which is exactly why it needs asserting rather than
# assuming: the recipe does not revoke it, so the floor here rests on a server default that older
# majors and Redshift do not share. See the grants fixture's header for the measured residual.
WRITES = [
    ("insert", "INSERT INTO orders (id, customer_id, amount, status) VALUES (99, 10, 1.0, 'x')"),
    ("update", "UPDATE orders SET amount = 0"),
    ("delete", "DELETE FROM orders"),
    ("truncate", "TRUNCATE TABLE orders"),
    ("drop", "DROP TABLE orders"),
    ("create", "CREATE TABLE floor_probe (x int)"),
]

# PostgreSQL's class 42 code for a privilege decision. Named as the constant it is because the whole
# assertion turns on this ONE value: any other code means the statement failed for a reason that is
# not the database refusing our role, and a test that accepted those would be green on an empty
# database.
INSUFFICIENT_PRIVILEGE = "42501"

# The negative control's statement, and the count `SCHEMA` seeds. Both read from the corpus so the
# control cannot drift from the warehouse the fixture actually built.
CONTROL_SQL = "SELECT id FROM orders"


@pytest.fixture()
def readonly_connection(pg_warehouse):
    """A raw driver connection as the read-only role. One per case, because a case that has just
    been refused leaves the transaction aborted and every later statement would fail on that."""
    conn = psycopg2.connect(harness.pg_readonly_dsn())
    try:
        yield conn
    finally:
        conn.close()


def _control(conn) -> None:
    """The negative control: this connection can read, so a refusal below is about the WRITE."""
    from safety.corpus import SCHEMA

    with conn.cursor() as cur:
        cur.execute("SELECT current_user")
        assert cur.fetchone()[0] == harness.PG_RO_USER, "connected as the wrong role"
        cur.execute(CONTROL_SQL)
        assert len(cur.fetchall()) == len(SCHEMA["orders"]["rows"])
    conn.rollback()


def _write_params():
    """One parameter per write, each carrying the `role_floor` MARKER.

    Applied by the parametrizer off `WRITES` itself, exactly as the corpus applies `db_path` off
    `CASES`: the marker is then a property of the data rather than a decoration on a function name,
    so thinning `WRITES` shrinks the marked set and the sentinel in `conftest.py` sees it.
    """
    return [pytest.param(label, sql, marks=pytest.mark.role_floor, id=label) for label, sql in WRITES]


@pytest.mark.parametrize("label, sql", _write_params())
def test_the_database_refuses_the_write_with_the_app_gate_out_of_the_loop(
    readonly_connection, label, sql
):
    """One write, issued straight at the database, and refused by the database.

    The control runs first on the same connection, so "it raised" cannot mean the connection was
    dead or the table was missing. Then the write, and the exception is interrogated rather than
    counted: `pgcode` is the database's own classification of what it just did, and only
    `insufficient_privilege` means it decided on privileges.
    """
    _control(readonly_connection)

    with pytest.raises(psycopg2.Error) as caught:
        with readonly_connection.cursor() as cur:
            cur.execute(sql)

    assert caught.value.pgcode == INSUFFICIENT_PRIVILEGE, (
        label,
        caught.value.pgcode,
        str(caught.value),
    )
    assert isinstance(caught.value, psycopg2.errors.InsufficientPrivilege), caught.value


def test_the_write_the_database_refuses_is_one_the_app_would_also_refuse():
    """The floor is only a floor if it sits under something.

    Nothing above runs a statement past the gate — that is the point of it. But that leaves the five
    statements unmoored: a role floor proved against statements the application would happily have
    sent is a proof about a different system. So the same five go through the read-only gate here,
    with no database in the path, and every one is refused. Two controls, same five statements,
    independent of each other — which is what defense in depth means when it is true.
    """
    import guardrail
    import sql_guard

    for label, sql in WRITES:
        refusal = sql_guard.check_read_only(sql)
        assert refusal is not None, label
        assert refusal.rule == guardrail.RULE_READ_ONLY, (label, refusal)
