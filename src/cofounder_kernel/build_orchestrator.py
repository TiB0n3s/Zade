"""Durable, local-first planning and single-task build execution."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .build_routing import BuildRouter, BuildStep, LocalAttempt, RouteDecision
from .build_store import BuildStore
from .build_types import (
    BuildAssessment,
    BuildLease,
    BuildTask,
    BuildTaskKind,
    BuildTaskStatus,
)
from .toolchain_profiles import ToolchainRegistry


TaskExecutor = Callable[[BuildTask, BuildAssessment], dict[str, Any]]
CloudTaskExecutor = Callable[[BuildTask, BuildAssessment, BuildLease], dict[str, Any]]
CancellationCallback = Callable[[int], None]


@dataclass(frozen=True)
class _TaskSpec:
    phase: str
    kind: BuildTaskKind
    title: str
    payload: dict[str, Any]
    max_attempts: int = 1


class BuildPlanner:
    """Materialize one deterministic, idempotent lifecycle graph per session."""

    def __init__(
        self,
        *,
        store: BuildStore,
        toolchains: ToolchainRegistry,
        ios_workflow: str = "ios.yml",
    ):
        self.store = store
        self.toolchains = toolchains
        self.ios_workflow = ios_workflow.strip() or "ios.yml"

    def plan(self, session_id: int, *, profile_id: str | None = None) -> list[BuildTask]:
        session = self.store.get_session(session_id)
        if session is None:
            raise ValueError(f"Build session not found: {session_id}")
        assessment = self.store.get_assessment(session.assessment_id)
        if assessment is None:
            raise ValueError(f"Build assessment not found: {session.assessment_id}")
        profile = (
            self.toolchains.profile(profile_id, session.workspace)
            if profile_id
            else self.toolchains.detect(session.workspace)
        )
        previous: BuildTask | None = None
        planned: list[BuildTask] = []
        for spec in self._specs(
            assessment, profile.id, ios_workflow=self.ios_workflow
        ):
            task = self.store.create_task(
                session_id,
                phase=spec.phase,
                kind=spec.kind,
                title=spec.title,
                payload=spec.payload,
                dependencies=(previous.id,) if previous else (),
                acceptance={"product_acceptance": assessment.acceptance},
                idempotency_key=f"lifecycle:{spec.phase}",
                max_attempts=spec.max_attempts,
            )
            planned.append(task)
            previous = task
        return planned

    @staticmethod
    def _specs(
        assessment: BuildAssessment,
        profile_id: str,
        *,
        ios_workflow: str = "ios.yml",
    ) -> tuple[_TaskSpec, ...]:
        shared = {
            "objective": assessment.task,
            "acceptance": assessment.acceptance,
            "toolchain_profile": profile_id,
        }
        requirements_contract = _output_contract("requirements.md")
        architecture_contract = _output_contract("architecture.md")
        planning_contract = _output_contract("plan.md")
        implementation_step = {
            "kind": "cross_cutting" if assessment.final_score >= 45 else "edit",
            "risk": "high" if assessment.final_score >= 45 else "medium",
            "cross_module": assessment.dimensions.get("change_breadth", 0) >= 6,
            "regression_risk": assessment.final_score >= 45,
            "critical_domains": list(_critical_domains(assessment.task)),
        }
        return (
            _TaskSpec(
                "discovery",
                BuildTaskKind.CHECKPOINT,
                "Record assessed scope and constraints",
                shared | {"route": "local", "operation": "assessment_checkpoint"},
            ),
            _TaskSpec(
                "requirements",
                BuildTaskKind.AGENT,
                "Define product requirements and acceptance criteria",
                shared
                | {
                    "route": "local",
                    "instructions": (
                        "Produce testable product requirements only in "
                        ".zade/build/requirements.md. Do not edit product code."
                    ),
                    "output_contract": requirements_contract,
                },
                max_attempts=2,
            ),
            _TaskSpec(
                "architecture",
                BuildTaskKind.AGENT,
                "Design architecture and resolve cross-module tradeoffs",
                shared
                | {
                    "route": "adaptive",
                    "allow_local_before_cloud": True,
                    "step": {
                        "kind": "architecture",
                        "risk": "high" if assessment.final_score >= 35 else "medium",
                        "cross_module": assessment.dimensions.get("change_breadth", 0) >= 4,
                    },
                    "instructions": "Produce an implementation-ready architecture decision.",
                    "output_contract": architecture_contract,
                },
                max_attempts=2,
            ),
            _TaskSpec(
                "planning",
                BuildTaskKind.AGENT,
                "Create the phased implementation plan",
                shared
                | {
                    "route": "local",
                    "instructions": (
                        "Create dependency-ordered build steps only in "
                        ".zade/build/plan.md. Do not edit product code."
                    ),
                    "output_contract": planning_contract,
                },
                max_attempts=2,
            ),
            _TaskSpec(
                "implementation",
                BuildTaskKind.AGENT,
                "Implement the product increment",
                shared
                | {
                    "route": "adaptive",
                    "allow_local_before_cloud": True,
                    "step": implementation_step,
                    "instructions": "Implement the approved increment and retain local evidence.",
                },
                max_attempts=2,
            ),
            _TaskSpec(
                "verification",
                BuildTaskKind.VERIFICATION,
                "Run the integrated product verification plan",
                shared | {"route": "local"},
            ),
            _TaskSpec(
                "review",
                BuildTaskKind.REVIEW,
                "Review correctness, security, and release risk",
                shared | {"route": "local"},
            ),
            _TaskSpec(
                "release",
                (
                    BuildTaskKind.GITHUB
                    if profile_id == "flutter-mobile"
                    else BuildTaskKind.CHECKPOINT
                ),
                "Assemble release evidence and readiness decision",
                shared
                | {
                    "route": "local",
                    "operation": (
                        "verify_workflow"
                        if profile_id == "flutter-mobile"
                        else "release_checkpoint"
                    ),
                    "workflow": ios_workflow if profile_id == "flutter-mobile" else "",
                },
            ),
            _TaskSpec(
                "complete",
                BuildTaskKind.CHECKPOINT,
                "Complete the governed build session",
                shared | {"route": "local", "operation": "complete"},
            ),
        )


class BuildOrchestrator:
    """Claim and execute exactly one ready task through typed dispatch seams."""

    def __init__(
        self,
        *,
        store: BuildStore,
        planner: BuildPlanner,
        router: BuildRouter,
        local_agent: Any,
        cloud_executor: CloudTaskExecutor | None = None,
        command_executor: TaskExecutor | None = None,
        verification_executor: TaskExecutor | None = None,
        github_executor: TaskExecutor | None = None,
        review_executor: TaskExecutor | None = None,
        cancellation_callback: CancellationCallback | None = None,
    ):
        self.store = store
        self.planner = planner
        self.router = router
        self.local_agent = local_agent
        self.cloud_executor = cloud_executor
        self.command_executor = command_executor
        self.verification_executor = verification_executor
        self.github_executor = github_executor
        self.review_executor = review_executor
        self.cancellation_callback = cancellation_callback

    def ensure_plan(self, session_id: int, *, profile_id: str | None = None) -> list[BuildTask]:
        tasks = self.store.list_tasks(session_id)
        return tasks if tasks else self.planner.plan(session_id, profile_id=profile_id)

    def run_next(self, session_id: int, *, worker_id: str = "foreground") -> dict[str, Any]:
        session = self.store.get_session(session_id)
        if session is None:
            raise ValueError(f"Build session not found: {session_id}")
        if session.status != "active":
            return {"status": session.status, "session_id": session_id}
        tasks = self.ensure_plan(session_id)
        ready = self.store.ready_tasks(session_id)
        if not ready:
            return self._idle_result(session_id, tasks)
        task = ready[0]
        assessment = self.store.get_assessment(session.assessment_id)
        if assessment is None:
            raise ValueError(f"Build assessment not found: {session.assessment_id}")
        route = self._route_task(session, task)
        if route.route == "founder":
            return {
                "status": "blocked",
                "session_id": session_id,
                "task_id": task.id,
                "route": "founder",
                "reasons": list(route.reasons),
                "blockers": list(route.blockers),
            }
        run = self.store.claim_task(task.id, worker_id=worker_id, backend=route.route)
        try:
            result = self._dispatch(task, assessment, route)
        except Exception as exc:
            result = {
                "ok": False,
                "status": "executor_error",
                "error": str(exc),
                "exception_type": type(exc).__name__,
            }
        result = self._apply_evidence_gate(task, assessment, result)
        current_session = self.store.get_session(session_id)
        cancelled = current_session is not None and current_session.status in {
            "cancelling",
            "cancelled",
            "quarantined",
        }
        succeeded = bool(result.get("ok")) and not cancelled
        terminal = (
            BuildTaskStatus.CANCELLED
            if cancelled
            else BuildTaskStatus.SUCCEEDED
            if succeeded
            else BuildTaskStatus.FAILED
        )
        finished = self.store.finish_task_run(
            run.id,
            status=terminal,
            result=result,
            error=str(result.get("error") or ""),
        )
        if terminal is BuildTaskStatus.SUCCEEDED:
            self.store.checkpoint(
                session_id,
                phase=task.phase,
                checkpoint={
                    **session.checkpoint,
                    "last_task_id": task.id,
                    "last_run_id": finished.id,
                    "last_route": route.route,
                    "route_history": [
                        *list(session.checkpoint.get("route_history") or []),
                        route.route,
                    ][-100:],
                },
            )
        updated_session = self.store.get_session(session_id)
        status = (
            "complete"
            if updated_session and updated_session.status == "complete"
            else terminal.value
        )
        return {
            "status": status,
            "session_id": session_id,
            "task_id": task.id,
            "run_id": finished.id,
            "phase": task.phase,
            "route": route.route,
            "reasons": list(route.reasons),
            "blockers": list(route.blockers),
            "error": finished.error,
            "result": result,
        }

    def cancel(self, session_id: int) -> None:
        if self.cancellation_callback is not None:
            self.cancellation_callback(session_id)

    def _route_task(self, session: Any, task: BuildTask) -> RouteDecision:
        requested = str(task.payload.get("route") or "local").strip().lower()
        provider = str(task.payload.get("provider") or "anthropic").strip().lower()
        if requested == "local":
            return RouteDecision("local", ("task_declared_local",))
        if requested == "cloud":
            lease = self.store.get_active_lease(session.id, provider=provider)
            if lease is None or lease.provider.strip().lower() != provider:
                return RouteDecision(
                    "founder",
                    ("task_declared_cloud",),
                    (f"no_active_{provider}_lease",),
                )
            if lease.state not in {"active", "warning"}:
                return RouteDecision(
                    "founder",
                    ("task_declared_cloud",),
                    (f"lease_{lease.state}",),
                )
            if self.cloud_executor is None:
                return RouteDecision(
                    "founder", ("task_declared_cloud",), ("cloud_executor_unavailable",)
                )
            return RouteDecision("cloud", ("task_declared_cloud",), lease_id=lease.id)
        if requested != "adaptive":
            return RouteDecision("founder", (), ("invalid_task_route",))
        step_payload = task.payload.get("step")
        values = step_payload if isinstance(step_payload, dict) else {}
        decision = self.router.route_step(
            session,
            BuildStep(
                kind=str(values.get("kind") or task.kind.value),
                risk=str(values.get("risk") or "low"),
                description=task.title,
                cross_module=bool(values.get("cross_module")),
                regression_risk=bool(values.get("regression_risk")),
                critical_domains=tuple(values.get("critical_domains") or ()),
                local_capability_exceeded=bool(values.get("local_capability_exceeded")),
            ),
            self._local_attempts(task),
        )
        if (
            decision.route == "founder"
            and bool(task.payload.get("allow_local_before_cloud"))
            and set(decision.blockers) <= {"no_active_lease"}
        ):
            return RouteDecision(
                "local", (*decision.reasons, "local_attempt_before_cloud_approval")
            )
        return decision

    def _dispatch(
        self, task: BuildTask, assessment: BuildAssessment, route: RouteDecision
    ) -> dict[str, Any]:
        if task.kind is BuildTaskKind.CHECKPOINT:
            return {"ok": True, "status": "ok", "checkpoint": task.payload}
        if route.route == "cloud":
            provider = str(task.payload.get("provider") or "anthropic")
            lease = self.store.get_active_lease(task.session_id, provider=provider)
            if lease is None or self.cloud_executor is None:
                raise RuntimeError("Cloud task lost its approved lease before execution")
            return self.cloud_executor(task, assessment, lease)
        executor = {
            BuildTaskKind.COMMAND: self.command_executor,
            BuildTaskKind.VERIFICATION: self.verification_executor,
            BuildTaskKind.GITHUB: self.github_executor,
            BuildTaskKind.REVIEW: self.review_executor,
        }.get(task.kind)
        if executor is not None:
            return executor(task, assessment)
        if task.kind in {BuildTaskKind.AGENT, BuildTaskKind.REVIEW}:
            instructions = str(task.payload.get("instructions") or "").strip()
            return self.local_agent.run(
                task=(
                    f"{task.title}\n\nProduct objective: {assessment.task}"
                    + (f"\n\nTask instructions: {instructions}" if instructions else "")
                ),
                workspace=assessment.workspace,
                context=self._task_context(task, assessment),
                verify_always=task.phase == "implementation",
                write_allowlist=_write_allowlist(task),
            )
        return {
            "ok": False,
            "status": "handler_unavailable",
            "error": f"No executor is configured for {task.kind.value} tasks",
        }

    @staticmethod
    def _apply_evidence_gate(
        task: BuildTask, assessment: BuildAssessment, result: dict[str, Any]
    ) -> dict[str, Any]:
        if not bool(result.get("ok")):
            return result
        if task.kind not in {BuildTaskKind.AGENT, BuildTaskKind.REVIEW}:
            return result

        gated = dict(result)
        contract = task.payload.get("output_contract")
        if isinstance(contract, dict):
            allowed = {
                _normalize_relative_path(item)
                for item in contract.get("allowed_write_paths", [])
            }
            required = {
                _normalize_relative_path(item)
                for item in contract.get("required_artifacts", [])
            }
            actual = _result_change_paths(result)
            unexpected = sorted(actual - allowed)
            missing = sorted(
                item
                for item in required
                if not (Path(assessment.workspace) / Path(item)).is_file()
            )
            if unexpected or missing:
                gated.update(
                    {
                        "ok": False,
                        "status": "phase_contract_failed",
                        "error": "Build phase output contract was not satisfied.",
                        "evidence_gate": {
                            "unexpected_changes": unexpected,
                            "missing_artifacts": missing,
                            "allowed_write_paths": sorted(allowed),
                        },
                    }
                )
                return gated

        verification = result.get("auto_verification")
        if isinstance(verification, dict) and verification.get("ok") is False:
            gated.update(
                {
                    "ok": False,
                    "status": "verification_failed",
                    "error": "Kernel mechanical verification failed.",
                }
            )
            return gated
        if task.phase == "implementation" and not (
            isinstance(verification, dict) and verification.get("ok") is True
        ):
            gated.update(
                {
                    "ok": False,
                    "status": "verification_required",
                    "error": "Implementation requires a positive kernel mechanical verification result.",
                }
            )
            return gated

        verifier = result.get("verifier_review")
        if isinstance(verifier, dict) and str(verifier.get("verdict") or "").lower() == "fail":
            notes = str(verifier.get("notes") or "Fresh-context verification failed.")
            gated.update(
                {
                    "ok": False,
                    "status": "verifier_rejected",
                    "error": notes[:2000],
                }
            )
        return gated

    def _local_attempts(self, task: BuildTask) -> tuple[LocalAttempt, ...]:
        return tuple(
            LocalAttempt(
                summary=run.error or str(run.result.get("status") or "failed"),
                action=str(run.result.get("response") or task.title),
                outcome="failed",
            )
            for run in self.store.list_task_runs(task_id=task.id)
            if run.status in {BuildTaskStatus.FAILED, BuildTaskStatus.INTERRUPTED}
        )

    @staticmethod
    def _task_context(task: BuildTask, assessment: BuildAssessment) -> str:
        payload = json.dumps(task.payload, sort_keys=True, ensure_ascii=True)
        return (
            f"Build phase: {task.phase}\n"
            f"Acceptance criteria: {assessment.acceptance or 'Not specified'}\n"
            f"Task data: {payload[:8000]}"
        )

    def _idle_result(self, session_id: int, tasks: list[BuildTask]) -> dict[str, Any]:
        session = self.store.get_session(session_id)
        if session is None:
            raise ValueError(f"Build session not found: {session_id}")
        if session.status != "active":
            return {"status": session.status, "session_id": session_id}
        failed = [task for task in tasks if task.status is BuildTaskStatus.FAILED]
        if failed:
            return {
                "status": "blocked",
                "session_id": session_id,
                "blockers": ["failed_dependency"],
                "failed_task_ids": [task.id for task in failed],
            }
        if tasks and all(task.status is BuildTaskStatus.SUCCEEDED for task in tasks):
            self.store.checkpoint(
                session_id,
                phase="complete",
                checkpoint={**session.checkpoint, "all_tasks_succeeded": True},
            )
            return {"status": "complete", "session_id": session_id}
        return {"status": "idle", "session_id": session_id}


def _critical_domains(task: str) -> tuple[str, ...]:
    lowered = task.lower()
    domains = (
        "authentication",
        "authorization",
        "billing",
        "migration",
        "payments",
        "release",
        "security",
    )
    return tuple(item for item in domains if item in lowered)


def _output_contract(filename: str) -> dict[str, list[str]]:
    path = f".zade/build/{filename}"
    return {"required_artifacts": [path], "allowed_write_paths": [path]}


def _normalize_relative_path(value: Any) -> str:
    normalized = str(value or "").replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Invalid build output path: {value!r}")
    return path.as_posix()


def _result_change_paths(result: dict[str, Any]) -> set[str]:
    raw: list[Any] = list(result.get("changed_files") or [])
    changes = result.get("workspace_changes")
    if isinstance(changes, dict):
        for key in ("added", "modified", "deleted"):
            raw.extend(changes.get(key) or [])
    return {_normalize_relative_path(item) for item in raw}


def _write_allowlist(task: BuildTask) -> tuple[str, ...] | None:
    contract = task.payload.get("output_contract")
    if not isinstance(contract, dict):
        return None
    return tuple(
        _normalize_relative_path(item)
        for item in contract.get("allowed_write_paths", [])
    )
