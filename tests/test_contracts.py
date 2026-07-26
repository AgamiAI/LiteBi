"""The 4-tool pydantic contracts mirror the existing local tool I/O.

The load-bearing check: a real sample of each tool's **current** stdio output parses into its
contract and dumps back **without loss** — proving the contracts match the local,
subject-area-primary shape. Samples here are copied from the dicts in `mcp_harness.py` /
`runtime.assemble_receipt`.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")

from contracts import (  # noqa: E402
    CrossAreaRelationship,
    DatasourceSchemaResult,
    ExecuteSqlResult,
    GuardrailAuditRecord,
    ListDatasourcesResult,
    PromptExamplesResult,
    QueryExecutionRecord,
)


def _roundtrip(model, sample: dict) -> dict:
    """Parse a sample into the contract and dump only the fields it set (by wire alias).
    A lossless round-trip == the contract matches the sample's shape."""
    return model.model_validate(sample).model_dump(by_alias=True, exclude_unset=True)


def test_list_datasources_roundtrip():
    sample = {
        "datasources": [
            {
                "datasource": "acme",
                "database_type": "postgres",
                "table_count": 8,
                "model_present": True,
                "is_active": True,
            },
        ],
        "active_datasource": "acme",
    }
    assert _roundtrip(ListDatasourcesResult, sample) == sample


def test_list_datasources_empty_note_roundtrip():
    sample = {"datasources": [], "note": "No profiles found in your credentials file."}
    assert _roundtrip(ListDatasourcesResult, sample) == sample


def test_get_datasource_schema_index_roundtrip_is_subject_area_primary():
    # Pass-1 index — the local shape is SUBJECT-AREA-primary (not config→table→metric).
    sample = {
        "datasource": "acme",
        "organization": "Acme Inc.",
        "subject_areas": [
            {
                "name": "sales",
                "description": "Orders + revenue",
                "default_time_window": "last_90_days",
                "tables": ["orders", "order_items"],
            },
        ],
        "cross_area_relationships": [
            {"from": "sales", "to": "finance", "for_questions_about": "revenue recognition"},
        ],
        "note": "Per-table detail is lazy-loaded.",
    }
    out = _roundtrip(DatasourceSchemaResult, sample)
    assert out == sample
    assert "subject_areas" in out and "config" not in out  # subject-area-primary local shape


def test_cross_area_relationship_from_alias():
    rel = CrossAreaRelationship.model_validate({"from": "a", "to": "b"})
    assert rel.from_ == "a" and rel.to == "b"
    assert rel.model_dump(by_alias=True, exclude_unset=True) == {"from": "a", "to": "b"}


def test_prompt_examples_empty_roundtrip():
    sample = {"examples": [], "note": "No examples under .../<area>/examples.yaml."}
    assert _roundtrip(PromptExamplesResult, sample) == sample


def test_execute_sql_result_with_receipt_roundtrip():
    sample = {
        "columns": ["total"],
        "rows": [[148.95]],
        "row_count": 1,
        "truncated": False,
        "units": {"total": "USD"},
        "markdown": "| total |\n| --- |\n| $148.95 |",
        "sql": "SELECT SUM(amount) AS total FROM orders",
        "execution_ms": 12,
        "receipt": {
            "sql": "SELECT SUM(amount) AS total FROM orders",
            "model_version": "abc123",
            "tables_used": [
                {"qname": "public.orders", "rows": 4000, "rows_as_of": None, "freshness": None},
            ],
            "relationships": [],
            "metrics": [],
            "named_filters": [],
            "assumptions": [],
            "warnings": ["Used an unreviewed join (orders→customers)."],
        },
    }
    assert _roundtrip(ExecuteSqlResult, sample) == sample


# The error/refusal wire moved to the shared guardrail Envelope + Refusal (see test_guardrail.py);
# the ad-hoc ErrorResult contract was retired.


def test_guardrail_audit_record_roundtrip():
    sample = {
        "audit_id": "a1b2",
        "ts": "2026-07-11T00:00:00Z",
        "status": "refused",
        "datasource": "sales",
        "refusal_kind": "permission",
        "sql": "DELETE FROM orders",
        "row_count": None,
        "execution_ms": None,
        "correlation_id": "turn-1",
        "source": "mcp_server",
        "error_detail": None,
    }
    assert _roundtrip(GuardrailAuditRecord, sample) == sample


def test_activity_sink_records_roundtrip():
    q = {
        "ts": "2026-06-25T00:00:00Z",
        "profile": "acme",
        "question": "how many orders?",
        "sql": "SELECT count(*) FROM orders",
        "row_count": 1,
        "source": "mcp_server",
    }
    assert _roundtrip(QueryExecutionRecord, q) == q


def test_contracts_tolerate_richer_payload_losslessly():
    # extra="allow": a richer payload (e.g. a future backend adds query_id) must parse AND survive
    # a round-trip — the contracts pin the local shape without rejecting forward-compatible fields.
    sample = {"datasources": [], "active_datasource": "acme", "query_id": "q-123"}
    assert _roundtrip(ListDatasourcesResult, sample) == sample
