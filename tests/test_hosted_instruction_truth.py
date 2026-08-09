"""The client-facing instruction text must not make a local privacy claim on a hosted server.

`SERVER_INSTRUCTIONS` was a module-level constant opening with "…execute SQL locally. All execution
is local." That sentence is true and worth saying on the stdio/skill install. On a hosted
deployment it is false — the SQL runs in the container, against the configured warehouse, and the
result rows come back over the wire and land in the activity log — and it shipped verbatim there,
because a constant evaluated at import cannot see the environment that decides which is true.

It is a privacy claim, which is why this is a test and not a docs nit.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")

import tools  # noqa: E402


def _instructions(monkeypatch, *, hosted: bool) -> str:
    for var in ("AGAMI_DB_URL", "APP_DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    if hosted:
        monkeypatch.setenv("AGAMI_DB_URL", "sqlite:///tmp/does-not-need-to-exist.db")
    return tools.server_instructions()


def test_a_hosted_server_does_not_claim_execution_is_local(monkeypatch):
    text = _instructions(monkeypatch, hosted=True)
    assert "All execution is local" not in text
    assert "execute SQL locally" not in text
    assert "HOSTED" in text
    assert "leave your machine" in text, "say plainly where the data goes"


def test_a_local_install_keeps_the_claim_that_is_true_there(monkeypatch):
    text = _instructions(monkeypatch, hosted=False)
    assert "All execution is local" in text


def test_only_the_preamble_differs_so_the_flow_cannot_drift(monkeypatch):
    """One sentence branches; everything after it is shared. Two full copies of the flow would
    drift, and the half that drifts is the half nobody re-reads."""
    hosted = _instructions(monkeypatch, hosted=True)
    local = _instructions(monkeypatch, hosted=False)
    assert hosted != local
    assert hosted.endswith(tools._SHARED_INSTRUCTIONS)
    assert local.endswith(tools._SHARED_INSTRUCTIONS)


def test_the_safety_directives_survive_on_both(monkeypatch):
    """The PII rule and the receipt-reading rule are the reason `extra_instructions` appends rather
    than replaces. A per-deployment preamble must not become a way to lose them."""
    for hosted in (True, False):
        text = _instructions(monkeypatch, hosted=hosted)
        assert "PII:" in text
        assert "sensitive: true" in text
        assert "receipt" in text


def test_a_local_install_with_no_store_still_resolves_a_profile(monkeypatch, tmp_path):
    """`resolve_profile`'s store step must be inert on a local install. `Store.from_env()` returns
    None with no DB configured, and the fallback that was always there has to stand."""
    for var in ("AGAMI_DB_URL", "APP_DATABASE_URL", "AGAMI_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    tools._sole_served_datasource.cache_clear()
    assert tools._sole_served_datasource("local") is None
    assert tools.resolve_profile() == "default"
    tools._sole_served_datasource.cache_clear()


def test_an_unreachable_store_does_not_break_profile_resolution(monkeypatch, tmp_path):
    """Profile resolution has to produce a name even when the database is down — it sits under
    every tool call, and raising here would turn a degraded store into a dead server. The answer
    with nothing resolvable is the same 'default' it always was."""
    for var in ("AGAMI_PROFILE",):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setenv("AGAMI_DB_URL", "postgresql://nobody@127.0.0.1:1/nope")
    tools._sole_served_datasource.cache_clear()
    assert tools._sole_served_datasource("local") is None
    assert tools.resolve_profile() == "default"
    tools._sole_served_datasource.cache_clear()
