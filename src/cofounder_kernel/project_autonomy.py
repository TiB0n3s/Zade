"""Durable truth + notification contract for project autonomy.

The later autonomous orchestrator records progress ONLY through
ProjectAutonomyReporter. The reporter persists every transition in the
append-only project_events ledger, keeps the current autonomy state in the
project's metadata, and routes founder-facing notifications through the one
existing NotificationBus. It never runs builds itself.

Evidence discipline: a criterion or MVP is complete only on mechanical
evidence (fresh verification results with real check records, a recorded
commit, and — for the MVP gate — a live clean-repo check performed here).
Model prose is never accepted as proof.
"""

from __future__ import annotations

import copy
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .db import KernelDatabase, utc_now
from .project_autonomy_store import ProjectAutonomyStore

PHASES = (
    "planning",
    "building",
    "verifying",
    "ready_for_next_increment",
    "needs_decision",
    "approval_required",
    "blocked",
    "mvp_complete",
)

ALLOWED_TRANSITIONS = {
    "planning": {"building", "needs_decision", "blocked"},
    "building": {"verifying", "needs_decision", "approval_required", "blocked"},
    "verifying": {
        "ready_for_next_increment",
        "needs_decision",
        "approval_required",
        "blocked",
    },
    "ready_for_next_increment": {"building", "mvp_complete", "needs_decision", "blocked"},
    "needs_decision": {"building", "blocked"},
    "approval_required": {"building", "blocked"},
    "blocked": {"planning", "building"},
    "mvp_complete": set(),
}

PRIORITIES = ("low", "normal", "high", "urgent")

# Authority boundaries whose crossing always requires founder approval.
# Reversible, local, low-risk implementation choices are NOT on this list:
# Zade picks a documented default and records it instead of interrupting.
APPROVAL_BOUNDARIES = (
    "credentials",
    "paid_services",
    "publishing_deployment",
    "app_store_submission",
    "legal_acceptance",
    "external_account_creation",
    "irreversible_external_commitment",
    "mvp_scope_expansion",
)

BLOCKED_SEVERITIES = ("warning", "critical")

# A "fresh" verification must have run recently enough that the repository it
# describes is still the repository on disk.
VERIFICATION_MAX_AGE_MINUTES = 60

AUTONOMY_PROJECTION_KEYS = (
    "phase",
    "priority",
    "mvp_criteria_total",
    "mvp_criteria_completed",
    "current_criterion_id",
    "current_increment",
    "last_verified_at",
    "next_action",
    "blocking_type",
    "blocking_reason",
    "decision_id",
    "approval_request_id",
    "active_run_id",
    "last_notification_id",
    "repo_head",
    "mvp_complete",
    "paused",
    "pause_reason",
)


