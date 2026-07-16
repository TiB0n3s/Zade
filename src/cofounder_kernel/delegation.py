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
                "Invoking a delegated build is an L3 action. Auto-invoke runs it without asking up to the daily budget; past that it requires the typed confirmation phrase.",
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
    ) -> dict[str, Any]:
        self._require_enabled()
        workspace = (workspace or "").strip()
        brief = (brief or "").strip() or self.build_brief(task=task, context=context, acceptance=acceptance)
        if workspace:
            # The approval item must show exactly where the agent will operate.
            brief = f"{brief}\n\n## Target project\nRun inside this existing project directory: {workspace}"
        want_auto = self.config.delegation.auto_invoke if auto_invoke is None else bool(auto_invoke)
        if workspace:
            # A run aimed at a founder-named project directory (outside the
            # default delegation workspace) always waits for the typed phrase.
            want_auto = False

        result = self.work_queue.enqueue(
            kind="delegation_run",
            title=f"Delegate: {task.strip()[:80]}",
            detail="Invoke an external agent on a scoped brief.\n\n" + brief,
            action=DELEGATION_RUN_ACTION,
            target="external-agent",
            permission_tier="L3_EXTERNAL_ACTION",
            priority=60,
            source="delegation",
            metadata={"task": task.strip(), "brief": brief, "workspace": workspace},
            unique_key=f"{DELEGATION_RUN_ACTION}:{utc_now()}",
        )
        payload = result.as_dict()

        # Budgeted auto-invoke: the authorized bypass. Only when auto is on, an
        # engine can actually run (native agent wired, or a bridge command
        # configured), and we're under today's cap. Otherwise it stays a gated
        # work item waiting on the typed phrase.
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
                dispatch = self.run_from_work_item(item)
                return payload | {"auto_invoked": True, "dispatch": dispatch}
        reason = (
            "auto-invoke disabled" if not want_auto
            else "engine cannot run (native agent not wired / no agent command configured)" if not engine_ready
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
            details={
                "work_item_id": item.id,
                "task": task,
                "artifact_chars": len(artifact),
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
            "artifact": artifact,
            "evidence_id": evidence_id,
            "error": error,
        }

    def _run_native(
        self, *, item: WorkItem, task: str, brief: str, workspace: str = ""
    ) -> dict[str, Any]:
        """Delegated build via the native local coding agent (no subprocess)."""
        assert self.coding_agent is not None  # guarded by run_from_work_item
        result = self.coding_agent.run(task=task, context=brief, workspace=workspace or None)
        artifact = str(result.get("response") or "")
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
                        ),
                        "metadata": {
                            "task": task,
                            "model": result.get("model"),
                            "changed_files": result.get("changed_files", []),
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
                "artifact_chars": len(artifact),
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
            "artifact": artifact,
            "evidence_id": evidence_id,
            "error": error or str(result.get("error") or ""),
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
                and event.get("status") in {"ok", "flow_error"}
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
