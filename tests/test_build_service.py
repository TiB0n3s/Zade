from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cofounder_kernel.build_assessment import BuildAssessmentService
from cofounder_kernel.build_budget import BuildBudgetService
from cofounder_kernel.build_budget import ProviderUsage
from cofounder_kernel.build_orchestrator import BuildOrchestrator, BuildPlanner
from cofounder_kernel.build_routing import BuildRouter
from cofounder_kernel.build_service import BuildService
from cofounder_kernel.build_store import BuildStore
from cofounder_kernel.config import (
    AnthropicConfig,
    AppConfig,
    DelegationConfig,
    KernelConfig,
    OllamaConfig,
    PathConfig,
)
from cofounder_kernel.db import KernelDatabase
from cofounder_kernel.egress import EgressPolicy
from cofounder_kernel.toolchain_profiles import ToolchainRegistry


CONFIRMATION = "make the jump to hyperspace"


class FakeCodingAgent:
    def __init__(self, *, provider: str = "ollama", ok: bool = True):
        self.provider = provider
        self.ok = ok
        self.calls: list[dict[str, Any]] = []

    def run(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        write_allowlist = tuple(kwargs.get("write_allowlist") or ())
        workspace = Path(str(kwargs.get("workspace") or ""))
        changed = list(write_allowlist[:1])
        if changed:
            target = workspace / changed[0]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# Test build artifact\n", encoding="utf-8")
        return {
            "ok": self.ok,
            "status": "ok" if self.ok else "model_error",
            "error": "" if self.ok else "failed",
            "model": kwargs.get("model") or "local-coder",
            "provider": {"provider": self.provider, "fallback_attempted": False},
            "workspace": str(workspace),
            "rounds": 1,
            "used_tools": False,
            "steps": [],
            "changed_files": changed,
            "workspace_changes": {"added": changed, "modified": [], "deleted": []},
            "auto_verification": {"ok": True, "command": ["pytest"]},
            "verifier_review": {"verdict": "pass", "notes": ""},
            "progress_notes": [],
            "response": "Completed and verified.",
        }


class FakeCloudFactory:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []
        self.agent = FakeCodingAgent(provider="anthropic")

    def __call__(self, session_id: int, authorize_egress: Any) -> FakeCodingAgent:
        self.calls.append(
            {"session_id": session_id, "authorize_egress": authorize_egress}
        )
        return self.agent


def make_service(tmp_path: Path) -> tuple[BuildService, KernelDatabase, FakeCodingAgent, FakeCloudFactory]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("LABEL = 'Old'\n", encoding="utf-8")
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(
            hot_root=tmp_path / "hot",
            cold_root=tmp_path / "cold",
            data_dir=tmp_path / "data",
        ),
        ollama=OllamaConfig(provider_policy="local_preferred"),
        anthropic=AnthropicConfig(enabled=True, model="claude-opus-4-8"),
        delegation=DelegationConfig(engine="hybrid", workspace_root=str(workspace)),
    )
    database = KernelDatabase(config.paths.database_path)
    database.migrate()
    store = BuildStore(database)
    budget = BuildBudgetService(store, config.build.anthropic_pricing.snapshot())
    router = BuildRouter(
        lease_lookup=store.get_active_lease,
        cloud_enabled=True,
        pricing_current=lambda: config.build.anthropic_pricing.is_current(),
    )
    local = FakeCodingAgent()
    cloud = FakeCloudFactory()
    service = BuildService(
        config=config,
        db=database,
        assessor=BuildAssessmentService(),
        store=store,
        budget=budget,
        router=router,
        local_coding_agent=local,
        cloud_coding_agent_factory=cloud,
        egress_policy=EgressPolicy.from_config(config),
        typed_confirmation_phrase=CONFIRMATION,
    )
    return service, database, local, cloud


def test_prepare_creates_assessment_session_and_one_approval(tmp_path: Path) -> None:
    service, database, local, cloud = make_service(tmp_path)
    workspace = tmp_path / "workspace"

    first = service.prepare(
        task="Build the app", workspace=workspace, acceptance="Tests pass"
    )
    second = service.prepare(
        task="Build the app", workspace=workspace, acceptance="Tests pass"
    )

    assert first["session"]["phase"] == "approval"
    assert first["assessment"]["recommended_tier"] in {"small", "medium", "large"}
    assert second["session"]["id"] != first["session"]["id"]
    pending = [
        item
        for item in database.list_approval_requests(status="pending")
        if item.source_type == "build_lease"
    ]
    assert len(pending) == 2
    assert pending[0].metadata["permitted_data_classes"] == ["source_code"]
    assert local.calls == []
    assert cloud.calls == []


def test_prepare_is_idempotent_for_a_linked_work_item(tmp_path: Path) -> None:
    service, database, _local, _cloud = make_service(tmp_path)
    workspace = tmp_path / "workspace"

    first = service.prepare(
        task="Build the app", workspace=workspace, work_item_id=42
    )
    second = service.prepare(
        task="Build the app", workspace=workspace, work_item_id=42
    )

    assert second["session"]["id"] == first["session"]["id"]
    pending = [
        item
        for item in database.list_approval_requests(status="pending")
        if item.source_type == "build_lease"
    ]
    assert len(pending) == 1


