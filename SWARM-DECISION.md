# Specialist-Agent Swarm — Decision Doc

Date: 2026-07-13
Owner: founder (decision) · drafted for the Deep Thought decommission plan (item 6 of 7)
Status: **FIRST SLICE BUILT + LIVE-VERIFIED 2026-07-15.** Founder chose **Option C hybrid with auto-invoke enabled**. Roles answered: red-team + triage + gap-finder (local) and engineering/research (delegated). Cost tolerance: **larger daily budget** before approval. Shipped: `roles.py` (RolePassService — 4 local roles on the general model, one governed pass each, recorded as telemetry; generalizes ContrarianCritic) at `GET /roles`, `GET /roles/status`, `POST /roles/run`; `delegation.py` (DelegationService — scoped brief builder + L3 `external.delegation.run` handler + **budgeted auto-invoke**: runs the configured external-agent argv command without asking up to `delegation.daily_budget` (default 25), then falls back to typed-phrase approval; brief-only when no agent command configured) at `GET /delegation/status`, `POST /delegation/brief`, `POST /delegation/run`. Config: `RolesConfig`/`DelegationConfig`; tests `test_roles.py` + `test_delegation.py` (19 green with screen); live-verified over real HTTP incl. a real local-model red-team pass (verdict "Premature deployment", 6.2s). Remaining/next increment: a UI console for roles/delegation; wire roles into the runtime as an on-demand panel; optionally let the founder pick the external agent command in Settings.

---

## The decision in one line

Does Zade **become** a multi-agent specialist swarm (build a native worker-dispatch runtime), or does it **orchestrate** specialist work out to the agent runtimes you already use (Codex / Claude Code), staying the founder-OS that decides and captures rather than the thing that executes?

This is the one remaining decommission item that is a *choice about scope*, not a missing feature. The comparison doc already flagged it: "Missing unless intentionally replaced by Codex/plugins." This doc exists to make that "intentionally" explicit.

---

## Current state (grounded)

**Deep Thought:** 236 specialist agents, 81 unique specialist tools, 238 dispatchers, async fan-out and swarms across engineering, marketing, security, trading, finance, design, testing. This is genuinely broad and is the single largest capability Zade does not reproduce.

**Zade today:** the "119 skills" are *procedural guidance*, not agents. At runtime up to 3 skill excerpts are ranked and pasted into **one** governed model call (`skills.py:294,373`; `runtime.py:508` — "Skills do not grant permission to take external action"). There is no dispatch, no fan-out, no worker runtime.

**But Zade is not empty of agentic primitives** — the hybrid option below builds on these, which already exist:
- `ContrarianCritic` (`critic.py:45`) — a *second, independent reasoning pass* on recommendation-shaped output. This is already a working "specialist role" pattern (one extra model with a distinct brief, attached to the record, non-blocking).
- `ActionPipelineService` (`actions.py:28`) — multi-step action plans with per-step authority and evidence on completion.
- Work queue + approval ladder + typed dispatch — the substrate every new capability in this decommission plan has plugged into (browser, vault, research all use it).
- Founder ledger (decisions, assumptions, bets, tasks, evidence) — the source of *what* specialist work is even needed, and the sink for results.
- Evals + model telemetry — a way to measure whether a given role/model is worth its latency.

## The constraint that dominates this decision

Zade runs on a **single local Ollama instance**, general model `qwen3:14b`, **~7.3 s average per call** (103 calls, 2026-07-13 telemetry snapshot). One GPU serves requests essentially serially.

That means a "20-agent swarm" on local hardware is **not** parallel the way a cloud swarm is — the calls queue on one GPU. A fan-out of 20 agents each making a few 7 s calls is *minutes* of wall-clock, serialized. And the quality ceiling of a 14B local model is well below the frontier models the specialist work (especially engineering, security, research) actually wants.

So the honest framing: **a native local swarm buys breadth of *roles* but not breadth of *throughput or quality*.** That reframes the whole choice.

---

## Options

### A. Build a native local swarm inside Zade
Spawn N role-prompted agents concurrently on local models, orchestrate fan-out, synthesize results. Port (a subset of) Deep Thought's 236 roles.

- **Pros:** fully local (no work leaves the machine), self-contained, matches DT's shape most literally, no external cost.
- **Cons:** single-GPU serialization makes "concurrency" mostly cosmetic; 14B quality ceiling for hard specialist work; **large, ongoing maintenance** (200+ role prompts rot); duplicates capability you already pay for in Codex/Claude Code; latency makes it painful for interactive use.
- **Effort:** Large. New orchestration runtime, role registry, result synthesis, concurrency/queueing, per-role evals. Weeks, plus perpetual prompt upkeep.

