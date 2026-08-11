"""Teammate self-onboarding via the admin setup link (the password-deployment path).

Proves the setup token is unforgeable + single-use, the /claim flow sets a password for a pending user
only, and the admin roster surfaces a copy-able setup link for pending users — but only when the
deployment has no OIDC configured. SQLite-backed; https base so the Secure admin cookie round-trips.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("starlette")
pytest.importorskip("mcp")
pytest.importorskip("jwt")
pytest.importorskip("argon2")

PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import jwt  # noqa: E402
import mcp_http  # noqa: E402
import oauth_server  # noqa: E402
import onboarding  # noqa: E402
import user_store  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402
from store import Store  # noqa: E402

BASE = "https://your-host.example.com"
SECRET = "x" * 40
ADMIN_USER = "admin@example.com"
ADMIN_PW = "admin-password-localtest"
PENDING = "newbie@example.com"

# The password a refused attempt sends. A whole English phrase rather than a credential-shaped string
# — the convention `tests/e2e/test_member_onboarding.py` already states for its own passphrase — and
# here it is load-bearing rather than stylistic: the credential-shaped version tripped the secret scan
# in CI on four lines. It only has to clear agami-core's eight-character floor and never be accepted.
# **Every password in the reset tests is an English phrase, not a credential-shaped string**, and
# that is a requirement rather than a style: the credential-shaped versions tripped two independent
# secret scanners in CI. They are synthetic either way — what changes is whether a scanner can tell.
# The convention (and the reason) is already stated in `tests/e2e/test_member_onboarding.py`. Each
# only has to clear agami-core's eight-character floor.
REFUSED_ATTEMPT = "the password this request must not be allowed to set"
CHOSEN_AT_RESET = "the passphrase chosen when the reset link was used"
REPLAYED_ATTEMPT = "the passphrase a replayed link would try to set"
SET_OUT_OF_BAND = "the passphrase set by some route other than the link"
ALREADY_HELD = "the passphrase this account already had"


@pytest.fixture
def env(tmp_path, monkeypatch):
    db_url = "sqlite://" + str(tmp_path / "onboard.db")
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("AGAMI_DB_URL", db_url)
    monkeypatch.setenv("AGAMI_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("AGAMI_ADMIN_USERNAME", ADMIN_USER)
    monkeypatch.setenv("AGAMI_ADMIN_PASSWORD", ADMIN_PW)
    # A password deployment — no OIDC configured.
    for var in (
        "AGAMI_OIDC_GOOGLE_CLIENT_ID",
        "AGAMI_OIDC_GOOGLE_CLIENT_SECRET",
        "AGAMI_OIDC_MICROSOFT_CLIENT_ID",
        "AGAMI_OIDC_MICROSOFT_CLIENT_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)
    s = Store.connect(db_url)
    s.run_migrations()
    user_store.seed_admin_from_env(s)
    user_store.create_user(s, username=PENDING, email=PENDING, password=None)  # pending teammate
    s.close()
    return db_url


@pytest.fixture
def client(env):
    return TestClient(mcp_http.build_app(), base_url=BASE)


# --- the token in isolation --------------------------------------------------


def test_setup_token_round_trips(env):
    token = onboarding.mint_setup_token(PENDING)
    assert onboarding.verify_setup_token(token) == PENDING


def test_setup_token_rejects_forged_expired_and_wrong_purpose(env):
    assert onboarding.verify_setup_token("not-a-jwt") is None
    # signed with a different key → bad signature
    forged = jwt.encode(
        {"sub": PENDING, "purpose": "setup", "exp": 9_999_999_999}, "y" * 40, algorithm="HS256"
    )
    assert onboarding.verify_setup_token(forged) is None
    # expired
    expired = jwt.encode(
        {
            "sub": PENDING,
            "purpose": "setup",
            "exp": int(datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp()),
        },
        SECRET,
        algorithm="HS256",
    )
    assert onboarding.verify_setup_token(expired) is None
    # right key, wrong purpose (e.g. a bearer/admin-session token replayed as a setup link)
    wrong = jwt.encode(
        {"sub": PENDING, "purpose": "admin_session", "exp": 9_999_999_999},
        SECRET,
        algorithm="HS256",
    )
    assert onboarding.verify_setup_token(wrong) is None


# --- the /claim flow ---------------------------------------------------------


def test_claim_sets_a_password_for_a_pending_user(client, env):
    token = onboarding.mint_setup_token(PENDING)
    assert "Set up your account" in client.get("/claim", params={"token": token}).text
    r = client.post("/claim", data={"token": token, "password": "teammate-pw-123"})
    assert r.status_code == 200 and f"{BASE}/mcp" in r.text
    s = Store.connect(env)
    assert user_store.authenticate(s, PENDING, "teammate-pw-123") is not None
    s.close()


def test_claim_link_is_single_use(client, env):
    token = onboarding.mint_setup_token(PENDING)
    client.post("/claim", data={"token": token, "password": "teammate-pw-123"})
    # replay: the user is no longer pending → generic invalid page, password unchanged
    r = client.post("/claim", data={"token": token, "password": REPLAYED_ATTEMPT})
    assert r.status_code == 400 and "isn't valid" in r.text
    s = Store.connect(env)
    assert (
        user_store.authenticate(s, PENDING, "teammate-pw-123") is not None
    )  # original still works
    assert user_store.authenticate(s, PENDING, REPLAYED_ATTEMPT) is None
    s.close()


def test_the_claim_page_says_whose_account_it_is(client, env):
    """A link is shared out-of-band, so the person holding it may have been sent the wrong one — or
    two. While the only link set a FIRST password that was tolerable; a reset link replaces a working
    one, and following the wrong one locks somebody out of their own account.

    Asserted on both purposes and on the re-render after a rejected password, because that last one is
    the render most easily forgotten and the one somebody is staring at when they are confused.
    """
    setup = onboarding.mint_setup_token(PENDING)
    assert PENDING in client.get("/claim", params={"token": setup}).text
    reset = _reset_token(env, ADMIN_USER)
    assert ADMIN_USER in client.get("/claim", params={"token": reset}).text
    # ...and when the password is refused for being too short.
    again = client.post("/claim", data={"token": setup, "password": "short"})
    assert again.status_code == 400 and PENDING in again.text


def test_claim_rejects_a_bad_token(client):
    assert client.get("/claim", params={"token": "nope"}, follow_redirects=False).status_code == 400
    assert (
        client.post("/claim", data={"token": "nope", "password": "whatever-123"}).status_code == 400
    )


def test_claim_rejects_a_short_password(client, env):
    token = onboarding.mint_setup_token(PENDING)
    r = client.post("/claim", data={"token": token, "password": "short"})
    assert r.status_code == 400 and "at least" in r.text
    s = Store.connect(env)
    assert onboarding.is_pending(user_store.get_user(s, PENDING))  # still pending — nothing set
    s.close()


def test_claim_for_an_already_password_user_is_refused(client, env):
    # The admin (a password user) isn't pending; a token for them can't overwrite their password.
    token = onboarding.mint_setup_token(ADMIN_USER)
    assert client.get("/claim", params={"token": token}).status_code == 400


# --- the RESET link ----------------------------------------------------------
#
# A reset link overwrites a credential somebody is using today, which the setup link never does. So
# these press on the four things that keep that safe: it works only on the account state it was minted
# for, it is spent by use, it can never reach an SSO or a switched-off account, and it ends the
# sessions running on the old password.


def _reset_token(env, username: str) -> str:
    """A reset link's token, minted the way the console mints one — against the credential the account
    holds at this moment."""
    s = Store.connect(env)
    try:
        return onboarding.mint_reset_token(
            username, user_store.credential_fingerprint(user_store.get_user(s, username))
        )
    finally:
        s.close()


def test_reset_token_and_setup_token_are_not_interchangeable(env):
    # The property the two purposes exist for. A setup token names a pending account; a reset token
    # names a claimed one; neither verifier accepts the other's token, so neither link can do the
    # other's job even before the store is consulted.
    setup, reset = onboarding.mint_setup_token(PENDING), _reset_token(env, ADMIN_USER)
    assert onboarding.verify_setup_token(reset) is None
    assert onboarding.verify_reset_token(setup) is None
    assert onboarding.verify_reset_token(reset)[0] == ADMIN_USER


def test_reset_link_sets_a_new_password_for_a_claimed_user(client, env):
    token = _reset_token(env, ADMIN_USER)
    # Worded for somebody who HAS an account — being told to "finish setting up" would read as though
    # theirs had been wiped.
    assert "Choose a new password" in client.get("/claim", params={"token": token}).text
    assert (
        client.post("/claim", data={"token": token, "password": CHOSEN_AT_RESET}).status_code == 200
    )
    s = Store.connect(env)
    assert user_store.authenticate(s, ADMIN_USER, CHOSEN_AT_RESET) is not None
    assert user_store.authenticate(s, ADMIN_USER, ADMIN_PW) is None  # the old one is gone
    s.close()


def test_reset_link_is_single_use(client, env):
    # Nothing about the ROW changes state here the way a claim flips `pending`, so the only thing
    # retiring this link is the credential marker in the token. If that check regressed, this replay
    # would succeed and an administrator's old link would stay live forever.
    token = _reset_token(env, ADMIN_USER)
    client.post("/claim", data={"token": token, "password": CHOSEN_AT_RESET})
    replay = client.post("/claim", data={"token": token, "password": REPLAYED_ATTEMPT})
    assert replay.status_code == 400 and "isn't valid" in replay.text
    s = Store.connect(env)
    assert user_store.authenticate(s, ADMIN_USER, CHOSEN_AT_RESET) is not None
    assert user_store.authenticate(s, ADMIN_USER, REPLAYED_ATTEMPT) is None
    s.close()


def test_a_reset_link_dies_when_the_password_moves_by_any_other_route(client, env):
    # The marker is bound to the credential, not to this request — so a link minted before somebody
    # changed their password elsewhere is already spent when it arrives.
    token = _reset_token(env, ADMIN_USER)
    s = Store.connect(env)
    user_store.reset_password(
        s, ADMIN_USER, SET_OUT_OF_BAND, user_store.get_user(s, ADMIN_USER)["password_hash"]
    )
    s.close()
    assert client.get("/claim", params={"token": token}).status_code == 400


def test_a_reset_token_cannot_act_on_a_pending_account(client, env):
    # The mirror of `test_claim_for_an_already_password_user_is_refused`, and the reason the purposes
    # are matched against the row's state rather than trusted from the token. The marker is hand-made:
    # there is no legitimate way to mint one for a pending account, which is the next test.
    forged_for_pending = onboarding.mint_reset_token(PENDING, "deadbeefdeadbeef")
    assert client.get("/claim", params={"token": forged_for_pending}).status_code == 400
    assert (
        client.post(
            "/claim", data={"token": forged_for_pending, "password": REFUSED_ATTEMPT}
        ).status_code
        == 400
    )


def test_a_credential_marker_cannot_be_taken_for_an_account_with_no_password(env):
    """The marker for a passwordless row would otherwise be `sha256("")` — one constant, identical for
    every such row in every deployment, so the binding that makes a reset link single-use would be a
    binding to a publicly known value exactly where the account is least protected. Raising means there
    is no way to mint one at all, rather than a way that quietly means nothing."""
    s = Store.connect(env)
    try:
        with pytest.raises(ValueError):
            user_store.credential_fingerprint(user_store.get_user(s, PENDING))
        # ...while a claimed account gives one, and it changes when the credential does.
        before = user_store.credential_fingerprint(user_store.get_user(s, ADMIN_USER))
        user_store.reset_password(
            s, ADMIN_USER, CHOSEN_AT_RESET, user_store.get_user(s, ADMIN_USER)["password_hash"]
        )
        assert user_store.credential_fingerprint(user_store.get_user(s, ADMIN_USER)) != before
    finally:
        s.close()


def test_a_reset_never_gives_an_sso_identity_a_password(client, env):
    # An SSO account has no password by design. A reset that added one would be a second way in that
    # the deployment never chose and the account holder would never know about.
    s = Store.connect(env)
    user_store.create_user(
        s,
        username="sso@example.com",
        email="sso@example.com",
        password=None,
        oidc_provider="google",
        oidc_subject="sub-123",
    )
    s.close()
    # Hand-made marker again — the account has no credential to mint one against.
    token = onboarding.mint_reset_token("sso@example.com", "deadbeefdeadbeef")
    # The GET matters as much as the POST: drawing the form and refusing the submit is the split this
    # whole resolver exists to avoid.
    assert client.get("/claim", params={"token": token}).status_code == 400
    assert (
        client.post("/claim", data={"token": token, "password": REFUSED_ATTEMPT}).status_code == 400
    )
    s = Store.connect(env)
    assert user_store.get_user(s, "sso@example.com")["password_hash"] is None
    s.close()


def test_a_reset_is_refused_once_an_account_has_bound_an_identity_provider(client, env):
    """The case that ISOLATES the `oidc_provider IS NULL` guard, and the reachable one.

    The test above does not: that account has no password either, so `password_hash IS NOT NULL`
    refuses it first and the OIDC guard is never consulted — removing the guard leaves that test
    green. This is the state where only the guard stands in the way: somebody who signed in with a
    password and has since bound a provider (`bind_oidc_subject`, on their first OIDC login), so the
    row carries BOTH. Their way in is now the provider, and a reset would quietly re-arm the password
    that is no longer how they get in.
    """
    s = Store.connect(env)
    user_store.create_user(
        s, username="both@example.com", email="both@example.com", password=ALREADY_HELD
    )
    user_store.bind_oidc_subject(s, "both@example.com", "sub-456")
    s.execute(
        "UPDATE users SET oidc_provider = ? WHERE username = ?", ("google", "both@example.com")
    )
    s.commit()
    token = onboarding.mint_reset_token(
        "both@example.com",
        user_store.credential_fingerprint(user_store.get_user(s, "both@example.com")),
    )
    s.close()
    # 400 on the GET too. When it was 200 the page was drawn for an account the write would refuse, so
    # somebody chose a password, submitted it and was told the link was invalid — and, because such a
    # token can never spend, every attempt paid for an argon2 hash on a public endpoint.
    assert client.get("/claim", params={"token": token}).status_code == 400
    assert (
        client.post("/claim", data={"token": token, "password": REFUSED_ATTEMPT}).status_code == 400
    )
    s = Store.connect(env)
    assert user_store.authenticate(s, "both@example.com", REFUSED_ATTEMPT) is None
    s.close()


def test_a_reset_cannot_revive_a_switched_off_account(client, env):
    # Switching an account off is a security decision; a link that undid it would be the stronger of
    # the two, and it is held by whoever was forwarded it.
    token = _reset_token(env, ADMIN_USER)
    s = Store.connect(env)
    user_store.set_status(s, ADMIN_USER, "disabled")
    s.close()
    # The GET matters on its own: without the handler's own check the page would be DRAWN for a
    # switched-off account and only the write would refuse, so somebody would choose a password, submit
    # it, and be told the link is invalid. Refusing here is the difference between the two layers.
    assert client.get("/claim", params={"token": token}).status_code == 400
    assert (
        client.post("/claim", data={"token": token, "password": CHOSEN_AT_RESET}).status_code == 400
    )
    s = Store.connect(env)
    assert user_store.authenticate(s, ADMIN_USER, CHOSEN_AT_RESET) is None
    s.close()


def test_reset_password_refuses_in_its_own_where_clause(env):
    """`user_store.reset_password`'s guards, exercised directly rather than through a page.

    **Not redundant with the tests above, and finding that out is why this exists.** `_actionable`
    checks the same conditions first, so through `/claim` these UPDATEs are never reached with a row
    that should be refused — deleting a condition from the WHERE left every page test green. A guard
    nothing can fail is a guard nothing is holding, and this one is the last line before a live
    credential is overwritten.
    """
    s = Store.connect(env)
    try:
        # Pending: no password to replace. This is `claim_pending_password`'s job, not this one's.
        assert user_store.reset_password(s, PENDING, CHOSEN_AT_RESET, "whatever") == 0
        # Bound to an identity provider: their way in is the provider now.
        user_store.create_user(
            s, username="bound@example.com", email="bound@example.com", password=ALREADY_HELD
        )
        s.execute(
            "UPDATE users SET oidc_provider = ? WHERE username = ?", ("google", "bound@example.com")
        )
        s.commit()
        bound = user_store.get_user(s, "bound@example.com")["password_hash"]
        assert user_store.reset_password(s, "bound@example.com", CHOSEN_AT_RESET, bound) == 0
        # Switched off: the account being off outranks the link.
        user_store.create_user(
            s, username="off@example.com", email="off@example.com", password=ALREADY_HELD
        )
        user_store.set_status(s, "off@example.com", "disabled")
        off = user_store.get_user(s, "off@example.com")["password_hash"]
        assert user_store.reset_password(s, "off@example.com", CHOSEN_AT_RESET, off) == 0
        # ...and the one it is for. `stale` is captured first so the replay below can present it.
        stale = user_store.get_user(s, ADMIN_USER)["password_hash"]
        assert user_store.reset_password(s, ADMIN_USER, CHOSEN_AT_RESET, stale) == 1
        assert user_store.authenticate(s, ADMIN_USER, CHOSEN_AT_RESET) is not None
        # **The race.** A second post of the same link presents the hash it read before the first
        # one landed. Without `password_hash = ?` in the WHERE both writes succeed and the later
        # one owns the account, while the person who legitimately reset is told it worked.
        assert user_store.reset_password(s, ADMIN_USER, REPLAYED_ATTEMPT, stale) == 0
        assert user_store.authenticate(s, ADMIN_USER, CHOSEN_AT_RESET) is not None
    finally:
        s.close()


def test_a_reset_revokes_the_sessions_running_on_the_old_password(client, env):
    # Otherwise the reset is one in name only: whoever was signed in keeps renewing indefinitely on a
    # credential that has been taken away from them.
    s = Store.connect(env)
    s.execute(
        "INSERT INTO oauth_refresh_token (token_hash, family, client_id, username, expires_at, revoked, created) "
        "VALUES (?, ?, ?, ?, ?, 0, ?)",
        ("hash-1", "fam-1", "client-1", ADMIN_USER, "2099-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )
    # A second principal's live token, to prove the revocation is scoped to one person.
    s.execute(
        "INSERT INTO oauth_refresh_token (token_hash, family, client_id, username, expires_at, revoked, created) "
        "VALUES (?, ?, ?, ?, ?, 0, ?)",
        ("hash-2", "fam-2", "client-1", PENDING, "2099-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )
    s.commit()
    s.close()
    token = _reset_token(env, ADMIN_USER)
    assert (
        client.post("/claim", data={"token": token, "password": CHOSEN_AT_RESET}).status_code == 200
    )
    s = Store.connect(env)
    assert (
        s.query("SELECT revoked FROM oauth_refresh_token WHERE token_hash = ?", ("hash-1",))[0][
            "revoked"
        ]
        == 1
    )
    assert (
        s.query("SELECT revoked FROM oauth_refresh_token WHERE token_hash = ?", ("hash-2",))[0][
            "revoked"
        ]
        == 0
    )
    # The count is the function's only output; asserting it is what stops it silently becoming 0.
    assert oauth_server.revoke_refresh_tokens_for(s, PENDING) == 1
    s.close()


def test_a_failed_reset_leaves_every_session_alone(client, env, monkeypatch):
    """The ordering: revocation happens only for a write that actually moved a row.

    **The obvious version of this test does not test it.** Posting a short password returns on the
    length check, before the store is opened at all — so the guard could be deleted and the test would
    still pass, which is what review found. The write has to be REACHED and refused, so the UPDATE is
    forced to match nothing: that is the lost-race outcome, where the credential moved between the
    check and the write.
    """
    s = Store.connect(env)
    s.execute(
        "INSERT INTO oauth_refresh_token (token_hash, family, client_id, username, expires_at, revoked, created) "
        "VALUES (?, ?, ?, ?, ?, 0, ?)",
        ("hash-3", "fam-3", "client-1", ADMIN_USER, "2099-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )
    s.commit()
    s.close()
    token = _reset_token(env, ADMIN_USER)
    monkeypatch.setattr(user_store, "reset_password", lambda *a, **k: 0)
    assert (
        client.post("/claim", data={"token": token, "password": CHOSEN_AT_RESET}).status_code == 400
    )
    s = Store.connect(env)
    assert (
        s.query("SELECT revoked FROM oauth_refresh_token WHERE token_hash = ?", ("hash-3",))[0][
            "revoked"
        ]
        == 0
    )
    s.close()


def test_claim_refuses_a_token_whose_purpose_is_not_one_of_the_two(client, env):
    """Wrong-purpose rejection, on the LIVE path.

    It was covered only against `verify_setup_token`, which the handler stopped calling when
    `_actionable` began decoding for itself — so the property still held and nothing was holding it:
    review mutated the purpose comparison so that an `admin_session` cookie JWT would act as a setup
    link, and every test stayed green. These post the tokens that actually exist in this deployment
    signed with the same secret, plus one carrying no purpose at all.
    """
    for purpose in ("admin_session", "", None, 7):
        claims = {"sub": PENDING, "exp": 9_999_999_999}
        if purpose is not None:
            claims["purpose"] = purpose
        token = jwt.encode(claims, SECRET, algorithm="HS256")
        assert client.get("/claim", params={"token": token}).status_code == 400, purpose
        assert (
            client.post("/claim", data={"token": token, "password": REFUSED_ATTEMPT}).status_code
            == 400
        ), purpose


def test_a_reset_token_with_a_malformed_marker_is_refused_rather_than_crashing(client, env):
    """A `cred` that is not a string reached `compare_digest` and raised, which is a 500 — the one
    response distinguishable from the uniform generic page, and therefore an oracle. The handler now
    goes through `verify_reset_token`, whose type check is what keeps this a 400."""
    for cred in (None, 7, ["x"]):
        claims = {"sub": ADMIN_USER, "purpose": "reset", "exp": 9_999_999_999}
        if cred is not None:
            claims["cred"] = cred
        token = jwt.encode(claims, SECRET, algorithm="HS256")
        assert client.get("/claim", params={"token": token}).status_code == 400, cred


# --- the admin roster setup link ---------------------------------------------


def _login(client):
    client.post("/admin/login", data={"username": ADMIN_USER, "password": ADMIN_PW})


def test_admin_roster_shows_a_setup_link_for_pending_users(client):
    _login(client)
    html = client.get("/admin").text
    assert "Setup link" in html
    m = re.search(r"/claim\?token=([\w.\-]+)", html)
    assert m and onboarding.verify_setup_token(m.group(1)) == PENDING
    # the admin's own (password) row offers no setup link
    admin_row = next(r for r in html.split("<tr>") if ADMIN_USER in r and "<td" in r)
    assert "Setup link" not in admin_row


def test_admin_roster_hides_setup_links_in_an_oidc_deployment(client, monkeypatch):
    # Configure an OIDC provider → it's no longer a password deployment → no setup links.
    monkeypatch.setenv("AGAMI_OIDC_GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("AGAMI_OIDC_GOOGLE_CLIENT_SECRET", "secret")
    _login(client)
    assert "Setup link" not in client.get("/admin").text
