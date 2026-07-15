"""Delegated specialist work — the swarm's frontier half.

Where roles.py runs cheap work LOCALLY, this hands heavy/frontier work OUT to the
agent runtimes the founder already uses (Claude Code / Codex). Zade stays the
orchestrator: it decides *what* specialist work a gap or task needs, packages a
scoped brief (goal + ledger context + acceptance criteria), and either runs it or
files it for approval — then captures the returned artifact back into the ledger.

Founder decisions baked in (2026-07-15):
  * Auto-invoke is ON: a delegation may automatically invoke the external agent as
    an approval-gated L3 action, up to a **larger daily budget**; past the budget
    it falls back to the normal typed-phrase approval. The budget is configurable.
  * The external agent is a configured argv command (no shell parsing). If none is
    configured, delegation is brief-only (prepare-not-send) — it can never invoke.

The invoker is injectable (module-level ``run_agent`` resolved by name at call
time) so the whole path is testable without spawning a real agent.
"""
from __future__ import annotations

import subprocess
from typing import Any, Callable

from .config import KernelConfig
from .db import KernelDatabase, WorkItem, utc_now
from .founder import FounderService
from .autonomy import WorkQueueService

DELEGATION_RUN_ACTION = "external.delegation.run"

AgentRunner = Callable[..., str]


