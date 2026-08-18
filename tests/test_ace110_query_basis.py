"""An agent can say what it based a query on, and why (ACE-110).

The activity log has always recorded what ran, and two self-reported columns carrying the caller's
framing of the question. What it never recorded is the reasoning in between — which example was
mirrored, which table was chosen, why a filter is there. `basis` is that, bounded at the boundary and
never adjudicated: core records the claim and leaves checking it to whoever holds the receipt.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")

PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import admin  # noqa: E402
import tools  # noqa: E402
from store import Store  # noqa: E402

ENTRY = {"kind": "example", "ref": "88e825e75112", "why": "closest match to the question"}


@pytest.fixture
def db(tmp_path, monkeypatch):
    url = "sqlite://" + str(tmp_path / "calls.db")
    monkeypatch.setenv("AGAMI_DB_URL", url)
    s = Store.connect(url)
    s.run_migrations()
    s.close()
    return url


def _rows(url):
    s = Store.connect(url)
    rows = s.query("SELECT * FROM tool_calls ORDER BY ts")
    s.close()
    return rows


def _record(basis=None, **extra):
    args = {"datasource": "SALES_DATA", "sql": "SELECT 1", **extra}
    if basis is not None:
        args["basis"] = basis
    tools.record_tool_call(
        name="execute_sql",
        arguments=args,
        result_text='{"row_count": 1}',
        execution_ms=4,
        actor="jordan@example.com",
    )


def _stored(raw):
    return json.loads(raw)


# --- the bound ---------------------------------------------------------------------------------


def test_no_basis_is_no_column():
    """Criterion 1's half that lives in the writer: absent must be NULL, not an empty envelope, or a
    call made by a client that never heard of the field stops matching one made before it existed."""
    assert tools._bounded_basis(None) is None
    assert tools._bounded_basis([]) is None


@pytest.mark.parametrize("raw", ["a string", 7, {"kind": "table"}, True])
def test_something_that_was_never_a_list_is_ignored(raw):
    assert tools._bounded_basis(raw) is None


def test_a_well_formed_entry_survives_verbatim():
    doc = _stored(tools._bounded_basis([ENTRY]))
    assert doc == {"entries": [ENTRY], "truncated": False}


# Written out rather than derived from `tools.BASIS_KINDS`: a test that builds its expectation from
# the constant it is checking asserts `X == X` and cannot fail. These eight are the contract.
DECLARED_KINDS = [
    "date_range",
    "entity",
    "example",
    "filter",
    "glossary",
    "join",
    "metric",
    "table",
]


def test_every_declared_kind_is_accepted():
    """The kinds the description advertises and the set enforced at the boundary have to be the same
    set, or a compliant client sends something the boundary silently drops."""
    entries = [{"kind": k, "ref": "x", "why": "y"} for k in DECLARED_KINDS]
    doc = _stored(tools._bounded_basis(entries))
    assert [e["kind"] for e in doc["entries"]] == DECLARED_KINDS
    assert doc["truncated"] is False


def test_the_advertised_kinds_and_the_enforced_set_agree():
    """The kinds are prose on the property rather than a schema `enum`, so nothing but this test
    holds the sentence the model reads and the set the boundary enforces to the same eight."""
    described = tools.TOOLS["execute_sql"]["inputSchema"]["properties"]["basis"]["description"]
    assert tools.BASIS_KINDS == set(DECLARED_KINDS)
    for kind in DECLARED_KINDS:
        assert kind in described, kind


def test_the_basis_schema_constrains_nothing_the_sdk_could_refuse():
    """The MCP SDK validates arguments against this schema BEFORE the handler runs, so every
    constraint here is a whole-query refusal rather than a bound — and this field is optional and
    advisory. `_bounded_basis` truncates instead, which is what the spec asks for. Anything but
    `type` reappearing here is a regression that would make an over-long ref lose the user's answer.
    """
    prop = tools.TOOLS["execute_sql"]["inputSchema"]["properties"]["basis"]
    assert set(prop) == {"type", "description"} and prop["type"] == "array"


def test_a_payload_the_boundary_trims_is_accepted_by_the_schema():
    """The two halves have to agree: what `_bounded_basis` is willing to trim, the schema must be
    willing to accept, or the trim never runs."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = tools.TOOLS["execute_sql"]["inputSchema"]
    for args in (
        {"sql": "SELECT 1", "basis": [{"kind": "filter", "ref": "x" * 400, "why": "y" * 400}]},
        {"sql": "SELECT 1", "basis": [{"kind": "table", "ref": "t"}] * 40},
        {"sql": "SELECT 1", "basis": [{"kind": "made_up", "ref": "t", "extra": 1}]},
    ):
        jsonschema.validate(args, schema)  # raises if the schema would refuse instead of trim


