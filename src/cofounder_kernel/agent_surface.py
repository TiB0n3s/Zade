"""The governed surface external agents reach Zade *through*.

The integration strategy's best structural idea: instead of handing a cloud agent
(Claude Agent SDK, Codex, Claude Desktop) broad filesystem/shell access to the
machine, expose a **tight, curated allowlist** of Zade capabilities. The agent
sees only these; every call still routes through the kernel's existing governance
— ``authority.AuthorityPolicy`` (allow/deny/approval), the audit ledger, the
``tool_calls`` table, and (for any egressing tool) the ``egress`` gate. The kernel
stays the governor; this is the doorway, not a bypass.

This module is the transport-agnostic core: it defines *what* is exposed and *how*
each call is attributed and policed. An MCP server (stdio, loopback-only) is the
first binding on top of it — see ZADE-MCP-SURFACE.md — but the allowlist and its
guarantees live here so they are testable without any protocol or dependency.

Design guarantees
-----------------
1. **Fail closed / allowlist, not blocklist.** An external agent can call ONLY a
   name in ``EXPOSED``. If the kernel's internal ``ToolRegistry`` grows a new
   tool, external agents still see nothing new until it is added here on purpose.
   This is deliberately *narrower* than the internal registry.
2. **No destructive reach.** Destructive capabilities are excluded by name even
   when the registry has them — e.g. ``memory.forget`` is internal-only. An
   external agent cannot delete the founder's memory.
3. **Attributed.** Every call is audited as ``actor="mcp:<client>"`` so the
   ledger shows which external agent did what, distinct from the kernel itself.
4. **Governed, not trusted.** The call still passes through the registry's
   authority evaluation and audit. A "write" tool remains L1/audited; the surface
   never elevates a tool's permission tier.
5. **Instruction-source boundary.** An external agent is untrusted input, exactly
   like a web page or a channel message. It may *call allowlisted tools*; it can
   never authorize egress, approve its own writes, or reach anything off-list.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .tools import ToolRegistry, ToolResult


# What an external agent may reach, and the strongest effect each entry may have.
# READ  -> non-mutating.
# WRITE -> mutating but non-destructive; stays L1/audited via the registry.
# Destructive/system/external tools are intentionally ABSENT (fail closed).
READ = "read"
WRITE = "write"

EXPOSED: dict[str, str] = {
    "memory.search": READ,
    "audit.recent": READ,
    "memory.write": WRITE,
    # memory.forget — deliberately NOT exposed (destructive: deletes founder memory)
}


# MCP-style tool descriptors: input schema + annotations. Kept here (not in the
# registry) because they describe the *external* contract, which is narrower and
# more conservative than the internal one.
_SCHEMAS: dict[str, dict[str, Any]] = {
    "memory.search": {
        "description": "Search Zade's local memory (SQLite FTS). Read-only.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Full-text search query."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 8},
            },
            "required": ["query"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    "audit.recent": {
        "description": "Read Zade's recent local audit events. Read-only.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
            },
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    "memory.write": {
        "description": "Write one local memory record. Mutating, audited, attributed to the calling agent.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "content": {"type": "string"},
                "kind": {"type": "string", "default": "note"},
                "source": {"type": "string", "description": "Origin label; defaults to the calling agent."},
                "metadata": {"type": "object"},
            },
            "required": ["title", "content"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    },
}


_CLIENT_RE = re.compile(r"[^a-z0-9_.-]+")


def _actor_for(client: str) -> str:
    """Namespace external agents under mcp: for the audit ledger. Sanitized so a
    hostile client name cannot forge another actor or inject into audit fields."""
    slug = _CLIENT_RE.sub("-", (client or "unknown").strip().lower()).strip("-") or "unknown"
    return f"mcp:{slug[:48]}"


@dataclass(frozen=True)
class SurfaceTool:
    name: str
    effect: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any]

    @property
    def read_only(self) -> bool:
        return self.effect == READ


class AgentSurface:
    """Transport-agnostic governed allowlist over the kernel ToolRegistry.

    Construct it with the live registry; a binding (MCP server, etc.) calls
    ``manifest()`` to advertise tools and ``call()`` to invoke one on behalf of a
    named external client."""

    def __init__(
        self,
        tools: ToolRegistry,
        *,
        exposed: dict[str, str] | None = None,
        require_write_approval: bool = True,
    ):
        self.tools = tools
        self.exposed = dict(exposed if exposed is not None else EXPOSED)
        # External-agent writes are HELD for founder approval by default. Since
        # every call through this surface is an external (mcp:) actor, this gates
        # all surface writes without gating the kernel's own internal writes.
        self.require_write_approval = require_write_approval

    def manifest(self) -> list[SurfaceTool]:
        """The tools this surface advertises — allowlist ∩ what the registry
        actually has. A name on the allowlist that the registry does not provide
        is omitted (fail closed), never fabricated."""
        registry_names = {t["name"] for t in self.tools.list_tools()}
        out: list[SurfaceTool] = []
        for name, effect in sorted(self.exposed.items()):
            if name not in registry_names or name not in _SCHEMAS:
                continue
            spec = _SCHEMAS[name]
            out.append(
                SurfaceTool(
                    name=name,
                    effect=effect,
                    description=spec["description"],
                    input_schema=spec["input_schema"],
                    annotations=spec["annotations"],
                )
            )
        return out

    def call(self, name: str, args: dict[str, Any], *, client: str) -> ToolResult:
        """Invoke an allowlisted tool for an external client.

        Refuses (fail closed) anything not on the allowlist BEFORE touching the
        registry, so an off-list or unknown name never even reaches kernel
        dispatch. Attributes the call to ``mcp:<client>`` in the audit ledger."""
        actor = _actor_for(client)
        if name not in self.exposed:
            audit_id = self.tools.db.audit(
                actor=actor,
                action="mcp.call",
                target=name,
                permission_tier="L0_READ",
                status="denied",
                details={"reason": "not_exposed", "args_keys": sorted(args.keys())},
            )
            return ToolResult(ok=False, data={"error": "not_exposed", "audit_id": audit_id})

        # Default a memory.write's source to the calling agent for provenance,
        # unless the caller set one — so founder can see external-written memory.
        if name == "memory.write" and not args.get("source"):
            args = {**args, "source": actor}

        # Approval gate: an external agent's memory write is HELD for founder
        # review rather than applied autonomously. Memory feeds recall/grounding,
        # so an unreviewed external write must not silently enter it. The write is
        # applied only when the founder approves (approve_pending_write), through
        # the same governed path as any other write.
        if name == "memory.write" and self.require_write_approval:
            return self._hold_write_for_approval(args, actor)

        # Delegate to the registry: its authority evaluation + audit still apply.
        # The registry records the call under our attributed actor.
        return self.tools.call(name, args, actor=actor)

    def _hold_write_for_approval(self, args: dict[str, Any], actor: str) -> ToolResult:
        """File a pending founder-approval request carrying the proposed write and
        return 'awaiting_approval' without touching memory."""
        payload = {
            key: args.get(key)
            for key in ("kind", "title", "content", "source", "metadata")
            if args.get(key) is not None
        }
        request, _created = self.tools.db.ensure_approval_request(
            source_type="mcp_memory_write",
            source_id=None,
            title=f"{actor} wants to write a memory",
            detail=str(args.get("title") or "")[:200],
            action="memory.write",
            target="memories",
            permission_tier="L1_MEMORY_WRITE",
            authority_decision="approval_required",
            authority={"reason": "External-agent memory write held for founder approval."},
            requested_by=actor,
            metadata={"write": payload, "actor": actor},
        )
        audit_id = self.tools.db.audit(
            actor=actor,
            action="mcp.write.gated",
            target="memories",
            permission_tier="L1_MEMORY_WRITE",
            status="pending",
            details={"approval_request_id": request.id, "title": args.get("title")},
        )
        return ToolResult(
            ok=True,
            data={
                "status": "awaiting_approval",
                "approval_request_id": request.id,
                "message": "Write held for founder approval; not stored yet.",
                "audit_id": audit_id,
            },
        )


# --- founder-side resolution of held external writes -----------------------
# These operate on the shared DB (and, for approve, the governed ingestion path).
# They live here because they are surface-domain logic; the kernel HTTP layer
# (which has the ingestion service) exposes them as endpoints. Deny can also go
# through the generic approvals console — this is the convenience pair.

_WRITE_SOURCE_TYPE = "mcp_memory_write"


def list_pending_writes(db: Any) -> list[dict[str, Any]]:
    """External-agent writes awaiting the founder's decision."""
    out: list[dict[str, Any]] = []
    for request in db.list_approval_requests(status="pending", limit=500):
        if request.source_type != _WRITE_SOURCE_TYPE:
            continue
        meta = request.metadata or {}
        write = meta.get("write", {})
        out.append(
            {
                "approval_request_id": request.id,
                "actor": meta.get("actor"),
                "title": write.get("title"),
                "requested_by": request.requested_by,
                "created_at": request.created_at,
            }
        )
    return out


