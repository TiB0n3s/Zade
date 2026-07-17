"""Tests for the stdio MCP server protocol logic.

`handle` is pure, so the JSON-RPC handshake and dispatch are tested without real
stdio. Pins: the live surface is READ-ONLY, off-list calls fail, unknown methods
error, and the client identity flows into audit attribution.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

from cofounder_kernel.agent_surface import READ, AgentSurface
from cofounder_kernel.db import KernelDatabase
from cofounder_kernel.mcp_server import LIVE_EXPOSED, McpServer
from cofounder_kernel.tools import ToolRegistry


def _server(tmp_path: Path) -> tuple[McpServer, KernelDatabase]:
    db = KernelDatabase(tmp_path / "kernel.sqlite")
    db.migrate()
    surface = AgentSurface(ToolRegistry(db), exposed=dict(LIVE_EXPOSED))
    return McpServer(surface), db


def _init(server: McpServer, client: str = "codex") -> dict:
    return server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": client}},
        }
    )


def test_live_surface_contents(tmp_path: Path) -> None:
    # two reads + the promoted non-destructive write; destructive stays off-wire.
    assert set(LIVE_EXPOSED) == {"memory.search", "audit.recent", "memory.write"}
    assert LIVE_EXPOSED["memory.search"] == READ
    server, _ = _server(tmp_path)
    _init(server)
    listed = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in listed["result"]["tools"]}
    assert names == {"memory.search", "audit.recent", "memory.write"}
    assert "memory.forget" not in names  # destructive remains excluded


def test_initialize_handshake(tmp_path: Path) -> None:
    server, _ = _server(tmp_path)
    resp = _init(server, client="Claude Desktop")
    r = resp["result"]
    assert r["protocolVersion"] == "2025-06-18"  # echoes the client's version
    assert "tools" in r["capabilities"]
    assert r["serverInfo"]["name"] == "zade"
    assert server.client == "Claude Desktop"


def test_initialized_notification_has_no_response(tmp_path: Path) -> None:
    server, _ = _server(tmp_path)
    out = server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert out is None
    assert server.initialized is True


def test_tools_list_exposes_schema_and_annotations(tmp_path: Path) -> None:
    server, _ = _server(tmp_path)
    _init(server)
    tools = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    search = next(t for t in tools if t["name"] == "memory.search")
    assert search["inputSchema"]["type"] == "object"
    assert search["annotations"]["readOnlyHint"] is True


def test_tools_call_read_succeeds_and_is_attributed(tmp_path: Path) -> None:
    server, db = _server(tmp_path)
    db.add_memory(kind="note", title="Runway", content="18 months", source="local", metadata={})
    _init(server, client="codex")
    resp = server.handle(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "memory.search", "arguments": {"query": "runway"}}}
    )
    result = resp["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["matches"]
    assert result["content"][0]["type"] == "text"
    # attributed to the client in the audit ledger
    assert any(e["actor"] == "mcp:codex" for e in db.recent_audit_events(10))


def test_tools_call_off_list_is_refused(tmp_path: Path) -> None:
    server, db = _server(tmp_path)
    memory_id = db.add_memory(kind="note", title="secret", content="x", source="local", metadata={})
    _init(server)
    for name, args in [("memory.forget", {"memory_id": memory_id}),
                       ("shell.rm", {})]:
        resp = server.handle(
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": name, "arguments": args}}
        )
        assert resp["result"]["isError"] is True, name
        assert resp["result"]["structuredContent"]["error"] == "not_exposed", name
    # the memory that forget targeted is untouched
    assert db.search_memories("secret", 5)


def test_tools_call_write_is_held_for_founder_approval(tmp_path: Path) -> None:
    server, db = _server(tmp_path)
    _init(server, client="codex")
    resp = server.handle(
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "memory.write", "arguments": {"title": "Agent note", "content": "written via mcp"}}}
    )
    # Not an error: the write is accepted but HELD for founder approval, not applied.
    assert resp["result"]["isError"] is False
    assert resp["result"]["structuredContent"]["status"] == "awaiting_approval"
    assert resp["result"]["structuredContent"]["approval_request_id"]
    # Nothing entered memory autonomously.
    assert db.search_memories("Agent note", 5) == []
    # Audited as gated, attributed to the calling agent.
    assert any(e["actor"] == "mcp:codex" and e["action"] == "mcp.write.gated" for e in db.recent_audit_events(10))


def test_unknown_method_errors_but_notification_is_silent(tmp_path: Path) -> None:
    server, _ = _server(tmp_path)
    err = server.handle({"jsonrpc": "2.0", "id": 5, "method": "does/not/exist"})
    assert err["error"]["code"] == -32601
    # an unknown *notification* (no id) draws no response
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/whatever"}) is None


def test_ping_and_bad_request(tmp_path: Path) -> None:
    server, _ = _server(tmp_path)
    assert server.handle({"jsonrpc": "2.0", "id": 7, "method": "ping"})["result"] == {}
    bad = server.handle({"id": 8, "method": "ping"})  # missing jsonrpc
    assert bad["error"]["code"] == -32600


def test_serve_loop_over_fake_stdio(tmp_path: Path) -> None:
    server, db = _server(tmp_path)
    db.add_memory(kind="note", title="Focus", content="ship the gate", source="local", metadata={})
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "codex"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "memory.search", "arguments": {"query": "gate"}}},
    ]
    stdin = io.StringIO("\n".join(json.dumps(m) for m in lines) + "\n")
    stdout = io.StringIO()
    server.serve(stdin, stdout)
    responses = [json.loads(l) for l in stdout.getvalue().splitlines() if l.strip()]
    # 3 responses (the notification produced none)
    assert [r.get("id") for r in responses] == [1, 2, 3]
    assert responses[2]["result"]["isError"] is False
    assert responses[2]["result"]["structuredContent"]["matches"]