@pytest.mark.parametrize("bad", ["nonsense", "", None, "EXAMPLE", 7, True])
def test_an_unknown_kind_is_dropped_at_the_boundary(bad):
    """Criterion 3, and the drop is the whole of it. The MCP SDK validates arguments against
    `inputSchema` before the handler runs, so a constraint declared there does not filter a bad entry
    out of `basis` — it refuses the entire call, taking the answer with it. That is why the property
    carries no `items` schema and the kinds are advertised as prose: enforcement belongs here, where
    an unknown kind costs its own entry and nothing else."""
    doc = _stored(tools._bounded_basis([{"kind": bad, "ref": "x", "why": "y"}, ENTRY]))
    assert doc["entries"] == [ENTRY]
    assert doc["truncated"] is True  # dropped is not verbatim, and the row has to say so


@pytest.mark.parametrize("unhashable", [["table"], {"a": 1}, [], {}])
def test_an_unhashable_kind_is_rejected_rather_than_raising(unhashable):
    """JSON hands us arbitrary types, and testing an array or object against a frozenset raises
    `TypeError`. That raise would come from the middle of building the audit record — after the query
    has already succeeded — and on the served path `record_tool_call` runs in an unwrapped `finally`,
    so it would replace a good answer with an internal error the caller sees."""
    doc = _stored(tools._bounded_basis([{"kind": unhashable, "ref": "x"}, ENTRY]))
    assert doc["entries"] == [ENTRY] and doc["truncated"] is True


@pytest.mark.parametrize("bad_ref", [None, "", 0, False, {"a": 1}, ["x"], 7])
def test_an_entry_with_no_usable_ref_is_dropped_and_flagged(bad_ref):
    """`ref` is required beside `kind`, so it is enforced the same way. Storing an entry whose ref
    went missing as if it were verbatim would tell an operator the agent named something when it
    did not — and a non-string ref would otherwise land in the column as a Python repr."""
    doc = _stored(tools._bounded_basis([{"kind": "filter", "ref": bad_ref, "why": "y"}, ENTRY]))
    assert doc["entries"] == [ENTRY] and doc["truncated"] is True


@pytest.mark.parametrize("bad", ["a string", 7, None, ["nested"]])
def test_an_entry_that_is_not_an_object_is_dropped(bad):
    doc = _stored(tools._bounded_basis([bad, ENTRY]))
    assert doc["entries"] == [ENTRY] and doc["truncated"] is True


@pytest.mark.parametrize("no_why", [{}, {"why": None}, {"why": ""}, {"why": 7}, {"why": ["x"]}])
def test_an_entry_with_no_usable_why_is_kept_with_an_empty_one(no_why):
    """`why` is NOT in the schema's `required`, unlike `kind` and `ref` — a ref with no justification
    is still a fact about what was chosen, so it is kept rather than dropped."""
    doc = _stored(tools._bounded_basis([{"kind": "table", "ref": "orders", **no_why}]))
    assert doc["entries"] == [{"kind": "table", "ref": "orders", "why": ""}]


def test_extra_keys_on_an_entry_do_not_ride_along():
    doc = _stored(tools._bounded_basis([{**ENTRY, "confidence": 0.9, "note": "x"}]))
    assert doc["entries"] == [ENTRY]


def test_all_entries_invalid_still_records_the_attempt():
    """An empty list is the same claim as no list — but an empty list that USED to hold something is
    not, and the flag is the only thing that distinguishes them."""
    doc = _stored(tools._bounded_basis([{"kind": "nope", "ref": "x"}]))
    assert doc == {"entries": [], "truncated": True}


def test_too_many_entries_are_capped_and_flagged():
    over = [
        {"kind": "table", "ref": f"t{i}", "why": "w"} for i in range(tools.BASIS_MAX_ENTRIES + 3)
    ]
    doc = _stored(tools._bounded_basis(over))
    assert len(doc["entries"]) == tools.BASIS_MAX_ENTRIES
    assert doc["truncated"] is True


def test_exactly_at_the_cap_is_not_flagged():
    at = [{"kind": "table", "ref": f"t{i}", "why": "w"} for i in range(tools.BASIS_MAX_ENTRIES)]
    doc = _stored(tools._bounded_basis(at))
    assert len(doc["entries"]) == tools.BASIS_MAX_ENTRIES and doc["truncated"] is False


