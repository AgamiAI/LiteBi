"""get_prompt_examples DB serving — scope + rank + cap, never the whole library (Slice D).

The fix that matters: a large library (e.g. accumulated corrections) returns a bounded, relevant
set. Default ranking is word-overlap (zero deps, zero egress); the embeddings tier stays off.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("pydantic")

import model_store  # noqa: E402
import tools  # noqa: E402
from store import Store  # noqa: E402


def _seed(tmp_path, examples) -> str:
    url = "sqlite://" + str(tmp_path / "agami.db")
    s = Store.connect(url)
    s.run_migrations()
    model_store.write_examples(s, "main", examples)
    s.close()
    return url


def test_large_library_is_ranked_and_capped(tmp_path, monkeypatch):
    examples = [
        {"area": "sales", "question": f"monthly revenue trend {i}", "sql": "SELECT 1"}
        for i in range(40)
    ]
    examples += [
        {"area": "sales", "question": f"unrelated widget count {i}", "sql": "SELECT 2"}
        for i in range(40)
    ]
    url = _seed(tmp_path, examples)
    monkeypatch.setenv("AGAMI_DB_URL", url)

    out = json.loads(tools.tool_get_prompt_examples({"datasource": "main", "query": "revenue"}))
    assert out["count"] <= 10  # top-K cap — never the whole 80-example library
    # the revenue-matching examples rank ahead of the unrelated ones
    assert out["examples"], "expected at least one match"
    assert all("revenue" in e["question"] for e in out["examples"][:5])


def test_char_budget_bounds_the_result(tmp_path, monkeypatch):
    # one giant example + many small: the budget stops accumulation (but always returns >=1).
    big = {"area": "s", "question": "x " * 50, "sql": "Q" * 30_000}
    examples = [big] + [{"area": "s", "question": f"q{i}", "sql": "SELECT 1"} for i in range(20)]
    url = _seed(tmp_path, examples)
    monkeypatch.setenv("AGAMI_DB_URL", url)
    out = json.loads(tools.tool_get_prompt_examples({"datasource": "main"}))
    serialized = sum(len(json.dumps(e)) for e in out["examples"])
    assert serialized <= 20_000 + 30_000  # bounded; the 30K example doesn't drag the whole library


def test_empty_library_returns_empty(tmp_path, monkeypatch):
    url = _seed(tmp_path, [])
    monkeypatch.setenv("AGAMI_DB_URL", url)
    out = json.loads(tools.tool_get_prompt_examples({"datasource": "main", "query": "anything"}))
    assert out["examples"] == [] and out["count"] == 0


def test_area_narrows_to_that_area_plus_the_cross_area_bucket(tmp_path, monkeypatch):
    """`select_examples` has always implemented this correctly — `area = ? OR area IS NULL`, which
    keeps genuinely cross-area examples while dropping other areas'. It was simply unreachable:
    `area` was missing from the tool's inputSchema, which sets `additionalProperties: false`, so a
    compliant client could not send it and the branch was dead on every MCP call.

    The pairing is the point: an area-scoped call must NOT silently lose the cross-area examples,
    which is why this asserts both halves.
    """
    examples = [
        {"area": "sales", "question": "how many orders by region", "sql": "SELECT 1"},
        {"area": "assets", "question": "how many assets by install status", "sql": "SELECT 2"},
        {"area": None, "question": "how many rows overall", "sql": "SELECT 3"},
    ]
    url = _seed(tmp_path, examples)
    monkeypatch.setenv("AGAMI_DB_URL", url)

    out = json.loads(tools.tool_get_prompt_examples({"datasource": "main", "area": "sales"}))
    got = {e["question"] for e in out["examples"]}
    assert "how many orders by region" in got          # the named area
    assert "how many rows overall" in got              # the cross-area bucket, not lost
    assert "how many assets by install status" not in got   # another area, dropped


def test_the_area_parameter_is_advertised_so_a_client_can_send_it(tmp_path):
    """The handler reading an argument is not enough — `additionalProperties: false` means an
    undeclared key is rejected at the transport, so the schema is what makes it reachable."""
    schema = tools.TOOLS["get_prompt_examples"]["inputSchema"]
    assert schema["additionalProperties"] is False
    assert "area" in schema["properties"]


# --- the local (file) path, which had no test at all -----------------------------------------


@pytest.fixture()
def local_library(tmp_path, monkeypatch):
    """A local install with a two-area curated library on disk."""
    for var in ("AGAMI_DB_URL", "APP_DATABASE_URL", "AGAMI_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    ex = tmp_path / "main" / "prompt_examples"
    for area, q in (("sales", "how many orders"), ("assets", "how many assets")):
        (ex / area).mkdir(parents=True)
        (ex / area / "examples.yaml").write_text(f"- question: {q}\n  sql: SELECT 1\n")
    tools.bootstrap_paths()
    return "main"


def test_the_local_path_returns_every_area_when_none_is_named(local_library):
    out = tools.tool_get_prompt_examples({"datasource": local_library})
    assert "subject area: sales" in out and "subject area: assets" in out


def test_the_local_path_honours_area_too(local_library):
    """One schema, one behaviour. Advertising `area` while only the served path honoured it would
    have been the same defect this batch is about, one layer down: a parameter a client can send
    that silently does nothing on half the deployments."""
    out = tools.tool_get_prompt_examples({"datasource": local_library, "area": "sales"})
    assert "subject area: sales" in out
    assert "subject area: assets" not in out
    assert "how many assets" not in out


def test_an_area_with_no_examples_on_disk_gives_the_empty_note(local_library):
    out = json.loads(tools.tool_get_prompt_examples(
        {"datasource": local_library, "area": "nonexistent"}))
    assert out["examples"] == []
    assert "prompt_examples" in out["note"]


@pytest.mark.parametrize("bad", [True, 1, 0, [], {}, 3.5])
def test_a_non_string_area_does_not_crash_the_local_path(local_library, bad):
    """`(x or "").strip()` raises on any TRUTHY non-string, and this handler is reachable outside
    a schema-validating transport — tests and embedders call it directly.

    It is a regression risk specific to this change: before `area` was honoured on the local path
    the argument was ignored entirely, so no input could crash it. Treated as "no scope" rather
    than refused — this path returns the curated library and has no vocabulary for an input error.
    """
    out = tools.tool_get_prompt_examples({"datasource": local_library, "area": bad})
    assert "subject area: sales" in out and "subject area: assets" in out


def test_a_whitespace_only_area_is_no_scope_not_an_empty_scope(local_library):
    """`"  "` is not the name of an area. Stripping to empty must read as "no scope given",
    not as "an area named nothing", which would match no directory and return the empty note."""
    out = tools.tool_get_prompt_examples({"datasource": local_library, "area": "   "})
    assert "subject area: sales" in out and "subject area: assets" in out
