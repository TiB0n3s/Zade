"""Durable, local-first execution loop for documented project MVP criteria."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

from .config import KernelConfig
from .db import KernelDatabase, utc_now
from .project_autonomy import APPROVAL_BOUNDARIES, ProjectAutonomyReporter
from .project_mvp_planner import MvpPlanResult, ProjectMvpPlanner


_PRIORITY_ORDER = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
_TERMINAL_PHASES = {"mvp_complete", "needs_decision", "approval_required", "blocked"}
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
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
                    result = {"status": "worker_error", "error": f"{type(exc).__name__}: {exc}"[:400]}
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

        try:
            _ensure_clean_repository(
                root,
                allow_dirty=str(state.get("phase") or "") in {"building", "verifying"},
            )
        except ValueError as exc:
            self.reporter.report_blocked(
                project_id,
                reason=str(exc),
                needed="restore a clean, reviewable Git baseline",
            )
            return {"status": "blocked", "project_id": project_id, "reason": str(exc)}

        if not state.get("mvp_criteria"):
            planning_project = dict(project)
            planning_metadata = dict(project.get("metadata") or {})
            founder_answers = [
                str(event.get("detail") or "")
                for event in reversed(self.db.list_project_events(project_id, limit=None))
                if event.get("event_type") == "decision_applied"
                and str(event.get("detail") or "").strip()
            ]
            planning_metadata["planner_founder_answers"] = founder_answers
            planning_project["metadata"] = planning_metadata
            try:
                planned = self.planner.plan(planning_project)
            except ValueError as exc:
                reason = f"Local MVP planner returned an invalid plan: {exc}"
                self.reporter.report_blocked(
                    project_id,
                    reason=reason,
                    verification_output=str(exc),
                    attempts=1,
                    needed="correct the documented MVP plan and wake project autonomy",
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
                        reason = f"Local MVP planner returned an invalid corrected plan: {exc}"
                        self.reporter.report_blocked(
                            project_id, reason=reason, verification_output=str(exc), attempts=2,
                            needed="correct the documented MVP plan and wake project autonomy",
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
                            needed="repair the local MVP planner response and wake project autonomy",
                        )
                        return {"status": "blocked", "project_id": project_id, "reason": reason}
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
        for attempt in range(attempts):
            self._renew(claim)
            current = self.reporter.state(project_id)
            unique_key = (
                f"project-autonomy:{project_id}:{current.get('plan_revision') or 'unplanned'}:"
                f"{criterion['id']}:{increment}:{attempt}"
            )
            result = self.delegation.queue_delegation(
                task=f"Complete documented MVP criterion {criterion['id']}: {criterion['title']}",
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
            boundary = self._classify_boundary(project, criterion, dispatch)
            if boundary is not None:
                return boundary
            if self.reporter.state(project_id).get("paused") is True:
                return {
                    "status": "paused",
                    "project_id": project_id,
                    "criterion_id": criterion["id"],
                }
            problem = _dispatch_problem(dispatch)
            if problem:
                failure_output = problem
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
        self.reporter.report_blocked(
            project_id,
            reason=reason[:400],
            criterion_id=criterion["id"],
            verification_output=reason,
            attempts=attempts,
            needed="repair the local verification failure or clarify the documented criterion",
        )
        return {
            "status": "blocked",
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
            if event.get("event_type") == "decision_applied" and str(event.get("detail") or "").strip()
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


def _is_runnable(state: dict[str, Any]) -> bool:
    if (
        state.get("mvp_complete") is True
        or state.get("paused") is True
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
    argv = [str(item) for item in raw_argv]
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
