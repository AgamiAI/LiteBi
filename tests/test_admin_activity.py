"""The admin Activity view — every tool call folded into its conversation (thread ▸ turn ▸ call).

Covers the read helper's grouping/degradation (all call types, audit-complete) and the rendered tab
(actor shown, SQL/question escaped, no secret leak, admin-gated).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("starlette")
pytest.importorskip("mcp")
pytest.importorskip("argon2")

PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import mcp_http  # noqa: E402
import model_store  # noqa: E402
import user_store  # noqa: E402
from contracts import ToolCallRecord  # noqa: E402
from model_store import DbActivitySink  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402
from store import Store  # noqa: E402

BASE = "https://your-host.example.com"
SECRET = "x" * 40
ADMIN_USER = "admin@example.com"
ADMIN_PW = "admin-password-localtest"


def _call(s, **kw):
    rec = {"ts": kw.pop("ts"), "tool_name": kw.pop("tool_name", "execute_sql"), "source": "mcp_server"}
    rec.update(kw)
    DbActivitySink(s).record_tool_call(ToolCallRecord(**rec))


def _observed(s, **kw):
    """A call an embedder dispatched itself, so the grouping fields were observed rather than
    self-reported. Any non-default `source` means that; the value is the embedder's to choose."""
    _call(s, source="embedded", **kw)


def _just_after(html: str, needle: str, n: int = 200) -> str:
    """The rendered text immediately following `needle` — where a per-question marker would sit."""
    return html[html.index(needle) : html.index(needle) + n]


@pytest.fixture
def env(tmp_path, monkeypatch):
    url = "sqlite://" + str(tmp_path / "activity.db")
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


# --- read helpers ------------------------------------------------------------


def test_list_sessions_groups_by_thread_and_degrades(env):
    s = Store.connect(env)
    _call(s, ts="2026-06-27T10:39:00Z", actor="jordan@example.com", sql="A", success=True,
          thread_id="t1", user_question="q1", execution_ms=10)
    _call(s, ts="2026-06-27T10:42:00Z", actor="jordan@example.com", sql="B", success=False,
          thread_id="t1", execution_ms=20)
    _call(s, ts="2026-06-27T10:41:00Z", actor="sam@example.com", sql="C", success=True,
          thread_id="t2", execution_ms=30)
    _call(s, ts="2026-06-27T10:30:00Z", actor="morgan@example.com", sql="D", success=True,
          thread_id=None, execution_ms=40)  # no thread → its own singleton session
    _call(s, ts="2026-06-27T10:50:00Z", actor="pat@example.com", sql="E", success=True,
          thread_id="t3")  # no latency recorded → avg_ms must be None
    sessions = model_store.list_sessions(s)
    s.close()
    by_thread = {x["thread_id"]: x for x in sessions if x["thread_id"]}
    assert by_thread["t1"]["call_count"] == 2 and by_thread["t1"]["error_count"] == 1
    assert by_thread["t1"]["started"] == "2026-06-27T10:39:00Z"
    assert by_thread["t1"]["last_activity"] == "2026-06-27T10:42:00Z"
    assert by_thread["t1"]["avg_ms"] == 15
    assert by_thread["t3"]["avg_ms"] is None  # all-null latency degrades cleanly
    assert by_thread["t2"]["call_count"] == 1
    singletons = [x for x in sessions if x["thread_id"] is None]
    assert len(singletons) == 1 and singletons[0]["call_count"] == 1  # degraded → ungrouped


def test_list_sessions_does_not_blend_different_actors_on_a_colliding_thread(env):
    # thread_id is self-reported (untrusted); a collision across users must not merge/misattribute them.
    s = Store.connect(env)
    _call(s, ts="2026-06-27T10:00:00Z", actor="jordan@example.com", sql="A", success=True, thread_id="shared")
    _call(s, ts="2026-06-27T10:01:00Z", actor="sam@example.com", sql="B", success=True, thread_id="shared")
    sessions = model_store.list_sessions(s)
    s.close()
    assert len(sessions) == 2
    assert {x["actor"] for x in sessions} == {"jordan@example.com", "sam@example.com"}
    assert all(x["call_count"] == 1 for x in sessions)


