"""Durable, local-first execution loop for documented project MVP criteria."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

from .config import KernelConfig
from .db import KernelDatabase, utc_now
from .project_autonomy import APPROVAL_BOUNDARIES, ProjectAutonomyReporter
from .project_mvp_planner import MvpPlanResult, ProjectMvpPlanner


_PRIORITY_ORDER = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
_TERMINAL_PHASES = {"needs_decision", "approval_required", "blocked", "awaiting_external_boundary"}
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_PROTECTED_DEPENDENCY_MANIFESTS = {
    "package.json",
    "pubspec.yaml",
    "pyproject.toml",
    "cargo.toml",
    "go.mod",
}
_SAFE_VERIFY_EXECUTABLES = {
    "python",
    "python.exe",
    "py",
    "py.exe",
    "pytest",
    "pytest.exe",
    "node",
    "node.exe",
    "npm",
    "npm.cmd",
    "npx",
    "npx.cmd",
    "flutter",
    "flutter.bat",
    "dart",
    "dart.bat",
    "java",
    "java.exe",
    "gradle",
    "gradle.bat",
    "gradlew",
    "gradlew.bat",
}


class ProjectAutonomyOrchestrator:
    """Advance one dependency-ready criterion at a time under a project lease."""

    def __init__(
        self,
        *,
        config: KernelConfig,
        db: KernelDatabase,
        reporter: ProjectAutonomyReporter,
        planner: ProjectMvpPlanner,
        delegation: Any,
        owner: str | None = None,
    ):
        self.config = config
        self.db = db
        self.reporter = reporter
        self.planner = planner
        self.delegation = delegation
        self.store = reporter.store
        self.owner = str(owner or f"zade-project-autonomy:{os.getpid()}")
        self._lock = threading.RLock()
        self._wake_reasons: list[dict[str, Any]] = []
        self._shutdown = False
        self._started = False
        self._workers: list[threading.Thread] = []
        self._condition = threading.Condition(self._lock)
        self._last_results: list[dict[str, Any]] = []
        self._wake_epoch = 0

    def start(self) -> None:
        with self._condition:
            if self._started:
                return
            if self._shutdown:
                raise RuntimeError("A shut down project autonomy orchestrator cannot restart.")
            self._started = True
            for index in range(self.config.project_intake.autonomy_max_workers):
                thread = threading.Thread(
                    target=self._worker_loop,
                    name=f"project-autonomy-{index + 1}",
                    daemon=True,
                )
                self._workers.append(thread)
                thread.start()

    def wake(self, project_id: int | None = None, *, reason: str) -> dict[str, Any]:
        with self._lock:
            if self._shutdown:
                return {"accepted": False, "reason": "orchestrator is shut down"}
            self._wake_reasons.append(
                {
                    "project_id": int(project_id) if project_id is not None else None,
                    "reason": str(reason or "unspecified")[:120],
                    "created_at": utc_now(),
                }
            )
            self._wake_reasons = self._wake_reasons[-200:]
            self._wake_epoch += 1
            self._condition.notify_all()
        return {"accepted": True, "project_id": project_id, "reason": reason}

    def replan(self, project_id: int) -> dict[str, Any]:
        """Replace a completed plan from the project's current documentation.

        This is an explicit founder-directed action.  A scan ingests newly added
        documents, but intentionally does not reopen a completed MVP on its own.
        """
        project = self.reporter.get_project(project_id)
        planning_project = dict(project)
        metadata = dict(project.get("metadata") or {})
        metadata["planner_founder_answers"] = [
            str(event.get("detail") or "")
            for event in reversed(self.db.list_project_events(project_id, limit=None))
            if event.get("event_type") == "decision_applied"
            and str(event.get("detail") or "").strip()
        ]
        planning_project["metadata"] = metadata
        planned = self.planner.plan(planning_project)
        founder_answers = list(metadata["planner_founder_answers"])
        if (
            planned.needs_decision is not None
            and self._decision_repeats_accepted_answer(planned.needs_decision, founder_answers)
        ):
            metadata["planner_rejected_duplicate_decision"] = json.dumps(
                planned.needs_decision, sort_keys=True
            )
            planning_project["metadata"] = metadata
            planned = self.planner.plan(planning_project)
        if planned.needs_decision is not None:
            self.reporter.begin_new_scope(
                project_id,
                plan_revision=planned.plan_revision,
                next_action="founder decision required before planning the documented scope",
            )
            project = self.reporter.get_project(project_id)
            decision_id = self._file_decision(
                project,
                planned.needs_decision,
                plan_revision=planned.plan_revision,
                criterion_id=None,
            )
            return self.reporter.report_needs_decision(
                project_id,
                decision_id=decision_id,
                question=planned.needs_decision["question"],
                recommendation=planned.needs_decision["recommendation"],
                options=planned.needs_decision["options"],
            )
        return self.reporter.plan(
            project_id,
            criteria=planned.criteria,
            priority=str((project.get("autonomy") or {}).get("priority") or "normal"),
            next_action="build the first dependency-ready criterion from the current documentation",
            external_boundaries=planned.external_boundaries,
            plan_revision=planned.plan_revision,
        )

    def claim_ready(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        if self._shutdown or not self.config.project_intake.autonomy_enabled:
            return []
        maximum = min(
            max(int(limit or self.config.project_intake.autonomy_max_workers), 1),
            self.config.project_intake.autonomy_max_workers,
        )
        candidates: list[tuple[int, int, dict[str, Any]]] = []
        for project in self.reporter.list_views(limit=500):
            state = project.get("autonomy") or {}
            if not _is_runnable(state):
                continue
            candidates.append(
                (
                    _PRIORITY_ORDER.get(str(state.get("priority") or "normal"), 2),
                    int(project["id"]),
                    project,
                )
            )
        claims: list[dict[str, Any]] = []
        for _priority, project_id, project in sorted(candidates)[: max(maximum * 4, maximum)]:
            stored = self.store.get(project_id)
            run_token = uuid.uuid4().hex
            lease = self.store.claim(
                project_id,
                owner=self.owner,
                run_id=run_token,
                lease_seconds=self.config.project_intake.autonomy_lease_seconds,
                expected_version=int(stored.get("version") or 0),
            )
            if lease is None:
                continue
            claims.append(
                {
                    "project_id": project_id,
                    "project": project,
                    "owner": self.owner,
                    "run_id": run_token,
                    "claimed_version": int(stored.get("version") or 0),
                    "lease": lease,
                }
            )
            if len(claims) >= maximum:
                break
        return claims

    def release_claim(self, claim: dict[str, Any]) -> bool:
        return self.store.release(
            int(claim["project_id"]),
            owner=str(claim["owner"]),
            run_id=str(claim["run_id"]),
        )

    def run_once(self) -> dict[str, Any]:
        if self._shutdown:
            return {"status": "shutdown"}
        claims = self.claim_ready(limit=1)
        if not claims:
            return {"status": "idle"}
        claim = claims[0]
        try:
            return self._execute_claim(claim)
        finally:
            self.release_claim(claim)

    def recover(self) -> dict[str, int]:
        expired = self.store.clear_expired_leases()
        orphaned = self.store.clear_orphaned_process_leases()
        unfinished = 0
        for project in self.reporter.list_views(limit=500):
            if _is_runnable(project.get("autonomy") or {}):
                unfinished += 1
        self.wake(reason="startup recovery")
        return {
            "expired_leases_cleared": expired,
            "orphaned_leases_cleared": orphaned,
            "unfinished_seen": unfinished,
            "duplicate_runs_created": 0,
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.config.project_intake.autonomy_enabled,
                "shutdown": self._shutdown,
                "started": self._started,
                "workers_alive": sum(1 for worker in self._workers if worker.is_alive()),
                "owner": self.owner,
                "max_workers": self.config.project_intake.autonomy_max_workers,
                "lease_seconds": self.config.project_intake.autonomy_lease_seconds,
                "repair_attempts": self.config.project_intake.autonomy_repair_attempts,
                "recent_wakes": list(self._wake_reasons[-20:]),
                "recent_results": list(self._last_results[-20:]),
            }

    def shutdown(self, wait: bool = False) -> None:
        with self._condition:
            self._shutdown = True
            self._condition.notify_all()
        if wait:
            for worker in list(self._workers):
                worker.join(timeout=5)

    def _worker_loop(self) -> None:
        reconcile = max(5, int(self.config.project_intake.autonomy_reconcile_seconds))
        observed_epoch = -1
        while True:
            with self._condition:
                if self._shutdown:
                    return
                if observed_epoch == self._wake_epoch:
                    self._condition.wait(timeout=reconcile)
                    if self._shutdown:
                        return
                observed_epoch = self._wake_epoch
            while not self._shutdown:
                try:
                    result = self.run_once()
                except Exception as exc:  # noqa: BLE001 - one project cannot kill a worker
                    result = {
                        "status": "worker_error",
                        "error": f"{type(exc).__name__}: {exc}"[:400],
                        "traceback": traceback.format_exc()[-4000:],
                    }
                with self._lock:
                    self._last_results.append({**result, "recorded_at": utc_now()})
                    self._last_results = self._last_results[-100:]
                if result.get("status") in {"idle", "shutdown"}:
                    break
                # Yield between projects so two busy workers do not spin on the
                # same SQLite writer lock while autonomous work remains queued.
                time.sleep(0.01)

    def _execute_claim(self, claim: dict[str, Any]) -> dict[str, Any]:
        project_id = int(claim["project_id"])
        project = self.reporter.get_project(project_id)
        root = Path(str(project["canonical_path"])).resolve()
        state = self.reporter.state(project_id)
        if state.get("phase") == "mvp_complete":
            self.reporter.migrate_legacy_mvp_completion(project_id)
            state = self.reporter.state(project_id)
        if state.get("paused") is True:
            return {"status": "paused", "project_id": project_id}
        policy_problem = self._local_execution_problem()
        if policy_problem:
            self.reporter.report_blocked(
                project_id,
                reason=policy_problem,
                needed="restore the native loopback project-intake execution policy",
            )
            return {"status": "blocked", "project_id": project_id, "reason": policy_problem}

        quarantine_root = (
            self.config.paths.data_dir
            / "project-autonomy-failed-attempts"
            / str(project_id)
        )
        try:
            # A recovered worker may inherit files from an interrupted attempt.
            # Preserve those files outside the repo, restore the last commit,
            # and start from a clean checkpoint. Never feed a half-applied
            # attempt into the next model call.
            _ensure_clean_repository(root, allow_dirty=True)
            dirty = _git_checked(root, "status", "--porcelain").stdout.strip()
            if dirty:
                if str(state.get("phase") or "") not in {"building", "verifying"}:
                    raise ValueError(
                        "Repository has uncommitted changes before the autonomy increment: "
                        + dirty[:800]
                    )
                checkpoint_head = _git_checked(root, "rev-parse", "HEAD").stdout.strip()
                quarantine = _quarantine_and_restore_attempt(
                    root,
                    expected_head=checkpoint_head,
                    quarantine_root=quarantine_root,
                )
                if quarantine is not None:
                    self.db.append_project_event(
                        project_id,
                        event_type="autonomy_attempt_quarantined",
                        detail="Recovered an interrupted build from a clean Git checkpoint.",
                        metadata={"quarantine_path": str(quarantine)},
                    )
        except ValueError as exc:
            self.reporter.report_blocked(
                project_id,
                reason=str(exc),
                needed="restore a clean, reviewable Git baseline",
            )
            return {"status": "blocked", "project_id": project_id, "reason": str(exc)}

        if not state.get("mvp_criteria"):
            scope_kind = "continuation" if state.get("scope_kind") == "continuation" else "mvp"
            planning_project = dict(project)
            planning_metadata = dict(project.get("metadata") or {})
            founder_answers = [
                str(event.get("detail") or "")
                for event in reversed(self.db.list_project_events(project_id, limit=None))
                if event.get("event_type") == "decision_applied"
                and str(event.get("detail") or "").strip()
            ]
            planning_metadata["planner_founder_answers"] = founder_answers
            planning_metadata["autonomy"] = state
            planning_project["metadata"] = planning_metadata
            try:
                planned = self.planner.plan(planning_project)
            except ValueError as exc:
                reason = f"Local {scope_kind} planner returned an invalid plan: {exc}"
                self.reporter.report_blocked(
                    project_id,
                    reason=reason,
                    verification_output=str(exc),
                    attempts=1,
                    needed=f"correct the documented {scope_kind} plan and wake project autonomy",
                )
                return {"status": "blocked", "project_id": project_id, "reason": reason}
            if planned.needs_decision is not None:
                if self._decision_repeats_accepted_answer(
                    planned.needs_decision, founder_answers
                ):
                    planning_metadata["planner_rejected_duplicate_decision"] = json.dumps(
                        planned.needs_decision, sort_keys=True
                    )
                    planning_project["metadata"] = planning_metadata
                    try:
                        planned = self.planner.plan(planning_project)
                    except ValueError as exc:
                        reason = f"Local {scope_kind} planner returned an invalid corrected plan: {exc}"
                        self.reporter.report_blocked(
                            project_id, reason=reason, verification_output=str(exc), attempts=2,
                            needed=f"correct the documented {scope_kind} plan and wake project autonomy",
                        )
                        return {"status": "blocked", "project_id": project_id, "reason": reason}
                    if planned.needs_decision is not None and self._decision_repeats_accepted_answer(
                        planned.needs_decision, founder_answers
                    ):
                        reason = "Local MVP planner repeated a founder decision that was already resolved after correction."
                        self.reporter.report_blocked(
                            project_id, reason=reason,
                            verification_output=json.dumps(planned.needs_decision, indent=2, sort_keys=True),
                            attempts=2,
                            needed=f"repair the local {scope_kind} planner response and wake project autonomy",
                        )
                        return {"status": "blocked", "project_id": project_id, "reason": reason}
                if planned.needs_decision is not None:
                    decision_id = self._file_decision(
                        project,
                        planned.needs_decision,
                        plan_revision=planned.plan_revision,
                        criterion_id=None,
                    )
                    self.reporter.report_needs_decision(
                        project_id,
                        decision_id=decision_id,
                        question=planned.needs_decision["question"],
                        recommendation=planned.needs_decision["recommendation"],
                        options=planned.needs_decision["options"],
                    )
                    return {
                        "status": "needs_decision",
                        "project_id": project_id,
                        "decision_id": decision_id,
                    }
            if scope_kind == "continuation" and not planned.criteria:
                self.reporter.await_external_boundary(
                    project_id,
                    external_boundaries=planned.external_boundaries,
                    source_hash=planned.source_hash,
                )
                return {"status": "awaiting_external_boundary", "project_id": project_id}
            if scope_kind == "continuation":
                self.reporter.begin_continuation(
                    project_id,
                    criteria=planned.criteria,
                    priority=str(state.get("priority") or "normal"),
                    next_action="build the first dependency-ready documented continuation criterion",
                    external_boundaries=planned.external_boundaries,
                    plan_revision=planned.plan_revision,
                    source_hash=planned.source_hash,
                )
            else:
                self.reporter.plan(
                    project_id,
                    criteria=planned.criteria,
                    priority=str(state.get("priority") or "normal"),
                    next_action="build the first dependency-ready documented MVP criterion",
                    external_boundaries=planned.external_boundaries,
                    plan_revision=planned.plan_revision,
                )
            state = self.reporter.state(project_id)
        criterion = _next_ready_criterion(state)
        if criterion is None:
            pending = [
                item
                for item in state.get("mvp_criteria") or []
                if item.get("status") != "complete"
            ]
            if pending:
                reason = "No dependency-ready MVP criterion exists in the durable plan."
                self.reporter.report_blocked(
                    project_id,
                    reason=reason,
                    needed="repair the documented MVP dependency graph",
                )
                return {
                    "status": "blocked",
                    "project_id": project_id,
                    "reason": reason,
                }
            return self._complete_if_ready(project_id, root, None)

        increment = int(state.get("current_increment") or 0)
        if state.get("phase") in {"planning", "ready_for_next_increment"}:
            increment += 1
            run_ref = _run_reference(project_id, state.get("plan_revision"), criterion["id"], increment)
            self.reporter.begin_increment(
                project_id,
                criterion_id=criterion["id"],
                increment=increment,
                run_id=run_ref,
                next_action=f"implement and verify {criterion['title']}",
            )
        else:
            increment = max(increment, 1)
            run_ref = _run_reference(project_id, state.get("plan_revision"), criterion["id"], increment)
            self.reporter.bind_run(project_id, run_id=run_ref)

        failure_output = ""
        attempts = self.config.project_intake.autonomy_repair_attempts + 1
        increment_head = _git_checked(root, "rev-parse", "HEAD").stdout.strip()
        for attempt in range(attempts):
            self._renew(claim)
            current = self.reporter.state(project_id)
            attempt_head = _git_checked(root, "rev-parse", "HEAD").stdout.strip()
            unique_key = (
                f"project-autonomy:{project_id}:{current.get('plan_revision') or 'unplanned'}:"
                f"{criterion['id']}:{increment}:{attempt}"
            )
            result = self.delegation.queue_delegation(
                task=(
                    f"Complete documented {state.get('scope_kind') or 'mvp'} criterion "
                    f"{criterion['id']}: {criterion['title']}"
                ),
                brief=self._build_brief(
                    project=project,
                    criterion=criterion,
                    root=root,
                    failure_output=failure_output,
                    attempt=attempt,
                ),
                acceptance="\n".join(str(item) for item in criterion.get("acceptance_checks") or []),
                auto_invoke=True,
                workspace=str(root),
                directed=True,
                unique_key=unique_key,
            )
            dispatch = result.get("dispatch") if isinstance(result.get("dispatch"), dict) else {}
            manifest_problem = _protected_manifest_rewrite_problem(root, attempt_head)
            if manifest_problem:
                quarantine = _quarantine_and_restore_attempt(
                    root,
                    expected_head=attempt_head,
                    quarantine_root=quarantine_root,
                )
                reason = (
                    "Autonomous attempt rewrote a protected dependency manifest: "
                    f"{manifest_problem}"
                )
                self.db.append_project_event(
                    project_id,
                    event_type="autonomy_attempt_quarantined",
                    detail=(
                        f"Attempt {attempt + 1} violated dependency-manifest integrity "
                        "and was rolled back without retry."
                    ),
                    metadata={
                        "attempt": attempt + 1,
                        "criterion_id": criterion["id"],
                        "failure_class": "protected_manifest_rewrite",
                        "quarantine_path": str(quarantine) if quarantine is not None else None,
                    },
                )
                self.reporter.report_blocked(
                    project_id,
                    reason=reason[:400],
                    criterion_id=criterion["id"],
                    verification_output=reason,
                    attempts=attempt + 1,
                    needed=(
                        "preserve the existing dependency manifest and make only "
                        "additive package changes"
                    ),
                )
                return {
                    "status": "blocked",
                    "project_id": project_id,
                    "criterion_id": criterion["id"],
                    "attempts": attempt + 1,
                    "reason": reason[:400],
                }
            local_choice = _reversible_local_implementation_choice(dispatch)
            if local_choice:
                quarantine = _quarantine_and_restore_attempt(
                    root,
                    expected_head=attempt_head,
                    quarantine_root=quarantine_root,
                )
                decision_item_id = _positive_int(dispatch.get("decision_item_id"))
                if decision_item_id is not None and self.db.get_work_item(decision_item_id) is not None:
                    self.db.update_work_item(
                        decision_item_id,
                        status="done",
                        result={
                            "status": "local_implementation_choice_applied",
                            "choice": local_choice,
                            "resumed_by": "project_autonomy_orchestrator",
                        },
                    )
                self.db.append_project_event(
                    project_id,
                    event_type="local_implementation_choice_applied",
                    detail=local_choice,
                    work_item_id=decision_item_id,
                    metadata={
                        "attempt": attempt + 1,
                        "criterion_id": criterion["id"],
                        "quarantine_path": str(quarantine) if quarantine is not None else None,
                    },
                )
                failure_output = (
                    "The kernel resolved a reversible, local implementation choice without "
                    f"interrupting the founder: {local_choice}\n\n"
                    "Apply this choice and complete the documented criterion."
                )
                continue
            boundary = self._classify_boundary(project, criterion, dispatch)
            if boundary is not None:
                _quarantine_and_restore_attempt(
                    root,
                    expected_head=attempt_head,
                    quarantine_root=quarantine_root,
                )
                return boundary
            if self.reporter.state(project_id).get("paused") is True:
                _quarantine_and_restore_attempt(
                    root,
                    expected_head=attempt_head,
                    quarantine_root=quarantine_root,
                )
                return {
                    "status": "paused",
                    "project_id": project_id,
                    "criterion_id": criterion["id"],
                }
            problem = _dispatch_problem(dispatch)
            if problem:
                failure_output = (
                    f"{problem}\n\nRepair the current working tree in place. Preserve the "
                    "implementation already present, correct the reported mechanical failure, "
                    "and rerun the project checks."
                )
                self.db.append_project_event(
                    project_id,
                    event_type="autonomy_repair_continuing",
                    detail=f"Attempt {attempt + 1} failed mechanical verification; repair in place.",
                    metadata={
                        "attempt": attempt + 1,
                        "criterion_id": criterion["id"],
                    },
                )
                continue
            try:
                commit = _commit_increment(root, criterion)
                self._renew(claim)
                verification = _post_commit_verification(root, dispatch, commit)
                current_phase = self.reporter.state(project_id).get("phase")
                if current_phase == "building":
                    self.reporter.begin_verification(project_id, run_id=run_ref)
                self.reporter.complete_criterion(
                    project_id,
                    criterion["id"],
                    verification=verification,
                    commit=commit,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                failure_output = str(exc)[:2000]
                continue
            return self._complete_if_ready(project_id, root, verification)

        reason = failure_output or "local delegated execution did not produce passing evidence"
        current_head = _git_checked(root, "rev-parse", "HEAD").stdout.strip()
        quarantine = None
        if current_head == increment_head:
            quarantine = _quarantine_and_restore_attempt(
                root,
                expected_head=increment_head,
                quarantine_root=quarantine_root,
            )
        if quarantine is not None:
            self.db.append_project_event(
                project_id,
                event_type="autonomy_attempt_quarantined",
                detail=(
                    f"Repair budget exhausted after {attempts} attempts; the cumulative "
                    "working tree was quarantined before automatic requeue."
                ),
                metadata={
                    "attempt": attempts,
                    "criterion_id": criterion["id"],
                    "quarantine_path": str(quarantine),
                },
            )
        self.reporter.requeue_mechanical_repair(
            project_id,
            criterion_id=criterion["id"],
            verification_output=reason,
            attempts=attempts,
        )
        return {
            "status": "repair_pending",
            "project_id": project_id,
            "criterion_id": criterion["id"],
            "attempts": attempts,
            "reason": reason[:400],
        }

    def _complete_if_ready(
        self,
        project_id: int,
        root: Path,
        verification: dict[str, Any] | None,
    ) -> dict[str, Any]:
        state = self.reporter.state(project_id)
        required = [item for item in state.get("mvp_criteria") or [] if item.get("required", True)]
        pending = [item for item in required if item.get("status") != "complete"]
        if pending:
            completed = next(
                (item for item in state.get("mvp_criteria") or [] if item.get("id") == state.get("current_criterion_id")),
                None,
            )
            return {
                "status": "criterion_complete",
                "project_id": project_id,
                "criterion_id": (completed or {}).get("id"),
                "next_criterion_id": _next_ready_criterion(state)["id"]
                if _next_ready_criterion(state)
                else None,
            }
        if verification is None:
            last = required[-1].get("verification") if required else None
            if not isinstance(last, dict):
                raise ValueError("Final MVP verification evidence is unavailable.")
            commands = required[-1].get("verification_commands") or []
            verification = _run_declared_verification(root, commands)
        if state.get("scope_kind") == "continuation":
            waited = self.reporter.await_external_boundary(project_id)
            return {
                "status": "awaiting_external_boundary",
                "project_id": project_id,
                "criterion_id": state.get("current_criterion_id"),
                "commit": (waited.get("metadata") or {}).get("autonomy", {}).get("repo_head"),
            }
        completed_project = self.reporter.complete_mvp(
            project_id, final_verification=verification
        )
        return {
            "status": "mvp_complete",
            "project_id": project_id,
            "criterion_id": state.get("current_criterion_id"),
            "commit": (completed_project.get("metadata") or {}).get("autonomy", {}).get(
                "mvp_completed_commit"
            ),
        }

    def _classify_boundary(
        self,
        project: dict[str, Any],
        criterion: dict[str, Any],
        dispatch: dict[str, Any],
    ) -> dict[str, Any] | None:
        status = str(dispatch.get("status") or "").strip().lower()
        if status == "needs_decision":
            question = dispatch.get("founder_question")
            question = question if isinstance(question, dict) else {}
            normalized = {
                "question": str(
                    question.get("question")
                    or "Which documented option should Zade use to continue?"
                ).strip(),
                "recommendation": str(
                    question.get("recommendation")
                    or ((question.get("options") or ["Use the safest local-first option"])[0])
                ).strip(),
                "options": _decision_options(question),
            }
            decision_id = _positive_int(dispatch.get("decision_item_id"))
            if decision_id is None or self.db.get_work_item(decision_id) is None:
                decision_id = self._file_decision(
                    project,
                    normalized,
                    plan_revision=str(self.reporter.state(project["id"]).get("plan_revision") or ""),
                    criterion_id=criterion["id"],
                )
            else:
                self.db.update_work_item_proposal(
                    decision_id,
                    metadata={
                        "project_id": project["id"],
                        "project_autonomy": True,
                        "project_autonomy_resume_only": True,
                        "workspace": project["canonical_path"],
                        "founder_question": normalized,
                    },
                )
            self.reporter.report_needs_decision(
                project["id"],
                decision_id=decision_id,
                question=normalized["question"],
                recommendation=normalized["recommendation"],
                options=normalized["options"],
            )
            return {
                "status": "needs_decision",
                "project_id": project["id"],
                "criterion_id": criterion["id"],
                "decision_id": decision_id,
            }
        if status == "approval_required":
            boundary = str(dispatch.get("boundary") or "").strip()
            if boundary not in APPROVAL_BOUNDARIES:
                return None
            approval_id = _positive_int(dispatch.get("approval_request_id"))
            if approval_id is None or self.db.get_approval_request(approval_id) is None:
                approval_id = self._file_approval(project, criterion, dispatch, boundary)
            self.reporter.report_approval_required(
                project["id"],
                approval_request_id=approval_id,
                action=str(dispatch.get("proposed_action") or criterion["title"]),
                reason=str(dispatch.get("reason") or f"Crosses {boundary}"),
                boundary=boundary,
            )
            return {
                "status": "approval_required",
                "project_id": project["id"],
                "criterion_id": criterion["id"],
                "approval_request_id": approval_id,
            }
        return None

    def _decision_repeats_accepted_answer(
        self,
        decision: dict[str, Any],
        answers: list[str],
    ) -> bool:
        stopwords = {
            "and",
            "are",
            "for",
            "from",
            "into",
            "its",
            "should",
            "that",
            "the",
            "this",
            "to",
            "use",
            "with",
        }

        def normalized_tokens(value: Any) -> set[str]:
            return {
                token
                for token in re.findall(r"[a-z0-9]+", str(value or "").casefold())
                if len(token) >= 3 and token not in stopwords
            }

        decision_parts: list[Any] = [
            decision.get("question"),
            decision.get("recommendation"),
        ]
        for option in decision.get("options") or []:
            if isinstance(option, dict):
                decision_parts.extend(option.values())
            else:
                decision_parts.append(option)
        decision_tokens = set().union(
            *(normalized_tokens(part) for part in decision_parts)
        )
        if not decision_tokens:
            return False
        for answer in answers:
            answer_tokens = normalized_tokens(answer)
            if len(answer_tokens) == 1:
                if answer_tokens <= decision_tokens:
                    return True
                continue
            if not answer_tokens:
                continue
            overlap = answer_tokens & decision_tokens
            if len(overlap) >= 2 and len(overlap) / len(answer_tokens) >= 0.75:
                return True
        return False

    def _file_decision(
        self,
        project: dict[str, Any],
        decision: dict[str, Any],
        *,
        plan_revision: str,
        criterion_id: str | None,
    ) -> int:
        unique_key = (
            f"project-autonomy-decision:{project['id']}:{plan_revision or 'unplanned'}:"
            f"{criterion_id or 'planning'}"
        )
        item_id, created = self.db.enqueue_work_item(
            kind="founder_decision",
            title=f"Decision needed: {str(decision['question'])[:70]}",
            detail=(
                f"{project['name']} is paused on a consequential documented MVP choice.\n\n"
                f"Question: {decision['question']}\n"
                f"Recommendation: {decision['recommendation']}\n\n"
                "Answer in Zade's Project Decisions panel. Telegram is notification-only."
            ),
            action="project.autonomy.resume",
            target=str(project["canonical_path"]),
            permission_tier="L3_EXTERNAL_ACTION",
            priority=90,
            source="project_autonomy",
            metadata={
                "project_id": project["id"],
                "project_autonomy": True,
                "project_autonomy_resume_only": True,
                "workspace": project["canonical_path"],
                "criterion_id": criterion_id,
                "founder_decision": True,
                "founder_question": decision,
                "brief": "Resume the autonomous project increment using the founder's UI answer.",
            },
            unique_key=unique_key,
        )
        if created:
            self.db.update_work_item(
                item_id,
                status="approval_required",
                authority_decision="approval_required",
            )
            self.db.ensure_approval_request(
                source_type="work_item",
                source_id=item_id,
                title=f"Decision needed: {str(decision['question'])[:70]}",
                detail=str(decision["question"]),
                action="project.autonomy.resume",
                target=str(project["canonical_path"]),
                permission_tier="L3_EXTERNAL_ACTION",
                authority_decision="approval_required",
                authority={
                    "decision": "approval_required",
                    "requires_typed_phrase": False,
                    "matched_rule": "project_autonomy.decision_answer_is_approval",
                },
                requested_by="project_autonomy",
                metadata={"project_autonomy": True, "project_id": project["id"]},
            )
        return item_id

    def _file_approval(
        self,
        project: dict[str, Any],
        criterion: dict[str, Any],
        dispatch: dict[str, Any],
        boundary: str,
    ) -> int:
        item_id, _created = self.db.enqueue_work_item(
            kind="project_autonomy_action",
            title=f"Approval required: {str(dispatch.get('proposed_action') or criterion['title'])[:70]}",
            detail=str(dispatch.get("reason") or f"Crosses {boundary}"),
            action="project.autonomy.external_action",
            target=str(project["canonical_path"]),
            permission_tier="L3_EXTERNAL_ACTION",
            priority=90,
            source="project_autonomy",
            metadata={
                "project_id": project["id"],
                "project_autonomy": True,
                "workspace": project["canonical_path"],
                "criterion_id": criterion["id"],
                "boundary": boundary,
            },
            unique_key=f"project-autonomy-approval:{project['id']}:{criterion['id']}:{boundary}",
        )
        self.db.update_work_item(
            item_id,
            status="approval_required",
            authority_decision="approval_required",
        )
        approval, _ = self.db.ensure_approval_request(
            source_type="work_item",
            source_id=item_id,
            title=f"Approval required: {dispatch.get('proposed_action') or criterion['title']}",
            detail=str(dispatch.get("reason") or f"Crosses {boundary}"),
            action="project.autonomy.external_action",
            target=str(project["canonical_path"]),
            permission_tier="L3_EXTERNAL_ACTION",
            authority_decision="approval_required",
            requested_by="project_autonomy",
            metadata={"project_autonomy": True, "project_id": project["id"], "boundary": boundary},
        )
        return approval.id

    def _build_brief(
        self,
        *,
        project: dict[str, Any],
        criterion: dict[str, Any],
        root: Path,
        failure_output: str,
        attempt: int,
    ) -> str:
        source = str(criterion.get("source") or "").split("#", 1)[0]
        source_path = (root / source).resolve()
        source_text = ""
        if source_path.is_file() and source_path.is_relative_to(root):
            source_text = source_path.read_text(encoding="utf-8-sig", errors="replace")[:20_000]
        decisions = [
            str(event.get("detail") or "")
            for event in self.db.list_project_events(project["id"], limit=100)
            if event.get("event_type")
            in {"decision_applied", "local_implementation_choice_applied"}
            and str(event.get("detail") or "").strip()
        ]
        head = _git(root, "rev-parse", "HEAD").stdout.strip()
        state = self.reporter.state(project["id"])
        return (
            "# Autonomous documented MVP increment\n\n"
            f"Project: {project['name']}\nRoot: {root}\nClean starting commit: {head}\n"
            f"Plan revision: {state.get('plan_revision')}\nAttempt: {attempt}\n\n"
            f"## Exact criterion\n{json.dumps(criterion, indent=2, sort_keys=True)}\n\n"
            f"## Cited founder document: {source}\n{source_text}\n\n"
            f"## Accepted founder answers\n{json.dumps(decisions, indent=2)}\n\n"
            f"## Recorded external boundaries\n{json.dumps(state.get('external_boundaries') or [])}\n\n"
            f"## Prior real verification failure\n{failure_output[:4000] or '(none)'}\n\n"
            "Implement only this criterion. Work inline in the registered repository. "
            "Preserve the existing project structure and dependency manifests. Never replace "
            "package.json, pubspec.yaml, or lockfiles wholesale. Prefer existing dependencies "
            "and platform APIs. If an accepted local choice requires a new package, add it "
            "through the ecosystem's package manager and preserve every unrelated manifest "
            "entry. Make the smallest coherent change that satisfies the criterion. "
            "Use local tools and the supplied verification commands. Do not publish, buy, "
            "create an external account, accept legal terms, or cross another recorded "
            "authority boundary. Ask the founder only for a genuinely consequential choice; "
            "otherwise choose the safest reversible local implementation and finish the work."
        )

    def _renew(self, claim: dict[str, Any]) -> None:
        renewed = self.store.renew(
            int(claim["project_id"]),
            owner=str(claim["owner"]),
            run_id=str(claim["run_id"]),
            lease_seconds=self.config.project_intake.autonomy_lease_seconds,
        )
        if renewed is None:
            raise RuntimeError("Project autonomy lease expired while work was in progress.")

    def _local_execution_problem(self) -> str:
        if str(self.config.delegation.engine) != "native":
            return "Project autonomy requires the inline native delegation engine; no fallback ran."
        host = (
            urllib.parse.urlparse(self.config.ollama.base_url).hostname or ""
        ).casefold()
        if host not in _LOOPBACK_HOSTS:
            return "Project autonomy requires a loopback Ollama endpoint; no remote fallback ran."
        if str(self.config.ollama.cloud_fallback or "never") != "never":
            return "Project autonomy requires cloud_fallback=never."
        if str(self.config.ollama.provider_policy) == "cloud_allowed":
            return "Project autonomy refuses cloud_allowed model inference."
        return ""


def _reversible_local_implementation_choice(dispatch: dict[str, Any]) -> str:
    """Select a safe local implementation detail instead of paging the founder."""
    if str(dispatch.get("status") or "").strip().casefold() != "needs_decision":
        return ""
    question = dispatch.get("founder_question")
    if not isinstance(question, dict):
        return ""
    raw_options = question.get("options") or []
    options = [
        str(item.get("option") if isinstance(item, dict) else item).strip()
        for item in raw_options
    ]
    options = [item for item in options if item]
    recommendation = str(question.get("recommendation") or "").strip()
    combined = " ".join(
        [str(question.get("question") or ""), recommendation, *options]
    ).casefold()
    local_markers = (
        "package",
        "dependency",
        "local database",
        "local storage",
        "on-device",
        "sharedpreferences",
        "sqflite",
        "library",
    )
    consequential_markers = (
        "paid",
        "purchase",
        "subscription",
        "cloud",
        "external account",
        "email account",
        "publish",
        "deploy",
        "app store",
        "legal",
        "privacy policy",
        "data retention",
        "scope expansion",
    )
    if not any(marker in combined for marker in local_markers):
        return ""
    if any(marker in combined for marker in consequential_markers):
        return ""
    return recommendation or (options[0] if options else "")


def _is_runnable(state: dict[str, Any]) -> bool:
    if (
        state.get("paused") is True
        or str(state.get("phase")) in _TERMINAL_PHASES
    ):
        return False
    # A crash can occur after the final criterion transition but before the
    # project-level completion attestation. Keep that state runnable so
    # recovery can perform the fresh final check and close the MVP gate.
    return True


def _next_ready_criterion(state: dict[str, Any]) -> dict[str, Any] | None:
    criteria = state.get("mvp_criteria") or []
    completed = {item.get("id") for item in criteria if item.get("status") == "complete"}
    current = str(state.get("current_criterion_id") or "")
    if str(state.get("phase")) in {"building", "verifying"} and current:
        for item in criteria:
            if item.get("id") == current and item.get("status") != "complete":
                return item
    for item in criteria:
        if item.get("status") == "complete":
            continue
        dependencies = {str(value) for value in item.get("depends_on") or []}
        if dependencies <= completed:
            return item
    return None


def _run_reference(project_id: int, revision: Any, criterion_id: str, increment: int) -> int:
    raw = f"{project_id}:{revision}:{criterion_id}:{increment}".encode("utf-8")
    return int(hashlib.sha256(raw).hexdigest()[:12], 16) % 2_000_000_000 + 1


def _dispatch_problem(dispatch: dict[str, Any]) -> str:
    if not dispatch:
        return "Delegation returned no dispatch result."
    if str(dispatch.get("status") or "").lower() not in {"ok", "completed"}:
        return str(dispatch.get("error") or dispatch.get("status") or "delegation failed")[:2000]
    provider = dispatch.get("provider")
    if isinstance(provider, dict) and provider.get("verified_local") is not True:
        return "Delegation provider was not verified local; result rejected without fallback."
    verification = dispatch.get("auto_verification")
    if not isinstance(verification, dict) or verification.get("ok") is not True:
        return str(
            (verification or {}).get("output")
            if isinstance(verification, dict)
            else "Delegation returned no mechanical verification."
        )[:2000]
    review = dispatch.get("verifier_review")
    if isinstance(review, dict) and str(review.get("verdict") or "").casefold() == "fail":
        return f"Fresh-context verifier rejected the increment: {review.get('notes') or 'no notes'}"[:2000]
    return ""


def _quarantine_and_restore_attempt(
    root: Path,
    *,
    expected_head: str,
    quarantine_root: Path,
) -> Path | None:
    """Preserve a failed attempt outside the repo, then restore clean HEAD.

    Autonomy enters an attempt only from a clean Git checkpoint. Any tracked
    diff or untracked file after a rejected dispatch therefore belongs to that
    attempt. The evidence is copied to the kernel data directory before the
    checkout is restored, so rollback is recoverable and never silently loses
    a concurrent founder file.
    """
    status = _git_checked(root, "status", "--porcelain").stdout.strip()
    if not status:
        return None
    current_head = _git_checked(root, "rev-parse", "HEAD").stdout.strip()
    if current_head != expected_head:
        raise ValueError(
            "Autonomous attempt changed repository HEAD; refusing automatic rollback."
        )
    resolved_root = root.resolve()
    resolved_quarantine_root = quarantine_root.resolve()
    if resolved_quarantine_root == resolved_root or resolved_quarantine_root.is_relative_to(
        resolved_root
    ):
        raise ValueError("Failed-attempt quarantine must be outside the project repository.")

    quarantine = resolved_quarantine_root / f"{time.time_ns()}-{uuid.uuid4().hex[:8]}"
    quarantine.mkdir(parents=True, exist_ok=False)
    (quarantine / "metadata.json").write_text(
        json.dumps(
            {"project_path": str(resolved_root), "head": expected_head, "status": status},
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    patch = _git_checked(root, "diff", "--binary", "HEAD", "--").stdout
    if patch:
        (quarantine / "tracked.patch").write_text(patch, encoding="utf-8")

    untracked_result = _git_checked(
        root, "ls-files", "--others", "--exclude-standard", "-z"
    ).stdout
    untracked: list[Path] = []
    for relative_text in (item for item in untracked_result.split("\0") if item):
        source = (resolved_root / relative_text).resolve()
        if not source.is_relative_to(resolved_root) or not source.is_file():
            continue
        relative = source.relative_to(resolved_root)
        destination = quarantine / "untracked" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        untracked.append(source)

    _git_checked(root, "restore", "--source=HEAD", "--staged", "--worktree", "--", ".")
    for source in untracked:
        if source.is_file():
            source.unlink()
        parent = source.parent
        while parent != resolved_root:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    remaining = _git_checked(root, "status", "--porcelain").stdout.strip()
    if remaining:
        raise ValueError(
            "Failed autonomous attempt could not be restored to a clean checkpoint: "
            + remaining[:800]
        )
    return quarantine


def _ensure_clean_repository(root: Path, *, allow_dirty: bool = False) -> None:
    inside = _git(root, "rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0:
        initialized = _git(root, "init", "--initial-branch=main")
        if initialized.returncode != 0:
            raise ValueError(f"git init failed: {initialized.stderr or initialized.stdout}")
    head = _git(root, "rev-parse", "HEAD")
    if head.returncode != 0:
        non_docs = [
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file()
            and ".git" not in path.parts
            and path.suffix.casefold() not in {".md", ".markdown", ".txt", ".gitignore"}
        ]
        if non_docs:
            raise ValueError(
                "Repository has no baseline commit and contains implementation files: "
                + ", ".join(non_docs[:10])
            )
        _git_checked(root, "add", "-A")
        _git_checked(
            root,
            "-c",
            "user.name=Zade",
            "-c",
            "user.email=zade@local",
            "commit",
            "-m",
            "chore: establish approved project intake baseline",
        )
    status = _git_checked(root, "status", "--porcelain").stdout.strip()
    if status and not allow_dirty:
        raise ValueError(
            "Repository has uncommitted changes before the autonomy increment: "
            + status[:800]
        )


def _commit_increment(root: Path, criterion: dict[str, Any]) -> str:
    status = _git_checked(root, "status", "--porcelain").stdout.strip()
    if status:
        _git_checked(root, "add", "-A")
        title = re.sub(r"\s+", " ", str(criterion.get("title") or criterion["id"])).strip()
        _git_checked(
            root,
            "-c",
            "user.name=Zade",
            "-c",
            "user.email=zade@local",
            "commit",
            "-m",
            f"feat: {title[:68]}",
        )
    return _git_checked(root, "rev-parse", "HEAD").stdout.strip()


def _post_commit_verification(
    root: Path, dispatch: dict[str, Any], expected_head: str
) -> dict[str, Any]:
    verification = dispatch.get("auto_verification")
    checks = verification.get("checks") if isinstance(verification, dict) else None
    if not isinstance(checks, list) or not checks:
        raise ValueError("Post-commit verification has no audited command to rerun.")
    results = [_run_check(root, item.get("argv")) for item in checks if isinstance(item, dict)]
    if not results or not all(item["ok"] for item in results):
        output = "\n\n".join(str(item.get("output") or "") for item in results)
        raise ValueError(f"Post-commit verification failed: {output[:1600]}")
    head = _git_checked(root, "rev-parse", "HEAD").stdout.strip()
    status = _git_checked(root, "status", "--porcelain").stdout.strip()
    if head != expected_head:
        raise ValueError("Repository HEAD changed during post-commit verification.")
    if status:
        raise ValueError(f"Post-commit verification dirtied the repository: {status[:800]}")
    return {
        "ok": True,
        "checked_at": utc_now(),
        "project_path": str(root),
        "repo_head": head,
        "repo_status": status,
        "checks": results,
    }


def _run_declared_verification(root: Path, commands: list[Any]) -> dict[str, Any]:
    import shlex

    results: list[dict[str, Any]] = []
    for command in commands:
        argv = shlex.split(str(command), posix=os.name != "nt")
        results.append(_run_check(root, argv))
    if not results or not all(item["ok"] for item in results):
        raise ValueError("Final declared MVP verification did not pass.")
    head = _git_checked(root, "rev-parse", "HEAD").stdout.strip()
    status = _git_checked(root, "status", "--porcelain").stdout.strip()
    return {
        "ok": True,
        "checked_at": utc_now(),
        "project_path": str(root),
        "repo_head": head,
        "repo_status": status,
        "checks": results,
    }


def _run_check(root: Path, raw_argv: Any) -> dict[str, Any]:
    if not isinstance(raw_argv, list) or not raw_argv or not all(str(item).strip() for item in raw_argv):
        raise ValueError("Verification command is missing an audited argv list.")
    argv = _resolve_check_argv([str(item) for item in raw_argv])
    executable = Path(argv[0]).name.casefold()
    if executable not in _SAFE_VERIFY_EXECUTABLES:
        raise ValueError(f"Post-commit verification executable is not allowlisted: {argv[0]}")
    if any(any(token in arg for token in ("&&", "||", ";", "`", "$(")) for arg in argv):
        raise ValueError("Shell operators are forbidden in post-commit verification argv.")
    try:
        result = subprocess.run(
            argv,
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=900,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"Verification command failed to run: {type(exc).__name__}: {exc}") from exc
    output = (result.stdout + result.stderr).strip()
    if not output:
        output = f"[command produced no output; exit code {result.returncode}]"
    return {
        "argv": argv,
        "ok": result.returncode == 0,
        "returncode": int(result.returncode),
        "output": output[:20_000],
    }


def _resolve_check_argv(argv: list[str]) -> list[str]:
    """Resolve Windows command shims before direct subprocess execution.

    The audited coding-agent checks use portable command names such as `npm`,
    but Windows exposes the executable as npm.cmd. PowerShell resolves that
    shim automatically; subprocess does not.
    """
    if os.name != "nt" or not argv:
        return argv
    resolved = shutil.which(argv[0])
    return [resolved, *argv[1:]] if resolved else argv


def _decision_options(question: dict[str, Any]) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    for raw in list(question.get("options") or [])[:3]:
        if isinstance(raw, dict):
            option = str(raw.get("option") or raw.get("label") or "").strip()
            impact = str(raw.get("impact") or raw.get("description") or "").strip()
        else:
            option = str(raw or "").strip()
            impact = "Uses this choice for the documented local MVP."
        if option:
            options.append({"option": option, "impact": impact or "Uses this MVP choice."})
    if len(options) < 2:
        options = [
            {
                "option": str(question.get("recommendation") or "Use Zade's local-first recommendation"),
                "impact": "Continues with the safest reversible documented option.",
            },
            {
                "option": "Pause and revise the written MVP",
                "impact": "Keeps implementation paused until the specification is explicit.",
            },
        ]
    return options


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _protected_manifest_rewrite_problem(root: Path, expected_head: str) -> str:
    changed = _git_checked(root, "diff", "--name-only", expected_head, "--").stdout.splitlines()
    for relative in changed:
        normalized = relative.strip().replace("\\", "/")
        if not normalized or Path(normalized).name.casefold() not in _PROTECTED_DEPENDENCY_MANIFESTS:
            continue
        baseline = _git(root, "show", f"{expected_head}:{normalized}")
        if baseline.returncode != 0:
            # A newly created manifest has no committed baseline to preserve.
            continue
        target = (root / normalized).resolve()
        if not target.is_relative_to(root.resolve()) or not target.is_file():
            return f"{normalized} was deleted"
        baseline_lines = [
            line
            for line in baseline.stdout.splitlines()
            if line.strip() and not line.lstrip().startswith(("#", "//"))
        ]
        candidate_lines = {
            line
            for line in target.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip() and not line.lstrip().startswith(("#", "//"))
        }
        missing = [line for line in baseline_lines if line not in candidate_lines]
        allowed_missing = max(3, (len(baseline_lines) + 3) // 4)
        if len(missing) > allowed_missing:
            return (
                f"{normalized} removed {len(missing)} of {len(baseline_lines)} "
                "existing nonblank lines"
            )
    return ""


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(["git", *args], 1, "", str(exc))


def _git_checked(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = _git(root, *args)
    if result.returncode != 0:
        raise ValueError(
            f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()[:1200]}"
        )
    return result
