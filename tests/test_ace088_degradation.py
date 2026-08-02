"""Every way the receipt can fail to be built, and the fact it reports instead.

The spec's whole premise is that silence reads as clean, so the paths where the receipt CANNOT be
assembled are the ones that most need a test. Each one has its own reason string, because "the
runtime is not installed", "no model was consulted" and "the assembler broke" are three different
facts and collapsing them would be the same defect one layer down.

These are the degradation branches specifically. The happy paths live in
`test_ace088_receipt_sections.py`, and the executed-versus-received property in
`test_ace088_executed_statement.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402
import guardrail  # noqa: E402
import tools  # noqa: E402

SQL = "SELECT id FROM orders"


@pytest.fixture()
def guard_model():
    """Set the request-scoped model the builder reads, and put it back.

    `_guard_model` is a ContextVar, so its `get` cannot be monkeypatched and a value left behind
    would follow the next test in this context. `execute_guarded` clears it at entry for the same
    reason: a model resolved for an earlier call must never be the one this call's receipt describes.
    """
    def _set(value):
        token = execute_sql._guard_model.set(value)
        tokens.append(token)

    tokens: list = []
    yield _set
    for token in reversed(tokens):
        execute_sql._guard_model.reset(token)


def _reasons(receipt: guardrail.Receipt) -> set[str | None]:
    return {getattr(receipt, name).undetermined for name in guardrail.Receipt.SECTIONS}


def _assert_wholly_undetermined(receipt: guardrail.Receipt, reason: str) -> None:
    """Every section carries the reason, and none of them is quietly clean."""
    assert _reasons(receipt) == {reason}
    assert all(getattr(receipt, name).items == () for name in guardrail.Receipt.SECTIONS)


# --- the chokepoint's builder -----------------------------------------------


def test_a_deployment_without_the_model_runtime_says_so(monkeypatch):
    """The vendored plugin mirror ships `guardrail.py` and `execute_sql.py` but no
    `semantic_model.runtime`, so the import inside the builder genuinely fails there. It must cost
    the caller its receipt, never its answer, and the receipt has to say which of the two it is."""
    monkeypatch.setitem(sys.modules, "semantic_model", None)

    receipt = execute_sql._receipt_for(SQL, "acme", refused=False)

    _assert_wholly_undetermined(receipt, execute_sql.RECEIPT_NO_RUNTIME)


def test_a_statement_that_consulted_no_model_says_so(guard_model):
    """Distinct from the one above: the runtime is right there, but nothing resolved a model for
    this call, so there was nothing to check the statement against."""
    guard_model(None)

    receipt = execute_sql._receipt_for(SQL, "acme", refused=False)

    _assert_wholly_undetermined(receipt, execute_sql.RECEIPT_NO_MODEL)


def test_an_assembler_that_raises_costs_the_receipt_and_not_the_answer(monkeypatch, guard_model):
    """A model that loaded and an assembler that then broke is a fault, so the stack goes to the
    server log. The caller still gets an Envelope, and a receipt that admits it has nothing."""
    runtime = pytest.importorskip("semantic_model.runtime")

    def boom(*_a, **_kw):
        raise RuntimeError("the assembler broke")

    guard_model(object())
    monkeypatch.setattr(runtime, "assemble_receipt", boom)

    receipt = execute_sql._receipt_for(SQL, "acme", refused=False)

    _assert_wholly_undetermined(receipt, execute_sql.RECEIPT_BUILD_FAILED)


def test_an_unpinnable_model_version_is_an_unpinned_receipt_not_a_failure(monkeypatch):
    """`tools` is absent from the vendored mirror, so the version resolver cannot be reached there.
    That is a receipt without a version pin, not a receipt that could not be built."""
    monkeypatch.setitem(sys.modules, "tools", None)

    assert execute_sql._receipt_model_version("acme") is None


# --- the fork path's builder ------------------------------------------------


def test_the_parent_reports_a_broken_assembler_rather_than_returning_none(monkeypatch):
    """SC-5, on the side of the fork that owns it. `_resolve_receipt` used to end
    `except Exception: return None`, which the caller could not tell apart from a clean statement.
    A receipt that could not be built is a fact, not an absence."""
    runtime = pytest.importorskip("semantic_model.runtime")

    def boom(*_a, **_kw):
        raise RuntimeError("the assembler broke")

    monkeypatch.setattr(tools, "get_cached_org", lambda _profile: object())
    monkeypatch.setattr(runtime, "assemble_receipt", boom)

    receipt = tools._resolve_receipt("acme", SQL)

    assert receipt is not None
    _assert_wholly_undetermined(receipt, tools.RECEIPT_UNAVAILABLE)


def test_a_datasource_with_no_model_at_all_is_an_ordinary_state(monkeypatch):
    """A bare local install has no model yet. That is not a server-log event, and the fact travels
    to the caller on the receipt rather than vanishing into a `None`."""

    def no_model(_profile):
        raise FileNotFoundError("no model here")

    monkeypatch.setattr(tools, "get_cached_org", no_model)

    _assert_wholly_undetermined(tools._resolve_receipt("acme", SQL), tools.RECEIPT_UNAVAILABLE)


def test_the_four_reasons_are_four_different_sentences():
    """Collapsing any two of these would reintroduce the defect one layer down: a caller reading
    "could not be established" cannot act, while "the runtime is not installed in this deployment"
    tells them exactly what to change."""
    reasons = {
        execute_sql.RECEIPT_NO_RUNTIME,
        execute_sql.RECEIPT_NO_MODEL,
        execute_sql.RECEIPT_BUILD_FAILED,
        tools.RECEIPT_UNAVAILABLE,
    }
    assert len(reasons) == 4
    assert all(r.strip().endswith(".") for r in reasons), "they surface next to an answer"


# --- the refusal assembler's own early returns ------------------------------


def test_a_refusal_on_an_unparseable_statement_still_returns_a_reasoned_receipt():
    """The two meet: `RULE_UNPARSEABLE` refuses a statement precisely because sqlglot could not read
    it, so the refusal receipt is asked to describe something it cannot parse. It must say that,
    rather than hand back five empty sections that read as "we looked and it was clean"."""
    runtime = pytest.importorskip("semantic_model.runtime")

    # `;;;` rather than a merely wrong statement: sqlglot parses most malformed SQL into something
    # when `error_level="ignore"`, and this returns a genuine `None` tree.
    assembled = runtime.assemble_refusal_receipt(object(), ";;;", model_version="v1")

    assert assembled["model_version"] == "v1"
    reasons = {s["undetermined"] for s in assembled["sections"].values()}
    assert reasons == {runtime.UNDETERMINED_UNPARSEABLE}
    assert all(s["items"] == [] for s in assembled["sections"].values())


def test_a_refusal_without_a_parser_says_that_instead(monkeypatch):
    """A deployment with no sqlglot cannot establish anything about any statement, refused or not,
    and that is a different sentence from "this statement would not parse"."""
    runtime = pytest.importorskip("semantic_model.runtime")
    monkeypatch.setattr(runtime, "_HAVE_SQLGLOT", False)

    assembled = runtime.assemble_refusal_receipt(object(), "SELECT id FROM orders")

    reasons = {s["undetermined"] for s in assembled["sections"].values()}
    assert reasons == {runtime.UNDETERMINED_NO_PARSER}
    assert runtime.UNDETERMINED_NO_PARSER != runtime.UNDETERMINED_UNPARSEABLE