def test_a_long_why_is_cut_and_flagged():
    doc = _stored(tools._bounded_basis([{**ENTRY, "why": "w" * (tools.BASIS_WHY_MAX_CHARS + 50)}]))
    assert len(doc["entries"][0]["why"]) == tools.BASIS_WHY_MAX_CHARS
    assert doc["truncated"] is True


def test_a_long_ref_is_cut_and_flagged():
    """`ref` is bounded for the same reason `why` is: for `filter` and `date_range` it IS the
    predicate, so it can carry a literal out of the customer's data."""
    long_ref = "region = '" + "x" * (tools.BASIS_REF_MAX_CHARS + 50) + "'"
    doc = _stored(
        tools._bounded_basis([{"kind": "filter", "ref": long_ref, "why": "asked for it"}])
    )
    assert len(doc["entries"][0]["ref"]) == tools.BASIS_REF_MAX_CHARS
    assert doc["truncated"] is True


# --- the surface -------------------------------------------------------------------------------


def test_basis_is_optional_and_the_schema_stays_closed():
    """Criterion 5. A client that has never heard of the field must be unaffected — which needs both
    halves: not required, and still declared, or `additionalProperties: false` makes it unsendable."""
    schema = tools.TOOLS["execute_sql"]["inputSchema"]
    assert "basis" not in schema["required"]
    assert "basis" in schema["properties"]
    assert schema["additionalProperties"] is False


def test_the_description_mentions_basis_once():
    """Criterion 7. The tool description ships on every request, so a second mention is paid forever."""
    assert tools.TOOLS["execute_sql"]["description"].count("basis") == 1


def test_the_description_tells_the_agent_its_note_will_be_trimmed_not_refused():
    """The model needs to know an over-long entry costs it the note, not the answer — otherwise the
    safe move is to send nothing."""
    described = tools.TOOLS["execute_sql"]["inputSchema"]["properties"]["basis"]["description"]
    assert "not refused" in described


def test_the_examples_tool_says_its_id_can_be_cited():
    """The `id` ACE-109 puts on every served example is inert unless the agent knows it may name it."""
    assert "basis" in tools.TOOLS["get_prompt_examples"]["description"]


# --- end to end --------------------------------------------------------------------------------


def test_a_call_without_basis_records_a_null_column(db):
    """Criterion 1, end to end."""
    _record(user_question="how many?", raw_query="count")
    row = _rows(db)[0]
    assert row["basis"] is None
    assert row["sql"] == "SELECT 1" and row["user_question"] == "how many?"


def test_sending_basis_perturbs_no_other_column(db):
    """Criterion 1's real claim — *byte-identical but for the column*. Asserting three fields would
    not catch a `basis` that quietly changed how anything else was derived, so this compares the two
    rows whole and names the only keys allowed to differ."""
    _record(user_question="how many?", raw_query="count")
    _record(user_question="how many?", raw_query="count", basis=[ENTRY])
    without, with_basis = _rows(db)
    volatile = {"id", "ts", "basis"}
    assert set(without) == set(with_basis)  # same columns, so the comparison below is total
    assert {k: v for k, v in without.items() if k not in volatile} == {
        k: v for k, v in with_basis.items() if k not in volatile
    }
    assert without["basis"] is None and with_basis["basis"] is not None


def test_the_migration_applies_to_a_table_that_already_has_rows(tmp_path):
    """Criterion 6. `ADD COLUMN` on a populated table is the case a deployment actually meets — an
    empty-database migration run never exercises it, and the existing rows must survive reading back
    with the new column NULL rather than the migration failing or rewriting them."""
    url = "sqlite://" + str(tmp_path / "existing.db")
    s = Store.connect(url)
    s.run_migrations()
    s.execute(
        "INSERT INTO tool_calls (id, ts, org_id, tool_name, source, success) VALUES (?,?,?,?,?,?)",
        ("row-1", "2026-08-17T00:00:00Z", "local", "execute_sql", "mcp", 1),
    )
    s.commit()
    # Re-running is a no-op via the ledger; the assertion is that the pre-existing row is intact and
    # simply carries NULL for the column that did not exist when it was written.
    s.run_migrations()
    row = s.query("SELECT id, basis FROM tool_calls WHERE id = ?", ("row-1",))[0]
    s.close()
    assert row["id"] == "row-1" and row["basis"] is None


def test_basis_round_trips_onto_the_row(db):
    """Criterion 2's write half."""
    _record(basis=[ENTRY])
    doc = _stored(_rows(db)[0]["basis"])
    assert doc == {"entries": [ENTRY], "truncated": False}


