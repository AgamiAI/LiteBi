#!/usr/bin/env python3
"""agami serve --http — the HTTP MCP transport (streamable-HTTP).

Advertises the **same** shared `tools.TOOLS` registry as the stdio entrypoint, but over a network
endpoint a remote client (claude.ai) can reach — plus the OAuth-discovery surface and a bearer
auth shim. This is the network product: unlike the stdio harness it binds a port, so it carries
auth — an unauthenticated request gets a `401` + `WWW-Authenticate` challenge, which triggers the
client's OAuth flow against the authorize/token endpoints (see `oauth_server`). The issued JWT then
gates `/mcp`; with no signing secret configured the OSS bearer-presence default applies instead.

Requires the **[server]** extra (the MCP SDK + ASGI stack). `PUBLIC_BASE_URL` must be set
explicitly — it backs the discovery documents + the `WWW-Authenticate` resource URL and cannot be
reliably auto-detected behind a proxy/LB.

    PUBLIC_BASE_URL=https://your-host python -m mcp_http
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from collections.abc import Callable
from contextvars import ContextVar
from pathlib import Path

import admin
import onboarding
import user_store
from async_offload import run_blocking
from execute_sql import BUILTIN_EXECUTOR
from oss_adapters import (
    FileActivitySink,
    PresenceAuthProvider,
    SingleTenantOrgResolver,
    WarnOnlyGovernancePolicy,
)
from ports import Adapters, AuthProvider, Org, OrgResolver
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from store import Store
from tools import (
    SERVER_INSTRUCTIONS,
    SERVER_NAME,
    TOOLS,
    _current_org_ctx,
    bootstrap_paths,
    record_tool_call,
    resolved_org_id,
    server_version,
    set_injected_executor,
)

_log = logging.getLogger(__name__)

# The authenticated user for the in-flight tool call. Set in `handle_mcp` (the raw-ASGI endpoint, which
# runs in the request's task) so it propagates into the MCP dispatch — the tool handler `_call_tool`
# only receives (name, arguments), and a contextvar set in the BaseHTTPMiddleware wouldn't reach it.
_actor_ctx: ContextVar[str | None] = ContextVar("agami_tool_actor", default=None)

# The CLIENT AUTHORIZATION behind the in-flight tool call — what tells two windows under one login apart.
# Set alongside `_actor_ctx` and for the same reason. None whenever the caller's auth has no session
# notion (presence auth, a hand-minted bearer): consumers must read that as "no session", not an error.
_session_ctx: ContextVar[str | None] = ContextVar("agami_tool_session", default=None)


def current_session_id() -> str | None:
    """The session id for the request being served, or None.

    Public because a consumer's tool handler needs it and the request never reaches the handler — only
    this contextvar does. Same shape as `tools.current_org_id`, so consumers don't have to reach into a
    private module attribute the way they otherwise would.
    """
    return _session_ctx.get()


def _principal_from_scope(scope: dict, auth: AuthProvider) -> object | None:
    """The authenticated caller for this /mcp request: the principal the auth middleware already validated
    (carried on the ASGI scope state), or — if that didn't propagate — re-validated from the bearer header
    on the scope (robust fallback). None under presence auth or if absent.

    ONE validation, from which every derived value is read. The subject and the session must describe the
    same caller, so deriving them from two independent `validate_token` calls would both double the work on
    the fallback path (a second signature check, or a second lookup for a DB-backed provider) and let them
    diverge if a provider's validation isn't deterministic.
    """
    principal = (scope.get("state") or {}).get("principal")
    if principal is not None:
        return principal
    for key, value in scope.get("headers", []):
        if key == b"authorization" and value[:7].lower() == b"bearer ":
            return auth.validate_token(value[7:].strip().decode("latin-1"))
    return None


def _actor_from_scope(scope: dict, auth: AuthProvider) -> str | None:
    """The authenticated user's subject for this /mcp request, or None. Thin wrapper over
    `_principal_from_scope` — `handle_mcp` resolves the principal once and reads both values off it, so this
    exists for callers that want only the subject."""
    return getattr(_principal_from_scope(scope, auth), "subject", None)


def _normalized_session(principal: object | None) -> str | None:
    """A principal's session id, or None — enforcing the `str | None` shape the contextvar promises.

    `AuthProvider` is a `@runtime_checkable` Protocol, so conformance is method presence only: a
    third-party provider's principal may expose no `session_id` at all, or a non-string one. Without this
    an unexpected type would reach `_session_ctx` and, through it, consumers using the value as a key.
    Same rule as `JwtAuthProvider.validate_token`'s read — non-string or blank is "no session"."""
    sid = getattr(principal, "session_id", None)
    return sid if isinstance(sid, str) and sid.strip() else None


