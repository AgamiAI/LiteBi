"""The admin read-only Model explorer.

A pure projection of the served model (`load_datasource`) + domain docs (`load_memory`): the
overview / area landing / table page render the served tree; it is admin-gated, read-only (a GET
only), never leaks `storage_config`, and escapes operator-authored text.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("starlette")
pytest.importorskip("mcp")
pytest.importorskip("argon2")
pytest.importorskip("pydantic")

PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import admin  # noqa: E402
import mcp_http  # noqa: E402
import model_store  # noqa: E402
import ui  # noqa: E402
import user_store  # noqa: E402
from semantic_model.models import Datasource  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402
from store import Store  # noqa: E402

BASE = "https://your-host.example.com"
SECRET = "x" * 40
ADMIN_USER = "admin@example.com"
ADMIN_PW = "admin-password-localtest"

# A neutral served model — `storage_config` carries a sentinel that must NEVER reach the rendered page.
ORG = {
    "datasource": "acme",
    "version": 1,
    "description": "Acme Commerce — the deployed model.",
    "fiscal_year_start_month": 1,
    "key_terminology": {"AOV": "average order value"},
    "storage_connections": [
        {
            "name": "warehouse",
            "storage_type": "PostgreSQL",
            "storage_config": {"host": "host-should-not-render"},
        }
    ],
    "subject_areas": [
        {
            "name": "Sales",
            "description": "Orders and revenue.",
            "default_time_window": "last_90_days",
            "tables": [{"storage_connection": "warehouse", "schema": "public", "table": "orders"}],
            "tables_defined": [
                {
                    "name": "orders",
                    "schema": "public",
                    "storage_connection": "warehouse",
                    "grain": ["id"],
                    "description": "One row per order.",
                    "columns": [
                        {"name": "id", "type": "integer", "primary_key": True},
                        {"name": "amount", "type": "decimal", "unit": "USD"},
                    ],
                    "performance_hints": {"estimated_row_count": 184000},
                },
                {
                    # A wide, low-trust table exercising every flag + truncation + escaping.
                    "name": "products",
                    "schema": "public",
                    "storage_connection": "warehouse",
                    "grain": ["id"],
                    "description": "Master product catalog.",
                    "description_source": "ai_unvalidated",
                    "confidence": "proposed",
                    "review_state": "unreviewed",
                    "caveats": ["Coffee = category='cafe'. danger <script>alert(1)</script>"],
                    "columns": [
                        {"name": "id", "type": "uuid", "primary_key": True},
                        {"name": "sku", "type": "string"},
                        {
                            "name": "category",
                            "type": "string",
                            "description": "Type.",
                            "description_source": "ai_unvalidated",
                            "choice_field": {"book": "Book", "cafe": "Cafe"},
                        },
                        {"name": "price", "type": "decimal", "unit": "INR"},
                        {
                            "name": "cost_price",
                            "type": "decimal",
                            "unit": "INR",
                            "caveats": [
                                "~20% of rows have cost_price > price — exclude for margins."
                            ],
                        },
                        {
                            "name": "supplier_email",
                            "type": "string",
                            "sensitive": True,
                            "description": "Distributor contact.",
                        },
                        {
                            "name": "added_by_user_id",
                            "type": "uuid",
                            "foreign_key": {"table": "users", "column": "id"},
                        },
                        {"name": "created_at", "type": "timestamp"},
                        {"name": "is_active", "type": "boolean"},
                        {"name": "stock_qty", "type": "integer"},
                        {"name": "genre", "type": "string"},
                        {"name": "publisher", "type": "string"},
                        {"name": "barcode", "type": "string"},
                        {"name": "language", "type": "string"},
                    ],
                    "performance_hints": {"estimated_row_count": 6591},
                },
                {
                    # A SQL-backed (view) table — exercises the "Defining SQL" block.
                    "name": "monthly_revenue",
                    "schema": "public",
                    "storage_connection": "warehouse",
                    "source_type": "sql",
                    "sql": "SELECT date_trunc('month', placed_at) AS m, SUM(amount) FROM orders "
                    "GROUP BY 1",
                    "grain": ["m"],
                    "description": "Monthly revenue rollup.",
                    "columns": [
                        {"name": "m", "type": "date"},
                        {"name": "revenue", "type": "decimal", "unit": "USD"},
                    ],
                },
            ],
            "metrics": [
                {
                    "name": "revenue",
                    "calculation": "sum of amount",
                    "other_names": ["sales"],
                    "unit": "USD",
                    "source_tables": ["orders"],
                }
            ],
            "entities": [{"name": "customer", "value_pattern": "^C[0-9]+$"}],
            "relationships": [
                {
                    "from_table": "orders",
                    "from_column": "customer_id",
                    "to_table": "customers",
                    "to_column": "id",
                    "relationship": "many_to_one",
                    "confidence": "inferred",
                    "review_state": "unreviewed",
                }
            ],
        }
    ],
}

# A model whose table authors `column_groups` — the grouped column view (the other 12%).
GROUPED_ORG = {
    "datasource": "acme",
    "storage_connections": [{"name": "c", "storage_type": "PostgreSQL"}],
    "subject_areas": [
        {
            "name": "Catalog",
            "tables": [{"storage_connection": "c", "schema": "public", "table": "items"}],
            "tables_defined": [
                {
                    "name": "items",
                    "schema": "public",
                    "storage_connection": "c",
                    "grain": ["id"],
                    "description": "Items.",
                    "column_groups": {"Identity": ["id", "sku"], "Pricing": ["price"]},
                    "column_group_descriptions": {"Identity": "keys & codes", "Pricing": "money"},
                    "columns": [
                        {"name": "id", "type": "uuid", "primary_key": True},
                        {"name": "sku", "type": "string"},
                        {"name": "price", "type": "decimal", "unit": "USD"},
                        {"name": "notes", "type": "string"},  # in no group → trailing "Other"
                    ],
                }
            ],
        }
    ],
}


def _seed(url, datasource="SALES_DATA", org=ORG):
    s = Store.connect(url)
    s.run_migrations()
    model_store.write_datasource(s, datasource, Datasource.model_validate(org))
    s.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    url = "sqlite://" + str(tmp_path / "model.db")
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("AGAMI_DB_URL", url)
    monkeypatch.setenv("AGAMI_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("AGAMI_ADMIN_USERNAME", ADMIN_USER)
    monkeypatch.setenv("AGAMI_ADMIN_PASSWORD", ADMIN_PW)
    for v in ("AGAMI_OIDC_GOOGLE_CLIENT_ID", "AGAMI_OIDC_GOOGLE_CLIENT_SECRET"):
        monkeypatch.delenv(v, raising=False)
    s = Store.connect(url)
    s.run_migrations()
    user_store.seed_admin_from_env(s)
    s.close()
    return url


@pytest.fixture
def client(env):
    return TestClient(mcp_http.build_app(), base_url=BASE)


def _login(c):
    c.post("/admin/login", data={"username": ADMIN_USER, "password": ADMIN_PW})


# --- read helper -------------------------------------------------------------


def test_list_datasources_is_sorted(env):
    _seed(env, "ZED")
    _seed(env, "ACME")
    s = Store.connect(env)
    assert model_store.list_datasources(s) == ["ACME", "ZED"]
    s.close()


def test_tool_list_datasources_reads_the_served_store(env):
    """Regression: on a served deploy (AGAMI_DB_URL set) the credentials file never ships, so the
    tool must enumerate the deployed models from the store instead of reporting empty. Previously it
    read only the credentials INI and returned {"datasources": [], "note": "...credentials file..."}
    on every self-hosted server, even while get_datasource_schema / execute_sql worked."""
    import json

    import tools

    _seed(env, "SALES_DATA")
    result = json.loads(tools.tool_list_datasources({}))

    assert [d["datasource"] for d in result["datasources"]] == ["SALES_DATA"]
    entry = result["datasources"][0]
    assert entry["table_count"] >= 1  # ORG seeds `orders` + `products`
    assert "note" not in result  # the "run agami-connect" note must NOT fire when a model is served
    # No `model_present`: this list is built FROM the model rows, so it could only ever be the
    # literal True — a constant dressed as a check. It survives on the local path, where it really
    # does check whether `<profile>/datasource.yaml` exists.
    assert "model_present" not in entry


def test_the_served_listing_carries_the_description_a_router_chooses_on(env):
    """The tool exists to answer "which datasource does this question touch?", and
    datasource/database_type/table_count cannot. Without a description an agent had to call
    get_datasource_schema on every candidate purely to choose — and that is the ~60 KB call."""
    import json

    import tools

    _seed(env, "SALES_DATA")
    entry = json.loads(tools.tool_list_datasources({}))["datasources"][0]
    assert entry["description"] == "Acme Commerce — the deployed model."


def test_the_sole_served_datasource_is_the_active_one(env):
    """`resolve_profile` resolved explicit -> AGAMI_PROFILE -> `.config` -> the literal 'default'.
    A DB-only deploy never populates `.config`, so a served install with AGAMI_PROFILE unset always
    landed on 'default' — a profile that does not exist. `active_datasource` then named it beside
    the real datasource, `is_active` was structurally incapable of being true, and omitting
    `datasource` (which every tool description says defaults to the active profile) refused."""
    import json

    import tools

    tools._sole_served_datasource.cache_clear()
    _seed(env, "SALES_DATA")
    result = json.loads(tools.tool_list_datasources({}))
    assert result["active_datasource"] == "SALES_DATA"
    assert result["datasources"][0]["is_active"] is True
    assert tools.resolve_profile() == "SALES_DATA"
    tools._sole_served_datasource.cache_clear()


def test_two_served_datasources_leave_the_active_one_unguessed(env):
    """"Exactly one" is the whole rule. With two models deployed there is no defensible answer to
    "which did they mean", and guessing would be worse than the refusal an omitted `datasource`
    already gives."""
    import json

    import tools

    tools._sole_served_datasource.cache_clear()
    _seed(env, "SALES_DATA")
    _seed(env, "FINANCE_DATA")
    result = json.loads(tools.tool_list_datasources({}))
    assert result["active_datasource"] == "default"
    assert not any(d["is_active"] for d in result["datasources"])
    tools._sole_served_datasource.cache_clear()


def test_tool_list_datasources_served_but_empty_gives_a_deploy_hint(env):
    """A served deployment with no model yet returns an honest 'deploy a model' note — not the
    local 'run the agami-connect skill' one, which doesn't apply on a server."""
    import json

    import tools

    result = json.loads(tools.tool_list_datasources({}))
    assert result["datasources"] == []
    assert "model_deploy" in result["note"]