class ProjectAutonomyReporter:
    """Record and expose autonomous project progress; notify only at real boundaries.

    needs_decision  = Zade cannot safely choose between consequential product or
                      architecture options from the approved documentation.
    approval_required = the next action crosses an authority boundary
                      (APPROVAL_BOUNDARIES).
    blocked         = verification repeatedly failed, requirements conflict, or
                      tooling is unavailable with no safe alternative. A
                      recoverable product choice is NEVER reported here — that
                      is report_needs_decision.
    Routine increment completions are ledger-only: recorded in project_events
    and the API, never a Telegram message.
    """

    def __init__(
        self,
        *,
        db: KernelDatabase,
        bus: Any | None = None,
        store: ProjectAutonomyStore | None = None,
    ):
        self.db = db
        self.bus = bus
        self.store = store or ProjectAutonomyStore(db)

    # ---- reads --------------------------------------------------------------

    def get_project(self, project_id: int) -> dict[str, Any]:
        project = self.db.get_project(project_id)
        if project is None:
            raise ValueError(f"Project not found: {project_id}")
        return self._attach_state(project, self._state_for_project(project))

    def state(self, project_id: int) -> dict[str, Any]:
        project = self.get_project(project_id)
        return _autonomy_state(project)

    def project_view(self, project_id: int) -> dict[str, Any]:
        raw_project = self.db.get_project(project_id)
        if raw_project is None:
            raise ValueError(f"Project not found: {project_id}")
        stored = self.store.get(project_id)
        legacy = (raw_project.get("metadata") or {}).get("autonomy")
        if int(stored.get("version") or 0) > 0 or isinstance(legacy, dict):
            project = self._attach_state(
                raw_project,
                self._state_from_store(stored)
                if int(stored.get("version") or 0) > 0
                else _autonomy_state(raw_project),
            )
        else:
            # An intake/scaffold record has no autonomous execution state yet.
            # Preserve that absence so the projection can derive the honest
            # lifecycle fallback (notably verified scaffold != planning MVP).
            project = raw_project
        project = _sanitize_project_for_api(project)
        projection = autonomy_projection(project)
        return {
            **project,
            "status": portfolio_bucket(project, projection),
            "autonomy": projection,
        }

    def list_views(
        self, *, lifecycle_state: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        projects = self.db.list_projects(
            lifecycle_state=lifecycle_state,
            limit=min(max(int(limit), 1), 5000),
        )
        return [self.project_view(int(project["id"])) for project in projects]

    def portfolio(self) -> dict[str, Any]:
        return portfolio_status(self.list_views(limit=500))

    # ---- planning and increments (ledger-only, no founder notifications) ----

    def plan(
        self,
        project_id: int,
        *,
        criteria: list[dict[str, Any]],
        priority: str | None = None,
        next_action: str = "",
        external_boundaries: list[str] | None = None,
        plan_revision: str | None = None,
    ) -> dict[str, Any]:
        project = self.get_project(project_id)
        if not criteria:
            raise ValueError("An MVP plan requires at least one documented criterion.")
        prior = self._state_for_project(project)
        prior_by_id = {
            str(item.get("id")): item
            for item in prior.get("mvp_criteria") or []
            if isinstance(item, dict) and item.get("id")
        }
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in criteria:
            if not isinstance(entry, dict):
                raise ValueError("Each MVP criterion must be a mapping with id and title.")
            criterion_id = str(entry.get("id") or "").strip()
            title = str(entry.get("title") or "").strip()
            if not criterion_id or not title:
                raise ValueError("Each MVP criterion requires a non-empty id and title.")
            if criterion_id in seen:
                raise ValueError(f"Duplicate MVP criterion id: {criterion_id}")
            seen.add(criterion_id)
            documented = {
                key: copy.deepcopy(value)
                for key, value in entry.items()
                if key not in {"status", "verified_at", "commit", "verification", "blocked_reason"}
            }
            documented.update(
                {
                    "id": criterion_id,
                    "title": title,
                    "required": bool(entry.get("required", True)),
                }
            )
            existing = prior_by_id.get(criterion_id)
            if existing is None:
                documented["status"] = "pending"
                normalized.append(documented)
            else:
                reconciled = copy.deepcopy(existing)
                reconciled.update(documented)
                normalized.append(reconciled)
        if priority is not None and priority not in PRIORITIES:
            raise ValueError(f"Priority must be one of: {', '.join(PRIORITIES)}")
        if prior.get("mvp_complete") and normalized == prior.get("mvp_criteria"):
            return project
        state = copy.deepcopy(prior if prior.get("mvp_criteria") else _default_state())
        if prior.get("mvp_complete"):
            state = _default_state()
        state.update(
            {
                "priority": priority or prior.get("priority") or "normal",
                "mvp_criteria": normalized,
                "next_action": next_action or str(prior.get("next_action") or ""),
                "external_boundaries": (
                    [str(item) for item in external_boundaries]
                    if external_boundaries is not None
                    else list(prior.get("external_boundaries") or [])
                ),
                "plan_revision": (
                    str(plan_revision).strip()
                    if plan_revision is not None
                    else str(prior.get("plan_revision") or "")
                ),
            }
        )
        return self._transition(
            project,
            state,
            event={
                "event_type": "autonomy_planned",
                "detail": next_action,
                "metadata": {
                    "phase": state["phase"],
                    "criteria": [item["id"] for item in normalized],
                    "plan_revision": state.get("plan_revision") or "",
                },
            },
        )

    def set_priority(self, project_id: int, priority: str) -> dict[str, Any]:
        if priority not in PRIORITIES:
            raise ValueError(f"Priority must be one of: {', '.join(PRIORITIES)}")
        project = self.get_project(project_id)
        state = self._state_for_project(project)
        state["priority"] = priority
        return self._transition(
            project,
            state,
            event={"event_type": "priority_changed", "metadata": {"priority": priority}},
        )

    def pause(self, project_id: int, *, reason: str = "founder paused autonomy") -> dict[str, Any]:
        project = self.get_project(project_id)
        state = self._state_for_project(project)
        if state.get("mvp_complete"):
            raise ValueError("A completed MVP cannot be paused.")
        if state.get("paused") is True:
            return project
        state.update(
            {
                "paused": True,
                "pause_reason": str(reason or "founder paused autonomy").strip()[:400],
                "active_run_id": None,
                "next_action": "paused by founder in Zade",
            }
        )
        return self._transition(
            project,
            state,
            event={
                "event_type": "autonomy_paused",
                "detail": state["pause_reason"],
                "metadata": {"phase": state.get("phase")},
            },
        )

    def resume(self, project_id: int) -> dict[str, Any]:
        project = self.get_project(project_id)
        state = self._state_for_project(project)
        if state.get("phase") == "blocked" and not state.get("mvp_criteria"):
            state.update({
                "phase": "planning", "blocking_type": None, "blocking_reason": None,
                "active_run_id": None, "next_action": "re-plan the documented MVP from recorded founder answers",
            })
            return self._transition(project, state, event={"event_type": "autonomy_resumed", "metadata": {"phase": "planning"}})
        if state.get("paused") is not True:
            return project
        state.update(
            {
                "paused": False,
                "pause_reason": "",
                "next_action": (
                    "resume the current documented MVP increment"
                    if state.get("phase") in {"building", "verifying"}
                    else "build the next dependency-ready documented MVP criterion"
                ),
            }
        )
        return self._transition(
            project,
            state,
            event={
                "event_type": "autonomy_resumed",
                "metadata": {"phase": state.get("phase")},
            },
        )

    def begin_increment(
        self,
        project_id: int,
        *,
        criterion_id: str,
        increment: int | None = None,
        run_id: int | None = None,
        next_action: str = "",
    ) -> dict[str, Any]:
        project = self.get_project(project_id)
        state = self._mutable_state(project)
        if state.get("phase") in {"needs_decision", "approval_required"}:
            raise ValueError(
                f"Cannot begin increment while project phase is {state.get('phase')}; "
                "resolve the canonical UI boundary first."
            )
        self._require_transition(state, "building", operation="begin increment")
        criterion = _find_criterion(state, criterion_id)
        if criterion["status"] == "complete":
            raise ValueError(f"MVP criterion already complete: {criterion_id}")
        state.update(
            {
                "phase": "building",
                "current_criterion_id": criterion_id,
                "current_increment": (
                    int(increment) if increment is not None else int(state.get("current_increment") or 0) + 1
                ),
                "active_run_id": _positive_int(run_id),
                "next_action": next_action or f"build increment for {criterion['title']}",
                "blocking_type": None,
                "blocking_reason": None,
            }
        )
        return self._transition(
            project,
            state,
            event={
                "event_type": "increment_started",
                "detail": criterion["title"],
                "metadata": {
                    "phase": "building",
                    "criterion_id": criterion_id,
                    "increment": state["current_increment"],
                    "run_id": state["active_run_id"],
                },
            },
        )

    def begin_verification(self, project_id: int, *, run_id: int | None = None) -> dict[str, Any]:
        project = self.get_project(project_id)
        state = self._mutable_state(project)
        self._require_transition(state, "verifying", operation="begin verification")
        state["phase"] = "verifying"
        if run_id is not None:
            state["active_run_id"] = _positive_int(run_id)
        return self._transition(
            project,
            state,
            event={
                "event_type": "verification_started",
                "metadata": {
                    "phase": "verifying",
                    "criterion_id": state.get("current_criterion_id"),
                },
            },
        )

    def bind_run(self, project_id: int, *, run_id: int) -> dict[str, Any]:
        """Attach a recovered/resumed building phase to its current local run."""
        project = self.get_project(project_id)
        state = self._mutable_state(project)
        if state.get("phase") not in {"building", "verifying"}:
            raise ValueError(
                f"Cannot bind a run while project phase is {state.get('phase')}."
            )
        bound = _positive_int(run_id)
        if bound is None:
            raise ValueError("A resumed autonomy run requires a positive run_id.")
        if state.get("active_run_id") == bound:
            return project
        state["active_run_id"] = bound
        return self._transition(
            project,
            state,
            event={
                "event_type": "increment_run_bound",
                "metadata": {
                    "phase": state.get("phase"),
                    "criterion_id": state.get("current_criterion_id"),
                    "run_id": bound,
                },
            },
        )

    def record_increment(
        self,
        project_id: int,
        *,
        summary: str = "",
        verification: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Routine increment completion: ledger + API only, never Telegram."""
        project = self.get_project(project_id)
        state = self._mutable_state(project)
        if state.get("phase") != "verifying":
            raise ValueError(
                f"Cannot record increment while phase is {state.get('phase')}; verifying is required."
            )
        snapshot = _validate_project_verification(
            verification,
            label="increment verification",
            project=project,
        )
        self._require_transition(state, "ready_for_next_increment", operation="record increment")
        state["phase"] = "ready_for_next_increment"
        state["active_run_id"] = None
        state["last_verified_at"] = str(verification.get("checked_at"))
        state["repo_head"] = snapshot["head"]
        return self._transition(
            project,
            state,
            event={
                "event_type": "increment_completed",
                "detail": summary[:400],
                "metadata": {
                    "phase": "ready_for_next_increment",
                    "criterion_id": state.get("current_criterion_id"),
                    "increment": state.get("current_increment"),
                    "verified": True,
                    "repo_head": snapshot["head"],
                    "verification": verification,
                },
            },
        )

    def complete_criterion(
        self,
        project_id: int,
        criterion_id: str,
        *,
        verification: dict[str, Any],
        commit: str,
    ) -> dict[str, Any]:
        """Mark one MVP criterion complete — only on mechanical evidence.

        Requires: implementation recorded at a commit, a fresh verification
        result whose checks all passed, and the evidence itself, which is
        persisted with the criterion. Scaffold-level 'verified' never reaches
        here; this is per-criterion completion inside the MVP plan.
        """
        project = self.get_project(project_id)
        state = self._mutable_state(project)
        criterion = _find_criterion(state, criterion_id)
        head = str(commit or "").strip()
        if not head:
            raise ValueError(
                f"Criterion {criterion_id} cannot complete without the verified repository commit."
            )
        if criterion.get("status") == "complete":
            if criterion.get("commit") == head and (
                (criterion.get("verification") or {}).get("repo_head") == head
            ):
                return project
            raise ValueError(
                f"Criterion {criterion_id} is already complete at a different commit."
            )
        if state.get("phase") != "verifying":
            raise ValueError(
                f"Cannot complete criterion while phase is {state.get('phase')}; verifying is required."
            )
        snapshot = _validate_project_verification(
            verification,
            label=f"criterion {criterion_id}",
            project=project,
            expected_commit=head,
        )
        self._require_transition(
            state, "ready_for_next_increment", operation="complete criterion"
        )
        evidence_summary = _verification_summary(verification)
        criterion.update(
            {
                "status": "complete",
                "verified_at": str(verification.get("checked_at")),
                "commit": head,
                "verification": evidence_summary,
            }
        )
        criterion.pop("blocked_reason", None)
        state.update(
            {
                "phase": "ready_for_next_increment",
                "last_verified_at": str(verification.get("checked_at")),
                "repo_head": head,
                "active_run_id": None,
                "blocking_type": None,
                "blocking_reason": None,
                "next_action": _next_pending_action(state),
            }
        )
        return self._transition(
            project,
            state,
            event={
                "event_type": "criterion_completed",
                "detail": criterion["title"],
                "metadata": {
                    "phase": "ready_for_next_increment",
                    "criterion_id": criterion_id,
                    "commit": snapshot["head"],
                    "checks": len(verification.get("checks") or []),
                    "verification": verification,
                },
            },
        )

    # ---- founder boundaries (notify through the existing bus) ---------------

    def report_needs_decision(
        self,
        project_id: int,
        *,
        decision_id: int,
        question: str,
        recommendation: str,
        options: list[dict[str, Any]],
    ) -> dict[str, Any]:
        project = self.get_project(project_id)
        state = self._mutable_state(project)
        decision = _positive_int(decision_id)
        if decision is None:
            raise ValueError("A needs_decision report requires a positive decision_id.")
        question = str(question or "").strip()
        recommendation = str(recommendation or "").strip()
        if not question or not recommendation:
            raise ValueError("A needs_decision report requires the exact question and a recommendation.")
        cleaned = _clean_options(options)
        self._require_transition(state, "needs_decision", operation="request decision")
        state.update(
            {
                "phase": "needs_decision",
                "blocking_type": "decision",
                "blocking_reason": question,
                "decision_id": decision,
                "active_run_id": None,
                "next_action": f"waiting for founder decision {decision}",
            }
        )
        options_block = "\n".join(
            f"{index}. {item['option']} — impact: {item['impact']}"
            for index, item in enumerate(cleaned, start=1)
        )
        outbox = {
            "topic": "project.decision_required",
            "title": f"{project['name']} needs a decision",
            "body": (
                f"Question: {question}\n"
                f"Recommendation: {recommendation}\n\n"
                f"Options:\n{options_block}\n\n"
                "Open Zade's Approvals & Actions screen to answer. "
                "Telegram is notification-only for project decisions."
            ),
            "severity": "warning",
            "dedupe_key": f"project:{project['id']}:decision:{decision}",
        }
        updated = self._transition(
            project,
            state,
            event={
                "event_type": "decision_requested",
                "detail": question,
                "work_item_id": decision if self.db.get_work_item(decision) else None,
                "metadata": {
                    "phase": "needs_decision",
                    "decision_id": decision,
                },
            },
            outbox=outbox,
        )
        self.deliver_due_notifications()
        return self.get_project(updated["id"])

    def report_approval_required(
        self,
        project_id: int,
        *,
        approval_request_id: int,
        action: str,
        reason: str,
        boundary: str,
        approve_hint: str = "",
    ) -> dict[str, Any]:
        project = self.get_project(project_id)
        state = self._mutable_state(project)
        approval = _positive_int(approval_request_id)
        if approval is None:
            raise ValueError("An approval_required report requires a positive approval_request_id.")
        action = str(action or "").strip()
        reason = str(reason or "").strip()
        if not action or not reason:
            raise ValueError("An approval_required report requires the proposed action and the reason.")
        if boundary not in APPROVAL_BOUNDARIES:
            raise ValueError(
                f"Authority boundary must be one of: {', '.join(APPROVAL_BOUNDARIES)}"
            )
        self._require_transition(state, "approval_required", operation="request approval")
        state.update(
            {
                "phase": "approval_required",
                "blocking_type": "approval",
                "blocking_reason": reason,
                "approval_request_id": approval,
                "active_run_id": None,
                "next_action": f"waiting for founder approval {approval}",
            }
        )
        outbox = {
            "topic": "project.approval_required",
            "title": f"{project['name']} needs founder approval",
            "body": (
                f"Proposed action: {action}\n"
                f"Why approval is required: {reason}\n"
                f"Authority boundary: {boundary}\n"
                f"Approval request: {approval}\n\n"
                "Open Zade's Approvals & Actions screen to approve or deny this request. "
                "Telegram is notification-only for approvals."
            ),
            "severity": "warning",
            "dedupe_key": f"project:{project['id']}:approval:{approval}",
        }
        updated = self._transition(
            project,
            state,
            event={
                "event_type": "approval_requested",
                "detail": action,
                "approval_request_id": (
                    approval if self.db.get_approval_request(approval) else None
                ),
                "metadata": {
                    "phase": "approval_required",
                    "boundary": boundary,
                    "approval_request_id": approval,
                },
            },
            outbox=outbox,
        )
        self.deliver_due_notifications()
        return self.get_project(updated["id"])

    def report_blocked(
        self,
        project_id: int,
        *,
        reason: str,
        criterion_id: str | None = None,
        verification_output: str = "",
        attempts: int = 0,
        needed: str = "",
        severity: str = "warning",
    ) -> dict[str, Any]:
        """Hard block: verification repeatedly failed, requirements conflict, or
        tooling is unavailable. A recoverable product choice must go through
        report_needs_decision instead — never through this generic failure path.
        """
        project = self.get_project(project_id)
        state = self._mutable_state(project)
        reason = str(reason or "").strip()
        if not reason:
            raise ValueError("A blocked report requires the concrete failure reason.")
        if severity not in BLOCKED_SEVERITIES:
            raise ValueError(f"Blocked severity must be one of: {', '.join(BLOCKED_SEVERITIES)}")
        self._require_transition(state, "blocked", operation="report blocked")
        criterion_title = "project-level"
        if criterion_id:
            criterion = _find_criterion(state, criterion_id)
            criterion["status"] = "blocked"
            criterion["blocked_reason"] = reason
            criterion_title = criterion["title"]
        state.update(
            {
                "phase": "blocked",
                "blocking_type": "error",
                "blocking_reason": reason,
                "active_run_id": None,
                "next_action": needed or "founder or tooling intervention required",
            }
        )
        outbox = {
            "topic": "project.build_blocked",
            "title": f"{project['name']} build is blocked",
            "body": (
                f"Failed criterion: {criterion_title}\n"
                f"Reason: {reason}\n"
                f"Verification output: {verification_output.strip()[:600] or 'none captured'}\n"
                f"Repair attempts: {int(attempts)}\n"
                f"Needed next: {needed or 'founder direction'}"
            ),
            "severity": severity,
            "dedupe_key": f"project:{project['id']}:blocked:{criterion_id or 'project'}",
        }
        updated = self._transition(
            project,
            state,
            event={
                "event_type": "build_blocked",
                "detail": reason,
                "metadata": {
                    "phase": "blocked",
                    "criterion_id": criterion_id,
                    "attempts": int(attempts),
                    "severity": severity,
                },
            },
            outbox=outbox,
        )
        self.deliver_due_notifications()
        return self.get_project(updated["id"])

    # ---- resumption ---------------------------------------------------------

    def resume_after_decision(self, decision_id: int, *, answer: str = "") -> dict[str, Any]:
        """Clear a needs_decision block and return exactly where to resume."""
        decision = _positive_int(decision_id)
        if decision is None:
            raise ValueError("A decision resolution requires a positive decision_id.")
        project = self._find_waiting_project("decision_id", decision)
        if project is None:
            raise ValueError(f"No project is waiting on decision {decision}.")
        state = self._state_for_project(project)
        if state.get("phase") != "needs_decision":
            raise ValueError(f"No project is waiting on decision {decision}.")
        self._require_transition(state, "building", operation="resume after decision")
        criterion_id = state.get("current_criterion_id")
        state.update(
            {
                "phase": "building",
                "blocking_type": None,
                "blocking_reason": None,
                "decision_id": None,
                "next_action": f"apply founder decision {decision} and continue",
            }
        )
        updated = self._transition(
            project,
            state,
            event={
                "event_type": "decision_applied",
                "detail": answer.strip()[:400],
                "metadata": {
                    "phase": "building",
                    "decision_id": decision,
                    "criterion_id": criterion_id,
                },
            },
        )
        return {"project": updated, "criterion_id": criterion_id, "decision_id": decision}

    def resume_after_approval(
        self, approval_request_id: int, *, approved: bool, note: str = ""
    ) -> dict[str, Any]:
        approval = _positive_int(approval_request_id)
        if approval is None:
            raise ValueError("An approval resolution requires a positive approval_request_id.")
        project = self._find_waiting_project("approval_request_id", approval)
        if project is None:
            raise ValueError(f"No project is waiting on approval {approval}.")
        state = self._state_for_project(project)
        if state.get("phase") != "approval_required":
            raise ValueError(f"No project is waiting on approval {approval}.")
        target_phase = "building" if approved else "blocked"
        self._require_transition(state, target_phase, operation="resume after approval")
        criterion_id = state.get("current_criterion_id")
        if approved:
            state.update(
                {
                    "phase": "building",
                    "blocking_type": None,
                    "blocking_reason": None,
                    "approval_request_id": None,
                    "next_action": f"continue with approved action {approval}",
                }
            )
        else:
            state.update(
                {
                    "phase": "blocked",
                    "blocking_type": "error",
                    "blocking_reason": note.strip() or f"founder denied approval {approval}",
                    "approval_request_id": None,
                    "next_action": "replan without the denied action",
                }
            )
        updated = self._transition(
            project,
            state,
            event={
                "event_type": "approval_resolved",
                "detail": note.strip()[:400],
                "metadata": {
                    "approval_request_id": approval,
                    "phase": state["phase"],
                    "approved": bool(approved),
                    "criterion_id": criterion_id,
                },
            },
        )
        return {
            "project": updated,
            "criterion_id": criterion_id,
            "approval_request_id": approval,
        }

    # ---- the MVP completion gate --------------------------------------------

    def complete_mvp(self, project_id: int, *, final_verification: dict[str, Any]) -> dict[str, Any]:
        """Enter mvp_complete only on full mechanical evidence.

        Gate: every required documented criterion is complete (with persisted
        verification evidence and a recorded commit), none remains blocked or
        unverified, the FRESH final project-level verification passed, and a
        live git check performed here shows the repository clean. Anything
        less raises ValueError — scaffold 'verified' never satisfies this.
        """
        project = self.get_project(project_id)
        state = self._state_for_project(project)
        criteria = state.get("mvp_criteria") or []
        if not criteria:
            raise ValueError(
                "MVP completion rejected: no documented MVP criteria are planned; "
                "scaffold verification alone never completes an MVP."
            )
        required = [item for item in criteria if item.get("required", True)]
        if not required:
            raise ValueError("MVP completion rejected: the plan has no required criteria.")
        for item in required:
            if item.get("status") == "blocked":
                raise ValueError(
                    f"MVP completion rejected: required criterion '{item['id']}' is blocked."
                )
            if item.get("status") != "complete":
                raise ValueError(
                    f"MVP completion rejected: required criterion '{item['id']}' is not complete."
                )
            evidence = item.get("verification")
            if not (isinstance(evidence, dict) and evidence.get("ok") is True and item.get("commit")):
                raise ValueError(
                    f"MVP completion rejected: required criterion '{item['id']}' lacks "
                    "persisted verification evidence at a recorded commit."
                )
        snapshot = _validate_project_verification(
            final_verification,
            label="final project-level verification",
            project=project,
        )
        head = snapshot["head"]
        if state.get("mvp_complete") and state.get("mvp_completed_commit") == head:
            return project  # already attested at this exact commit; exactly-one notification holds
        self._require_transition(state, "mvp_complete", operation="complete MVP")
        completed = sum(1 for item in criteria if item.get("status") == "complete")
        checks = final_verification.get("checks") or []
        boundaries = [str(item) for item in (state.get("external_boundaries") or [])] or [
            f"store release not yet submitted: {target}"
            for target in project.get("distribution_targets") or []
        ]
        state.update(
            {
                "phase": "mvp_complete",
                "mvp_complete": True,
                "mvp_completed_commit": head,
                "repo_head": head,
                "last_verified_at": str(final_verification.get("checked_at")),
                "final_verification": _verification_summary(final_verification),
                "active_run_id": None,
                "blocking_type": None,
                "blocking_reason": None,
                "next_action": "founder review; external release boundaries remain founder-approved",
            }
        )
        outbox = {
            "topic": "project.mvp_complete",
            "title": f"{project['name']} MVP complete",
            "body": (
                f"MVP criteria complete: {completed}/{len(criteria)}\n"
                f"Final verification: {len(checks)} checks passed at {final_verification.get('checked_at')}\n"
                f"Repository: {project['canonical_path']}\n"
                f"Commit: {head}\n"
                f"Remaining external boundaries: {'; '.join(boundaries) or 'none recorded'}"
            ),
            "severity": "info",
            "dedupe_key": f"project:{project['id']}:mvp:{head}",
        }
        updated = self._transition(
            project,
            state,
            event={
                "event_type": "mvp_completed",
                "detail": f"{completed}/{len(criteria)} criteria complete at {head}",
                "metadata": {
                    "phase": "mvp_complete",
                    "commit": head,
                    "checks": len(checks),
                    "verification": final_verification,
                },
            },
            outbox=outbox,
        )
        self.deliver_due_notifications()
        return self.get_project(updated["id"])

    # ---- internals ----------------------------------------------------------

    def _mutable_state(self, project: dict[str, Any]) -> dict[str, Any]:
        state = self._state_for_project(project)
        if state.get("mvp_complete"):
            raise ValueError(
                f"Project '{project['name']}' is already mvp_complete; re-plan a new scope first."
            )
        return state

    def _transition(
        self,
        project: dict[str, Any],
        state: dict[str, Any],
        *,
        event: dict[str, Any],
        outbox: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state["updated_at"] = utc_now()
        if state.get("phase") not in PHASES:
            raise ValueError(f"Phase must be one of: {', '.join(PHASES)}")
        expected_version = int(state.get("_store_version") or 0)
        persisted = {
            key: copy.deepcopy(value)
            for key, value in state.items()
            if not key.startswith("_store_")
        }
        stored = self.store.transition(
            project["id"],
            expected_version=expected_version,
            state=persisted,
            event=event,
            outbox=outbox,
        )
        current = self.db.get_project(project["id"])
        if current is None:
            raise RuntimeError(f"Project vanished during autonomy transition: {project['id']}")
        return self._attach_state(current, self._state_from_store(stored))

    def _state_for_project(self, project: dict[str, Any]) -> dict[str, Any]:
        stored = self.store.get(project["id"])
        if int(stored.get("version") or 0) > 0:
            return self._state_from_store(stored)
        legacy = _autonomy_state(project)
        legacy["_store_version"] = 0
        return legacy

    @staticmethod
    def _state_from_store(stored: dict[str, Any]) -> dict[str, Any]:
        state = _default_state()
        state.update(
            {
                key: copy.deepcopy(value)
                for key, value in stored.items()
                if key not in {"project_id", "created_at", "updated_at", "version"}
            }
        )
        state["_store_version"] = int(stored.get("version") or 0)
        state["_store_created_at"] = str(stored.get("created_at") or "")
        state["_store_updated_at"] = str(stored.get("updated_at") or "")
        return state

    @staticmethod
    def _attach_state(project: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        attached = copy.deepcopy(project)
        metadata = dict(attached.get("metadata") or {})
        metadata["autonomy"] = {
            key: copy.deepcopy(value)
            for key, value in state.items()
            if not key.startswith("_store_")
        }
        attached["metadata"] = metadata
        return attached

    @staticmethod
    def _require_transition(
        state: dict[str, Any], target: str, *, operation: str
    ) -> None:
        current = str(state.get("phase") or "planning")
        if target not in ALLOWED_TRANSITIONS.get(current, set()):
            raise ValueError(
                f"Cannot {operation} while project phase is {current}; "
                f"transition to {target} is not allowed."
            )

    def _find_waiting_project(self, field: str, value: int) -> dict[str, Any] | None:
        if field not in {"decision_id", "approval_request_id"}:
            raise ValueError(f"Unsupported autonomy reference field: {field}")
        json_path = f"$.{field}"
        legacy_path = f"$.autonomy.{field}"
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT s.project_id AS project_id
                FROM project_autonomy_states AS s
                WHERE CAST(json_extract(s.state_json, ?) AS INTEGER) = ?
                UNION ALL
                SELECT p.id AS project_id
                FROM projects AS p
                LEFT JOIN project_autonomy_states AS s ON s.project_id = p.id
                WHERE s.project_id IS NULL
                  AND CAST(json_extract(p.metadata_json, ?) AS INTEGER) = ?
                ORDER BY project_id
                """,
                (json_path, value, legacy_path, value),
            ).fetchall()
        for row in rows:
            project = self.get_project(int(row["project_id"]))
            if _positive_int(self._state_for_project(project).get(field)) == value:
                return project
        return None

    def deliver_due_notifications(self, limit: int = 50) -> dict[str, int]:
        rows = self.store.due_outbox(limit=limit)
        result = {"seen": len(rows), "delivered": 0, "retried": 0}
        if self.bus is None:
            return result
        for row in rows:
            notification: dict[str, Any] | None = None
            try:
                attempt = int(row.get("attempts") or 0) + 1
                notification = self.bus.notify(
                    topic=row["topic"],
                    title=row["title"],
                    body=row["body"],
                    severity=row["severity"],
                    source="project_autonomy",
                    dedupe_key=f"autonomy-outbox:{row['id']}:attempt:{attempt}",
                    metadata={
                        "project_id": row["project_id"],
                        "autonomy_outbox_id": row["id"],
                        "producer_dedupe_key": row["dedupe_key"],
                    },
                )
                raw_notification_id = _positive_int((notification or {}).get("id"))
                notification_id = self._persisted_notification_id(raw_notification_id)
                retry_reason = _notification_retry_reason(notification or {})
                if retry_reason:
                    delay_seconds = min(3600, 60 * (2 ** min(attempt, 5)))
                    self.store.reschedule_outbox(
                        row["id"],
                        error=retry_reason,
                        notification_id=notification_id,
                        delay_seconds=delay_seconds,
                    )
                    result["retried"] += 1
                    continue
                delivered = self.store.mark_outbox_delivered(
                    row["id"], notification_id=notification_id
                )
                result["delivered"] += 1
                if notification_id is not None:
                    project = self.get_project(int(row["project_id"]))
                    state = self._state_for_project(project)
                    state["last_notification_id"] = notification_id
                    self._transition(
                        project,
                        state,
                        event={
                            "event_type": "notification_delivered",
                            "detail": row["topic"],
                            "notification_id": notification_id,
                            "metadata": {
                                "outbox_id": delivered["id"],
                                "topic": row["topic"],
                            },
                        },
                    )
            except Exception as exc:  # noqa: BLE001 - the durable row must survive any adapter failure
                notification_id = self._persisted_notification_id(
                    _positive_int((notification or {}).get("id"))
                    if isinstance(notification, dict)
                    else None
                )
                try:
                    self.store.reschedule_outbox(
                        row["id"],
                        error=f"{type(exc).__name__}: {exc}",
                        notification_id=notification_id,
                        delay_seconds=300,
                    )
                except ValueError:
                    pass
                result["retried"] += 1
        return result

    def _persisted_notification_id(self, notification_id: int | None) -> int | None:
        if notification_id is None:
            return None
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM notifications WHERE id = ?", (notification_id,)
            ).fetchone()
        return notification_id if row is not None else None


