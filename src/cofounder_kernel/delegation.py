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

import os
import re
import subprocess
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from .config import KernelConfig
from .db import KernelDatabase, WorkItem, utc_now
from .founder import FounderService
from .autonomy import WorkQueueService

DELEGATION_RUN_ACTION = "external.delegation.run"

AgentRunner = Callable[..., str]

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


# ---- verification-claim cross-check ------------------------------------------
# The chat layer already refuses to narrate work it did not do; this pushes the
# same guarantee into delegated artifacts. An artifact asserting a verification
# (type check, tests, audit, build) is cross-checked against the run's AUDITED
# step list; a claim with no matching successfully executed run_command step is
# marked UNVERIFIED before the evidence is filed.

def _claim(label: str, claim: str, command: str) -> tuple[str, re.Pattern[str], re.Pattern[str]]:
    return (label, re.compile(claim, re.IGNORECASE), re.compile(command, re.IGNORECASE))


_VERIFICATION_CLAIMS: tuple[tuple[str, re.Pattern[str], re.Pattern[str]], ...] = (
    _claim("TypeScript type check (tsc)", r"\btsc\b|\btype[\s-]?check(?:s|ed|ing)?\b", r"\btsc\b"),
    _claim("npm test", r"\bnpm\s+(?:run\s+)?test\b", r"\bnpm(?:\.cmd)?\b.*\btest\b"),
    _claim("npm audit", r"\bnpm\s+audit\b", r"\bnpm(?:\.cmd)?\b.*\baudit\b"),
    _claim("build", r"\bbuild\s+(?:succeed(?:ed|s)?|pass(?:ed|es)?|completed)\b", r"\bbuild\b"),
    _claim(
        "tests pass",
        r"\btests?\b[^.\n]{0,80}\b(?:pass(?:ed|es|ing)?|green)\b"
        r"|\bpass(?:ed|es)?\b[^.\n]{0,80}\btests?\b"
        r"|\bpytest\b[^.\n]{0,80}\bpass(?:ed|es)?\b",
        r"\bpytest\b|\bnpm(?:\.cmd)?\b.*\btest\b|\bnode\b.*\btest\b",
    ),
)


def find_unverified_claims(artifact: str, steps: Any) -> list[str]:
    """Return the verification claims asserted in ``artifact`` that no audited
    step actually executed. ``steps`` is the native run's audited step list;
    an external (bridge) artifact carries none, so every verification claim it
    makes comes back unverified — which is the honest posture."""
    text = str(artifact or "")
    if not text.strip():
        return []
    commands: list[str] = []
    for step in steps or []:
        if not isinstance(step, dict) or step.get("tool") != "run_command":
            continue
        if not step.get("ok", False):
            continue
        argv = (step.get("arguments") or {}).get("argv")
        if isinstance(argv, (list, tuple)):
            commands.append(" ".join(str(part) for part in argv))
    unverified: list[str] = []
    for label, claim_re, command_re in _VERIFICATION_CLAIMS:
        match = claim_re.search(text)
        if match is None:
            continue
        if any(command_re.search(command) for command in commands):
            continue
        snippet = " ".join(match.group(0).split())[:120]
        unverified.append(f"{label} (artifact says: {snippet!r})")
    return unverified


def _unverified_notes_suffix(unverified: list[str], *, engine: str) -> str:
    if not unverified:
        return ""
    header = (
        "UNVERIFIED CLAIM(S) — asserted in the artifact but not backed by any "
        "audited executed command"
    )
    if engine == "bridge":
        header += " (external agents return no audited step list)"
    bullets = "".join(f"\n- {claim}" for claim in unverified)
    return f"\n{header}:{bullets}\nTreat these claims as unconfirmed until re-verified."