def test_list_sessions_folds_non_query_calls_into_the_conversation(env):
    # The point of the unified view: a non-execute_sql call sharing a thread_id groups into the SAME
    # conversation as the query (not dropped, as it was when list_sessions filtered to execute_sql).
    s = Store.connect(env)
    _call(s, ts="2026-06-27T10:00:00Z", tool_name="list_datasources", actor="a", success=True,
          thread_id="t1", user_question="what datasources do I have?", correlation_id="c0")
    _call(s, ts="2026-06-27T10:01:00Z", tool_name="execute_sql", actor="a", sql="SELECT 1", success=True,
          datasource="SALES_DATA", thread_id="t1", user_question="revenue?", correlation_id="c1")
    sessions = model_store.list_sessions(s)
    s.close()
    assert len(sessions) == 1  # one conversation holding both calls
    conv = sessions[0]
    assert conv["call_count"] == 2
    tool_names = {c["tool_name"] for t in conv["turns"] for c in t["calls"]}
    assert tool_names == {"list_datasources", "execute_sql"}
    # The conversation opens with a datasource-less list_datasources; the row still shows the real
    # datasource from the call that had one (not "—").
    assert conv["datasources"] == ["SALES_DATA"]


def test_conversation_lists_every_datasource_it_touched_in_order(env):
    # A conversation can switch datasources mid-session (or a single turn can span two). The row shows
    # the full distinct set in first-seen order, skipping datasource-less calls (list_datasources).
    s = Store.connect(env)
    _call(s, ts="2026-06-27T10:00:00Z", tool_name="list_datasources", actor="a", success=True,
          thread_id="t1", correlation_id="c0")  # no datasource — contributes nothing
    _call(s, ts="2026-06-27T10:01:00Z", actor="a", sql="SELECT 1", success=True,
          datasource="SALES_DATA", thread_id="t1", correlation_id="c1")
    _call(s, ts="2026-06-27T10:02:00Z", actor="a", sql="SELECT 2", success=True,
          datasource="SUPPORT_DATA", thread_id="t1", correlation_id="c1")  # same turn, 2nd datasource
    _call(s, ts="2026-06-27T10:03:00Z", actor="a", sql="SELECT 3", success=True,
          datasource="SALES_DATA", thread_id="t1", correlation_id="c2")  # back to the first, no dupe
    sessions = model_store.list_sessions(s)
    s.close()
    assert sessions[0]["datasources"] == ["SALES_DATA", "SUPPORT_DATA"]  # distinct, first-seen order


def test_activity_drawer_shows_each_call_datasource_for_a_spanning_turn(client, env):
    # One turn (correlation_id="c1") spanning two datasources — each call card must show its OWN
    # datasource, and the conversation row both.
    s = Store.connect(env)
    _call(s, ts="2026-06-27T10:01:00Z", actor="jordan@example.com", sql="SELECT a FROM x", success=True,
          datasource="SALES_DATA", thread_id="t1", correlation_id="c1", user_question="compare sales vs support")
    _call(s, ts="2026-06-27T10:01:30Z", actor="jordan@example.com", sql="SELECT b FROM y", success=True,
          datasource="SUPPORT_DATA", thread_id="t1", correlation_id="c1", user_question="compare sales vs support")
    s.close()
    _login(client)
    html = client.get("/admin?tab=activity").text
    assert "SALES_DATA" in html and "SUPPORT_DATA" in html  # both datasources surfaced, per call


