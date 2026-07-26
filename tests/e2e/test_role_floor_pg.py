"""ACE-040 — the ROLE FLOOR: the read-only DB role rejects a write even with the APP GATE BYPASSED.

The app-layer read-only guard (`check_read_only`) refuses INSERT / UPDATE / DELETE / DDL before any
driver call — but that is defense in depth. The PRIMARY control is the database role: agami connects
as a SELECT-only user (see plugins/agami/shared/readonly-grants.md), so even if the app gate were
bypassed by a bug or a new code path, the DATABASE itself rejects the write.

This test proves that floor. It issues writes on a RAW psycopg2 connection opened AS the read-only
role — deliberately NOT through `tool_execute_sql` (which would refuse them at the app gate first) —
and asserts Postgres raises `InsufficientPrivilege`. A read succeeds, so the floor blocks writes, not
work. Opt-in: skips unless AGAMI_IT_PG_PASSWORD is set + a Postgres is reachable (the integration-pg
CI job provides both; locally: `docker compose -f tests/integration/docker-compose.yml up -d postgres`).
"""

from __future__ import annotations

import pytest

# The read-only role must PERMIT reads and REJECT every write path — at the database, not the app.
_WRITE_ATTEMPTS = [
    "INSERT INTO agami_floor VALUES (9, 'x')",
    "UPDATE agami_floor SET label = 'y'",
    "DELETE FROM agami_floor",
    "TRUNCATE agami_floor",
    "DROP TABLE agami_floor",
]


def test_read_only_role_permits_select(pg_ro_conn):
    _psycopg2, ro = pg_ro_conn
    with ro.cursor() as c:
        c.execute("SELECT id, label FROM agami_floor ORDER BY id")
        assert c.fetchall() == [(1, "a"), (2, "b")]  # the floor permits reads (agami only SELECTs)


@pytest.mark.parametrize("write_sql", _WRITE_ATTEMPTS, ids=[w.split()[0] for w in _WRITE_ATTEMPTS])
def test_read_only_role_rejects_writes_with_app_gate_bypassed(pg_ro_conn, write_sql):
    psycopg2, ro = pg_ro_conn
    # NOTE: issued DIRECTLY on the role's connection — the app read-only gate is NOT in the loop here.
    # If this write ever SUCCEEDS, the primary control has regressed (the role isn't SELECT-only).
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        with ro.cursor() as c:
            c.execute(write_sql)
    ro.rollback()  # clear the aborted transaction before the fixture tears down
