"""AH-112 — parse_golden_feedback.py (golden-dataset explorer back-channel parser).

The explorer page emits a `profile:` line plus a `golden-ops:` JSON array of
curation actions. This guards the two load-bearing rules — the block names the
profile it targets (SC8), and no queued action can grant confirmation (SC9) —
plus the anomaly / needs_judgment split for everything else.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import parse_golden_feedback as F  # noqa: E402

FULL = """profile: demo
golden-ops:
[{"op":"add-tag","dataset":"orders","id":"orders-by-month","value":"smoke"},
 {"op":"remove-tag","dataset":"orders","id":"orders-by-month","value":"draft"},
 {"op":"set-match","dataset":"orders","id":"orders-count","value":"values"},
 {"op":"edit-question","dataset":"orders","id":"orders-count","value":"How many orders were placed in 2024?"},
 {"op":"remove-item","dataset":"orders","id":"orders-stale"},
 {"op":"withdraw-confirmation","dataset":"orders","id":"orders-by-channel"}]
done
"""


def test_the_block_names_the_profile_it_targets():
    data, anomalies, needs = F.parse("profile: demo\ndone\n")
    assert data["profile"] == "demo"
    assert anomalies == [] and needs is None


def test_a_block_with_no_profile_line_names_none_rather_than_assuming_the_active_one():
    data, _, _ = F.parse(
        'golden-ops:\n[{"op":"add-tag","dataset":"orders","id":"o1","value":"smoke"}]\ndone\n'
    )
    assert data["profile"] is None
    assert len(data["ops"]) == 1


def test_an_op_setting_sql_confirmed_is_needs_judgment_and_applies_nothing():
    block = (
        "profile: demo\n"
        "golden-ops:\n"
        '[{"op":"set-match","dataset":"orders","id":"o1","sql_confirmed":true}]\n'
        "done\n"
    )
    data, _, needs = F.parse(block)
    assert needs and needs["kind"] == "confirmation_cannot_be_granted"
    assert needs["ask"]
    assert data["ops"] == []  # nothing from a block that tried to forge ground truth


def test_an_op_carrying_expected_or_sql_cannot_be_applied_either():
    for payload in ('"expected":{"sql":"SELECT 1"}', '"sql":"SELECT 1"'):
        block = 'golden-ops:\n[{"op":"edit-question","dataset":"d","id":"i",%s}]\ndone\n' % payload
        data, _, needs = F.parse(block)
        assert needs and needs["kind"] == "confirmation_cannot_be_granted"
        assert data["ops"] == []


def test_withdrawing_confirmation_is_accepted_because_weakening_needs_no_evidence():
    block = 'golden-ops:\n[{"op":"withdraw-confirmation","dataset":"orders","id":"o1"}]\ndone\n'
    data, anomalies, needs = F.parse(block)
    assert needs is None and anomalies == []
    assert data["ops"] == [{"op": "withdraw-confirmation", "dataset": "orders", "id": "o1"}]


def test_an_unknown_op_is_an_anomaly_and_its_siblings_still_apply():
    block = (
        "golden-ops:\n"
        '[{"op":"delete-dataset","dataset":"orders","id":"o1"},\n'
        ' {"op":"add-tag","dataset":"orders","id":"o2","value":"smoke"}]\n'
        "done\n"
    )
    data, anomalies, needs = F.parse(block)
    assert needs is None
    assert any(a["kind"] == "unknown_op" for a in anomalies)
    assert [o["op"] for o in data["ops"]] == ["add-tag"]


def test_bad_json_is_needs_judgment_not_crash():
    data, anomalies, needs = F.parse("golden-ops: [not json]\ndone\n")
    assert needs and needs["kind"] == "unparseable_json"
    assert any(a["kind"] == "bad_json" for a in anomalies)
    assert data["ops"] == []


def test_golden_ops_that_is_not_a_list_is_an_anomaly():
    data, anomalies, needs = F.parse('golden-ops: {"op":"add-tag"}\ndone\n')
    assert needs is None
    assert any(a["kind"] == "golden_ops_not_list" for a in anomalies)
    assert data["ops"] == []


def test_an_op_naming_no_target_is_an_anomaly():
    block = (
        "golden-ops:\n"
        '[{"op":"add-tag","id":"o1","value":"smoke"},\n'
        ' {"op":"add-tag","dataset":"orders","value":"smoke"}]\n'
        "done\n"
    )
    data, anomalies, _ = F.parse(block)
    assert [a["kind"] for a in anomalies] == ["op_missing_target", "op_missing_target"]
    assert data["ops"] == []


def test_an_empty_block_is_benign():
    data, anomalies, needs = F.parse("done\n")
    assert data == {"profile": None, "ops": []}
    assert not anomalies and needs is None


def test_the_full_block_round_trips_every_allowed_op_kind():
    data, anomalies, needs = F.parse(FULL)
    assert anomalies == [] and needs is None
    assert data["profile"] == "demo"
    assert [o["op"] for o in data["ops"]] == [
        "add-tag",
        "remove-tag",
        "set-match",
        "edit-question",
        "remove-item",
        "withdraw-confirmation",
    ]
    assert data["ops"][0] == {
        "op": "add-tag",
        "dataset": "orders",
        "id": "orders-by-month",
        "value": "smoke",
    }


def test_the_page_and_the_parser_agree_on_the_vocabulary():
    """The page emits and this parser accepts, and neither file mentions the other. A verb added to
    one side and not the other is silently dropped feedback, which is the drift the spec names: a
    page that disagrees with the door it writes through is worse than no page."""
    template = (
        REPO_ROOT / "plugins" / "agami" / "shared" / "golden-datasets-template.html"
    ).read_text(encoding="utf-8")
    emitted = set(re.findall(r"queueOp\('([a-z-]+)'", template))

    assert emitted == set(F._ALLOWED_OPS)


def test_the_page_never_emits_a_field_that_would_grant_confirmation():
    """SC9 holds at the parser so a hand-edited page cannot get past it. This is the other half:
    the page as shipped never even builds such a field, so the refusal above stays a guard against
    tampering rather than something the page trips over in normal use."""
    template = (
        REPO_ROOT / "plugins" / "agami" / "shared" / "golden-datasets-template.html"
    ).read_text(encoding="utf-8")
    queued = re.search(r"function queueOp\(.*?\n    \}", template, re.S).group(0)

    assert "sql_confirmed" not in queued and "expected" not in queued


# --- The routes around the confirmation rule ------------------------------
#
# The field check is a denylist of three names, and a denylist cannot hold a rule this
# load-bearing on its own. What actually holds it is the projection below it: an op reaches the
# write door as the four keys the page emits, so a field the page never wrote cannot arrive by
# somebody editing the block. These are the variants that walked past the denylist.


def _ops(*ops):
    return "profile: demo\ngolden-ops:\n" + json.dumps(list(ops)) + "\ndone\n"


def test_a_confirming_field_under_another_key_does_not_reach_the_write_door():
    data, _, _ = F.parse(
        _ops(
            {
                "op": "edit-question",
                "dataset": "orders",
                "id": "o1",
                "value": "Why?",
                "expected_patch": {"sql_confirmed": True, "sql": "SELECT 1"},
            }
        )
    )

    assert data["ops"] == [
        {"op": "edit-question", "dataset": "orders", "id": "o1", "value": "Why?"}
    ]


def test_a_confirming_field_spelled_in_another_case_does_not_reach_the_write_door():
    data, _, _ = F.parse(
        _ops(
            {
                "op": "add-tag",
                "dataset": "orders",
                "id": "o1",
                "value": "smoke",
                "SQL_CONFIRMED": True,
                "sql_confirmed ": True,
                "confirmed_by": "you@example.com",
            }
        )
    )

    assert data["ops"] == [{"op": "add-tag", "dataset": "orders", "id": "o1", "value": "smoke"}]


def test_a_value_that_is_not_text_is_refused_rather_than_carried():
    """`"value": {"sql_confirmed": true}` names none of the denied fields at the top level. A value
    is what somebody typed, so a structure here is not a value."""
    data, anomalies, _ = F.parse(
        _ops({"op": "set-match", "dataset": "orders", "id": "o1", "value": {"sql_confirmed": True}})
    )

    assert data["ops"] == []
    assert [a["kind"] for a in anomalies] == ["op_value_not_text"]


def test_the_refusal_discards_ops_read_before_it():
    """A block carrying a confirming field is evidence the page was hand-edited, so applying the
    half of it that looked well-behaved is worse than refusing the whole."""
    data, _, needs = F.parse(
        _ops(
            {"op": "add-tag", "dataset": "orders", "id": "first", "value": "smoke"},
            {"op": "add-tag", "dataset": "orders", "id": "second", "sql_confirmed": True},
        )
    )

    assert needs["kind"] == "confirmation_cannot_be_granted"
    assert data["ops"] == []


def test_an_entry_that_is_not_an_object_is_an_anomaly_rather_than_a_traceback():
    """The block is text somebody pasted, and every other malformation here degrades. A traceback
    where the refusal is signalled is the wrong failure mode even when it fails closed."""
    data, anomalies, _ = F.parse(
        _ops({"op": "add-tag", "dataset": "orders", "id": "o1", "value": "smoke"}, 5, "x", None)
    )

    assert len(data["ops"]) == 1
    assert [a["kind"] for a in anomalies] == ["op_not_an_object"] * 3


def test_an_op_name_that_is_not_a_string_is_an_unknown_op_rather_than_a_traceback():
    """A list is unhashable, so looking it up in the allowed set raises rather than reporting."""
    data, anomalies, _ = F.parse(_ops({"op": ["add-tag"], "dataset": "orders", "id": "o1"}))

    assert data["ops"] == []
    assert [a["kind"] for a in anomalies] == ["unknown_op"]


def test_a_repeated_key_is_reported_rather_than_silently_overwritten():
    """A second `golden-ops:` keeps the last block, which means a first block that would have
    tripped the refusal can be suppressed by appending a clean one. The last block still wins, but
    the caller is told the earlier one existed."""
    block = (
        "profile: demo\n"
        'golden-ops:\n[{"op":"add-tag","dataset":"o","id":"a","sql_confirmed":true}]\n'
        'golden-ops:\n[{"op":"add-tag","dataset":"o","id":"b","value":"t"}]\ndone\n'
    )

    data, anomalies, _ = F.parse(block)

    assert [a["kind"] for a in anomalies] == ["key_repeated"]
    assert [o["id"] for o in data["ops"]] == ["b"]