# --- gating + empty state ----------------------------------------------------


def test_model_requires_a_session(client):
    assert client.get("/admin/model", follow_redirects=False).status_code == 302


def test_model_route_is_get_only(client, env):
    # Read-only by construction: no write verb is mounted at /admin/model.
    _login(client)
    assert client.post("/admin/model", follow_redirects=False).status_code == 405


def test_empty_state_when_no_model(client, env):
    _login(client)
    html = client.get("/admin/model").text
    assert "No model deployed yet" in html


# --- overview ----------------------------------------------------------------


def test_overview_renders_org_and_areas(client, env):
    _seed(env)
    _login(client)
    html = client.get("/admin/model").text
    assert "acme" in html and "Sales" in html
    assert "Acme Commerce" in html  # the org description renders
    assert "AOV" in html and "average order value" in html  # glossary
    assert "warehouse" in html and "PostgreSQL" in html  # storage names/types


def test_overview_never_leaks_storage_config(client, env):
    _seed(env)
    _login(client)
    assert "host-should-not-render" not in client.get("/admin/model").text


def test_picker_only_when_multiple_datasources(client, env):
    _seed(env, "SALES_DATA")
    _login(client)
    assert 'name="datasource"' not in client.get("/admin/model").text  # one → no picker
    _seed(env, "MARKETING")
    html = client.get("/admin/model").text
    assert 'name="datasource"' in html  # two → picker
    # The picker form must have a real submit control (no-JS fallback, not only onchange JS).
    assert 'class="ds-go"' in html and 'type="submit"' in html