def _session_from_scope(scope: dict, auth: AuthProvider) -> str | None:
    """The session id for this /mcp request, or None. Thin wrapper over `_principal_from_scope`, the mirror
    of `_actor_from_scope`."""
    return _normalized_session(_principal_from_scope(scope, auth))


def _org_id_from_scope(scope: dict) -> str | None:
    """The resolved org id for this /mcp request — the org the auth middleware attached to the ASGI scope
    state (`request.state.org`). None under presence auth / single-tenant, where the tool layer falls back
    to AGAMI_ORG_ID / 'local'. Keeps the per-process model cache tenant-safe (ACE-045): cache entries key on
    this, so under a multi-tenant resolver one org never gets another's cached model."""
    org = (scope.get("state") or {}).get("org")
    return getattr(org, "id", None)


# The brand assets (logo, provider icons, favicon) served at /static — packaged alongside this module.
_STATIC_DIR = Path(__file__).resolve().parent / "static"


def _build_auth_provider() -> AuthProvider:
    """Pick the token validator. Presence (any non-empty bearer) is the local OSS default ONLY when
    no signing secret is configured *at all*. If `AGAMI_SIGNING_SECRET` is present — even empty or
    too weak — that signals intent to run real JWT auth, so we validate it now (fail fast at
    construction) rather than silently downgrade a misconfigured hosted deploy to presence."""
    if "AGAMI_SIGNING_SECRET" in os.environ:
        from oauth_server import JwtAuthProvider, _signing_secret

        _signing_secret()  # raises on an empty/weak secret — no insecure fallback on misconfig
        return JwtAuthProvider()
    return PresenceAuthProvider()


def _build_org_resolver() -> SingleTenantOrgResolver:
    """The OSS default tenancy: single-tenant, one configured org. The id is the F14 minted uuid
    resolved by `tools.resolved_org_id()` (AGAMI_ORG_ID env -> organization.yaml -> "local") — the SAME
    resolution the deploy stamp uses, so writes and reads agree. Multi-tenant is a future change at
    the *schema* layer plus an authz check — not a resolver swap, so the seam lives here now."""
    org_id = resolved_org_id()
    # Load-bearing for a hand-rolled DB-only deploy: log the resolved id + where it came from, so an
    # operator can see "org_id=local (default)" and realize organization.yaml/AGAMI_ORG_ID isn't reaching the
    # server (see F14's documented residual risk).
    source = (
        "env"
        if os.environ.get("AGAMI_ORG_ID", "").strip()
        else ("default" if org_id == "local" else "organization.yaml")
    )
    _log.info("single-tenant org_id=%s (source=%s)", org_id, source)
    return SingleTenantOrgResolver(Org(id=org_id))


def default_adapters() -> Adapters:
    """The OSS default adapters bundled for the composition root (env-driven auth + org). Used only by
    `create_app(adapters=None)` — the HTTP server's defaults. `create_app` wires `auth_provider` +
    `org_resolver` into the request path and registers `executor`; `activity_sink` + `governance` are
    carried on the container for consumers.

    `executor=BUILTIN_EXECUTOR` (ACE-028) makes the HTTP server run execution **in-process** by default
    — no per-query subprocess fork, no CSV round-trip — behind the same guard (AH-012). The forking
    subprocess is the wrong shape for a long-running server; the local stdio path (`mcp_harness`) and
    the `python -m execute_sql` CLI never build these adapters, so they keep the subprocess isolation."""
    return Adapters(
        activity_sink=FileActivitySink(),
        org_resolver=_build_org_resolver(),
        auth_provider=_build_auth_provider(),
        governance=WarnOnlyGovernancePolicy(),
        executor=BUILTIN_EXECUTOR,
    )


