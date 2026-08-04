"""The corpus again, on the deployment shape the file path is not: model served from the app
database, warehouse a real Postgres reached as the least-privilege role — and the same verdicts.

Two axes move together here and they are orthogonal, which is why they are named apart rather than
called "the DB path" and left at that:

  * the MODEL comes from the app database. It is deployed with `model_deploy.deploy_one` and read
    back with `model_store.load_datasource`, and `AGAMI_ARTIFACTS_DIR` points at an empty directory
    for the run, so there is no disk copy to fall back to and no way for this to pass on the file
    path's model by accident.
  * the WAREHOUSE is Postgres in a container, connected as `agami_ro` — the same read-only role
    `test_role_floor_pg.py` proves the floor of. So the governed vectors execute under exactly the
    privilege a deployment gives the server, not under the owner that seeded the tables.

Each vector runs on BOTH paths inside one test and the two decisions are compared. Comparing against
a recorded expectation would be the weaker test: it would pin what the DB path does without ever
establishing that the file path does the same, which is the entire claim.

**One class is carved out of the identical-verdicts claim, by design.** `AGAMI_DB_URL` is also what
`execute_sql._hosted()` reads, so configuring an app database changes what an unresolvable model
does: it fails closed on the served path and stays a no-op locally. That is the intended behaviour of
a different control, so it is asserted PER PATH at the bottom of this file instead of being held to a
sameness it was never supposed to have.

The five backtick/bracket-quoted vectors do not run here: identifier quoting is engine-specific and
those are pinned to SQLite, where the same text means something. `Case.runs_on` is what filters
them, and `safety.corpus.EXPECTED_DB_VECTORS` counts what is left.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = TESTS_ROOT.parent
for _path in (
    TESTS_ROOT,
    Path(__file__).resolve().parent,
    REPO_ROOT / "packages" / "agami-core" / "src",
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import itdeps  # noqa: E402

# The DB path's driver. `importorfail`, not `importorskip`: a run that declared it carries this
# evidence must not lose the driver and report green, which is the failure mode the whole file exists
# to make impossible.
itdeps.importorfail("psycopg2")

import guardrail  # noqa: E402
import harness  # noqa: E402

from safety.corpus import CASES, DB_PATH_ENGINE, EXPECTED_DB_VECTORS  # noqa: E402

if not harness.PG_ENABLED:
    pytest.skip(
        "set AGAMI_IT_PG_PASSWORD to run the safety corpus against the compose fixture",
        allow_module_level=True,
    )

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
pytest.importorskip("yaml")


def _db_params():
    """One parameter per DB-path vector, each carrying the `db_path` MARKER.

    The marker is applied here, by the parametrizer, off the corpus's own `runs_on` filter — never
    by a `-k` substring over the node id. That is the whole mechanism the session hook in
    `conftest.py` counts, and the reason a rename can no longer silently shrink this run.
    """
    return [
        pytest.param(case, marks=pytest.mark.db_path, id=case.id)
        for case in CASES
        if case.runs_on(DB_PATH_ENGINE)
    ]


@pytest.mark.parametrize("case", _db_params())
def test_a_vector_gets_the_same_verdict_on_the_db_path_as_on_the_file_path(
    pg_warehouse, tmp_path, monkeypatch, case
):
    """One vector, two paths, one verdict — and that verdict is the one the corpus pins.

    The file path runs first and the DB path second, on the same `monkeypatch`: the second build
    overwrites the model root, the warehouse DSN and the app database, so what the second call reads
    is entirely the second path. Both go over HTTP, matching `test_safety_corpus.py`, because what is
    being compared is what a caller receives.
    """
    def lower_the_ceiling_if_this_vector_needs_it() -> None:
        # Both builds clear `AGAMI_SQL_MAX_ROWS` — no other vector may run under a lowered one, and
        # the governed ones return more rows than this value on purpose — so the two availability
        # vectors re-lower it after each build rather than once before them.
        if case.rule == guardrail.RULE_RESOURCE_LIMIT:
            monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", str(harness.LOW_ROW_CAP))

    harness.build_file_path(tmp_path, monkeypatch)
    lower_the_ceiling_if_this_vector_needs_it()
    over_file = harness.ROUTES["http"](case.sql)

    harness.build_db_path(tmp_path, monkeypatch)
    lower_the_ceiling_if_this_vector_needs_it()
    over_db = harness.ROUTES["http"](case.sql)

    harness.reset_injected_executor()

    # The assertion this whole dimension exists for.
    assert harness.verdict(over_db) == harness.verdict(over_file), (over_db, over_file)

    # And the shared verdict is the expected one, so two paths cannot pass by agreeing on the wrong
    # answer. Asserted on the rule and its reason, never on `status` alone: a refusal by the wrong
    # gate reads green under a status check and is a different security posture.
    if case.rule is None:
        assert over_db["status"] == "ok", over_db
        assert "refusal" not in over_db, over_db
        return
    assert over_db["status"] == "refused", over_db
    assert over_db["refusal"]["rule"] == case.rule, over_db
    assert over_db["refusal"]["reason"] == guardrail.REASON_FOR_RULE[case.rule], over_db


# ---------------------------------------------------------------------------
# The carve-out: the one class that is SUPPOSED to differ between the paths
# ---------------------------------------------------------------------------

# A governed vector's own SQL, taken from the corpus rather than written again: the two tests below
# need a statement every gate allows, or they would prove their point about a statement that was
# refused for an unrelated reason.
_GOVERNED_SQL = next(case.sql for case in CASES if case.rule is None)


def test_the_served_path_refuses_when_its_model_is_gone(pg_warehouse, tmp_path, monkeypatch):
    """Two things at once, and the second is why this test is here rather than in a fixture comment.

    The claim under test is the carve-out: with an app database configured the deployment is served,
    so a model it cannot resolve is a refusal — it never runs the statement with the scope gates
    silently off. `model_unavailable`, and nothing else, because there IS no model.

    The second thing it establishes is that the model above was genuinely coming out of the database.
    Deleting the deployed rows changes a governed vector from `ok` to refused, which it could only do
    if those rows were what the gates were reading. A path that had quietly fallen back to a disk
    copy would be entirely unmoved by this.
    """
    from store import Store

    built = harness.build_db_path(tmp_path, monkeypatch)
    assert harness.ROUTES["http"](_GOVERNED_SQL)["status"] == "ok"

    store = Store.connect(built.app_db_url)
    try:
        store.execute("DELETE FROM datasource_model")
        store.commit()
    finally:
        store.close()

    body = harness.ROUTES["http"](_GOVERNED_SQL)

    assert body["status"] == "refused", body
    assert body["refusal"]["rule"] == guardrail.RULE_MODEL_UNAVAILABLE, body
    expected_reason = guardrail.REASON_FOR_RULE[guardrail.RULE_MODEL_UNAVAILABLE]
    assert body["refusal"]["reason"] == expected_reason, body


def test_the_local_path_does_not_refuse_when_its_model_is_gone(tmp_path, monkeypatch):
    """The other half of the carve-out, asserted rather than assumed.

    Same missing model, no app database — a laptop that has not built one yet — and the statement
    runs. That is the deliberate asymmetry `execute_sql._hosted()` exists to draw, and stating it
    here is what keeps the identical-verdicts claim above honest: the one class that differs between
    the paths is named, bounded and tested, instead of being an exception a reader has to infer.
    """
    harness.build_file_path(tmp_path, monkeypatch)
    empty = tmp_path / "no-model-here"
    empty.mkdir()
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(empty))

    body = harness.ROUTES["http"](_GOVERNED_SQL)

    assert body["status"] == "ok", body
    # The receipt is where the local path says what the served one refuses over: nothing was
    # checked. Asserted so "ok" here cannot be read as "checked and allowed".
    assert body["receipt"]["tables"]["undetermined"], body


def test_the_db_path_carries_every_vector_the_corpus_says_it_should():
    """The sentinel's constant, asserted against the parametrization it governs.

    `conftest.py` compares a COLLECTED count against `EXPECTED_DB_VECTORS`, which catches a run that
    lost vectors. It cannot catch a constant that was wrong to begin with — a corpus and a hook
    reading the same mistaken number agree perfectly. This is the other end: the number describes
    the real corpus, minus exactly the vectors pinned to another engine, and nothing else.
    """
    assert len(_db_params()) == EXPECTED_DB_VECTORS
    excluded = [case for case in CASES if not case.runs_on(DB_PATH_ENGINE)]
    assert EXPECTED_DB_VECTORS == len(CASES) - len(excluded)
    # Not a tautology with the line above: it pins that something IS excluded, so a corpus that lost
    # its engine pins would fail here rather than quietly widening the run.
    assert excluded, "the engine-pinned vectors vanished, so this path is no longer a subset"
    assert all(DB_PATH_ENGINE not in (case.engines or ()) for case in excluded)
