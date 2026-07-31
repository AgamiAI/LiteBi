"""The admin web surface — onboard/enable/disable/list users, plus the friendly browser landings.

Two auth surfaces live in this server: the MCP bearer JWT (claude.ai) and — here — a browser
**session cookie** for the human admin. `/admin/*` is session-gated (the admin-gate = the
env-configured `AGAMI_ADMIN_USERNAME`); a non-admin, even with valid credentials, can't get in. This
module also renders the friendly landings a human sees if they point a browser at the server.

The page builders (`*_html`) are split from the request handlers so previews can render them with
sample values. Every interpolated value goes through `ui.esc` (these pages show emails/names).
"""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import jwt
import ui
import user_store
from async_offload import run_blocking

# Reuse the OAuth provider's shared HS256 secret accessor + store opener + the admin OIDC-start handler
# so the admin surface signs with the same key, reads the same datastore, and runs the same hardened
# OIDC flow (no second source of truth).
from oauth_server import _open_store, _signing_secret, admin_oidc_start
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response


def _full_name(user: dict[str, Any]) -> str:
    """A user's display name from first/last; falls back to the email's local part when unnamed."""
    name = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p).strip()
    return name or (user.get("email") or "").split("@")[0]


# ---------------------------------------------------------------------------
# Auth pages
# ---------------------------------------------------------------------------


def admin_login_body_html(error: str = "", provider: str | None = None) -> str:
    """The admin sign-in page: the admin's pinned social provider (when configured) above the password
    form — matching the MCP login's options. Only the admin's *one* pinned provider is offered, since
    that's the only one that can resolve to the admin (a second button would just say "not an admin").
    No banner copy — the logo and the form speak for themselves (this is the admin's own login)."""
    button = (
        ui.provider_button(provider, f"/admin/oidc/start?provider={provider}") if provider else ""
    )
    social = f'<div class="providers">{button}</div><div class="divider">or</div>' if button else ""
    alert = f'<div class="alert error">{ui.esc(error)}</div>' if error else ""
    body = f"""{alert}{social}
<form method="post">
<label for="u">Email</label>
<input id="u" name="username" type="email" autocomplete="email" placeholder="you@example.com">
<label for="p">Password</label>
<input id="p" name="password" type="password" autocomplete="current-password" placeholder="••••••••">
<button class="btn" type="submit" style="margin-top:22px">Sign in</button>
</form>"""
    return ui.auth_page("Admin sign in", body)


# ---------------------------------------------------------------------------
# Admin console — Users tab (+ Dashboard / Activity placeholders)
# ---------------------------------------------------------------------------


def _status_pill(status: str) -> str:
    cls = "active" if status == "active" else "disabled"
    return f'<span class="pill {cls}">{ui.esc(status)}</span>'


def _row_action(user: dict[str, Any], csrf: str, admin_username: str) -> str:
    # The admin can't disable their own account (it would lock themselves out). Keyed on username —
    # the stable identity column the status update writes against (email is display-only).
    if user.get("username") == admin_username:
        return '<span class="muted">—</span>'
    active = user["status"] == "active"
    target, label, cls = (
        ("disabled", "Disable", "danger") if active else ("active", "Enable", "secondary")
    )
    return (
        '<form method="post" action="/admin/users/status" style="display:inline">'
        f'<input type="hidden" name="csrf" value="{ui.esc(csrf)}">'
        f'<input type="hidden" name="username" value="{ui.esc(user.get("username"))}">'
        f'<input type="hidden" name="status" value="{target}">'
        f'<button class="btn tiny {cls}" type="submit">{label}</button></form>'
    )


def _add_user_drawer(csrf: str) -> str:
    """A CSS-only right-side drawer (no JS) with a minimal 'Add user' form.

    Just email + first/last name: the teammate picks their own sign-in method (Google, Microsoft, or
    a password they set themselves) the first time they sign in — the admin never sets a password."""
    return f"""<input type="checkbox" id="add-user" class="drawer-toggle">
<div class="drawer-wrap">
<label for="add-user" class="drawer-backdrop"></label>
<aside class="drawer">
<div class="drawer-head"><h1 style="font-size:17px">Add user</h1>
<label for="add-user" class="drawer-x" aria-label="Close">&times;</label></div>
<p class="sub" style="margin-bottom:8px">They'll sign in with this email — through Google, Microsoft,
or a password they set the first time — and can then use this agami server from Claude.</p>
<form method="post" action="/admin/users">
<input type="hidden" name="csrf" value="{ui.esc(csrf)}">
<label for="d-email">Email</label>
<input id="d-email" name="email" type="email" placeholder="you@example.com">
<label for="d-first">First name</label>
<input id="d-first" name="first_name" type="text" placeholder="Jordan">
<label for="d-last">Last name</label>
<input id="d-last" name="last_name" type="text" placeholder="Lee">
<button class="btn" type="submit" style="margin-top:22px">Add user</button>
</form>
</aside>
</div>"""


def _signin_cell(user: dict[str, Any], setup_links: dict[str, str]) -> str:
    """The Sign-in column: the user's method, plus — for a *pending* user in a password deployment —
    a copy-able setup link the admin shares out-of-band (the page is session-gated, admin-only)."""
    sign_in = user.get("oidc_provider") or (
        "password" if user.get("has_password") else "not set yet"
    )
    link = setup_links.get(user.get("username", ""))
    extra = (
        f'<details class="setup"><summary>Setup link</summary>'
        f'<input class="code" readonly value="{ui.esc(link)}" style="width:100%;margin-top:6px">'
        f"</details>"
        if link
        else ""
    )
    return f'<td class="muted">{ui.esc(sign_in)}{extra}</td>'


def users_tab_html(
    users: list[dict[str, Any]],
    csrf: str,
    *,
    admin_username: str = "",
    admin_email: str = "",
    admin_label: str = "",
    setup_links: dict[str, str] | None = None,
    error: str = "",
    ok: str = "",
) -> str:
    """The Users tab: a roster table + an 'Add user' button that opens the drawer. `setup_links`
    (username → URL) attaches a copy-able setup link to each pending row (password deployments)."""
    setup_links = setup_links or {}
    rows = ""
    for u in users:
        rows += (
            "<tr>"
            f"<td><strong>{ui.esc(_full_name(u))}</strong></td>"
            f'<td class="muted">{ui.esc(u.get("email") or "—")}</td>'
            f"{_signin_cell(u, setup_links)}"
            f"<td>{_status_pill(u['status'])}</td>"
            f'<td style="text-align:right">{_row_action(u, csrf, admin_username)}</td>'
            "</tr>"
        )
    alerts = (f'<div class="alert ok">{ui.esc(ok)}</div>' if ok else "") + (
        f'<div class="alert error">{ui.esc(error)}</div>' if error else ""
    )
    panel = f"""{alerts}
<div class="row" style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
<p class="muted" style="margin:0">People who can use this agami server.</p>
<label for="add-user" class="btn tiny">+ Add user</label>
</div>
<div class="table-wrap"><table>
<thead><tr><th>Name</th><th>Email</th><th>Sign-in</th><th>Status</th><th></th></tr></thead>
<tbody>{rows}</tbody>
</table></div>"""
    return ui.admin_shell(
        "Users · agami admin",
        "users",
        panel,
        admin_label=admin_label or admin_username,
        admin_email=admin_email,
        extra=_add_user_drawer(csrf),
    )


