"""The declared-filter report, at the distances a unit test cannot see across.

Three slices built this determination and pinned it where it is computed: the resolver against the
walk it replaced, `check_declared_filters` against the applied/omitted/undetermined matrix, and the
`tables` item shape against the receipt's four-state contract. Every one of those assertions holds a
function to its own promise. None of them can see whether that promise survives the trip to a
caller, and each of the six properties below is a way it could stop surviving without a single one
of those tests going red.

**A report inside a bound is not the same thing as a report.** `_RECEIPT_MAX_REFS` truncates the
reference list, and the filter determination is computed only for the references that survive it.
Compute the two against different lists and the section reports one reference's filters under
another one's name; raise the bound to "fit the report in" and a fifty-line statement becomes a
four-hundred-entry response the caller paid nothing for. Both failures live at the assembler, above
every unit under it.

**A finding that refuses is not a finding.** The whole spec rests on a declared filter being
business logic rather than policy: "how many orders were deleted?" is a question, not a breach.
`check_declared_filters` cannot enforce that, because it does not decide statuses — the gates above
it do, and a later gate could learn to refuse on `omitted` without touching a line this file's
siblings cover. What holds the line is the shape of the refusal vocabulary itself, plus one
statement that omits a filter and comes back `ok` anyway.

**The one outcome that may carry the report is the one a caller cannot provoke.** A declared filter
names columns and literals the MODEL author wrote, so it is model metadata riding on an answer, and
a caller who can force a non-ok body at will must not be able to read it out. `refused` was pinned
when the report landed; `failed` is the sibling case, and it is the one that is easy to get wrong,
because a `failed` body looks like a subset of an `ok` body and is not — a column the model declares
and the warehouse lacks can ONLY ever come back `failed`.

**Two processes compute this, and only one of them is the one you are debugging.** `tools` runs the
chokepoint in-process when an executor is injected and forks `python -m execute_sql` otherwise, and
on the fork the child's Envelope dies at the process boundary and the PARENT re-assembles the
receipt. A report wired into one builder and not the other is invisible to everything that tests
either builder alone, and the default local path is the forked one.

**"The same receipt for the same SQL" is a claim about processes, not about calls.** A status
derived from a set or a dict iteration is stable within one interpreter and different in the next,
so the only instrument that can catch it is a second process with a different `PYTHONHASHSEED`.

**And a receipt nobody recorded is a receipt nobody can audit.** The answer and the audit row are
built from one Envelope on purpose; what this file pins is that they still are, filters and all.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any, get_args

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
from semantic_model import loader as L  # noqa: E402
from semantic_model import runtime as rt  # noqa: E402

PROFILE = "acme"

# --- fixture ----------------------------------------------------------------
#
# Written in this file rather than imported from another test's model, for the reason the two
# sibling ACE-099 files give: the declared filters ARE the specification of every assertion below,
# so an edit to somebody else's fixture must not be able to change what a test here means.
#
# Two declared filters on one table, because one cannot tell "the report was computed" from "the
# report happened to be a single entry". The second one is also the leak canary: neither its column
# nor its literal is written by any statement in this file, so either one appearing in a `failed`
# body came out of the model's YAML rather than out of the request.
CANARY_COLUMN = "region"
CANARY_LITERAL = "internal-sandbox"
DECLARED_FILTERS = [
    "{alias}.is_deleted = false",
    f"{{alias}}.{CANARY_COLUMN} <> '{CANARY_LITERAL}'",
]
# Declared by the model and deliberately ABSENT from the warehouse the fixture builds, which is what
# makes a statement naming it pass every gate and then be rejected by the driver. The same device
# `test_ace088_non_ok_disclosure.py` uses to reach a `failed` outcome for real.
ABSENT_COLUMN = "amount"


def _write_model(root: Path) -> None:
    """`orders`, declaring two filters, plus a `customers` table declaring none.

    A real model on disk rather than a stub: every route below resolves its own model from the
    artifacts dir, and the fork route resolves it in a different process entirely.
    """
    import yaml

    (root / "subject_areas" / "sales" / "tables").mkdir(parents=True)
    (root / "datasource.yaml").write_text(
        yaml.safe_dump({"datasource": "Acme", "version": 1,
                        "storage_connections": [{"name": "c", "storage_type": "SQLite"}],
                        "subject_areas": ["subject_areas/sales"]})
    )
    (root / "subject_areas" / "sales" / "subject_area.yaml").write_text(
        yaml.safe_dump({"name": "sales", "tables": [
            {"storage_connection": "c", "schema": "public", "table": "orders"},
            {"storage_connection": "c", "schema": "public", "table": "customers"}]})
    )
    (root / "subject_areas" / "sales" / "tables" / "orders.yaml").write_text(
        yaml.safe_dump({
            "name": "orders", "schema": "public", "storage_connection": "c", "grain": ["id"],
            "description": "orders",
            "default_filters": DECLARED_FILTERS,
            "columns": [
                {"name": "id", "type": "integer", "primary_key": True},
                {"name": "customer_id", "type": "integer"},
                {"name": "is_deleted", "type": "boolean"},
                {"name": ABSENT_COLUMN, "type": "integer"},
            ],
        })
    )
    (root / "subject_areas" / "sales" / "tables" / "customers.yaml").write_text(
        yaml.safe_dump({
            "name": "customers", "schema": "public", "storage_connection": "c", "grain": ["id"],
            "description": "customers",
            "columns": [{"name": "id", "type": "integer", "primary_key": True}],
        })
    )


@pytest.fixture(autouse=True)
def _isolate():
    """`_INJECTED_EXECUTOR` is a process global — `create_app()` sets it and the in-process route
    sets it — so it must not leak between tests."""
    tools.set_injected_executor(None)
    yield
    tools.set_injected_executor(None)


@pytest.fixture()
def declared(tmp_path, monkeypatch):
    """The model above under profile `acme`, plus a real sqlite warehouse to execute against.

    The warehouse has `orders(id, customer_id, is_deleted)` and `customers(id)` and NOT
    `orders.amount`, so both an `ok` and a `failed` outcome are reachable without stubbing an
    executor — which matters because the fork route runs in a child process that no monkeypatch of
    this one reaches. `DATASOURCE_URL__ACME` is an environment variable and the child inherits it.
    """
    artifacts = tmp_path / "artifacts"
    _write_model(artifacts / PROFILE)

    warehouse = tmp_path / "warehouse.db"
    con = sqlite3.connect(warehouse)
    con.execute("CREATE TABLE orders (id INTEGER, customer_id INTEGER, is_deleted INTEGER)")
    con.execute("CREATE TABLE customers (id INTEGER)")
    con.executemany("INSERT INTO orders (id, customer_id, is_deleted) VALUES (?, ?, ?)",
                    [(1, 10, 0), (2, 10, 1)])
    con.execute("INSERT INTO customers (id) VALUES (10)")
    con.commit()
    con.close()

    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(artifacts))
    monkeypatch.setenv("DATASOURCE_URL__ACME", f"sqlite:///{warehouse}")
    # Local, not hosted: the disk model is the one the gates read and the receipt is built against.
    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    monkeypatch.delenv("AGAMI_ORG_ID", raising=False)
    monkeypatch.delenv("AGAMI_SQL_TIMEOUT_S", raising=False)
    return SimpleNamespace(artifacts=artifacts, profile_root=artifacts / PROFILE)


def _org(declared):
    return L.load_datasource(declared.profile_root)


def _route_in_process(sql: str) -> dict:
    """`execute_guarded` runs in THIS process, so the receipt it built is the one that is emitted."""
    tools.set_injected_executor(execute_sql.BUILTIN_EXECUTOR)
    return json.loads(tools.tool_execute_sql({"sql": sql, "datasource": PROFILE}))


def _route_fork(sql: str) -> dict:
    """A real `python -m execute_sql` subprocess. The child's Envelope dies at the process boundary
    and the PARENT re-assembles the receipt, so this is a different builder for the same facts."""
    tools.set_injected_executor(None)
    return json.loads(tools.tool_execute_sql({"sql": sql, "datasource": PROFILE}))


ROUTES = {"in_process": _route_in_process, "fork": _route_fork}

# One table read twice under two scopes, plus a reference to the CTE itself and a table declaring no
# filters. The CTE body writes the first declared filter and nothing writes the second, so a single
# statement produces `applied`, `omitted` and an empty list — enough that a route computing none of
# it, or computing it against the wrong reference, cannot match by accident.
SCOPED_SQL = (
    "WITH recent AS (SELECT id, customer_id FROM orders WHERE orders.is_deleted = false) "
    "SELECT c.id FROM customers c JOIN recent r ON r.customer_id = c.id"
)

# What every route, every process and the audit record must agree on for `SCOPED_SQL`.
EXPECTED_FILTER_ITEMS = [
    ("customers", "main", []),
    ("recent", "main", []),
    ("orders", "cte:recent", [
        ("orders.is_deleted = false", "applied"),
        (f"orders.{CANARY_COLUMN} <> '{CANARY_LITERAL}'", "omitted"),
    ]),
]


# One table read twice, in two arms of a set operation, under different aliases. The leading arm
# writes the first declared filter and the trailing arm writes nothing, so the two references
# disagree — which is exactly the pair that read identically before the arm ordinal existed.
ARMED_SQL = (
    "SELECT o.id FROM orders o WHERE o.is_deleted = false "
    "UNION ALL SELECT o2.id FROM orders o2"
)

# What every route must agree on for `ARMED_SQL`. The two entries differ ONLY by alias, ordinal and
# verdict, so a route that dropped the ordinal would produce two rows a reader cannot tell apart.
EXPECTED_ARM_ITEMS = [
    ("orders", "main#1", [
        ("o.is_deleted = false", "applied"),
        (f"o.{CANARY_COLUMN} <> '{CANARY_LITERAL}'", "omitted"),
    ]),
    ("orders", "main#2", [
        ("o2.is_deleted = false", "omitted"),
        (f"o2.{CANARY_COLUMN} <> '{CANARY_LITERAL}'", "omitted"),
    ]),
]


def _filter_items(receipt: dict) -> list[tuple[str, str, list[tuple[str, str]]]]:
    """(ref, scope, [(declared text, status), …]) per listed reference, in the section's own order."""
    return [
        (i["ref"], i["scope"], [(f["expr"], f["status"]) for f in i["filters"]])
        for i in receipt["tables"]["items"]
    ]


