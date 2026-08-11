"""A capability the server has and the surface never mentions is a capability no client uses.

Four things shipped working and undocumented, which is a worse failure than shipping them broken:
a broken feature gets reported, a silent one just never gets called.

  - `execute_sql` returns three statuses and the description named two. The third, `failed`, is the
    whole database-error channel, carrying a `kind` from a declared ten-value enum. An agent that
    has never been told the shape exists meets it for the first time in the one situation where it
    is least able to reason about it, and improvises instead of retrying.
  - `list_datasources` has always returned `database_type` and `table_count`, and described
    neither. So the dialect was guessed on every non-metric expression, and the schema call was
    made blind to how big the answer would be.
  - `get_datasource_schema` ships each table's declared `default_filters` in its payload and its
    description named none of them. A declared filter was therefore first MET on the receipt —
    after the statement it belonged in had already run.

The tests derive from what the code emits rather than restating the strings, for the reason
`test_hosted_instruction_truth` gives about `binding`: a literal-vs-literal assertion passes right
up until someone renames the thing, and then it passes anyway. Add a `FailureKind` and this file is
what tells you the surface still lists nine.
"""

from __future__ import annotations

import ast
import inspect
from typing import get_args

import pytest

pytest.importorskip("pydantic")

import guardrail  # noqa: E402
import tools  # noqa: E402


def _emits_key(source: str, key: str) -> bool:
    """Does `source` build a dict entry under `key`?

    Quote-agnostic on purpose. Matching only `"key"` would make a no-op restyle to single quotes
    fail a test about payload SHAPE, which is not what any of these are asserting. `ruff format`
    would put the double quotes back, but a test that depends on the formatter to stay true is
    testing the formatter.
    """
    return f'"{key}"' in source or f"'{key}'" in source


def _signoff_keys() -> list[str]:
    """The relationship keys every join item carries, read off the assembler's own literal.

    `assemble_receipt` seeds them as `{key: None for key in (...)}` precisely so both branches
    carry an identical key set, which makes that tuple the one true statement of the join shape.
    Parsed rather than copied: copying it here would just create the second place to drift that
    this whole change is about closing.
    """
    from semantic_model import runtime

    tree = ast.parse(inspect.getsource(runtime.assemble_receipt))
    for node in ast.walk(tree):
        if not isinstance(node, ast.DictComp) or not isinstance(node.value, ast.Constant):
            continue
        if node.value.value is not None:
            continue
        source = node.generators[0].iter
        if isinstance(source, ast.Tuple):
            return [e.value for e in source.elts if isinstance(e, ast.Constant)]
    raise AssertionError("the join sign-off key set is no longer a literal tuple — re-derive it")


def _instruction_variants(monkeypatch) -> dict[str, str]:
    """Both deployment wordings. A rule that survives only one of them is a rule half the
    installs do not have."""
    out = {}
    for label, hosted in (("hosted", True), ("local", False)):
        for var in ("AGAMI_DB_URL", "APP_DATABASE_URL"):
            monkeypatch.delenv(var, raising=False)
        if hosted:
            monkeypatch.setenv("AGAMI_DB_URL", "sqlite:///tmp/does-not-need-to-exist.db")
        out[label] = tools.server_instructions()
    return out


# --- the failure channel ----------------------------------------------------


def test_every_failure_kind_the_server_can_return_is_named_on_the_tool():
    """Derived from the enum, so a new kind fails here rather than reaching a client unannounced.

    Four of the ten are declared-but-unreachable today (`guardrail.FailureKind` says which and
    why). They are named anyway: the point of documenting a closed set is that a client can branch
    exhaustively on it, and a set that grows silently under the reader is not closed.
    """
    described = tools.TOOLS["execute_sql"]["description"]
    missing = [kind for kind in get_args(guardrail.FailureKind) if kind not in described]
    assert missing == [], f"execute_sql never tells a client it can return: {', '.join(missing)}"


def test_all_three_statuses_are_documented_not_just_the_two_that_were():
    """`ok` and `refused` were described; `failed` was not, and it is the one a client cannot
    guess the shape of from the other two — `refusal` names its own fix and `failure` cannot."""
    described = tools.TOOLS["execute_sql"]["description"]
    for status in ("ok", "refused", "failed"):
        assert f"status:'{status}'" in described, f"the {status} outcome is undescribed"
    # The distinction that decides whether an agent should retry at all.
    assert "failure:{kind, message}" in described


def test_the_tool_says_which_failures_are_worth_retrying():
    """A taxonomy with no policy attached just moves the guesswork one level down. The split is
    the useful part: a statement the schema can repair, versus deployment configuration that no
    number of retries will fix."""
    described = tools.TOOLS["execute_sql"]["description"]
    assert "retry SILENTLY" in described
    for repairable in ("syntax", "column_not_found", "table_not_found"):
        assert repairable in described
    for unfixable in ("auth", "dsn", "driver_missing"):
        assert unfixable in described


# --- fields that always shipped and were never described ---------------------


def test_list_datasources_describes_the_fields_it_actually_returns():
    """Both backends behind that tool build the same keys; the description named only one of
    them. Sourced from the handler so a renamed key breaks this rather than silently making the
    description wrong again."""
    handler_src = inspect.getsource(tools.tool_list_datasources)
    described = tools.TOOLS["list_datasources"]["description"]
    for key in ("database_type", "table_count"):
        assert _emits_key(handler_src, key), f"handler no longer emits {key}; fix the description"
        assert f"`{key}`" in described, f"{key} ships on every call and is described nowhere"