def test_list_sessions_keeps_a_thread_less_non_query_call_as_a_singleton(env):
    # Audit-complete: a call with NO thread_id is never dropped — it shows as its own conversation,
    # whatever the tool (here a non-query get_datasource_schema with no SQL).
    s = Store.connect(env)
    _call(s, ts="2026-06-27T10:00:00Z", tool_name="get_datasource_schema", actor="a", success=True)
    sessions = model_store.list_sessions(s)
    s.close()
    assert len(sessions) == 1 and sessions[0]["call_count"] == 1
    assert sessions[0]["turns"][0]["calls"][0]["tool_name"] == "get_datasource_schema"


# --- the rendered tabs -------------------------------------------------------


@pytest.fixture
def client(env):
    return TestClient(mcp_http.build_app(), base_url=BASE)


def _login(c):
    c.post("/admin/login", data={"username": ADMIN_USER, "password": ADMIN_PW})


def test_activity_tab_renders_query_and_non_query_calls(client, env):
    # A conversation with both a non-query call (list_datasources — tool name, no SQL) and a query call
    # (SQL + rows + latency). Both must render in the one Activity drawer.
    s = Store.connect(env)
    _call(s, ts="2026-06-27T10:41:00Z", tool_name="list_datasources", actor="jordan@example.com",
          success=True, thread_id="t1", correlation_id="c0", user_question="what can I ask about?")
    _call(s, ts="2026-06-27T10:42:00Z", actor="jordan@example.com", datasource="SALES_DATA",
          sql="SELECT region FROM orders", row_count=5, execution_ms=84, success=True,
          thread_id="t1", correlation_id="c1", user_question="revenue by region?")
    s.close()
    _login(client)
    html = client.get("/admin?tab=activity").text
    assert "jordan@example.com" in html and "SALES_DATA" in html
    assert "list_datasources" in html  # the non-query call shows its tool name (no SQL)
    assert "SELECT region FROM orders" in html and "revenue by region?" in html  # the query, in the drawer


def test_activity_views_escape_sql_and_question(client, env):
    s = Store.connect(env)
    _call(s, ts="2026-06-27T10:42:00Z", actor="x", sql="SELECT '<script>alert(1)</script>'",
          user_question="<img src=x onerror=alert(1)>", success=True, thread_id="t1")
    s.close()
    _login(client)
    html = client.get("/admin?tab=activity").text
    assert "<script>alert(1)" not in html and "<img src=x" not in html
    assert "&lt;script&gt;" in html


def test_activity_tab_groups_and_handles_missing_question(client, env):
    s = Store.connect(env)
    _call(s, ts="2026-06-27T10:42:00Z", actor="jordan@example.com", sql="A", success=True,
          thread_id="t1", user_question="what is the revenue by region")
    _call(s, ts="2026-06-27T10:30:00Z", actor="sam@example.com", sql="B", success=True, thread_id=None)
    s.close()
    _login(client)
    html = client.get("/admin?tab=activity").text
    assert "what is the revenue by region" in html  # the self-reported question
    assert "no question reported" in html  # graceful when Claude didn't supply one


def test_activity_requires_a_session(client):
    assert client.get("/admin?tab=activity", follow_redirects=False).status_code == 302


def test_activity_does_not_leak_the_password_hash(client, env):
    s = Store.connect(env)
    _call(s, ts="2026-06-27T10:42:00Z", actor=ADMIN_USER, sql="SELECT 1", success=True)
    s.close()
    _login(client)
    assert "password_hash" not in client.get("/admin?tab=activity").text


def test_activity_tab_has_one_unified_tab_and_no_split_tabs(client, env):
    # one Activity tab — the Tool-calls / Sessions split is gone from the rendered nav.
    _login(client)
    html = client.get("/admin?tab=activity").text
    assert ">Activity</a>" in html
    assert ">Tool calls</a>" not in html and ">Sessions</a>" not in html
    # And the old redundant helper sentences stay gone.
    assert "Every tool call, newest first" not in html
    assert "Queries grouped into conversations" not in html


# --- the turn level (correlation_id) --------------------------------