class DelegationService:
    def __init__(
        self,
        *,
        config: KernelConfig,
        db: KernelDatabase,
        founder: FounderService,
        work_queue: WorkQueueService,
        runner: AgentRunner | None = None,
        coding_agent: Any | None = None,
    ):
        self.config = config
        self.db = db
        self.founder = founder
        self.work_queue = work_queue
        self._runner = runner
        # Native local coding agent (Ollama tool loop). Injected from api.py;
        # when the engine is "native" this is the delegated-build implementation
        # and no external agent process is launched at all.
        self.coding_agent = coding_agent

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
        engine = getattr(self.config.delegation, "engine", "native")
        return {
            "enabled": self.config.delegation.enabled,
            "engine": engine,
            "native_agent_available": self.coding_agent is not None,
            "agent_configured": bool(self.config.delegation.agent_command),
            "agent_command": list(self.config.delegation.agent_command),
            "workspace_root": getattr(self.config.delegation, "workspace_root", ""),
            "daily_budget": budget,
            "invocations_today": used,
            "budget_remaining": max(0, budget - used),
            "auto_invoke": self.config.delegation.auto_invoke,
            "operating_rules": [
                "Engine 'native' (default): Zade's own coding loop on the LOCAL Ollama model — no external agent process, no cloud.",
                "Engine 'bridge': the configured CLI runs as a LOCAL COMPATIBILITY BRIDGE — under a local provider policy its environment is forced to the loopback Ollama Anthropic-compatible API with no inherited keys.",
                "There is no automatic fallback between engines or to any cloud provider; failures fail closed and are reported.",
                "Invoking a delegated build is an L3 action. A DIRECT founder command runs at full auto — immediately, including into a named project workspace — up to the daily budget. Autonomous (non-directed) runs auto-invoke only in the default delegation workspace; a workspace-targeted autonomous run always waits for the founder.",
                "When the agent hits a genuine decision it cannot make safely, it stops and files a founder decision item instead of guessing; the run resumes when the founder answers or clears the item.",
                "Every invocation is audited and its artifact filed as delegated-work evidence — a sourced claim, never native certainty.",
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
        workspace: str = "",
        directed: bool = False,
    ) -> dict[str, Any]:
        """Queue (and, when authorized, immediately run) a delegated build.

        ``directed=True`` marks a DIRECT founder command from chat: the command
        itself is the authorization, so the run executes at full auto — including
        into a founder-named project workspace — bounded only by the daily budget
        and the engine actually being able to run. Non-directed (autonomous) runs
        keep the conservative posture: a workspace target always waits for
        founder approval.
        """
        self._require_enabled()
        workspace = (workspace or "").strip()
        brief = (brief or "").strip() or self.build_brief(task=task, context=context, acceptance=acceptance)
        if workspace:
            # The approval item must show exactly where the agent will operate.
            brief = f"{brief}\n\n## Target project\nRun inside this existing project directory: {workspace}"
        want_auto = self.config.delegation.auto_invoke if auto_invoke is None else bool(auto_invoke)
        if workspace and not directed:
            # An AUTONOMOUS run aimed at a founder-named project directory
            # (outside the default delegation workspace) always waits for the
            # founder. A directed command already carries that authorization.
            want_auto = False

        metadata: dict[str, Any] = {"task": task.strip(), "brief": brief, "workspace": workspace}
        if directed:
            # Founder-implied approval: the work queue marks the item approved
            # with no typed-phrase request — she already gave the word in chat.
            metadata["founder_command"] = True
        result = self.work_queue.enqueue(
            kind="delegation_run",
            title=f"Delegate: {task.strip()[:80]}",
            detail="Invoke an external agent on a scoped brief.\n\n" + brief,
            action=DELEGATION_RUN_ACTION,
            target="external-agent",
            permission_tier="L3_EXTERNAL_ACTION",
            priority=60,
            source="founder.delegation" if directed else "delegation",
            metadata=metadata,
            unique_key=f"{DELEGATION_RUN_ACTION}:{utc_now()}",
        )
        payload = result.as_dict()

        # Budgeted auto-invoke: the authorized bypass. Only when auto is on, an
        # engine can actually run (native agent wired, or a bridge command
        # configured), and we're under today's cap. Otherwise it stays a gated
        # work item waiting on the founder.
        engine = getattr(self.config.delegation, "engine", "native")
        engine_ready = (
            (engine == "native" and self.coding_agent is not None)
            or (engine == "bridge" and bool(self.config.delegation.agent_command))
        )
        can_auto = (
            want_auto
            and engine_ready
            and self._invocations_today() < self.config.delegation.daily_budget
        )
        if can_auto:
            item = self.db.get_work_item(result.item_id)
            if item is not None:
                try:
                    dispatch = self.run_from_work_item(item)
                except Exception as exc:  # noqa: BLE001 - an exploding run is a flow error, not a 500
                    dispatch = {
                        "handler": DELEGATION_RUN_ACTION,
                        "status": "flow_error",
                        "ok": False,
                        "task": task.strip(),
                        "error": str(exc)[:400],
                    }
                # Close the loop on the item itself — an auto-invoked run must
                # never leave a stale "waiting for approval" entry in the Inbox
                # for work that already happened.
                self._finalize_auto_invoked_item(item.id, dispatch)
                return payload | {"auto_invoked": True, "dispatch": dispatch}
        reason = (
            "auto-invoke disabled" if not want_auto
            else "engine cannot run (native agent not wired / no agent command configured)" if not engine_ready
            else "daily budget reached — waiting on you in the Inbox"
        )
        return payload | {"auto_invoked": False, "reason": reason}

    def _finalize_auto_invoked_item(self, item_id: int, dispatch: dict[str, Any]) -> None:
        """Record the auto-invoked run's outcome on its own work item."""
        try:
            ok = bool(dispatch.get("ok"))
            status = str(dispatch.get("status") or "")
            item_status = "done" if ok or status == "needs_decision" else "error"
            error = "" if item_status == "done" else str(dispatch.get("error") or status)[:400]
            self.db.update_work_item(item_id, status=item_status, result=dispatch, error=error)
        except Exception:  # noqa: BLE001 - bookkeeping must not discard the dispatch result
            pass

    # ---- dispatch handler ----
    def run_from_work_item(self, item: WorkItem) -> dict[str, Any]:
        metadata = item.metadata or {}
        task = str(metadata.get("task", "")).strip()
        brief = str(metadata.get("brief", "")).strip()
        if not brief:
            raise ValueError("Delegation work item is missing its brief.")
        workspace = str(metadata.get("workspace", "")).strip()
        if workspace:
            problem = _target_workspace_problem(workspace)
            if problem:
                return {
                    "handler": DELEGATION_RUN_ACTION,
                    "status": "flow_error",
                    "ok": False,
                    "task": task,
                    "brief": brief,
                    "error": problem,
                }
        engine = getattr(self.config.delegation, "engine", "native")

        # Engine: native — Zade's own coding loop on the local Ollama model.
        # There is deliberately NO fallback from native to bridge or to any
        # cloud provider: a native failure is returned as a local failure.
        if engine == "native":
            if self.coding_agent is None:
                return {
                    "handler": DELEGATION_RUN_ACTION,
                    "status": "flow_error",
                    "ok": False,
                    "task": task,
                    "brief": brief,
                    "error": "Delegation engine is 'native' but no coding agent is wired in.",
                }
            return self._run_native(item=item, task=task, brief=brief, workspace=workspace)

        # Engine: brief — prepare-not-send.
        command = self.config.delegation.agent_command
        if engine == "brief" or not command:
            return {
                "handler": DELEGATION_RUN_ACTION,
                "status": "prepared",
                "ok": False,
                "task": task,
                "brief": brief,
                "note": (
                    "Delegation engine is prepare-not-send"
                    if engine == "brief"
                    else "No external agent configured; brief prepared for you to run manually."
                ),
            }

        # Engine: bridge — the external CLI as a LOCAL COMPATIBILITY BRIDGE.
        runner = self._runner or run_agent
        status = "ok"
        error = ""
        artifact = ""
        bridge_env: dict[str, str] | None = None
        bridge_note: dict[str, Any] = {}
        try:
            bridge_env, bridge_note = self._bridge_environment()
            artifact = runner(
                list(command),
                brief=brief,
                timeout=self.config.delegation.timeout_seconds,
                max_output_chars=self.config.delegation.max_output_chars,
                cwd=workspace or self._workspace_cwd(),
                env=bridge_env,
            )
        except Exception as exc:  # noqa: BLE001 - a failed invocation is a flow error, not a 500
            status = "flow_error"
            error = str(exc)[:400]

        # An external agent returns no audited step list, so its verification
        # claims cannot be cross-checked against executed commands — they are
        # marked unverified by construction.
        unverified_claims = find_unverified_claims(artifact, None)
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
                        "notes": (
                            "Produced by a delegated external agent. Treat as a sourced external claim, not native certainty."
                            + _unverified_notes_suffix(unverified_claims, engine="bridge")
                        ),
                        "metadata": {
                            "task": task,
                            "unverified_claims": unverified_claims,
                            "entity_boundary": "External agent produced; Zade records as delegated evidence.",
                        },
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
            details={
                "work_item_id": item.id,
                "task": task,
                "artifact_chars": len(artifact),
                "unverified_claims": unverified_claims,
                "evidence_id": evidence_id,
                "engine": "bridge",
                # Effective bridge posture: base host + model only — no prompts,
                # no keys. Proves where the subprocess was pointed.
                "bridge": bridge_note,
            },
        )
        return {
            "handler": DELEGATION_RUN_ACTION,
            "status": status,
            "ok": status == "ok",
            "task": task,
            "engine": "bridge",
            "bridge": bridge_note,
            "unverified_claims": unverified_claims,
            "artifact": artifact,
            "evidence_id": evidence_id,
            "error": error,
        }

    def _run_native(
        self, *, item: WorkItem, task: str, brief: str, workspace: str = ""
    ) -> dict[str, Any]:
        """Delegated build via the native local coding agent (no subprocess)."""
        assert self.coding_agent is not None  # guarded by run_from_work_item
        result = self.coding_agent.run(
            task=task, context=brief, workspace=workspace or None, verify_always=True
        )
        if str(result.get("status")) == "needs_decision":
            # The agent stopped on a genuine founder decision. That is a
            # successful outcome of the run — the pending work moves to a
            # decision item in the Inbox instead of being guessed at.
            return self._file_founder_decision(
                item=item, task=task, brief=brief, workspace=workspace, result=result
            )
        artifact = str(result.get("response") or "")
        unverified_claims = find_unverified_claims(artifact, result.get("steps"))
        auto_verification = (
            result.get("auto_verification")
            if isinstance(result.get("auto_verification"), dict)
            else None
        )
        workspace_changes = (
            result.get("workspace_changes")
            if isinstance(result.get("workspace_changes"), dict)
            else None
        )
        verify_note = ""
        if auto_verification is not None:
            if auto_verification.get("ok") is False:
                verify_note = (
                    " KERNEL CHECK FAILED on the changed files — the work did not verify; "
                    "the real failing output is in the artifact."
                )
            elif auto_verification.get("ok") is None:
                verify_note = (
                    " No kernel check could run for the changed files — the work is UNVERIFIED."
                )
        evidence_id = None
        error = str(result.get("error") or "")
        if result.get("ok") and artifact.strip():
            try:
                evidence = self.founder.create_evidence(
                    {
                        "evidence_type": "delegated_work",
                        "source": "delegation:native-coding-agent",
                        "reliability": self.config.delegation.default_reliability,
                        "claim_supported": f"Native local coding-agent artifact for '{task}': {artifact[:400]}",
                        "strength": 55,
                        "notes": (
                            "Produced by Zade's native coding agent on the local Ollama model "
                            f"{result.get('model')}. Verified-local run."
                            + verify_note
                            + _unverified_notes_suffix(unverified_claims, engine="native")
                        ),
                        "metadata": {
                            "task": task,
                            "model": result.get("model"),
                            "changed_files": result.get("changed_files", []),
                            "workspace_changes": workspace_changes,
                            "unverified_claims": unverified_claims,
                            "auto_verification": auto_verification,
                            "entity_boundary": "Local coding agent produced; recorded as delegated evidence.",
                        },
                    }
                )
                evidence_id = evidence.id
            except Exception as exc:  # noqa: BLE001 - filing must not discard a returned artifact
                error = (error + f" | filing error: {exc}")[:400]
        self.db.audit(
            actor="approved-handler",
            action=DELEGATION_RUN_ACTION,
            target="native-coding-agent",
            permission_tier=item.permission_tier,
            status="ok" if result.get("ok") else "flow_error",
            details={
                "work_item_id": item.id,
                "task": task,
                "engine": "native",
                "workspace": str(result.get("workspace") or workspace or ""),
                "model": result.get("model"),
                "provider": result.get("provider"),
                "rounds": result.get("rounds"),
                "changed_files": result.get("changed_files", []),
                "workspace_changes": workspace_changes,
                "artifact_chars": len(artifact),
                "unverified_claims": unverified_claims,
                "auto_verification": auto_verification,
                "evidence_id": evidence_id,
                "error": error,
            },
        )
        return {
            "handler": DELEGATION_RUN_ACTION,
            "status": "ok" if result.get("ok") else "flow_error",
            "ok": bool(result.get("ok")),
            "task": task,
            "engine": "native",
            "model": result.get("model"),
            "provider": result.get("provider"),
            "rounds": result.get("rounds"),
            "steps": result.get("steps", []),
            "changed_files": result.get("changed_files", []),
            "workspace_changes": workspace_changes,
            "unverified_claims": unverified_claims,
            "auto_verification": auto_verification,
            "artifact": artifact,
            "evidence_id": evidence_id,
            "error": error or str(result.get("error") or ""),
        }

    def _file_founder_decision(
        self,
        *,
        item: WorkItem,
        task: str,
        brief: str,
        workspace: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """File the agent's blocking question as a founder decision item.

        The decision item carries a resume brief: clearing it re-runs the
        delegation with the question surfaced, and an answer given in chat
        routes as a fresh directed command. Either way the work is queued, not
        lost — and nothing was guessed at."""
        question_info = result.get("founder_question") or {}
        question = str(question_info.get("question") or "").strip() or "The agent needs your direction to continue."
        options = [str(o) for o in (question_info.get("options") or []) if str(o).strip()]
        options_block = ("\nOptions:\n" + "\n".join(f"- {o}" for o in options)) if options else ""
        resume_brief = (
            f"{brief}\n\n## Founder decision\n"
            f"The previous run stopped on this question:\n{question}{options_block}\n"
            "If the founder cleared this item without a written answer, choose the safest "
            "reasonable option, state which you chose, and complete the task end to end."
        )
        decision_item_id = None
        error = ""
        try:
            queued = self.work_queue.enqueue(
                kind="founder_decision",
                title=f"Decision needed: {question[:70]}",
                detail=(
                    f"The delegated run for '{task}' is paused on a decision.\n\n"
                    f"Question: {question}{options_block}\n\n"
                    "Clearing this item resumes the run (best safe judgment); answering in chat "
                    "with direction starts a fresh directed run."
                ),
                action=DELEGATION_RUN_ACTION,
                target="native-coding-agent",
                permission_tier="L3_EXTERNAL_ACTION",
                priority=70,
                source="delegation",
                # founder_decision: Zade's own question to the founder. Her
                # answer/confirmation IS the approval — no typed phrase.
                metadata={
                    "task": task,
                    "brief": resume_brief,
                    "workspace": workspace,
                    "founder_decision": True,
                },
                unique_key=f"{DELEGATION_RUN_ACTION}:decision:{utc_now()}",
            )
            decision_item_id = queued.item_id
        except Exception as exc:  # noqa: BLE001 - the question must still reach the founder via the reply
            error = f"decision filing error: {str(exc)[:200]}"
        self.db.audit(
            actor="approved-handler",
            action=DELEGATION_RUN_ACTION,
            target="native-coding-agent",
            permission_tier=item.permission_tier,
            status="needs_decision",
            details={
                "work_item_id": item.id,
                "task": task,
                "engine": "native",
                "question": question,
                "options": options,
                "decision_item_id": decision_item_id,
                "rounds": result.get("rounds"),
                "changed_files": result.get("changed_files", []),
            },
        )
        return {
            "handler": DELEGATION_RUN_ACTION,
            "status": "needs_decision",
            "ok": True,
            "task": task,
            "engine": "native",
            "model": result.get("model"),
            "rounds": result.get("rounds"),
            "steps": result.get("steps", []),
            "changed_files": result.get("changed_files", []),
            "founder_question": {"question": question, "options": options},
            "decision_item_id": decision_item_id,
            "artifact": str(result.get("response") or ""),
            "error": error,
        }

    def _bridge_environment(self) -> tuple[dict[str, str], dict[str, Any]]:
        """Build the sanitized subprocess environment for the compatibility bridge.

        Under a local provider policy (local_only, or cloud not explicitly
        allowed) the bridge may only speak to the loopback Ollama
        Anthropic-compatible API: ANTHROPIC_* inheritance is stripped (no keys,
        no remote base URL), the base URL is forced to the local Ollama host,
        and the model is the resolved local coding model. Returns (env, note)
        where note records the effective host/model for the audit trail —
        never prompts or secrets.
        """
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("ANTHROPIC_")}
        policy = getattr(self.config.ollama, "provider_policy", "local_only")
        cloud_ok = policy == "cloud_allowed" and bool(
            getattr(self.config.ollama, "allow_cloud_inference", False)
        )
        if cloud_ok:
            # Deliberate cloud opt-in: inherit the caller's Anthropic settings.
            return dict(os.environ), {"mode": "cloud_opt_in", "policy": policy}
        base_url = self.config.ollama.base_url
        host = (urllib.parse.urlparse(base_url).hostname or "").lower()
        if host not in _LOOPBACK_HOSTS:
            raise ValueError(
                f"Bridge refused: Ollama base_url host {host!r} is not loopback under a local "
                "provider policy. No subprocess was launched."
            )
        model = self._bridge_model()
        env["ANTHROPIC_BASE_URL"] = base_url
        env["ANTHROPIC_AUTH_TOKEN"] = "ollama"
        env["ANTHROPIC_MODEL"] = model
        env["ANTHROPIC_SMALL_FAST_MODEL"] = model
        note = {
            "mode": "local_compatibility_bridge",
            "policy": policy,
            "base_host": host,
            "model": model,
            "anthropic_api_key_present": False,
        }
        return env, note

    def _bridge_model(self) -> str:
        if self.coding_agent is not None:
            try:
                return str(self.coding_agent.resolve_model())
            except Exception:  # noqa: BLE001 - fall through to the configured coding model
                pass
        from .ollama import is_cloud_model

        model = (getattr(self.config.ollama, "coding_agent_model", "") or "").strip() or (
            self.config.ollama.coding_model
        )
        if is_cloud_model(model):
            raise ValueError(
                f"Bridge refused: model {model!r} is a cloud variant, forbidden under a local "
                "provider policy."
            )
        return model

    # ---- internals ----
    def _workspace_cwd(self) -> str | None:
        """Resolve (and create) the delegated-work directory. None = inherit the
        kernel's cwd, the pre-workspace behavior."""
        root = (self.config.delegation.workspace_root or "").strip()
        if not root:
            return None
        path = Path(root).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

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
                and event.get("status") in {"ok", "flow_error", "needs_decision"}
            ):
                count += 1
        return count


