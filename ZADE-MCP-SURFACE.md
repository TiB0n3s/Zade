# Zade MCP Surface — the governed doorway for external agents

**Status:** governance core + stdio MCP server landed (`agent_surface.py`, `mcp_server.py`, `python -m cofounder_kernel mcp` — off by default, zero new dependency, read-only surface). End-to-end stdio smoke passed.
**Date:** 2026-07-17
**From:** the integration assessment — Anthropic recommendation #4 ("expose a tight Zade MCP/HTTP surface instead of giving Claude broad local access"), which the pressure-test flagged as the best structural item on the list.

---

## 1. The principle

A cloud/agentic coding tool (Claude Agent SDK, Codex, Claude Desktop) is powerful precisely because it can read files, run shell, and act. Pointed at *this* machine, that is exactly the wrong amount of access — it would sit inside Zade's trust boundary with none of Zade's governance.

The fix is to invert it: the external agent gets **no machine access at all**. It gets a **narrow, curated set of Zade capabilities** over MCP, and every one of them still passes through the kernel's existing governance:

```
external agent ──MCP──▶ AgentSurface (allowlist) ──▶ ToolRegistry ──▶ AuthorityPolicy
                         (this build)                                  + audit ledger
                                                                       + egress gate (if egressing)
```

The kernel stays the governor. The MCP surface is the **doorway**, deliberately narrower than what Zade can do internally — not a bypass around the guards.

---

## 2. What's exposed (the allowlist)

Landed in [`agent_surface.py`](src/cofounder_kernel/agent_surface.py) as `EXPOSED` — an **allowlist, not a blocklist**, so the internal registry can grow without ever widening the external surface by accident:

| Tool | Effect | Notes |
|---|---|---|
| `memory.search` | read | FTS over Zade's memory |
| `audit.recent` | read | recent audit events |
| `memory.write` | write | **live (promoted 2026-07-17)**; non-destructive/append-only, L1/audited, `source` auto-stamped to the calling agent |
| ~~`memory.forget`~~ | — | **deliberately excluded** — destructive; an external agent must not delete founder memory |

> **Governance note — external writes are currently autonomous.** `memory.write` routes through the authority policy as a known-local L1 action, which is auto-allowed (no founder approval), attributed, and audited. It is append-only (an agent cannot edit or delete), but because memory feeds Zade's recall/grounding, an agent could append unreviewed records. If that's too permissive, the follow-up is to force approval for `mcp:`-actor writes, or quarantine externally-written memory out of grounding until reviewed. Not done — flagged for your call.

Everything else the kernel can do (shell, vault, trading, browser, delegation, founder-OS mutations) is **absent**. Anything off-list is refused *before* it reaches kernel dispatch (`not_exposed`, fail closed).

Growing the surface is an explicit edit here + a schema in `_SCHEMAS` — never automatic.

---

## 3. Guarantees (each test-pinned in [`test_agent_surface.py`](tests/test_agent_surface.py))

1. **Allowlist / fail closed** — an agent can call only an `EXPOSED` name; unknown or off-list → `not_exposed` before dispatch.
2. **No destructive reach** — `memory.forget` is refused even though the registry (and the authority policy) would permit it.
3. **Attributed** — every call is audited as `actor="mcp:<client>"` (sanitized: no slashes, whitespace, or newlines can forge another actor or inject into audit fields). The founder can see exactly which external agent did what.
4. **Never elevates governance** — the surface delegates to `ToolRegistry.call`, so the authority evaluation, audit, and `tool_calls` ledger all still apply; a write stays a write.
5. **Instruction-source boundary** — an external agent is untrusted input, like a web page or channel message. It may *call allowlisted tools*; it can never authorize egress, approve its own writes, or reach off-list. (Ties directly to the egress gate's founder-only-grants rule.)

---

## 4. Transport + the dependency decision — YOUR call

The core above is transport-agnostic. The first binding should be an **MCP server, stdio, loopback-only** (a local agent spawns it; it talks to the running kernel). Two ways to build the protocol layer:

| Option | Dependency | Trade |
|---|---|---|
| **A — hand-rolled stdio** | **none** | MCP stdio is newline-delimited JSON-RPC 2.0; `initialize` + `tools/list` + `tools/call` is ~150 lines. Keeps Zade's zero-heavy-dep, local-first posture. Risk: manual protocol conformance as the spec evolves. |
| **B — official `mcp` SDK (FastMCP)** | adds `mcp` (Python SDK) | Conformant, future-proof, less code. Cost: a real new dependency + its transitive tree, against the local-first ethos (you removed cloud coding deps for exactly this reason — see f6799ff). |

**DECIDED: A (hand-rolled).** Built in [`mcp_server.py`](src/cofounder_kernel/mcp_server.py) — newline-delimited JSON-RPC 2.0, zero new dependency. Implements `initialize` / `notifications/initialized` / `tools/list` / `tools/call` / `ping`; `handle()` is a pure function so the protocol is unit-tested without stdio ([`test_mcp_server.py`](tests/test_mcp_server.py)). Revisit B (official `mcp` SDK) only if you later need resources/prompts/streaming, not just tools.

---

## 5. When the doorway opens — YOUR call

Built to these constraints:

- **Off by default** — runs only via `python -m cofounder_kernel mcp` ([`__main__.py`](src/cofounder_kernel/__main__.py)), never auto-started, never in the tray autostart. The kernel's HTTP server is untouched.
- **No network boundary** — stdio only: the agent spawns it as a subprocess, so it listens on no port and is reachable only by whoever can spawn the process (the local user). It reads the same SQLite DB the kernel owns; read tools need no coordination and their audit rows land in the founder-visible ledger.
- **Read-first, then promoted** — shipped `LIVE_EXPOSED` = `{memory.search, audit.recent}`; after the read doorway was verified with a real agent, `memory.write` was promoted to the wire (2026-07-17). `memory.forget` stays off.

### Connecting a client
Point a local MCP client (Claude Desktop, Codex) at the command:
```json
{ "command": "C:\\LocalAICofounder\\.venv\\Scripts\\python.exe", "args": ["-m", "cofounder_kernel", "mcp"] }
```
The client sees two read tools, attributed in Zade's audit as `mcp:<clientName>`.

---

## 6. Rollout

| Phase | Scope | Status |
|---|---|---|
| **1** | Governance core (`AgentSurface`) + tests, decoupled, no dependency. | ✅ landed |
| **2** | MCP stdio binding (Option A or B), `python -m cofounder_kernel mcp`, off by default, loopback + token. Read tools first. | pending your §4/§5 calls |
| **3** | Point a real agent (Claude Desktop / Codex) at it; watch the `mcp:<client>` audit trail; then enable `memory.write`. | after Phase 2 |
| **4** | Consider richer exposed tools (work status, evidence filing) — each an explicit allowlist + schema addition, read-biased. | later |

---

## 7. What this deliberately does **not** do

- No new dependency, no running server, no open port — this slice is pure governance logic.
- It does not give external agents shell/file/system access — that's the entire point.
- It does not widen the surface automatically when the internal registry grows.
- It is not a second authority engine — it's a *narrower gate in front of* the existing one.