def _coming_soon(tab: str, label: str, *, admin_label: str = "", admin_email: str = "") -> str:
    panel = f'<div class="empty"><strong>{ui.esc(label)}</strong><br>Coming soon.</div>'
    return ui.admin_shell(
        f"{label} · agami admin", tab, panel, admin_label=admin_label, admin_email=admin_email
    )


def dashboard_tab_html(*, admin_label: str = "", admin_email: str = "") -> str:
    return _coming_soon("dashboard", "Dashboard", admin_label=admin_label, admin_email=admin_email)


# ---------------------------------------------------------------------------
# Activity view — every tool call, folded into its conversation (thread ▸ turn ▸ call)
# ---------------------------------------------------------------------------


def _ok_pill(success: Any) -> str:
    ok = bool(success)
    return f'<span class="pill {"active" if ok else "disabled"}">{"ok" if ok else "error"}</span>'


def _utc(ts: str | None) -> str:
    """A <time> the browser-local script (ui._doc) renders in the viewer's zone; the UTC value is the
    fallback text for no-JS."""
    return f'<time data-utc="{ui.esc(ts)}">{ui.esc(ts)}</time>'


def _call_card(c: dict[str, Any]) -> str:
    """One call inside a turn. A **query** call (has `sql`) shows its agent-framing + SQL; a **non-query**
    call (list_datasources, get_datasource_schema, …) has no SQL, so it shows its tool name — so every call
    in the conversation is visible, not just the queries. Every card's meta line carries the call's OWN
    datasource (a turn can span datasources — a cross-datasource question runs one execute_sql each — so
    per-call attribution matters). SQL/framing are self-reported + attacker-influenceable → escaped; rows
    shown only when the call recorded a count."""
    if c.get("sql"):
        head = (
            f'<div class="muted" style="margin:0 0 6px">↳ {ui.esc(c["agent_query"])} '
            f'<span class="muted">· agent-reported</span></div>'
            if c.get("agent_query")
            else ""
        )
        body = (
            f'<pre class="code" style="white-space:pre-wrap;padding:12px;display:block;margin-top:4px">'
            f"{ui.esc(c['sql'])}</pre>"
        )
    else:
        head = (
            '<div style="margin:0 0 6px">'
            f'<span class="pill" style="background:var(--chip);color:var(--ink)">{ui.esc(c["tool_name"])}</span>'
            "</div>"
        )
        body = ""
    ds = ui.esc(c.get("datasource") or "—")
    lat = (str(c["execution_ms"]) + " ms") if c.get("execution_ms") is not None else ""
    rows_bit = f" · {c['row_count']} rows" if c.get("row_count") is not None else ""
    return (
        '<div style="border-top:1px solid var(--line);padding:9px 0 11px">'
        f"{head}{body}"
        f'<div class="muted" style="font-size:13px;margin-top:6px">{_utc(c["ts"])} · {ds} · '
        f"{lat} {_ok_pill(c['success'])}{rows_bit}</div></div>"
    )


def _session_drawer(s: dict[str, Any], idx: int) -> str:
    sid = f"sess-{idx}"  # DOM id is the row index, never the (self-reported, attacker-influenceable) key
    # Render the conversation as **turns** (one user question -> the N calls answering it), grouped on
    # correlation_id by list_sessions. The turn header shows the verbatim question once; each call (a
    # query's SQL, or a non-query tool's name) lists beneath it. Degrades cleanly: a call with no
    # correlation_id is its own one-call turn, so this reads like a flat list when Claude doesn't self-report.
    cards = ""
    for t in s["turns"]:
        question = t.get("question") or "(no question reported)"
        n = len(t["calls"])
        call_cards = "".join(_call_card(c) for c in t["calls"])
        # Provenance on the question. Where it was copied out of the tool arguments the model wrote it is
        # a self-report (best-effort, attacker-influenceable) and is marked so, like the rest of the
        # activity log; where the caller observed the question directly the marker would *understate* the
        # trust, so it is omitted. `_group_turns` decides, and errs toward keeping the marker.
        marker = (
            ' <span class="muted" style="font-size:13px">· self-reported</span>'
            if t.get("question_self_reported", True)
            else ""
        )
        asked = (
            f'<span class="muted">User asked</span> <strong>{ui.esc(question)}</strong>{marker}'
            if t.get("question")
            else f'<strong class="muted">{ui.esc(question)}</strong>'
        )
        cards += (
            '<div style="border-top:2px solid var(--line);padding:14px 0 2px;margin-top:8px">'
            f'<div style="margin-bottom:4px">{asked} '
            f'<span class="muted" style="font-size:13px">· {n} {"call" if n == 1 else "calls"}</span>'
            f"</div>{call_cards}</div>"
        )
    return f"""<input type="checkbox" id="{sid}" class="drawer-toggle">
<div class="drawer-wrap"><label for="{sid}" class="drawer-backdrop"></label>
<aside class="drawer" style="width:560px">
<div class="drawer-head"><h1 style="font-size:17px">Conversation</h1>
<label for="{sid}" class="drawer-x" aria-label="Close">&times;</label></div>
<p class="sub" style="margin-bottom:8px">{ui.esc(s.get("actor") or "—")} · {ui.esc(", ".join(s["datasources"]) or "—")} · {s["call_count"]} calls · started {_utc(s["started"])}</p>
{cards}</aside></div>"""


def activity_tab_html(
    sessions: list[dict[str, Any]] | None = None, *, admin_label: str = "", admin_email: str = ""
) -> str:
    """The Activity tab: **every** tool call grouped into conversations (best-effort via the self-reported
    `thread_id`; ungrouped singletons otherwise — so it stays audit-complete). Each row opens to the
    conversation's turns, and within them every call (query or not)."""
    sessions = sessions or []
    body_rows, drawers = "", ""
    for i, s in enumerate(sessions):
        body_rows += (
            "<tr>"
            f'<td><label for="sess-{i}" style="cursor:pointer;color:var(--brand)">{_utc(s["started"])}</label></td>'
            f"<td><strong>{ui.esc(s.get('actor') or '—')}</strong></td>"
            f'<td class="muted">{ui.esc(", ".join(s["datasources"]) or "—")}</td>'
            f'<td class="muted">{s["call_count"]}</td>'
            f'<td class="muted">{s["error_count"] or "—"}</td>'
            f'<td class="muted">{(str(s["avg_ms"]) + " ms") if s.get("avg_ms") is not None else "—"}</td>'
            f"<td>{_utc(s['last_activity'])}</td>"
            f'<td style="text-align:right"><label for="sess-{i}" class="btn tiny secondary">Open</label></td>'
            "</tr>"
        )
        drawers += _session_drawer(s, i)
    empty = '<p class="muted">No activity yet.</p>' if not sessions else ""
    panel = f"""{empty}
<div class="table-wrap"><table>
<thead><tr><th>Started</th><th>User</th><th>Datasources</th><th>Calls</th><th>Errors</th><th>Avg time</th><th>Last activity</th><th></th></tr></thead>
<tbody>{body_rows}</tbody></table></div>"""
    return ui.admin_shell(
        "Activity · agami admin",
        "activity",
        panel,
        admin_label=admin_label,
        admin_email=admin_email,
        extra=drawers,
    )


# ---------------------------------------------------------------------------
# Friendly browser landings
# ---------------------------------------------------------------------------


def _connect_block(base_url: str) -> str:
    return (
        '<p class="small" style="margin-bottom:6px">Add this server to Claude as a custom connector:</p>'
        f'<p><span class="code">{ui.esc(base_url)}/mcp</span></p>'
    )


