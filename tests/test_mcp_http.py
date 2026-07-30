"""The HTTP MCP transport — auth shim, OAuth discovery, and tools/list parity over HTTP.

Needs the [server] extra (the MCP SDK + ASGI stack); skipped cleanly without it.
"""

from __future__ import annotations

import json
import re

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")

import mcp_http  # noqa: E402
import tools  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

BASE = "https://demo.example.com"
PRODUCT_TOOLS = {
    "list_datasources",
    "get_datasource_schema",
    "get_prompt_examples",
    "execute_sql",
}


@pytest.fixture
def base_url(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    # These tests exercise the bearer-presence default — clear any ambient signing secret so the
    # provider selection is deterministic (a dev with AGAMI_SIGNING_SECRET exported would otherwise
    # get JWT mode and see "Bearer present" rejected).
    monkeypatch.delenv("AGAMI_SIGNING_SECRET", raising=False)
    return BASE


def test_public_base_url_is_required(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="PUBLIC_BASE_URL"):
        mcp_http.public_base_url()


def test_build_app_fails_fast_without_public_base_url(monkeypatch):
    # S2: the missing-env error must surface at construction, not as a per-request 500 inside the
    # auth middleware (which would leak a traceback under debug).
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="PUBLIC_BASE_URL"):
        mcp_http.build_app()


def test_auth_skip_is_scoped_to_discovery_routes_only(base_url):
    # S1: only the OAuth-discovery prefixes are open; any other /.well-known/* path still requires
    # auth (no blanket "/.well-known/" bypass).
    c = TestClient(mcp_http.build_app())
    assert c.get("/.well-known/openid-configuration").status_code == 401
    # A sibling that merely shares the prefix (no path boundary) must NOT skip auth either — the
    # skip matches on a boundary (exact or prefix + "/"), not a bare startswith.
    assert c.get("/.well-known/oauth-protected-resource-evil").status_code == 401
    assert c.get("/.well-known/oauth-protected-resource").status_code == 200  # exact route open
    assert (
        c.get("/.well-known/oauth-protected-resource/mcp").status_code == 200
    )  # suffixed variant open


def test_build_app_requires_an_https_base(monkeypatch):
    # The Secure admin cookie + OAuth need TLS — a plain-http base must fail fast at construction.
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://insecure.example.com")
    with pytest.raises(RuntimeError, match="https"):
        mcp_http.build_app()


def test_static_admin_and_root_skip_the_bearer_gate(base_url):
    # The brand assets, the root landing, and the /admin/* pages are open at the *bearer* layer (admin
    # pages do their own session auth). Lookalikes that merely share a prefix must NOT skip — the skip
    # matches on a path boundary, not a bare startswith.
    assert mcp_http._is_public_path("/static/logo_h.svg")
    assert mcp_http._is_public_path("/")
    for p in mcp_http_admin_paths():
        assert mcp_http._is_public_path(p)
    assert not mcp_http._is_public_path("/static-evil")
    assert not mcp_http._is_public_path("/admin-evil")
    assert not mcp_http._is_public_path("/admin/secret")  # a non-routed /admin path stays gated
    assert not mcp_http._is_public_path("/mcp")


def mcp_http_admin_paths():
    import admin

    return admin.ADMIN_PATHS


