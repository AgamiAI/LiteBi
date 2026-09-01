#!/usr/bin/env python3
"""Parse the golden-dataset explorer's back-channel feedback block into a
curate-ready ops set — deterministically.

The explorer page renders one dataset and queues curation actions against it.
This script does the parse so the skill reads structured output and applies it
through the golden write path (append-only, validator in front).

Input (stdin or --block-file):

    profile: <name>
    golden-ops:
    [{"op":"add-tag","dataset":"orders","id":"orders-by-month","value":"smoke"}]
    done

`golden-ops:` is a bare header line whose JSON array may span multiple lines
(value = everything up to `done`). Output (the standard contract):

    {"ok": true,
     "data": {"profile": "<name>"|null, "ops": [ ...ops verbatim... ]},
     "anomalies": [...], "needs_judgment": {...}|null}

The block names the profile it targets on its first line, and `data["profile"]`
is that name rather than whatever profile happens to be active — a page rendered
for one profile must not apply to another by accident.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_KEYS = {"profile", "golden-ops"}
_ALLOWED_OPS = frozenset(
    {"add-tag", "remove-tag", "set-match", "edit-question", "remove-item", "withdraw-confirmation"}
)
# A queued action may weaken a claim and may never grant one. Ticking a confirmed
# box from a page would forge ground truth: it is the easiest possible way to make
# a failing suite green, because a statement nobody ran becomes the thing every
# future run is measured against. Confirmation is earned by running the item and
# accepting the result through the save door — never by editing a page. The rule
# lives here, at the parser, so a hand-edited page cannot route around it either.
_CONFIRMING_FIELDS = ("sql_confirmed", "expected", "sql")


def _key_of(line: str):
    low = line.strip().lower()
    for k in _KEYS:
        if low.startswith(k + ":") or low == k + ":":
            return k
    return None


def _sections(text: str) -> dict:
    """Split the block into {key: raw value text}, value spanning until the next key/done."""
    out: dict = {}
    cur_key = None
    cur_val: list[str] = []
    for raw in text.splitlines():
        if raw.strip().lower() == "done":
            break
        k = _key_of(raw)
        if k:
            if cur_key:
                out[cur_key] = "\n".join(cur_val).strip()
            cur_key = k
            cur_val = [raw.split(":", 1)[1]]
        elif cur_key:
            cur_val.append(raw)
        # lines before the first key (headers) are ignored
    if cur_key:
        out[cur_key] = "\n".join(cur_val).strip()
    return out


def parse(text: str) -> tuple[dict, list, dict | None]:
    sec = _sections(text)
    anomalies: list = []
    needs: dict | None = None
    ops: list = []
    data: dict = {"profile": None, "ops": ops}

    if "profile" in sec:
        data["profile"] = sec["profile"].strip() or None

    if "golden-ops" not in sec:
        return data, anomalies, needs

    try:
        parsed = json.loads(sec["golden-ops"])
    except Exception as e:
        anomalies.append({"kind": "bad_json", "where": "golden-ops", "detail": str(e)})
        needs = {
            "kind": "unparseable_json",
            "section": "golden-ops",
            "ask": "the `golden-ops:` block isn't valid JSON — re-copy it from the page",
        }
        return data, anomalies, needs
    if not isinstance(parsed, list):
        anomalies.append({"kind": "golden_ops_not_list", "detail": "expected a JSON array"})
        return data, anomalies, needs

    for op in parsed:
        name = op.get("op")
        confirming = [f for f in _CONFIRMING_FIELDS if f in op]
        if confirming:
            needs = {
                "kind": "confirmation_cannot_be_granted",
                "fields": confirming,
                "ask": "an op tried to set the answer key from the page — confirming an item "
                "means running it and accepting the result through the save door, not "
                "editing the page; re-send the block without these fields",
            }
            return data, anomalies, needs  # nothing applies from a block that tried this
        if name not in _ALLOWED_OPS:
            anomalies.append({"kind": "unknown_op", "detail": str(name)})
            continue
        if not op.get("dataset") or not op.get("id"):
            anomalies.append({"kind": "op_missing_target", "detail": str(name)})
            continue
        ops.append(op)

    return data, anomalies, needs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Parse the golden-dataset explorer back-channel block."
    )
    ap.add_argument("--block-file", help="path to the pasted block (else stdin)")
    args = ap.parse_args(argv)
    text = (
        Path(args.block_file).read_text(encoding="utf-8") if args.block_file else sys.stdin.read()
    )
    data, anomalies, needs = parse(text)
    print(
        json.dumps(
            {"ok": True, "data": data, "anomalies": anomalies, "needs_judgment": needs}, indent=2
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