def landing_body_html(base_url: str) -> str:
    """The root page a human lands on if they open the server URL in a browser."""
    body = f"""<div class="consent"><p class="who">agami</p>
<p class="small">A governed, self-hosted data agent for Claude.</p></div>
{_connect_block(base_url)}
<p class="foot"><a href="/admin">Admin sign in →</a></p>"""
    return ui.auth_page("agami", body)


def not_admin_body_html(base_url: str) -> str:
    """Shown when a valid but non-admin user signs in at /admin/login."""
    body = f"""<div class="consent"><p class="who">You're signed in</p>
<p class="small">This account isn't an administrator.</p></div>
{_connect_block(base_url)}
<p class="foot muted">Only the administrator can manage users here.</p>"""
    return ui.auth_page("Signed in", body)


def not_authorized_body_html(email: str) -> str:
    """The branded "your identity is real but no admin has added you" page for an un-onboarded
    Google/Microsoft sign-in. Not yet wired into the OIDC rejection (which still returns a JSON OAuth
    error); rendered in previews and ready for the self-onboarding flow to adopt. No connector hint —
    they can't use it yet."""
    body = f"""<div class="consent"><p class="who">Not set up yet</p>
<p class="small">{ui.esc(email)} isn't authorized for this agami server.</p></div>
<p class="foot muted">Ask the administrator to add you, then sign in again.</p>"""
    return ui.auth_page("Not authorized", body)


def mcp_landing_body_html(base_url: str) -> str:
    """The branded body returned when a *browser* hits /mcp (a machine endpoint) unauthenticated."""
    body = f"""<div class="consent"><p class="who">This is an MCP endpoint</p>
<p class="small">It's meant for Claude, not a browser.</p></div>
{_connect_block(base_url)}
<p class="foot"><a href="/admin">Admin sign in →</a></p>"""
    return ui.auth_page("agami · MCP endpoint", body)


# ---------------------------------------------------------------------------
# Session auth — a browser session cookie (separate from the MCP bearer JWT)
# ---------------------------------------------------------------------------
#
# `/admin/*` is gated by a signed session cookie, NOT the MCP bearer token. The two share the HS256
# signing secret + algorithm, so the `purpose` claim keeps them from being interchangeable: a token
# minted for the query surface (`issue_jwt`, which carries no `purpose`) must never satisfy the admin
# gate — even for the admin's own user. The gate also requires `sub == AGAMI_ADMIN_USERNAME`, so a
# valid non-admin can never hold an admin session.

_SESSION_COOKIE = "agami_admin_session"
_SESSION_TTL = timedelta(hours=12)
_SESSION_PURPOSE = "admin_session"


def _admin_username() -> str | None:
    """The single admin's username = the **normalized** admin email (the admin-gate). Lowercased+trimmed
    to match how the seed stores it + how OIDC resolves it, so the gate is case-insensitive. Unset ⇒ the
    admin UI is disabled entirely."""
    name = os.environ.get("AGAMI_ADMIN_USERNAME", "").strip().lower()
    return name or None


def _admin_provider() -> str | None:
    """The admin's pinned OIDC provider (`AGAMI_ADMIN_PROVIDER`), or None — only when it's a provider
    that's actually configured (client id/secret present)."""
    key = os.environ.get("AGAMI_ADMIN_PROVIDER", "").strip().lower()
    if not key:
        return None
    import oidc  # lazy: the egress module, server-only

    return key if key in oidc.available_providers() else None


def _admin_login_provider() -> str | None:
    """The provider button to render on the admin login: the pinned, configured provider, but ONLY
    when the admin's stored row is actually bound to it. This avoids a dead button — e.g. if
    `AGAMI_ADMIN_PROVIDER` is set after the admin was seeded password-only (the seed is idempotent and
    won't backfill `oidc_provider`), the button would otherwise show but dead-end at "not an admin"."""
    provider = _admin_provider()
    admin = _admin_username()
    if provider is None or admin is None:
        return None
    store = _open_store()
    if store is None:
        return None
    try:
        row = user_store.get_user(store, admin)
    finally:
        store.close()
    return provider if row is not None and row.get("oidc_provider") == provider else None


def issue_session(username: str) -> str:
    """A short-TTL HS256 session JWT (sub=username, purpose=admin_session) for the cookie."""
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": username,
            "purpose": _SESSION_PURPOSE,
            "iat": int(now.timestamp()),
            "exp": int((now + _SESSION_TTL).timestamp()),
        },
        _signing_secret(),
        algorithm="HS256",
    )


def current_admin(request: Request) -> str | None:
    """The signed-in admin's username, or None: verify the cookie (sig + exp + purpose) AND that its
    subject is THE configured admin. Any failure → None (fail closed → redirect to login)."""
    admin = _admin_username()
    token = request.cookies.get(_SESSION_COOKIE)
    if admin is None or not token:
        return None
    try:
        claims = jwt.decode(
            token, _signing_secret(), algorithms=["HS256"], options={"require": ["exp", "sub"]}
        )
    except Exception:
        return None
    if claims.get("purpose") != _SESSION_PURPOSE:
        return None
    sub = claims.get("sub")
    if not isinstance(sub, str) or sub != admin:
        return None
    return sub


def _set_session(resp: Response, token: str) -> None:
    # HttpOnly (no JS read) + Secure (HTTPS only — deployments are HTTPS) + SameSite=Lax (not sent on
    # cross-site POSTs); scoped to /admin so it never rides along to /mcp or /static.
    resp.set_cookie(
        _SESSION_COOKIE,
        token,
        max_age=int(_SESSION_TTL.total_seconds()),
        httponly=True,
        secure=True,
        samesite="lax",
        path="/admin",
    )


def _clear_session(resp: Response) -> None:
    resp.delete_cookie(_SESSION_COOKIE, path="/admin")


# CSRF: a token derived from the session cookie via HMAC(secret, cookie). An attacker who can neither
# read the HttpOnly cookie nor know the server secret can't forge it — defense-in-depth over SameSite.
def _csrf_for(session_token: str) -> str:
    return hmac.new(_signing_secret().encode(), session_token.encode(), hashlib.sha256).hexdigest()


def _origin_ok(request: Request) -> bool:
    """A second, independent CSRF gate: if the browser sent an Origin (or Referer) on this POST, its
    scheme+host MUST match our own. Browsers always attach Origin to cross-site POSTs, so a forged
    request from another site is caught here even before the token check. When neither header is
    present (some same-origin form posts, test clients) we don't fail — the signed CSRF token carries
    the load — so this only ever *adds* protection."""
    from urllib.parse import urlsplit

    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return True
    want = urlsplit(_base_url())
    got = urlsplit(origin)
    return (got.scheme, got.hostname, got.port) == (want.scheme, want.hostname, want.port)


def _csrf_ok(request: Request, presented: str) -> bool:
    token = request.cookies.get(_SESSION_COOKIE)
    if not token or not presented:
        return False
    if not _origin_ok(request):
        return False
    return hmac.compare_digest(_csrf_for(token), presented)


# ---------------------------------------------------------------------------
# Request handlers
# ---------------------------------------------------------------------------