def _target_workspace_problem(workspace: str) -> str:
    """Validate a founder-named target project directory at dispatch time.
    Returns a human-readable refusal, or "" when the target is usable. The
    kernel's own repository is never a valid target — delegated runs do not
    modify the kernel itself."""
    path = Path(workspace).expanduser()
    if not path.is_dir():
        return f"Target project directory does not exist: {workspace!r}. Nothing was run."
    resolved = path.resolve()
    kernel_root = Path(__file__).resolve().parents[2]
    if resolved == kernel_root or kernel_root in resolved.parents:
        return (
            "Target project resolves inside the kernel's own repository; "
            "delegated runs may not modify the kernel. Nothing was run."
        )
    return ""


# ---- module-level invoker (the actual external egress) ----
def run_agent(
    command: list[str],
    *,
    brief: str,
    timeout: float = 600.0,
    max_output_chars: int = 20000,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Run a configured external agent command, feeding the brief on stdin.

    argv only (no shell parsing). Captures stdout+stderr, byte-bounded via the
    char cap. A non-zero exit or a timeout raises, surfaced by the caller as a
    flow error. ``cwd`` confines the agent to the delegation workspace; ``env``
    is the sanitized bridge environment (None = inherit, used only by tests).
    """
    try:
        completed = subprocess.run(
            command,
            input=brief,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=cwd,
            env=env,
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
