"""The four port Protocols — the seams adapters plug into.

agami-core keeps one MCP implementation across deployments; deployment-specific behavior is
swapped at the composition root through these ports, never by forking a tool:

  - ``ActivitySink``     — where query-execution records go (file by default)
  - ``OrgResolver``      — single vs multi tenancy as a config flag, not a schema fork
  - ``AuthProvider``     — bearer token → principal (presence by default)
  - ``Executor``         — the connect-and-run step, *behind* the shared guard (built-in by default;
                           a consumer injects a pooled/RBAC/tunnel executor without forking the guard)

These are **interfaces only** — `typing.Protocol`, so an adapter satisfies a port by shape, with
no import coupling back to core. The OSS default adapters live in ``oss_adapters`` (so the local
product runs out of the box); a downstream consumer supplies its own.

The seam value types (``Org`` / ``Principal``) are stdlib dataclasses, not
pydantic models, so this module imports with **zero dependencies** — a consumer can depend on the
seams without pulling the model deps. The wire shapes that need validation (the 4-tool I/O) live
in ``contracts`` (pydantic). Each type is kept minimal — only what a default adapter or a
consumer needs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    # Only for type-checkers — kept out of the runtime import graph so the Protocols (and a
    # consumer that needs only the seams) import without the pydantic model deps. With
    # `from __future__ import annotations` the method annotations are lazy strings, and
    # @runtime_checkable only checks method *names*, so isinstance() works without these.
    from contracts import QueryExecutionRecord

    # ``ExecResult`` is defined in ``execute_sql`` (not here): it is the executor's result type and
    # ``execute_sql`` ships in the stdlib-lean plugin mirror that does NOT include this module, so it
    # cannot import ``ports`` at runtime. Referencing it under TYPE_CHECKING keeps the ``Executor``
    # annotation resolvable for type-checkers without a runtime import cycle.
    from execute_sql import ExecResult

# ---------------------------------------------------------------------------
# Seam value types (minimal — a consumer extends them when it needs more)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Org:
    """A resolved organization. The model hierarchy is org → datasource → subject-area; the
    default deployment is single-tenant (one Org)."""

    id: str
    name: str | None = None


@dataclass(frozen=True)
class Principal:
    """An authenticated caller. By default this is just token presence (one user); real
    providers populate identity/claims.

    `session_id` distinguishes CONCURRENT CLIENTS of the same subject — two windows under one login are
    otherwise indistinguishable here, so anything the server remembers per-caller (an active selection, a
    per-conversation binding) would be shared between them, last write wins. Optional because most auth
    paths have no such notion: presence auth, a hand-minted bearer, and a username/password check all
    leave it None, and a consumer must read it as "no session", never as an error. It is an identity, not
    a liveness signal — see `oauth_server._session_id`.
    """

    subject: str
    session_id: str | None = None


# ---------------------------------------------------------------------------
# The four ports
# ---------------------------------------------------------------------------


@runtime_checkable
class ActivitySink(Protocol):
    """Sink for runtime activity, written through the single ``execute_sql`` chokepoint.

    OSS default = the file/jsonl writer (keeps the local skill working). Record shape is the
    local log record (``contracts.QueryExecutionRecord``)."""

    def record_query_execution(self, record: QueryExecutionRecord) -> None: ...


@runtime_checkable
class OrgResolver(Protocol):
    """Resolve the calling context to an ``Org`` — the single/multi-tenant seam.

    OSS default = single-tenant (returns the one configured org). An implementation that *can* fail to
    place a caller raises ``PermissionError`` to refuse — the server turns that into a 403, so a
    resolver never has to choose between a wrong default and a 500."""

    def resolve_org(self, ctx: object | None = None) -> Org: ...


@runtime_checkable
class AuthProvider(Protocol):
    """Validate a bearer token → ``Principal`` (or ``None`` if invalid).

    OSS default = presence only (enough for a token-gated server); real providers come later."""

    def validate_token(self, token: str) -> Principal | None: ...


@runtime_checkable
class Executor(Protocol):
    """Connect to a datasource and run **already-vetted** SQL — the only swappable part of the
    execution path. It runs *inside* the guarded envelope (guard → executor → shape/log), so it
    **only ever receives SQL the guard already passed**, never raw user input; it does no guarding,
    logging, or governance itself. This is the seam a hosted consumer overrides to supply a
    pooled / per-user-RBAC / SSH-tunnel executor **behind agami-core's one guard** — no fork.

    ``profile`` is the datasource identity a pooling executor keys its reused connection on. The
    built-in OSS default (subprocess/direct connect-per-query) implements this same shape, so a
    plain deploy is unchanged.

    Returns:
        An ``ExecResult``. Returning ``None`` (or anything else) is a contract violation and is
        reported to the caller as ``failed`` / ``other`` — never as a successful empty result.

    Raises:
        ``execute_sql.ExecutorError`` for a connect / credential / driver / run failure. That is the
        classified channel: its ``code`` picks the ``Failure.kind`` the caller sees and its ``msg``
        is relayed, so an adapter that wants a specific kind and a useful message raises this.

        Any **other** exception is caught by ``execute_guarded`` and becomes ``failed`` / ``other``
        with a generic, value-free message; the raw text and stack go to the server log only. So a
        pooled executor's own ``PoolError`` is safe to raise — it will not escape the chokepoint and
        it will not skip the audit row — but it will not reach the caller either. Prefer
        ``ExecutorError``."""

    def execute(self, vetted_sql: str, creds: dict[str, str], *, profile: str) -> ExecResult: ...


# ---------------------------------------------------------------------------
# Composition-root container — the adapters passed as one argument
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Adapters:
    """The port adapters, bundled so ``mcp_http.create_app`` takes them as one argument.

    A consumer builds this with its own implementations of the ports (its own ``OrgResolver``,
    ``AuthProvider``, ``ActivitySink``, and optionally an ``Executor``);
    passing ``adapters=None`` to ``create_app`` uses the OSS defaults (``mcp_http.default_adapters``).
    Today ``create_app`` wires ``auth_provider`` + ``org_resolver`` into the request path;
    ``activity_sink`` is carried here for consumers and not yet referenced by a core call site.

    ``executor`` is optional and defaults to ``None`` — meaning "use the built-in executor" (the
    subprocess/direct connect-per-query path, byte-identical to today). A consumer sets it to run
    execution in-process behind the shared guard (see ``tools.tool_execute_sql``)."""

    activity_sink: ActivitySink
    org_resolver: OrgResolver
    auth_provider: AuthProvider
    executor: Executor | None = None
    # `tool_visibility(tool_name) -> bool` narrows the ADVERTISED tool surface per request. None (the
    # default) is exactly today's behaviour — the whole registry, listed and callable. A registry
    # assembled once at composition time cannot answer "may THIS caller see this tool", and for a
    # consumer running a server-side agent that gap is the difference between a control and a
    # suggestion: the model chooses what to call, so withholding the tool is the control. Subtractive
    # only — it filters the one shared registry and never adds or reshapes a tool. See
    # `mcp_http.build_server` for where it is applied (both list AND call, deliberately).
    tool_visibility: Callable[[str], bool] | None = None
