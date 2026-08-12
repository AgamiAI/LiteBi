"""Teammate self-onboarding — the **setup link** and the **reset link**, for a password deployment.

When a deployment has no OIDC configured, a teammate the admin created is a *pending* user (no
password, no provider) and there's no email to send an invite to. The admin copies a link from the
console (a signed, time-boxed token — no new table) and shares it out-of-band; the teammate opens it
and chooses a password. (OIDC deployments don't use this — teammates bind on first OIDC login at the
connector; see `oauth_server`.)

**Two purposes, kept strictly disjoint**, because they are two different acts on two different
accounts and one of them overwrites a live credential:

  - `setup` — a *pending* account chooses its first password. Single-use by construction: claiming
    flips the user out of pending, so the guarded `claim_pending_password` UPDATE no-ops any replay.
  - `reset` — a *claimed* account is given a new password, because an administrator asked for one on
    that person's behalf. There is no self-service "forgot password" here, so without this an
    administrator can create a colleague's account and then never help them back into it.

A reset token cannot act on a pending account and a setup token cannot act on a claimed one, so
neither link can be used for the other's job.

**The reset link is single-use too, by a different mechanism, and it needs one.** The pending flag
retires a setup token; a reset leaves the account exactly as claimed as it was, so nothing about the
row would change and the link would work forever. Instead the token carries a one-way marker of the
credential it was minted against (`user_store.credential_fingerprint`), and the page re-derives it
from the live row: setting a new password changes the stored hash, which retires every link minted
against the old one — including the one just used.

**A reset ends the sessions running on the old password**, up to one access-token lifetime (see
`oauth_server.revoke_refresh_tokens_for`, which is precise about why "up to"). Two things it does NOT
end, stated because a security control that is believed to cover more than it does is worse than a
narrow one: an outstanding OAuth **authorization code** (a ten-minute window), and the **`/admin`
console cookie**, which is a stateless 12-hour JWT that nothing here revokes. The console cookie only
matters when the account being reset is the configured operator admin, and closing it properly means
binding that cookie to something a reset invalidates — worth doing, and not this change.

**Holding a reset link is a change-detector for that account.** The page answers 200 while the link
can still act and 400 once it cannot, so whoever holds it can tell the moment the target's password
moves by any route. That falls out of binding the token to the credential and is the price of the
link being single-use; it discloses nothing about the account beyond that one bit.

This is a public surface (the teammate has no session yet), so it self-checks: a bad/expired/used
token, a purpose that does not match the account's state, or a marker that no longer matches, all
yield the same generic page — never a credential overwrite the token did not authorise, and no
email-enumeration. (The token is a signed JWT — unforgeable, though its payload is base64url-readable
by whoever holds the link, which is why the marker is a hash and never the credential itself; the
no-enumeration property comes from the handler returning the same generic page regardless, not from
the token being secret.)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hmac import compare_digest
from typing import Any

import jwt
import oauth_server
import ui
import user_store
from async_offload import run_blocking
from oauth_server import _open_store, _signing_secret
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

# A generous TTL — the admin shares the link out-of-band and the teammate may take a while. The token
# is still single-use (the pending guard), so the window only bounds an unused link, not a claimed one.
_SETUP_TTL = timedelta(days=14)
# Deliberately much shorter. An unused setup link is a door to an account nobody has ever been in; an
# unused RESET link is a door to a working account somebody is using today, so the window in which a
# forwarded or mislaid link is worth anything should be a few days, not a fortnight.
_RESET_TTL = timedelta(days=3)
_SETUP_PURPOSE = "setup"  # marks this token apart from the OAuth bearer + the admin session JWT
_RESET_PURPOSE = "reset"  # ...and marks the two links apart from each other; see the module note
_MIN_PASSWORD_LEN = 8
_MAX_PASSWORD_LEN = (
    256  # cap the input (a sane bound; argon2's cost is fixed, this just rejects junk)
)


def _decode(token: str) -> dict[str, Any] | None:
    """A token's claims if the signature and expiry hold and it names somebody, else None. Says
    nothing about PURPOSE — that is each caller's question, and one shared decoder is what keeps the
    two from validating differently."""
    try:
        claims = jwt.decode(
            token, _signing_secret(), algorithms=["HS256"], options={"require": ["exp", "sub"]}
        )
    except Exception:
        return None
    sub = claims.get("sub")
    return claims if isinstance(sub, str) and sub else None


def mint_setup_token(username: str) -> str:
    """A signed, time-boxed setup token for `username` — the body of the admin's copy-able setup link."""
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": username,
            "purpose": _SETUP_PURPOSE,
            "iat": int(now.timestamp()),
            "exp": int((now + _SETUP_TTL).timestamp()),
        },
        _signing_secret(),
        algorithm="HS256",
    )


