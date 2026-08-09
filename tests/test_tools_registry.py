"""The shared TOOLS registry — one impl, both transports.

Both the stdio entrypoint (mcp_harness) and the HTTP entrypoint (mcp_http) import the SAME
`tools.TOOLS` object, so the surface can't drift. These assert the surface is exactly the 4
product tools, the dropped tools are gone, and the registry the two transports share is identical.
"""

from __future__ import annotations

import mcp_harness
import tools

PRODUCT_TOOLS = {
    "list_datasources",
    "get_datasource_schema",
    "get_prompt_examples",
    "execute_sql",
}
# Subsumed by the smart get_datasource_schema / folded / internal / skill-operation, or (log_feedback)
# simply removed from the surface. Deliberately NOT on the MCP surface of either transport.
DROPPED_FROM_MCP = {
    "log_feedback",
    "list_subject_areas",
    "get_subject_area_bundle",
    "get_table_context",
    "identify_entity",
    "pre_flight_check",
    "save_correction",
}


def test_surface_is_exactly_the_four_product_tools():
    assert set(tools.TOOLS) == PRODUCT_TOOLS


def test_dropped_tools_are_absent():
    assert DROPPED_FROM_MCP.isdisjoint(tools.TOOLS)


def test_both_transports_share_one_registry():
    # The strongest no-drift guarantee: it's literally the same object, not two copies.
    assert mcp_harness.TOOLS is tools.TOOLS


def test_every_tool_has_handler_and_input_schema():
    for name, meta in tools.TOOLS.items():
        assert callable(meta["handler"]), name
        assert (
            isinstance(meta["inputSchema"], dict) and meta["inputSchema"].get("type") == "object"
        ), name


def test_db_type_label_covers_advertised_databases():
    # The `database_type` shown by list_datasources maps a DSN scheme → label for every DB agami
    # advertises; an unknown scheme passes through verbatim (execution is execute_sql's job).
    cases = {
        "postgresql://h/db": "postgres",
        "mysql://h/db": "mysql",
        "snowflake://acct": "snowflake",
        "mssql://h/db": "sqlserver",
        "oracle://h/db": "oracle",
        "databricks://h": "databricks",
        "trino://h": "trino",
        "duckdb:///tmp/f.db": "duckdb",
        "exotic://h": "exotic",  # unknown → passthrough
    }
    for url, expected in cases.items():
        assert tools._db_type_for("p", {"p": {"url": url}}) == expected, url


def test_every_argument_a_handler_reads_is_declared_on_its_own_schema():
    """A parameter the handler consumes but the schema does not advertise is unreachable.

    Every tool sets `additionalProperties: false`, so a client sending an undeclared key is
    REJECTED and a client omitting it gets None — the branch behind it can never run. That is not
    theoretical: `get_prompt_examples` passed `area=args.get("area")` into `select_examples`, which
    implements area scoping correctly (`area = ? OR area IS NULL` — the area plus the cross-area
    bucket), and `area` was missing from the schema. A finished, correct feature, dead on every
    call, and invisible to CI because nothing compared the two halves.

    Reads the handler's own source rather than a hand-kept list, so a parameter added to a handler
    without its schema entry fails here on the next run instead of shipping unreachable.
    """
    import inspect
    import re

    # `args.get("x")` / `args.get('x', default)` — the one way these handlers read their input.
    reads = re.compile(r"""\bargs\.get\(\s*["']([a-z_][a-z0-9_]*)["']""")
    for name, meta in tools.TOOLS.items():
        try:
            src = inspect.getsource(meta["handler"])
        except (OSError, TypeError):  # a consumer-registered handler with no readable source
            continue
        declared = set(meta["inputSchema"].get("properties", {}))
        undeclared = sorted(set(reads.findall(src)) - declared)
        assert not undeclared, (
            f"{name} reads {undeclared} but does not declare it; "
            f"additionalProperties is {meta['inputSchema'].get('additionalProperties')!r}, "
            "so no client can send it"
        )