# Flash text is server-owned (keyed by a short code in the redirect query) — never echoed user input,
# so a redirect can't be turned into a reflected-content vector.
_OK_FLASH = {"added": "User added.", "enabled": "User enabled.", "disabled": "User disabled."}
_ERR_FLASH = {
    "dup": "A user with that email already exists.",
    "bad_email": "Enter a valid email address.",
    "csrf": "Your session expired — please try again.",
    "self": "You can't change your own status.",
    "bad": "That action isn't allowed.",
    "notfound": "No such user.",
}


async def _form(request: Request) -> dict[str, str]:
    data = await request.form()
    return {k: (v if isinstance(v, str) else "") for k, v in data.items()}


def _base_url() -> str:
    from mcp_http import public_base_url

    return public_base_url()


def _is_integrity_error(exc: Exception) -> bool:
    # Portable across backends (sqlite3.IntegrityError / psycopg2 UniqueViolation) without importing
    # the driver here — a UNIQUE collision is "duplicate", any other error must surface.
    return any(cls.__name__ == "IntegrityError" for cls in type(exc).__mro__)


def _admin_chrome(store: Any, admin_username: str) -> dict[str, str]:
    """Avatar label + email for the top-bar account menu (from the admin's own user row)."""
    row = user_store.get_user(store, admin_username) if store is not None else None
    return {
        "admin_label": _full_name(row) if row else admin_username,
        "admin_email": (row or {}).get("email") or "",
    }


def complete_admin_oidc_login(username: str | None) -> Response:
    """Finish an admin OIDC login (called by the shared OIDC callback for an `admin_login` state): a
    verified identity that resolves to THE configured admin gets an admin session; anyone else — an
    unresolved identity, or a valid but non-admin user — is refused with the branded page, no session.
    The provider-pin + subject binding were already enforced upstream by `_resolve_oidc_user`."""
    if username is not None and username == _admin_username():
        resp: Response = RedirectResponse("/admin", status_code=302)
        _set_session(resp, issue_session(username))
        return resp
    return HTMLResponse(not_admin_body_html(_base_url()), status_code=403)


async def admin_login(request: Request) -> Response:
    """GET → the admin sign-in page; POST → authenticate, gate on the admin-username, mint a session."""
    if request.method == "GET":
        if current_admin(request) is not None:
            return RedirectResponse("/admin", status_code=302)
        return HTMLResponse(admin_login_body_html(provider=_admin_login_provider()))

    form = await _form(request)
    # Email is the identity: normalize the typed address (trim + lowercase) so login is
    # case-insensitive and matches the normalized username the seed stored.
    typed = form.get("username", "").strip().lower()
    # The whole credential check (DB reads + the ~50-100 ms argon2 verify) runs off the event loop in a worker
    # thread with its OWN Store, so an admin login never freezes concurrent requests and no loop-thread Store is
    # shared across threads (ACE-048). No datastore configured → None → the generic "invalid" re-render below.
    principal = await run_blocking(
        user_store.authenticate_with_own_store, typed, form.get("password", "")
    )
    if principal is None:
        # Same generic message for wrong password, unknown user, or disabled — no enumeration oracle.
        # Keep the social button on the re-render so a failed password attempt doesn't hide it.
        return HTMLResponse(
            admin_login_body_html(
                error="Invalid email or password.", provider=_admin_login_provider()
            ),
            status_code=401,
        )
    if principal.subject != _admin_username():
        # Valid credentials, but not THE admin: no session minted; a friendly "use via Claude" page.
        return HTMLResponse(not_admin_body_html(_base_url()), status_code=403)
    resp = RedirectResponse("/admin", status_code=302)
    _set_session(resp, issue_session(principal.subject))
    return resp


async def admin_logout(request: Request) -> Response:
    resp = RedirectResponse("/admin/login", status_code=302)
    _clear_session(resp)
    return resp


async def admin_home(request: Request) -> Response:
    """The console. `?tab=` picks Dashboard / Users (default) / Activity. Session-gated."""
    import model_store
    import tools

    admin = current_admin(request)
    if admin is None:
        return RedirectResponse("/admin/login", status_code=302)
    store = _open_store()
    try:
        chrome = _admin_chrome(store, admin)
        tab = request.query_params.get("tab", "users")
        if tab == "dashboard":
            return HTMLResponse(dashboard_tab_html(**chrome))
        if tab == "activity":
            # Admin requests skip the bearer middleware, so no request-org is set — this falls back to
            # AGAMI_ORG_ID/'local' (the operator's own org). A cross-tenant operator view is REQ-014.
            org_id = tools.current_org_id()
            sessions = model_store.list_sessions(store, org_id=org_id) if store is not None else []
            return HTMLResponse(activity_tab_html(sessions, **chrome))
        users = user_store.list_users(store) if store is not None else []
    finally:
        if store is not None:
            store.close()
    # current_admin already proved the cookie is present + valid; .get (not bracket) keeps a malformed
    # duplicate-cookie header from turning into an unhandled 500.
    csrf = _csrf_for(request.cookies.get(_SESSION_COOKIE, ""))
    ok = _OK_FLASH.get(request.query_params.get("ok", ""), "")
    err = _ERR_FLASH.get(request.query_params.get("err", ""), "")
    return HTMLResponse(
        users_tab_html(
            users,
            csrf,
            admin_username=admin,
            setup_links=_setup_links(users),
            ok=ok,
            error=err,
            **chrome,
        )
    )


def _setup_links(users: list[dict[str, Any]]) -> dict[str, str]:
    """Per-pending-user setup links — but only in a **password** deployment (no OIDC configured). When
    an OIDC provider is configured, teammates onboard by signing in with it, so no link is offered."""
    import oidc  # lazy: the egress module, server-only

    if oidc.available_providers():
        return {}
    import onboarding

    base = _base_url()
    return {
        u["username"]: f"{base}/claim?token={onboarding.mint_setup_token(u['username'])}"
        for u in users
        if onboarding.is_pending(u)
    }


def _valid_email(email: str) -> bool:
    # A deliberately loose check — we're not validating deliverability, just rejecting obvious junk
    # before it becomes a username. Real verification happens when the user first signs in (a later
    # self-onboarding step).
    email = email.strip()
    return "@" in email and "." in email.rsplit("@", 1)[-1] and " " not in email


async def admin_create_user(request: Request) -> Response:
    """Onboard a teammate: a *pending* user (no password, no provider) keyed by their email. They pick
    their sign-in method on first login (a later self-onboarding step). Admin-gated + CSRF-checked."""
    admin = current_admin(request)
    if admin is None:
        return RedirectResponse("/admin/login", status_code=302)
    form = await _form(request)
    if not _csrf_ok(request, form.get("csrf", "")):
        return RedirectResponse("/admin?err=csrf", status_code=302)
    email = (form.get("email") or "").strip()
    if not _valid_email(email):
        return RedirectResponse("/admin?err=bad_email", status_code=302)
    store = _open_store()
    if store is None:
        return RedirectResponse("/admin?err=bad", status_code=302)
    try:
        # Username == the normalized email, so the teammate signs in with the address they know.
        normalized = email.lower()
        user_store.create_user(
            store,
            username=normalized,
            email=normalized,
            first_name=form.get("first_name", ""),
            last_name=form.get("last_name", ""),
            password=None,
        )
    except Exception as exc:
        # A UNIQUE collision is a duplicate → flash, not a 500. Closing the store in `finally` rolls
        # back the failed INSERT so its write lock doesn't strand the connection.
        if _is_integrity_error(exc):
            return RedirectResponse("/admin?err=dup", status_code=302)
        raise
    finally:
        store.close()
    return RedirectResponse("/admin?ok=added", status_code=302)