# ---- pure projection (no database access) -----------------------------------


def autonomy_projection(project: dict[str, Any]) -> dict[str, Any]:
    """The autonomy object exposed on every project API representation.

    Projects the durable reporter state when present; otherwise derives an
    honest view from the intake lifecycle. Scaffold 'verified' projects as
    ready_for_next_increment with mvp_complete strictly False — it NEVER
    reads as MVP completion.
    """
    metadata = project.get("metadata") or {}
    raw = metadata.get("autonomy")
    state = raw if isinstance(raw, dict) else {}
    scaffold = metadata.get("existing_scaffold_verification")
    scaffold = scaffold if isinstance(scaffold, dict) else {}
    snapshot = scaffold.get("git_snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    criteria = state.get("mvp_criteria") or []
    if state:
        phase = str(state.get("phase") or "planning")
        blocking_type = state.get("blocking_type")
        blocking_reason = state.get("blocking_reason")
        decision_id = _positive_int(state.get("decision_id"))
        approval_request_id = _positive_int(state.get("approval_request_id"))
        next_action = str(state.get("next_action") or "")
    else:
        lifecycle = str(project.get("lifecycle_state") or "")
        decision_id = _positive_int(metadata.get("decision_id"))
        approval_request_id = _positive_int(metadata.get("approval_request_id"))
        blocked_reason = metadata.get("blocked_reason")
        if lifecycle == "verified":
            phase, blocking_type, blocking_reason = "ready_for_next_increment", None, None
            next_action = "scaffold verified; awaiting an MVP plan"
        elif decision_id is not None:
            phase, blocking_type = "needs_decision", "decision"
            question = metadata.get("founder_question")
            question = question if isinstance(question, dict) else {}
            blocking_reason = str(question.get("question") or "founder decision pending")
            next_action = f"waiting for founder decision {decision_id}"
        elif approval_request_id is not None:
            phase, blocking_type = "approval_required", "approval"
            blocking_reason = str(blocked_reason or "founder approval pending")
            next_action = f"waiting for founder approval {approval_request_id}"
        elif lifecycle == "blocked":
            phase, blocking_type = "blocked", "error"
            blocking_reason = str(blocked_reason or "build did not verify")
            next_action = "founder or tooling intervention required"
        elif lifecycle == "building":
            phase, blocking_type, blocking_reason = "building", None, None
            next_action = "initial scaffold build in progress"
        else:
            phase, blocking_type, blocking_reason = "planning", None, None
            next_action = "project intake"
    return {
        "phase": phase,
        "priority": str(state.get("priority") or "normal"),
        "mvp_criteria_total": len(criteria),
        "mvp_criteria_completed": sum(1 for item in criteria if item.get("status") == "complete"),
        "current_criterion_id": state.get("current_criterion_id"),
        "current_increment": _positive_int(state.get("current_increment")),
        "last_verified_at": state.get("last_verified_at") or scaffold.get("checked_at"),
        "next_action": next_action,
        "blocking_type": blocking_type,
        "blocking_reason": blocking_reason,
        "decision_id": decision_id,
        "approval_request_id": approval_request_id,
        "active_run_id": _positive_int(state.get("active_run_id")),
        "last_notification_id": _positive_int(state.get("last_notification_id")),
        "repo_head": state.get("repo_head") or (snapshot.get("head") or None),
        "mvp_complete": state.get("mvp_complete") is True,
        "paused": state.get("paused") is True,
        "pause_reason": str(state.get("pause_reason") or ""),
    }


def portfolio_bucket(project: dict[str, Any], projection: dict[str, Any] | None = None) -> str:
    """One honest portfolio bucket per project — never a collapsed 'verified'."""
    autonomy = projection or autonomy_projection(project)
    phase = autonomy["phase"]
    if autonomy["mvp_complete"]:
        return "mvp_complete"
    if autonomy.get("paused"):
        return "paused"
    if phase == "needs_decision":
        return "waiting_decision"
    if phase == "approval_required":
        return "waiting_approval"
    if phase == "blocked":
        return "blocked"
    if phase in {"building", "verifying"} and autonomy.get("active_run_id") is not None:
        return "actively_building"
    if phase == "ready_for_next_increment" and autonomy["mvp_criteria_total"] > 0:
        return "ready_for_next_increment"
    if autonomy["mvp_criteria_total"] > 0:
        return "planned"
    if str(project.get("lifecycle_state") or "") == "verified":
        return "scaffold_verified"
    return "intake"


def portfolio_status(projects: list[dict[str, Any]]) -> dict[str, Any]:
    totals = {
        "scaffold_verified": 0,
        "actively_building": 0,
        "waiting_decision": 0,
        "waiting_approval": 0,
        "blocked": 0,
        "mvp_complete": 0,
        "paused": 0,
        "planned": 0,
        "ready_for_next_increment": 0,
        "intake": 0,
    }
    items: list[dict[str, Any]] = []
    for project in projects:
        projection = (
            project.get("autonomy")
            if isinstance(project.get("autonomy"), dict)
            else autonomy_projection(project)
        )
        bucket = portfolio_bucket(project, projection)
        totals[bucket] += 1
        items.append(
            {
                **project,
                "status": bucket,
                "autonomy": projection,
            }
        )
    return {"totals": totals, "projects": items}


# ---- helpers -----------------------------------------------------------------


def _sanitize_project_for_api(project: dict[str, Any]) -> dict[str, Any]:
    sanitized = copy.deepcopy(project)

    def bounded(value: Any, *, key: str = "") -> Any:
        if isinstance(value, dict):
            return {name: bounded(item, key=str(name)) for name, item in value.items()}
        if isinstance(value, list):
            return [bounded(item, key=key) for item in value]
        if key in {"output", "stdout", "stderr", "artifact", "response"}:
            text = str(value or "")
            return text if len(text) <= 240 else f"{text[:240]}…"
        return value

    sanitized["metadata"] = bounded(sanitized.get("metadata") or {})
    return sanitized


def _default_state() -> dict[str, Any]:
    return {
        "phase": "planning",
        "priority": "normal",
        "plan_revision": "",
        "mvp_criteria": [],
        "current_criterion_id": None,
        "current_increment": 0,
        "last_verified_at": None,
        "next_action": "",
        "blocking_type": None,
        "blocking_reason": None,
        "decision_id": None,
        "approval_request_id": None,
        "active_run_id": None,
        "last_notification_id": None,
        "repo_head": None,
        "mvp_complete": False,
        "mvp_completed_commit": None,
        "paused": False,
        "pause_reason": "",
        "external_boundaries": [],
    }


def _autonomy_state(project: dict[str, Any]) -> dict[str, Any]:
    metadata = project.get("metadata") or {}
    raw = metadata.get("autonomy")
    state = _default_state()
    if isinstance(raw, dict):
        state.update(raw)
    return state


def _find_criterion(state: dict[str, Any], criterion_id: str) -> dict[str, Any]:
    for item in state.get("mvp_criteria") or []:
        if item.get("id") == criterion_id:
            return item
    raise ValueError(f"Unknown MVP criterion: {criterion_id}. Plan criteria first.")


def _clean_options(options: list[dict[str, Any]]) -> list[dict[str, str]]:
    cleaned: list[dict[str, str]] = []
    for entry in options or []:
        if not isinstance(entry, dict):
            raise ValueError("Each decision option must be a mapping with option and impact.")
        option = str(entry.get("option") or "").strip()
        impact = str(entry.get("impact") or "").strip()
        if not option or not impact:
            raise ValueError("Each decision option requires non-empty option and impact text.")
        cleaned.append({"option": option, "impact": impact})
    if not 2 <= len(cleaned) <= 3:
        raise ValueError("A founder decision requires 2-3 concrete options with impacts.")
    return cleaned


def _validate_verification(verification: Any, *, label: str) -> None:
    """Accept only fresh command evidence; repository binding is checked separately."""
    if not isinstance(verification, dict) or verification.get("ok") is not True:
        raise ValueError(f"Rejected: {label} did not pass (verification.ok must be True).")
    checks = verification.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ValueError(
            f"Rejected: {label} carries no mechanical check records; prose is not evidence."
        )
    for check in checks:
        if not isinstance(check, dict) or check.get("ok") is not True:
            raise ValueError(f"Rejected: {label} contains a failed or malformed check.")
        argv = check.get("argv")
        command = check.get("command")
        has_argv = isinstance(argv, list) and bool(argv) and all(str(item).strip() for item in argv)
        has_command = isinstance(command, str) and bool(command.strip())
        if not (has_argv or has_command):
            raise ValueError(f"Rejected: {label} contains a check without a recorded command.")
        returncode = check.get("returncode")
        if isinstance(returncode, bool) or not isinstance(returncode, int) or returncode != 0:
            raise ValueError(f"Rejected: {label} contains a check with a failing returncode.")
        if not str(check.get("output") or "").strip():
            raise ValueError(f"Rejected: {label} contains a check without captured output.")
    checked_at = str(verification.get("checked_at") or "").strip()
    if not checked_at:
        raise ValueError(f"Rejected: {label} has no checked_at timestamp.")
    try:
        checked = datetime.fromisoformat(checked_at)
    except ValueError as exc:
        raise ValueError(f"Rejected: {label} checked_at is not a valid timestamp.") from exc
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - checked).total_seconds()
    if age < -(5 * 60):
        raise ValueError(f"Rejected: {label} checked_at is more than five minutes in the future.")
    if age > VERIFICATION_MAX_AGE_MINUTES * 60:
        raise ValueError(
            f"Rejected: {label} is stale (older than {VERIFICATION_MAX_AGE_MINUTES} minutes); "
            "run a fresh verification."
        )