def public_base_url() -> str:
    """The explicit public base URL. Required — discovery + redirect URIs are built from it and it
    can't be inferred behind a proxy/LB (the OAUTH_ISSUER_URL gotcha)."""
    url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if not url:
        raise RuntimeError(
            "PUBLIC_BASE_URL must be set — it backs OAuth/MCP discovery + the WWW-Authenticate "
            "resource URL and can't be auto-detected behind a proxy/LB."
        )
    return url


def _resource_metadata_url(base: str) -> str:
    return f"{base}/.well-known/oauth-protected-resource"


def _unauthenticated(base: str, request: Request | None = None) -> Response:
    """401 + WWW-Authenticate pointing at the discovery doc — the challenge that starts OAuth.

    Content-negotiated: a browser (Accept: text/html) gets a branded "this is an MCP endpoint" page so
    a human who pastes the URL isn't met with raw JSON; claude.ai (JSON / event-stream Accept) gets the
    JSON body it expects. **Same 401 status + WWW-Authenticate header either way**, so the machine
    challenge that bootstraps OAuth is unchanged — only the body differs."""
    headers = {
        "WWW-Authenticate": f'Bearer resource_metadata="{_resource_metadata_url(base)}"',
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Expose-Headers": "WWW-Authenticate",
    }
    if request is not None and "text/html" in (request.headers.get("accept") or ""):
        return HTMLResponse(admin.mcp_landing_body_html(base), status_code=401, headers=headers)
    return JSONResponse({"error": "Not authenticated"}, status_code=401, headers=headers)


# Only the OAuth-discovery endpoints are reachable unauthenticated (the client probes them before
# it has a token). Scoped to these exact prefixes — NOT a blanket `/.well-known/` skip — so the
# open surface is exactly the routes we serve, not "anything starting with /.well-known/".
_PUBLIC_PREFIXES = (
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-authorization-server",
)


# The OAuth flow endpoints are pre-auth by definition — the user has no bearer token yet (they're
# obtaining one). They enforce their own validation (credential check, PKCE, single-use codes,
# redirect allow-listing), so they're public at the transport layer.
_OAUTH_PATHS = (
    "/oauth/authorize",
    "/oauth/token",
    "/oauth/register",
    "/oauth/oidc/start",
    "/oauth/oidc/callback",
)


def _is_public_path(path: str) -> bool:
    """True for the surface reachable without an MCP bearer token: the OAuth discovery routes + flow
    endpoints, the static brand assets, the root landing, and the `/admin/*` pages. Discovery and
    static use boundary matching (they have suffix/sub-path routes, and a bare `startswith` would let
    `/.well-known/oauth-protected-resource-x` or `/static-x` slip through); the OAuth + admin
    endpoints are matched *exactly* (only those exact paths are routed), so a future
    `/oauth/token/...` or `/admin/...` route can't inherit public access by accident.

    Static + admin are "public" only at the *bearer* layer: assets are genuinely public, and the
    admin pages run their OWN session-cookie auth (see `admin.current_admin`) — they are not
    unguarded, just guarded by a different credential than the MCP token."""
    if any(path == p or path.startswith(p + "/") for p in _PUBLIC_PREFIXES):
        return True
    if path == "/static" or path.startswith("/static/"):
        return True
    if path == "/":
        return True
    return path in _OAUTH_PATHS or path in admin.ADMIN_PATHS or path in onboarding.PUBLIC_PATHS


