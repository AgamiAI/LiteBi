"""The four port Protocols + their OSS default adapters.

Proves: the Protocols are importable (and dependency-free), and each OSS default adapter
type-checks against its Protocol via @runtime_checkable + behaves as specified.
"""

from __future__ import annotations

import json
import subprocess
import sys

from ports import (
    ActivitySink,
    AuthProvider,
    GovernancePolicy,
    GovernanceVerdict,
    Org,
    OrgResolver,
    Principal,
)


def test_protocols_importable_and_runtime_checkable():
    # Each is a runtime_checkable typing.Protocol (so the isinstance checks below are valid).
    for proto in (ActivitySink, OrgResolver, AuthProvider, GovernancePolicy):
        assert proto.__module__ == "ports"
        assert getattr(proto, "_is_runtime_protocol", False), (
            f"{proto.__name__} not runtime_checkable"
        )


def test_ports_module_imports_without_model_deps():
    """The seam interfaces must import with zero deps — a consumer using only a port should not
    pull in pydantic. Checked in a clean subprocess (the shared pytest process has pydantic)."""
    code = (
        "import sys, ports; assert 'pydantic' not in sys.modules and 'contracts' not in sys.modules"
    )
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


def test_guardrail_module_imports_without_model_deps():
    """`guardrail` is held to the same bar as `ports`, and one notch tighter: it is vendored into
    the plugin mirror, where there is no pip and no package — so it must import on a bare python3.
    `execute_sql` is in the assertion because `Envelope.data`'s annotation is TYPE_CHECKING-only;
    if that import ever escapes to runtime it would also create a cycle (`execute_sql` imports
    this module)."""
    code = (
        "import sys, guardrail; "
        "assert not {'pydantic', 'contracts', 'execute_sql'} & set(sys.modules)"
    )
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


# --- default adapters satisfy their Protocol (runtime_checkable) -------------


def test_default_adapters_satisfy_protocols():
    from oss_adapters import (
        FileActivitySink,
        PresenceAuthProvider,
        SingleTenantOrgResolver,
        WarnOnlyGovernancePolicy,
    )

    assert isinstance(FileActivitySink(), ActivitySink)
    assert isinstance(SingleTenantOrgResolver(), OrgResolver)
    assert isinstance(PresenceAuthProvider(), AuthProvider)
    assert isinstance(WarnOnlyGovernancePolicy(), GovernancePolicy)


# --- adapter behavior -------------------------------------------------------


def test_single_tenant_resolver_returns_one_org():
    from oss_adapters import SingleTenantOrgResolver

    r = SingleTenantOrgResolver()
    assert r.resolve_org().id == "local"
    # any context resolves to the same org (N=1)
    assert r.resolve_org({"anything": 1}) == r.resolve_org()
    # configurable
    custom = SingleTenantOrgResolver(Org(id="acme", name="Acme"))
    assert custom.resolve_org().name == "Acme"


def test_presence_auth_accepts_nonempty_rejects_empty():
    from oss_adapters import PresenceAuthProvider

    p = PresenceAuthProvider()
    assert isinstance(p.validate_token("any-token"), Principal)
    assert p.validate_token("") is None
    assert p.validate_token("   ") is None


def test_warn_only_governance_never_blocks():
    from oss_adapters import WarnOnlyGovernancePolicy

    v = WarnOnlyGovernancePolicy().evaluate()
    assert isinstance(v, GovernanceVerdict)
    assert v.allowed is True


def test_file_activity_sink_writes_jsonl(tmp_path):
    from contracts import QueryExecutionRecord
    from oss_adapters import FileActivitySink

    ql = tmp_path / "query.jsonl"
    sink = FileActivitySink(query_log=ql)

    sink.record_query_execution(
        QueryExecutionRecord(
            id="4b1d9c0e5f3a47829d6e8f0a1b2c3d4e",
            ts="2026-06-25T00:00:00Z",
            profile="acme",
            question="how many?",
            sql="SELECT count(*) FROM orders",
            row_count=1,
            source="mcp_server",
            status="ok",
        )
    )

    qrec = json.loads(ql.read_text().splitlines()[0])
    assert qrec["profile"] == "acme" and qrec["row_count"] == 1 and qrec["source"] == "mcp_server"
    # The jsonl sink carries the audit fields too, so the file-mode install records the same verdict
    # the DB-backed one does — `id` is the `audit_id` the answer returned, not a second identifier.
    assert qrec["id"] == "4b1d9c0e5f3a47829d6e8f0a1b2c3d4e" and qrec["status"] == "ok"
