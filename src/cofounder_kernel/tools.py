from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable

from .authority import AuthorityDecision, AuthorityPolicy, AuthorityRequest
from .db import KernelDatabase


class PermissionTier(StrEnum):
    READ = "L0_READ"
    MEMORY_WRITE = "L1_MEMORY_WRITE"
    FILE_WRITE = "L2_FILE_WRITE"
    EXTERNAL_ACTION = "L3_EXTERNAL_ACTION"


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    data: dict[str, Any]


ToolHandler = Callable[[dict[str, Any]], ToolResult]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    permission_tier: PermissionTier
    handler: ToolHandler


class ToolRegistry:
    def __init__(self, db: KernelDatabase, authority: AuthorityPolicy | None = None, ingestion: Any | None = None):
        self.db = db
        # The governed write path (secret filter + semantic dedupe + embedding +
        # file mirror). When present, memory.write routes through it instead of a
        # raw db.add_memory, so every write — internal or via the agent surface —
        # is secret-blocked and made recallable.
        self.ingestion = ingestion
        default_root = db.path.parent
        self.authority = authority or AuthorityPolicy(hot_root=default_root, cold_root=default_root, data_dir=default_root)
        self._tools: dict[str, ToolDefinition] = {}
        self.register(
            ToolDefinition(
                name="memory.write",
                description="Write a local memory record to SQLite.",
                permission_tier=PermissionTier.MEMORY_WRITE,
                handler=self._memory_write,
            )
        )
        self.register(
            ToolDefinition(
                name="memory.forget",
                description="Delete a local memory record and its search-index entry at the founder's request.",
                permission_tier=PermissionTier.MEMORY_WRITE,
                handler=self._memory_forget,
            )
        )
        self.register(
            ToolDefinition(
                name="memory.search",
                description="Search local memory using SQLite FTS.",
                permission_tier=PermissionTier.READ,
                handler=self._memory_search,
            )
        )
        self.register(
            ToolDefinition(
                name="audit.recent",
                description="Read recent local audit events.",
                permission_tier=PermissionTier.READ,
                handler=self._audit_recent,
            )
        )

    def register(self, tool: ToolDefinition) -> None:
        self._tools[tool.name] = tool

    def list_tools(self) -> list[dict[str, Any]]:
        tools = []
        for tool in sorted(self._tools.values(), key=lambda item: item.name):
            authority = self.authority.evaluate(
                AuthorityRequest(
                    action=f"tool.{tool.name}",
                    permission_tier=tool.permission_tier.value,
                    target=tool.name,
                )
            )
            tools.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "permission_tier": tool.permission_tier.value,
                    "authority": authority.as_dict(),
                }
            )
        return tools

    def call(self, name: str, args: dict[str, Any], actor: str = "kernel") -> ToolResult:
        if name not in self._tools:
            audit_id = self.db.audit(
                actor=actor,
                action="tool.call",
                target=name,
                permission_tier=PermissionTier.READ.value,
                status="denied",
                details={"reason": "unknown_tool", "args": args},
            )
            return ToolResult(ok=False, data={"error": "unknown_tool", "audit_id": audit_id})

        tool = self._tools[name]
        authority = self.authority.evaluate(
            AuthorityRequest(
                action=f"tool.{name}",
                permission_tier=tool.permission_tier.value,
                target=name,
                metadata={"args_keys": sorted(args.keys())},
            )
        )
        if authority.decision != AuthorityDecision.ALLOW:
            status = "denied" if authority.decision == AuthorityDecision.DENY else "approval_required"
            result = ToolResult(ok=False, data={"error": status, "authority": authority.as_dict()})
            audit_id = self.db.audit(
                actor=actor,
                action="tool.call",
                target=name,
                permission_tier=tool.permission_tier.value,
                status=status,
                details={"args": args, "authority": authority.as_dict()},
            )
            self._record_tool_call(name, tool.permission_tier, args, result, status)
            result.data["audit_id"] = audit_id
            return result

        try:
            result = tool.handler(args)
            status = "ok" if result.ok else "error"
            audit_id = self.db.audit(
                actor=actor,
                action="tool.call",
                target=name,
                permission_tier=tool.permission_tier.value,
                status=status,
                details={"args": args, "result": result.data},
            )
            self._record_tool_call(name, tool.permission_tier, args, result, status)
            result.data["audit_id"] = audit_id
            return result
        except Exception as exc:
            audit_id = self.db.audit(
                actor=actor,
                action="tool.call",
                target=name,
                permission_tier=tool.permission_tier.value,
                status="error",
                details={"args": args, "error": str(exc)},
            )
            self._record_tool_call(
                name,
                tool.permission_tier,
                args,
                ToolResult(ok=False, data={"error": str(exc)}),
                "error",
            )
            return ToolResult(ok=False, data={"error": str(exc), "audit_id": audit_id})

    def _record_tool_call(
        self,
        name: str,
        tier: PermissionTier,
        args: dict[str, Any],
        result: ToolResult,
        status: str,
    ) -> None:
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO tool_calls (created_at, tool_name, permission_tier, args_json, result_json, status)
                VALUES (datetime('now'), ?, ?, ?, ?, ?)
                """,
                (name, tier.value, json.dumps(args, sort_keys=True), json.dumps(result.data, sort_keys=True), status),
            )

    def _memory_write(self, args: dict[str, Any]) -> ToolResult:
        kind = str(args.get("kind") or "note")
        title = str(args.get("title") or "")
        content = str(args.get("content") or "")
        source = str(args.get("source") or "local")
        metadata = dict(args.get("metadata") or {})
        if self.ingestion is not None:
            # Governed write: refuses obvious secrets, dedupes, embeds, mirrors.
            result = self.ingestion.save_memory(
                kind=kind, title=title, content=content, source=source, metadata=metadata
            )
            status = result.get("status")
            if status == "written":
                return ToolResult(ok=True, data={"memory_id": result["memory_id"], "degraded": result.get("degraded", False)})
            if status == "duplicate":
                return ToolResult(ok=True, data={"status": "duplicate", "duplicate_of": result.get("duplicate_of")})
            if status == "blocked_secret":
                # A credential/token was refused before it could land in memory.
                return ToolResult(ok=False, data={"error": "blocked_secret", "reason": result.get("reason")})
            return ToolResult(ok=False, data={"error": status or "write_failed"})
        # No ingestion wired (should not happen in the kernel/surface): raw insert.
        memory_id = self.db.add_memory(kind=kind, title=title, content=content, source=source, metadata=metadata)
        return ToolResult(ok=True, data={"memory_id": memory_id})

    def _memory_forget(self, args: dict[str, Any]) -> ToolResult:
        memory_id = int(args["memory_id"])
        deleted = self.db.delete_memory(memory_id)
        if deleted is None:
            return ToolResult(ok=False, data={"error": "memory_not_found", "memory_id": memory_id})
        return ToolResult(ok=True, data={"forgotten": deleted})

    def _memory_search(self, args: dict[str, Any]) -> ToolResult:
        records = self.db.search_memories(str(args["query"]), int(args.get("limit", 8)))
        return ToolResult(ok=True, data={"matches": [record.__dict__ for record in records]})

    def _audit_recent(self, args: dict[str, Any]) -> ToolResult:
        return ToolResult(ok=True, data={"events": self.db.recent_audit_events(int(args.get("limit", 25)))})