def _load_pending_write(db: Any, request_id: int) -> Any:
    request = db.get_approval_request(request_id)
    if request is None or request.source_type != _WRITE_SOURCE_TYPE:
        raise ValueError(f"Not an MCP memory-write request: {request_id}")
    if request.status not in {"pending", "deferred"}:
        raise ValueError(f"Request already {request.status}.")
    return request


def approve_pending_write(db: Any, ingestion: Any, request_id: int, *, resolved_by: str = "founder") -> dict[str, Any]:
    """Founder approves a held write: apply it through the GOVERNED path (secret
    filter + dedupe + embedding + mirror), keeping the agent's provenance and
    attribution, then resolve the request. A secret still gets blocked here even
    though the founder approved — defense in depth."""
    request = _load_pending_write(db, request_id)
    meta = request.metadata or {}
    write = meta.get("write", {})
    actor = str(meta.get("actor") or "mcp:unknown")
    result = ingestion.save_memory(
        kind=str(write.get("kind") or "note"),
        title=str(write.get("title") or ""),
        content=str(write.get("content") or ""),
        source=str(write.get("source") or actor),
        metadata=dict(write.get("metadata") or {}),
    )
    write_status = result.get("status")
    db.resolve_approval_request(
        request_id, status="approved", resolved_by=resolved_by, resolution_note=f"write:{write_status}"
    )
    db.audit(
        actor=actor,
        action="mcp.write.approved",
        target="memories",
        permission_tier="L1_MEMORY_WRITE",
        status="ok" if write_status == "written" else str(write_status),
        details={
            "approval_request_id": request_id,
            "write_status": write_status,
            "memory_id": result.get("memory_id"),
            "approved_by": resolved_by,
        },
    )
    out = {"approval_request_id": request_id, "write_status": write_status, "approved_by": resolved_by}
    for key in ("memory_id", "duplicate_of", "reason", "degraded"):
        if key in result:
            out[key] = result[key]
    return out


def deny_pending_write(db: Any, request_id: int, *, resolved_by: str = "founder") -> dict[str, Any]:
    """Founder denies a held write: nothing is written, the request is closed."""
    request = _load_pending_write(db, request_id)
    actor = str((request.metadata or {}).get("actor") or "mcp:unknown")
    db.resolve_approval_request(request_id, status="denied", resolved_by=resolved_by, resolution_note="denied")
    db.audit(
        actor=actor,
        action="mcp.write.denied",
        target="memories",
        permission_tier="L1_MEMORY_WRITE",
        status="denied",
        details={"approval_request_id": request_id, "denied_by": resolved_by},
    )
    return {"approval_request_id": request_id, "status": "denied"}