def _all_keys(node: Any) -> set[str]:
    """Every dict key appearing anywhere in a body, however deeply nested.

    Structural rather than a substring sweep of the serialized text: `"scope"` and `"filters"` are
    common enough words that a text search would be satisfied by prose in an `undetermined` marker,
    and the claim is about KEYS reaching the caller.
    """
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in _all_keys(v)}
    if isinstance(node, list):
        return {k for v in node for k in _all_keys(v)}
    return set()


# ---------------------------------------------------------------------------
# SC-1 / SC-4 — the report rides inside the reference cap, which is unchanged
# ---------------------------------------------------------------------------


def test_the_reference_cap_is_the_bound_it_has_always_been(declared):
    """SC-1, quoted: "`_RECEIPT_MAX_REFS` already bounds that list and counts what it dropped; this
    spec does not raise or bypass the cap."

    Named as a number rather than left implicit, because the tempting way to make a filter report
    "complete" is to widen the list it rides on — and the bound is not about completeness. It is the
    response-amplification bound: one entry per name the CALLER's statement wrote, so a statement
    inventing four hundred references would otherwise turn a small request into a large answer at no
    cost to whoever asked for it.
    """
    assert rt._RECEIPT_MAX_REFS == 50


def test_a_statement_past_the_cap_lists_fifty_references_each_with_its_own_filters(declared):
    """The two halves that have to hold together, and that a unit test of either one cannot see.

    The determination is computed for the references that SURVIVE the cap and no others, from the
    same truncated list the items are built from. Compute the two against different lists — cap one
    and not the other — and the section reports one reference's filters under another reference's
    name, which is worse than reporting nothing: it is a confident, wrong answer about which read of
    a table was filtered.

    The overflow is COUNTED and never listed, and the count is the caller's own number, so stating
    it discloses nothing. `undetermined` here is the cap clause ALONE: every listed reference is
    accounted for (both declared filters read `omitted`, which is a determination), so the section's
    other clause must not appear — a marker that said references could not be accounted for would be
    a false claim standing beside fifty items that each say otherwise.
    """
    over = rt._RECEIPT_MAX_REFS + 5
    sql = "SELECT o0.id FROM orders o0 " + " ".join(
        f"JOIN orders o{i} ON o{i}.id = o0.id" for i in range(1, over)
    )

    section = rt.assemble_receipt(_org(declared), sql)["tables"]

    assert len(section["items"]) == rt._RECEIPT_MAX_REFS
    for item in section["items"]:
        assert [f["status"] for f in item["filters"]] == ["omitted", "omitted"], item
        assert [f["expr"] for f in item["filters"]] == [
            f"{item['alias']}.is_deleted = false",
            f"{item['alias']}.{CANARY_COLUMN} <> '{CANARY_LITERAL}'",
        ], item
    assert section["undetermined"] == "5 further reference(s) are not listed."