async def admin_set_status(request: Request) -> Response:
    """Enable/disable a user (the existing active/disabled status flag). Admin-gated + CSRF-checked;
    can't disable self."""
    admin = current_admin(request)
    if admin is None:
        return RedirectResponse("/admin/login", status_code=302)
    form = await _form(request)
    if not _csrf_ok(request, form.get("csrf", "")):
        return RedirectResponse("/admin?err=csrf", status_code=302)
    username = form.get("username", "")
    status = form.get("status", "")
    if status not in ("active", "disabled"):
        return RedirectResponse("/admin?err=bad", status_code=302)
    if username == admin:
        return RedirectResponse("/admin?err=self", status_code=302)
    store = _open_store()
    if store is None:
        # Mirror create: a missing datastore is an error, not a silent "done" (no false success flash).
        return RedirectResponse("/admin?err=bad", status_code=302)
    try:
        changed = user_store.set_status(store, username, status)
    finally:
        store.close()
    if not changed:
        # The username matched no row (e.g. a stale form) — don't flash a success for a no-op.
        return RedirectResponse("/admin?err=notfound", status_code=302)
    return RedirectResponse(
        f"/admin?ok={'disabled' if status == 'disabled' else 'enabled'}", status_code=302
    )


# ---------------------------------------------------------------------------
# Admin console — Model tab (read-only model explorer)
#
# A pure projection of the served model (`model_store.load_datasource`) + the domain docs
# (`load_memory`) — the SAME tree every MCP tool reads, so there is zero drift and no second store.
# Read-only by construction: the only route is a GET (see `routes()`), there is no write path, and
# `storage_connections[].storage_config` (hosts/credentials) is NEVER rendered. The catalog idiom (a
# browse tree + one page at a time) keeps a wide model — real tables run to dozens of columns —
# legible. Builders are split from the handler so previews can render them with sample data.
# ---------------------------------------------------------------------------

# Trust posture is surfaced as an honest read-only badge (agami's differentiator: the model says how
# much to trust each piece), never as a clickable review control — that editor is Hosted.
_CONF_LABEL = {"confirmed": "✓ confirmed", "inferred": "~ inferred", "proposed": "⋯ proposed"}


def _conf_badge(confidence: str | None) -> str:
    c = (confidence or "").lower()
    if c not in _CONF_LABEL:
        return ""
    return f'<span class="badge b-{c}">{_CONF_LABEL[c]}</span>'


def _human_count(n: int | None) -> str:
    """A compact row-count, e.g. 6591 -> '≈ 6.6k', 612000 -> '≈ 612k'. None -> '' (unknown)."""
    if n is None:
        return ""
    if n < 1000:
        return f"≈ {n}"
    if n < 1_000_000:
        return f"≈ {n / 1000:.1f}k".replace(".0k", "k")
    return f"≈ {n / 1_000_000:.1f}M".replace(".0M", "M")


def _model_url(
    datasource: str, *, area: str | None = None, table: str | None = None, view: str | None = None
) -> str:
    """An attribute-safe `/admin/model` href. Values are %-encoded (names may contain spaces); the
    `&` separators are written as `&amp;` so the whole string is safe in an HTML attribute."""
    parts = [f"datasource={quote(datasource)}"]
    if area is not None:
        parts.append(f"area={quote(area)}")
    if table is not None:
        parts.append(f"table={quote(table)}")
    if view is not None:
        parts.append(f"view={quote(view)}")
    return "/admin/model?" + "&amp;".join(parts)


def _area_nav_html(
    a: Any, datasource: str, active_area: str | None, active_table: str | None
) -> str:
    """One area node; when it's the active area it expands into links to its tables."""
    head = (
        f'<a class="navitem{" active" if a.name == active_area else ""}" '
        f'href="{_model_url(datasource, area=a.name)}">{ui.esc(a.name)} '
        f'<span class="n">{len(a.tables_defined)}</span></a>'
    )
    if a.name != active_area:
        return head
    leaves = "".join(
        f'<a class="leaf{" active" if t.name == active_table else ""}" '
        f'href="{_model_url(datasource, area=a.name, table=t.name)}">{ui.esc(t.name)}</a>'
        for t in a.tables_defined
    )
    return head + f'<div class="children">{leaves}</div>' if leaves else head


def _model_tree_html(
    org: Any,
    datasource: str,
    datasources: list[str],
    *,
    active_area: str | None = None,
    active_table: str | None = None,
    active_view: str | None = None,
) -> str:
    """The left browse rail: a datasource picker (only when more than one is served), an Overview
    link, the subject areas (the active one expands to its tables), and the Domain-context node."""
    if len(datasources) > 1:
        opts = "".join(
            f'<option value="{ui.esc(d)}"{" selected" if d == datasource else ""}>{ui.esc(d)}'
            "</option>"
            for d in datasources
        )
        # onchange auto-submits for JS users; the Go button is the no-JS fallback (the rest of the
        # admin UI is JS-free, so the picker keeps a working control without JavaScript too).
        picker = (
            '<form class="ds" method="get" action="/admin/model">'
            '<span class="muted">Datasource</span>'
            f'<select name="datasource" onchange="this.form.submit()">{opts}</select>'
            '<button type="submit" class="ds-go">Go</button></form>'
        )
    else:
        picker = (
            f'<div class="ds"><span class="muted">Datasource</span>'
            f"<b>{ui.esc(datasource)}</b></div>"
        )
    overview_cls = "navitem" + (" active" if active_area is None and active_view is None else "")
    areas = "".join(
        _area_nav_html(a, datasource, active_area, active_table) for a in org.subject_areas
    )
    # The cross-area Relationships node only appears when the model actually has org-level
    # (cross-subject-area) relationships — no dead node for a single-area model.
    rel_node = ""
    if org.cross_subject_area_relationships:
        rel_cls = "navitem" + (" active" if active_view == "relationships" else "")
        rel_node = (
            f'<a class="{rel_cls}" href="{_model_url(datasource, view="relationships")}">'
            f'Relationships <span class="n">{len(org.cross_subject_area_relationships)}</span></a>'
        )
    context_cls = "navitem" + (" active" if active_view == "context" else "")
    return (
        f'<aside class="tree">{picker}'
        f'<a class="{overview_cls}" href="{_model_url(datasource)}">Overview</a>'
        f"<h4>Subject areas</h4>{areas}"
        f"<h4>Browse</h4>{rel_node}"
        f'<a class="{context_cls}" href="{_model_url(datasource, view="context")}">Domain context</a>'
        "</aside>"
    )


def _model_shell(content: str, tree: str, *, admin_label: str = "", admin_email: str = "") -> str:
    """Wrap the browse tree + a content pane in the admin shell with the Model tab active."""
    body = f'<div class="explorer">{tree}<main class="content">{content}</main></div>'
    return ui.admin_shell(
        "Model · agami admin", "model", body, admin_label=admin_label, admin_email=admin_email
    )


def model_empty_html(datasource: str, datasources: list[str], **chrome: str) -> str:
    """The clean state when nothing is deployed yet (no served model rows)."""
    content = (
        '<div class="crumbs">Model</div><h1>Model</h1>'
        '<p class="lead">No model deployed yet. Author your semantic model in Claude with the agami '
        "plugin and deploy it — the served subject areas, tables, and metrics will show up here, "
        "read-only.</p>"
    )
    label = datasource or (datasources[0] if datasources else "")
    tree = (
        f'<aside class="tree"><div class="ds"><span class="muted">Datasource</span>'
        f"<b>{ui.esc(label) or '—'}</b></div></aside>"
    )
    return _model_shell(content, tree, **chrome)


