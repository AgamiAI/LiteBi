"""The `users` store + the password credential check — per-user identity for the hosted server.

This is the credential authenticator the OAuth authorize page calls: `authenticate` turns a
username/password into a `Principal` (the identity the token endpoint then mints a JWT for). It is
NOT the bearer-token `AuthProvider` (that validates the issued token — a later, separate adapter).
Flat access only: there is no role column (roles are paid).

Thin SQL over the portable `Store` (same shape as `model_store.py`), so it runs on SQLite (CI) and
Postgres (prod) unchanged. Passwords never touch this module in plaintext beyond the moment they're
hashed/verified via `passwords.py`.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from passwords import hash_password, needs_rehash, verify_password
from ports import Principal
from store import Store

#: The status a live account holds. **Public, because two modules must agree on it**: `onboarding`
#: refuses a reset for a switched-off account before drawing the page, and `reset_password`'s WHERE
#: refuses the write. Those two checks exist deliberately as separate layers, and a copy of this
#: string in each is exactly how layers drift apart — which is the defect this whole flow was fixed
#: for once already. `_ACTIVE` stays as the in-module alias so nothing below has to change.
ACTIVE_STATUS = "active"
_ACTIVE = ACTIVE_STATUS

# The OIDC provider keys a deploy may pin the admin to. Mirrors `oidc._PROVIDERS`, duplicated here on
# purpose: `oidc` is the one egress module (httpx), and `user_store` must stay import-light + egress-free.
_KNOWN_OIDC_PROVIDERS = ("google", "microsoft")

# A throwaway argon2id hash that no password verifies against. `authenticate` verifies against it on
# the user-absent path so every code path pays the same KDF cost — without it, a missing username
# returns far faster than a wrong password and leaks which usernames exist (an enumeration oracle).
_DUMMY_HASH = hash_password(uuid4().hex)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_email(email: str | None) -> str | None:
    """Canonical email form for storage + lookup: trimmed + lowercased, or None. Both paths use this
    so a stray-whitespace or mixed-case address can't dodge the one-to-one (UNIQUE) email identity."""
    if not email:
        return None
    return email.strip().lower() or None


def create_user(
    store: Store,
    username: str,
    password: str | None = None,
    email: str | None = None,
    status: str = _ACTIVE,
    oidc_provider: str | None = None,
    oidc_subject: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> str:
    """Create a user; returns the minted id. `password=None` makes a **passwordless** user (OIDC-only —
    no password login); a non-None password is argon2id-hashed and must be non-empty. `oidc_provider`
    pins which IdP the user signs in with; `oidc_subject` (the IdP `sub`) is usually bound on first
    login. `first_name`/`last_name` are display-only (blank → NULL). Raises if the username/email
    already exists (the UNIQUE constraints)."""
    if password is not None and not password.strip():
        raise ValueError("password must not be empty (pass None for a passwordless OIDC user)")
    user_id = uuid4().hex
    password_hash = hash_password(password) if password is not None else None
    # Emails are the OIDC lookup key — store them normalized (trim + lowercase) so the identity
    # matches regardless of casing/whitespace; the UNIQUE index keeps them one-to-one.
    normalized_email = _normalize_email(email)
    store.execute(
        "INSERT INTO users (id, username, password_hash, email, status, created, oidc_provider, "
        "oidc_subject, first_name, last_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user_id,
            username,
            password_hash,
            normalized_email,
            status,
            _now_iso(),
            oidc_provider,
            oidc_subject,
            (first_name or "").strip() or None,
            (last_name or "").strip() or None,
        ),
    )
    store.commit()
    return user_id


def bind_oidc_subject(store: Store, username: str, subject: str) -> None:
    """Pin the IdP subject to a user on first OIDC login — only when it isn't already set, so a later
    login can't silently rebind the account to a different IdP identity (the WHERE guard makes it a
    one-time, no-clobber bind)."""
    store.execute(
        "UPDATE users SET oidc_subject = ? WHERE username = ? AND oidc_subject IS NULL",
        (subject, username),
    )
    store.commit()


def claim_pending_oidc(store: Store, username: str, provider: str, subject: str) -> int:
    """A **pending** user (no password, no provider) adopts an OIDC provider + subject on first login.
    Guarded so it fires ONLY while still pending — if a password was set (a password deployment) or
    another provider already bound, the WHERE matches nothing and this no-ops; the caller re-reads and
    rejects rather than logging in against an unexpected binding. Returns the row count (0 ⇒ not
    pending / not claimable)."""
    cur = store.execute(
        "UPDATE users SET oidc_provider = ?, oidc_subject = ? "
        "WHERE username = ? AND oidc_provider IS NULL AND password_hash IS NULL",
        (provider, subject, username),
    )
    store.commit()
    return cur.rowcount