# ---------------------------------------------------------------------------
# SC-3 — a statement that omits a declared filter runs, and is reported
# ---------------------------------------------------------------------------


def test_the_refusal_vocabulary_has_no_member_a_declared_filter_could_fill(declared):
    """SC-3's structural half. "Refusing on an omitted filter" is out of scope by construction, not
    by convention: `RefusalReason` is a closed three-member Literal, and a declared filter the
    statement did not write is none of them. It is not `unsafe` — nothing about the statement is
    hazardous. It is not `out_of_scope` — every name in it is declared. It is not `undetermined` —
    the receipt determined the answer and says so.

    Asserted against the type rather than by grepping for the word "filter", because the property is
    that a fourth member cannot appear without editing one line a reviewer sees in the diff. Every
    rule that HAS a pinned reason is checked to land inside that set, so a rule added for a declared
    filter would have to widen the vocabulary here first.
    """
    assert set(get_args(guardrail.RefusalReason)) == {"unsafe", "out_of_scope", "undetermined"}
    assert set(guardrail.REASON_FOR_RULE.values()) <= set(get_args(guardrail.RefusalReason))


@pytest.mark.parametrize("route", list(ROUTES), ids=list(ROUTES))
def test_a_statement_that_omits_a_declared_filter_answers_and_reports_the_omission(
    declared, route
):
    """SC-3's reported half, end to end.

    `test_ace042_no_filter_injection.py::test_asking_about_the_rows_a_filter_excludes_is_not_refused`
    already pins the non-refusal at `_model_safety`, which is where a refusal would be decided. What
    it cannot say is what the caller is TOLD: a statement that runs while silently dropping the
    model's own definition of the table is exactly the answer ACE-042 left unexplained, and half a
    fix — it runs, and nobody is told — is the state this spec exists to end.

    So both facts at once, through `tool_execute_sql`: status `ok`, rows back, AND the omission on
    the receipt the caller received. Deliberately a question ABOUT the excluded rows, because that
    is the case where refusing is most tempting and most wrong.
    """
    body = ROUTES[route]("SELECT COUNT(o.id) AS n FROM orders o")

    assert body["status"] == "ok", body
    assert body["rows"] == [["2"]], "every row, including the one the declared filter excludes"
    assert _filter_items(body["receipt"]) == [
        ("orders", "main", [
            ("o.is_deleted = false", "omitted"),
            (f"o.{CANARY_COLUMN} <> '{CANARY_LITERAL}'", "omitted"),
        ]),
    ]
    assert "refusal" not in body


