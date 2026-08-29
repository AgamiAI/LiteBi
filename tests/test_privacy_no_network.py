"""
Privacy invariant: NO shipped script opens a connection of its own.

agami's promise is that nothing leaves your machine unless you asked for it. This
test is the automated backing for that claim: it scans every Python script that
ships in the plugin and fails the build if any of them references a network-egress
primitive.

This docstring used to say "NO shipped script makes a network call", and that is
no longer the posture. `semantic_model/golden_run.py` starts the operator's OWN
AI client as a CHILD PROCESS to generate the SQL a golden-dataset eval grades —
their client, their account, an explicit command, and a child whose every tool,
MCP server and setting source is switched off BY A FLAG (`--tools ""`,
`--strict-mcp-config`, `--setting-sources ""`), started in an empty directory,
with an allowlisted environment. Being out of process, it matches
none of the primitives below, so nothing here went red when it landed. That is
exactly why the wording is corrected deliberately: a doc that keeps promising what
the code stopped doing is worse than one that never promised it. What the scan
still holds, and what the product still promises, is that there is no in-process
network CLIENT — nothing agami ships opens a socket itself.

What's allowed and why:
  - `urllib.parse` — pure string parsing of DSNs (execute_sql.py). NOT network.
  - DB drivers (psycopg2 / pymysql / snowflake.connector / google.cloud.bigquery)
    — these connect to *your own* database/warehouse, which is the whole point of
    local execution. They are imported lazily inside execute_sql.py and are not
    arbitrary-host egress.

What's forbidden: generic outbound HTTP/socket/mail primitives that could
exfiltrate data to an arbitrary host. If you ever genuinely need one, you are
almost certainly building the hosted product, not the local plugin — and this
test should make you stop and think.

(This replaces the old tests/test_telemetry_privacy.py, which pinned the payload
allowlist of a telemetry sender that has since been removed entirely.)
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "plugins" / "agami" / "scripts"
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
# The local serving path — skill scripts + the agami-core library (executor, stdio harness, the
# shared tool registry, the semantic model) — must stay network-free. Two modules are deliberate
# network surfaces of the HOSTED server (server-extra only; never imported by the local skill),
# excluded by design: `mcp_http` (binds a port, speaks HTTP) and `oidc` (the OIDC client's outbound
# calls to the identity provider). Users who want zero egress run the local `agami serve`.
NETWORK_MODULES = {"mcp_http.py", "oidc.py"}

# Regexes for network-egress primitives. Deliberately precise: `urllib.request`
# is forbidden but `urllib.parse` is not; DB-driver imports are not matched.
FORBIDDEN = [
    r"\burllib\.request\b",
    r"\burlopen\b",
    r"\bimport\s+socket\b",
    r"\bsocket\.socket\b",
    r"\bimport\s+http\.client\b",
    r"\bfrom\s+http\.client\b",
    r"\bhttp\.client\b",
    r"\bimport\s+requests\b",
    r"\brequests\.(get|post|put|delete|patch|request|Session)\b",
    r"\bimport\s+httpx\b",
    r"\bhttpx\.",
    r"\bimport\s+(ftplib|smtplib|telnetlib|poplib|imaplib)\b",
    r"\bimport\s+websocket",
    r"\bwebsockets\b",
]
_FORBIDDEN_RE = [re.compile(p) for p in FORBIDDEN]

SCRIPTS = (
    sorted(SCRIPTS_DIR.glob("*.py"))
    + sorted(p for p in PKG_SRC.glob("*.py") if p.name not in NETWORK_MODULES)
    + sorted((PKG_SRC / "semantic_model").glob("*.py"))
)


def test_there_are_scripts_to_scan():
    # Guard against the glob silently matching nothing (which would make the
    # scan below vacuously pass).
    assert SCRIPTS, f"no scripts found under {SCRIPTS_DIR}"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_has_no_network_egress(script: Path):
    text = script.read_text()
    hits = []
    for line_no, line in enumerate(text.splitlines(), 1):
        for rx in _FORBIDDEN_RE:
            if rx.search(line):
                hits.append(f"  {script.name}:{line_no}: {line.strip()}")
    assert not hits, (
        f"network-egress primitive found in {script.name} — agami scripts must stay "
        f"local-only (see docs/privacy.md):\n" + "\n".join(hits)
    )


def test_urllib_parse_is_still_allowed():
    # Sanity: the precise rules must NOT trip on urllib.parse (used for DSN parsing).
    sample = "import urllib.parse\nu = urllib.parse.urlparse(dsn)\n"
    assert not any(rx.search(sample) for rx in _FORBIDDEN_RE)