### B. Orchestrate external agents (delegation)
Zade never tries to *be* the swarm. It decides *what* specialist work a gap/decision needs, packages a scoped brief, hands it to the right external runtime (Claude Code for engineering — the thing you're literally using now — Codex, etc.), and captures the returned artifact back into the ledger as evidence/action.

- **Pros:** leverages runtimes you already have and pay for; **frontier quality**; no local-GPU bottleneck; near-zero role-maintenance (the external agents are maintained by their vendors); Zade stays lean and plays to its actual strength (founder-OS: knowing what matters and recording outcomes).
- **Cons:** specialist work *leaves the machine* (a posture shift — though you already use these tools daily); needs a handoff mechanism; if handoff is auto-invoked it's an external action (approval-gated) and has external cost.
- **Effort:** Small-to-Medium. A `DelegationService` (brief-out, capture-in) on the existing work-queue/approval substrate — the same pattern as connectors/browser/research.

### C. Hybrid — Zade orchestrates, with a small *local* role panel (recommended)
Delegation (Option B) as the default for heavy/frontier work, **plus** a small, bounded set of *local* role passes for the cheap, latency-tolerant, privacy-sensitive tasks a 14B model does fine and that shouldn't leave the machine:
- critique / red-team (generalize the existing `ContrarianCritic`),
- triage / classify / summarize,
- "does this assumption have a gap" style reviews (feeds the research daydream already built).

Zade = **orchestrator + ledger**: it routes each unit of specialist work to *local role pass* or *external delegation* based on task type, sensitivity, and cost — and files every result as evidence/action.

- **Pros:** best-fit per task; keeps sensitive/cheap work local; sends only hard/frontier work out; reuses the critic pattern that already exists; incremental (ship the local panel first, add delegation second).
- **Cons:** two paths to reason about; routing policy needs definition.
- **Effort:** Medium, but *incremental* — each slice is shippable and verifiable like the last four builds.

---

## Recommendation

**Option C (hybrid), leaning heavily on delegation.** Rationale:

1. The single-GPU + 14B constraint makes a large native local swarm (Option A) a poor return: you'd invest weeks and perpetual prompt-maintenance to get *serialized, sub-frontier* specialists — for engineering and research especially, that's strictly worse than the Claude Code/Codex you already run.
2. Zade's demonstrated strength across this whole decommission project is being the **governed kernel** — deciding, gating, and recording. Orchestration extends that strength; it doesn't fight the hardware.
3. A small local role panel captures the genuinely-local wins (privacy-sensitive triage, cheap critique) without the maintenance tail of 200+ roles.
4. It's incremental and verifiable — same build rhythm that shipped browser/vault/tray/research.

## What this means for decommissioning Deep Thought

The decommission gate for this item closes via the **"explicitly decide these capabilities are not part of Zade's required surface"** branch, *not* the "build full parity" branch:

- You are accepting that **Zade will not be a 236-agent local swarm**, and that DT's swarm is retired in favor of *external agents + Zade orchestration*.
- Concretely, "specialist work" after DT = Claude Code / Codex for execution + Zade for deciding-and-capturing (+ a small local role panel).
- This is a legitimate closure of the gate — but it should be a **conscious** acceptance, which is the whole point of this doc.

If instead you want true local-only specialist autonomy (Option A), the gate stays open and this becomes a multi-week build.

---

## If we go hybrid — concrete first slice (v1)

Two small, independently shippable pieces on the existing substrate:

1. **Local role panel** — generalize `ContrarianCritic` into a `RolePass` primitive: a named role (system prompt) + the governed runtime → one extra local model pass, attached to the subject as a review/finding. Ship 3–4 roles first (red-team, triage, summarize, gap-finder). Reuses existing runtime + telemetry + evals. Fully local, no new posture.
2. **`DelegationService`** — takes a founder task/gap → produces a *scoped agent brief* (goal, context from the ledger, acceptance criteria) → records a `delegation` work item. **Draft-brief-only by default** (Zade writes the brief for you to run in Claude Code/Codex; auto-invoking an external agent is an L3 external action, gated) → captures the returned artifact as evidence/action when you paste it back. Mirrors the `dev.draft.write` "prepare-not-send" pattern already in the codebase.

Both are verifiable end-to-end the same way the last four builds were.

---

## Open questions for you (these change the build)

1. **Auto-invoke vs brief-only.** Should Zade ever *automatically* call an external agent (L3 external action, real cost), or only ever *produce a brief* you run yourself? (Recommend brief-only for v1.)
2. **Which roles do you actually use** from DT's 236? Naming the ~5–10 you rely on prevents rebuilding dead weight — and tells us which belong local vs. delegated.
3. **Posture:** are you comfortable specialist work leaving the machine via Claude Code/Codex (you already use them), or is local-only a hard requirement for some categories (e.g. anything touching the vault / trading)?
4. **Cost tolerance** for external agent calls, if auto-invoke is ever on.

Answer 1–3 and the first slice is unblocked.
