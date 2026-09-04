"""`thread_id` promoted to a required input, and the flag that gates it.

`thread_id` groups a conversation's calls and is entirely self-reported: `SERVER_INSTRUCTIONS` asks
for it on every call and ends "Best-effort; omit if unknown". Models take the omission — measured on
one deployment across two consecutive conversations, 2 of 10 calls carried one and then 0 of 8. With
the property marked required the same client sent it on 9 of 9.

These tests hold the SHAPE of the change and the default. Whether a model populates a required field
is not something a unit test can decide, and the flag exists precisely because that has to be
observed on a real client rather than asserted here.
"""

from __future__ import annotations

from copy import deepcopy

import pytest
from tools import TOOLS, require_thread_id, thread_id_is_required


def _tool(properties: dict, required: list | None = None) -> dict:
    schema: dict = {"type": "object", "properties": properties}
    if required is not None:
        schema["required"] = required
    return {"handler": lambda args: "ok", "inputSchema": schema}


def test_a_tool_declaring_thread_id_gains_it_as_a_requirement():
    out = require_thread_id({"a": _tool({"thread_id": {"type": "string"}})})
    assert out["a"]["inputSchema"]["required"] == ["thread_id"]


def test_an_existing_requirement_is_kept_rather_than_replaced():
    """`execute_sql` requires `sql`. Losing that would let an empty call reach the handler — a worse
    bug than the one being fixed."""
    out = require_thread_id(
        {
            "execute_sql": _tool(
                {"sql": {"type": "string"}, "thread_id": {"type": "string"}}, ["sql"]
            )
        }
    )
    assert out["execute_sql"]["inputSchema"]["required"] == ["sql", "thread_id"]


def test_a_tool_without_thread_id_is_untouched():
    """The property is never invented. Marking a non-existent property required would fail every
    call to that tool forever."""
    original = _tool({"sql": {"type": "string"}}, ["sql"])
    out = require_thread_id({"t": original})
    assert out["t"] is original


def test_it_is_idempotent():
    once = require_thread_id({"a": _tool({"thread_id": {"type": "string"}})})
    twice = require_thread_id(once)
    assert twice["a"]["inputSchema"]["required"] == ["thread_id"]


def test_the_shared_registry_is_never_mutated():
    """Copy-on-write, and it is load-bearing: a process that builds more than one server (the tests
    do) must not leak the requirement into a registry that never asked for it.

    **`deepcopy`, not `dict(...)`** — raised in review, and the distinction is the whole test. A
    shallow copy shares the nested `required` LIST with the original, so an implementation that
    appended in place (`schema["required"].append("thread_id")` — the obvious way to write this
    wrong) would mutate both sides and the comparison would be a list against itself. The one bug
    this test exists to catch is precisely the one it would have missed.
    """
    before = {name: deepcopy(meta.get("inputSchema") or {}) for name, meta in TOOLS.items()}
    require_thread_id(TOOLS)
    for name, meta in TOOLS.items():
        assert (meta.get("inputSchema") or {}).get("required") == before[name].get("required")


def test_it_covers_every_shipped_tool_that_declares_the_property():
    """Not a hand-written list of names — the rule is "declares it, requires it", so a tool added
    later is covered without anyone remembering to update this."""
    out = require_thread_id(TOOLS)
    for name, meta in TOOLS.items():
        schema = meta.get("inputSchema") or {}
        if "thread_id" in (schema.get("properties") or {}):
            assert "thread_id" in out[name]["inputSchema"]["required"], name


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_the_flag_turns_it_on(monkeypatch, value):
    monkeypatch.setenv("AGAMI_REQUIRE_THREAD_ID", value)
    assert thread_id_is_required() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "  "])
def test_anything_else_leaves_it_off(monkeypatch, value):
    monkeypatch.setenv("AGAMI_REQUIRE_THREAD_ID", value)
    assert thread_id_is_required() is False


def test_the_default_is_off(monkeypatch):
    """**The most important assertion here.** The MCP SDK validates arguments against `inputSchema`
    before dispatch, so a call omitting a required property never reaches its handler — it returns
    "Input validation error". Against a measured omission rate of up to 100%, switching this on for
    existing deployments would take their tools out of service rather than improve their logs."""
    monkeypatch.delenv("AGAMI_REQUIRE_THREAD_ID", raising=False)
    assert thread_id_is_required() is False


def test_the_served_surface_is_unchanged_while_the_flag_is_off(monkeypatch):
    """The whole promise made to existing deployments and to every other consumer of this registry:
    nothing about the tool surface moves until they opt in."""
    monkeypatch.delenv("AGAMI_REQUIRE_THREAD_ID", raising=False)
    from mcp_http import build_server

    build_server()
    for meta in TOOLS.values():
        schema = meta.get("inputSchema") or {}
        assert "thread_id" not in (schema.get("required") or ())
