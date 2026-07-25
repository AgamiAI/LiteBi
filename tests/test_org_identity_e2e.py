"""F14 / ACE-056–057 end-to-end: the deployment mints a portable org identity, keeps it immutable,
and the deploy-stamp + serve-resolve agree on it — the invariant the whole feature rests on.

No network is touched (the id is a local uuid4 + a file write); `tests/test_privacy_no_network.py`
is the separate static gate that proves no egress primitive was introduced.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("yaml")

PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import tools  # noqa: E402
from semantic_model import build, loader  # noqa: E402
from semantic_model.models import Organization  # noqa: E402

_HEX = set("0123456789abcdef")


def _minimal_org(name: str = "acme") -> Organization:
    return Organization(datasource=name)


def test_write_tree_mints_uuid4_and_is_immutable(tmp_path):
    prof = tmp_path / "acme"
    build.write_tree(_minimal_org(), prof)

    minted = loader.load_org_id(prof)
    assert minted and len(minted) == 32 and set(minted) <= _HEX  # a uuid4 hex, minted locally

    # load -> mutate -> rewrite: the id is preserved, never re-minted (immutability guarantee).
    org = loader.load_organization(prof)
    assert org.org_id == minted
    org.description = "edited"
    build.write_tree(org, prof)
    assert loader.load_org_id(prof) == minted


def test_deployment_scoped_second_profile_adopts_the_id(tmp_path):
    # A company with several datasources is ONE tenant: a new profile adopts the deployment's id
    # rather than minting a second one.
    build.write_tree(_minimal_org("acme"), tmp_path / "sales")
    deployment_id = loader.load_org_id(tmp_path / "sales")

    build.write_tree(_minimal_org("acme"), tmp_path / "support")
    assert loader.load_org_id(tmp_path / "support") == deployment_id
    assert loader.deployment_org_id(tmp_path) == deployment_id


def test_deploy_and_serve_resolve_the_same_id(tmp_path, monkeypatch):
    # The load-bearing invariant: the deploy-time stamp (_default_org) and the serve-time resolver
    # (resolved_org_id) call the SAME function over the SAME artifacts dir, so they can't disagree —
    # even with AGAMI_PROFILE unset and the model under a named profile (not 'default').
    import model_deploy

    build.write_tree(_minimal_org("acme"), tmp_path / "northpeak_salesforce")
    minted = loader.load_org_id(tmp_path / "northpeak_salesforce")

    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.delenv("AGAMI_ORG_ID", raising=False)
    monkeypatch.delenv("AGAMI_PROFILE", raising=False)
    tools.resolved_org_id.cache_clear()

    assert model_deploy._default_org() == minted          # deploy stamps this
    assert tools.resolved_org_id() == minted              # serve resolves this
    assert tools._current_org_id() == minted              # ...and the per-request path agrees


def test_ensure_org_id_cli_mints_into_copied_model(tmp_path, capsys):
    # The sample-copy path (agami-connect 6A) drops a prebuilt datasource.yaml with no id; `sm ensure-org-id`
    # mints one into it. Idempotent: a second run prints the SAME id and doesn't rewrite.
    from semantic_model import cli

    prof = tmp_path / "agami-example"
    prof.mkdir()
    (prof / "datasource.yaml").write_text("datasource: Acme Store\nversion: 1\n")

    assert cli.main(["ensure-org-id", str(prof)]) == 0
    minted = capsys.readouterr().out.strip()
    assert minted and len(minted) == 32 and set(minted) <= _HEX
    assert loader.load_org_id(prof) == minted  # persisted into datasource.yaml

    assert cli.main(["ensure-org-id", str(prof)]) == 0
    assert capsys.readouterr().out.strip() == minted  # idempotent — same id, no re-mint


def test_legacy_profile_without_org_id_resolves_local(tmp_path, monkeypatch):
    # A pre-F14 datasource.yaml (no org_id key) still loads and resolves to the 'local' sentinel — no crash,
    # no forced mint at serve time (serve is read-only; minting happens only at connect/build).
    (tmp_path / "old").mkdir()
    (tmp_path / "old" / "datasource.yaml").write_text("datasource: legacy\nversion: 1\n")
    assert loader.load_org_id(tmp_path / "old") is None
    assert loader.load_organization(tmp_path / "old").org_id is None

    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.delenv("AGAMI_ORG_ID", raising=False)
    tools.resolved_org_id.cache_clear()
    assert tools.resolved_org_id() == "local"


# --- F14 regression guard: minting an org must NOT break credential resolution -------------------
# execute_sql is fail-closed — a NAMED tenant never falls back to the shared org-less
# DATASOURCE_URL[__<PROFILE>] vars. That rule keyed on the literal "local", so minting a uuid for a
# single-tenant deployment would have silently cut every DATASOURCE_URL-based deploy off from its
# warehouse. `_credential_org_id` maps the deployment's OWN id back to the sentinel; a genuinely
# named tenant still passes through fail-closed.

def test_minted_org_still_resolves_the_orgless_datasource_url(tmp_path, monkeypatch):
    build.write_tree(_minimal_org(), tmp_path / "np")
    minted = loader.load_org_id(tmp_path / "np")
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.delenv("AGAMI_ORG_ID", raising=False)
    tools.resolved_org_id.cache_clear()
    tools._current_org_ctx.set(None)

    assert tools._current_org_id() == minted          # rows are stamped with the minted uuid …
    assert tools._credential_org_id() == "local"      # … but credentials use the single-tenant channel

    import execute_sql
    monkeypatch.setenv("DATASOURCE_URL__NP", "postgresql://u:p@h:5432/np")
    assert execute_sql._env_datasource_dsn("np", tools._credential_org_id()) is not None


def test_named_tenant_stays_fail_closed(tmp_path, monkeypatch):
    # An explicitly-named AGAMI_ORG_ID is a real tenant: it must NOT borrow the org-less DSN.
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setenv("AGAMI_ORG_ID", "acme")
    tools.resolved_org_id.cache_clear()
    tools._current_org_ctx.set(None)
    assert tools._credential_org_id() == "acme"

    import execute_sql
    monkeypatch.setenv("DATASOURCE_URL__NP", "postgresql://u:p@h:5432/np")
    assert execute_sql._env_datasource_dsn("np", tools._credential_org_id()) is None  # fail-closed


def test_per_request_tenant_stays_fail_closed(tmp_path, monkeypatch):
    # A tenant chosen per-request by a multi-tenant resolver (the contextvar) is also fail-closed,
    # even though the deployment itself has a minted id.
    build.write_tree(_minimal_org(), tmp_path / "np")
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.delenv("AGAMI_ORG_ID", raising=False)
    tools.resolved_org_id.cache_clear()
    tools._current_org_ctx.set("tenant-b")
    try:
        assert tools._credential_org_id() == "tenant-b"
    finally:
        tools._current_org_ctx.set(None)
