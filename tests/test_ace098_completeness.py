"""The record is enough to reproduce the judgement without the database (ACE-098, principle 7).

This is Wave 5's done-bar, and it is the only check that can tell whether the recorded fields are
**sufficient** rather than merely present. A field list nobody has re-derived from is a guess about
what sufficiency requires.

**How the re-derivation works, and why it needs no new code.** The obvious reading — a second
function that redoes the gate battery — would be a second place that decides, against the
one-chokepoint invariant and free to drift from the real one. It is not needed. A refusal never
reaches the executor, so `execute_guarded` can be re-run exactly as it ships with an executor that
raises if called: no database is involved, by construction rather than by mocking. What comes back
is the same verdict the row recorded, or the record was not sufficient.

**What is exempt, and it is one rule.** Principle 9 carves out the two RUNTIME bounds — the
statement timeout (ACE-038) and the result bound (ACE-087) — because they are determined at
runtime rather than from the SQL and the model: the same statement executes at 29s and refuses at
31s. Both emit ONE rule id, `resource_limit`, and ACE-087 keeps it that way deliberately (one rule,
one emit site). So the exempt set has one member, and this file asserts the set rather than
accepting whatever the harness happens to skip — the carve-out cannot widen by accident.

**Synthetic only.** Every statement, model and record here is fabricated. The record's contents are
customer data in a real deployment, and this repo is public.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402
import guardrail  # noqa: E402
import tools  # noqa: E402
from store import Store  # noqa: E402

PROFILE = "acme"

# The one rule the completeness bar does not apply to. Asserted as a SET below, not consulted as a
# convenience: an exemption the harness merely honours can grow silently, and the whole value of
# principle 9's carve-out is that it is bounded.
EXEMPT_RULES = frozenset({guardrail.RULE_RESOURCE_LIMIT})


@pytest.fixture(autouse=True)
def _isolate():
    tools.set_injected_executor(None)
    yield
    tools.set_injected_executor(None)


def _write_model(root: Path) -> None:
    """A two-table model: `orders` is declared, `secrets` is not.

    `secrets` is what makes a 4b vector possible — a statement reaching a table the model does not
    expose — without inventing a table the warehouse also lacks, which would produce `failed`.
    """
    import yaml

    (root / "subject_areas" / "sales" / "tables").mkdir(parents=True)
    (root / "datasource.yaml").write_text(
        yaml.safe_dump(
            {"datasource": "Shop", "version": 1,
             "storage_connections": [{"name": "c", "storage_type": "SQLite"}],
             "subject_areas": ["subject_areas/sales"]}
        )
    )
    (root / "subject_areas" / "sales" / "subject_area.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "sales",
                "tables": [{"storage_connection": "c", "schema": "public", "table": "orders"}],
            }
        )
    )
    (root / "subject_areas" / "sales" / "tables" / "orders.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "orders",
                "schema": "public",
                "storage_connection": "c",
                "grain": ["id"],
                "description": "orders",
                "columns": [
                    {"name": "id", "type": "integer", "primary_key": True},
                    {"name": "status", "type": "string"},
                    # Declared here and deliberately ABSENT from the warehouse below. That is how a
                    # statement gets past every gate and is then rejected by the database — the `failed`
                    # branch, reached for real rather than by stubbing the executor.
                    {"name": "amount", "type": "integer"},
                ],
            }
        )
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    app_db = "sqlite://" + str(tmp_path / "app.db")
    store = Store.connect(app_db)
    store.run_migrations()
    store.close()

    artifacts = tmp_path / "artifacts"
    _write_model(artifacts / PROFILE)

    warehouse = tmp_path / "warehouse.db"
    con = sqlite3.connect(warehouse)
    con.execute("CREATE TABLE orders (id INTEGER, status TEXT)")
    con.executemany("INSERT INTO orders VALUES (?, ?)", [(1, "paid"), (2, "open")])
    con.execute("CREATE TABLE secrets (id INTEGER)")
    con.commit()
    con.close()

    monkeypatch.setenv("AGAMI_DB_URL", app_db)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(artifacts))
    monkeypatch.setenv("DATASOURCE_URL__ACME", f"sqlite:///{warehouse}")
    monkeypatch.delenv("AGAMI_ORG_ID", raising=False)
    return SimpleNamespace(app_db=app_db, artifacts=artifacts, warehouse=warehouse)


class _NeverExecutes:
    """The database, absent by construction.

    Not a mock of one: it is the assertion. A re-derivation that needed the warehouse would call
    this and fail the test on the call itself, which is the only way to prove the record alone was
    enough rather than the fixture quietly helping.
    """

    def execute(self, sql, creds, *, profile=None, **kwargs):
        raise AssertionError("re-derivation reached the database; the record was not sufficient")


# The corpus. One vector per REFUSAL REASON — the three principle 4 admits — plus an executed call
# and a database failure, because 7's "whether it executed or was refused" covers both and a corpus
# of refusals alone would not notice a receipt that only ever ships on one status.
CORPUS = [
    ("4a unsafe", "DELETE FROM orders", "refused", "unsafe"),
    ("4b out_of_scope", "SELECT id FROM secrets", "refused", "out_of_scope"),
    ("4c undetermined", "SELECT * FROM orders", "refused", "undetermined"),
    ("executed", "SELECT id, status FROM orders", "ok", None),
    ("database failed", "SELECT amount FROM orders", "failed", None),
]


def _record_one(sql: str) -> dict:
    """Run one statement through the real chokepoint and the real serializer, as a caller would."""
    envelope = execute_sql.execute_guarded(
        sql, PROFILE, "sales", executor=execute_sql.BUILTIN_EXECUTOR
    )
    body = tools._emit(envelope, sql=sql, execution_ms=None, profile=PROFILE)
    return {"envelope": envelope, "body": json.loads(body)}


def _rows(url: str) -> list[dict]:
    store = Store.connect(url)
    try:
        return store.query(
            "SELECT id, sql, status, reason, rule, detail, receipt, model_version "
            "FROM query_executions ORDER BY ts"
        )
    finally:
        store.close()


# ---------------------------------------------------------------------------
# The completeness bar
# ---------------------------------------------------------------------------


def test_the_corpus_spans_every_refusal_reason():
    """The bar is over EVERY reason, so a corpus that quietly lost one would pass on less."""
    covered = {reason for _, _, status, reason in CORPUS if status == "refused"}
    assert covered == set(guardrail._REASONS)


def test_every_vector_produces_the_status_the_corpus_claims(env):
    """The corpus declares an expected status per vector; nothing else checks it holds.

    Worth its own test because the failure is silent in the direction that matters. A vector that
    quietly started refusing — a model fixture typo is enough, and one cost an hour here — would
    still leave the completeness assertion green while the corpus no longer spanned what it says it
    spans. The `ok` and `failed` rows in particular exist to prove the receipt survives on a status
    other than `refused`.
    """
    for label, sql, expected_status, expected_reason in CORPUS:
        envelope = _record_one(sql)["envelope"]
        assert envelope.status == expected_status, (
            f"{label}: {envelope.refusal or envelope.failure}"
        )
        if expected_reason is not None:
            assert envelope.refusal.reason == expected_reason, label


def test_the_exempt_set_is_exactly_one_rule():
    """Asserted, not accepted. An exemption a harness merely honours can grow without a diff.

    It is one rule and not two because the statement timeout and the result bound share
    `resource_limit` — ACE-087 keeps one rule with one emit site, so the only thing separating them
    is the refusal's wording, and reading THAT is what this spec removes.
    """
    assert EXEMPT_RULES == {guardrail.RULE_RESOURCE_LIMIT}
    assert EXEMPT_RULES < set(guardrail.REASON_FOR_RULE)


def test_every_recorded_verdict_re_derives_from_the_record_alone(env):
    """The done-bar. Write the corpus, read the rows back, re-derive each with no database.

    The rows are the ONLY input to the second half: the statement comes off the row, not from the
    corpus, so a field the record failed to keep cannot be silently supplied by the test.
    """
    for _, sql, _, _ in CORPUS:
        _record_one(sql)

    rows = _rows(env.app_db)
    assert len(rows) == len(CORPUS)

    re_derived = 0
    for row in rows:
        if row["rule"] in EXEMPT_RULES:
            continue
        if row["status"] != "refused":
            continue  # only a refusal carries a verdict to re-derive; the ok/failed rows are below
        envelope = execute_sql.execute_guarded(
            row["sql"], PROFILE, "sales", executor=_NeverExecutes()
        )
        assert envelope.status == "refused", row["sql"]
        assert envelope.refusal.reason == row["reason"], row["sql"]
        assert envelope.refusal.rule == row["rule"], row["sql"]
        assert envelope.refusal.detail == row["detail"], row["sql"]
        re_derived += 1

    assert re_derived == 3, "every refusal reason must have been re-derived, not skipped"


def test_the_record_carries_the_receipt_for_an_executed_call_and_a_refused_one(env):
    """7c: everything principle 6 reported is in the record, `undetermined` markers included.

    The marker is the half worth pinning. A section nobody checked has to keep saying so in the
    record too, or the audit trail turns "nobody looked" back into "nothing wrong" one layer down
    from where ACE-088 fixed it.
    """
    _record_one("SELECT id, status FROM orders")  # ok
    _record_one("DELETE FROM orders")  # refused

    for row in _rows(env.app_db):
        receipt = json.loads(row["receipt"])
        assert set(receipt) == {"model_version", *guardrail.Receipt.SECTIONS}
        markers = [receipt[s]["undetermined"] for s in guardrail.Receipt.SECTIONS]
        assert any(m for m in markers), (
            f"{row['status']} row carries no undetermined marker at all — the state that says "
            "'nobody looked' has to survive into the record"
        )


def test_the_model_version_is_a_selectable_column(env):
    """SC-4. It rides inside the receipt JSON too, and that copy cannot be filtered on portably.

    The version is written EXPLICITLY here rather than taken from whatever the fixture happens to
    resolve. A disk-only deployment pins no version at all — verified against a real Postgres, where
    every row came back with `model_version` NULL — so a test that read the fixture's own value
    would have compared NULL to NULL and passed while proving nothing about a version that exists.
    """
    receipt = guardrail.Receipt(model_version="v-2026-08-03")
    tools._emit(
        execute_sql._envelope(
            "refused",
            refusal=guardrail.refuse(guardrail.RULE_READ_ONLY, detail="d", remediation="r"),
            receipt=receipt,
        ),
        sql="DELETE FROM orders",
        execution_ms=None,
        profile=PROFILE,
    )

    row = _rows(env.app_db)[0]
    assert row["model_version"] == "v-2026-08-03"
    assert json.loads(row["receipt"])["model_version"] == "v-2026-08-03", (
        "the column and the receipt must agree; they are two copies of one fact"
    )

    store = Store.connect(env.app_db)
    try:
        hit = store.query(
            "SELECT count(*) AS n FROM query_executions WHERE model_version = ?",
            ("v-2026-08-03",),
        )
        # A replay against a DIFFERENT version is detectable rather than silently wrong.
        miss = store.query(
            "SELECT count(*) AS n FROM query_executions WHERE model_version = ?", ("v-other",)
        )
    finally:
        store.close()
    assert (hit[0]["n"], miss[0]["n"]) == (1, 0)


def test_the_recorded_statement_is_the_executed_statement_byte_for_byte(env):
    """Wave 2 made these the same value, which turns this from a field to populate into an assertion.

    Compared on the bytes rather than after any normalization: the whole point of ACE-093 is that
    nothing between the caller and the driver alters the statement, and a comparison that trimmed or
    re-cased would not notice if something started to.
    """
    sql = "SELECT id, status  FROM  orders"  # irregular spacing on purpose
    result = _record_one(sql)

    assert result["envelope"].status == "ok"
    assert _rows(env.app_db)[0]["sql"].encode() == sql.encode()
    assert result["body"]["sql"].encode() == sql.encode()


def test_the_detail_names_which_bound_fired_and_what_it_was_set_to(env, monkeypatch):
    """Principle 9's carve-out claims the record states "which one, and what it was set to".

    Before ACE-098 it could not: both runtime bounds share `resource_limit` and the row held only
    `reason` and `rule`. `detail` is where the distinction and the number already lived.
    """
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", "7")
    refusal = execute_sql._resource_limit_refusal(execute_sql._ResourceLimit())
    tools._emit(
        execute_sql._envelope("refused", refusal=refusal, receipt=guardrail.Receipt()),
        sql="SELECT 1",
        execution_ms=None,
        profile=PROFILE,
    )

    row = _rows(env.app_db)[0]
    assert row["rule"] == guardrail.RULE_RESOURCE_LIMIT
    assert "7" in row["detail"], "the configured bound is not in the record"


def test_the_detail_tells_the_result_bound_apart_from_the_timeout(env):
    """The other half of the carve-out, and the reason `detail` is recorded rather than a new rule.

    ACE-087 gives `_resource_limit_refusal` a `None` arm for the transfer bound and keeps ONE rule
    id, so the record distinguishes them by this field or not at all.
    """
    refusal = execute_sql._resource_limit_refusal(None)
    assert "row" in refusal.detail.lower()
