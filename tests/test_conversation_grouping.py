"""The server decides which calls are one conversation, and the model has no say in it.

`thread_id` answers the same question and is the model's answer. Measured on one deployment: asked in
prose it arrived on 2 of 10 calls and then 0 of 8; made a required property it arrived on 9 of 9 and
the values collided — two conversations two days apart both came through as `t1` and were shown as
one. Handed a server-minted id to echo back, it ignored that too.

So these tests are about a value no caller can influence. What they pin is the rule, its edges, and
the two properties that make it safe: it never merges two people, and it never continues after a
pause.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/agami-core/src"))

from tools import CONVERSATION_IDLE_MINUTES, _conversation_id_for, _minutes_between  # noqa: E402


class _Store:
    """The one query the rule makes, answered from a list. A real store is not needed to pin a rule
    about ordering and gaps, and a fake makes the boundary cases writable."""

    def __init__(self, rows: list[dict] | None = None, raises: bool = False) -> None:
        self.rows = rows or []
        self.raises = raises
        self.queries = 0

    def query(self, sql: str, params: tuple):  # noqa: ARG002
        self.queries += 1
        if self.raises:
            raise RuntimeError("database is unreachable")
        return self.rows


def test_a_call_close_behind_another_continues_its_conversation():
    store = _Store([{"conversation_id": "conv-1", "ts": "2026-09-05T10:00:00Z"}])
    got = _conversation_id_for(store, "acme", "you@example.com", "2026-09-05T10:01:00Z")
    assert got == "conv-1"


def test_a_call_after_a_long_pause_starts_a_new_one():
    store = _Store([{"conversation_id": "conv-1", "ts": "2026-09-05T10:00:00Z"}])
    got = _conversation_id_for(store, "acme", "you@example.com", "2026-09-05T14:00:00Z")
    assert got != "conv-1"


def test_the_boundary_is_inclusive_and_the_minute_after_it_is_not():
    """Exactly at the threshold still continues; one minute past does not. Stated as a test because
    "longer than" and "at least" are one character apart and the difference is invisible in prose."""
    at = _Store([{"conversation_id": "conv-1", "ts": "2026-09-05T10:00:00Z"}])
    assert (
        _conversation_id_for(
            at, "acme", "you@example.com", f"2026-09-05T10:{CONVERSATION_IDLE_MINUTES:02d}:00Z"
        )
        == "conv-1"
    )
    past = _Store([{"conversation_id": "conv-1", "ts": "2026-09-05T10:00:00Z"}])
    assert (
        _conversation_id_for(
            past,
            "acme",
            "you@example.com",
            f"2026-09-05T10:{CONVERSATION_IDLE_MINUTES + 1:02d}:00Z",
        )
        != "conv-1"
    )


def test_the_first_call_anybody_makes_starts_a_conversation():
    assert _conversation_id_for(_Store([]), "acme", "you@example.com", "2026-09-05T10:00:00Z")


def test_a_call_with_no_actor_never_continues_anything():
    """**The property that makes this safe.** Presence auth records no actor, so chaining those
    together would file everyone who ever called anonymously into one conversation — the
    cross-person merge the whole design exists to make impossible. Each gets its own id, and the
    store is not even asked."""
    store = _Store([{"conversation_id": "conv-1", "ts": "2026-09-05T10:00:00Z"}])
    first = _conversation_id_for(store, "acme", None, "2026-09-05T10:00:10Z")
    second = _conversation_id_for(store, "acme", "", "2026-09-05T10:00:20Z")
    assert first != second != "conv-1"
    assert store.queries == 0, "an actorless call has nothing to look up"


def test_an_unreadable_database_costs_the_grouping_and_never_the_row():
    """A conversation id is an annotation on an audit row. Losing the row because the lookup failed
    would be the worse trade by a distance, so a failure mints a new id — which reads as a
    conversation boundary: visible and wrong, rather than silently attaching this call to somebody
    else's conversation."""
    got = _conversation_id_for(
        _Store(raises=True), "acme", "you@example.com", "2026-09-05T10:00:00Z"
    )
    assert got


def test_a_timestamp_that_cannot_be_read_is_a_boundary_not_a_zero():
    """`None` from the gap means "cannot tell", and cannot-tell must not be treated as no-time-passed
    — an unparseable timestamp is not evidence that two calls belong together."""
    assert _minutes_between("not a date", "2026-09-05T10:00:00Z") is None
    store = _Store([{"conversation_id": "conv-1", "ts": "not a date"}])
    assert (
        _conversation_id_for(store, "acme", "you@example.com", "2026-09-05T10:00:00Z") != "conv-1"
    )