class _AuthMiddleware(BaseHTTPMiddleware):
    """Gate every request on a Bearer token via the configured `AuthProvider` (a real JWT validator
    in the hosted OAuth path, or bearer-presence locally); the discovery + OAuth-flow endpoints stay
    open. On a request that passes auth, resolve the single-tenant org and attach it to
    request.state.org — the explicit single-tenant contract + the multi-tenant seam."""

    def __init__(self, app, resolver: OrgResolver, auth: AuthProvider) -> None:
        super().__init__(app)
        self._resolver = resolver
        self._auth = auth

    async def dispatch(self, request: Request, call_next):
        if _is_public_path(request.url.path):
            return await call_next(request)
        authz = request.headers.get("authorization") or request.headers.get("Authorization") or ""
        # Require the Bearer scheme specifically (not just any Authorization header), then hand the
        # token to the configured provider — a real JWT validator in the hosted OAuth path, or
        # bearer-presence locally.
        if not authz.lower().startswith("bearer "):
            return _unauthenticated(public_base_url(), request)
        principal = self._auth.validate_token(authz[7:].strip())
        if principal is None:
            return _unauthenticated(public_base_url(), request)
        # Carry the validated principal on the ASGI scope state so the /mcp endpoint can stamp the
        # activity log with the actor (the tool dispatch doesn't get the request).
        request.state.principal = principal
        # Resolve the org for this request. Single-tenant returns the one configured org regardless
        # of context; nothing downstream consumes it yet (tools key on `datasource`), so this asserts
        # the contract and reserves the seam — it does not add org-scoped behavior.
        try:
            request.state.org = self._resolver.resolve_org(request)
        except PermissionError:
            # A resolver may refuse a principal it cannot place. The refusal has to land as a clean 403,
            # not the 500 an uncaught raise would give (which leaks a traceback under debug). Authentication
            # already passed by here — this is "you are who you say, but you have no org", so 403, not 401.
            # The OSS resolver never raises, so this path is inert single-tenant.
            return JSONResponse({"error": "Forbidden"}, status_code=403)
        return await call_next(request)


