from pathlib import Path

from cofounder_kernel.db import KernelDatabase
from cofounder_kernel.tools import PermissionTier, ToolDefinition, ToolRegistry, ToolResult


def test_tool_registry_memory_roundtrip(tmp_path: Path) -> None:
    db = KernelDatabase(tmp_path / "kernel.sqlite")
    db.migrate()
    tools = ToolRegistry(db)

    write = tools.call(
        "memory.write",
        {
            "kind": "note",
            "title": "Local only",
            "content": "The first build should not rely on external APIs.",
        },
    )
    search = tools.call("memory.search", {"query": "external APIs", "limit": 3})

    assert write.ok is True
    assert search.ok is True
    assert search.data["matches"][0]["title"] == "Local only"
    assert write.data["audit_id"] > 0


def test_unknown_tool_is_denied(tmp_path: Path) -> None:
    db = KernelDatabase(tmp_path / "kernel.sqlite")
    db.migrate()
    tools = ToolRegistry(db)

    result = tools.call("shell.delete_everything", {})

    assert result.ok is False
    assert result.data["error"] == "unknown_tool"


def test_tool_registry_does_not_run_approval_required_tool(tmp_path: Path) -> None:
    db = KernelDatabase(tmp_path / "kernel.sqlite")
    db.migrate()
    tools = ToolRegistry(db)
    called = False

    def handler(args: dict) -> ToolResult:
        nonlocal called
        called = True
        return ToolResult(ok=True, data={"sent": True})

    tools.register(
        ToolDefinition(
            name="email.send",
            description="Send an outbound email.",
            permission_tier=PermissionTier.EXTERNAL_ACTION,
            handler=handler,
        )
    )

    result = tools.call("email.send", {"to": "founder@example.com"})

    assert result.ok is False
    assert result.data["error"] == "approval_required"
    assert called is False


def test_tool_registry_denies_hard_boundary_tool(tmp_path: Path) -> None:
    db = KernelDatabase(tmp_path / "kernel.sqlite")
    db.migrate()
    tools = ToolRegistry(db)
    called = False

    def handler(args: dict) -> ToolResult:
        nonlocal called
        called = True
        return ToolResult(ok=True, data={"order_id": "unsafe"})

    tools.register(
        ToolDefinition(
            name="broker.place_order",
            description="Place a live broker order.",
            permission_tier=PermissionTier.EXTERNAL_ACTION,
            handler=handler,
        )
    )

    result = tools.call("broker.place_order", {"symbol": "SPY"})

    assert result.ok is False
    assert result.data["error"] == "denied"
    assert called is False