def claim_pending_password(store: Store, username: str, password: str) -> int:
    """A **pending** user sets their own password (a password deployment, via the admin setup link).
    Guarded so it fires ONLY while still pending — if an OIDC provider already bound (or a password was
    already set), the WHERE matches nothing and this no-ops. Returns the row count (0 ⇒ not claimable)."""
    cur = store.execute(
        "UPDATE users SET password_hash = ? "
        "WHERE username = ? AND password_hash IS NULL AND oidc_provider IS NULL",
        (hash_password(password), username),
    )
    store.commit()
    return cur.rowcount


def reset_password(
    store: Store, username: str, password: str, expected_hash: str, *, commit: bool = True
) -> int:
    """Set the password of an account that **already has one** — the administrator-initiated reset.

    Deliberately the opposite of `claim_pending_password` above, which fires only while an account is
    still pending. This one overwrites a live credential, so every condition that makes that safe is
    in the WHERE rather than trusted to the caller: a guard a route forgets to apply is a guard that
    is not there, and this UPDATE is one route away from a public page.

    - `oidc_provider IS NULL` — an SSO identity never grows a password. Without it, a reset would add
      a second way into an account whose deployment decided there is exactly one, and the person
      whose password it is would have no idea it existed.
    - `status = ?` (active) — a reset cannot resurrect a switched-off account. The account being off
      is the security decision; letting a link undo it silently would make the link the stronger one.
    - `password_hash = ?` (`expected_hash`) — **this is what actually makes a reset link single-use.**
      Checking the credential marker in the handler and then writing is check-then-act across two
      connections: two simultaneous posts of the same link both read the old hash, both pass, and both
      UPDATE, so whoever commits last owns the account while the other is told they succeeded. As a
      condition of the UPDATE the loser matches no row, gets 0, and is shown the same invalid page as
      any other spent link. `claim_pending_password` above is single-use by exactly this shape
      (`password_hash IS NULL`); this is that argument applied to a credential that already exists.

    `commit=False` leaves the transaction open so the caller can land the revocation of that
    principal's sessions in the same one — a password that has moved with sessions still renewing on
    the old one is a reset in name only, and the two must not be separately committable.

    Returns the row count: 0 means refused, and the caller must not report success. It cannot tell
    you WHICH condition refused, on purpose — the page this is reached from is public and answers
    every failure identically.
    """
    cur = store.execute(
        "UPDATE users SET password_hash = ? WHERE username = ? AND password_hash = ? "
        "AND password_hash IS NOT NULL AND oidc_provider IS NULL AND status = ?",
        (hash_password(password), username, expected_hash, _ACTIVE),
    )
    if commit:
        store.commit()
    return cur.rowcount


def credential_fingerprint(user: dict[str, Any]) -> str:
    """A short, one-way marker of the credential an account holds right now.

    This is what makes a reset link **single-use**, and it needs its own mechanism because the one
    that makes a *setup* link single-use does not apply: a setup token dies because claiming flips the
    account out of pending, and a reset leaves the account exactly as claimed as it was. So the token
    carries this marker, the claim page re-computes it, and setting a new password changes the stored
    hash — which retires every link minted against the old one, including the one just used.

    **A hash of the hash, never the hash itself.** A setup link's payload is base64url and readable by
    whoever holds it (`onboarding`'s module note says so), so putting `password_hash` in a token would
    hand an argon2 digest to anyone the link is forwarded to. Truncated because this is a change
    detector, not a credential: it needs to differ when the hash differs, and it is compared only
    against a value re-derived from the same row.

    Argon2 salts every hash, so re-setting the *same* password still produces a different digest and
    still retires the old link. `authenticate`'s opportunistic rehash changes the stored hash too, so
    a cost-parameter bump silently spends outstanding reset links on the owner's next sign-in — which
    is the safe direction and worth knowing before it looks like a bug.

    **Raises for an account with no password**, rather than fingerprinting the empty string. That
    fallback would have produced `sha256("")` — one publicly computable constant, identical for every
    passwordless row in every deployment — so the marker would have been a marker of nothing and the
    single-use binding would have been vacuous exactly where the account is most exposed. There is no
    legitimate caller: a reset is for an account that HAS a credential, and a pending one is the setup
    link's job.
    """
    stored = user.get("password_hash")
    if not stored:
        raise ValueError(
            "this account has no password to fingerprint; a reset does not apply to it"
        )
    return hashlib.sha256(stored.encode()).hexdigest()[:16]


def get_user_by_email(store: Store, email: str) -> dict[str, Any] | None:
    """Look up a user by email — the onboarded-only lookup OIDC uses. Case-insensitive (emails are
    stored lowercased) and one-to-one (the email index is UNIQUE)."""
    normalized = _normalize_email(email)
    if normalized is None:
        return None
    rows = store.query("SELECT * FROM users WHERE email = ?", (normalized,))
    return rows[0] if rows else None


def get_user(store: Store, username: str) -> dict[str, Any] | None:
    rows = store.query("SELECT * FROM users WHERE username = ?", (username,))
    return rows[0] if rows else None