# --- area landing ------------------------------------------------------------


def test_area_landing_renders_tables_metrics_entities(client, env):
    _seed(env)
    _login(client)
    html = client.get("/admin/model?datasource=SALES_DATA&area=Sales").text
    assert "orders" in html  # its table
    assert "revenue" in html and "sum of amount" in html  # a metric + its calculation
    assert "customer" in html  # an entity


def test_unknown_area_falls_back_to_overview(client, env):
    _seed(env)
    _login(client)
    # A stale/unknown area param must not 500 — it degrades to the datasource overview.
    r = client.get("/admin/model?datasource=SALES_DATA&area=Nope")
    assert r.status_code == 200
    assert "acme" in r.text and "Subject areas" in r.text


# --- table (dataset) page ----------------------------------------------------


def _products(client):
    return client.get("/admin/model?datasource=SALES_DATA&area=Sales&table=products").text


def test_table_page_shows_every_flag(client, env):
    _seed(env)
    _login(client)
    html = _products(client)
    assert ">PK<" in html  # primary key
    assert "FK → users" in html  # foreign key → target
    assert ">enum<" in html  # choice_field
    assert ">INR<" in html  # unit
    assert "sensitive" in html  # sensitive column flag
    assert "caveat" in html  # a column with caveats is flagged
    assert "AI-described" in html  # ai_unvalidated table description
    assert "proposed" in html  # the table-level (low) confidence badge


