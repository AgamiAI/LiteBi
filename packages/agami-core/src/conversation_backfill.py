"""Stamp `conversation_id` onto tool calls written before the server started deciding it.

    AGAMI_DB_URL=postgresql://…  python -m conversation_backfill [--org ORG] [--apply]

**Why this is a command and not part of the migration.** A migration that quietly rewrote history
would be doing the one thing this whole change exists to stop: presenting a computed guess as a
recorded fact. Grouping rows that were never grouped is a judgement about what happened, so it is an
explicit act, run by a person who can read what it is about to do first. It prints a summary and
changes nothing unless `--apply` is given.

**It applies exactly the rule the server now applies**, imported rather than restated: calls by one
actor in one organization continue a conversation until a pause longer than
`tools.CONVERSATION_IDLE_MINUTES`. A second copy of that rule here would drift from the live one, and
the drift would be invisible — history and new rows grouped by two slightly different definitions,
on the same screen.

**What it cannot recover.** Where the model reported one `thread_id` across two real conversations,
nothing in the data says where the first ended; the pause rule may split them correctly, or may not,
and it has no way to tell which it did. That is why this is worth running once, on purpose, rather
than trusting: it makes history *consistent with the rule*, not *known to be right*.

**Never touches a row that already has one.** New calls are stamped at write time and are the
authoritative ones; this only fills the gap behind them.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from typing import Any

from tools import CONVERSATION_IDLE_MINUTES, _minutes_between


def plan(store: Any, org_id: str | None = None) -> list[tuple[str, str]]:
    """`(row id, conversation id)` for every unstamped call, in the order the rule walks them.

    Ordered by `(org_id, actor, ts)` so one pass assigns every row: the rule only ever looks at the
    call before it, and reading in that order means the call before it is the previous row.

    **A NULL actor never continues a conversation**, exactly as the live rule has it — presence auth
    records no actor, and chaining those together would merge everyone who ever called anonymously
    into one conversation. Each gets its own id.
    """
    where, params = "conversation_id IS NULL", ()
    if org_id:
        where, params = "conversation_id IS NULL AND org_id = ?", (org_id,)
    rows = store.query(
        f"SELECT id, org_id, actor, ts FROM tool_calls WHERE {where} "  # noqa: S608 - literal clause
        "ORDER BY org_id, actor, ts",
        params,
    )
    assignments: list[tuple[str, str]] = []
    previous_key: tuple[str, str] | None = None
    previous_ts: str | None = None
    current: str | None = None
    for raw in rows:
        row = dict(raw)
        actor = row["actor"] or ""
        key = (row["org_id"], actor)
        gap = _minutes_between(previous_ts, row["ts"]) if previous_ts else None
        same_person = key == previous_key
        within = gap is not None and gap <= CONVERSATION_IDLE_MINUTES
        if not actor or not same_person or not within or current is None:
            current = uuid.uuid4().hex
        assignments.append((row["id"], current))
        previous_key, previous_ts = key, row["ts"]
    return assignments


def summarize(assignments: list[tuple[str, str]]) -> str:
    conversations = len({conversation for _, conversation in assignments})
    return f"{len(assignments)} unstamped calls would become {conversations} conversations"


def apply(store: Any, assignments: list[tuple[str, str]]) -> int:
    """Write the plan. Guarded on `conversation_id IS NULL` in the UPDATE itself, not just in the
    plan: a call recorded between planning and applying already has an authoritative id from the
    write path, and this must not overwrite it."""
    for row_id, conversation_id in assignments:
        store.execute(
            "UPDATE tool_calls SET conversation_id = ? WHERE id = ? AND conversation_id IS NULL",
            (conversation_id, row_id),
        )
    store.commit()
    return len(assignments)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", help="only this organization (default: every one)")
    parser.add_argument(
        "--apply", action="store_true", help="write the changes (default: print what would change)"
    )
    args = parser.parse_args(argv)

    from store import Store

    if not os.environ.get("AGAMI_DB_URL"):
        print("conversation_backfill: AGAMI_DB_URL is not set", file=sys.stderr)
        return 2
    store = Store.from_env()
    if store is None:
        print("conversation_backfill: no database", file=sys.stderr)
        return 2
    try:
        assignments = plan(store, args.org)
        print(summarize(assignments))
        if not assignments:
            return 0
        if not args.apply:
            print("nothing written — pass --apply to write it")
            return 0
        print(f"stamped {apply(store, assignments)} calls")
    finally:
        store.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - the CLI entrypoint
    raise SystemExit(main())