def mint_reset_token(username: str, fingerprint: str) -> str:
    """A signed, time-boxed reset token for `username`, bound to the credential they hold right now.

    `fingerprint` is `user_store.credential_fingerprint` of that account's current row. Binding it
    into the token is what makes the link single-use: the page re-derives the marker from the live
    row and refuses when they differ, so the first successful reset retires this token. Minting is a
    caller's decision — the authorization for it lives in whichever console asked, not here.
    """
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": username,
            "purpose": _RESET_PURPOSE,
            "cred": fingerprint,
            "iat": int(now.timestamp()),
            "exp": int((now + _RESET_TTL).timestamp()),
        },
        _signing_secret(),
        algorithm="HS256",
    )


def verify_setup_token(token: str) -> str | None:
    """The username a valid setup token names, or None (bad signature / expired / wrong purpose)."""
    claims = _decode(token)
    if claims is None or claims.get("purpose") != _SETUP_PURPOSE:
        return None
    return claims["sub"]


def verify_reset_token(token: str) -> tuple[str, str] | None:
    """`(username, fingerprint)` a valid reset token names, or None. The fingerprint is returned
    rather than checked here: this module does not read the store, and a checker that could not see
    the row would have to be trusted by the handler anyway."""
    claims = _decode(token)
    if claims is None or claims.get("purpose") != _RESET_PURPOSE:
        return None
    cred = claims.get("cred")
    return (claims["sub"], cred) if isinstance(cred, str) and cred else None


def is_pending(user: dict[str, Any]) -> bool:
    """A user who hasn't claimed a credential yet: no password AND no OIDC provider. Works for both row
    shapes — `get_user` (carries `password_hash`) and `list_users` (carries the derived `has_password`)."""
    has_password = user.get("password_hash") is not None or bool(user.get("has_password"))
    return not has_password and user.get("oidc_provider") is None


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


# What each purpose calls itself, on every page the person sees. Held as one table rather than
# branched at each of the four places it is needed: somebody arriving at a reset link has an account
# and knows it, and being told to "finish setting up" would read as though theirs had been wiped.
_WORDING: dict[str, dict[str, str]] = {
    _SETUP_PURPOSE: {
        "title": "Set up your account",
        "lead": "Choose a password to finish setting up.",
        "submit": "Set password",
        "done": "Your password is set.",
    },
    _RESET_PURPOSE: {
        "title": "Choose a new password",
        "lead": "Your administrator asked us to let you set a new password.",
        "submit": "Save password",
        # Says the part that is surprising if unexplained: they will have to sign in again elsewhere.
        "done": "Your new password is set. Anywhere you were signed in will ask for it again.",
    },
}


def claim_page_html(
    token: str, purpose: str = _SETUP_PURPOSE, error: str = "", username: str = ""
) -> str:
    """The choose-a-password page reached from a valid link, worded for what the link is for.

    **It names the account.** Without that, somebody following a link is asked to choose a password
    with no way to tell WHOSE it is — and the link is shared out-of-band, so the person holding it may
    have been sent the wrong one, or two of them. That was tolerable while the only link set a first
    password on an account the recipient was expecting; it is not now that a link can REPLACE a working
    password, where following the wrong one silently locks somebody out of their own account.

    It discloses nothing new: whoever holds the link already holds a token whose payload is base64url
    and carries this same address in the clear (the module note says so). Showing it turns something
    they could decode into something they can check.
    """
    words = _WORDING[purpose]
    alert = f'<div class="alert error">{ui.esc(error)}</div>' if error else ""
    whose = f'<p class="small">for <strong>{ui.esc(username)}</strong></p>' if username else ""
    body = f"""<div class="consent"><p class="who">{ui.esc(words["title"])}</p>
{whose}
<p class="small">{ui.esc(words["lead"])}</p></div>
{alert}
<form method="post" action="/claim">
<input type="hidden" name="token" value="{ui.esc(token)}">
<label for="p">Password</label>
<input id="p" name="password" type="password" autocomplete="new-password" placeholder="••••••••">
<button class="btn" type="submit" style="margin-top:22px">{ui.esc(words["submit"])}</button>
</form>"""
    return ui.auth_page(words["title"], body)