def test_wide_table_collapses_extra_columns(client, env):
    _seed(env)
    _login(client)
    assert "Show all 14 columns" in _products(client)


def test_table_page_escapes_caveat(client, env):
    _seed(env)
    _login(client)
    html = _products(client)
    assert "<script>alert(1)" not in html
    assert "&lt;script&gt;" in html


def test_table_page_never_leaks_storage_config(client, env):
    _seed(env)
    _login(client)
    assert "host-should-not-render" not in _products(client)


def test_table_page_shows_relationships_and_metrics(client, env):
    _seed(env)
    _login(client)
    html = client.get("/admin/model?datasource=SALES_DATA&area=Sales&table=orders").text
    assert "Relationships" in html and "customers" in html  # a relationship touching orders
    assert "many_to_one" in html  # the cardinality renders, not just the table name
    assert "Used by metrics" in html and "revenue" in html  # a metric whose source is orders
    assert 'class="caveat"' not in html  # orders has no caveats → no stray callout


def test_table_with_no_relationships_omits_the_section(client, env):
    _seed(env)
    _login(client)
    # products is in no relationship → the filter must omit the section, not show every relationship.
    assert "Relationships" not in _products(client)


def test_empty_description_renders_a_dash(client, env):
    _seed(env)
    _login(client)
    assert 'class="dash"' in _products(client)  # description-less columns (sku, barcode…) show "—"


def test_flat_columns_when_no_authored_groups(client, env):
    _seed(env)
    _login(client)
    html = _products(client)
    assert 'class="grp"' not in html  # products authors no column_groups → flat
    assert 'class="cols"' in html and "supplier_email" in html  # …and the columns actually render


def test_grouped_columns_when_authored(client, env):
    _seed(env, "CATALOG", GROUPED_ORG)
    _login(client)
    html = client.get("/admin/model?datasource=CATALOG&area=Catalog&table=items").text
    assert 'class="grp"' in html  # collapsible groups
    assert "Identity" in html and "keys &amp; codes" in html  # group label + gloss
    assert "Pricing" in html
    assert "Other" in html  # a column in no authored group falls into a trailing group


def test_unknown_table_falls_back_to_area(client, env):
    _seed(env)
    _login(client)
    # A stale/unknown table param degrades to the area landing, not a 500.
    r = client.get("/admin/model?datasource=SALES_DATA&area=Sales&table=nope")
    assert r.status_code == 200
    assert "Tables" in r.text and "orders" in r.text


# --- domain context (markdown) ----------------------------------------------


def test_md_renders_subset_and_escapes_raw_html():
    out = ui.md("# Title\n\nHello **bold** and `c`.\n\n- one\n- two\n\n<script>alert(1)</script>")
    assert "<h1>Title</h1>" in out
    assert "<strong>bold</strong>" in out and "<code>c</code>" in out
    assert "<ul>" in out and "<li>one</li>" in out
    assert "<script>alert(1)" not in out and "&lt;script&gt;" in out


