"""An omitted `datasource` on a multi-datasource deployment must name the real choices.

`_sole_served_datasource` already removed the invented `'default'` for a deployment serving exactly
one. Serving several, resolution still falls through to that literal — correctly, because guessing
between three is worse than refusing — and the caller then heard `no such datasource: default`.

Two audiences read that sentence and both are misled. An administrator sees a name no customer has
and concludes their data has gone missing. A model sees a failed lookup and invents another name,
because the tool's own description told it a default existed.

So the split under test is between a caller who NAMED a datasource (a typo — tell them the name is
wrong) and one who named none (a decision not yet made — tell them what there is to choose from).
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("pydantic")

import tools  # noqa: E402


@pytest.fixture()
def served(tmp_path, monkeypatch):
    """A deployment serving three datasources, none of them called 'default'.

    `get_cached_org` is left real: pointing artifacts at an empty directory is what makes it raise
    the `FileNotFoundError` this branch hangs off, so the test enters the code the way production
    does rather than through a patched exception.
    """
    for var in ("AGAMI_PROFILE", "AGAMI_DB_URL", "APP_DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    tools.bootstrap_paths()
    tools._sole_served_datasource.cache_clear()
    monkeypatch.setattr(
        tools, "_served_datasources", lambda _org: ["acme_crm", "acme_erp", "acme_tickets"]
    )
    yield
    tools._sole_served_datasource.cache_clear()


def test_omitting_the_datasource_returns_the_organizations_own_datasources(served):
    """The whole point: the reply names what the caller can choose, not a name nobody has."""
    out = json.loads(tools.tool_get_datasource_schema({}))
    assert out["error"]["kind"] == "datasource_required"
    assert out["error"]["datasources"] == ["acme_crm", "acme_erp", "acme_tickets"]
    # The literal that caused the bug must not appear anywhere in what the caller reads.
    assert "default" not in json.dumps(out)


def test_an_empty_datasource_counts_as_omitted_not_as_a_name(served):
    """`resolve_profile` treats `""` as omitted — its check is `if explicit:` — so a caller who sends
    an empty string lands on exactly the fallback chain an absent argument does, and must get the same
    answer here. Testing `is None` instead would send them down the typo branch and hand back the
    `no such datasource: default` sentence this whole change exists to remove.

    Whitespace is deliberately NOT special-cased: `resolve_profile` returns `"   "` unchanged, so it
    is a name that does not exist, and reporting it as a name is the honest answer.
    """
    out = json.loads(tools.tool_get_datasource_schema({"datasource": ""}))
    assert out["error"]["kind"] == "datasource_required"
    assert out["error"]["datasources"] == ["acme_crm", "acme_erp", "acme_tickets"]
    assert "default" not in json.dumps(out)


def test_naming_a_datasource_that_does_not_exist_still_says_so(served):
    """A typo is not an undecided choice. Answering it with the catalog would bury the correction."""
    out = json.loads(tools.tool_get_datasource_schema({"datasource": "acme_crmm"}))
    assert out["error"]["kind"] == "not_found"


def test_an_org_with_nothing_deployed_is_told_that_and_not_given_a_fake_name(served, monkeypatch):
    monkeypatch.setattr(tools, "_served_datasources", lambda _org: [])
    out = json.loads(tools.tool_get_datasource_schema({}))
    assert out["error"]["kind"] == "not_found"
    assert "no datasources are deployed" in out["error"]["remediation"]


def test_an_unreachable_store_keeps_the_old_message_rather_than_guessing(served, monkeypatch):
    """None means "could not ask", and is the one case where saying nothing is right. Claiming an
    org has no datasources because the database blinked is worse than the message we already had."""
    monkeypatch.setattr(tools, "_served_datasources", lambda _org: None)
    out = json.loads(tools.tool_get_datasource_schema({}))
    assert out["error"]["kind"] == "not_found"
    assert "datasources" not in out["error"]


def test_the_sole_served_shortcut_survives_the_refactor(monkeypatch):
    """`_sole_served_datasource` now reads through `_served_datasources`; its rule is unchanged —
    exactly one resolves, several do not."""
    tools._sole_served_datasource.cache_clear()
    monkeypatch.setattr(tools, "_served_datasources", lambda _org: ["only_one"])
    assert tools._sole_served_datasource("acme") == "only_one"
    tools._sole_served_datasource.cache_clear()
    monkeypatch.setattr(tools, "_served_datasources", lambda _org: ["a", "b"])
    assert tools._sole_served_datasource("acme") is None
    tools._sole_served_datasource.cache_clear()


def test_no_tool_promises_a_default_that_a_served_deployment_cannot_honour():
    """The description is half the defect. A model that reads "defaults to the active profile"
    omits the argument on purpose, so fixing only the refusal would leave it still being triggered
    every turn — and on `/mcp` there is no prompt of ours to correct it with."""
    for name in ("get_datasource_schema", "get_prompt_examples", "execute_sql"):
        described = tools.TOOLS[name]["inputSchema"]["properties"]["datasource"]["description"]
        assert "list_datasources" in described
        assert "no default" in described or "there is no default" in described