def list_users(store: Store) -> list[dict[str, Any]]:
    """Identity rows only — never selects `password_hash` itself (so a listing can't leak the secret).
    `has_password` is a derived 0/1 flag (the admin roster needs to show the sign-in method without
    ever touching the hash)."""
    return store.query(
        "SELECT id, username, email, status, created, oidc_provider, first_name, last_name, "
        "(CASE WHEN password_hash IS NOT NULL THEN 1 ELSE 0 END) AS has_password "
        "FROM users ORDER BY username"
    )


def set_status(store: Store, username: str, status: str) -> int:
    """Set a user's status; returns the number of rows changed (0 ⇒ no such username), so a caller can
    tell an applied change from a no-op rather than reporting a false success."""
    cur = store.execute("UPDATE users SET status = ? WHERE username = ?", (status, username))
    store.commit()
    return cur.rowcount


def authenticate(store: Store, username: str, password: str) -> Principal | None:
    """The credential check: an active user whose password verifies → a `Principal`, else None.

    A disabled user fails even with the right password. Every path runs exactly one argon2 verify
    (against a dummy hash when the user is absent/ineligible), so response time never reveals whether
    a username exists — closing a timing-enumeration oracle. On success, opportunistically upgrade
    the stored hash if the cost profile has risen (the cross-tier cost-bump path)."""
    user = get_user(store, username)
    # A passwordless (OIDC-only) user has a NULL hash and can never password-login; verify against the
    # dummy hash so the timing is identical to a missing/active-password user.
    stored_hash = user["password_hash"] if (user and user["password_hash"]) else _DUMMY_HASH
    verified = verify_password(stored_hash, password)
    if user is None or user["status"] != _ACTIVE or user["password_hash"] is None or not verified:
        return None
    if needs_rehash(user["password_hash"]):
        try:
            store.execute(
                "UPDATE users SET password_hash = ? WHERE username = ?",
                (hash_password(password), username),
            )
            store.commit()
        except Exception:
            # Best-effort upgrade — a DB hiccup on the opportunistic rehash must not fail an
            # otherwise-valid login.
            pass
    return Principal(subject=username)


def authenticate_with_own_store(username: str, password: str) -> Principal | None:
    """`authenticate`, but opening (and closing) its OWN Store — so an async handler can run the whole
    credential check (DB reads + the ~50-100 ms argon2 verify) in a worker thread via `run_blocking`
    without sharing its event-loop Store across threads (SQLite forbids cross-thread connection use).
    Returns None when no datastore is configured (the login handlers treat that as a failed login)."""
    store = Store.from_env()
    if store is None:
        return None
    try:
        return authenticate(store, username, password)
    finally:
        store.close()


def seed_admin_from_env(store: Store) -> str | None:
    """Seed the initial admin from the AGAMI_ADMIN_* env. Returns the seeded username (the email), or
    None if there's nothing to seed / the admin already exists.

    The admin is keyed by **email** (`AGAMI_ADMIN_USERNAME` = the email; `username` = the normalized
    email, so login is by the address they know). Auth method is whatever the deploy supplies — a
    password (`AGAMI_ADMIN_PASSWORD`) and/or a pinned OIDC provider (`AGAMI_ADMIN_PROVIDER`,
    google|microsoft); **at least one is required**, and both is fine (the provider is the primary path,
    the password a fallback). `AGAMI_ADMIN_FIRST_NAME`/`LAST_NAME` are display-only.

    Create-if-absent and idempotent: a redeploy never duplicates the admin or clobbers changes the
    admin has since made. (Switching an existing admin's auth method is a manual re-seed — out of scope.)"""
    raw = os.environ.get("AGAMI_ADMIN_USERNAME", "").strip()
    email = _normalize_email(raw)
    if email is None:
        return None
    # A whitespace-only password is a misconfig — treat it as unset (so it falls back to the provider,
    # or no-ops) rather than letting create_user raise and crash startup.
    pw_env = os.environ.get("AGAMI_ADMIN_PASSWORD", "")
    password = pw_env if pw_env.strip() else None
    provider = os.environ.get("AGAMI_ADMIN_PROVIDER", "").strip().lower() or None
    # An unknown provider key is a misconfig — ignore it (fall back to the password if present) rather
    # than seed an admin pinned to a provider that can never resolve.
    if provider is not None and provider not in _KNOWN_OIDC_PROVIDERS:
        provider = None
    if password is None and provider is None:
        return None  # no usable credential → nothing to seed
    # Idempotent across an upgrade: skip if an admin already exists under the normalized email OR under
    # a pre-existing (possibly mixed-case) raw username — so a redeploy never creates a duplicate row.
    if get_user(store, email) is not None or (raw != email and get_user(store, raw) is not None):
        return None
    create_user(
        store,
        username=email,
        password=password,
        email=email,
        status=_ACTIVE,
        oidc_provider=provider,
        first_name=os.environ.get("AGAMI_ADMIN_FIRST_NAME", ""),
        last_name=os.environ.get("AGAMI_ADMIN_LAST_NAME", ""),
    )
    return email