# ---------------------------------------------------------------------------
# SC-5 — the report rides the `ok` receipt only, and `failed` is its own channel
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", list(ROUTES), ids=list(ROUTES))
def test_a_failed_body_carries_no_filter_report_and_no_declared_filter_text(declared, route):
    """SC-5 on `failed`, the status that looks like a subset of `ok` and is not.

    `orders.amount` is declared by the model and absent from the warehouse, so this statement passes
    every gate and can ONLY come back `failed` — `ok` is structurally unreachable for it, which
    makes `failed` a disclosure channel a caller can aim, one deliberately-chosen name at a time.
    The vector is `test_ace088_non_ok_disclosure.py`'s, reused rather than reinvented; what is new
    is the payload being probed for.

    A declared filter is the model author's own text: `region`, `'internal-sandbox'` — a column they
    chose to scope the table by, and a literal that names something about their business. Neither
    was written by the statement here. Pinned three ways, because each catches a different mistake:
    the item shape (nothing named `filters` or `scope` reached an item), the KEY sweep of the whole
    body (the report did not arrive under some other name instead), and the text sweep (the filter's
    words did not arrive as prose).
    """
    body = ROUTES[route](f"SELECT o.{ABSENT_COLUMN} FROM orders o")

    assert body["status"] == "failed", body
    assert {frozenset(i) for i in body["receipt"]["tables"]["items"]} == {
        frozenset({"ref", "declared"})
    }
    keys = _all_keys(body)
    assert "filters" not in keys and "scope" not in keys, sorted(keys)
    text = json.dumps(body)
    for leaked in (CANARY_COLUMN, CANARY_LITERAL, "is_deleted"):
        assert leaked not in text, f"{leaked!r} leaked into: {text}"