@pytest.mark.parametrize(
    ("earlier", "later", "expected"),
    [
        ("2026-09-05T10:00:00Z", "2026-09-05T10:30:00Z", 30.0),
        ("2026-09-05T10:00:00+00:00", "2026-09-05T10:01:30Z", 1.5),
        ("2026-09-05T10:00:00.500Z", "2026-09-05T10:00:00.500Z", 0.0),
    ],
)
def test_the_gap_reads_the_timestamp_shapes_this_codebase_writes(earlier, later, expected):
    """Both resolutions and both spellings of UTC — `_now_iso` writes `Z`, and other writers use the
    offset form. A gap function that understood only one would silently return None for the other and
    split every conversation."""
    assert _minutes_between(earlier, later) == pytest.approx(expected)


def test_two_people_in_one_company_are_never_one_conversation():
    """The lookup is scoped by actor in the SQL, so this is really a test that the scoping is asked
    for — the fake returns whatever it is given regardless. It reads the parameters to check."""
    seen: list[tuple] = []

    class _Recording(_Store):
        def query(self, sql: str, params: tuple):
            seen.append(params)
            return []

    _conversation_id_for(_Recording(), "acme", "you@example.com", "2026-09-05T10:00:00Z")
    assert seen == [("acme", "you@example.com")], "both the company and the person must scope it"


# --- the Activity view reads it, or the column is a feature nothing consumes -------------------


def test_a_negative_gap_is_unknown_rather_than_small():
    """Raised in review. `abs()` would fold a clock that went backwards — an NTP correction, a
    rolled-back VM, rows arriving out of order — into a SMALL positive number, and a small gap
    CONTINUES a conversation. Time not moving forward is exactly where the rule has no evidence."""
    assert _minutes_between("2026-09-05T10:30:00Z", "2026-09-05T10:00:00Z") is None
    store = _Store([{"conversation_id": "conv-1", "ts": "2026-09-05T10:30:00Z"}])
    assert (
        _conversation_id_for(store, "acme", "you@example.com", "2026-09-05T10:00:00Z") != "conv-1"
    )


def test_the_activity_view_groups_by_the_servers_answer_not_the_models():
    """The column would be a feature nothing consumes if `list_sessions` still keyed on `thread_id`.
    Here two calls carry DIFFERENT self-reported thread ids and the same server-decided conversation:
    one session, because the server's answer wins."""
    from model_store import list_sessions

    class _Rows:
        def query(self, sql: str, params: tuple):  # noqa: ARG002
            return [
                {
                    "id": "c1",
                    "ts": "2026-09-05T10:00:00Z",
                    "actor": "you@example.com",
                    "tool_name": "execute_sql",
                    "datasource": "d",
                    "sql": "SELECT 1",
                    "row_count": 1,
                    "execution_ms": 5,
                    "success": 1,
                    "error_kind": None,
                    "user_question": "q",
                    "agent_query": None,
                    "thread_id": "t1",
                    "correlation_id": "x",
                    "source": "mcp_server",
                    "refusal_detail": None,
                    "refusal_remediation": None,
                    "basis": None,
                    "conversation_id": "server-1",
                },
                {
                    "id": "c2",
                    "ts": "2026-09-05T10:01:00Z",
                    "actor": "you@example.com",
                    "tool_name": "execute_sql",
                    "datasource": "d",
                    "sql": "SELECT 2",
                    "row_count": 1,
                    "execution_ms": 5,
                    "success": 1,
                    "error_kind": None,
                    "user_question": "q",
                    "agent_query": None,
                    "thread_id": "DIFFERENT",
                    "correlation_id": "y",
                    "source": "mcp_server",
                    "refusal_detail": None,
                    "refusal_remediation": None,
                    "basis": None,
                    "conversation_id": "server-1",
                },
            ]

    assert len(list_sessions(_Rows(), org_id="acme")) == 1


def test_a_row_written_before_the_column_still_groups_the_old_way():
    """The fallback is not politeness — every row written before 021 has no conversation, and
    `thread_id` is the only grouping those have. Dropping it would make the whole history singletons
    on a view whose promise is that every call appears."""
    from model_store import list_sessions

    class _Rows:
        def query(self, sql: str, params: tuple):  # noqa: ARG002
            return [
                {
                    "id": f"c{n}",
                    "ts": f"2026-09-05T10:0{n}:00Z",
                    "actor": "you@example.com",
                    "tool_name": "execute_sql",
                    "datasource": "d",
                    "sql": "SELECT 1",
                    "row_count": 1,
                    "execution_ms": 5,
                    "success": 1,
                    "error_kind": None,
                    "user_question": "q",
                    "agent_query": None,
                    "thread_id": "legacy-thread",
                    "correlation_id": "x",
                    "source": "mcp_server",
                    "refusal_detail": None,
                    "refusal_remediation": None,
                    "basis": None,
                    "conversation_id": None,
                }
                for n in (1, 2)
            ]

    assert len(list_sessions(_Rows(), org_id="acme")) == 1