def _glossary_html(key_terminology: dict[str, str]) -> str:
    if not key_terminology:
        return ""
    terms = "".join(
        f'<span class="term"><b>{ui.esc(k)}</b> {ui.esc(v)}</span>'
        for k, v in key_terminology.items()
    )
    return f'<h2 class="sec">Glossary</h2><div class="gloss">{terms}</div>'


def _storage_html(connections: list[Any]) -> str:
    # Names + types only — storage_config (hosts/credentials) is deliberately never rendered.
    if not connections:
        return ""
    rows = "".join(
        f'<span class="term"><b>{ui.esc(c.name)}</b> '
        f"{ui.esc(getattr(c, 'storage_type', '') or '')}</span>"
        for c in connections
    )
    return f'<h2 class="sec">Storage connections</h2><div class="gloss">{rows}</div>'


def model_overview_html(
    org: Any, version: str | None, datasource: str, datasources: list[str], **chrome: str
) -> str:
    """The datasource landing: org header + glossary + storage + the subject-area list."""
    tree = _model_tree_html(org, datasource, datasources)
    table_total = sum(len(a.tables_defined) for a in org.subject_areas)
    ver = ui.esc(version[:8]) if version else f"v{org.version}"
    stats = (
        f'<div class="stat"><div class="k">Subject areas</div>'
        f'<div class="v">{len(org.subject_areas)}</div></div>'
        f'<div class="stat"><div class="k">Tables</div><div class="v">{table_total}</div></div>'
        f'<div class="stat"><div class="k">Version</div>'
        f'<div class="v mono" style="font-size:14px">{ver}</div></div>'
        f'<div class="stat"><div class="k">Fiscal year</div>'
        f'<div class="v" style="font-size:14px">Starts month {org.fiscal_year_start_month}</div></div>'
    )
    areas = "".join(
        f'<a class="trow" href="{_model_url(datasource, area=a.name)}">'
        f'<span class="nm">{ui.esc(a.name)}</span>'
        f'<span class="d">{ui.esc(a.description or "")}</span>'
        f'<span class="meta">{len(a.tables_defined)} tables · {len(a.metrics)} metrics</span>'
        '<span class="chev">›</span></a>'
        for a in org.subject_areas
    )
    content = (
        '<div class="crumbs">Model</div>'
        f'<div class="h1row"><h1>{ui.esc(org.datasource)}</h1>'
        '<span class="readonly-pill">Read-only · edit in Claude</span></div>'
        f'<p class="lead">{ui.esc(org.description or "The deployed semantic model.")}</p>'
        f'<div class="statrow">{stats}</div>'
        f"{_glossary_html(org.key_terminology)}"
        f"{_storage_html(org.storage_connections)}"
        f'<h2 class="sec">Subject areas <span class="c">{len(org.subject_areas)}</span></h2>'
        f'<div class="tlist">{areas}</div>'
    )
    # Org-level (cross-area) metrics/entities belong to no single area — surface them here so they
    # aren't silently dropped.
    if org.cross_subject_area_metrics:
        cards = "".join(_metric_card_html(m) for m in org.cross_subject_area_metrics)
        content += f'<h2 class="sec">Cross-area metrics</h2><div class="grid">{cards}</div>'
    if org.cross_subject_area_entities:
        cards = "".join(_entity_card_html(e) for e in org.cross_subject_area_entities)
        content += f'<h2 class="sec">Cross-area entities</h2><div class="grid">{cards}</div>'
    return _model_shell(content, tree, **chrome)


def _metric_card_html(m: Any) -> str:
    aliases = ", ".join(m.other_names) if m.other_names else ""
    alias_html = f' <span class="al">· {ui.esc(aliases)}</span>' if aliases else ""
    unit = f' <span class="al">· {ui.esc(m.unit)}</span>' if m.unit else ""
    calc = ui.esc(m.calculation or "")
    return (
        f'<div class="mcard"><div class="nm">{ui.esc(m.name)}{alias_html}{unit}</div>'
        f'<div class="muted" style="font-size:13px">{ui.esc(m.description or "")}</div>'
        f'<span class="calc">{calc}</span></div>'
    )


def _entity_card_html(e: Any) -> str:
    aliases = ", ".join(e.other_names) if e.other_names else ""
    alias_html = f' <span class="al">· {ui.esc(aliases)}</span>' if aliases else ""
    pattern = (
        f' <span class="muted mono" style="font-size:12px">{ui.esc(e.value_pattern)}</span>'
        if e.value_pattern
        else ""
    )
    return (
        f'<div class="mcard"><div class="nm">{ui.esc(e.name)}{alias_html} '
        f"{_conf_badge(e.confidence)}</div>"
        f'<div class="muted" style="font-size:13px">{ui.esc(e.description or "")}{pattern}</div></div>'
    )


def model_area_html(
    org: Any, area: Any, datasource: str, datasources: list[str], **chrome: str
) -> str:
    """A subject-area landing: its tables (scannable), then metrics + entities as cards."""
    tree = _model_tree_html(org, datasource, datasources, active_area=area.name)
    tables = "".join(
        f'<a class="trow" href="{_model_url(datasource, area=area.name, table=t.name)}">'
        f'<span class="nm">{ui.esc(t.name)}</span>'
        f'<span class="d">{ui.esc(t.description or "")}</span>'
        f'<span class="meta">{len(t.columns)} cols · '
        f"{ui.esc(_human_count(_est_rows_obj(t)))}</span>{_conf_badge(t.confidence)}"
        '<span class="chev">›</span></a>'
        for t in area.tables_defined
    )
    metrics = "".join(_metric_card_html(m) for m in area.metrics)
    entities = "".join(_entity_card_html(e) for e in area.entities)
    window = (
        f"<span>default window · <b>{ui.esc(area.default_time_window)}</b></span>"
        if area.default_time_window
        else ""
    )
    content = (
        f'<div class="crumbs"><a href="{_model_url(datasource)}">{ui.esc(datasource)}</a>'
        f'<span class="sep">/</span>{ui.esc(area.name)}</div>'
        f"<h1>{ui.esc(area.name)}</h1>"
        f'<div class="subline"><span><b>{len(area.tables_defined)}</b> tables</span>'
        f"<span><b>{len(area.metrics)}</b> metrics</span>"
        f"<span><b>{len(area.entities)}</b> entities</span>{window}</div>"
        f'<p class="lead">{ui.esc(area.description or "")}</p>'
        f'<h2 class="sec">Tables <span class="c">{len(area.tables_defined)}</span></h2>'
        f'<div class="tlist">{tables}</div>'
    )
    if metrics:
        content += f'<h2 class="sec">Metrics</h2><div class="grid">{metrics}</div>'
    if entities:
        content += f'<h2 class="sec">Entities</h2><div class="grid">{entities}</div>'
    return _model_shell(content, tree, **chrome)


def _est_rows_obj(table: Any) -> int | None:
    ph = getattr(table, "performance_hints", None)
    return getattr(ph, "estimated_row_count", None) if ph is not None else None


# --- the table (dataset) page ------------------------------------------------

_COL_THEAD = (
    '<thead><tr><th style="width:210px">Column</th><th style="width:120px">Type</th>'
    '<th>Description</th><th style="width:170px" class="flags">Flags</th></tr></thead>'
)


