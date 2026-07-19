from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from cofounder_kernel.build_orchestrator import BuildOrchestrator, BuildPlanner
from cofounder_kernel.build_routing import BuildRouter
from cofounder_kernel.build_store import BuildStore
from cofounder_kernel.build_types import BuildAssessment, BuildTaskKind, BuildTaskStatus, BuildTier
from cofounder_kernel.build_workers import BuildExecutionManager
from cofounder_kernel.db import KernelDatabase
from cofounder_kernel.toolchain_profiles import ToolchainRegistry


class BlockingAgent:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def run(self, **_kwargs: Any) -> dict[str, Any]:
        self.entered.set()
        self.release.wait(timeout=5)
        return {
            "ok": True,
            "status": "ok",
            "response": "done",
            "auto_verification": {"ok": True},
            "verifier_review": {"verdict": "pass", "notes": ""},
        }


class SuccessAgent:
    def run(self, **_kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "status": "ok",
            "response": "done",
            "auto_verification": {"ok": True},
            "verifier_review": {"verdict": "pass", "notes": ""},
        }


def make_runtime(tmp_path: Path, agent: Any):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    database = KernelDatabase(tmp_path / "kernel.sqlite")
    database.migrate()
    store = BuildStore(database)
    assessment = BuildAssessment(
        id=None,
        task="Build product",
        acceptance="Tests pass",
        workspace=str(workspace),
        repo_fingerprint="fingerprint",
        deterministic_score=20,
        local_adjustment=0,
        final_score=20,
        confidence=0.9,
        recommended_tier=BuildTier.SMALL,
        dimensions={},
        floor_rules=(),
        evidence={},
        unknowns=(),
        local_work=(),
        cloud_reasons=(),
        created_at="2026-07-19T12:00:00+00:00",
    )
    session = store.create_session(assessment)
    planner = BuildPlanner(store=store, toolchains=ToolchainRegistry())
    router = BuildRouter(
        lease_lookup=store.get_active_lease,
        cloud_enabled=True,
        pricing_current=lambda: True,
    )
    orchestrator = BuildOrchestrator(
        store=store,
        planner=planner,
        router=router,
        local_agent=agent,
    )
    manager = BuildExecutionManager(store=store, orchestrator=orchestrator, max_workers=2)
    return store, session, manager


def test_background_worker_runs_session_to_completion(tmp_path: Path) -> None:
    store, session, manager = make_runtime(tmp_path, SuccessAgent())
    first = store.create_task(
        session.id,
        phase="implementation",
        kind=BuildTaskKind.AGENT,
        title="Implement",
        payload={"route": "local"},
        idempotency_key="implement",
    )
    store.create_task(
        session.id,
        phase="complete",
        kind=BuildTaskKind.CHECKPOINT,
        title="Complete",
        dependencies=(first.id,),
        idempotency_key="complete",
    )

    started = manager.start(session.id)
    outcome = manager.wait(session.id, timeout=5)
    manager.shutdown()

    assert started["started"] is True
    assert outcome["status"] == "complete"
    assert store.get_session(session.id).status == "complete"


def test_cancel_is_durable_and_running_result_cannot_complete_session(tmp_path: Path) -> None:
    agent = BlockingAgent()
    store, session, manager = make_runtime(tmp_path, agent)
    task = store.create_task(
        session.id,
        phase="implementation",
        kind=BuildTaskKind.AGENT,
        title="Long implementation",
        payload={"route": "local"},
        idempotency_key="long-task",
    )

    manager.start(session.id)
    assert agent.entered.wait(timeout=2)
    cancelling = manager.cancel(session.id)
    agent.release.set()
    outcome = manager.wait(session.id, timeout=5)
    manager.shutdown()

    assert cancelling["status"] == "cancelling"
    assert outcome["status"] == "cancelled"
    assert store.get_session(session.id).status == "cancelled"
    assert store.get_task(task.id).status is BuildTaskStatus.CANCELLED


def test_recover_requeues_interrupted_task_and_restarts_active_session(tmp_path: Path) -> None:
    store, session, first_manager = make_runtime(tmp_path, SuccessAgent())
    task = store.create_task(
        session.id,
        phase="implementation",
        kind=BuildTaskKind.AGENT,
        title="Recover implementation",
        payload={"route": "local"},
        idempotency_key="recover",
        max_attempts=2,
    )
    store.claim_task(task.id, worker_id="dead-worker")
    first_manager.shutdown()

    router = BuildRouter(
        lease_lookup=store.get_active_lease,
        cloud_enabled=True,
        pricing_current=lambda: True,
    )
    orchestrator = BuildOrchestrator(
        store=store,
        planner=BuildPlanner(store=store, toolchains=ToolchainRegistry()),
        router=router,
        local_agent=SuccessAgent(),
    )
    manager = BuildExecutionManager(store=store, orchestrator=orchestrator, max_workers=1)

    recovered = manager.recover()
    outcome = manager.wait(session.id, timeout=5)
    manager.shutdown()

    assert recovered["interrupted_runs"] == 1
    assert recovered["restarted_sessions"] == [session.id]
    assert outcome["status"] in {"idle", "complete"}
    assert store.get_task(task.id).status is BuildTaskStatus.SUCCEEDED


def test_shutdown_prevents_worker_from_starting_another_task(tmp_path: Path) -> None:
    agent = BlockingAgent()
    store, session, manager = make_runtime(tmp_path, agent)
    first = store.create_task(
        session.id,
        phase="implementation",
        kind=BuildTaskKind.AGENT,
        title="Current task",
        payload={"route": "local"},
        idempotency_key="current-task",
    )
    second = store.create_task(
        session.id,
        phase="verification",
        kind=BuildTaskKind.AGENT,
        title="Must not start",
        payload={"route": "local"},
        dependencies=(first.id,),
        idempotency_key="next-task",
    )

    manager.start(session.id)
    assert agent.entered.wait(timeout=2)
    manager.shutdown(wait=False)
    agent.release.set()
    outcome = manager.wait(session.id, timeout=5)

    assert outcome["status"] == "stopped"
    assert store.get_task(first.id).status is BuildTaskStatus.SUCCEEDED
    assert store.get_task(second.id).status is BuildTaskStatus.PENDING
