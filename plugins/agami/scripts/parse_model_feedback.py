#!/usr/bin/env python3
"""Parse the agami-model dashboard's back-channel feedback block into a single
curate-ready ops set — deterministically.

agami-model used to parse this multi-format block in prose: a `profile:` line,
comma-lists (`exclude/include tables:` + `… columns:`), and three JSON sub-blocks
(`curate-ops:` array, `new-metrics:` array, `key-terminology:` object). This
script does the parse + the exclude/include → curate-op translation, so the skill
just reads structured output and applies it via `sm curate` / `sm add` /
`sm set-terminology`.

Input (stdin or --block-file):

    profile: <name>
    exclude tables:  <area>.<table>, <area>.<table>
    include tables:  <area>.<table>
    exclude columns: <area>.<table>.<column>, ...
    curate-ops: [ {"op":"approve","kind":"metric","area":"...","name":"...","at":"..."}, ... ]
    new-metrics: [ {"area":"...","name":"...","calculation":"...", ...}, ... ]
    key-terminology: {"gold tier": "lifetime spend > $10k", ...}
    signed-off-by: jane@x.com (cfo)
    done

JSON-valued keys may span multiple lines (value = everything up to the next key
or `done`). Output (the standard contract):

    {"ok": true,
     "data": {
       "profile": "<name>"|null,
       "ops": [ ...curate ops: exclude/include translated + curate-ops merged verbatim... ],
       "new_metrics_by_area": {"<area>": [ ...metric dicts... ]},
       "key_terminology": {..}|null,
       "signer": "jane@x.com"|null, "role": "cfo"|null
     },
     "anomalies": [...], "needs_judgment": {...}|null}

A malformed target (a table without its `<area>.` prefix, unparseable JSON) is a
`needs_judgment` — the skill asks the user to fix it rather than guessing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_LIST_KEYS = {"exclude tables", "include tables", "exclude columns", "include columns"}
_JSON_KEYS = {"curate-ops", "new-metrics", "key-terminology", "example-edits", "new-examples"}
_KEYS = _LIST_KEYS | _JSON_KEYS | {"profile", "signed-off-by", "datasource-md"}
_TABLE_QNAME = re.compile(r"^[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")
_COLUMN_QNAME = re.compile(r"^[A-Za-z0-9_]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")


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
    data: dict = {"profile": None, "ops": ops, "new_metrics_by_area": {},
                  "examples_by_area": {}, "key_terminology": None, "datasource_md": None,
                  "signer": None, "role": None}

    if "profile" in sec:
        data["profile"] = sec["profile"].strip() or None
    if "signed-off-by" in sec:
        sb = sec["signed-off-by"].strip()
        if "/" in sb:  # `email / role`
            email, role = sb.split("/", 1)
            data["signer"], data["role"] = email.strip(), role.strip() or None
        elif "(" in sb:  # `email (role)`
            email, rest = sb.split("(", 1)
            data["signer"], data["role"] = email.strip(), rest.rstrip(")").strip() or None
        else:
            data["signer"] = sb or None

    def _qnames(val: str) -> list[str]:
        return [t.strip() for t in val.replace("\n", ",").split(",") if t.strip()]

    bad_targets: list[str] = []
    for key, op, is_col in (("exclude tables", "exclude", False), ("include tables", "include", False),
                            ("exclude columns", "exclude", True), ("include columns", "include", True)):
        if key not in sec:
            continue
        for q in _qnames(sec[key]):
            rx = _COLUMN_QNAME if is_col else _TABLE_QNAME
            if not rx.match(q):
                bad_targets.append(q)
                continue
            parts = q.split(".")
            entry = {"op": op, "kind": "table", "area": parts[0], "name": parts[1]}
            if is_col:
                entry["column"] = parts[2]
            ops.append(entry)

    for jk in ("curate-ops", "new-metrics", "key-terminology", "example-edits", "new-examples", "datasource-md"):
        if jk not in sec:
            continue
        try:
            parsed = json.loads(sec[jk])
        except Exception as e:
            anomalies.append({"kind": "bad_json", "where": jk, "detail": str(e)})
            needs = {"kind": "unparseable_json", "section": jk,
                     "ask": f"the `{jk}:` block isn't valid JSON — re-copy it from the dashboard"}
            continue
        if jk == "curate-ops":
            if isinstance(parsed, list):
                ops.extend(parsed)  # merge verbatim
            else:
                anomalies.append({"kind": "curate_ops_not_list", "detail": "expected a JSON array"})
        elif jk == "new-metrics":
            for m in (parsed or []):
                area = m.get("area")
                if not area:
                    anomalies.append({"kind": "metric_missing_area", "detail": str(m.get("name"))})
                    continue
                data["new_metrics_by_area"].setdefault(area, []).append(m)
        elif jk in ("example-edits", "new-examples"):  # both apply via sm add-example
            for ex in (parsed or []):
                area = ex.get("area")
                if not area:
                    anomalies.append({"kind": "example_missing_area", "detail": str(ex.get("question"))})
                    continue
                data["examples_by_area"].setdefault(area, []).append(ex)
        elif jk == "key-terminology":
            data["key_terminology"] = parsed
        elif jk == "datasource-md":
            data["datasource_md"] = parsed  # JSON-encoded string → the full datasource.md text

    if bad_targets:
        needs = {"kind": "malformed_targets", "targets": bad_targets,
                 "ask": "these need an <area>. prefix (e.g. `sales.STG_LEADS`, not `STG_LEADS`) — confirm and re-send"}
    return data, anomalies, needs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Parse the agami-model back-channel feedback block.")
    ap.add_argument("--block-file", help="path to the pasted block (else stdin)")
    args = ap.parse_args(argv)
    text = Path(args.block_file).read_text(encoding="utf-8") if args.block_file else sys.stdin.read()
    data, anomalies, needs = parse(text)
    print(json.dumps({"ok": True, "data": data, "anomalies": anomalies, "needs_judgment": needs}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
