"""Moving people who already have password accounts onto an identity provider.

**The situation this exists for.** A deployment has been running on usernames and passwords; people
have accounts, conversations, roles and an audit trail. It now moves to single sign-on. Those people
must keep everything they have, and `claim_pending_oidc` cannot help — it fires only on an account
nobody has used yet, so every existing person would be refused at sign-in.

Two properties carry the weight here:

  * **the row survives**, so everything keyed on that address survives with it; and
  * **the password does not**, because a deployment that says the directory decides cannot leave a
    way around the directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

from store import MIGRATIONS_DIR, Store  # noqa: E402
from user_store import (  # noqa: E402
    authenticate,
    create_user,
    get_user,
    migrate_password_user_to_oidc,
    set_user_names,
)

ADDRESS = "someone@example.com"
THEIR_PASSWORD = "the-password-they-chose-in-july"


@pytest.fixture
def store(tmp_path):
    s = Store.connect("sqlite://" + str(tmp_path / "users.db"))
    s.run_migrations(MIGRATIONS_DIR)
    yield s
    s.close()


def _a_person_who_has_been_using_passwords(store, address: str = ADDRESS) -> None:
    create_user(store, username=address, password=THEIR_PASSWORD, email=address, status="active")
    store.commit()


# --- the migration ------------------------------------------------------------------------------


def test_an_existing_password_account_is_adopted_rather_than_refused(store):
    _a_person_who_has_been_using_passwords(store)

    assert migrate_password_user_to_oidc(store, ADDRESS, "microsoft", "subject-1") == 1

    row = get_user(store, ADDRESS)
    assert row["oidc_provider"] == "microsoft"
    assert row["oidc_subject"] == "subject-1"


def test_it_is_the_same_row_so_everything_keyed_on_it_survives(store):
    """The whole reason for adopting rather than creating: conversations, memberships and audit lines
    are keyed on the address, and a new account would orphan every one of them."""
    _a_person_who_has_been_using_passwords(store)
    before = get_user(store, ADDRESS)

    migrate_password_user_to_oidc(store, ADDRESS, "microsoft", "subject-1")

    after = get_user(store, ADDRESS)
    assert after["id"] == before["id"]
    assert after["username"] == before["username"]
    assert after["created"] == before["created"]
    assert len(store.query("SELECT * FROM users")) == 1


def test_the_password_no_longer_works(store):
    """**The point, not tidiness.** Left in place, revoking somebody in the directory would not stop
    them signing in with the password they chose months ago, and nothing would show it."""
    _a_person_who_has_been_using_passwords(store)
    assert authenticate(store, ADDRESS, THEIR_PASSWORD) is not None  # it works beforehand

    migrate_password_user_to_oidc(store, ADDRESS, "microsoft", "subject-1")

    assert get_user(store, ADDRESS)["password_hash"] is None
    assert authenticate(store, ADDRESS, THEIR_PASSWORD) is None


def test_adopting_twice_is_harmless(store):
    """Every sign-in runs this. It must be the same on the second one as on the first."""
    _a_person_who_has_been_using_passwords(store)
    migrate_password_user_to_oidc(store, ADDRESS, "microsoft", "subject-1")
    assert migrate_password_user_to_oidc(store, ADDRESS, "microsoft", "subject-1") == 1
    assert get_user(store, ADDRESS)["oidc_subject"] == "subject-1"


# --- and what it must never do -------------------------------------------------------------------


def test_an_account_bound_to_another_provider_is_not_taken(store):
    """The guard. Without it, a second identity provider could claim somebody by presenting the same
    address — which is the confusion `_resolve_oidc_user`'s provider binding exists to close."""
    create_user(
        store,
        username=ADDRESS,
        password=None,
        email=ADDRESS,
        status="active",
        oidc_provider="google",
        oidc_subject="google-subject",
    )
    store.commit()

    assert migrate_password_user_to_oidc(store, ADDRESS, "microsoft", "subject-1") == 0

    row = get_user(store, ADDRESS)
    assert row["oidc_provider"] == "google"
    assert row["oidc_subject"] == "google-subject"


