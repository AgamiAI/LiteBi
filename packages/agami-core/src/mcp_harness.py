#!/usr/bin/env python3
"""
agami serve — the local, single-player MCP server (stdio transport).

A thin adapter over the shared tool registry (`tools.TOOLS`): it speaks newline-delimited
JSON-RPC 2.0 on stdin/stdout so a developer can use agami from any client that launches a local
stdio server — chiefly Claude Code (`claude mcp add`) and Claude Desktop. The HTTP transport
(`mcp_http`) advertises the *same* registry, so the two surfaces never drift.

Security model = the OS user boundary. A stdio server is a child process of the client, running
as you, reading the creds you already have. There is deliberately NO authentication, NO network
call, NO telemetry — this entrypoint never binds a port (that's the HTTP product, `mcp_http`).

Wire it up (the agami-core package must be installed in the chosen python):
    # Claude Code
    claude mcp add agami -- /ABS/PATH/python3 -m mcp_harness

    # Claude Desktop — claude_desktop_config.json
    {
      "mcpServers": {
        "agami": {
          "command": "/ABS/PATH/python3",
          "args": ["-m", "mcp_harness"],
          "env": { "AGAMI_PROFILE": "main" }
        }
      }
    }
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable

from tools import (
    SERVER_NAME,
    TOOLS,
    bootstrap_paths,
    server_instructions,
    server_version,
)

# MCP negotiates a protocol version during `initialize`: the client names the version it wants and
# the server echoes it back to accept it (see _handle_initialize). This is the value we assume only
# when a client connects without naming one (older clients) — it's a fallback, not a pin.
DEFAULT_PROTOCOL_VERSION = "2024-11-05"


def _log(msg: str) -> None:
    """Diagnostics go to stderr — stdout is reserved for protocol messages."""
    sys.stderr.write(f"[agami-mcp] {msg}\n")
    sys.stderr.flush()


def _send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def _result(req_id: Any, result: Any) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id: Any, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def _handle_initialize(req_id: Any, params: dict[str, Any]) -> None:
    requested = params.get("protocolVersion")
    protocol = requested if isinstance(requested, str) and requested else DEFAULT_PROTOCOL_VERSION
    _result(req_id, {
        "protocolVersion": protocol,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": server_version()},
        "instructions": server_instructions(),
    })


def _handle_tools_list(req_id: Any) -> None:
    _result(req_id, {
        "tools": [
            {"name": name, "description": meta["description"], "inputSchema": meta["inputSchema"]}
            for name, meta in TOOLS.items()
        ]
    })


def _handle_tools_call(req_id: Any, params: dict[str, Any]) -> None:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    meta = TOOLS.get(name)
    if meta is None:
        _error(req_id, -32602, f"Unknown tool: {name}")
        return
    handler: Callable[[dict[str, Any]], str] = meta["handler"]
    try:
        text = handler(arguments)
        _result(req_id, {"content": [{"type": "text", "text": text}], "isError": False})
    except Exception as exc:  # never let one tool call kill the server
        _log(f"tool {name} raised: {exc!r}")
        _result(req_id, {
            "content": [{"type": "text", "text": json.dumps({"error": {"kind": "other", "remediation": str(exc)}})}],
            "isError": True,
        })


def serve() -> int:
    bootstrap_paths()
    _log(f"starting (version {server_version()}); reading stdio JSON-RPC")
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            _log(f"skipping non-JSON line: {line[:80]!r}")
            continue

        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}

        # Notifications (no id) — acknowledge by doing nothing that needs a reply.
        if req_id is None:
            if method == "notifications/initialized":
                _log("client initialized")
            # notifications/cancelled, etc. are safely ignored
            continue

        if method == "initialize":
            _handle_initialize(req_id, params)
        elif method == "ping":
            _result(req_id, {})
        elif method == "tools/list":
            _handle_tools_list(req_id)
        elif method == "tools/call":
            _handle_tools_call(req_id, params)
        else:
            _error(req_id, -32601, f"Method not found: {method}")
    _log("stdin closed; exiting")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(serve())
    except KeyboardInterrupt:
        sys.exit(0)
