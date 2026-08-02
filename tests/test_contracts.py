"""The 4-tool pydantic contracts mirror the existing local tool I/O.

The load-bearing check: a real sample of each tool's **current** stdio output parses into its
contract and dumps back **without loss** — proving the contracts match the local,
subject-area-primary shape. Samples here are copied from the dicts in `mcp_harness.py`.

The trust receipt is deliberately not among them: it is typed by `guardrail.Receipt` and asserted
against the real tool-edge serializer, not respelled in pydantic here.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("pydantic")

import contracts  # noqa: E402
from contracts import (  # noqa: E402
    CrossAreaRelationship,
    DatasourceSchemaResult,
    ExecuteSqlResult,
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


def test_execute_sql_result_roundtrip():
    sample = {
        "columns": ["total"],
        "rows": [[148.95]],
        "row_count": 1,
        "truncated": False,
        "units": {"total": "USD"},
        "markdown": "| total |\n| --- |\n| $148.95 |",
        "sql": "SELECT SUM(amount) AS total FROM orders",
        "execution_ms": 12,
    }
    assert _roundtrip(ExecuteSqlResult, sample) == sample


def test_the_receipt_is_the_envelopes_and_this_module_does_not_respell_it():
    """`contracts.Receipt` is gone, and its absence is asserted rather than left to be noticed.

    It typed a `data.receipt` that no longer exists, in the flat pre-section shape, and nothing in
    shipped code ever constructed or validated against it. The receipt a caller actually gets is
    `guardrail.Receipt` — the frozen dataclass in the stdlib-only module both the executor and the
    tool edge can reach, and the plugin mirror can vendor. A second pydantic spelling here would be
    a shape nothing checks and a second thing to keep in step.

    `extra="allow"` is why this needs a test at all: a stray `receipt=` on `ExecuteSqlResult` would
    parse and round-trip silently rather than fail.
    """
    import guardrail

    assert not hasattr(contracts, "Receipt")
    assert not hasattr(contracts, "TableUsed")
    assert "receipt" not in ExecuteSqlResult.model_fields
    assert guardrail.Receipt.SECTIONS == (
        "columns", "tables", "joins", "aggregates", "assumptions")


def test_a_rejected_execute_sql_is_an_envelope_body_not_an_error_contract():
    """`ErrorResult` / `ToolError` are gone — the guardrail Envelope replaced both.

    `execute_sql` no longer returns `{"error": {kind, remediation}}` for anything. A decision of
    ours arrives as `{"status": "refused", "refusal": {reason, rule, detail, remediation}}` and the
    database's as `{"status": "failed", "failure": {kind, message}}`, both carrying the `audit_id`
    of the recorded execution. Asserted against the real tool-edge serializer rather than a
    hand-written sample, so this pins the shape a client actually receives.

    The absence is asserted too: `contracts` is where a reader looks for a tool's error shape, and
    finding nothing has to be a deliberate removal rather than something that fell out by accident.
    """
    import guardrail
    import tools

    refused = json.loads(tools._emit(
        guardrail.Envelope(
            status="refused",
            refusal=guardrail.refuse(
                guardrail.RULE_READ_ONLY,
                detail="only a single read statement is allowed",
                remediation="Rewrite it as a SELECT.",
            ),
            audit_id="0" * 32,
        ),
        sql="DELETE FROM orders", execution_ms=None,
    ))
    # `receipt` joined both non-ok bodies in ACE-088: a refused caller most needs the facts, so the
    # receipt rides every refusal and every failure (on a refusal, bounded to the identifiers the
    # caller's own statement wrote).
    assert set(refused) == {"status", "refusal", "sql", "audit_id", "receipt"}
    assert set(refused["refusal"]) == {"reason", "rule", "detail", "remediation"}

    failed = json.loads(tools._emit(
        guardrail.Envelope(
            status="failed",
            failure=guardrail.Failure(kind="syntax", message="no such column"),
            audit_id="1" * 32,
        ),
        sql="SELECT nope FROM orders", execution_ms=0,
    ))
    assert set(failed) == {"status", "failure", "sql", "execution_ms", "audit_id", "receipt"}
    assert set(failed["failure"]) == {"kind", "message"}

    assert not hasattr(contracts, "ErrorResult") and not hasattr(contracts, "ToolError")


def test_activity_sink_records_roundtrip():
    # `id` is the Envelope's `audit_id` — supplied by the caller, never minted by the sink — so the
    # answer and the row recording it share one key. `status`/`reason`/`rule` are the verdict.
    q = {
        "id": "9f2c1e4a7b6d4c0f8e3a5b7c9d1e2f30",
        "ts": "2026-06-25T00:00:00Z",
        "profile": "acme",
        "question": "how many orders?",
        "sql": "SELECT count(*) FROM orders",
        "row_count": 1,
        "source": "mcp_server",
        "status": "ok",
    }
    assert _roundtrip(QueryExecutionRecord, q) == q


def test_a_refusal_record_carries_its_reason_and_rule():
    # The audit row for a refusal is the one a reviewer most needs, and it is the reason + rule that
    # make it reviewable: without them a refused row is indistinguishable from a query that ran.
    q = {
        "id": "3d5f7a91c2b84e6d90f1a2b3c4d5e6f7",
        "ts": "2026-06-25T00:00:00Z",
        "profile": "acme",
        "sql": "DELETE FROM orders",
        "row_count": 0,
        "source": "mcp_server",
        "status": "refused",
        "reason": "unsafe",
        "rule": "read_only",
    }
    assert _roundtrip(QueryExecutionRecord, q) == q


def test_contracts_tolerate_richer_payload_losslessly():
    # extra="allow": a richer payload (e.g. a future backend adds query_id) must parse AND survive
    # a round-trip — the contracts pin the local shape without rejecting forward-compatible fields.
    sample = {"datasources": [], "active_datasource": "acme", "query_id": "q-123"}
    assert _roundtrip(ListDatasourcesResult, sample) == sample
