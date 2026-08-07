"""The guard that decides whether a missing prerequisite is a skip or a failure.

`importorfail` exists because `pytest` exits 0 when every test skips. A job whose whole purpose is
to execute the DB-backed half of this corpus can therefore lose its driver, skip every test it owns
and report green — which is exactly the class of silent hole the corpus was built to close. So the
guard gets its own direct test rather than being exercised only through its caller: a guard proved
solely by the thing it guards is proved by nothing when that thing is the part that vanished.

Four cases, because the helper has two axes and both matter: sentinel set or unset, module present
or absent. The unset half must keep behaving like `pytest.importorskip`, or a developer without a
Postgres driver can no longer run the suite.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import itdeps  # noqa: E402

# A module name nothing will ever provide, so "missing" is a fact about the environment rather than
# a guess about which optional dependency happens not to be installed on this runner.
ABSENT = "agami_module_that_does_not_exist"


def test_a_present_module_is_no_event_either_way(monkeypatch):
    """The helper is a gate, not a loader: when the import works it gets out of the way."""
    monkeypatch.delenv(itdeps.REQUIRED, raising=False)
    itdeps.importorfail("json", "pathlib")

    monkeypatch.setenv(itdeps.REQUIRED, "1")
    itdeps.importorfail("json", "pathlib")


def test_without_the_sentinel_a_missing_module_skips(monkeypatch):
    """The local default. A developer with no Postgres driver still gets a usable suite."""
    monkeypatch.delenv(itdeps.REQUIRED, raising=False)

    with pytest.raises(pytest.skip.Exception) as caught:
        itdeps.importorfail(ABSENT)

    assert ABSENT in str(caught.value)


def test_with_the_sentinel_a_missing_module_fails_instead_of_skipping(monkeypatch):
    """The whole point. A run that DECLARED it must execute the DB proof cannot skip its way to
    green when the prerequisite for that proof is gone."""
    monkeypatch.setenv(itdeps.REQUIRED, "1")

    with pytest.raises(pytest.fail.Exception) as caught:
        itdeps.importorfail(ABSENT)

    assert ABSENT in str(caught.value)
    # A skip and a failure are different exceptions and the caller's exit code turns on which one
    # this is. Asserting the failure is not ALSO a skip is what keeps the two from being conflated.
    assert not isinstance(caught.value, pytest.skip.Exception)


def test_the_first_missing_module_decides(monkeypatch):
    """Several names, one verdict: the guard stops at the first thing it cannot import rather than
    reporting the last, so the message names the dependency that is actually absent."""
    monkeypatch.setenv(itdeps.REQUIRED, "1")

    with pytest.raises(pytest.fail.Exception) as caught:
        itdeps.importorfail(ABSENT, "json")

    assert ABSENT in str(caught.value)
