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

    def __init__(self, tools: ToolRegistry, *, exposed: dict[str, str] | None = None):
        self.tools = tools
        self.exposed = dict(exposed if exposed is not None else EXPOSED)

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

        # Delegate to the registry: its authority evaluation + audit still apply.
        # The registry records the call under our attributed actor.
        return self.tools.call(name, args, actor=actor)
