from __future__ import annotations

from pathlib import Path
from typing import Any

from cofounder_kernel.build_orchestrator import BuildOrchestrator, BuildPlanner
from cofounder_kernel.build_routing import BuildRouter
from cofounder_kernel.build_store import BuildStore
from cofounder_kernel.build_types import (
    BuildAssessment,
    BuildTaskKind,
    BuildTaskStatus,
    BuildTier,
    LeaseLimits,
)
from cofounder_kernel.db import KernelDatabase
from cofounder_kernel.toolchain_profiles import ToolchainRegistry


class FakeAgent:
    def __init__(self, *, ok: bool = True, result: dict[str, Any] | None = None):
        self.ok = ok
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def run(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.result is not None:
            return dict(self.result)
        return {
            "ok": self.ok,
            "status": "ok" if self.ok else "model_error",
            "error": "" if self.ok else "local model failed",
            "response": "done" if self.ok else "failed",
            "auto_verification": None,
        }


def make_store(tmp_path: Path) -> BuildStore:
    database = KernelDatabase(tmp_path / "kernel.sqlite")
    database.migrate()
    return BuildStore(database)


def create_session(store: BuildStore, workspace: Path):
    assessment = BuildAssessment(
        id=None,
        task="Build a SaaS product",
        acceptance="All local checks pass",
        workspace=str(workspace),
        repo_fingerprint="fingerprint",
        deterministic_score=36,
        local_adjustment=2,
        final_score=38,
        confidence=0.9,
        recommended_tier=BuildTier.MEDIUM,
        dimensions={"product_surfaces": 5, "change_breadth": 4},
        floor_rules=(),
        evidence={"frameworks": ["fastapi"]},
        unknowns=(),
        local_work=("Implement locally",),
        cloud_reasons=("Review architecture",),
        created_at="2026-07-19T12:00:00+00:00",
    )
    return store.create_session(assessment)


def make_router(store: BuildStore) -> BuildRouter:
    return BuildRouter(
        lease_lookup=store.get_active_lease,
        cloud_enabled=True,
        pricing_current=lambda: True,
    )


def test_planner_creates_idempotent_full_lifecycle_graph(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    store = make_store(tmp_path)
    session = create_session(store, workspace)
    planner = BuildPlanner(store=store, toolchains=ToolchainRegistry())

    first = planner.plan(session.id)
    second = planner.plan(session.id)

    assert second == first
    assert [task.phase for task in first] == [
        "discovery",
        "requirements",
        "architecture",
        "planning",
        "implementation",
        "verification",
        "review",
        "release",
        "complete",
    ]
    assert first[0].dependencies == ()
    assert all(task.dependencies == (first[index - 1].id,) for index, task in enumerate(first) if index)
    assert first[4].payload["toolchain_profile"] == "python-saas"
    assert first[1].payload["output_contract"] == {
        "required_artifacts": [".zade/build/requirements.md"],
        "allowed_write_paths": [".zade/build/requirements.md"],
    }
    assert first[2].payload["output_contract"] == {
        "required_artifacts": [".zade/build/architecture.md"],
        "allowed_write_paths": [".zade/build/architecture.md"],
    }
    assert first[3].payload["output_contract"] == {
        "required_artifacts": [".zade/build/plan.md"],
        "allowed_write_paths": [".zade/build/plan.md"],
    }
    assert first[1].max_attempts == 2
    assert first[3].max_attempts == 2


def test_flutter_plan_requires_github_ios_workflow_evidence(tmp_path: Path) -> None:
    workspace = tmp_path / "flutter_app"
    (workspace / "lib").mkdir(parents=True)
    (workspace / "pubspec.yaml").write_text("name: demo\n", encoding="utf-8")
    store = make_store(tmp_path)
    session = create_session(store, workspace)
    tasks = BuildPlanner(store=store, toolchains=ToolchainRegistry()).plan(session.id)

    release = next(task for task in tasks if task.phase == "release")

    assert release.kind is BuildTaskKind.GITHUB
    assert release.payload["operation"] == "verify_workflow"
    assert release.payload["workflow"] == "ios.yml"


def test_local_task_runs_without_any_cloud_lease(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = make_store(tmp_path)
    session = create_session(store, workspace)
    task = store.create_task(
        session.id,
        phase="requirements",
        kind=BuildTaskKind.AGENT,
        title="Write requirements",
        payload={"route": "local", "instructions": "Define the MVP"},
        idempotency_key="requirements",
    )
    local = FakeAgent()
    orchestrator = BuildOrchestrator(
        store=store,
        planner=BuildPlanner(store=store, toolchains=ToolchainRegistry()),
        router=make_router(store),
        local_agent=local,
    )

    result = orchestrator.run_next(session.id, worker_id="test-worker")

    assert result["status"] == "succeeded"
    assert result["route"] == "local"
    assert result["task_id"] == task.id
    assert len(local.calls) == 1
    assert store.get_task(task.id).status is BuildTaskStatus.SUCCEEDED


def test_explicit_cloud_task_waits_for_matching_lease(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = make_store(tmp_path)
    session = create_session(store, workspace)
    task = store.create_task(
        session.id,
        phase="review",
        kind=BuildTaskKind.REVIEW,
        title="Cloud security review",
        payload={"route": "cloud", "provider": "anthropic"},
        idempotency_key="cloud-review",
    )
    local = FakeAgent()
    cloud_calls: list[int] = []
    orchestrator = BuildOrchestrator(
        store=store,
        planner=BuildPlanner(store=store, toolchains=ToolchainRegistry()),
        router=make_router(store),
        local_agent=local,
        cloud_executor=lambda _task, _assessment, lease: cloud_calls.append(lease.id)
        or {"ok": True, "status": "ok", "response": "reviewed"},
    )

    blocked = orchestrator.run_next(session.id)

    assert blocked["status"] == "blocked"
    assert blocked["blockers"] == ["no_active_anthropic_lease"]
    assert store.get_task(task.id).status is BuildTaskStatus.PENDING
    assert local.calls == []
    assert cloud_calls == []

    store.create_lease(
        session.id,
        BuildTier.MEDIUM,
        LeaseLimits(3_000_000, 400_000, 40_000, 16, 14_400),
        provider="anthropic",
        model="claude-opus-4-8",
        approval_request_id=7,
    )
    completed = orchestrator.run_next(session.id)

    assert completed["status"] == "succeeded"
    assert completed["route"] == "cloud"
    assert len(cloud_calls) == 1


def test_dependencies_execute_in_order_and_complete_the_session(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = make_store(tmp_path)
    session = create_session(store, workspace)
    first = store.create_task(
        session.id,
        phase="implementation",
        kind=BuildTaskKind.CHECKPOINT,
        title="Implementation checkpoint",
        idempotency_key="first",
    )
    second = store.create_task(
        session.id,
        phase="complete",
        kind=BuildTaskKind.CHECKPOINT,
        title="Complete build",
        dependencies=(first.id,),
        idempotency_key="second",
    )
    orchestrator = BuildOrchestrator(
        store=store,
        planner=BuildPlanner(store=store, toolchains=ToolchainRegistry()),
        router=make_router(store),
        local_agent=FakeAgent(),
    )

    one = orchestrator.run_next(session.id)
    two = orchestrator.run_next(session.id)

    assert one["task_id"] == first.id
    assert two["task_id"] == second.id
    assert two["status"] == "complete"
    assert store.get_session(session.id).status == "complete"


def test_failed_task_is_recorded_without_raising_from_run_next(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = make_store(tmp_path)
    session = create_session(store, workspace)
    task = store.create_task(
        session.id,
        phase="implementation",
        kind=BuildTaskKind.AGENT,
        title="Implement feature",
        payload={"route": "local"},
        idempotency_key="implementation",
    )
    orchestrator = BuildOrchestrator(
        store=store,
        planner=BuildPlanner(store=store, toolchains=ToolchainRegistry()),
        router=make_router(store),
        local_agent=FakeAgent(ok=False),
    )

    result = orchestrator.run_next(session.id)

    assert result["status"] == "failed"
    assert "local model failed" in result["error"]
    assert store.get_task(task.id).status is BuildTaskStatus.FAILED


def test_phase_contract_rejects_product_code_written_during_requirements(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = make_store(tmp_path)
    session = create_session(store, workspace)
    task = store.create_task(
        session.id,
        phase="requirements",
        kind=BuildTaskKind.AGENT,
        title="Write requirements",
        payload={
            "route": "local",
            "output_contract": {
                "required_artifacts": [".zade/build/requirements.md"],
                "allowed_write_paths": [".zade/build/requirements.md"],
            },
        },
        idempotency_key="requirements",
    )
    agent = FakeAgent(
        result={
            "ok": True,
            "status": "ok",
            "error": "",
            "changed_files": ["app/src/AuthActivity.java"],
            "workspace_changes": {
                "added": ["app/src/AuthActivity.java"],
                "modified": [],
                "deleted": [],
            },
            "auto_verification": None,
            "verifier_review": None,
        }
    )
    orchestrator = BuildOrchestrator(
        store=store,
        planner=BuildPlanner(store=store, toolchains=ToolchainRegistry()),
        router=make_router(store),
        local_agent=agent,
    )

    result = orchestrator.run_next(session.id)

    assert result["status"] == "failed"
    assert result["result"]["status"] == "phase_contract_failed"
    assert "app/src/AuthActivity.java" in result["result"]["evidence_gate"]["unexpected_changes"]
    assert store.get_task(task.id).status is BuildTaskStatus.FAILED


def test_implementation_requires_positive_mechanical_verification(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = make_store(tmp_path)
    session = create_session(store, workspace)
    task = store.create_task(
        session.id,
        phase="implementation",
        kind=BuildTaskKind.AGENT,
        title="Implement feature",
        payload={"route": "local"},
        idempotency_key="implementation",
    )
    orchestrator = BuildOrchestrator(
        store=store,
        planner=BuildPlanner(store=store, toolchains=ToolchainRegistry()),
        router=make_router(store),
        local_agent=FakeAgent(
            result={
                "ok": True,
                "status": "ok",
                "error": "",
                "changed_files": ["lib/main.dart"],
                "auto_verification": {"ok": None, "mode": "none"},
                "verifier_review": {"verdict": "pass", "notes": ""},
            }
        ),
    )

    result = orchestrator.run_next(session.id)

    assert result["status"] == "failed"
    assert result["result"]["status"] == "verification_required"
    assert store.get_task(task.id).status is BuildTaskStatus.FAILED


def test_fresh_context_verifier_failure_prevents_task_advancement(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = make_store(tmp_path)
    session = create_session(store, workspace)
    task = store.create_task(
        session.id,
        phase="implementation",
        kind=BuildTaskKind.AGENT,
        title="Implement feature",
        payload={"route": "local"},
        idempotency_key="implementation",
    )
    orchestrator = BuildOrchestrator(
        store=store,
        planner=BuildPlanner(store=store, toolchains=ToolchainRegistry()),
        router=make_router(store),
        local_agent=FakeAgent(
            result={
                "ok": True,
                "status": "ok",
                "error": "",
                "changed_files": ["lib/main.dart"],
                "auto_verification": {"ok": True, "mode": "flutter-test"},
                "verifier_review": {
                    "verdict": "fail",
                    "notes": "lib/main.dart does not satisfy acceptance",
                },
            }
        ),
    )

    result = orchestrator.run_next(session.id)

    assert result["status"] == "failed"
    assert result["result"]["status"] == "verifier_rejected"
    assert "does not satisfy" in result["error"]