class _NormalizeMcpSlash:
    """Rewrite the exact path ``/mcp`` → ``/mcp/`` before routing.

    Starlette's ``Mount("/mcp", …)`` answers a request to the bare ``/mcp`` (no trailing slash) with a
    307 redirect to ``/mcp/``. claude.ai posts the connector URL ``{base}/mcp`` and does **not** follow
    that redirect, so the connector errors right after login. ``handle_mcp`` ignores the sub-path, so
    normalizing here is loss-free. Pure ASGI (not ``BaseHTTPMiddleware``) so it edits the scope path
    with no request-body buffering; only the exact ``/mcp`` is touched — ``/mcp/…`` and every other
    route pass through untouched. Placed outermost so it runs before auth + routing; the fix lives in
    the app, so it also covers the Caddy-less ``cloud-run`` profile (no proxy rewrite needed).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == "/mcp":
            scope = dict(scope, path="/mcp/", raw_path=b"/mcp/")
        await self.app(scope, receive, send)


async def _protected_resource(request: Request) -> JSONResponse:
    """RFC 9728 — tells the client where the authorization server is."""
    base = public_base_url()
    return JSONResponse(
        {
            "resource": f"{base}/mcp",
            "authorization_servers": [base],
            "bearer_methods_supported": ["header"],
        },
        headers={"Access-Control-Allow-Origin": "*"},
    )


async def _auth_server(request: Request) -> JSONResponse:
    """RFC 8414 — the authorization-server metadata advertising the authorize/token/register
    endpoints (served by `oauth_server`) so the client can run the OAuth flow."""
    base = public_base_url()
    return JSONResponse(
        {
            "issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
        },
        headers={"Access-Control-Allow-Origin": "*"},
    )


def build_server(
    registry: dict | None = None,
    extra_instructions: str | None = None,
    visibility: Callable[[str], bool] | None = None,
):
    """A low-level MCP Server whose tool surface IS the given registry — list_tools / call_tool read
    from it, so HTTP advertises exactly what stdio does (no duplicate defs). Defaults to the shared
    `tools.TOOLS`; `create_app` passes a merged copy (base + a consumer's extra tools).

    `extra_instructions` is APPENDED to `SERVER_INSTRUCTIONS` (never replaces it) and surfaced to the
    model in the MCP `initialize` result — append-only so a consumer can add guidance but can't drop
    the base protocol's safety directives (e.g. the sensitive-column output rule). None = no-op.

    `visibility(tool_name) -> bool` narrows the surface PER REQUEST. None (the default) is exactly
    today's behaviour: the whole registry, listed and callable. It exists because a registry assembled
    once at composition time cannot answer "may THIS caller see this tool", and for a consumer running a
    server-side agent that is the difference between a control and a suggestion: the model decides what
    to call, so withholding the tool is the control and "it shouldn't call that" is not.

    An empty hook by design — this module decides nothing about who may see what. The predicate is the
    consumer's, and it reads the request context itself: this callable runs inside the request's task
    (see `handle_mcp`), so `_actor_ctx` / `_session_ctx` / `tools._current_org_ctx` are all live in it.

    SUBTRACTIVE ONLY. It filters the one shared registry; it never adds, renames, or reshapes a tool. A
    surviving tool's description and inputSchema pass through untouched, so a consumer cannot fork the
    surface into a private variant under cover of "visibility".
    """
    import mcp.types as mt
    from mcp.server.lowlevel import Server

    registry = TOOLS if registry is None else registry

    def _visible(name: str) -> bool:
        """Applied at BOTH seams. Listing alone would leave an unlisted tool callable by name, which is
        worse than either control on its own — the surface would look narrowed while remaining open."""
        if visibility is None:
            return True
        try:
            return bool(visibility(name))
        except Exception:
            # A consumer's predicate that raises must not be an accidental grant. Refuse the tool and
            # keep serving; the alternative (propagating) turns one bad classification into a dead
            # transport for every caller.
            _log.exception("tool visibility predicate failed for %r; hiding the tool", name)
            return False

    # Appended, never replacing: SERVER_INSTRUCTIONS carries the PII output rule, so replace-semantics
    # would let a consumer silently drop a safety directive.
    instructions = SERVER_INSTRUCTIONS
    if extra_instructions:
        instructions = f"{instructions}\n{extra_instructions}"
    server = Server(SERVER_NAME, version=server_version(), instructions=instructions)

    @server.list_tools()
    async def _list_tools() -> list:
        return [
            mt.Tool(name=name, description=meta["description"], inputSchema=meta["inputSchema"])
            for name, meta in registry.items()
            if _visible(name)
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict) -> list:
        meta = registry.get(name)
        # A hidden tool answers as an ABSENT one, not as a refused one: the same `Unknown tool` a typo
        # gets. Distinguishing them would turn the list into an oracle — a caller could enumerate what
        # exists but is withheld, which is the fact hiding it was meant to keep.
        if meta is None or not _visible(name):
            raise ValueError(f"Unknown tool: {name}")
        # Record every tool call to the admin activity log — timed, attributed to the authenticated
        # actor, never allowed to break the tool (logging is best-effort + double-guarded).
        started = time.monotonic()
        result_text = None
        raised = False
        try:
            # Run the tool handler OFF the event loop. The heavy handlers block for the whole query —
            # execute_sql runs it under the executor's own bounds (the per-statement budget, plus the
            # supervisor's slack on the fork path) and hits the warehouse — so on the loop a single
            # slow query would freeze every other in-flight request. This completes ACE-048 (which
            # off-loaded the KDF/OIDC/audit calls but left the handler on the loop).
            # `run_blocking` (anyio.to_thread) copies the request context into the worker thread, so
            # the org-scoped model cache (ACE-045, read via `_current_org_ctx`) stays tenant-correct.
            result_text = await run_blocking(meta["handler"], arguments or {})
            return [mt.TextContent(type="text", text=result_text)]
        except Exception:
            raised = True
            raise
        finally:
            # The per-call audit write opens a fresh Store + INSERT + close; run it off the event loop so
            # it doesn't add DB latency to every tool call on the loop (ACE-048). `_actor_ctx.get()` is read
            # here (on the loop) and passed in. Still best-effort — a logging failure never breaks the tool.
            try:
                await run_blocking(
                    record_tool_call,
                    name=name,
                    arguments=arguments,
                    result_text=result_text,
                    execution_ms=int((time.monotonic() - started) * 1000),
                    actor=_actor_ctx.get(),
                    raised=raised,
                )
            except Exception:
                pass

    return server


def create_app(
    extra_tools: dict | None = None,
    adapters: Adapters | None = None,
    extra_instructions: str | None = None,
) -> Starlette:
    """The ASGI app + the composition factory: the `.well-known` discovery routes + the
    streamable-HTTP MCP endpoint at /mcp, behind the auth middleware. Merges `extra_tools` over a
    COPY of the shared TOOLS (never mutating the global) and wires the `adapters` into the request
    path (auth + org resolution; OSS defaults when None). `create_app()` with no args == the
    historical `build_app()` behavior.

    Reusing an existing tool name in `extra_tools` overrides that tool in this app's registry copy —
    intentional at the composition root (the caller opts in explicitly). `tools.register` is the
    guarded path that refuses a duplicate name.

    `extra_instructions` is APPENDED to the base MCP instructions and surfaced to the model via the
    MCP `initialize` result (never replaces the base protocol — see `build_server`). None = no-op."""
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    # Fail fast at construction if PUBLIC_BASE_URL is unset — not per-request inside the middleware
    # (where the RuntimeError would surface as a 500, leaking a traceback under debug). Anything that
    # builds the app via --factory / an embedding harness gets a clear error up front.
    base = public_base_url()
    # TLS is mandatory: claude.ai's OAuth and the Secure admin session cookie both require https. A
    # plain-http PUBLIC_BASE_URL would silently break the admin login (the browser drops a Secure
    # cookie), so fail fast with a clear message instead. (Set this to the public https URL even when
    # TLS terminates at a proxy — the browser↔proxy hop is what must be https.)
    if not base.startswith("https://"):
        raise RuntimeError(
            "PUBLIC_BASE_URL must be https:// (OAuth + the Secure admin cookie need TLS)."
        )
    bootstrap_paths()
    adapters = adapters or default_adapters()
    auth_provider = adapters.auth_provider
    # AH-012: register the composition-root executor (None = the default subprocess path). Behind the
    # shared guard in `tool_execute_sql`; a hosted consumer injects a pooled/RBAC/tunnel executor here.
    set_injected_executor(adapters.executor)
    # Validate consumer-supplied tools up front so a malformed entry fails at construction with a
    # clear error, not later as a KeyError/500 inside tools/list or tools/call.
    for tool_name, meta in (extra_tools or {}).items():
        if not isinstance(meta, dict) or not {"handler", "description", "inputSchema"} <= set(meta):
            raise ValueError(
                f"extra tool {tool_name!r} must be a dict with handler, description, inputSchema"
            )
        if not callable(meta["handler"]):
            raise ValueError(f"extra tool {tool_name!r} handler must be callable")
    # Merge the consumer's extra tools over a COPY of TOOLS — the module global is never mutated.
    registry = {**TOOLS, **(extra_tools or {})}
    session_manager = StreamableHTTPSessionManager(
        # `tool_visibility` is read from the adapters rather than taken as a `create_app` parameter, so it
        # rides the seam a consumer already swaps (`replace(default_adapters(), …)`) instead of adding a
        # second, parallel way to configure the surface. None on the OSS defaults ⇒ unchanged behaviour.
        app=build_server(
            registry,
            extra_instructions=extra_instructions,
            visibility=getattr(adapters, "tool_visibility", None),
        ),
        json_response=True,
        stateless=True,
    )

    async def handle_mcp(scope, receive, send):
        # Set the actor + resolved org for this request's tool calls, then run the MCP dispatch in the same
        # task so the contextvars reach `_call_tool` and the per-process model cache. Prefer what the auth
        # middleware attached to the scope state; the actor falls back to re-validating the bearer.
        # Resolve the caller ONCE: the subject and the session must describe the same principal, and on the
        # fallback path two lookups would mean validating the same bearer twice.
        principal = _principal_from_scope(scope, auth_provider)
        token = _actor_ctx.set(getattr(principal, "subject", None))
        org_token = _current_org_ctx.set(_org_id_from_scope(scope))
        session_token = _session_ctx.set(_normalized_session(principal))
        try:
            await session_manager.handle_request(scope, receive, send)
        finally:
            _actor_ctx.reset(token)
            _current_org_ctx.reset(org_token)
            _session_ctx.reset(session_token)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette):
        # Heal the schema before serving: apply any pending migrations so freshly-deployed code never hits
        # an old DB shape (a column a migration adds, selected before it's applied, 500s the admin). This
        # is fail-closed — a failing migration propagates and aborts startup; a half-migrated DB never
        # serves. File-mode (no DB configured) has nothing to migrate.
        store = Store.from_env()
        if store is not None:
            try:
                applied = store.run_migrations()  # fail-closed: a bad migration aborts startup
                if applied:
                    _log.info("applied migrations: %s", ", ".join(applied))
                # Seed the configured admin (AGAMI_ADMIN_*) so a fresh deploy has someone who can sign in —
                # nothing else creates it. Create-if-absent + idempotent. BEST-EFFORT, unlike migrations: when
                # several instances boot together they can race on the admin INSERT (a UNIQUE violation); the
                # admin is seeded either way, so log + roll back + continue rather than aborting startup.
                try:
                    if user_store.seed_admin_from_env(store):
                        _log.info("seeded the configured admin")  # not the email — no PII in logs
                    store.commit()
                except Exception:  # noqa: BLE001 — a concurrent boot won the seed; not fatal
                    store.conn.rollback()
                    _log.warning(
                        "admin seed skipped (already seeded or a concurrent boot won the race)"
                    )
            finally:
                store.close()
        async with session_manager.run():
            yield

    from oauth_server import authorize, oidc_callback, oidc_start, register, token

    async def _root(request: Request) -> Response:
        """The bare base URL in a browser → a branded landing (connector URL + admin link), not a 404."""
        return HTMLResponse(admin.landing_body_html(public_base_url()))

    routes = [
        Route("/", _root, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource", _protected_resource),
        Route("/.well-known/oauth-protected-resource/{rest:path}", _protected_resource),
        Route("/.well-known/oauth-authorization-server", _auth_server),
        Route("/.well-known/oauth-authorization-server/{rest:path}", _auth_server),
        Route("/oauth/authorize", authorize, methods=["GET", "POST"]),
        Route("/oauth/token", token, methods=["POST"]),
        Route("/oauth/register", register, methods=["POST"]),
        Route("/oauth/oidc/start", oidc_start, methods=["GET"]),
        Route("/oauth/oidc/callback", oidc_callback, methods=["GET"]),
        *admin.routes(),
        *onboarding.routes(),
        Mount("/static", app=StaticFiles(directory=_STATIC_DIR), name="static"),
        Mount("/mcp", app=handle_mcp),
    ]
    middleware = [
        # Outermost: normalize the bare `/mcp` → `/mcp/` before routing so Starlette's Mount doesn't
        # 307-redirect it (claude.ai posts `{base}/mcp` and won't follow the redirect). See _NormalizeMcpSlash.
        Middleware(_NormalizeMcpSlash),
        Middleware(_AuthMiddleware, resolver=adapters.org_resolver, auth=auth_provider),
    ]
    return Starlette(routes=routes, middleware=middleware, lifespan=lifespan)


def build_app() -> Starlette:
    """Backwards-compatible entrypoint — `create_app()` with the OSS defaults and no extra tools, so
    the existing `python -m mcp_http` / `main()` path is unchanged."""
    return create_app()


def main() -> int:
    import uvicorn

    public_base_url()  # fail fast if unset
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    # Bind via the import-string factory (not a built instance) so `WORKERS=N` can fork N worker processes —
    # uvicorn re-imports `build_app` in each worker (ACE-048). Multi-worker is safe: session state is stateless
    # JWT + Postgres, boot migrations are guarded by a pg advisory lock (store.run_migrations), and the admin
    # seed tolerates a concurrent-boot race (lifespan). WORKERS defaults to 1 (unchanged single-process behaviour).
    workers = int(os.environ.get("WORKERS", "1"))
    uvicorn.run("mcp_http:build_app", factory=True, host=host, port=port, workers=workers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
