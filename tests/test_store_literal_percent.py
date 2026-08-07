"""A literal `%` in SQL must mean the same thing on both engines.

`Store` exists so one statement runs unchanged on SQLite (tests, small self-hosts) and Postgres
(deployments). A percent sign is where that promise used to break: psycopg2 treats a non-None
`params` as a request to interpolate and reads `%` as a placeholder marker, so `LIKE 'thing%'` raised
`IndexError` on Postgres while running fine on SQLite.

**Which is the dangerous direction.** The suite runs on SQLite, so the engine that fails is the one
with no coverage — the first `LIKE` anybody writes passes every test and fails in a deployment, with
an error that points nowhere near the SQL.

The SQLite half runs everywhere. The Postgres half is opt-in on `AGAMI_IT_PG_PASSWORD`, the same
switch and fixture `test_postgres_named_cursor_integration.py` uses:

    docker compose -f tests/integration/docker-compose.yml up -d postgres
    AGAMI_IT_PG_PASSWORD=<the fixture's POSTGRES_PASSWORD> uv run pytest tests/test_store_literal_percent.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

from store import Store  # noqa: E402

# One statement, no parameters, one literal percent. Deliberately needs no table: this is about the
# driver's handling of the string, not about anything stored.
LIKE_A_PERCENT = "SELECT 1 AS ok WHERE 'abc' LIKE 'a%'"


def test_a_literal_percent_runs_on_sqlite(tmp_path):
    store = Store.connect("sqlite://" + str(tmp_path / "percent.db"))
    try:
        assert store.query(LIKE_A_PERCENT) == [{"ok": 1}]
    finally:
        store.close()


def test_parameters_still_reach_the_statement_on_sqlite(tmp_path):
    """The other half of the change: skipping interpolation when there are no parameters must not
    skip it when there are. A fix that quietly stopped binding would pass the test above."""
    store = Store.connect("sqlite://" + str(tmp_path / "params.db"))
    try:
        store.execute("CREATE TABLE t (name TEXT)")
        store.execute("INSERT INTO t (name) VALUES (?)", ("acme",))
        store.commit()

        assert store.query("SELECT name FROM t WHERE name = ?", ("acme",)) == [{"name": "acme"}]
        assert store.query("SELECT name FROM t WHERE name = ?", ("demo",)) == []
        # ...and both together: a bound parameter beside a literal percent.
        assert store.query("SELECT name FROM t WHERE name LIKE 'ac%' AND name = ?", ("acme",)) == [
            {"name": "acme"}
        ]
    finally:
        store.close()


@pytest.mark.skipif(
    not os.environ.get("AGAMI_IT_PG_PASSWORD"),
    reason="set AGAMI_IT_PG_PASSWORD to run the Postgres half (see the module docstring)",
)
def test_a_literal_percent_runs_on_postgres_too():
    """The half that was broken, against a real driver.

    A fake cursor cannot reproduce it: the failure is psycopg2's own interpolation step, so only the
    real driver raises.
    """
    dsn = (
        f"postgresql://{os.environ.get('AGAMI_IT_PG_USER', 'agami_test')}:"
        f"{os.environ['AGAMI_IT_PG_PASSWORD']}@"
        f"{os.environ.get('AGAMI_IT_PG_HOST', '127.0.0.1')}:"
        f"{os.environ.get('AGAMI_IT_PG_PORT', '55432')}/"
        f"{os.environ.get('AGAMI_IT_PG_DB', 'shop')}"
    )
    store = Store.connect(dsn)
    try:
        assert store.query(LIKE_A_PERCENT) == [{"ok": 1}]
        # And a bound parameter still binds on this engine, where `?` is rewritten to `%s` — the
        # rewrite and the interpolation are the two halves that have to keep agreeing.
        assert store.query("SELECT ? AS who", ("acme",)) == [{"who": "acme"}]
    finally:
        store.close()
