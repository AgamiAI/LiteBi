"""The exit-code table is the fork's whole vocabulary, so every kind needs a code.

`execute_guarded` classifies a driver error at the chokepoint and returns a `Failure`. On
the in-process and HTTP surfaces the caller receives that object directly. On the DEFAULT
stdio surface the tool edge forks, and `main` collapses the kind to an exit code the parent
reads back through this table — so a kind with no code of its own is a kind the fork
silently loses.

That is why these tests exist as a slice of their own, landing before anything produces the
new kinds. The failure they prevent is invisible to any test that exercises only the
in-process path: the classification is correct where it is made and wrong where it arrives.
"""

from __future__ import annotations

import re

import execute_sql
import guardrail
import pytest
import tools
from execute_sql import EXIT_TO_FAILURE_KIND, FAILURE_KIND_TO_EXIT

# Minted by the subprocess supervisor at the tool edge when a child never returns, so it
# never reaches `main` and never needs an encoding. See the comment on FAILURE_KIND_TO_EXIT.
_NOT_ENCODABLE = {"timeout"}


def test_every_failure_kind_but_timeout_has_an_exit_code() -> None:
    """The round trip is total, and the one exclusion is named rather than left as a gap."""
    encodable = set(guardrail._FAILURE_KINDS) - _NOT_ENCODABLE
    assert set(FAILURE_KIND_TO_EXIT) == encodable


def test_the_round_trip_is_exact_in_both_directions() -> None:
    for code, kind in EXIT_TO_FAILURE_KIND.items():
        assert FAILURE_KIND_TO_EXIT[kind] == code
    assert len(FAILURE_KIND_TO_EXIT) == len(EXIT_TO_FAILURE_KIND), "a kind is double-mapped"


def test_the_documented_table_is_the_table_in_code() -> None:
    """The module claims to own the contract "because it documents it here". Check the claim.

    A table maintained in a docstring drifts from the dict beside it the first time someone
    edits one and not the other, and the docstring is what the two out-of-repo consumers
    (the agami-query skill's error classifier, `semantic_model.cli`) are written against.
    """
    documented = {
        int(m.group(1))
        for m in re.finditer(r"^\s{4}(\d+)\s+—", execute_sql.__doc__ or "", re.MULTILINE)
    }
    # 0 (success) and 1 (refused) are contract codes with no failure kind behind them.
    assert documented - {0, 1} == set(EXIT_TO_FAILURE_KIND)


@pytest.mark.parametrize(
    ("code", "kind"),
    [(7, "column_not_found"), (8, "table_not_found"), (9, "permission"), (10, "network")],
)
def test_the_tool_edge_reads_the_new_codes_back(code: int, kind: str) -> None:
    """`tools._classify_exit` delegates to this table rather than keeping a second copy.

    Asserted rather than assumed: the delegation is what makes extending the table
    sufficient, and a re-introduced local copy would pass every other test in this file.
    """
    assert tools._classify_exit(code) == kind


@pytest.mark.parametrize("code", [7, 8, 9, 10])
def test_a_child_exiting_a_new_code_has_its_text_relayed(code: int) -> None:
    """The second half of the bug, and the easier half to miss.

    `_child_failure_message` relays the child's already-sanitized line only for a code in
    the table. Before this slice the four new codes were not in it, so the parent discarded
    the message and substituted the generic unexpected-failure text — losing the kind AND
    the message in one step.
    """
    relayed = tools._child_failure_message(code, "the child's sanitized line")
    assert relayed == "the child's sanitized line"
    assert relayed != execute_sql.UNEXPECTED_FAILURE_MESSAGE


def test_an_unmapped_code_is_still_other_not_dsn() -> None:
    """The property the `6` code was introduced to hold, unchanged by widening the table."""
    assert tools._classify_exit(99) == "other"
    assert execute_sql._DEFAULT_FAILURE_EXIT == 6