def test_browser_hitting_mcp_gets_a_branded_html_401(base_url):
    # A human who pastes the connector URL into a browser gets a friendly page, not raw JSON — but the
    # SAME 401 + WWW-Authenticate, so the machine OAuth bootstrap is unchanged.
    c = TestClient(mcp_http.build_app())
    r = c.get("/mcp", headers={"accept": "text/html"})
    assert r.status_code == 401
    assert "text/html" in r.headers["content-type"]
    assert "<html" in r.text and "MCP endpoint" in r.text
    assert r.headers.get("www-authenticate", "").startswith("Bearer ")
    # claude.ai (JSON / event-stream Accept) still gets the JSON body it expects.
    j = c.post(
        "/mcp",
        headers={"accept": "application/json"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert j.status_code == 401 and "application/json" in j.headers["content-type"]


def test_discovery_advertises_public_base_url(base_url):
    c = TestClient(mcp_http.build_app())
    pr = c.get("/.well-known/oauth-protected-resource")
    assert pr.status_code == 200
    assert pr.json()["resource"] == f"{BASE}/mcp"
    assert pr.json()["authorization_servers"] == [BASE]
    # the path-suffixed variant the connector probes resolves to the same doc
    assert c.get("/.well-known/oauth-protected-resource/mcp").status_code == 200
    as_ = c.get("/.well-known/oauth-authorization-server")
    assert as_.status_code == 200 and as_.json()["issuer"] == BASE


def test_unauthenticated_request_gets_401_challenge(base_url):
    c = TestClient(mcp_http.build_app())
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401
    www = r.headers.get("www-authenticate", "")
    assert www.startswith("Bearer ")
    assert f'resource_metadata="{BASE}/.well-known/oauth-protected-resource"' in www


def test_non_bearer_and_empty_tokens_are_rejected(base_url):
    c = TestClient(mcp_http.build_app())
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    # Only the Bearer scheme with a non-empty token counts as present.
    for authz in ("", "Bearer ", "Bearer    ", "Basic abc123", "token xyz"):
        r = c.post("/mcp", headers={"Authorization": authz}, json=body)
        assert r.status_code == 401, authz


def test_mcp_bare_path_is_not_307_redirected(base_url):
    """Regression: Mount('/mcp') 307-redirects the no-slash /mcp to /mcp/, and claude.ai (unlike
    TestClient's default) does NOT follow it, so the connector errors right after login. The shim must
    let an authed POST /mcp reach the handler directly — a non-307 status, matching /mcp/.

    `follow_redirects=False` is load-bearing: with the default (follow on), the auto-followed 307
    would hide the bug — which is exactly why it shipped."""
    headers = {
        "Authorization": "Bearer present",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        },
    }
    with TestClient(mcp_http.build_app(), follow_redirects=False) as c:
        # Auth still gates the bare path (the shim must not become a bypass) — no bearer → 401, not a 307.
        assert c.post("/mcp", json=init).status_code == 401
        bare = c.post("/mcp", headers=headers, json=init)
        slashed = c.post("/mcp/", headers=headers, json=init)
    assert bare.status_code != 307  # the fix: the bare path is served, not redirected
    assert bare.status_code == 200  # reaches `initialize` directly
    assert bare.status_code == slashed.status_code  # parity with the trailing-slash form


def test_http_tools_list_is_the_same_four(base_url):
    """Authed end-to-end: initialize → tools/list over HTTP returns exactly the 4 product tools —
    the same surface stdio advertises (mirrored, not forked)."""
    headers = {
        "Authorization": "Bearer present",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    with TestClient(mcp_http.build_app()) as c:
        init = c.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"},
                },
            },
        )
        assert init.status_code == 200
        sid = init.headers.get("mcp-session-id")
        h2 = {**headers, **({"mcp-session-id": sid} if sid else {})}
        c.post("/mcp", headers=h2, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        tl = c.post("/mcp", headers=h2, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert tl.status_code == 200
        # the streamable-HTTP response may be SSE-framed; pull the JSON-RPC envelope out
        payload = json.loads(re.search(r"\{.*\}", tl.text, re.DOTALL).group(0))
        names = {t["name"] for t in payload["result"]["tools"]}
    assert names == PRODUCT_TOOLS


def test_presence_auth_yields_no_session_id(base_url):
    """The fallback that matters most: presence auth mints no token at all, so there is no session to
    report. It must read as "no session" — never break the caller."""
    from oss_adapters import PresenceAuthProvider

    principal = PresenceAuthProvider().validate_token("present")
    assert principal is not None and principal.subject == "local"
    assert principal.session_id is None


def test_the_session_id_reaches_a_tool_handler(base_url, monkeypatch):
    """End to end over the real transport: a tool runs on a WORKER THREAD, so the contextvar set in
    `handle_mcp` only reaches it because anyio copies the request context across the thread hop. Assert
    that rather than trusting it — it is the whole delivery mechanism for a consumer's session key."""
    seen = {}

    def _probe(args: dict) -> str:
        seen["session"] = mcp_http.current_session_id()
        return "ok"

    tool = {
        "handler": _probe,
        "description": "probe",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    }

    class _P:
        subject = "jordan@example.com"
        session_id = "sess-42"

    class _Auth:
        def validate_token(self, token):
            return _P() if (token or "").strip() else None

    from dataclasses import replace

    adapters = replace(mcp_http.default_adapters(), auth_provider=_Auth())
    app = mcp_http.create_app(extra_tools={"probe": tool}, adapters=adapters)
    headers = {
        "Authorization": "Bearer anything",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    with TestClient(app) as c:
        c.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"},
                },
            },
        )
        c.post(
            "/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        r = c.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "probe", "arguments": {}},
            },
        )
        assert r.status_code == 200, r.text
    assert seen["session"] == "sess-42"
    # ...and it does not leak past the request.
    assert mcp_http.current_session_id() is None


