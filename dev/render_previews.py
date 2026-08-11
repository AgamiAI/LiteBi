#!/usr/bin/env python3
"""Render every server-rendered page to standalone previews/*.html with sample values.

A dev tool (not shipped/imported by the server) so the UI can be eyeballed in a browser without
running the service. Rewrites the absolute /static/ asset paths to relative + copies the assets in,
so each preview file opens correctly via file://.
"""

import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent  # repo root (this dev tool lives in dev/)
PKG = ROOT / "packages" / "agami-core" / "src"
sys.path.insert(0, str(PKG))

import admin  # noqa: E402
import oauth_server  # noqa: E402
import onboarding  # noqa: E402

OUT = ROOT / "previews"
OUT.mkdir(exist_ok=True)
shutil.copytree(PKG / "static", OUT / "static", dirs_exist_ok=True)

BASE = "https://demo-a1b2c3.trycloudflare.com"
OAUTH = {
    "client_id": "cid_9f2c",
    "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
    "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
    "state": "xyz789",
}
ADMIN_EMAIL = "you@example.com"
USERS = [
    {
        "username": ADMIN_EMAIL,
        "first_name": "Alex",
        "last_name": "Kim",
        "email": ADMIN_EMAIL,
        "status": "active",
        "oidc_provider": None,
        "has_password": True,
    },
    {
        "username": "jordan@example.com",
        "first_name": "Jordan",
        "last_name": "Lee",
        "email": "jordan@example.com",
        "status": "active",
        "oidc_provider": "google",
        "has_password": False,
    },
    {
        "username": "sam@example.com",
        "first_name": "Sam",
        "last_name": "Okafor",
        "email": "sam@example.com",
        "status": "active",
        "oidc_provider": "microsoft",
        "has_password": False,
    },
    {
        "username": "riley@example.com",
        "first_name": "Riley",
        "last_name": "Chen",
        "email": "riley@example.com",
        "status": "disabled",
        "oidc_provider": None,
        "has_password": False,
    },
    {
        "username": "morgan@example.com",
        "first_name": "Morgan",
        "last_name": "Diaz",
        "email": "morgan@example.com",
        "status": "active",
        "oidc_provider": None,
        "has_password": False,
    },
]


def write(name: str, html: str) -> None:
    html = html.replace('"/static/', '"static/')  # absolute → relative so file:// resolves
    (OUT / name).write_text(html)


ADMIN = {"admin_username": ADMIN_EMAIL, "admin_label": "Alex Kim", "admin_email": ADMIN_EMAIL}
CHROME = {"admin_label": "Alex Kim", "admin_email": ADMIN_EMAIL}

write(
    "01-login.html",
    oauth_server.login_body_html(OAUTH, providers=("google", "microsoft"), wrap=True),
)
write("02-login-password-only.html", oauth_server.login_body_html(OAUTH, wrap=True))
write(
    "03-login-error.html",
    oauth_server.login_body_html(
        OAUTH, error="Invalid email or password.", providers=("google", "microsoft"), wrap=True
    ),
)
write("04-admin-login.html", admin.admin_login_body_html(provider="google"))
# In a password deployment the roster shows a copy-able setup link per pending user.
SETUP_LINKS = {
    u["username"]: f"{BASE}/claim?token=eyJhbGciOi.SAMPLE-SETUP-TOKEN.xyz"
    for u in USERS
    if onboarding.is_pending(u)
}
write(
    "05-admin-users.html",
    admin.users_tab_html(USERS, csrf="t0ken", ok="User added.", setup_links=SETUP_LINKS, **ADMIN),
)
write("06-admin-dashboard.html", admin.dashboard_tab_html(**CHROME))

# The activity view — rendered from the REAL builders + read helpers over a temp store (no drift).
import os  # noqa: E402
import tempfile  # noqa: E402

import model_store  # noqa: E402
from contracts import ToolCallRecord  # noqa: E402
from model_store import DbActivitySink  # noqa: E402
from store import Store  # noqa: E402

