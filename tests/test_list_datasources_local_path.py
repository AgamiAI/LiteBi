"""The LOCAL (credentials-file) path of `list_datasources`, which had no test.

It matters now because the two paths deliberately DIVERGE: `model_present` was dropped from the
served listing — there the list is built FROM the model rows, so the field could only ever be the
literal `True`, a constant dressed as a check — and kept here, where it really does answer "has
this profile been introspected yet?". An asymmetry asserted on one side only is an asymmetry that
can quietly become a symmetry again.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("pydantic")

import tools  # noqa: E402


@pytest.fixture()
def local(tmp_path, monkeypatch):
    """A local install: two credential profiles, only one of them introspected."""
    for var in ("AGAMI_DB_URL", "APP_DATABASE_URL", "AGAMI_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    (tmp_path / "local").mkdir(parents=True)
    (tmp_path / "local" / "credentials").write_text(
        "[modelled]\nurl = postgresql://u:p@h/db\n\n[bare]\nurl = mysql://u:p@h/db\n"
    )
    d = tmp_path / "modelled"
    (d / "subject_areas" / "s" / "tables").mkdir(parents=True)
    (d / "datasource.yaml").write_text("datasource: modelled\nversion: 1\n")
    (d / "subject_areas" / "s" / "tables" / "orders.yaml").write_text("name: orders\n")
    tools.bootstrap_paths()
    tools._sole_served_datasource.cache_clear()
    yield
    tools._sole_served_datasource.cache_clear()


def test_model_present_is_a_real_check_on_the_local_path(local):
    """The reason it survives here: it distinguishes a credentials profile that has been
    introspected from one that has not. That is a genuine question with two answers."""
    out = json.loads(tools.tool_list_datasources({}))
    by_name = {d["datasource"]: d for d in out["datasources"]}
    assert by_name["modelled"]["model_present"] is True
    assert by_name["bare"]["model_present"] is False


def test_the_local_path_reports_its_own_table_count_and_db_type(local):
    by_name = {d["datasource"]: d for d in json.loads(
        tools.tool_list_datasources({}))["datasources"]}
    assert by_name["modelled"]["table_count"] == 1
    assert by_name["modelled"]["database_type"] == "postgres"
    assert by_name["bare"]["table_count"] == 0


def test_no_credentials_gives_the_connect_hint_not_the_deploy_hint(tmp_path, monkeypatch):
    """The two empty states are different advice, and sending a local user to `model_deploy` (the
    served hint) would be sending them to a tool they are not running."""
    for var in ("AGAMI_DB_URL", "APP_DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    tools.bootstrap_paths()
    out = json.loads(tools.tool_list_datasources({}))
    assert out["datasources"] == []
    assert "agami-connect" in out["note"] and "model_deploy" not in out["note"]


def test_the_store_step_sits_below_every_step_that_was_already_there(tmp_path, monkeypatch):
    """`resolve_profile` gained ONE step and it is the last one before the literal fallback.

    Order is the contract: explicit -> AGAMI_PROFILE -> `.config.active_profile` -> sole served
    datasource -> 'default'. A store lookup that preempted `.config` would silently retarget any
    local install that has both, which is every developer machine with a server DB configured.
    """
    import json as _json

    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    (tmp_path / "local").mkdir(parents=True)
    (tmp_path / "local" / ".config").write_text(_json.dumps({"active_profile": "from_config"}))
    tools.bootstrap_paths()
    tools._sole_served_datasource.cache_clear()

    # `.config` wins over the store step...
    monkeypatch.setattr(tools, "_sole_served_datasource", lambda _org: "from_store")
    assert tools.resolve_profile() == "from_config"
    # ...and the two above it still win over `.config`.
    monkeypatch.setenv("AGAMI_PROFILE", "from_env")
    assert tools.resolve_profile() == "from_env"
    assert tools.resolve_profile("explicit") == "explicit"
