"""Tests for the governed external-agent surface.

Pins the guarantees: allowlist (fail closed), no destructive reach, per-agent
audit attribution, and that the surface never elevates a tool's governance.
"""
from __future__ import annotations

from pathlib import Path

from cofounder_kernel.agent_surface import AgentSurface, _actor_for
from cofounder_kernel.db import KernelDatabase
from cofounder_kernel.tools import ToolRegistry


def _surface(tmp_path: Path) -> tuple[AgentSurface, KernelDatabase]:
    db = KernelDatabase(tmp_path / "kernel.sqlite")
    db.migrate()
    return AgentSurface(ToolRegistry(db)), db


def test_manifest_exposes_curated_allowlist_only(tmp_path: Path) -> None:
    surface, _ = _surface(tmp_path)
    names = {t.name for t in surface.manifest()}
    assert names == {"memory.search", "audit.recent", "memory.write"}
    # memory.forget exists in the registry but is NOT exposed (destructive).
    assert "memory.forget" not in names


def test_manifest_carries_mcp_annotations(tmp_path: Path) -> None:
    surface, _ = _surface(tmp_path)
    by_name = {t.name: t for t in surface.manifest()}
    assert by_name["memory.search"].annotations["readOnlyHint"] is True
    assert by_name["memory.search"].read_only is True
    assert by_name["memory.write"].annotations["readOnlyHint"] is False
    assert by_name["memory.write"].annotations["destructiveHint"] is False
    # every exposed tool advertises an input schema
    assert all(t.input_schema.get("type") == "object" for t in surface.manifest())


def test_forget_is_refused_fail_closed(tmp_path: Path) -> None:
    """memory.forget is a real registry tool the authority policy would allow —
    the surface refuses it anyway, before dispatch, because it is off-list."""
    surface, db = _surface(tmp_path)
    # seed a memory so forget WOULD succeed if it reached the registry
    memory_id = db.add_memory(kind="note", title="t", content="c", source="local", metadata={})
    result = surface.call("memory.forget", {"memory_id": memory_id}, client="codex")
    assert result.ok is False
    assert result.data["error"] == "not_exposed"
    # and the memory is still there — forget never executed
    assert db.search_memories("t", 5)


def test_unknown_tool_refused(tmp_path: Path) -> None:
    surface, _ = _surface(tmp_path)
    result = surface.call("shell.rm_rf", {}, client="codex")
    assert result.ok is False
    assert result.data["error"] == "not_exposed"


def test_read_call_is_attributed_to_the_client(tmp_path: Path) -> None:
    surface, db = _surface(tmp_path)
    db.add_memory(kind="note", title="Runway", content="18 months of runway", source="local", metadata={})
    result = surface.call("memory.search", {"query": "runway"}, client="Claude Desktop")
    assert result.ok is True
    assert result.data["matches"]
    events = db.recent_audit_events(10)
    actors = {e["actor"] for e in events}
    assert "mcp:claude-desktop" in actors  # sanitized + namespaced


def test_write_defaults_source_to_agent_and_is_attributed(tmp_path: Path) -> None:
    surface, db = _surface(tmp_path)
    result = surface.call(
        "memory.write", {"title": "External note", "content": "from an agent"}, client="codex"
    )
    assert result.ok is True
    # the written record's source is the attributed agent (provenance)
    matches = db.search_memories("External note", 5)
    assert matches and matches[0].source == "mcp:codex"
    # audited under the agent actor
    assert any(e["actor"] == "mcp:codex" and e["action"] == "tool.call" for e in db.recent_audit_events(10))


def test_actor_sanitization_cannot_forge_or_inject() -> None:
    assert _actor_for("codex") == "mcp:codex"
    assert _actor_for("Claude Desktop") == "mcp:claude-desktop"
    assert _actor_for("") == "mcp:unknown"
    # path/slash characters are neutralized, never passed through
    assert "/" not in _actor_for("../../kernel")
    assert _actor_for("../../kernel").startswith("mcp:")
    # no whitespace or newlines can leak into the audit actor field
    forged = _actor_for("kernel\n actor=root")
    assert forged.startswith("mcp:") and "\n" not in forged and " " not in forged