def _col_flags_html(col: Any) -> str:
    """Per-column flags — only what carries signal (PK / FK / enum / unit / sensitive / caveat); the
    redundant per-column 'confirmed/approved' the old view repeated on every row is left out."""
    flags = []
    if col.primary_key:
        flags.append('<span class="badge b-pk">PK</span>')
    fk = getattr(col, "foreign_key", None)
    if fk is not None and getattr(fk, "table", None):
        flags.append(f'<span class="badge b-fk">FK → {ui.esc(fk.table)}</span>')
    if getattr(col, "choice_field", None):
        flags.append('<span class="badge b-soft">enum</span>')
    if col.unit:
        flags.append(f'<span class="badge b-soft">{ui.esc(str(col.unit))}</span>')
    if col.sensitive:
        flags.append('<span class="badge b-sensitive">● sensitive</span>')
    if col.caveats:
        flags.append('<span class="badge b-proposed">⚠ caveat</span>')
    return " ".join(flags)


def _col_rows_html(columns: list[Any]) -> str:
    """The <tr>s for a set of columns; a column with caveats gets an inline note row beneath it."""
    out = ""
    for col in columns:
        if col.description:
            desc = ui.esc(col.description)
            if getattr(col, "description_source", None) == "ai_unvalidated":
                desc += ' <span class="aichip" title="AI-described, unvalidated">AI</span>'
        else:
            desc = '<span class="dash">—</span>'
        out += (
            '<tr class="crow">'
            f'<td class="cn">{ui.esc(col.name)}</td>'
            f'<td><span class="ct">{ui.esc(str(col.type))}</span></td>'
            f'<td class="cd">{desc}</td>'
            f'<td class="flags">{_col_flags_html(col)}</td></tr>'
        )
        if col.caveats:
            note = "<br>".join(ui.esc(c) for c in col.caveats)
            out += f'<tr class="noterow"><td colspan="4"><div class="note">{note}</div></td></tr>'
    return out


def _columns_flat_html(columns: list[Any]) -> str:
    """A flat schema table. Narrow tables show in full; wide ones show the first 8 and tuck the rest
    behind a JS-free 'show all N' <details> — the default stays short without hiding anything."""
    if len(columns) <= 12:
        return f'<table class="cols">{_COL_THEAD}<tbody>{_col_rows_html(columns)}</tbody></table>'
    head = _col_rows_html(columns[:8])
    rest = _col_rows_html(columns[8:])
    return (
        f'<table class="cols">{_COL_THEAD}<tbody>{head}</tbody></table>'
        f'<details class="showmore"><summary>Show all {len(columns)} columns</summary>'
        f'<table class="cols"><tbody>{rest}</tbody></table></details>'
    )


def _columns_grouped_html(table: Any) -> str:
    """Collapsible groups from the table's authored `column_groups` (labelled by
    `column_group_descriptions`); columns in no authored group fall into a trailing 'Other'."""
    descs = getattr(table, "column_group_descriptions", {}) or {}
    by_name = {c.name: c for c in table.columns}
    seen: set[str] = set()
    blocks = ""
    for i, (gname, colnames) in enumerate(table.column_groups.items()):
        cols = [by_name[n] for n in colnames if n in by_name]
        seen.update(colnames)
        gloss = ui.esc(descs.get(gname, ""))
        gloss_html = f'<span class="gdesc">{gloss}</span>' if gloss else ""
        blocks += (
            f'<details class="grp"{" open" if i < 2 else ""}><summary>'
            f'<span class="gname">{ui.esc(gname)}</span>{gloss_html}'
            f'<span class="gn">{len(cols)}</span></summary>'
            f'<table class="cols"><tbody>{_col_rows_html(cols)}</tbody></table></details>'
        )
    other = [c for c in table.columns if c.name not in seen]
    if other:
        blocks += (
            '<details class="grp"><summary><span class="gname">Other</span>'
            f'<span class="gn">{len(other)}</span></summary>'
            f'<table class="cols"><tbody>{_col_rows_html(other)}</tbody></table></details>'
        )
    return blocks


def _caveat_callout(caveats: list[str]) -> str:
    if not caveats:
        return ""
    body = "<br>".join(ui.esc(c) for c in caveats)
    return f'<div class="caveat"><span class="ic">⚠</span><div class="t">{body}</div></div>'


def _table_rels_html(org: Any, area: Any, table_name: str) -> str:
    """Relationships touching this table — within-area + the org-level cross-area ones."""
    rels = list(area.relationships) + list(org.cross_subject_area_relationships)
    rows = "".join(
        f'<div class="rel"><span class="mono">{ui.esc(r.from_table)}</span>'
        f'<span class="arr">→</span><span class="mono">{ui.esc(r.to_table)}</span>'
        f'<span class="badge b-soft">{ui.esc(str(r.relationship))}</span>'
        f'<span class="ro">{ui.esc(str(r.join_type))} · {ui.esc(str(r.confidence))}</span></div>'
        for r in rels
        if r.from_table == table_name or r.to_table == table_name
    )
    if not rows:
        return ""
    return f'<h2 class="sec">Relationships</h2><div class="card">{rows}</div>'


def _table_metrics_html(area: Any, table_name: str) -> str:
    """Metrics whose `source_tables` include this table."""
    using = [m for m in area.metrics if table_name in (m.source_tables or [])]
    if not using:
        return ""
    cards = "".join(_metric_card_html(m) for m in using)
    return f'<h2 class="sec">Used by metrics</h2><div class="grid">{cards}</div>'


def model_table_html(
    org: Any, area: Any, table: Any, datasource: str, datasources: list[str], **chrome: str
) -> str:
    """A table (dataset) page — the heart of the explorer: header, caveats, columns
    (grouped-when-authored else flat), then relationships + metrics that use it."""
    tree = _model_tree_html(
        org, datasource, datasources, active_area=area.name, active_table=table.name
    )
    schema = (
        f'<span class="schema">{ui.esc(table.schema_name)}.</span>' if table.schema_name else ""
    )
    rows = _human_count(_est_rows_obj(table))
    grain = ", ".join(table.grain) if table.grain else ""
    aichip = (
        ' <span class="descsrc">AI-described · unvalidated</span>'
        if getattr(table, "description_source", None) == "ai_unvalidated"
        else ""
    )
    sql_block = ""
    if getattr(table, "source_type", None) == "sql" and table.sql:
        sql_block = (
            '<h2 class="sec">Defining SQL</h2>'
            f'<pre class="code" style="white-space:pre-wrap;display:block;padding:12px">'
            f"{ui.esc(table.sql)}</pre>"
        )
    subline = "".join(
        f"<span>{s}</span>"
        for s in (
            f"<b>{len(table.columns)}</b> columns",
            f"<b>{ui.esc(rows)}</b> rows" if rows else "",
            f'grain · <b class="mono">{ui.esc(grain)}</b>' if grain else "",
            ui.esc(table.storage_connection or ""),
        )
        if s
    )
    columns = (
        _columns_grouped_html(table) if table.column_groups else _columns_flat_html(table.columns)
    )
    content = (
        f'<div class="crumbs"><a href="{_model_url(datasource)}">{ui.esc(datasource)}</a>'
        f'<span class="sep">/</span>'
        f'<a href="{_model_url(datasource, area=area.name)}">{ui.esc(area.name)}</a>'
        f'<span class="sep">/</span>{ui.esc(table.name)}</div>'
        f'<div class="h1row"><h1>{schema}{ui.esc(table.name)}</h1>'
        f"{_conf_badge(table.confidence)}"
        '<span class="readonly-pill">Read-only · edit in Claude</span></div>'
        f'<div class="subline">{subline}</div>'
        f'<p class="desc">{ui.esc(table.description or "")}{aichip}</p>'
        f"{_caveat_callout(table.caveats)}"
        f'<h2 class="sec">Columns <span class="c">{len(table.columns)}</span></h2>'
        f"{columns}{sql_block}"
        f"{_table_rels_html(org, area, table.name)}"
        f"{_table_metrics_html(area, table.name)}"
    )
    return _model_shell(content, tree, **chrome)


