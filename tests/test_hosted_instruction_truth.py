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


def test_the_instructions_name_the_metric_sql_key_the_payload_actually_sends(monkeypatch):
    """An instruction that says "reuse this field VERBATIM" must name a field that arrives.

    This is the same defect as the one at the top of this file, one layer down: text shipped to a
    client asserting something the server does not do. Both instruction surfaces said to reuse a
    metric's `calculation`/**`bindings`** — the MODEL's field name, and the natural thing to write
    — while `_metric_full` emits **`binding`**, singular, already resolved to this deployment's
    engine. No payload has ever carried a `bindings` key. An agent told in capitals to reuse a
    field it never receives hand-rolls the SQL instead, and hand-rolled SQL does not reduce to the
    declared binding, so the receipt then reads `unmatched` on a column that does compute the
    metric. A wrong word in the instructions costs a true match on the receipt.

    Derived from `_metric_full` rather than spelled out: rename the key and this fails, instead of
    the text and the payload drifting apart again in silence.
    """
    class _Metric:  # duck-typed on purpose — this pins the payload, not the pydantic model
        name = "revenue"
        description = "Closed-won value."
        calculation = "the value of deals that reached the won stage"
        other_names: list[str] = []
        review_state = "approved"
        bindings = {"PostgreSQL": "SUM(amount) FILTER (WHERE stage = 'won')"}

    emitted = set(tools._metric_full(_Metric(), "sales", "PostgreSQL"))
    assert emitted & {"binding", "bindings"}, "no metric-SQL key at all — the field is gone"

    surfaces = {
        "hosted server_instructions": _instructions(monkeypatch, hosted=True),
        "local server_instructions": _instructions(monkeypatch, hosted=False),
        "get_datasource_schema description": tools.TOOLS["get_datasource_schema"]["description"],
    }
    for where, text in surfaces.items():
        for candidate in ("binding", "bindings"):
            named = f"`{candidate}`" in text
            sent = candidate in emitted
            assert named == sent, (
                f"{where} {'names' if named else 'does not name'} `{candidate}`, but the payload "
                f"{'sends' if sent else 'does not send'} it"
            )


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


def test_an_unreachable_store_is_retried_rather_than_pinned(monkeypatch, tmp_path):
    """A failure must NOT be memoized.

    `lru_cache` would pin the `None` returned when the store is unreachable, so a container that
    starts before its database is ready — or takes one blip on its first tool call — would fall
    back to the literal 'default' for the life of the process, silently reinstating every symptom
    the store step exists to fix: `active_datasource` naming a profile that does not exist,
    `is_active` never true, and an omitted `datasource` refusing.
    """
    import model_store

    monkeypatch.delenv("AGAMI_PROFILE", raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    url = "sqlite://" + str(tmp_path / "m.db")
    monkeypatch.setenv("AGAMI_DB_URL", url)
    tools._sole_served_datasource.cache_clear()

    # First call while the store is down.
    boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("connection refused"))
    monkeypatch.setattr(model_store, "list_datasources", boom)
    assert tools.resolve_profile() == "default"

    # The store recovers; the very next call must see it.
    monkeypatch.setattr(model_store, "list_datasources", lambda *a, **k: ["recovered"])
    assert tools.resolve_profile() == "recovered", \
        "a transient failure was cached and never retried"
    tools._sole_served_datasource.cache_clear()


def test_a_successful_resolution_is_memoized(monkeypatch, tmp_path):
    """The positive IS cached — it cannot change under a running server (models are deployed
    before the process starts), and the alternative is a Store connection per tool call."""
    import model_store

    monkeypatch.delenv("AGAMI_PROFILE", raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setenv("AGAMI_DB_URL", "sqlite://" + str(tmp_path / "m.db"))
    tools._sole_served_datasource.cache_clear()

    calls = []
    monkeypatch.setattr(model_store, "list_datasources",
                        lambda *a, **k: (calls.append(1), ["only"])[1])
    assert tools.resolve_profile() == "only"
    assert tools.resolve_profile() == "only"
    assert len(calls) == 1, f"queried the store {len(calls)} times for a stable answer"
    tools._sole_served_datasource.cache_clear()
