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

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .db import KernelDatabase, utc_now
from .project_intake import _git_snapshot

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

    def __init__(self, *, db: KernelDatabase, bus: Any | None = None):
        self.db = db
        self.bus = bus

    # ---- reads --------------------------------------------------------------

    def get_project(self, project_id: int) -> dict[str, Any]:
        project = self.db.get_project(project_id)
        if project is None:
            raise ValueError(f"Project not found: {project_id}")
        return project

    def state(self, project_id: int) -> dict[str, Any]:
        return _autonomy_state(self.get_project(project_id))

    # ---- planning and increments (ledger-only, no founder notifications) ----

    def plan(
        self,
        project_id: int,
        *,
        criteria: list[dict[str, Any]],
        priority: str | None = None,
        next_action: str = "",
        external_boundaries: list[str] | None = None,
    ) -> dict[str, Any]:
        project = self.get_project(project_id)
        if not criteria:
            raise ValueError("An MVP plan requires at least one documented criterion.")
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
            normalized.append(
                {
                    "id": criterion_id,
                    "title": title,
                    "required": bool(entry.get("required", True)),
                    "status": "pending",
                }
            )
        if priority is not None and priority not in PRIORITIES:
            raise ValueError(f"Priority must be one of: {', '.join(PRIORITIES)}")
        prior = _autonomy_state(project)
        state = _default_state()
        state.update(
            {
                "phase": "planning",
                "priority": priority or prior.get("priority") or "normal",
                "mvp_criteria": normalized,
                "next_action": next_action,
                "external_boundaries": [str(item) for item in (external_boundaries or [])],
            }
        )
        project = self._save(project, state)
        self.db.append_project_event(
            project_id,
            event_type="autonomy_planned",
            detail=next_action,
            metadata={"phase": "planning", "criteria": [item["id"] for item in normalized]},
        )
        return project

    def set_priority(self, project_id: int, priority: str) -> dict[str, Any]:
        if priority not in PRIORITIES:
            raise ValueError(f"Priority must be one of: {', '.join(PRIORITIES)}")
        project = self.get_project(project_id)
        state = _autonomy_state(project)
        state["priority"] = priority
        project = self._save(project, state)
        self.db.append_project_event(
            project_id, event_type="priority_changed", metadata={"priority": priority}
        )
        return project

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
        project = self._save(project, state)
        self.db.append_project_event(
            project_id,
            event_type="increment_started",
            detail=criterion["title"],
            metadata={
                "phase": "building",
                "criterion_id": criterion_id,
                "increment": state["current_increment"],
                "run_id": state["active_run_id"],
            },
        )
        return project

    def begin_verification(self, project_id: int, *, run_id: int | None = None) -> dict[str, Any]:
        project = self.get_project(project_id)
        state = self._mutable_state(project)
        state["phase"] = "verifying"
        if run_id is not None:
            state["active_run_id"] = _positive_int(run_id)
        project = self._save(project, state)
        self.db.append_project_event(
            project_id,
            event_type="verification_started",
            metadata={"phase": "verifying", "criterion_id": state.get("current_criterion_id")},
        )
        return project

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
        state["phase"] = "ready_for_next_increment"
        state["active_run_id"] = None
        if isinstance(verification, dict) and verification.get("ok") is True:
            state["last_verified_at"] = str(verification.get("checked_at") or utc_now())
        project = self._save(project, state)
        self.db.append_project_event(
            project_id,
            event_type="increment_completed",
            detail=summary[:400],
            metadata={
                "phase": "ready_for_next_increment",
                "criterion_id": state.get("current_criterion_id"),
                "increment": state.get("current_increment"),
                "verified": bool(isinstance(verification, dict) and verification.get("ok") is True),
            },
        )
        return project

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
        _validate_verification(verification, label=f"criterion {criterion_id}")
        head = str(commit or "").strip()
        if not head:
            raise ValueError(
                f"Criterion {criterion_id} cannot complete without the verified repository commit."
            )
        criterion.update(
            {
                "status": "complete",
                "verified_at": str(verification.get("checked_at")),
                "commit": head,
                "verification": verification,
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
        project = self._save(project, state)
        self.db.append_project_event(
            project_id,
            event_type="criterion_completed",
            detail=criterion["title"],
            metadata={
                "phase": "ready_for_next_increment",
                "criterion_id": criterion_id,
                "commit": head,
                "checks": len(verification.get("checks") or []),
            },
        )
        return project

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
        state.update(
            {
                "phase": "needs_decision",
                "blocking_type": "decision",
                "blocking_reason": question,
                "decision_id": decision,
                "next_action": f"waiting for founder decision {decision}",
            }
        )
        options_block = "\n".join(
            f"{index}. {item['option']} — impact: {item['impact']}"
            for index, item in enumerate(cleaned, start=1)
        )
        notification_id = self._notify(
            topic="project.decision_required",
            title=f"{project['name']} needs a decision",
            body=(
                f"Question: {question}\n"
                f"Recommendation: {recommendation}\n\n"
                f"Options:\n{options_block}\n\n"
                f"Reply exactly: decision {decision}: <your answer>"
            ),
            severity="warning",
            dedupe_key=f"project:{project['id']}:decision:{decision}",
            metadata={
                "project_id": project["id"],
                "decision_id": decision,
                "criterion_id": state.get("current_criterion_id"),
            },
        )
        state["last_notification_id"] = notification_id or state.get("last_notification_id")
        project = self._save(project, state)
        self.db.append_project_event(
            project_id,
            event_type="decision_requested",
            detail=question,
            metadata={
                "phase": "needs_decision",
                "decision_id": decision,
                "notification_id": notification_id,
            },
        )
        return project

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
        hint = approve_hint.strip() or (
            f"Approve: POST /work/items/{approval}/approve — "
            f"Deny: POST /work/items/{approval}/deny"
        )
        state.update(
            {
                "phase": "approval_required",
                "blocking_type": "approval",
                "blocking_reason": reason,
                "approval_request_id": approval,
                "next_action": f"waiting for founder approval {approval}",
            }
        )
        notification_id = self._notify(
            topic="project.approval_required",
            title=f"{project['name']} needs founder approval",
            body=(
                f"Proposed action: {action}\n"
                f"Why approval is required: {reason}\n"
                f"Authority boundary: {boundary}\n"
                f"Approval request: {approval}\n\n"
                f"{hint}"
            ),
            severity="warning",
            dedupe_key=f"project:{project['id']}:approval:{approval}",
            metadata={
                "project_id": project["id"],
                "approval_request_id": approval,
                "boundary": boundary,
            },
        )
        state["last_notification_id"] = notification_id or state.get("last_notification_id")
        project = self._save(project, state)
        self.db.append_project_event(
            project_id,
            event_type="approval_requested",
            detail=action,
            metadata={
                "phase": "approval_required",
                "boundary": boundary,
                "approval_request_id": approval,
                "notification_id": notification_id,
            },
        )
        return project

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
                "next_action": needed or "founder or tooling intervention required",
            }
        )
        notification_id = self._notify(
            topic="project.build_blocked",
            title=f"{project['name']} build is blocked",
            body=(
                f"Failed criterion: {criterion_title}\n"
                f"Reason: {reason}\n"
                f"Verification output: {verification_output.strip()[:600] or 'none captured'}\n"
                f"Repair attempts: {int(attempts)}\n"
                f"Needed next: {needed or 'founder direction'}"
            ),
            severity=severity,
            dedupe_key=f"project:{project['id']}:blocked:{criterion_id or 'project'}",
            metadata={
                "project_id": project["id"],
                "criterion_id": criterion_id,
                "attempts": int(attempts),
            },
        )
        state["last_notification_id"] = notification_id or state.get("last_notification_id")
        project = self._save(project, state)
        self.db.append_project_event(
            project_id,
            event_type="build_blocked",
            detail=reason,
            metadata={
                "phase": "blocked",
                "criterion_id": criterion_id,
                "attempts": int(attempts),
                "severity": severity,
                "notification_id": notification_id,
            },
        )
        return project

    # ---- resumption ---------------------------------------------------------

    def resume_after_decision(self, decision_id: int, *, answer: str = "") -> dict[str, Any]:
        """Clear a needs_decision block and return exactly where to resume."""
        decision = _positive_int(decision_id)
        if decision is None:
            raise ValueError("A decision resolution requires a positive decision_id.")
        for project in self.db.list_projects(limit=500):
            state = _autonomy_state(project)
            if _positive_int(state.get("decision_id")) != decision:
                continue
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
            updated = self._save(project, state)
            self.db.append_project_event(
                project["id"],
                event_type="decision_applied",
                detail=answer.strip()[:400],
                metadata={"phase": "building", "decision_id": decision, "criterion_id": criterion_id},
            )
            return {"project": updated, "criterion_id": criterion_id, "decision_id": decision}
        raise ValueError(f"No project is waiting on decision {decision}.")

    def resume_after_approval(
        self, approval_request_id: int, *, approved: bool, note: str = ""
    ) -> dict[str, Any]:
        approval = _positive_int(approval_request_id)
        if approval is None:
            raise ValueError("An approval resolution requires a positive approval_request_id.")
        for project in self.db.list_projects(limit=500):
            state = _autonomy_state(project)
            if _positive_int(state.get("approval_request_id")) != approval:
                continue
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
            updated = self._save(project, state)
            self.db.append_project_event(
                project["id"],
                event_type="approval_resolved",
                detail=note.strip()[:400],
                metadata={
                    "approval_request_id": approval,
                    "phase": state["phase"],
                    "approved": bool(approved),
                    "criterion_id": criterion_id,
                },
            )
            return {"project": updated, "criterion_id": criterion_id, "approval_request_id": approval}
        raise ValueError(f"No project is waiting on approval {approval}.")

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
        state = _autonomy_state(project)
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
        _validate_verification(final_verification, label="final project-level verification")
        root = Path(project["canonical_path"])
        if not (root / ".git").is_dir():
            raise ValueError("MVP completion rejected: the project has no git repository to attest.")
        snapshot = _git_snapshot(root)
        head = snapshot.get("head") or ""
        if not head:
            raise ValueError("MVP completion rejected: the repository has no commit to record.")
        if snapshot.get("status"):
            raise ValueError(
                "MVP completion rejected: the repository is not clean at the recorded commit."
            )
        if state.get("mvp_complete") and state.get("mvp_completed_commit") == head:
            return project  # already attested at this exact commit; exactly-one notification holds
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
                "final_verification": final_verification,
                "active_run_id": None,
                "blocking_type": None,
                "blocking_reason": None,
                "next_action": "founder review; external release boundaries remain founder-approved",
            }
        )
        notification_id = self._notify(
            topic="project.mvp_complete",
            title=f"{project['name']} MVP complete",
            body=(
                f"MVP criteria complete: {completed}/{len(criteria)}\n"
                f"Final verification: {len(checks)} checks passed at {final_verification.get('checked_at')}\n"
                f"Repository: {project['canonical_path']}\n"
                f"Commit: {head}\n"
                f"Remaining external boundaries: {'; '.join(boundaries) or 'none recorded'}"
            ),
            severity="info",
            dedupe_key=f"project:{project['id']}:mvp:{head}",
            metadata={"project_id": project["id"], "commit": head, "criteria_total": len(criteria)},
            force_channels=("telegram",),
        )
        state["last_notification_id"] = notification_id or state.get("last_notification_id")
        project = self._save(project, state)
        self.db.append_project_event(
            project_id,
            event_type="mvp_completed",
            detail=f"{completed}/{len(criteria)} criteria complete at {head}",
            metadata={
                "phase": "mvp_complete",
                "commit": head,
                "checks": len(checks),
                "notification_id": notification_id,
            },
        )
        return project

    # ---- internals ----------------------------------------------------------

    def _mutable_state(self, project: dict[str, Any]) -> dict[str, Any]:
        state = _autonomy_state(project)
        if state.get("mvp_complete"):
            raise ValueError(
                f"Project '{project['name']}' is already mvp_complete; re-plan a new scope first."
            )
        return state

    def _save(self, project: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        state["updated_at"] = utc_now()
        if state.get("phase") not in PHASES:
            raise ValueError(f"Phase must be one of: {', '.join(PHASES)}")
        return self.db.update_project_metadata(project["id"], {"autonomy": state})

    def _notify(self, **kwargs: Any) -> int | None:
        if self.bus is None:
            return None
        notification = self.bus.notify(source="project_autonomy", **kwargs)
        return _positive_int((notification or {}).get("id"))


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
    }


