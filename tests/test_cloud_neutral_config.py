"""Cloud-neutral config hardening — lock in the no-GCP-lock-in invariants.

agami-core is self-hostable on any cloud (VM + Postgres, or a stateless platform + managed
Postgres). These tests pin the config contract so it can't silently regress:

  1. the DB env var (canonical `AGAMI_DB_URL`, alias `APP_DATABASE_URL`) opens the same store;
  2. the server boots with NO required GCP platform dependency (opt-in datasource drivers excepted);
  3. the single-tenant org resolver is wired into the HTTP server;
  4. a DB-backed instance serves with no local-disk artifacts (stateless-platform-safe).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))


# --- 1. DB env var: canonical + alias resolve to the same store --------------


def test_app_database_url_is_accepted_as_an_alias(tmp_path, monkeypatch):
    from store import Store

    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.setenv("APP_DATABASE_URL", "sqlite://" + str(tmp_path / "a.db"))
    s = Store.from_env()
    assert s is not None and s.dialect == "sqlite"
    s.close()


def test_canonical_agami_db_url_wins_over_alias(tmp_path, monkeypatch):
    # Both set → the canonical name is the unambiguous override.
    from store import Store

    monkeypatch.setenv("AGAMI_DB_URL", "sqlite://" + str(tmp_path / "canonical.db"))
    monkeypatch.setenv("APP_DATABASE_URL", "postgresql://should-not-be-used/db")
    s = Store.from_env()
    assert s is not None and s.dialect == "sqlite"  # picked canonical, not the postgres alias
    s.close()


def test_no_db_env_means_local_file_path(monkeypatch):
    from store import Store

    monkeypatch.delenv("AGAMI_DB_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    assert Store.from_env() is None  # unset ⇒ the local file path is used (unchanged)


# --- 2. No required GCP platform dependency to boot --------------------------

# The library + server must run on any cloud — so they may not *require* a GCP platform service
# (Cloud Logging, Secret Manager, the Cloud SQL connector, ADC). These are matched only at module
# top level (a required import). An opt-in datasource driver imported lazily inside a function body
# is NOT a platform coupling — it connects to the user's own warehouse — exactly how execute_sql.py
# imports `google.cloud.bigquery`; such indented imports are intentionally allowed.
_FORBIDDEN_GCP = [
    re.compile(p)
    for p in (
        r"google\.cloud\.logging",
        r"google\.cloud\.secret_?manager",
        r"google\.cloud\.sql",  # the Cloud SQL Python connector namespace
        r"cloud[_-]sql[_-]python[_-]connector",
        r"\bgoogle\.auth\b",  # Application Default Credentials
    )
]
# bigquery is a user-chosen warehouse driver, not a platform coupling — never forbidden (mirrors
# the allowance in tests/test_privacy_no_network.py).
_LIBRARY_MODULES = sorted(PKG_SRC.glob("*.py")) + sorted((PKG_SRC / "semantic_model").glob("*.py"))


def _is_module_level_import(line: str) -> bool:
    # No leading whitespace ⇒ top level (required at import time); indented ⇒ inside a function
    # body (lazy / opt-in). This is the seam between "required to boot" and "imported on demand".
    return line == line.lstrip() and (line.startswith("import ") or line.startswith("from "))


def test_there_are_library_modules_to_scan():
    # Guard against the glob silently matching nothing (a vacuous pass).
    assert _LIBRARY_MODULES, f"no modules found under {PKG_SRC}"


@pytest.mark.parametrize("module", _LIBRARY_MODULES, ids=lambda p: p.name)
def test_no_required_gcp_platform_import(module: Path):
    hits = []
    for line_no, raw in enumerate(module.read_text().splitlines(), 1):
        line = raw.rstrip()
        if _is_module_level_import(line) and any(rx.search(line) for rx in _FORBIDDEN_GCP):
            hits.append(f"  {module.name}:{line_no}: {line.strip()}")
    assert not hits, (
        f"required (module-level) GCP platform import in {module.name} — the server must boot on "
        f"any cloud with no GCP service required. Import it lazily inside the function that needs "
        f"it (the opt-in-driver pattern), or remove the coupling:\n" + "\n".join(hits)
    )


def test_server_extra_declares_no_gcp_platform_package():
    # The [server] extra must not pull a GCP platform package (logging / secret-manager / cloud-sql
    # connector). A text scan keeps this 3.10-safe (no tomllib) and is precise enough for package names.
    text = (REPO_ROOT / "packages" / "agami-core" / "pyproject.toml").read_text().lower()
    for pkg in ("google-cloud-logging", "google-cloud-secret", "cloud-sql-python-connector"):
        assert pkg not in text, f"{pkg} must not be a declared dependency (GCP lock-in)"


# --- 3. Single-tenant org resolver wired into the HTTP server ----------------


def test_org_resolver_defaults_to_local(monkeypatch):
    pytest.importorskip("starlette")
    pytest.importorskip("mcp")
    import mcp_http

    monkeypatch.delenv("AGAMI_ORG_ID", raising=False)
    assert mcp_http._build_org_resolver().resolve_org().id == "local"


def test_org_resolver_reads_agami_org_id(monkeypatch):
    pytest.importorskip("starlette")
    pytest.importorskip("mcp")
    import mcp_http

    monkeypatch.setenv("AGAMI_ORG_ID", "acme")
    assert mcp_http._build_org_resolver().resolve_org().id == "acme"


def test_auth_middleware_attaches_resolved_org_after_auth(monkeypatch):
    # An authed request gets the resolved org on request.state.org; the seam is live even though
    # nothing downstream consumes it yet.
    pytest.importorskip("starlette")
    pytest.importorskip("mcp")
    import asyncio

    import mcp_http
    from ports import Org
    from starlette.requests import Request
    from starlette.responses import Response

    mw = mcp_http._AuthMiddleware(
        app=None,
        resolver=mcp_http.SingleTenantOrgResolver(Org(id="acme")),
        auth=mcp_http.PresenceAuthProvider(),
    )
    captured: dict[str, object] = {}

    async def call_next(request):
        captured["org"] = request.state.org
        return Response("ok")

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(b"authorization", b"Bearer present")],
    }
    resp = asyncio.run(mw.dispatch(Request(scope), call_next))
    assert resp.status_code == 200
    assert isinstance(captured["org"], Org) and captured["org"].id == "acme"


# --- 4. No local-disk state required to serve (stateless-platform-safe) -------


def test_db_backed_serve_needs_no_artifacts_dir(tmp_path, monkeypatch):
    # A fresh instance on a stateless platform has no local model files — only AGAMI_DB_URL. The
    # serving path must answer from the DB alone, so cloud-neutrality is self-checked here rather
    # than relying on the DB-serving tests elsewhere.
    pytest.importorskip("pydantic")
    import json

    import model_store
    import tools
    from semantic_model.models import Datasource
    from store import Store

    db_url = "sqlite://" + str(tmp_path / "agami.db")
    s = Store.connect(db_url)
    s.run_migrations()
    model_store.write_datasource(
        s,
        "main",
        Datasource.model_validate(
            {
                "datasource": "acme",
                "version": 1,
                "subject_areas": [
                    {"name": "sales", "metrics": [{"name": "revenue", "calculation": "sum"}]}
                ],
            }
        ),
    )
    s.close()

    monkeypatch.setenv("AGAMI_DB_URL", db_url)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "does-not-exist"))  # no disk state
    head = json.JSONDecoder().raw_decode(tools.tool_get_datasource_schema({"datasource": "main"}))[
        0
    ]
    assert head["subject_areas"] and head["subject_areas"][0]["name"] == "sales"