def test_correlation_id_round_trips(env):
    s = Store.connect(env)
    _call(
        s,
        ts="2026-06-28T10:00:00Z",
        actor="a",
        sql="SELECT 1",
        success=True,
        thread_id="t1",
        correlation_id="turn-1",
    )
    sessions = model_store.list_sessions(s)
    s.close()
    assert sessions[0]["turns"][0]["calls"][0]["correlation_id"] == "turn-1"


def test_execute_sql_inputschema_exposes_correlation_id():
    import tools

    props = tools.TOOLS["execute_sql"]["inputSchema"]["properties"]
    assert "correlation_id" in props
    assert "turn" in props["correlation_id"]["description"].lower()


def test_server_instructions_ask_for_verbatim_question_and_per_turn_correlation():
    import tools

    instr = tools.SERVER_INSTRUCTIONS
    assert "correlation_id" in instr and "VERBATIM" in instr


def test_turns_use_earliest_question_and_group_refinements(env):
    # Two calls of ONE turn: the 2nd drifts user_question; the turn must keep the FIRST (verbatim) one.
    s = Store.connect(env)
    _call(
        s,
        ts="2026-06-28T10:00:00Z",
        actor="a",
        sql="Q1",
        success=True,
        thread_id="t",
        correlation_id="c1",
        user_question="feature adoption vs churn",
        agent_query="band",
    )
    _call(
        s,
        ts="2026-06-28T10:01:00Z",
        actor="a",
        sql="Q2",
        success=True,
        thread_id="t",
        correlation_id="c1",
        user_question="churn by cancel_reason (drift)",
        agent_query="lost MRR",
    )
    sessions = model_store.list_sessions(s)
    s.close()
    turns = sessions[0]["turns"]
    assert len(turns) == 1
    assert turns[0]["question"] == "feature adoption vs churn"  # earliest, not the drift
    assert [c["sql"] for c in turns[0]["calls"]] == [
        "Q1",
        "Q2",
    ]  # chronological, both refinements


def test_turn_question_comes_from_earliest_call_that_reported_one(env):
    # A turn now folds in setup calls (get_datasource_schema) that carry no user_question; they lead the
    # turn chronologically but must NOT mask the real question the execute_sql reported.
    s = Store.connect(env)
    _call(s, ts="2026-06-28T10:00:00Z", tool_name="get_datasource_schema", actor="a", success=True,
          thread_id="t", correlation_id="c1")  # no user_question
    _call(s, ts="2026-06-28T10:01:00Z", actor="a", sql="Q", success=True, datasource="SALES_DATA",
          thread_id="t", correlation_id="c1", user_question="give me the incident trend")
    sessions = model_store.list_sessions(s)
    s.close()
    turn = sessions[0]["turns"][0]
    assert turn["question"] == "give me the incident trend"  # not None from the leading schema call


def test_turns_degrade_to_singletons_without_correlation_id(env):
    s = Store.connect(env)
    _call(
        s,
        ts="2026-06-28T10:00:00Z",
        actor="a",
        sql="A",
        success=True,
        thread_id="t",
        user_question="q-a",
    )  # no correlation_id
    _call(
        s,
        ts="2026-06-28T10:01:00Z",
        actor="a",
        sql="B",
        success=True,
        thread_id="t",
        user_question="q-b",
    )  # no correlation_id
    sessions = model_store.list_sessions(s)
    s.close()
    turns = sessions[0]["turns"]
    assert len(turns) == 2  # each bare call is its own turn (flat behaviour preserved)
    assert {t["question"] for t in turns} == {"q-a", "q-b"}


def test_activity_drawer_renders_turn_with_user_asked_and_agent_queries(client, env):
    s = Store.connect(env)
    _call(
        s,
        ts="2026-06-28T10:00:00Z",
        actor="jordan@example.com",
        sql="Q1",
        success=True,
        thread_id="t",
        correlation_id="c1",
        user_question="feature adoption vs churn",
        agent_query="churn by adoption band",
    )
    _call(
        s,
        ts="2026-06-28T10:01:00Z",
        actor="jordan@example.com",
        sql="Q2",
        success=True,
        thread_id="t",
        correlation_id="c1",
        user_question="drifted",
        agent_query="lost MRR",
    )
    s.close()
    _login(client)
    html = client.get("/admin?tab=activity").text
    assert (
        "User asked" in html and "feature adoption vs churn" in html
    )  # the turn question (earliest)
    assert "drifted" not in html  # the drifted user_question is NOT shown
    assert "churn by adoption band" in html and "lost MRR" in html  # both agent refinements
    assert "2 calls" in html


