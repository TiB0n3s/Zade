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
        self.register(
            ToolDefinition(
                name="work.status",
                description="Read the work queue: status counts and recent items. Read-only.",
                permission_tier=PermissionTier.READ,
                handler=self._work_status,
            )
        )
        self.register(
            ToolDefinition(
                name="evidence.recent",
                description="Read recently filed founder-OS evidence records. Read-only.",
                permission_tier=PermissionTier.READ,
                handler=self._evidence_recent,
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
        # 'grounding_status' is a caller-set control (the agent surface sets it to
        # 'quarantined' for external writes; internal callers leave it 'active').
        # It is not part of the tool's public schema.
        grounding_status = str(args.get("grounding_status") or "active")
        if self.ingestion is not None:
            # Governed write: refuses obvious secrets, dedupes, embeds, mirrors.
            result = self.ingestion.save_memory(
                kind=kind, title=title, content=content, source=source, metadata=metadata,
                grounding_status=grounding_status,
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
        # 'shareable_only' is a caller-set control: the agent surface pins it True so
        # an external client only reads founder-marked-shareable memory, never the
        # private store. Absent/False -> full recall (internal callers, Zade itself).
        records = self.db.search_memories(
            str(args["query"]), int(args.get("limit", 8)), shareable_only=bool(args.get("shareable_only"))
        )
        return ToolResult(ok=True, data={"matches": [record.__dict__ for record in records]})

    def _work_status(self, args: dict[str, Any]) -> ToolResult:
        """Work-queue readout: status counts + recent items. Read-only, no
        payload bodies — titles and states only, so an external reader learns
        what work exists and where it stands, not its contents."""
        limit = max(1, min(int(args.get("limit", 10)), 50))
        status = str(args.get("status") or "").strip() or None
        items = [
            {
                "id": item.id,
                "created_at": item.created_at,
                "kind": item.kind,
                "title": item.title,
                "status": item.status,
                "priority": item.priority,
            }
            for item in self.db.list_work_items(status=status, limit=limit)
        ]
        return ToolResult(ok=True, data={"counts": self.db.work_queue_counts(), "items": items})

    def _evidence_recent(self, args: dict[str, Any]) -> ToolResult:
        """Filed-evidence readout: the most recent founder_evidence rows, newest
        first. Read-only and curated — the metadata blob stays internal; a reader
        gets the claim, grade, strength, and linkage, which is what an external
        reviewer needs to judge what evidence exists and how strong it is."""
        limit = max(1, min(int(args.get("limit", 10)), 50))
        reliability = str(args.get("reliability") or "").strip().upper() or None
        evidence_type = str(args.get("evidence_type") or "").strip() or None
        query = (
            "SELECT id, created_at, evidence_type, source, evidence_date, reliability, "
            "strength, claim_supported, claim_contradicted, linked_assumption_id, "
            "linked_decision_id, notes FROM founder_evidence"
        )
        clauses: list[str] = []
        params: list[Any] = []
        if reliability:
            clauses.append("reliability = ?")
            params.append(reliability)
        if evidence_type:
            clauses.append("evidence_type = ?")
            params.append(evidence_type)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self.db.connect() as conn:
            rows = [dict(row) for row in conn.execute(query, params).fetchall()]
        return ToolResult(ok=True, data={"evidence": rows, "count": len(rows)})

    def _audit_recent(self, args: dict[str, Any]) -> ToolResult:
        # 'audit_scope_actor' is a caller-set control: the agent surface pins it to
        # the calling agent so an external client sees ONLY its own audit rows, not
        # the whole kernel's ledger. Absent -> unscoped (internal callers).
        scope_actor = args.get("audit_scope_actor")
        events = self.db.recent_audit_events(
            int(args.get("limit", 25)), actor=str(scope_actor) if scope_actor else None
        )
        return ToolResult(ok=True, data={"events": events})