def test_approve_mints_lease_then_local_route_spends_zero(tmp_path: Path) -> None:
    service, _database, local, cloud = make_service(tmp_path)
    prepared = service.prepare(
        task="Rename one label", workspace=tmp_path / "workspace"
    )

    approved = service.approve(
        prepared["session"]["id"], typed_phrase=CONFIRMATION
    )

    assert approved["run"]["route"] == "local"
    assert approved["usage"]["actual_microdollars"] == 0
    assert len(local.calls) == 1
    assert cloud.calls == []


def test_durable_service_can_run_local_phases_before_cloud_approval(tmp_path: Path) -> None:
    service, _database, local, cloud = make_service(tmp_path)
    planner = BuildPlanner(store=service.store, toolchains=ToolchainRegistry())
    orchestrator = BuildOrchestrator(
        store=service.store,
        planner=planner,
        router=service.router,
        local_agent=local,
    )
    service.orchestrator = orchestrator
    prepared = service.prepare(
        task="Build the app", workspace=tmp_path / "workspace"
    )

    discovery = service.run(prepared["session"]["id"])
    requirements = service.run(prepared["session"]["id"])

    assert discovery["status"] == "succeeded"
    assert requirements["status"] == "succeeded"
    assert requirements["route"] == "local"
    assert service.status(prepared["session"]["id"])["lease"] is None
    assert len(local.calls) == 1
    assert cloud.calls == []


def test_approval_requires_exact_typed_phrase(tmp_path: Path) -> None:
    service, _database, _local, _cloud = make_service(tmp_path)
    prepared = service.prepare(task="Rename label", workspace=tmp_path / "workspace")

    with pytest.raises(ValueError, match="typed confirmation"):
        service.approve(prepared["session"]["id"], typed_phrase="approve")

    assert service.status(prepared["session"]["id"])["lease"] is None


def test_security_sensitive_build_routes_to_cloud_only_after_approval(
    tmp_path: Path,
) -> None:
    service, _database, local, cloud = make_service(tmp_path)
    prepared = service.prepare(
        task="Add production authentication and Stripe billing",
        workspace=tmp_path / "workspace",
    )
    assert cloud.calls == []

    approved = service.approve(
        prepared["session"]["id"], typed_phrase=CONFIRMATION
    )

    assert approved["run"]["route"] == "cloud"
    assert len(cloud.calls) == 1
    assert len(cloud.agent.calls) == 1
    assert local.calls == []


def test_exhaustion_continues_local_once_and_deduplicates_upgrade(
    tmp_path: Path,
) -> None:
    service, database, local, _cloud = make_service(tmp_path)
    prepared = service.prepare(
        task="Rename one label", workspace=tmp_path / "workspace"
    )
    approved = service.approve(
        prepared["session"]["id"], typed_phrase=CONFIRMATION
    )
    lease_id = approved["lease"]["id"]
    service.store.exhaust_lease(lease_id)

    first = service.run(prepared["session"]["id"])
    second = service.run(prepared["session"]["id"])

    assert first["lease"]["state"] == "exhausted"
    assert first["local_continued"] is True
    assert second["local_continued"] is False
    assert first["upgrade_request_id"] == second["upgrade_request_id"]
    upgrades = [
        item
        for item in database.list_approval_requests(status="pending")
        if item.source_type == "build_lease_upgrade"
    ]
    assert len(upgrades) == 1
    assert len(local.calls) == 2  # initial approved run plus one continuation


def test_deny_closes_session_without_creating_lease(tmp_path: Path) -> None:
    service, _database, local, cloud = make_service(tmp_path)
    prepared = service.prepare(task="Build app", workspace=tmp_path / "workspace")

    denied = service.deny(prepared["session"]["id"], note="Not now")

    assert denied["session"]["status"] == "complete"
    assert denied["lease"] is None
    assert local.calls == []
    assert cloud.calls == []


def test_approved_upgrade_adds_limits_without_resetting_usage(tmp_path: Path) -> None:
    service, _database, _local, _cloud = make_service(tmp_path)
    prepared = service.prepare(
        task="Rename one label", workspace=tmp_path / "workspace"
    )
    approved = service.approve(
        prepared["session"]["id"], typed_phrase=CONFIRMATION
    )
    session_id = prepared["session"]["id"]
    reservation = service.budget.reserve(
        session_id=session_id,
        request_id="usage-before-upgrade",
        input_upper=1000,
        max_output=100,
        cache_mode="none",
    )
    service.budget.settle(
        reservation.id, ProviderUsage(input_tokens=800, output_tokens=80)
    )
    before = service.store.get_active_lease(session_id)
    assert before is not None
    service.store.exhaust_lease(before.id)
    service.run(session_id)

    upgraded = service.approve(session_id, typed_phrase=CONFIRMATION)

    after = service.store.get_active_lease(session_id)
    assert after is not None
    assert after.version == before.version + 1
    assert after.actual_input_tokens == before.actual_input_tokens
    assert after.actual_output_tokens == before.actual_output_tokens
    assert after.actual_microdollars == before.actual_microdollars
    assert after.cloud_turns == before.cloud_turns
    assert after.limits.dollar_micro == before.limits.dollar_micro + 3_000_000
    assert upgraded["upgrade_request_id"] is None