def claim_done_html(base_url: str, purpose: str = _SETUP_PURPOSE) -> str:
    body = f"""<div class="consent"><p class="who">You're all set</p>
<p class="small">{ui.esc(_WORDING[purpose]["done"])} Add this server to Claude as a custom
connector:</p></div>
<p><span class="code">{ui.esc(base_url)}/mcp</span></p>"""
    return ui.auth_page("All set", body)


def setup_invalid_html() -> str:
    """One page for every failure — see the module note on why it says nothing specific."""
    body = """<div class="consent"><p class="who">This link isn't valid</p>
<p class="small">It may have expired or already been used. Ask your administrator for a new
link.</p></div>"""
    return ui.auth_page("Invalid link", body)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


async def _form(request: Request) -> dict[str, str]:
    data = await request.form()
    return {k: (v if isinstance(v, str) else "") for k, v in data.items()}


def _user(username: str) -> dict[str, Any] | None:
    """The account a token names, or None (unknown, or no store)."""
    store = _open_store()
    if store is None:
        return None
    try:
        return user_store.get_user(store, username)
    finally:
        store.close()


def _actionable(token: str) -> tuple[str, str, str | None] | None:
    """`(username, purpose, expected_hash)` for a token that may act on that account right now.

    The one place the two purposes are matched against the state of the row, so a link can only ever
    do the job it was minted for:

      - `setup` requires a **pending** account — nothing to overwrite.
      - `reset` requires a **claimed, active, password-based** account whose credential is still the
        one the token was minted against. Together the two mean a reset token cannot act on a pending
        account, nor a setup token on a claimed one.

    **Every condition here mirrors `user_store.reset_password`'s WHERE, and the mirroring is the
    point.** When it did not, two things broke at once and neither was obvious. A token for an account
    that had since bound an identity provider got a **200 and a password form**, because only the
    write refused — so somebody chose a password, submitted it and was told the link was invalid,
    which is the exact split the switched-off case already calls unacceptable. And because such a
    token could never spend (the write refuses, so the credential never changes, so the marker still
    matches), it stayed actionable for its whole three-day life while every POST paid for an argon2
    hash: an unauthenticated, unrate-limited, unbounded CPU lever, measured at 45x a rejected token.
    Refusing here is what makes both of those a flat 400 that hashes nothing.

    `expected_hash` is handed back so the UPDATE can be guarded on it. Checking the marker here and
    writing afterwards is check-then-act across two connections; see `reset_password` for why that
    loses the single-use property it appears to provide.
    """
    # Through the verifiers rather than re-deriving from `_decode`: each purpose's claim validation
    # then exists once. When this decoded for itself, `verify_reset_token`'s `isinstance` check on
    # `cred` was skipped on the live path — so the type-checked function was dead code and the public
    # path was the one missing the check, which is the drift `_decode` exists to prevent.
    setup_for = verify_setup_token(token)
    if setup_for is not None:
        user = _user(setup_for)
        return (setup_for, _SETUP_PURPOSE, None) if user and is_pending(user) else None

    reset = verify_reset_token(token)
    if reset is None:
        return None
    username, cred = reset
    user = _user(username)
    if user is None:
        return None
    stored = user.get("password_hash")
    if (
        not stored
        or user.get("oidc_provider") is not None
        or user.get("status") != user_store.ACTIVE_STATUS
    ):
        return None
    # `compare_digest` rather than `==` for the usual reason a secret-derived value gets it: this is
    # the check that decides whether a link is spent, and it is reachable by anyone holding it.
    if not compare_digest(user_store.credential_fingerprint(user), cred):
        return None
    return (username, _RESET_PURPOSE, stored)