def test_md_fenced_code_and_link_scheme_check():
    out = ui.md("```\nSELECT 1\n```\n\n[ok](https://example.com) [bad](javascript:alert(1))")
    assert "<pre" in out and "SELECT 1" in out
    assert 'href="https://example.com"' in out
    assert "javascript:" not in out  # a non-http link degrades to plain text, never a live href


def test_md_empty_and_unterminated_fence():
    assert ui.md("") == ""
    out = ui.md(
        "```\nopen fence never closed"
    )  # an unterminated fence still renders, dropping nothing
    assert "open fence never closed" in out


def test_md_inline_code_is_literal_not_reparsed():
    # A link or bold inside backticks must stay literal text in a <code> span — not become a live
    # <a>/<strong> (Copilot finding: code spans were re-parsed after substitution).
    out = ui.md("Use the syntax `[label](https://example.com)` and `**not bold**`.")
    assert "<code>[label](https://example.com)</code>" in out
    assert "<a href" not in out  # the link inside backticks did NOT become live
    assert "<code>**not bold**</code>" in out and "<strong>" not in out


def test_context_page_renders_org_md(client, env):
    _seed(env)
    s = Store.connect(env)
    model_store.write_memory(s, "SALES_DATA", datasource_doc="# About\n\nAcme **notes**.")
    s.close()
    _login(client)
    html = client.get("/admin/model?datasource=SALES_DATA&view=context").text
    assert "<h1>About</h1>" in html and "<strong>notes</strong>" in html


def test_context_page_escapes_doc(client, env):
    _seed(env)
    s = Store.connect(env)
    model_store.write_memory(s, "SALES_DATA", datasource_doc="<script>alert(1)</script>")
    s.close()
    _login(client)
    html = client.get("/admin/model?datasource=SALES_DATA&view=context").text
    assert "<script>alert(1)" not in html and "&lt;script&gt;" in html


def test_context_page_empty_when_no_doc(client, env):
    _seed(env)
    _login(client)
    html = client.get("/admin/model?datasource=SALES_DATA&view=context").text
    assert "No domain context" in html


# --- cross-area (org-level) objects are not dropped --------------------------

CROSS_ORG = {
    "datasource": "acme",
    "storage_connections": [{"name": "c", "storage_type": "PostgreSQL"}],
    "subject_areas": [{"name": "A", "tables": [], "tables_defined": []}],
    "cross_subject_area_metrics": [{"name": "global_rev", "calculation": "sum all"}],
    "cross_subject_area_entities": [{"name": "company"}],
}


def test_cross_area_objects_surface_on_overview(client, env):
    _seed(env, "X", CROSS_ORG)
    _login(client)
    html = client.get("/admin/model?datasource=X").text
    assert "Cross-area metrics" in html and "global_rev" in html
    assert "Cross-area entities" in html and "company" in html


# --- cross-area relationships view -------------------------------------------

XREL_ORG = {
    "datasource": "acme",
    "storage_connections": [{"name": "c", "storage_type": "PostgreSQL"}],
    "subject_areas": [
        {"name": "finance", "tables": [], "tables_defined": []},
        {"name": "billing", "tables": [], "tables_defined": []},
        {"name": "crm", "tables": [], "tables_defined": []},
    ],
    "cross_subject_area_relationships": [
        {
            "from_table": "revenue",
            "to_table": "subscriptions",
            "from_column": "sub_id",
            "to_column": "id",
            "from_schema": "finance",
            "to_schema": "billing",
            "join_type": "LEFT",
            "relationship": "many_to_one",
            "confidence": "proposed",
            "review_state": "approved",
            "from_subject_area": "finance",
            "to_subject_area": "billing",
        },
        {
            "from_table": "revenue",
            "to_table": "customers",
            "from_column": "cust_id",
            "to_column": "id",
            "from_schema": "finance",
            "to_schema": "billing",
            "join_type": "LEFT",
            "relationship": "many_to_one",
            "confidence": "proposed",
            "review_state": "approved",
            "from_subject_area": "finance",
            "to_subject_area": "billing",
        },
        {
            "from_table": "invoice",
            "to_table": "account",
            "from_column": "acct_id",
            "to_column": "id",
            "from_schema": "billing",
            "to_schema": "crm",
            "join_type": "LEFT",
            "relationship": "many_to_one",
            "confidence": "confirmed",
            "review_state": "approved",
            "from_subject_area": "billing",
            "to_subject_area": "crm",
        },
    ],
}