# --- every tool carries the grouping ids ----------------------------


def test_all_tools_expose_thread_and_correlation_ids():
    # The non-query tools must also accept thread_id/correlation_id so their calls can group into a
    # conversation — without these, list_datasources/get_datasource_schema/... log with NULL ids.
    import tools

    for name in ("list_datasources", "get_datasource_schema", "get_prompt_examples",
                 "execute_sql"):
        props = tools.TOOLS[name]["inputSchema"]["properties"]
        assert "thread_id" in props and "correlation_id" in props, name


def test_server_instructions_say_pass_ids_on_every_call():
    import tools

    assert "EVERY tool call" in tools.SERVER_INSTRUCTIONS


# --- provenance on the turn question -----------------------------------------


def test_the_question_marker_tracks_who_reported_it(client, env):
    """One test, both paths, deliberately: the marker is a trust signal, and a change to either path
    that silently flipped the other would be exactly the regression worth catching.

    On the transport path the question is copied out of the model's own tool arguments, so it is a
    self-report and says so. A caller that dispatched the tool itself observed the question directly;
    there the marker would understate the trust, so it is dropped."""
    s = Store.connect(env)
    _call(s, ts="2026-06-28T10:00:00Z", actor="jordan@example.com", sql="Q1", success=True,
          thread_id="t-mcp", correlation_id="c-mcp", user_question="reported by the model",
          agent_query="the model's framing")
    _observed(s, ts="2026-06-28T10:05:00Z", actor="jordan@example.com", sql="Q2", success=True,
              thread_id="t-obs", correlation_id="c-obs", user_question="observed by the caller",
              agent_query="still the model's framing")
    s.close()
    _login(client)
    html = client.get("/admin?tab=activity").text
    assert "reported by the model" in html and "observed by the caller" in html
    # exactly one turn still carries the marker...
    assert html.count("· self-reported") == 1
    # ...and it is the transport one. Checked on the text just after each question rather than by
    # position: sessions render newest-first, so the two turns' order is not the insertion order.
    assert "· self-reported" in _just_after(html, "reported by the model")
    assert "· self-reported" not in _just_after(html, "observed by the caller")
    # `· agent-reported` is about the agent's framing, not the question — it stays on BOTH paths
    assert html.count("· agent-reported") == 2


def test_a_turn_whose_calls_disagree_keeps_the_marker(env):
    """The marker signals *lower* trust, so an ambiguous turn keeps it rather than losing it."""
    s = Store.connect(env)
    _call(s, ts="2026-06-28T11:00:00Z", actor="a", sql="Q1", success=True,
          thread_id="t", correlation_id="c", user_question="mixed turn")
    _observed(s, ts="2026-06-28T11:00:01Z", actor="a", sql="Q2", success=True,
              thread_id="t", correlation_id="c")
    (session,) = model_store.list_sessions(s)
    s.close()
    (turn,) = session["turns"]
    assert turn["question_self_reported"] is True


def test_a_row_written_before_the_source_column_counts_as_self_reported(env):
    """Old rows have no source at all. Absent evidence, keep the marker."""
    s = Store.connect(env)
    s.execute(
        "INSERT INTO tool_calls (id, ts, org_id, tool_name, success, thread_id, correlation_id, "
        "user_question) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("legacy-1", "2026-06-28T12:00:00Z", "local", "execute_sql", 1, "t", "c", "from before"),
    )
    s.commit()
    (session,) = model_store.list_sessions(s)
    s.close()
    assert session["turns"][0]["question_self_reported"] is True