# ---------------------------------------------------------------------------
# REQ-022 — the two execution paths report the same thing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", list(ROUTES), ids=list(ROUTES))
def test_each_execution_path_reports_the_filters_each_reference_applied(declared, route):
    """The CTE determination, delivered — not merely computed.

    `orders` is read once inside a CTE that writes the declared soft-delete predicate. That
    reference applied it; the second declared filter it did not; `customers` declares nothing and
    `recent` is a name the statement defined for itself, so it never resolves to a model table
    however closely it matches. Four different answers about one statement, all of which have to
    survive whichever process ran it.

    The fork column is the one that can go quiet on its own: the child assembles a receipt inside a
    process whose Envelope dies at the boundary, and the parent builds the emitted one from the
    RECEIVED statement. A report wired into the chokepoint's builder alone would pass every
    in-process assertion in this repo and reach no local user at all, because forking is the default.
    """
    body = ROUTES[route](SCOPED_SQL)

    assert body["status"] == "ok", body
    assert _filter_items(body["receipt"]) == EXPECTED_FILTER_ITEMS


@pytest.mark.parametrize("route", list(ROUTES), ids=list(ROUTES))
def test_each_execution_path_numbers_the_arms_of_a_set_operation(declared, route):
    """The arm ordinal, delivered rather than merely computed.

    One table read in two arms, one applying the declared soft-delete predicate and one not. Before
    the ordinal both references reported `scope: "main"`, so the receipt held two rows that differed
    only in a verdict with nothing to attribute it to — a reader could see that some arm dropped the
    filter and not which. The ordinal is the whole of what distinguishes them here, which is why the
    two expected entries are otherwise near-identical.

    Parametrized over both routes for the reason the sibling test above gives: the ordinal is
    composed below the point where the two builders diverge, so they cannot disagree by
    construction — and this is what keeps that true rather than assumed. Forking is the default
    path, so a fact that reached only the in-process builder would reach no local user at all.
    """
    body = ROUTES[route](ARMED_SQL)

    assert body["status"] == "ok", body
    assert _filter_items(body["receipt"]) == EXPECTED_ARM_ITEMS


def test_the_two_execution_paths_report_identical_filter_items(declared):
    """The comparison the parametrize above cannot make: the two answers against each other.

    Asserted directly as well as against the expectation, because "both routes match the literal in
    this file" and "the two routes agree" fail differently. A future change that moves the report
    behind a flag only one route reads would keep one column green and this red, which is the signal
    worth having.
    """
    in_process = _route_in_process(SCOPED_SQL)
    forked = _route_fork(SCOPED_SQL)

    assert in_process["status"] == forked["status"] == "ok", (in_process, forked)
    assert in_process["receipt"]["tables"] == forked["receipt"]["tables"]


# ---------------------------------------------------------------------------
# SC-8 — what was reported reaches the audit record
# ---------------------------------------------------------------------------