def _validate_project_verification(
    verification: Any,
    *,
    label: str,
    project: dict[str, Any],
    expected_commit: str | None = None,
) -> dict[str, str]:
    _validate_verification(verification, label=label)
    assert isinstance(verification, dict)
    root = Path(str(project.get("canonical_path") or ""))
    expected_path = _normalized_path(root)
    evidence_path = str(verification.get("project_path") or "").strip()
    if not evidence_path or _normalized_path(Path(evidence_path)) != expected_path:
        raise ValueError(
            f"Rejected: {label} project_path does not match the registered project root."
        )
    snapshot = _git_state(root)
    evidence_head = str(verification.get("repo_head") or "").strip()
    if not evidence_head or evidence_head != snapshot["head"]:
        raise ValueError(
            f"Rejected: {label} repo_head does not match the current repository commit."
        )
    if expected_commit is not None and str(expected_commit).strip() != evidence_head:
        raise ValueError(
            f"Rejected: {label} commit does not match the mechanically verified repo_head."
        )
    evidence_status = verification.get("repo_status")
    if not isinstance(evidence_status, str) or evidence_status != snapshot["status"]:
        raise ValueError(
            f"Rejected: {label} repo_status does not match the live git status."
        )
    if snapshot["status"]:
        raise ValueError(f"Rejected: {label} repository is not clean at the recorded commit.")
    return snapshot