async def claim(request: Request) -> Response:
    """GET → the choose-a-password page for a link that can still act; POST → write the password.

    Every failure (bad token, wrong state for the purpose, a spent reset link, a weak password, a lost
    race) is the same generic page — no credential overwrite a token did not authorise, and no
    enumeration. This endpoint is **not** rate-limited in-process — rely on the deployment's proxy/LB
    for that (a documented gap, like the other public endpoints).
    """
    if request.method == "GET":
        token = request.query_params.get("token", "")
        actionable = _actionable(token)
        if actionable is None:
            return HTMLResponse(setup_invalid_html(), status_code=400)
        return HTMLResponse(claim_page_html(token, actionable[1], username=actionable[0]))

    form = await _form(request)
    token = form.get("token", "")
    actionable = _actionable(token)
    if actionable is None:
        return HTMLResponse(setup_invalid_html(), status_code=400)
    username, purpose, expected_hash = actionable
    password = form.get("password", "")
    # Both bounds, and each says which one was missed. One message for both read "Use at least 8
    # characters" to somebody who had just typed three hundred — advice that makes the problem worse
    # the more carefully it is followed. Neither message names the password back, and neither is
    # branched on anything but its length.
    if not _MIN_PASSWORD_LEN <= len(password) <= _MAX_PASSWORD_LEN:
        too_short = len(password) < _MIN_PASSWORD_LEN
        return HTMLResponse(
            claim_page_html(
                token,
                purpose,
                error=(
                    f"Use at least {_MIN_PASSWORD_LEN} characters."
                    if too_short
                    else f"Use at most {_MAX_PASSWORD_LEN} characters."
                ),
                username=username,
            ),
            status_code=400,
        )
    # Off the event loop. Argon2 is deliberately slow and memory-hard, and this handler is `async` on a
    # public endpoint — hashing inline stalls every other request in flight for the duration, which is
    # the reason `oauth_server`'s own credential check offloads. Everything blocking goes together, so
    # the store work rides the same worker rather than crossing back and forth.
    changed = await run_blocking(_write_password, username, purpose, password, expected_hash)
    if not changed:
        return HTMLResponse(setup_invalid_html(), status_code=400)
    from mcp_http import public_base_url

    return HTMLResponse(claim_done_html(public_base_url(), purpose))


def _write_password(username: str, purpose: str, password: str, expected_hash: str | None) -> int:
    """Set the password and, for a reset, revoke that principal's sessions. Rows changed, 0 if refused.

    **One transaction, and it has to be.** The password moving and the old sessions dying are one
    event: committed separately, a revocation that failed after the write would leave the credential
    changed, every old refresh token live, and the caller looking at a 500 — a reset in name only, on
    a link that is now spent so they cannot retry. Committed together, either both happen or neither
    does. `revoke_refresh_tokens_for` is also called only when the write actually moved a row, so a
    lost race never signs somebody out on behalf of a change that did not happen.

    Synchronous by design — it is called through `run_blocking`; see the caller.
    """
    store = _open_store()
    if store is None:
        return 0
    try:
        if purpose == _SETUP_PURPOSE:
            # Guarded: the UPDATE only fires while still pending — a concurrent claim makes it a no-op.
            return user_store.claim_pending_password(store, username, password)
        # Likewise guarded, on different conditions (`user_store.reset_password` spells them out) —
        # including the credential it was minted against, which is what makes a replay a no-op here.
        changed = user_store.reset_password(
            store, username, password, expected_hash or "", commit=False
        )
        if changed:
            oauth_server.revoke_refresh_tokens_for(store, username, commit=False)
        store.commit()
        return changed
    finally:
        store.close()


def routes() -> list:
    from starlette.routing import Route

    return [Route("/claim", claim, methods=["GET", "POST"])]


PUBLIC_PATHS = ("/claim",)