def test_the_stored_basis_reaches_the_reader(db):
    """`_TOOL_CALL_COLS` is the SELECT list and is narrower than the INSERT — a column missing from
    it is written on every row and read by nobody, so the view would render nothing forever."""
    import model_store

    _record(basis=[ENTRY], thread_id="t1")
    s = Store.connect(db)
    sessions = model_store.list_sessions(s)
    s.close()
    call = sessions[0]["turns"][0]["calls"][0]
    assert _stored(call["basis"])["entries"] == [ENTRY]


# --- the view ----------------------------------------------------------------------------------


def _card(basis_value):
    return admin._call_card(
        {
            "sql": "SELECT 1",
            "agent_query": "count",
            "ts": "2026-08-17T00:00:00Z",
            "datasource": "SALES_DATA",
            "execution_ms": 4,
            "success": 1,
            "row_count": 1,
            "tool_name": "execute_sql",
            "basis": basis_value,
        }
    )


def test_the_call_card_shows_the_basis_under_its_sql():
    """Criterion 2's render half — under the statement it explains, so the reasoning sits next to the
    SQL rather than in a separate pane."""
    html = _card(json.dumps({"entries": [ENTRY], "truncated": False}))
    assert "88e825e75112" in html and "closest match to the question" in html
    assert html.index("SELECT 1") < html.index("88e825e75112")
    assert "agent-reported" in html  # whose claim it is, like every other self-reported field


def test_a_basis_lands_under_its_own_call_not_a_neighbour():
    """The criterion is 'under the RIGHT statement'. A turn runs several calls and only some carry a
    basis, so rendering one card per call is not enough — the block has to stay inside the card whose
    SQL it explains."""
    with_basis = {
        "sql": "SELECT 1",
        "ts": "2026-08-17T00:00:00Z",
        "datasource": "SALES_DATA",
        "execution_ms": 4,
        "success": 1,
        "tool_name": "execute_sql",
        "basis": json.dumps({"entries": [ENTRY], "truncated": False}),
    }
    without = {**with_basis, "sql": "SELECT 2", "basis": None}
    html = admin._call_card(without) + admin._call_card(with_basis)
    assert html.index("SELECT 2") < html.index("SELECT 1") < html.index("88e825e75112")
    assert html.count("88e825e75112") == 1  # only the call that reported one


def test_a_truncated_basis_says_so_in_the_view():
    html = _card(json.dumps({"entries": [ENTRY], "truncated": True}))
    assert "truncated" in html


def test_a_card_with_no_basis_renders_nothing_extra():
    assert "basis" not in _card(None)


@pytest.mark.parametrize(
    "junk",
    [
        "not json",
        "[]",
        '{"no": "entries"}',
        "",
        '"a bare string"',
        "7",
        '{"entries": null}',
        '{"entries": 5}',
        '{"entries": "text"}',
        '{"entries": [{"kind": 5, "ref": "x"}]}',
        '{"entries": [{"kind": "table", "ref": 7}]}',
        '{"entries": [{"kind": "table", "ref": "x", "why": 9}]}',
        '{"entries": ["not an object"]}',
    ],
)
def test_a_malformed_basis_does_not_take_the_page_down(junk):
    """One card's bad self-report must not cost the operator the log they opened to read — and the
    blast radius is the page, not the card: `_session_drawer` joins every card into one string.

    The non-string inner values matter because `ui.esc` takes `str | None` and raises on anything
    else truthy. `_bounded_basis` never writes one, but this column is plain TEXT on a self-hostable
    schema and an embedder builds `ToolCallRecord` itself, so the renderer cannot assume its writer.
    """
    html = _card(junk)
    assert "SELECT 1" in html  # the card still renders, which is the whole claim


def test_a_wholly_rejected_basis_still_shows_that_a_claim_was_made():
    """`_bounded_basis` stores `{"entries": [], "truncated": true}` when every entry was rejected, so
    the audit log records the attempt. The view has to show it: on the one surface this feature
    exists for, "the agent said nothing" and "everything the agent said was rejected" must not look
    the same."""
    html = _card(json.dumps({"entries": [], "truncated": True}))
    assert "basis" in html and "truncated" in html


def test_an_empty_unflagged_basis_renders_nothing():
    """Not something the writer emits — it returns None for an empty claim — so there is nothing to
    say and no heading to draw."""
    assert "basis" not in _card(json.dumps({"entries": [], "truncated": False}))


def test_the_rendered_basis_is_escaped():
    """Self-reported and attacker-influenceable, exactly like the SQL and the question beside it."""
    html = _card(
        json.dumps(
            {
                "entries": [
                    {
                        "kind": "filter",
                        "ref": "<script>alert(1)</script>",
                        "why": 'a "quoted" & <b>bold</b> reason',
                    }
                ],
                "truncated": False,
            }
        )
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html and "&amp;" in html