def portfolio_bucket(project: dict[str, Any], projection: dict[str, Any] | None = None) -> str:
    """One honest portfolio bucket per project — never a collapsed 'verified'."""
    autonomy = projection or autonomy_projection(project)
    phase = autonomy["phase"]
    if autonomy["mvp_complete"]:
        return "mvp_complete"
    if phase == "needs_decision":
        return "waiting_decision"
    if phase == "approval_required":
        return "waiting_approval"
    if phase == "blocked":
        return "blocked"
    if phase in {"building", "verifying"}:
        return "actively_building"
    if autonomy["mvp_criteria_total"] > 0:
        return "actively_building"  # planned MVP between increments is active work
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
        "intake": 0,
    }
    items: list[dict[str, Any]] = []
    for project in projects:
        projection = autonomy_projection(project)
        bucket = portfolio_bucket(project, projection)
        totals[bucket] += 1
        items.append(
            {
                "id": project["id"],
                "name": project["name"],
                "canonical_path": project["canonical_path"],
                "lifecycle_state": project["lifecycle_state"],
                "status": bucket,
                "autonomy": projection,
            }
        )
    return {"totals": totals, "projects": items}


# ---- helpers -----------------------------------------------------------------


def _default_state() -> dict[str, Any]:
    return {
        "phase": "planning",
        "priority": "normal",
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
    """Accept only fresh mechanical verification evidence, never prose."""
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
        if not (check.get("argv") or check.get("command") or check.get("name")):
            raise ValueError(f"Rejected: {label} contains a check without a recorded command.")
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
    if age > VERIFICATION_MAX_AGE_MINUTES * 60:
        raise ValueError(
            f"Rejected: {label} is stale (older than {VERIFICATION_MAX_AGE_MINUTES} minutes); "
            "run a fresh verification."
        )


def _next_pending_action(state: dict[str, Any]) -> str:
    for item in state.get("mvp_criteria") or []:
        if item.get("status") not in {"complete"}:
            return f"start next increment for criterion {item['id']}"
    return "run final project-level verification and complete the MVP"


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