def test_relationships_node_only_when_cross_rels_exist(client, env):
    _seed(env)  # the demo ORG has no cross-area relationships
    _login(client)
    assert "view=relationships" not in client.get("/admin/model").text  # no dead node
    _seed(env, "XR", XREL_ORG)
    assert "view=relationships" in client.get("/admin/model?datasource=XR").text  # node appears


def test_relationships_view_groups_by_area_pair(client, env):
    _seed(env, "XR", XREL_ORG)
    _login(client)
    html = client.get("/admin/model?datasource=XR&view=relationships").text
    assert "Cross-area relationships" in html
    assert "finance" in html and "billing" in html and "crm" in html  # the area-pairs
    assert "revenue" in html and "subscriptions" in html  # a schema-qualified endpoint pair
    assert "many_to_one" in html  # the cardinality
    # finance→billing has 2 rels, billing→crm has 1 → grouped (3 total, 2 groups)
    assert html.count('class="grp"') == 2
    assert html.count('class="rel"') == 3


def test_relationships_view_empty_when_none(client, env):
    _seed(env)  # demo: no cross-area rels — reachable by direct URL, must not 500
    _login(client)
    r = client.get("/admin/model?datasource=SALES_DATA&view=relationships")
    assert r.status_code == 200
    assert "No cross-area relationships" in r.text


# --- odds and ends -----------------------------------------------------------


def test_human_count_branches():
    assert admin._human_count(None) == ""
    assert admin._human_count(42) == "≈ 42"
    assert admin._human_count(6591) == "≈ 6.6k"
    assert admin._human_count(184000) == "≈ 184k"
    assert admin._human_count(2_400_000) == "≈ 2.4M"


def test_sql_backed_table_shows_defining_sql(client, env):
    _seed(env)
    _login(client)
    html = client.get("/admin/model?datasource=SALES_DATA&area=Sales&table=monthly_revenue").text
    assert "Defining SQL" in html and "date_trunc" in html


def test_stale_datasource_param_falls_back_to_first(client, env):
    _seed(env, "SALES_DATA")
    _login(client)
    # An unknown datasource param degrades to the first served one, not a 500.
    r = client.get("/admin/model?datasource=DOES_NOT_EXIST")
    assert r.status_code == 200
    assert "acme" in r.text and "Subject areas" in r.text


def test_a_model_with_no_description_omits_the_key_rather_than_sending_empty(env):
    """Absent, not "". An empty string reads as "described as nothing"; omitting the key says
    "this model declares no description", which is the true statement and the one a router can
    act on."""
    import json

    import tools

    org = dict(ORG)
    org.pop("description")
    s = Store.connect(env)
    s.run_migrations()
    model_store.write_datasource(s, "NO_DESC", Datasource.model_validate(org))
    s.close()

    entry = json.loads(tools.tool_list_datasources({}))["datasources"][0]
    assert entry["datasource"] == "NO_DESC"
    assert "description" not in entry


def test_no_served_model_leaves_the_profile_unresolved(env):
    """Zero served datasources is not "one", so the store step contributes nothing and the literal
    fallback stands. The deploy hint is what should fire here, not a guessed profile."""
    import json

    import tools

    tools._sole_served_datasource.cache_clear()
    result = json.loads(tools.tool_list_datasources({}))
    assert result["datasources"] == []
    assert tools.resolve_profile() == "default"
    tools._sole_served_datasource.cache_clear()


def test_an_explicit_profile_still_wins_over_the_served_model(env):
    """The store step is a FALLBACK — it sits below the explicit argument and AGAMI_PROFILE, so it
    can only ever fill in where nothing else answered. A deployment that names its profile keeps
    naming it."""
    import tools

    tools._sole_served_datasource.cache_clear()
    _seed(env, "SALES_DATA")
    assert tools.resolve_profile("CHOSEN") == "CHOSEN"
    tools._sole_served_datasource.cache_clear()
