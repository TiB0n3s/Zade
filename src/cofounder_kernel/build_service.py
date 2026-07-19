"""Lifecycle orchestration for local-first, lease-governed builds."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Sequence

from .build_assessment import BuildAssessmentService
from .build_budget import BuildBudgetService
from .build_orchestrator import BuildOrchestrator
from .build_routing import (
    BuildContextSelector,
    BuildRouter,
    BuildStep,
    LocalAttempt,
    RouteDecision,
    SelectedContext,
)
from .build_store import BuildStore
from .build_types import BuildAssessment, BuildLease, BuildSession, BuildTier
from .build_workers import BuildExecutionManager
from .config import KernelConfig
from .db import KernelDatabase
from .egress import (
    DataClass,
    EgressPolicy,
    EgressRequest,
    authorize_build_egress,
)


BUILD_LEASE_SOURCE = "build_lease"
BUILD_UPGRADE_SOURCE = "build_lease_upgrade"
_TIER_ORDER = (BuildTier.SMALL, BuildTier.MEDIUM, BuildTier.LARGE)


CloudCodingAgentFactory = Callable[[int, Callable[[int, dict[str, Any]], bool]], Any]


class BuildService:
    def __init__(
        self,
        *,
        config: KernelConfig,
        db: KernelDatabase,
        assessor: BuildAssessmentService,
        store: BuildStore,
        budget: BuildBudgetService,
        router: BuildRouter,
        local_coding_agent: Any,
        cloud_coding_agent_factory: CloudCodingAgentFactory | None,
        egress_policy: EgressPolicy,
        typed_confirmation_phrase: str = "make the jump to hyperspace",
        orchestrator: BuildOrchestrator | None = None,
        execution_manager: BuildExecutionManager | None = None,
    ):
        self.config = config
        self.db = db
        self.assessor = assessor
        self.store = store
        self.budget = budget
        self.router = router
        self.local_coding_agent = local_coding_agent
        self.cloud_coding_agent_factory = cloud_coding_agent_factory
        self.egress_policy = egress_policy
        self.typed_confirmation_phrase = typed_confirmation_phrase
        self.orchestrator = orchestrator
        self.execution_manager = execution_manager

    def configure_orchestration(
        self,
        *,
        orchestrator: BuildOrchestrator,
        execution_manager: BuildExecutionManager,
    ) -> None:
        self.orchestrator = orchestrator
        self.execution_manager = execution_manager

    def prepare(
        self,
        *,
        task: str,
        workspace: str | Path,
        acceptance: str = "",
        work_item_id: int | None = None,
    ) -> dict[str, Any]:
        if work_item_id is not None:
            existing = self.store.get_session_for_work_item(work_item_id)
            if existing is not None:
                return self.status(existing.id)
        assessment = self.assessor.assess(
            task=task, workspace=workspace, acceptance=acceptance
        )
        session = self.store.create_session(assessment, work_item_id=work_item_id)
        if self.orchestrator is not None:
            self.orchestrator.ensure_plan(session.id)
        stored_assessment = self.store.get_assessment(session.assessment_id)
        assert stored_assessment is not None
        limits = self.config.build.limits(stored_assessment.recommended_tier)
        approval, _created = self.db.ensure_approval_request(
            source_type=BUILD_LEASE_SOURCE,
            source_id=session.id,
            title=(
                f"Approve {stored_assessment.recommended_tier.value} Anthropic build lease"
            ),
            detail=(
                f"Workspace: {stored_assessment.workspace}\n"
                f"Goal: {stored_assessment.task}\n"
                f"Expires after {limits.duration_seconds} seconds. Local work remains default."
            ),
            action="build.lease.approve",
            target=stored_assessment.workspace,
            permission_tier="L3_EXTERNAL_ACTION",
            authority_decision="approval_required",
            authority={
                "reason": "Source-code egress and paid inference require founder approval.",
                "requires_typed_phrase": True,
            },
            requested_by="build.assessment",
            metadata={
                "session_id": session.id,
                "workspace": stored_assessment.workspace,
                "repo_fingerprint": stored_assessment.repo_fingerprint,
                "provider": "anthropic",
                "model": self.config.build.anthropic_pricing.model,
                "recommended_tier": stored_assessment.recommended_tier.value,
                "limits": asdict(limits),
                "score": stored_assessment.final_score,
                "confidence": stored_assessment.confidence,
                "dimensions": stored_assessment.dimensions,
                "floor_rules": list(stored_assessment.floor_rules),
                "evidence": {
                    "file_count": stored_assessment.evidence.get("file_count", 0),
                    "frameworks": stored_assessment.evidence.get("frameworks", []),
                    "truncated": stored_assessment.evidence.get("truncated", False),
                },
                "permitted_data_classes": [DataClass.SOURCE_CODE.value],
                "local_first_rules": [
                    "routine work remains local",
                    "no paid retry or provider fallback",
                    "the lease cannot enlarge itself",
                    "local verification controls completion claims",
                ],
            },
        )
        payload = self.status(session.id)
        payload["approval_request_id"] = approval.id
        return payload

    def approve(
        self,
        session_id: int,
        *,
        typed_phrase: str,
        tier: BuildTier | str | None = None,
        audit_note: str = "",
    ) -> dict[str, Any]:
        if typed_phrase.strip() != self.typed_confirmation_phrase:
            raise ValueError(
                f"Build lease requires typed confirmation phrase: {self.typed_confirmation_phrase}"
            )
        session, assessment = self._session_assessment(session_id)
        existing_lease = self.store.get_active_lease(session_id)
        if existing_lease is not None:
            upgrade = self.db.get_pending_approval_for_source(
                source_type=BUILD_UPGRADE_SOURCE, source_id=session_id
            )
            if upgrade is None:
                run_result = self.run(session_id)
                return self.status(session_id) | {"run": run_result}
            requested = str((upgrade.metadata or {}).get("next_tier") or "")
            if tier is None and requested == "custom":
                raise ValueError("A custom upgrade requires an explicit tier")
            selected_upgrade = BuildTier(tier or requested)
            additional = self.config.build.limits(selected_upgrade)
            upgraded = self.store.upgrade_lease(
                session_id,
                selected_upgrade,
                additional,
                approval_request_id=upgrade.id,
            )
            self.db.resolve_approval_request(
                upgrade.id,
                status="approved",
                resolved_by="founder",
                resolution_note=audit_note.strip() or "cumulative build lease upgrade approved",
            )
            self.store.checkpoint(
                session_id,
                phase="planning",
                checkpoint={
                    **session.checkpoint,
                    "lease_id": upgraded.id,
                    "approved_tier": selected_upgrade.value,
                    "upgrade_approval_request_id": upgrade.id,
                    "cumulative_limits": asdict(upgraded.limits),
                },
            )
            run_result = self.run(session_id)
            return self.status(session_id) | {"run": run_result}
        approval = self.db.get_pending_approval_for_source(
            source_type=BUILD_LEASE_SOURCE, source_id=session_id
        )
        if approval is None:
            raise ValueError(f"No pending build lease approval for session {session_id}")
        selected_tier = BuildTier(tier) if tier is not None else assessment.recommended_tier
        if _TIER_ORDER.index(selected_tier) < _TIER_ORDER.index(assessment.recommended_tier):
            if not audit_note.strip():
                raise ValueError("A lower-than-recommended tier requires an audit note")
        limits = self.config.build.limits(selected_tier)
        lease = self.store.create_lease(
            session.id,
            selected_tier,
            limits,
            provider="anthropic",
            model=self.config.build.anthropic_pricing.model,
            approval_request_id=approval.id,
        )
        self.db.resolve_approval_request(
            approval.id,
            status="approved",
            resolved_by="founder",
            resolution_note=audit_note.strip() or "build lease approved",
        )
        self.store.checkpoint(
            session.id,
            phase="planning",
            checkpoint={
                **session.checkpoint,
                "lease_id": lease.id,
                "approved_tier": selected_tier.value,
                "approval_request_id": approval.id,
                "tier_override_note": audit_note.strip(),
            },
        )
        run_result = self.run(session.id)
        return self.status(session.id) | {"run": run_result}

    def deny(
        self,
        session_id: int,
        *,
        note: str = "",
        resolved_by: str = "founder",
    ) -> dict[str, Any]:
        session, _assessment = self._session_assessment(session_id)
        approval = self.db.get_pending_approval_for_source(
            source_type=BUILD_LEASE_SOURCE, source_id=session_id
        )
        if approval is None:
            raise ValueError(f"No pending build lease approval for session {session_id}")
        self.db.resolve_approval_request(
            approval.id,
            status="denied",
            resolved_by=resolved_by,
            resolution_note=note.strip() or "build lease denied",
        )
        if self.store.list_tasks(session_id):
            self.store.cancel_session(session_id)
        self.store.checkpoint(
            session.id,
            phase="complete",
            checkpoint={
                **session.checkpoint,
                "denied": True,
                "denial_note": note.strip(),
                "approval_request_id": approval.id,
            },
        )
        return self.status(session.id)

    def run(self, session_id: int) -> dict[str, Any]:
        if self.orchestrator is not None:
            return self.orchestrator.run_next(session_id)
        session, assessment = self._session_assessment(session_id)
        lease = self.store.get_active_lease(session_id)
        if lease is None:
            return {
                "status": "approval_required",
                "route": "founder",
                "reasons": [],
                "blockers": ["no_active_lease"],
                "local_continued": False,
                "upgrade_request_id": None,
                "lease": None,
            }
        if lease.state == "exhausted":
            return self._continue_after_exhaustion(session, assessment, lease)
        if session.phase == "complete":
            return {
                "status": "complete",
                "route": str(session.checkpoint.get("route") or "local"),
                "reasons": list(session.checkpoint.get("route_reasons") or []),
                "blockers": [],
                "local_continued": False,
                "upgrade_request_id": self._pending_upgrade_id(session.id),
                "lease": _lease_dict(lease),
            }

        attempts = tuple(
            LocalAttempt(
                str(item.get("summary") or ""),
                action=str(item.get("action") or ""),
                outcome=str(item.get("outcome") or "failed"),
            )
            for item in session.checkpoint.get("local_attempts", [])
            if isinstance(item, dict)
        )
        decision = self.router.route_step(
            session, self._step_for(assessment), attempts
        )
        if decision.route == "local":
            result = self._run_local(assessment)
            return self._finish_run(
                session, assessment, decision, result, selected_context=None
            )
        if decision.route == "cloud" and self.cloud_coding_agent_factory is not None:
            selected = self._select_context(assessment)
            authorize = self._egress_authorizer(lease, selected)
            cloud_agent = self.cloud_coding_agent_factory(session.id, authorize)
            result = cloud_agent.run(
                task=assessment.task,
                workspace=assessment.workspace,
                context=_render_context(assessment, selected),
                model=lease.model,
                verify_always=True,
            )
            current_lease = self.store.get_active_lease(session.id) or lease
            if current_lease.state == "exhausted":
                finished = self._finish_run(
                    session,
                    assessment,
                    decision,
                    result,
                    selected_context=selected,
                    force_incomplete=True,
                )
                continuation = self._continue_after_exhaustion(
                    self.store.get_session(session.id) or session,
                    assessment,
                    current_lease,
                )
                return finished | continuation
            return self._finish_run(
                session, assessment, decision, result, selected_context=selected
            )

        local_continued = False
        local_result: dict[str, Any] | None = None
        if not session.checkpoint.get("blocked_local_continuation"):
            local_result = self._run_local(assessment)
            local_continued = True
        blocked_checkpoint = {
            **session.checkpoint,
            "route": decision.route,
            "route_history": _append_route(
                session.checkpoint, "local" if local_continued else decision.route
            ),
            "route_reasons": list(decision.reasons),
            "route_blockers": list(decision.blockers),
            "blocked_local_continuation": local_continued,
            "local_result": _result_summary(local_result) if local_result else None,
        }
        self.store.checkpoint(
            session.id, phase="implementation", checkpoint=blocked_checkpoint
        )
        return {
            "status": "blocked",
            "route": decision.route,
            "reasons": list(decision.reasons),
            "blockers": list(decision.blockers),
            "local_continued": local_continued,
            "result": local_result,
            "upgrade_request_id": None,
            "lease": _lease_dict(self.store.get_active_lease(session.id) or lease),
        }

    def status(self, session_id: int) -> dict[str, Any]:
        session, assessment = self._session_assessment(session_id)
        lease = self.store.get_active_lease(session_id)
        approval = self.db.get_pending_approval_for_source(
            source_type=BUILD_LEASE_SOURCE, source_id=session_id
        )
        usage_events = self.store.list_session_usage(session_id) if lease else []
        tasks = self.store.list_tasks(session_id)
        task_runs = [
            run
            for task in tasks
            for run in self.store.list_task_runs(task_id=task.id)
        ]
        return {
            "assessment": _assessment_dict(assessment),
            "session": _session_dict(session),
            "lease": _lease_dict(lease) if lease else None,
            "recommended_limits": asdict(
                self.config.build.limits(assessment.recommended_tier)
            ),
            "usage": _usage_dict(lease, usage_events),
            "usage_events": [_usage_event_dict(item) for item in usage_events],
            "route_counts": _route_counts(session.checkpoint),
            "approval_request_id": approval.id if approval else None,
            "upgrade_request_id": self._pending_upgrade_id(session_id),
            "tasks": [_task_dict(task) for task in tasks],
            "task_runs": [_task_run_dict(run) for run in task_runs],
            "artifacts": [asdict(item) for item in self.store.list_artifacts(session_id)],
            "worker": (
                self.execution_manager.status(session_id)
                if self.execution_manager is not None
                else None
            ),
        }

    def start(self, session_id: int) -> dict[str, Any]:
        if self.execution_manager is None:
            raise ValueError("Durable build background execution is not configured")
        return self.execution_manager.start(session_id)

    def pause(self, session_id: int) -> dict[str, Any]:
        if self.execution_manager is None:
            session = self.store.pause_session(session_id)
            return {"status": session.status, "session_id": session_id}
        return self.execution_manager.pause(session_id)

    def resume(self, session_id: int) -> dict[str, Any]:
        if self.execution_manager is None:
            session = self.store.resume_session(session_id)
            return {"status": session.status, "session_id": session_id}
        return self.execution_manager.resume(session_id)

    def cancel(self, session_id: int) -> dict[str, Any]:
        if self.execution_manager is None:
            session = self.store.cancel_session(session_id)
            return {"status": session.status, "session_id": session_id}
        return self.execution_manager.cancel(session_id)

    def execute_cloud_task(
        self,
        task: Any,
        assessment: BuildAssessment,
        lease: BuildLease,
    ) -> dict[str, Any]:
        if self.cloud_coding_agent_factory is None:
            raise ValueError("Anthropic cloud build execution is not configured")
        if lease.provider != "anthropic":
            raise ValueError(f"Unsupported cloud coding provider: {lease.provider}")
        selected = self._select_context(assessment)
        authorize = self._egress_authorizer(lease, selected)
        cloud_agent = self.cloud_coding_agent_factory(task.session_id, authorize)
        instructions = str(task.payload.get("instructions") or "").strip()
        return cloud_agent.run(
            task=(
                f"{task.title}\n\nProduct objective: {assessment.task}"
                + (f"\n\nTask instructions: {instructions}" if instructions else "")
            ),
            workspace=assessment.workspace,
            context=_render_context(assessment, selected),
            model=lease.model,
            verify_always=task.phase == "implementation",
        )

    def review_context(self, session_id: int) -> str:
        """Return the same bounded, secret-filtered project context used by cloud builds."""
        _session, assessment = self._session_assessment(session_id)
        return _render_context(assessment, self._select_context(assessment))

    def list_sessions(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return [self.status(session.id) for session in self.store.list_sessions(limit=limit)]

    def _session_assessment(
        self, session_id: int
    ) -> tuple[BuildSession, BuildAssessment]:
        session = self.store.get_session(session_id)
        if session is None:
            raise ValueError(f"Build session not found: {session_id}")
        assessment = self.store.get_assessment(session.assessment_id)
        if assessment is None:
            raise ValueError(f"Build assessment not found: {session.assessment_id}")
        return session, assessment

    def _run_local(self, assessment: BuildAssessment) -> dict[str, Any]:
        return self.local_coding_agent.run(
            task=assessment.task,
            workspace=assessment.workspace,
            context=(
                f"Acceptance criteria:\n{assessment.acceptance}"
                if assessment.acceptance
                else ""
            ),
            verify_always=True,
        )

    def _finish_run(
        self,
        session: BuildSession,
        assessment: BuildAssessment,
        decision: RouteDecision,
        result: dict[str, Any],
        *,
        selected_context: SelectedContext | None,
        force_incomplete: bool = False,
    ) -> dict[str, Any]:
        verified = not (
            isinstance(result.get("auto_verification"), dict)
            and result["auto_verification"].get("ok") is False
        )
        complete = bool(result.get("ok")) and verified and not force_incomplete
        checkpoint = {
            **session.checkpoint,
            "route": decision.route,
            "route_history": _append_route(session.checkpoint, decision.route),
            "route_reasons": list(decision.reasons),
            "route_blockers": list(decision.blockers),
            "result": _result_summary(result),
            "verification": result.get("auto_verification"),
        }
        if selected_context is not None:
            checkpoint["selected_context"] = [
                {
                    "path": item.path,
                    "start_line": item.start_line,
                    "end_line": item.end_line,
                    "content_hash": item.content_hash,
                    "truncated": item.truncated,
                    "utf8_bytes": item.utf8_bytes,
                }
                for item in selected_context.excerpts
            ]
        if decision.route == "local" and not result.get("ok"):
            attempts = list(checkpoint.get("local_attempts") or [])
            attempts.append(
                {
                    "summary": str(result.get("error") or result.get("status") or "failed")[:300],
                    "action": str(result.get("response") or assessment.task)[:500],
                    "outcome": "failed",
                }
            )
            checkpoint["local_attempts"] = attempts[-8:]
        updated = self.store.checkpoint(
            session.id,
            phase="complete" if complete else "implementation",
            checkpoint=checkpoint,
        )
        payload = {
            "status": "complete" if complete else str(result.get("status") or "failed"),
            "route": decision.route,
            "reasons": list(decision.reasons),
            "blockers": list(decision.blockers),
            "local_continued": False,
            "result": result,
            "upgrade_request_id": self._pending_upgrade_id(session.id),
            "lease": _lease_dict(self.store.get_active_lease(session.id)),
        }
        self._sync_work_item(updated, payload)
        return payload

    def _continue_after_exhaustion(
        self,
        session: BuildSession,
        assessment: BuildAssessment,
        lease: BuildLease,
    ) -> dict[str, Any]:
        checkpoint = dict(session.checkpoint)
        marker = f"lease:{lease.version}"
        local_continued = checkpoint.get("exhaustion_local_continuation") != marker
        local_result = self._run_local(assessment) if local_continued else None
        if local_continued:
            checkpoint["exhaustion_local_continuation"] = marker
            checkpoint["local_continuation_result"] = _result_summary(local_result)
        upgrade_id = self._ensure_upgrade_request(
            session, assessment, lease, checkpoint
        )
        checkpoint["upgrade_request_id"] = upgrade_id
        checkpoint["route"] = "founder"
        checkpoint["route_history"] = _append_route(
            checkpoint, "local" if local_continued else "founder"
        )
        checkpoint["route_blockers"] = ["lease_exhausted"]
        updated = self.store.checkpoint(
            session.id, phase="implementation", checkpoint=checkpoint
        )
        payload = {
            "status": "exhausted",
            "route": "founder",
            "reasons": list(assessment.cloud_reasons),
            "blockers": ["lease_exhausted"],
            "local_continued": local_continued,
            "result": local_result,
            "upgrade_request_id": upgrade_id,
            "lease": _lease_dict(lease),
        }
        self._sync_work_item(updated, payload)
        return payload

    def _ensure_upgrade_request(
        self,
        session: BuildSession,
        assessment: BuildAssessment,
        lease: BuildLease,
        checkpoint: dict[str, Any],
    ) -> int:
        existing = self.db.get_pending_approval_for_source(
            source_type=BUILD_UPGRADE_SOURCE, source_id=session.id
        )
        if existing is not None:
            return existing.id
        try:
            next_tier = _TIER_ORDER[_TIER_ORDER.index(lease.tier) + 1].value
        except IndexError:
            next_tier = "custom"
        summary = self.budget.request_upgrade_summary(
            session.id, "Current lease cannot fit the next eligible cloud turn."
        )
        approval, _created = self.db.ensure_approval_request(
            source_type=BUILD_UPGRADE_SOURCE,
            source_id=session.id,
            title=f"Upgrade exhausted {lease.tier.value} build lease",
            detail=(
                f"Build session {session.id} exhausted lease version {lease.version}. "
                f"Requested next tier: {next_tier}. Local work may continue."
            ),
            action="build.lease.upgrade",
            target=assessment.workspace,
            permission_tier="L3_EXTERNAL_ACTION",
            authority_decision="approval_required",
            authority={"reason": "Only the founder may enlarge a paid build envelope."},
            requested_by="build.budget",
            metadata={
                "session_id": session.id,
                "lease_id": lease.id,
                "lease_version": lease.version,
                "current_tier": lease.tier.value,
                "next_tier": next_tier,
                "budget": summary,
                "completed_work": checkpoint.get("result"),
                "remaining_work": assessment.task,
            },
        )
        return approval.id

    def _pending_upgrade_id(self, session_id: int) -> int | None:
        pending = self.db.get_pending_approval_for_source(
            source_type=BUILD_UPGRADE_SOURCE, source_id=session_id
        )
        return pending.id if pending else None

    def _select_context(self, assessment: BuildAssessment) -> SelectedContext:
        root = Path(assessment.workspace)
        candidates = [
            root / str(relative)
            for relative in assessment.evidence.get("scanned_paths", [])
        ]
        return BuildContextSelector(root).select(
            task=f"{assessment.task}\n{assessment.acceptance}", candidates=candidates
        )

    def _egress_authorizer(
        self, lease: BuildLease, selected: SelectedContext
    ) -> Callable[[int, dict[str, Any]], bool]:
        def authorize(session_id: int, summary: dict[str, Any]) -> bool:
            current = self.store.get_active_lease(session_id)
            if current is None or current.id != lease.id:
                return False
            request_id = str(summary.get("usage_request_id") or "")
            if not request_id:
                return False
            request = EgressRequest(
                request_id=request_id,
                data_class=DataClass.SOURCE_CODE,
                vendor="anthropic",
                purpose="lease-scoped build reasoning and coding",
                byte_estimate=selected.total_bytes,
            )
            decision = authorize_build_egress(
                self.db, self.egress_policy, request, lease=current
            )
            return decision.allowed

        return authorize

    @staticmethod
    def _step_for(assessment: BuildAssessment) -> BuildStep:
        text = f"{assessment.task} {assessment.acceptance}".lower()
        domains = tuple(
            domain
            for domain in (
                "authentication",
                "authorization",
                "billing",
                "payments",
                "migration",
                "security",
                "release",
            )
            if domain in text
            or any(domain in rule for rule in assessment.floor_rules)
        )
        if domains:
            return BuildStep(
                kind="review", risk="high", critical_domains=domains
            )
        if any(word in text for word in ("architecture", "cross-module", "cross module")):
            return BuildStep(
                kind="architecture", risk="high", cross_module=True
            )
        if any(word in text for word in ("debug", "failing", "failure")):
            return BuildStep(kind="debug", risk="high")
        return BuildStep(kind="edit", risk="low")

    def _sync_work_item(
        self, session: BuildSession, result: dict[str, Any]
    ) -> None:
        if session.work_item_id is None or self.db.get_work_item(session.work_item_id) is None:
            return
        status = "done" if result.get("status") == "complete" else "approval_required"
        self.db.update_work_item(
            session.work_item_id,
            status=status,
            result={"build_session_id": session.id, "build": result},
            error="" if status == "done" else str(result.get("status") or "")[:400],
        )


def _assessment_dict(assessment: BuildAssessment) -> dict[str, Any]:
    return {
        "id": assessment.id,
        "task": assessment.task,
        "acceptance": assessment.acceptance,
        "workspace": assessment.workspace,
        "repo_fingerprint": assessment.repo_fingerprint,
        "deterministic_score": assessment.deterministic_score,
        "local_adjustment": assessment.local_adjustment,
        "final_score": assessment.final_score,
        "confidence": assessment.confidence,
        "recommended_tier": assessment.recommended_tier.value,
        "dimensions": assessment.dimensions,
        "floor_rules": list(assessment.floor_rules),
        "evidence": assessment.evidence,
        "unknowns": list(assessment.unknowns),
        "local_work": list(assessment.local_work),
        "cloud_reasons": list(assessment.cloud_reasons),
        "created_at": assessment.created_at,
    }


def _session_dict(session: BuildSession) -> dict[str, Any]:
    return {
        "id": session.id,
        "assessment_id": session.assessment_id,
        "work_item_id": session.work_item_id,
        "workspace": session.workspace,
        "repo_fingerprint": session.repo_fingerprint,
        "phase": session.phase,
        "status": session.status,
        "checkpoint": session.checkpoint,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


def _lease_dict(lease: BuildLease | None) -> dict[str, Any] | None:
    if lease is None:
        return None
    return {
        "id": lease.id,
        "session_id": lease.session_id,
        "version": lease.version,
        "tier": lease.tier.value,
        "provider": lease.provider,
        "model": lease.model,
        "state": lease.state,
        "approval_request_id": lease.approval_request_id,
        "limits": asdict(lease.limits),
        "actual_input_tokens": lease.actual_input_tokens,
        "actual_output_tokens": lease.actual_output_tokens,
        "actual_microdollars": lease.actual_microdollars,
        "reserved_input_tokens": lease.reserved_input_tokens,
        "reserved_output_tokens": lease.reserved_output_tokens,
        "reserved_microdollars": lease.reserved_microdollars,
        "cloud_turns": lease.cloud_turns,
        "started_at": lease.started_at,
        "expires_at": lease.expires_at,
    }


def _task_dict(task: Any) -> dict[str, Any]:
    return {
        "id": task.id,
        "session_id": task.session_id,
        "phase": task.phase,
        "position": task.position,
        "kind": task.kind.value,
        "title": task.title,
        "payload": task.payload,
        "dependencies": list(task.dependencies),
        "acceptance": task.acceptance,
        "idempotency_key": task.idempotency_key,
        "status": task.status.value,
        "max_attempts": task.max_attempts,
        "attempt_count": task.attempt_count,
        "active_run_id": task.active_run_id,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def _task_run_dict(run: Any) -> dict[str, Any]:
    return {
        "id": run.id,
        "task_id": run.task_id,
        "session_id": run.session_id,
        "attempt_number": run.attempt_number,
        "worker_id": run.worker_id,
        "backend": run.backend,
        "command": list(run.command),
        "pid": run.pid,
        "status": run.status.value,
        "result": run.result,
        "error": run.error,
        "log_path": run.log_path,
        "artifact_ids": list(run.artifact_ids),
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


def _usage_dict(lease: BuildLease | None, events: Sequence[Any]) -> dict[str, Any]:
    if lease is None:
        return {
            "authorized_input_tokens": 0,
            "authorized_output_tokens": 0,
            "authorized_microdollars": 0,
            "authorized_cloud_turns": 0,
            "actual_input_tokens": 0,
            "actual_output_tokens": 0,
            "actual_microdollars": 0,
            "reserved_input_tokens": 0,
            "reserved_output_tokens": 0,
            "reserved_microdollars": 0,
            "cloud_turns": 0,
            "remaining_input_tokens": 0,
            "remaining_output_tokens": 0,
            "remaining_microdollars": 0,
            "remaining_cloud_turns": 0,
            "cache_write_5m_tokens": 0,
            "cache_write_1h_tokens": 0,
            "cache_read_tokens": 0,
        }
    committed_input = lease.actual_input_tokens + lease.reserved_input_tokens
    committed_output = lease.actual_output_tokens + lease.reserved_output_tokens
    committed_cost = lease.actual_microdollars + lease.reserved_microdollars
    return {
        "authorized_input_tokens": lease.limits.input_tokens,
        "authorized_output_tokens": lease.limits.output_tokens,
        "authorized_microdollars": lease.limits.dollar_micro,
        "authorized_cloud_turns": lease.limits.cloud_turns,
        "actual_input_tokens": lease.actual_input_tokens,
        "actual_output_tokens": lease.actual_output_tokens,
        "actual_microdollars": lease.actual_microdollars,
        "reserved_input_tokens": lease.reserved_input_tokens,
        "reserved_output_tokens": lease.reserved_output_tokens,
        "reserved_microdollars": lease.reserved_microdollars,
        "cloud_turns": lease.cloud_turns,
        "remaining_input_tokens": max(0, lease.limits.input_tokens - committed_input),
        "remaining_output_tokens": max(0, lease.limits.output_tokens - committed_output),
        "remaining_microdollars": max(0, lease.limits.dollar_micro - committed_cost),
        "remaining_cloud_turns": max(0, lease.limits.cloud_turns - lease.cloud_turns),
        "cache_write_5m_tokens": sum(item.cache_write_5m_tokens for item in events),
        "cache_write_1h_tokens": sum(item.cache_write_1h_tokens for item in events),
        "cache_read_tokens": sum(item.cache_read_tokens for item in events),
    }


def _append_route(checkpoint: dict[str, Any], route: str) -> list[str]:
    history = [
        str(item)
        for item in checkpoint.get("route_history", [])
        if str(item) in {"local", "cloud", "founder"}
    ]
    history.append(route)
    return history[-100:]


def _route_counts(checkpoint: dict[str, Any]) -> dict[str, int]:
    history = checkpoint.get("route_history", [])
    return {
        route: sum(1 for item in history if item == route)
        for route in ("local", "cloud", "founder")
    }


def _usage_event_dict(event: Any) -> dict[str, Any]:
    return {
        "id": event.id,
        "lease_id": event.lease_id,
        "request_id": event.request_id,
        "turn_number": event.turn_number,
        "status": event.status,
        "input_tokens": event.input_tokens,
        "cache_write_5m_tokens": event.cache_write_5m_tokens,
        "cache_write_1h_tokens": event.cache_write_1h_tokens,
        "cache_read_tokens": event.cache_read_tokens,
        "output_tokens": event.output_tokens,
        "reserved_microdollars": event.reserved_microdollars,
        "settled_microdollars": event.settled_microdollars,
        "provider": event.pricing.provider,
        "model": event.pricing.model,
        "pricing_review_after": event.pricing.review_after,
        "created_at": event.created_at,
        "settled_at": event.settled_at,
    }


def _result_summary(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "ok": bool(result.get("ok")),
        "status": str(result.get("status") or ""),
        "error": str(result.get("error") or "")[:400],
        "model": result.get("model"),
        "provider": result.get("provider"),
        "rounds": result.get("rounds"),
        "changed_files": list(result.get("changed_files") or []),
        "workspace_changes": result.get("workspace_changes"),
        "auto_verification": result.get("auto_verification"),
        "response": str(result.get("response") or "")[:2000],
    }


def _render_context(
    assessment: BuildAssessment, selected: SelectedContext
) -> str:
    parts = [
        f"Acceptance criteria:\n{assessment.acceptance or '(not supplied)'}",
        "Selected local context (only these excerpts were approved for this turn):",
    ]
    for item in selected.excerpts:
        parts.append(
            f"--- {item.path}:{item.start_line}-{item.end_line} sha256={item.content_hash} ---\n"
            f"{item.content}"
        )
    return "\n\n".join(parts)