def test_the_one_conditional_field_is_described_as_conditional():
    """`description` is the only entry here a client must not assume.

    The served path emits it only when the model declares one and the local path never does, so
    "each entry carries a description" was the same class of untruth this file exists to catch —
    and the cost is specific: routing without a schema call is the tool's whole purpose, and an
    agent that reads a missing key as an error re-adds the call it was meant to avoid.
    """
    described = tools.TOOLS["list_datasources"]["description"]
    assert "WHEN it declares one" in described
    assert "still routes on its name" in described


def test_the_dialect_rule_points_at_the_field_that_carries_it(monkeypatch):
    """The instructions used to be silent on dialect entirely, on the theory that a metric's
    `binding` arrives pre-resolved. True, and it covers only the metrics — date arithmetic, string
    functions and casts are the agent's to write, and it had nothing to write them from."""
    for label, text in _instruction_variants(monkeypatch).items():
        assert "database_type" in text, f"{label}: no dialect source named"
        assert "never assume" in text, f"{label}: no instruction not to guess"
        # And it must not undo the reason core could stay silent before.
        assert "`binding`" in text, f"{label}: lost the copy-the-binding rule"


def test_the_schema_tool_names_the_table_context_it_ships():
    """`_table_contexts` requests these four and the description mentioned none, so the only place
    a declared filter appeared was the receipt — which the agent reads after the query ran."""
    context_src = inspect.getsource(tools._table_contexts)
    described = tools.TOOLS["get_datasource_schema"]["description"]
    for block in ("default_filters", "relationships", "caveats", "value_transforms"):
        assert _emits_key(context_src, block), f"{block} is no longer requested; fix the wording"
        assert f"`{block}`" in described, f"{block} ships in the payload and is described nowhere"


def test_the_join_item_shape_is_described_as_the_assembler_builds_it():
    """The declared shape said `{predicate, from_to, scope, status}` and stopped there, while the
    next two sentences referenced `name` and `review_state` — and the instructions tell an agent to
    filter joins ON `review_state`. So the surface named a field in its rule that its own shape
    said did not exist, which is the exact failure mode of the four undocumented capabilities this
    change is about, one layer in.

    Derived from `assemble_receipt`'s key tuple: add a sign-off field and this says so.
    """
    described = tools.TOOLS["execute_sql"]["description"]
    missing = [key for key in _signoff_keys() if key not in described]
    assert missing == [], f"join items carry these and the shape omits them: {', '.join(missing)}"
    # Present-but-null is the part a consumer cannot guess, and it is what makes "review_state is
    # null when status is not 'declared'" a readable rule rather than a contradiction.
    assert "always PRESENT" in described


# --- where each kind of guidance lives ---------------------------------------


def test_the_receipt_vocabulary_lives_on_the_tool_that_returns_it():
    """The field-by-field definitions moved off the always-on preamble and onto `execute_sql`,
    where a host renders them beside the result they describe. Read at initialize, four steps
    before the payload exists, they were the least-situated text on the surface."""
    described = tools.TOOLS["execute_sql"]["description"]
    for section in ("columns", "tables", "joins", "aggregates", "assumptions"):
        assert section in described
    # The status vocabularies — the part a reader cannot infer and must be able to look up.
    for vocabulary in ("undeclarable", "not_multiplied", "unmatched", "'reference'"):
        assert vocabulary in described, f"{vocabulary} is defined nowhere a client can reach"


def test_the_instructions_keep_the_rules_that_outlive_any_one_field(monkeypatch):
    """What stayed behind is deliberately not a summary of what moved. These are decisions —
    show it, don't refuse over it, don't read silence as clean — and they hold whatever the
    payload's field names become."""
    for label, text in _instruction_variants(monkeypatch).items():
        assert "NOT CHECKED" in text, f"{label}: lost the not-checked-vs-clean rule"
        assert "answer and warn" in text, f"{label}: lost the don't-refuse rule"
        assert "review_state" in text, f"{label}: lost the show-the-unapproved rule"


def test_the_two_grounding_calls_are_declared_independent(monkeypatch):
    """A numbered flow reads as a sequence, so a compliant agent serialized two round trips that
    share no state. Stated as a prohibition rather than a permission: an affordance can be
    declined, a rule gets checked against."""
    for label, text in _instruction_variants(monkeypatch).items():
        assert "INDEPENDENT" in text, f"{label}: the two grounding calls still read as ordered"
        assert "Never serialize what is independent" in text, f"{label}: stated too weakly"


def test_the_surface_admits_it_cannot_save_a_correction(monkeypatch):
    """Absence and omission are indistinguishable to a reader. There is no save-a-correction tool
    here on purpose — that is a skill operation — but an agent told nothing about it will claim to
    have remembered something, which is the one failure worse than not offering the feature."""
    for label, text in _instruction_variants(monkeypatch).items():
        assert "Corrections:" in text, f"{label}: the absence reads as an oversight"
        assert "not persisted" in text, f"{label}: does not say the correction is not saved"