_fd, _db_path = tempfile.mkstemp(suffix=".db")  # atomic, not the race-prone mktemp
os.close(_fd)
_s = Store.connect("sqlite://" + _db_path)
_s.run_migrations()
_sink = DbActivitySink(_s)
_SAMPLE_CALLS = [
    # One conversation (thread t1), two turns — and EVERY call folds in, not just the execute_sql ones:
    # turn c1 scopes the datasource (list_datasources), turn c2 answers a question (schema + 2 queries).
    dict(
        ts="2026-06-27T10:40:50Z",
        tool_name="list_datasources",
        source="mcp_server",
        actor="jordan@example.com",
        execution_ms=3,
        success=True,
        user_question="What datasources can I ask about?",
        thread_id="t1",
        correlation_id="c1",
    ),
    dict(
        ts="2026-06-27T10:41:05Z",
        tool_name="get_datasource_schema",
        source="mcp_server",
        actor="jordan@example.com",
        datasource="SALES_DATA",
        execution_ms=12,
        success=True,
        user_question="What's our revenue by region this quarter?",
        thread_id="t1",
        correlation_id="c2",
    ),
    dict(
        ts="2026-06-27T10:42:17Z",
        tool_name="execute_sql",
        source="mcp_server",
        actor="jordan@example.com",
        datasource="SALES_DATA",
        sql="SELECT region, SUM(amount) AS revenue\nFROM orders\nGROUP BY region\nORDER BY revenue DESC",
        row_count=5,
        execution_ms=84,
        success=True,
        user_question="What's our revenue by region this quarter?",
        agent_query="revenue by region",
        thread_id="t1",
        correlation_id="c2",
    ),
    dict(
        ts="2026-06-27T10:42:41Z",
        tool_name="execute_sql",
        source="mcp_server",
        actor="jordan@example.com",
        datasource="SALES_DATA",
        sql="SELECT date_trunc('month', placed_at) AS month, SUM(amount)\nFROM orders\nWHERE region = 'West'\nGROUP BY 1\nORDER BY 1",
        row_count=3,
        execution_ms=61,
        success=True,
        user_question="What's our revenue by region this quarter?",
        agent_query="monthly trend for the top region (West)",
        thread_id="t1",
        correlation_id="c2",
    ),
    # A separate one-query turn that errored (thread t2).
    dict(
        ts="2026-06-27T10:41:50Z",
        tool_name="execute_sql",
        source="mcp_server",
        actor="sam@example.com",
        datasource="SALES_DATA",
        sql="SELECT * FROM ordrs",
        execution_ms=31,
        success=False,
        error_kind="syntax",
        user_question="how many orders today?",
        agent_query="count today's orders",
        thread_id="t2",
        correlation_id="c3",
    ),
    # A cross-datasource turn (thread t3, one correlation): a question spanning two datasources runs one
    # execute_sql per datasource — the row lists both, and each call card shows its own.
    dict(
        ts="2026-06-27T10:55:10Z",
        tool_name="execute_sql",
        source="mcp_server",
        actor="jordan@example.com",
        datasource="SALES_DATA",
        sql="SELECT region, SUM(amount) AS revenue\nFROM orders GROUP BY region",
        row_count=5,
        execution_ms=72,
        success=True,
        user_question="revenue vs open support tickets by region",
        agent_query="revenue by region",
        thread_id="t3",
        correlation_id="c4",
    ),
    dict(
        ts="2026-06-27T10:55:31Z",
        tool_name="execute_sql",
        source="mcp_server",
        actor="jordan@example.com",
        datasource="SUPPORT_DATA",
        sql="SELECT region, COUNT(*) AS open_tickets\nFROM tickets WHERE status='open' GROUP BY region",
        row_count=5,
        execution_ms=58,
        success=True,
        user_question="revenue vs open support tickets by region",
        agent_query="open tickets by region",
        thread_id="t3",
        correlation_id="c4",
    ),
    # A bare call with no self-reported ids → its own singleton conversation (audit-complete degradation).
    dict(
        ts="2026-06-27T10:40:03Z",
        tool_name="list_datasources",
        source="mcp_server",
        actor="morgan@example.com",
        execution_ms=3,
        success=True,
    ),
]
for _c in _SAMPLE_CALLS:
    _sink.record_tool_call(ToolCallRecord(**_c))
write("07-admin-activity.html", admin.activity_tab_html(model_store.list_sessions(_s), **CHROME))

# The read-only model explorer — rendered from the REAL builders over the SAME served tree the MCP
# tools read (load_datasource), so the previews can't drift from production. Neutral demo data only.
from semantic_model.models import Datasource  # noqa: E402