def _qualified(schema: str | None, table: str) -> str:
    return f"{ui.esc(schema)}.{ui.esc(table)}" if schema else ui.esc(table)


def _cross_rel_row_html(r: Any) -> str:
    """One cross-area relationship: schema-qualified from→to, the join columns, cardinality, trust."""
    on = f"{ui.esc(r.from_column)} = {ui.esc(r.to_column)}" if r.from_column and r.to_column else ""
    meta = " · ".join(p for p in (on, ui.esc(str(r.join_type)), ui.esc(str(r.confidence))) if p)
    return (
        f'<div class="rel"><span class="mono">{_qualified(r.from_schema, r.from_table)}</span>'
        f'<span class="arr">→</span>'
        f'<span class="mono">{_qualified(r.to_schema, r.to_table)}</span>'
        f'<span class="badge b-soft">{ui.esc(str(r.relationship))}</span>'
        f'<span class="ro">{meta}</span></div>'
    )


def model_relationships_html(
    org: Any, datasource: str, datasources: list[str], **chrome: str
) -> str:
    """The cross-area relationships — the org-level joins that span subject areas — grouped by
    area-pair, so the model's cross-area topology is readable in one place (within-area joins stay
    on each table page)."""
    tree = _model_tree_html(org, datasource, datasources, active_view="relationships")
    rels = org.cross_subject_area_relationships
    groups: dict[tuple[str, str], list[Any]] = {}
    for r in rels:
        groups.setdefault((r.from_subject_area, r.to_subject_area), []).append(r)
    # Most-connected area-pairs first, then alphabetical — the same ordering as the topology view.
    blocks = ""
    for (fa, ta), items in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        rows = "".join(_cross_rel_row_html(r) for r in items)
        blocks += (
            f'<details class="grp" open><summary>'
            f'<span class="gname">{ui.esc(fa)} <span class="arr">→</span> {ui.esc(ta)}</span>'
            f'<span class="gn">{len(items)}</span></summary>{rows}</details>'
        )
    body = blocks if rels else '<p class="lead">No cross-area relationships in this model.</p>'
    content = (
        f'<div class="crumbs"><a href="{_model_url(datasource)}">{ui.esc(datasource)}</a>'
        '<span class="sep">/</span>Relationships</div><h1>Cross-area relationships</h1>'
        f'<p class="lead">The <b>{len(rels)}</b> org-level joins that span subject areas, grouped by '
        "area-pair. (Joins within a single area show on each table page.)</p>"
        f"{body}"
    )
    return _model_shell(content, tree, **chrome)


def model_context_html(
    org: Any, memory: dict[str, str], datasource: str, datasources: list[str], **chrome: str
) -> str:
    """The Domain-context page — the deployed datasource.md rendered as (safe) markdown."""
    tree = _model_tree_html(org, datasource, datasources, active_view="context")
    datasource_md = memory.get("datasource")
    doc = (
        f'<div class="context">{ui.md(datasource_md)}</div>'
        if datasource_md
        else '<p class="lead">No domain context (datasource.md) deployed for this datasource.</p>'
    )
    content = (
        f'<div class="crumbs"><a href="{_model_url(datasource)}">{ui.esc(datasource)}</a>'
        '<span class="sep">/</span>Domain context</div><h1>Domain context</h1>'
        '<p class="lead">The deployed datasource.md — the domain notes Claude reads as context. '
        f"Read-only.</p>{doc}"
    )
    return _model_shell(content, tree, **chrome)


async def admin_model(request: Request) -> Response:
    """The read-only Model explorer. Session-gated; a pure GET projection of the served model. Query:
    `?datasource=` (defaults to the first served), `?area=`, `?view=context`."""
    import model_store
    import tools

    admin = current_admin(request)
    if admin is None:
        return RedirectResponse("/admin/login", status_code=302)
    store = _open_store()
    try:
        chrome = _admin_chrome(store, admin)
        # No request-org on the admin path (bearer middleware skipped) → the operator's own org.
        org_id = tools.current_org_id()
        datasources = (
            model_store.list_datasources(store, org_id=org_id) if store is not None else []
        )
        if not datasources:
            return HTMLResponse(model_empty_html("", [], **chrome))
        datasource = request.query_params.get("datasource") or datasources[0]
        if datasource not in datasources:  # an unknown/stale datasource param → the first served
            datasource = datasources[0]
        org = model_store.load_datasource(store, datasource, org_id=org_id)
        if org is None:
            return HTMLResponse(model_empty_html(datasource, datasources, **chrome))
        view = request.query_params.get("view")
        if view == "relationships":
            return HTMLResponse(model_relationships_html(org, datasource, datasources, **chrome))
        if view == "context":
            memory = model_store.load_memory(store, datasource, org_id=org_id)
            return HTMLResponse(model_context_html(org, memory, datasource, datasources, **chrome))
        area_name = request.query_params.get("area")
        if area_name:
            area = next((a for a in org.subject_areas if a.name == area_name), None)
            if area is not None:
                table_name = request.query_params.get("table")
                if table_name:
                    table = next((t for t in area.tables_defined if t.name == table_name), None)
                    if table is not None:
                        return HTMLResponse(
                            model_table_html(org, area, table, datasource, datasources, **chrome)
                        )
                return HTMLResponse(model_area_html(org, area, datasource, datasources, **chrome))
        version = model_store.newest_model_version(store, datasource, org_id=org_id)
        return HTMLResponse(model_overview_html(org, version, datasource, datasources, **chrome))
    finally:
        if store is not None:
            store.close()


def routes() -> list:
    """The `/admin/*` routes, for the transport to mount. Each is session-gated in the handler (the
    transport adds these paths to the bearer public-skip — they do their own auth, not the MCP one)."""
    from starlette.routing import Route

    return [
        Route("/admin", admin_home, methods=["GET"]),
        # Read-only model explorer — GET only, by design (no write path can hide behind /admin/model).
        Route("/admin/model", admin_model, methods=["GET"]),
        Route("/admin/login", admin_login, methods=["GET", "POST"]),
        Route("/admin/logout", admin_logout, methods=["GET"]),
        # The admin OIDC start (handler lives in oauth_server, with the OIDC machinery). The IdP
        # redirects back to the shared /oauth/oidc/callback, which branches on the state's purpose.
        Route("/admin/oidc/start", admin_oidc_start, methods=["GET"]),
        Route("/admin/users", admin_create_user, methods=["POST"]),
        Route("/admin/users/status", admin_set_status, methods=["POST"]),
    ]


ADMIN_PATHS = (
    "/admin",
    "/admin/model",
    "/admin/login",
    "/admin/logout",
    "/admin/oidc/start",
    "/admin/users",
    "/admin/users/status",
)