def test_one_envelope_reaches_both_the_returned_body_and_the_recorder(
    declared, monkeypatch, tmp_path
):
    """ONE Envelope describes one answer to two audiences, joined by the id the caller carries away.

    `_emit` attaches `env.receipt` to the body and then hands the SAME `env` to `_record_execution`.
    Nothing enforces that ordering except the control flow, and it is exactly the kind of thing a
    later refactor reorders: build the record from a re-derived receipt, or record before the
    receipt is resolved, and the trail describes a different answer from the one the caller was
    given while both look perfectly well-formed.

    So: the Envelope the recorder was handed is captured and compared against the body the caller
    received, and the persisted row is located by the `audit_id` the caller carried away — which is
    what makes "recorded" mean a row a reviewer can actually find rather than a call that happened.

    WHAT THIS DOES NOT SHOW, stated as narrowly as it now has to be: this test does not read the
    PERSISTED receipt. It used to say the receipt was not stored at all, which was true when it was
    written and stopped being true with ACE-098 — `QueryExecutionRecord` carries the whole receipt
    and its `model_version`, and `_record_execution` writes them to the same row this test locates
    by `audit_id`. What is asserted here is still the identity above, between the body and the
    Envelope the recorder was handed, plus the row's id; the row's own `receipt` column is not read.
    """
    log = tmp_path / "query_log.jsonl"
    monkeypatch.setattr(tools, "QUERY_LOG", log)
    recorded: list[guardrail.Envelope] = []
    real = tools._record_execution
    monkeypatch.setattr(
        tools, "_record_execution",
        lambda env, **kw: (recorded.append(env), real(env, **kw))[1],
    )

    body = _route_in_process(SCOPED_SQL)

    assert body["status"] == "ok", body
    assert len(recorded) == 1, recorded
    # Through `json` because `ReceiptSection.items` is a tuple — frozen types hold no lists — and the
    # wire has no tuple.
    recorded_tables = json.loads(json.dumps(asdict(recorded[0].receipt)))["tables"]
    assert recorded_tables == body["receipt"]["tables"]
    assert _filter_items({"tables": recorded_tables}) == EXPECTED_FILTER_ITEMS

    rows = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert [r["id"] for r in rows] == [body["audit_id"]], rows
    assert rows[0]["status"] == "ok"


# ---------------------------------------------------------------------------
# REQ-022 — the same receipt in every process
# ---------------------------------------------------------------------------

_PROBE = """
import json, sys
sys.path.insert(0, sys.argv[1])
from semantic_model import loader as L
from semantic_model import runtime as rt
org = L.load_datasource(sys.argv[3])
print(json.dumps(rt.assemble_receipt(org, sys.argv[2])["tables"], sort_keys=True))
"""


def test_the_filter_report_is_the_same_in_every_process(declared):
    """REQ-022: the receipt is "the same for the same SQL and model version" — a claim about
    processes, which is why nothing inside one can test it.

    Two failure modes hide behind a seed. A status derived from a set or a dict iteration is stable
    within an interpreter and different in the next, and so is any ORDER decided by walking one: the
    item order here comes from a list walk of the parse tree and the filter order from the model's
    own declaration list, so both should be fixed — but "should be" is the state this instrument
    exists to check. Four seeds, four processes, one answer.

    The whole `tables` section is compared rather than the statuses alone, so either mode is caught:
    a re-ordered item list and a flipped status both change the serialized section, and neither
    changes a count.
    """
    seen = set()
    for seed in ("0", "1", "42", "12345"):
        proc = subprocess.run(
            [sys.executable, "-c", _PROBE, str(PKG_SRC), SCOPED_SQL, str(declared.profile_root)],
            capture_output=True, text=True, env={**os.environ, "PYTHONHASHSEED": seed},
        )
        assert proc.returncode == 0, proc.stderr
        seen.add(proc.stdout.strip())

    assert len(seen) == 1, f"the filter report differed across hash seeds: {seen}"
    # And it is the same answer this process gets, so the four agreeing on something wrong would
    # still fail rather than agree quietly.
    assert _filter_items({"tables": json.loads(seen.pop())}) == EXPECTED_FILTER_ITEMS
