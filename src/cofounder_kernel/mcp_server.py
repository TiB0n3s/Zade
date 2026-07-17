"""Zade MCP server — stdio, loopback-by-nature, off by default.

The first binding on top of ``agent_surface.AgentSurface``. A local agent
(Claude Desktop, Codex, Claude Agent SDK) spawns this as a subprocess and speaks
MCP over stdin/stdout; it can reach ONLY the surface's curated allowlist, and
every call is governed + audited by the kernel exactly as if the kernel made it.

Transport: newline-delimited JSON-RPC 2.0 over stdio (no dependency). Only the
messages real clients use are implemented — ``initialize``, ``notifications/
initialized``, ``tools/list``, ``tools/call``, ``ping``. Because it is stdio, it
listens on no port and is reachable only by whoever can spawn the process (the
local user) — so there is no network trust boundary to defend here.

It is never auto-started: it runs only via ``python -m cofounder_kernel mcp``.
The live surface is READ-ONLY for now (memory.search, audit.recent); mutating
tools are added deliberately once the read doorway has been watched.

``handle`` is a pure function of (server-state, message) so the protocol logic is
tested without real stdio; ``serve`` is the thin I/O loop around it.
"""
from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from .agent_surface import READ, AgentSurface
from .config import KernelConfig, load_config
from .db import KernelDatabase
from .tools import ToolRegistry

# Protocol version we implement. On initialize we echo the client's requested
# version when it sends one (interop convention), else advertise this.
PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "zade", "title": "Zade governed surface", "version": "0.1.0"}

# The LIVE surface for the shipped server: read-only. Mutating tools stay off the
# wire until the founder promotes them (agent_surface.EXPOSED knows about
# memory.write; the server just doesn't expose it yet).
LIVE_EXPOSED = {"memory.search": READ, "audit.recent": READ}

# JSON-RPC error codes.
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602


class McpServer:
    def __init__(self, surface: AgentSurface):
        self.surface = surface
        self.client = "unknown"
        self.initialized = False

    # -- request routing --------------------------------------------------
    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Return a JSON-RPC response, or None for a notification (no reply)."""
        if message.get("jsonrpc") != "2.0" or "method" not in message:
            return _error(message.get("id"), _INVALID_REQUEST, "Not a JSON-RPC 2.0 request.")
        method = message["method"]
        msg_id = message.get("id")
        params = message.get("params") or {}
        is_notification = "id" not in message

        if method == "initialize":
            return self._initialize(msg_id, params)
        if method == "notifications/initialized":
            self.initialized = True
            return None  # notification: no response
        if method == "ping":
            return _result(msg_id, {})
        if method == "tools/list":
            return _result(msg_id, {"tools": self._tools_list()})
        if method == "tools/call":
            return self._tools_call(msg_id, params)

        # Unknown method: error for requests, silence for notifications.
        if is_notification:
            return None
        return _error(msg_id, _METHOD_NOT_FOUND, f"Unknown method: {method}")

    def _initialize(self, msg_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        client_info = params.get("clientInfo") or {}
        self.client = str(client_info.get("name") or "unknown")
        requested = params.get("protocolVersion")
        version = requested if isinstance(requested, str) and requested else PROTOCOL_VERSION
        return _result(
            msg_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": (
                    "Zade's governed surface. You may call only the listed tools; they are "
                    "audited and cannot reach the machine, delete data, or send data off-box."
                ),
            },
        )

    def _tools_list(self) -> list[dict[str, Any]]:
        tools = []
        for tool in self.surface.manifest():
            tools.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": tool.input_schema,
                    "annotations": tool.annotations,
                }
            )
        return tools

    def _tools_call(self, msg_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            return _error(msg_id, _INVALID_PARAMS, "tools/call requires a string 'name'.")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _error(msg_id, _INVALID_PARAMS, "tools/call 'arguments' must be an object.")
        result = self.surface.call(name, arguments, client=self.client)
        # MCP wraps a tool outcome in content + isError; the structured payload
        # rides along in structuredContent for clients that use it.
        payload = json.dumps(result.data, ensure_ascii=False, indent=2)
        return _result(
            msg_id,
            {
                "content": [{"type": "text", "text": payload}],
                "structuredContent": result.data,
                "isError": not result.ok,
            },
        )

    # -- stdio loop -------------------------------------------------------
    def serve(self, stdin: TextIO, stdout: TextIO) -> None:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                _write(stdout, _error(None, _PARSE_ERROR, "Invalid JSON."))
                continue
            if not isinstance(message, dict):
                _write(stdout, _error(None, _INVALID_REQUEST, "Expected a JSON object."))
                continue
            response = self.handle(message)
            if response is not None:
                _write(stdout, response)


def _result(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _write(stdout: TextIO, message: dict[str, Any]) -> None:
    stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    stdout.flush()


def build_server(config: KernelConfig | None = None) -> McpServer:
    cfg = config or load_config()
    db = KernelDatabase(cfg.paths.database_path)
    # Read-only tools open the same DB the kernel uses; reads need no coordination
    # and land their audit rows in the founder-visible ledger.
    surface = AgentSurface(ToolRegistry(db), exposed=dict(LIVE_EXPOSED))
    return McpServer(surface)


def run(argv: list[str]) -> int:
    """Entry point for ``python -m cofounder_kernel mcp``. Blocks on stdio."""
    server = build_server()
    server.serve(sys.stdin, sys.stdout)
    return 0