class DelegationService:
    def __init__(
        self,
        *,
        config: KernelConfig,
        db: KernelDatabase,
        founder: FounderService,
        work_queue: WorkQueueService,
        runner: AgentRunner | None = None,
    ):
        self.config = config
        self.db = db
        self.founder = founder
        self.work_queue = work_queue
        self._runner = runner

    # ---- registration ----
    def register_into(self, registry: Any) -> list[str]:
        if not self.config.delegation.enabled:
            return []
        registry.register(
            DELEGATION_RUN_ACTION,
            "Invoke a configured external agent (Claude Code/Codex) on a scoped brief and file its artifact (approved external action).",
            self.run_from_work_item,
        )
        return [DELEGATION_RUN_ACTION]

    def status(self) -> dict[str, Any]:
        used = self._invocations_today()
        budget = self.config.delegation.daily_budget
        return {
            "enabled": self.config.delegation.enabled,
            "agent_configured": bool(self.config.delegation.agent_command),
            "agent_command": list(self.config.delegation.agent_command),
            "daily_budget": budget,
            "invocations_today": used,
            "budget_remaining": max(0, budget - used),
            "auto_invoke": self.config.delegation.auto_invoke,
            "operating_rules": [
                "Zade orchestrates: it packages a scoped brief and captures the artifact — it does not try to BE the agent.",
                "Invoking an external agent is an L3 external action. Auto-invoke runs it without asking up to the daily budget; past that it requires the typed confirmation phrase.",
                "With no agent command configured, delegation is brief-only (prepare-not-send) and can never invoke.",
                "Every invocation is audited and its artifact filed as delegated-work evidence — an external claim, never native certainty.",
            ],
        }

    # ---- local brief building (no approval, no network) ----
    def build_brief(self, *, task: str, context: str = "", acceptance: str = "") -> str:
        """Assemble a scoped agent brief from the task + the founder's own context.

        Fully LOCAL and deterministic (template, not a model call): the value is the
        structured packaging — goal, grounding, and a done-definition the external
        agent can execute against.
        """
        task = (task or "").strip()
        if not task:
            raise ValueError("A delegation needs a task.")
        one_thing = ""
        try:
            one_thing = str(self.founder.dashboard().get("one_thing_that_matters_most_today", "")).strip()
        except Exception:  # noqa: BLE001 - the brief is still useful without the dashboard line
            one_thing = ""
        lines = [
            f"# Delegated task for an external agent (briefed by {self.config.identity.name})",
            "",
            "## Goal",
            task,
            "",
            "## Context",
            context.strip() or "(none supplied)",
        ]
        if one_thing:
            lines += ["", f"Founder's current priority: {one_thing}"]
        lines += [
            "",
            "## Acceptance criteria",
            acceptance.strip() or "Produce a concrete, reviewable artifact that directly addresses the goal.",
            "",
            "## Constraints",
            "- Return the artifact only; no preamble.",
            "- If you cannot complete it, state precisely what is blocking and what you'd need.",
        ]
        return "\n".join(lines)

    # ---- queue / auto-invoke ----
    def queue_delegation(
        self,
        *,
        task: str,
        brief: str = "",
        context: str = "",
        acceptance: str = "",
        auto_invoke: bool | None = None,
    ) -> dict[str, Any]:
        self._require_enabled()
        brief = (brief or "").strip() or self.build_brief(task=task, context=context, acceptance=acceptance)
        want_auto = self.config.delegation.auto_invoke if auto_invoke is None else bool(auto_invoke)

        result = self.work_queue.enqueue(
            kind="delegation_run",
            title=f"Delegate: {task.strip()[:80]}",
            detail="Invoke an external agent on a scoped brief.\n\n" + brief,
            action=DELEGATION_RUN_ACTION,
            target="external-agent",
            permission_tier="L3_EXTERNAL_ACTION",
            priority=60,
            source="delegation",
            metadata={"task": task.strip(), "brief": brief},
            unique_key=f"{DELEGATION_RUN_ACTION}:{utc_now()}",
        )
        payload = result.as_dict()

        # Budgeted auto-invoke: the authorized bypass. Only when auto is on, an agent
        # command is configured, and we're under today's cap. Otherwise it stays a
        # gated work item waiting on the typed phrase.
        can_auto = (
            want_auto
            and bool(self.config.delegation.agent_command)
            and self._invocations_today() < self.config.delegation.daily_budget
        )
        if can_auto:
            item = self.db.get_work_item(result.item_id)
            if item is not None:
                dispatch = self.run_from_work_item(item)
                return payload | {"auto_invoked": True, "dispatch": dispatch}
        reason = (
            "auto-invoke disabled" if not want_auto
            else "no agent command configured" if not self.config.delegation.agent_command
            else "daily budget reached — requires typed-phrase approval"
        )
        return payload | {"auto_invoked": False, "reason": reason}

    # ---- dispatch handler ----
    def run_from_work_item(self, item: WorkItem) -> dict[str, Any]:
        metadata = item.metadata or {}
        task = str(metadata.get("task", "")).strip()
        brief = str(metadata.get("brief", "")).strip()
        if not brief:
            raise ValueError("Delegation work item is missing its brief.")
        command = self.config.delegation.agent_command
        if not command:
            # Brief-only: nothing to invoke. Return the prepared brief so the founder
            # can run it themselves — the prepare-not-send fallback.
            return {
                "handler": DELEGATION_RUN_ACTION,
                "status": "prepared",
                "ok": False,
                "task": task,
                "brief": brief,
                "note": "No external agent configured; brief prepared for you to run manually.",
            }
        runner = self._runner or run_agent
        status = "ok"
        error = ""
        artifact = ""
        try:
            artifact = runner(
                list(command),
                brief=brief,
                timeout=self.config.delegation.timeout_seconds,
                max_output_chars=self.config.delegation.max_output_chars,
            )
        except Exception as exc:  # noqa: BLE001 - a failed invocation is a flow error, not a 500
            status = "flow_error"
            error = str(exc)[:400]

        evidence_id = None
        if status == "ok" and artifact.strip():
            try:
                evidence = self.founder.create_evidence(
                    {
                        "evidence_type": "delegated_work",
                        "source": "delegation:external-agent",
                        "reliability": self.config.delegation.default_reliability,
                        "claim_supported": f"External agent artifact for '{task}': {artifact[:400]}",
                        "strength": 55,
                        "notes": "Produced by a delegated external agent. Treat as a sourced external claim, not native certainty.",
                        "metadata": {"task": task, "entity_boundary": "External agent produced; Zade records as delegated evidence."},
                    }
                )
                evidence_id = evidence.id
            except Exception as exc:  # noqa: BLE001 - filing must not discard a returned artifact
                error = (error + f" | filing error: {exc}")[:400]

        self.db.audit(
            actor="approved-handler",
            action=DELEGATION_RUN_ACTION,
            target="external-agent",
            permission_tier=item.permission_tier,
            status=status,
            details={"work_item_id": item.id, "task": task, "artifact_chars": len(artifact), "evidence_id": evidence_id},
        )
        return {
            "handler": DELEGATION_RUN_ACTION,
            "status": status,
            "ok": status == "ok",
            "task": task,
            "artifact": artifact,
            "evidence_id": evidence_id,
            "error": error,
        }

    # ---- internals ----
    def _require_enabled(self) -> None:
        if not self.config.delegation.enabled:
            raise ValueError("Delegation is disabled (delegation.enabled = false).")

    def _invocations_today(self) -> int:
        today = utc_now()[:10]
        count = 0
        for event in self.db.recent_audit_events(limit=300):
            if (
                event.get("action") == DELEGATION_RUN_ACTION
                and str(event.get("created_at", "")).startswith(today)
                and event.get("status") in {"ok", "flow_error"}
            ):
                count += 1
        return count


# ---- module-level invoker (the actual external egress) ----
def run_agent(command: list[str], *, brief: str, timeout: float = 600.0, max_output_chars: int = 20000) -> str:
    """Run a configured external agent command, feeding the brief on stdin.

    argv only (no shell parsing). Captures stdout+stderr, byte-bounded via the
    char cap. A non-zero exit or a timeout raises, surfaced by the caller as a
    flow error.
    """
    try:
        completed = subprocess.run(
            command,
            input=brief,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"External agent timed out after {timeout}s.") from exc
    except FileNotFoundError as exc:
        raise ValueError(f"External agent command not found: {command[0]!r}.") from exc
    out = (completed.stdout or "")
    if completed.returncode != 0:
        detail = (completed.stderr or out or "").strip()[:400]
        raise ValueError(f"External agent exited {completed.returncode}: {detail}")
    return out[:max_output_chars]