# --- the tool-visibility seam --------------------------------------------------


def _visibility_app(predicate):
    """An app whose advertised surface is narrowed by `predicate`, wired the way a consumer would."""
    from dataclasses import replace

    probe = {
        "handler": lambda a: "ran",
        "description": "probe",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    }
    adapters = replace(mcp_http.default_adapters(), tool_visibility=predicate)
    return mcp_http.create_app(extra_tools={"probe": probe}, adapters=adapters)


def _mcp(c, method, params=None, rid=1):
    headers = {
        "Authorization": "Bearer present",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    body = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        body["params"] = params
    return c.post("/mcp", headers=headers, json=body)


def _handshake(c):
    _mcp(
        c,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        },
        rid=1,
    )
    _mcp(c, "notifications/initialized")


def test_no_visibility_predicate_leaves_the_surface_unchanged(base_url):
    """The default must be byte-identical to before the seam existed — this is an additive hook."""
    with TestClient(_visibility_app(None)) as c:
        _handshake(c)
        names = {t["name"] for t in _mcp(c, "tools/list", {}, rid=2).json()["result"]["tools"]}
    assert "probe" in names
    assert names >= set(tools.TOOLS)  # every core tool still advertised


def test_a_hidden_tool_is_absent_from_the_list(base_url):
    with TestClient(_visibility_app(lambda name: name != "probe")) as c:
        _handshake(c)
        listed = _mcp(c, "tools/list", {}, rid=2).json()["result"]["tools"]
        names = {t["name"] for t in listed}
    assert "probe" not in names
    assert "execute_sql" in names  # subtractive: only the named tool goes


def test_a_hidden_tool_is_also_not_callable(base_url):
    """Listing alone would leave it callable by name — a surface that looks narrowed but is not."""
    with TestClient(_visibility_app(lambda name: name != "probe")) as c:
        _handshake(c)
        r = _mcp(c, "tools/call", {"name": "probe", "arguments": {}}, rid=2)
    assert "Unknown tool" in r.text  # answers as ABSENT, not as refused — the list is no oracle


def test_a_visible_tool_still_runs(base_url):
    with TestClient(_visibility_app(lambda name: True)) as c:
        _handshake(c)
        r = _mcp(c, "tools/call", {"name": "probe", "arguments": {}}, rid=2)
    assert "ran" in r.text


def test_a_predicate_that_raises_hides_rather_than_grants(base_url):
    """A consumer's broken predicate must not be an accidental grant, and must not kill the transport."""

    def boom(name):
        raise RuntimeError("classification blew up")

    with TestClient(_visibility_app(boom)) as c:
        _handshake(c)
        names = {t["name"] for t in _mcp(c, "tools/list", {}, rid=2).json()["result"]["tools"]}
        called = _mcp(c, "tools/call", {"name": "execute_sql", "arguments": {}}, rid=3)
    assert names == set()  # everything hidden, nothing granted
    assert "Unknown tool" in called.text


def test_the_predicate_sees_the_request_context(base_url):
    """The whole point of a per-request hook: the consumer's predicate can read who is asking."""
    seen = {}

    def by_caller(name):
        seen["actor"] = mcp_http._actor_ctx.get()
        return True

    with TestClient(_visibility_app(by_caller)) as c:
        _handshake(c)
        _mcp(c, "tools/list", {}, rid=2)
    assert "actor" in seen  # ran inside the request, not at composition time


def test_a_surviving_tools_schema_is_untouched(base_url):
    """Subtractive only — a filter may remove a tool but never reshape one that survives."""
    with TestClient(_visibility_app(lambda name: name != "probe")) as c:
        _handshake(c)
        listed = {t["name"]: t for t in _mcp(c, "tools/list", {}, rid=2).json()["result"]["tools"]}
    assert listed["execute_sql"]["inputSchema"] == tools.TOOLS["execute_sql"]["inputSchema"]
    assert listed["execute_sql"]["description"] == tools.TOOLS["execute_sql"]["description"]
