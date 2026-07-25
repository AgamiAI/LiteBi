"""Phase C — parse_model_feedback.py (agami-model back-channel parser).

The dashboard's "Generate feedback" block (profile + exclude/include lists +
curate-ops/new-metrics/key-terminology JSON) used to be parsed in prose. This
guards the parse + the exclude/include → curate-op translation, and the
`needs_judgment` escape hatch for malformed input.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import parse_model_feedback as F  # noqa: E402

FULL = """profile: sales_prod
exclude tables: sales.STG_LEADS, sales.STG_RAW
exclude columns: sales.CUSTOMERS.SSN
include tables: sales.PAYMENTS
curate-ops: [{"op":"approve","kind":"metric","area":"sales","name":"revenue","at":"2026-06-18T00:00:00Z"}]
new-metrics: [{"area":"sales","name":"aov","calculation":"revenue / orders"}]
key-terminology: {"gold tier": "lifetime spend > $10k"}
signed-off-by: jane@x.com (cfo)
done
"""


def test_full_block_parse_and_translate():
    data, anomalies, needs = F.parse(FULL)
    assert data["profile"] == "sales_prod"
    assert (data["signer"], data["role"]) == ("jane@x.com", "cfo")
    # exclude/include translated to curate ops + curate-ops merged verbatim
    ops = data["ops"]
    assert {"op": "exclude", "kind": "table", "area": "sales", "name": "STG_LEADS"} in ops
    assert {"op": "exclude", "kind": "table", "area": "sales", "name": "CUSTOMERS", "column": "SSN"} in ops
    assert {"op": "include", "kind": "table", "area": "sales", "name": "PAYMENTS"} in ops
    assert any(o.get("op") == "approve" and o.get("name") == "revenue" for o in ops)
    assert data["new_metrics_by_area"] == {"sales": [{"area": "sales", "name": "aov", "calculation": "revenue / orders"}]}
    assert data["key_terminology"] == {"gold tier": "lifetime spend > $10k"}
    assert anomalies == [] and needs is None


def test_malformed_target_is_needs_judgment():
    _, _, needs = F.parse("exclude tables: STG_LEADS\ndone\n")
    assert needs and needs["kind"] == "malformed_targets"
    assert "STG_LEADS" in needs["targets"]


def test_bad_json_is_needs_judgment_not_crash():
    data, anomalies, needs = F.parse("curate-ops: [not json]\ndone\n")
    assert needs and needs["kind"] == "unparseable_json"
    assert any(a["kind"] == "bad_json" for a in anomalies)
    assert data["ops"] == []  # nothing applied from the bad block


def test_multiline_json_block():
    block = (
        "new-metrics: [\n"
        '  {"area":"a","name":"m1","calculation":"x"},\n'
        '  {"area":"b","name":"m2","calculation":"y"}\n'
        "]\n"
        "done\n"
    )
    data, _, needs = F.parse(block)
    assert needs is None
    assert set(data["new_metrics_by_area"]) == {"a", "b"}


def test_empty_block_is_benign():
    data, anomalies, needs = F.parse("done\n")
    assert data["ops"] == [] and not anomalies and needs is None


def test_examples_orgmd_and_slash_signer():
    block = (
        "signed-off-by: bob@x.com / data_lead\n"
        'example-edits: [{"area":"sales","question":"q1","sql":"SELECT 1"}]\n'
        'new-examples: [{"area":"ops","question":"q2","sql":"SELECT 2"}]\n'
        'datasource-md: "About this DB.\\nMRR = monthly recurring revenue."\n'
        "done\n"
    )
    data, _, needs = F.parse(block)
    assert needs is None
    assert (data["signer"], data["role"]) == ("bob@x.com", "data_lead")
    assert set(data["examples_by_area"]) == {"sales", "ops"}
    assert data["datasource_md"].startswith("About this DB.")