def test_an_account_that_does_not_exist_is_not_created(store):
    assert migrate_password_user_to_oidc(store, "nobody@example.com", "microsoft", "s") == 0
    assert store.query("SELECT * FROM users") == []


def test_one_persons_migration_does_not_touch_another(store):
    _a_person_who_has_been_using_passwords(store)
    _a_person_who_has_been_using_passwords(store, "other@example.com")

    migrate_password_user_to_oidc(store, ADDRESS, "microsoft", "subject-1")

    other = get_user(store, "other@example.com")
    assert other["oidc_provider"] is None
    assert other["password_hash"] is not None


# --- filling in the name --------------------------------------------------------------------------


def test_a_name_is_filled_in_from_the_provider(store):
    _a_person_who_has_been_using_passwords(store)
    assert get_user(store, ADDRESS)["first_name"] is None

    set_user_names(store, ADDRESS, "Ada", "Lovelace")

    row = get_user(store, ADDRESS)
    assert (row["first_name"], row["last_name"]) == ("Ada", "Lovelace")


def test_a_provider_that_sends_nothing_does_not_erase_a_name(store):
    """**Silence is not an instruction to delete.** A provider that simply omits a name would
    otherwise wipe one an administrator typed in, on the next sign-in, with nothing to point at."""
    _a_person_who_has_been_using_passwords(store)
    set_user_names(store, ADDRESS, "Ada", "Lovelace")

    assert set_user_names(store, ADDRESS, "", "") == 0

    row = get_user(store, ADDRESS)
    assert (row["first_name"], row["last_name"]) == ("Ada", "Lovelace")


def test_half_a_name_updates_only_that_half(store):
    _a_person_who_has_been_using_passwords(store)
    set_user_names(store, ADDRESS, "Ada", "Lovelace")

    set_user_names(store, ADDRESS, "Augusta", "")

    row = get_user(store, ADDRESS)
    assert (row["first_name"], row["last_name"]) == ("Augusta", "Lovelace")


def test_a_corrected_name_replaces_the_old_one(store):
    """The directory is authoritative when it speaks — that is what makes a name change there reach
    the product without anybody retyping it."""
    _a_person_who_has_been_using_passwords(store)
    set_user_names(store, ADDRESS, "Ada", "Byron")

    set_user_names(store, ADDRESS, "Ada", "Lovelace")

    assert get_user(store, ADDRESS)["last_name"] == "Lovelace"


def test_whitespace_is_not_a_name(store):
    _a_person_who_has_been_using_passwords(store)
    assert set_user_names(store, ADDRESS, "   ", "\t") == 0
    assert get_user(store, ADDRESS)["first_name"] is None


# --- and the one thing that made testing any of this locally impossible --------------------------


@pytest.mark.parametrize(
    "base, allowed",
    [
        ("https://agami.example.com", True),
        ("https://localhost:5173", True),
        # Loopback over plain http: a secure context by the browser's own definition, so a `Secure`
        # cookie survives — and the only redirect an identity provider will accept from a laptop.
        ("http://localhost:5173", True),
        ("http://127.0.0.1:8080", True),
        ("http://[::1]:8080", True),
        # Everything else stays refused. The third of these is the one a prefix match would have let
        # through: it is somebody else's domain that merely begins with the right letters.
        ("http://agami.example.com", False),
        ("http://192.168.1.10:8080", False),
        ("http://localhost.example.com", False),
        ("http://notlocalhost", False),
    ],
)
def test_which_base_urls_may_be_plain_http(base, allowed):
    """TLS is required because a browser drops a `Secure` cookie over http, which would break the
    admin session silently. Loopback is exempt because the browser makes the same exception — not
    because the rule is being relaxed."""
    from mcp_http import _is_loopback

    assert (base.startswith("https://") or _is_loopback(base)) is allowed
    # ...and asked directly, because the `or` above short-circuits on every https case and would
    # leave the answer for those untested. An https address is not loopback-over-http, whatever its
    # host: `https://localhost` is allowed by the line above, not by this function.
    assert _is_loopback(base) is (base.startswith("http://") and allowed)