_MODEL_DS = {
    "datasource": "acme",
    "version": 1,
    "description": "Acme Commerce — the deployed semantic model.",
    "fiscal_year_start_month": 1,
    "key_terminology": {"AOV": "average order value", "Net revenue": "sales minus refunds"},
    "storage_connections": [
        {
            "name": "warehouse",
            "storage_type": "PostgreSQL",
            "storage_config": {"host": "never-rendered"},
        }
    ],
    "subject_areas": [
        {
            "name": "Catalog",
            "description": "Products, pricing and stock.",
            "default_time_window": "last_90_days",
            "tables": [
                {"storage_connection": "warehouse", "schema": "public", "table": "products"}
            ],
            "tables_defined": [
                {
                    "name": "products",
                    "schema": "public",
                    "storage_connection": "warehouse",
                    "grain": ["id"],
                    "description": "Master product catalog.",
                    "description_source": "ai_unvalidated",
                    "confidence": "proposed",
                    "review_state": "unreviewed",
                    "caveats": [
                        "\"Coffee\" = category='cafe' AND name ILIKE '%coffee%','%latte%'…"
                    ],
                    "columns": [
                        {"name": "id", "type": "uuid", "primary_key": True},
                        {"name": "sku", "type": "string"},
                        {"name": "name", "type": "string"},
                        {
                            "name": "category",
                            "type": "string",
                            "description": "Product type: 'book', 'cafe', 'merchandise', or 'other'.",
                            "description_source": "ai_unvalidated",
                            "choice_field": {"book": "Book", "cafe": "Cafe"},
                        },
                        {"name": "price", "type": "decimal", "unit": "USD"},
                        {
                            "name": "cost_price",
                            "type": "decimal",
                            "unit": "USD",
                            "caveats": [
                                "~20% of rows have cost_price > price (data-entry errors). "
                                "Treat as valid only when cost_price <= price."
                            ],
                        },
                        {
                            "name": "supplier_email",
                            "type": "string",
                            "sensitive": True,
                            "description": "Distributor contact.",
                        },
                        {
                            "name": "category_id",
                            "type": "uuid",
                            "foreign_key": {"table": "categories", "column": "id"},
                        },
                        {"name": "stock_qty", "type": "integer"},
                        {"name": "is_active", "type": "boolean"},
                        {"name": "created_at", "type": "timestamp"},
                        {"name": "author", "type": "string"},
                        {"name": "publisher", "type": "string"},
                        {"name": "barcode", "type": "string"},
                    ],
                    "performance_hints": {"estimated_row_count": 6591},
                },
                {
                    "name": "inventory",
                    "schema": "public",
                    "storage_connection": "warehouse",
                    "grain": ["id"],
                    "description": "Per-location stock levels.",
                    "confidence": "confirmed",
                    "review_state": "approved",
                    "column_groups": {
                        "Identifiers": ["id", "product_id", "location_id"],
                        "Levels": ["on_hand", "reserved"],
                    },
                    "column_group_descriptions": {
                        "Identifiers": "keys & references",
                        "Levels": "stock counts",
                    },
                    "columns": [
                        {"name": "id", "type": "uuid", "primary_key": True},
                        {
                            "name": "product_id",
                            "type": "uuid",
                            "foreign_key": {"table": "products", "column": "id"},
                        },
                        {"name": "location_id", "type": "uuid"},
                        {"name": "on_hand", "type": "integer"},
                        {"name": "reserved", "type": "integer"},
                        {"name": "counted_at", "type": "timestamp"},  # in no group → "Other"
                    ],
                    "performance_hints": {"estimated_row_count": 41000},
                },
            ],
            "metrics": [
                {
                    "name": "active products",
                    "calculation": "COUNT(*) FILTER (WHERE is_active)",
                    "other_names": ["live items"],
                }
            ],
            "entities": [
                {
                    "name": "product",
                    "other_names": ["sku", "item"],
                    "description": "A sellable catalog item.",
                    "value_pattern": "SKU-[0-9]{6}",
                    "confidence": "inferred",
                }
            ],
            "relationships": [
                {
                    "from_table": "products",
                    "to_table": "categories",
                    "from_column": "category_id",
                    "to_column": "id",
                    "relationship": "many_to_one",
                    "confidence": "confirmed",
                    "review_state": "approved",
                }
            ],
        },
        {
            "name": "Sales",
            "description": "Orders, line items and revenue.",
            "tables": [{"storage_connection": "warehouse", "schema": "public", "table": "orders"}],
            "tables_defined": [
                {
                    "name": "orders",
                    "schema": "public",
                    "storage_connection": "warehouse",
                    "grain": ["id"],
                    "description": "One row per placed order.",
                    "confidence": "confirmed",
                    "review_state": "approved",
                    "columns": [
                        {"name": "id", "type": "integer", "primary_key": True},
                        {
                            "name": "customer_id",
                            "type": "integer",
                            "foreign_key": {"table": "customers", "column": "id"},
                        },
                        {"name": "amount", "type": "decimal", "unit": "USD"},
                        {"name": "placed_at", "type": "timestamp"},
                    ],
                    "performance_hints": {"estimated_row_count": 184000},
                }
            ],
            "metrics": [
                {
                    "name": "revenue",
                    "calculation": "SUM(amount)",
                    "unit": "USD",
                    "other_names": ["sales"],
                    "source_tables": ["orders"],
                }
            ],
            "entities": [],
            "relationships": [],
        },
    ],
    # Cross-area joins (Sales → Catalog) — surfaced in the Relationships browse view, grouped by pair.
    "cross_subject_area_relationships": [
        {
            "from_table": "orders",
            "to_table": "products",
            "from_column": "product_id",
            "to_column": "id",
            "from_schema": "public",
            "to_schema": "public",
            "join_type": "LEFT",
            "relationship": "many_to_one",
            "confidence": "inferred",
            "review_state": "unreviewed",
            "from_subject_area": "Sales",
            "to_subject_area": "Catalog",
        },
        {
            "from_table": "orders",
            "to_table": "inventory",
            "from_column": "warehouse_id",
            "to_column": "location_id",
            "from_schema": "public",
            "to_schema": "public",
            "join_type": "LEFT",
            "relationship": "many_to_one",
            "confidence": "proposed",
            "review_state": "unreviewed",
            "from_subject_area": "Sales",
            "to_subject_area": "Catalog",
        },
    ],
}
_DS_MD = (
    "# About Acme Commerce\n\n"
    "Acme sells direct-to-consumer goods online. Every checkout writes an **orders** row.\n\n"
    "## Key terms\n\n"
    "- **Net revenue** excludes cancelled orders and nets refunds.\n"
    "- A **member** has a row in the loyalty table — not just anyone who checked out.\n"
)
model_store.write_datasource(_s, "SALES_DATA", Datasource.model_validate(_MODEL_DS))
model_store.write_datasource(_s, "MARKETING", Datasource.model_validate(_MODEL_DS))  # → picker
model_store.write_memory(_s, "SALES_DATA", datasource_doc=_DS_MD)
model_store.write_model_version(_s, "SALES_DATA", "a1f4c39c0b2e", created_at="2026-06-27T09:00:00Z")
_dss = model_store.list_datasources(_s)
_org = model_store.load_datasource(_s, "SALES_DATA")
_ver = model_store.newest_model_version(_s, "SALES_DATA")
_catalog = next(a for a in _org.subject_areas if a.name == "Catalog")
_products = next(t for t in _catalog.tables_defined if t.name == "products")
_inventory = next(t for t in _catalog.tables_defined if t.name == "inventory")
write("16-model-overview.html", admin.model_overview_html(_org, _ver, "SALES_DATA", _dss, **CHROME))
write("17-model-area.html", admin.model_area_html(_org, _catalog, "SALES_DATA", _dss, **CHROME))
write(
    "18-model-table-flat.html",
    admin.model_table_html(_org, _catalog, _products, "SALES_DATA", _dss, **CHROME),
)
write(
    "19-model-table-grouped.html",
    admin.model_table_html(_org, _catalog, _inventory, "SALES_DATA", _dss, **CHROME),
)
write(
    "20-model-context.html",
    admin.model_context_html(
        _org, model_store.load_memory(_s, "SALES_DATA"), "SALES_DATA", _dss, **CHROME
    ),
)
write(
    "21-model-relationships.html",
    admin.model_relationships_html(_org, "SALES_DATA", _dss, **CHROME),
)
_s.close()
write("08-not-admin.html", admin.not_admin_body_html(BASE))
write("09-landing.html", admin.landing_body_html(BASE))
write("10-mcp-in-browser.html", admin.mcp_landing_body_html(BASE))
write("11-not-authorized.html", admin.not_authorized_body_html("morgan@example.com"))
write("12-setup-password.html", onboarding.claim_page_html("eyJhbGciOi.SAMPLE.xyz"))
write("13-setup-done.html", onboarding.claim_done_html(BASE))
write("14-setup-invalid.html", onboarding.setup_invalid_html())
# The reset link wears the same two pages with different words, so both need previewing or the
# wording nobody sees in a test goes unreviewed.
write("22-reset-password.html", onboarding.claim_page_html("eyJhbGciOi.SAMPLE.xyz", "reset"))
write("23-reset-done.html", onboarding.claim_done_html(BASE, "reset"))

print(f"Wrote {len(list(OUT.glob('*.html')))} previews to {OUT}/")
for p in sorted(OUT.glob("*.html")):
    print(f"  open {p}")
