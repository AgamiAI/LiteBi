"""A curated example keeps the same id across deploys, and the caller can see it (ACE-109).

The `id` column existed and was worthless: absent an authored id the seed minted a uuid4, nothing
wrote it back, so the next deploy minted a different one for the same example. Here the id is
*derived* from the example's own content, which is stable by construction and needs no coordination.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("pydantic")

import model_store  # noqa: E402
from store import Store  # noqa: E402

QUESTION = "how many active assets"
SQL = "SELECT count(*) FROM assets WHERE status = 'active'"


def _store(tmp_path, name: str = "agami.db") -> Store:
    s = Store.connect("sqlite://" + str(tmp_path / name))
    s.run_migrations()
    return s


def _ids(store: Store, datasource: str = "main") -> list[str]:
    rows = store.query(
        "SELECT id FROM prompt_example WHERE datasource = ? ORDER BY id", (datasource,)
    )
    return [r["id"] for r in rows]


# --- the derivation ---------------------------------------------------------------------------


def test_the_id_is_pinned_to_its_construction():
    """A golden constant, not a round-trip: the whole point of the id is that it is the same one
    next release, so the test that matters is the one that fails when someone 'improves' the hash."""
    assert model_store.example_id({"question": QUESTION, "sql": SQL}) == "88e825e75112"


def test_each_part_is_nul_terminated_so_the_split_cannot_be_ambiguous():
    """Joining instead of terminating would collapse these two onto one id."""
    assert model_store.example_id({"question": "ab", "sql": "c"}) != model_store.example_id(
        {"question": "a", "sql": "bc"}
    )


@pytest.mark.parametrize("key", ["question", "sql"])
def test_editing_the_question_or_the_sql_moves_the_id(key):
    base = {"question": QUESTION, "sql": SQL}
    edited = {**base, key: base[key] + " -- reworked"}
    assert model_store.example_id(edited) != model_store.example_id(base)


@pytest.mark.parametrize("key", ["notes", "tables", "columns", "status", "metric", "source"])
def test_metadata_edits_leave_the_id_alone(key):
    """Curation churn — retagging, a note, a status flip — is not a different example."""
    base = {"question": QUESTION, "sql": SQL}
    assert model_store.example_id({**base, key: "anything at all"}) == model_store.example_id(base)


def test_the_id_is_byte_exact_so_whitespace_is_a_difference():
    """No strip, no case-fold: a normalizer is a second thing that can disagree."""
    assert model_store.example_id(
        {"question": QUESTION + " ", "sql": SQL}
    ) != model_store.example_id({"question": QUESTION, "sql": SQL})


def test_an_example_missing_its_sql_still_derives_an_id():
    """Malformed items already reach the seed — `test_malformed_examples_file_is_skipped_not_fatal`
    is the file-level guard, not an item-level one."""
    assert len(model_store.example_id({"question": QUESTION})) == 12


def test_the_id_is_twelve_hex_characters():
    ex_id = model_store.example_id({"question": QUESTION, "sql": SQL})
    assert len(ex_id) == 12 and all(c in "0123456789abcdef" for c in ex_id)


# --- the seed ---------------------------------------------------------------------------------


def test_two_databases_get_the_same_ids(tmp_path):
    """The property the minted uuid4 could never have: seed the same library twice, get the same
    identities — no shared state, no write-back, no coordination."""
    examples = [
        {"area": "sales", "question": f"question {i}", "sql": f"SELECT {i}"} for i in range(5)
    ]
    first, second = _store(tmp_path, "a.db"), _store(tmp_path, "b.db")
    model_store.write_examples(first, "main", examples)
    model_store.write_examples(second, "main", examples)
    assert _ids(first) == _ids(second)
    first.close()
    second.close()


def test_reseeding_the_same_library_keeps_every_id(tmp_path):
    examples = [{"area": "sales", "question": QUESTION, "sql": SQL}]
    s = _store(tmp_path)
    model_store.write_examples(s, "main", examples)
    before = _ids(s)
    model_store.write_examples(s, "main", examples)
    assert _ids(s) == before
    s.close()


def test_an_authored_id_wins_verbatim(tmp_path):
    """The existing escape hatch is untouched — derivation is only the fallback."""
    s = _store(tmp_path)
    model_store.write_examples(
        s, "main", [{"area": "sales", "id": "hand-written", "question": QUESTION, "sql": SQL}]
    )
    assert _ids(s) == ["hand-written"]
    s.close()


def test_an_authored_id_of_zero_still_wins(tmp_path):
    """`0` is a legal id and a falsy one. YAML parses an unquoted `id: 0` as an int, so a truthiness
    check reads it as absent and derives over it — and a numbered library starts at exactly this
    value. The failure is silent: the example keeps working, under an id its author did not choose."""
    s = _store(tmp_path)
    model_store.write_examples(
        s, "main", [{"area": "sales", "id": 0, "question": QUESTION, "sql": SQL}]
    )
    assert _ids(s) == ["0"]
    s.close()


@pytest.mark.parametrize("empty", [None, ""])
def test_an_id_that_names_nothing_is_treated_as_absent(tmp_path, empty):
    """The other half, and the reason the check is not simply `is not None`: a key present but empty
    names no example, so derivation is the right answer rather than an id of ""."""
    s = _store(tmp_path)
    model_store.write_examples(
        s, "main", [{"area": "sales", "id": empty, "question": QUESTION, "sql": SQL}]
    )
    assert _ids(s) == [model_store.example_id({"question": QUESTION, "sql": SQL})]
    s.close()


def test_one_example_in_two_areas_is_one_row_not_a_crash(tmp_path):
    """`area` is not in the primary key and is deliberately not in the hash, so the same example
    filed under two subject areas now collapses to one id. A bare INSERT would raise here and take
    the whole deploy down; first-wins dedup is what makes that a no-op instead."""
    s = _store(tmp_path)
    model_store.write_examples(
        s,
        "main",
        [
            {"area": "sales", "question": QUESTION, "sql": SQL},
            {"area": "assets", "question": QUESTION, "sql": SQL},
        ],
    )
    rows = s.query("SELECT area FROM prompt_example WHERE datasource = ?", ("main",))
    assert len(rows) == 1
    assert rows[0]["area"] == "sales"  # first wins, so the surviving row is stable across deploys
    s.close()


def test_examples_that_differ_only_by_area_are_still_two_rows(tmp_path):
    """The dedup keys on the id, not on carelessness — different content stays distinct."""
    s = _store(tmp_path)
    model_store.write_examples(
        s,
        "main",
        [
            {"area": "sales", "question": QUESTION, "sql": SQL},
            {"area": "assets", "question": "how many regions", "sql": SQL},
        ],
    )
    assert len(_ids(s)) == 2
    s.close()


# --- the read ---------------------------------------------------------------------------------


def test_select_examples_returns_the_id_on_every_example(tmp_path):
    s = _store(tmp_path)
    model_store.write_examples(s, "main", [{"area": "sales", "question": QUESTION, "sql": SQL}])
    out = model_store.select_examples(s, "main")
    assert out[0]["id"] == model_store.example_id({"question": QUESTION, "sql": SQL})
    assert out[0]["question"] == QUESTION  # the rest of the doc still rides along
    s.close()


def test_example_by_id_round_trips_a_served_id(tmp_path):
    """The derivation is one-way, so this is the only way a caller holding an id gets back to the
    example it names."""
    s = _store(tmp_path)
    model_store.write_examples(
        s, "main", [{"area": "sales", "question": QUESTION, "sql": SQL, "tables": ["assets"]}]
    )
    served = model_store.select_examples(s, "main")[0]
    found = model_store.example_by_id(s, datasource="main", example_id=served["id"])
    assert found == served
    assert found["tables"] == ["assets"]  # the tagged tables a consumer checks the SQL against
    s.close()


def test_example_by_id_is_none_for_an_id_that_was_never_seeded(tmp_path):
    s = _store(tmp_path)
    model_store.write_examples(s, "main", [{"question": QUESTION, "sql": SQL}])
    assert model_store.example_by_id(s, datasource="main", example_id="0" * 12) is None
    s.close()


def test_example_by_id_does_not_cross_datasource_or_org(tmp_path):
    """Scoped like every other read in the module — an id is only an identity within its org and
    datasource, and the same curated example imported elsewhere derives the same 12 characters."""
    s = _store(tmp_path)
    example = {"question": QUESTION, "sql": SQL}
    model_store.write_examples(s, "other", [example])
    model_store.write_examples(s, "main", [example], "another-org")
    ex_id = model_store.example_id(example)

    assert model_store.example_by_id(s, datasource="other", example_id=ex_id) is not None
    assert model_store.example_by_id(s, datasource="main", example_id=ex_id) is None
    assert (
        model_store.example_by_id(s, org_id="another-org", datasource="main", example_id=ex_id)
        is not None
    )
    s.close()


def test_the_column_wins_over_an_id_inside_the_doc(tmp_path):
    """`write_examples` writes the two in agreement, so only a row inserted behind its back can tell
    them apart — and the column has to win, because it is the value `example_by_id` matches on. Both
    reads must agree, or an id taken off one would not resolve through the other."""
    s = _store(tmp_path)
    s.execute(
        "INSERT INTO prompt_example (org_id, datasource, area, id, question, doc) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            model_store.DEFAULT_ORG,
            "main",
            "sales",
            "the-column",
            QUESTION,
            json.dumps({"id": "stale-in-the-doc", "question": QUESTION, "sql": SQL}),
        ),
    )
    s.commit()

    assert model_store.select_examples(s, "main")[0]["id"] == "the-column"
    found = model_store.example_by_id(s, datasource="main", example_id="the-column")
    assert found is not None and found["id"] == "the-column"
    s.close()
