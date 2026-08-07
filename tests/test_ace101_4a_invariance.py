"""ACE-101's load-bearing sentence, asserted adversarially: with the semantic-model pass turned OFF,
no statement the read-only, recon or resource gates would refuse becomes executable.

The switch is scoped to the 4b/4c pass, and the argument for that scoping is structural rather than a
rule anyone has to remember. `check_read_only` and `check_no_recon` are composed in `execute_guarded`
ABOVE the pass, and the resource bounds are applied around the executor below it, so there is no
ordering in which the switch could reach them. This file is the adversarial half of that argument: it
reads none of that source, it drives the real attack corpus through the real chokepoint with the
switch off and asks what actually comes back.

**Every assertion is on the RULE, never on `status == "refused"` alone.** That is ACE-071's handoff
rule and it is not pedantry here: with the pass off, a bug that made the chokepoint refuse everything
for some unrelated reason (an unreachable audit store is one line of fixture away from happening)
would satisfy a status-only assertion on every vector in the file while proving nothing about the
gate the vector is named for. Pinning the rule and its `REASON_FOR_RULE` reason is what makes each
vector answer for its own gate.

The corpus is `tests/safety/corpus.py`, shared with the end-to-end drivers rather than restated here,
so a vector added there is driven under this posture too without anyone remembering to. `Case.rule`
holds a `guardrail.RULE_*` symbol and the reason is asserted as `guardrail.REASON_FOR_RULE[rule]`, so
the contract's own enum stays the single source and a vector cannot pin a reason it disagrees with.

Everything here runs on every PR. The read-only and recon vectors refuse above `_model_safety` and
before `_load_credentials`, so they reach no warehouse at all; the resource class needs only a SQLite
file seeded from the corpus's own schema, which is the same mechanism `tests/e2e/harness.py` drives
its availability vectors with (`LOW_ROW_CAP`, a deployment ceiling one below the seeded row count).
No Postgres, no container, no opt-in password.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402
import guardrail  # noqa: E402

from safety.corpus import CASES, SCHEMA  # noqa: E402

PROFILE = "acme"
AREA = "sales"

# The two gates that sit ABOVE the pass the switch turns off. Filtered by RULE rather than by the
# corpus's `cls` label on purpose: the class says what a vector ATTEMPTS, and two of the `recon`
# vectors (`information_schema`, `pg_catalog`) are refused by table scope, which IS part of the pass
# and so must not be asserted to survive it.
_PRE_MODEL_CASES = [
    case for case in CASES if case.rule in (guardrail.RULE_READ_ONLY, guardrail.RULE_RECON)
]

# The gate that sits BELOW it, applied around the executor rather than before it, so proving it needs
# a statement that really runs.
_RESOURCE_CASES = [case for case in CASES if case.rule == guardrail.RULE_RESOURCE_LIMIT]

# The deployment ceiling the resource vectors are driven under, derived from the seed data exactly as
# `tests/e2e/harness.py::LOW_ROW_CAP` derives it: `orders` seeds three rows, so a ceiling one below
# that is over-run by the plain projection and further over-run by the cross join. Derived here rather
# than imported from that module because importing it would drag in the end-to-end transport
# dependency guard (`itdeps.importorfail` at its module scope), and this file must run on every PR
# whether or not the HTTP transport's extras are installed.
_LOW_ROW_CAP = len(SCHEMA["orders"]["rows"]) - 1


class _ExecutorReached(BaseException):
    """Raised by the executor these vectors must never reach.

    A `BaseException` rather than the `AssertionError` the neighbouring suites use, and the reason is
    specific to this file: `execute_guarded` is TOTAL, so its catch-all would absorb an
    `AssertionError` into a `failed` Envelope and the test would then report "expected refused, got
    failed" instead of "the statement reached the database". The first message sends a reader to the
    gate; the second sends them here. `BaseException` escapes the chokepoint's three handlers, so the
    failure names itself.
    """


class _NeverExecutes:
    """The warehouse, absent by construction rather than mocked away."""

    def execute(self, sql, creds, *, profile=None, **kwargs):
        raise _ExecutorReached(f"the statement reached the executor: {sql!r}")


def _seed_warehouse(path: Path) -> None:
    """Create and seed the SQLite warehouse `safety.corpus.SCHEMA` describes.

    The same derivation the end-to-end harness makes, for the same reason: the seeded row count is
    what `_LOW_ROW_CAP` is computed against, so a corpus that changed its seed data moves the ceiling
    with it instead of leaving a hard-coded number behind that no longer over-runs.
    """
    con = sqlite3.connect(path)
    try:
        for name, spec in SCHEMA.items():
            columns = ", ".join(f"{column} {type_}" for column, type_ in spec["columns"])
            con.execute(f"CREATE TABLE {name} ({columns})")
            placeholders = ", ".join("?" for _ in spec["columns"])
            con.executemany(f"INSERT INTO {name} VALUES ({placeholders})", spec["rows"])
        con.commit()
    finally:
        con.close()


def _pass_off(tmp_path: Path, monkeypatch) -> None:
    """Hosted, with the semantic-model pass switched OFF: the posture every vector here runs under.

    `AGAMI_GOVERNANCE_ENFORCED` is DELETED rather than set to a false spelling, because deleting it is
    the deployment posture the spec actually ships (the switch defaults off) and because the suite's
    autouse fixture in `tests/conftest.py` pins it on for every other file. Setting a falsy value
    would test the parser; deleting it tests the default, which is the security-relevant direction.

    The app database is a REACHABLE SQLite file, not an unreachable URL. `_audit_store_reachable`
    runs above every gate below it, so an unreachable store turns all of these into
    `audit_unavailable` and the file would go green measuring the wrong refusal entirely.

    The artifacts directory is pointed at nothing that exists. With the pass off no model is consulted
    at all, and proving that is part of the point: a vector that only refused because a model happened
    to be resolvable would not be proving the gate is above the pass.
    """
    monkeypatch.setenv("AGAMI_DB_URL", "sqlite://" + str(tmp_path / "app.db"))
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    monkeypatch.delenv("AGAMI_GOVERNANCE_ENFORCED", raising=False)
    monkeypatch.delenv("AGAMI_ORG_ID", raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "no_model"))


@pytest.fixture()
def pass_off(tmp_path, monkeypatch) -> None:
    """The posture, plus the two assertions that keep this whole file from being vacuous.

    Both are cheap and both have a real failure mode. If a future default flipped the switch on, every
    vector below would still refuse and every assertion would still pass, while the file no longer
    said anything about the off posture; and if `_hosted()` were false the switch would never be
    consulted at all and the vectors would be running under the ordinary local path.
    """
    _pass_off(tmp_path, monkeypatch)
    assert execute_sql._hosted() is True
    assert execute_sql._governance_enforced() is False


@pytest.fixture()
def pass_off_with_a_warehouse(tmp_path, monkeypatch) -> SimpleNamespace:
    """The same posture, with a real SQLite warehouse and the deployment ceiling lowered.

    The resource bound is applied around the executor, so unlike the two gates above it this one can
    only be reached by a statement that genuinely runs and genuinely returns more rows than the
    deployment allows.
    """
    _pass_off(tmp_path, monkeypatch)

    warehouse = tmp_path / "warehouse.db"
    _seed_warehouse(warehouse)
    monkeypatch.setenv(f"DATASOURCE_URL__{PROFILE.upper()}", f"sqlite:///{warehouse}")
    # Per test rather than per session: only the availability vectors want a tightened bound, and a
    # session-wide ceiling would turn an ordinary governed result into an availability refusal.
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", str(_LOW_ROW_CAP))

    assert execute_sql._hosted() is True
    assert execute_sql._governance_enforced() is False
    return SimpleNamespace(warehouse=warehouse, cap=_LOW_ROW_CAP)


# ---------------------------------------------------------------------------
# The gates above the pass: read-only and recon
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", _PRE_MODEL_CASES, ids=[case.id for case in _PRE_MODEL_CASES])
def test_a_read_only_or_recon_vector_is_still_refused_with_the_pass_off(case, pass_off):
    """Each vector, through the real chokepoint, with the switch off, and refused by its OWN rule.

    The executor raises if it is called, so "did this become executable?" is answered by construction
    rather than by inferring it from a status. Nothing here needs credentials: both gates decide above
    `_load_credentials`, which is why the whole class runs on every PR with no warehouse.
    """
    envelope = execute_sql.execute_guarded(case.sql, PROFILE, AREA, executor=_NeverExecutes())

    assert envelope.status == "refused", envelope.refusal or envelope.failure
    assert envelope.refusal.rule == case.rule
    assert envelope.refusal.reason == guardrail.REASON_FOR_RULE[case.rule]


def test_the_read_only_and_recon_vector_set_is_not_empty():
    """A corpus refactor that dropped every one of these vectors would leave the test above
    parametrized over nothing, and a run that collects zero items reports green.

    The set is asserted rather than merely counted: the file claims to cover BOTH gates, so a refactor
    that kept the read-only vectors and lost the recon ones would otherwise still pass while half the
    claim went untested.
    """
    assert _PRE_MODEL_CASES, "the corpus has no read_only or recon vectors left to drive"
    assert {case.rule for case in _PRE_MODEL_CASES} == {
        guardrail.RULE_READ_ONLY,
        guardrail.RULE_RECON,
    }


def test_the_two_gates_are_reached_without_any_model_at_all(pass_off):
    """The structural claim, stated once rather than left implicit in the class above.

    With the pass off there is no resolvable model anywhere: the app database is empty and the
    artifacts directory does not exist. A write and a fingerprinting probe are still refused, which is
    what "these gates are composed above the pass" means in observable terms. The same two statements
    under an ENFORCING deployment would be refused by the same two rules, so this is not asserting a
    difference; it is asserting the absence of one.
    """
    assert execute_sql._resolve_guard_model(PROFILE) is None

    write = execute_sql.execute_guarded(
        "DELETE FROM orders", PROFILE, AREA, executor=_NeverExecutes()
    )
    assert write.refusal.rule == guardrail.RULE_READ_ONLY

    probe = execute_sql.execute_guarded(
        "SELECT version()", PROFILE, AREA, executor=_NeverExecutes()
    )
    assert probe.refusal.rule == guardrail.RULE_RECON


# ---------------------------------------------------------------------------
# The gate below the pass: the deployment's result bound
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", _RESOURCE_CASES, ids=[case.id for case in _RESOURCE_CASES])
def test_a_resource_vector_is_still_refused_with_the_pass_off(case, pass_off_with_a_warehouse):
    """The bound is applied around the executor, so the switch cannot reach it from above either.

    This half is the one that needed a warehouse, and it needed only a SQLite file: the ceiling is a
    deployment variable (`AGAMI_SQL_MAX_ROWS`) rather than anything engine-specific, and the seeded
    corpus tables over-run it on both vectors. Nothing about the class required the Postgres `db_path`
    driver, so it is proved here on every PR rather than only in the job that has a container.
    """
    envelope = execute_sql.execute_guarded(
        case.sql, PROFILE, AREA, executor=execute_sql.BUILTIN_EXECUTOR
    )

    assert envelope.status == "refused", envelope.refusal or envelope.failure
    assert envelope.refusal.rule == case.rule
    assert envelope.refusal.reason == guardrail.REASON_FOR_RULE[case.rule]
    # A refusal carries no data, and on this rule that is the whole substance of it: the executor
    # holds whichever rows the engine emitted first, which with no ORDER BY is an arbitrary sample.
    assert envelope.data is None


def test_the_resource_vector_set_is_not_empty():
    """The same guard as its sibling above, for the same reason: zero parametrized vectors is a
    passing run that asserts nothing."""
    assert _RESOURCE_CASES, "the corpus has no resource_limit vectors left to drive"
    assert {case.rule for case in _RESOURCE_CASES} == {guardrail.RULE_RESOURCE_LIMIT}


def test_a_result_inside_the_ceiling_still_answers_with_the_pass_off(pass_off_with_a_warehouse):
    """The other half of the bound, and what stops the class above from passing on a broken fixture.

    A warehouse that failed to seed, a DSN that resolved to nothing, or a ceiling accidentally set to
    zero rows would refuse `resource_limit` for a reason that has nothing to do with the bound, and
    every assertion in the parametrized test would still be satisfied. A statement whose result fits
    under the same ceiling has to come back `ok` with its rows, or the refusals above are not
    evidence of anything.
    """
    envelope = execute_sql.execute_guarded(
        f"SELECT id FROM orders LIMIT {pass_off_with_a_warehouse.cap}",
        PROFILE,
        AREA,
        executor=execute_sql.BUILTIN_EXECUTOR,
    )

    assert envelope.status == "ok", envelope.refusal or envelope.failure
    assert len(envelope.data.rows) == pass_off_with_a_warehouse.cap