def _git_state(root: Path) -> dict[str, str]:
    inside = _run_git(root, "rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip().lower() != "true":
        raise ValueError(
            "MVP evidence rejected: the registered project root is not a git worktree."
        )
    head_result = _run_git(root, "rev-parse", "HEAD")
    if head_result.returncode != 0 or not head_result.stdout.strip():
        raise ValueError("MVP evidence rejected: git rev-parse HEAD failed.")
    status_result = _run_git(root, "status", "--porcelain")
    if status_result.returncode != 0:
        detail = status_result.stderr.strip() or "unknown error"
        raise ValueError(f"MVP evidence rejected: git status failed: {detail}")
    return {"head": head_result.stdout.strip(), "status": status_result.stdout.strip()}


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(
            ["git", *args],
            1,
            "",
            f"{type(exc).__name__}: {exc}",
        )


def _verification_summary(verification: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "checked_at": str(verification.get("checked_at") or ""),
        "project_path": str(verification.get("project_path") or ""),
        "repo_head": str(verification.get("repo_head") or ""),
        "repo_status": str(verification.get("repo_status") or ""),
        "checks_count": len(verification.get("checks") or []),
    }


def _normalized_path(path: Path) -> str:
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        resolved = path.absolute()
    return os.path.normcase(str(resolved))


def _notification_retry_reason(notification: dict[str, Any]) -> str:
    deliveries = notification.get("deliveries")
    if isinstance(deliveries, list):
        telegram = [
            item
            for item in deliveries
            if isinstance(item, dict) and item.get("channel") == "telegram"
        ]
        if telegram:
            incomplete = [item for item in telegram if item.get("status") != "delivered"]
            if incomplete:
                return "; ".join(
                    str(item.get("detail") or item.get("status") or "telegram not delivered")
                    for item in incomplete
                )[:400]
            return ""
    status = str(notification.get("status") or "").strip().lower()
    if status and status != "delivered":
        return str(
            notification.get("suppressed_reason") or f"notification status {status}"
        )[:400]
    return ""


def _next_pending_action(state: dict[str, Any]) -> str:
    for item in state.get("mvp_criteria") or []:
        if item.get("status") not in {"complete"}:
            return f"start next increment for criterion {item['id']}"
    return "run final project-level verification and complete the MVP"


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
