import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from cofounder_kernel.config import (
    DelegationConfig,
    KernelConfig,
    OllamaConfig,
    PathConfig,
    ProjectIntakeConfig,
)
from cofounder_kernel.db import KernelDatabase
from cofounder_kernel.project_autonomy import ProjectAutonomyReporter
from cofounder_kernel.project_autonomy_orchestrator import ProjectAutonomyOrchestrator
from cofounder_kernel.project_mvp_planner import MvpPlanResult


CRITERIA = [
    {
        "id": "mvp-one",
        "title": "First capability",
        "description": "The first documented capability works.",
        "source": "MVP.md",
        "acceptance_checks": ["The first capability is covered by a passing check."],
        "verification_commands": ["python -m pytest -q"],
        "depends_on": [],
    },
    {
        "id": "mvp-two",
        "title": "Second capability",
        "description": "The second documented capability works.",
        "source": "MVP.md",
        "acceptance_checks": ["The second capability is covered by a passing check."],
        "verification_commands": ["python -m pytest -q"],
        "depends_on": ["mvp-one"],
    },
]


class FakePlanner:
    def __init__(self, result: MvpPlanResult | None = None):
        self.result = result or MvpPlanResult(
            criteria=CRITERIA,
            external_boundaries=["app_store_submission"],
            source_hash="source-hash",
            plan_revision="plan-revision",
            needs_decision=None,
        )
        self.calls: list[int] = []

    def plan(self, project: dict[str, Any]) -> MvpPlanResult:
        self.calls.append(int(project["id"]))
        return self.result


class FakeDelegation:
    def __init__(self, dispatches: list[dict[str, Any]] | None = None):
        self.dispatches = list(dispatches or [])
        self.calls: list[dict[str, Any]] = []

    def queue_delegation(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        root = Path(kwargs["workspace"])
        marker = root / "src" / f"increment-{len(self.calls)}.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"completed attempt {len(self.calls)}\n", encoding="utf-8")
        dispatch = self.dispatches.pop(0) if self.dispatches else passing_dispatch()
        return {
            "item_id": len(self.calls),
            "created": True,
            "auto_invoked": True,
            "dispatch": dispatch,
        }


def passing_dispatch() -> dict[str, Any]:
    return {
        "status": "ok",
        "ok": True,
        "engine": "native",
        "provider": {
            "provider": "ollama",
            "endpoint_host": "127.0.0.1",
            "verified_local": True,
        },
        "auto_verification": {
            "ok": True,
            "checks": [
                {
                    "argv": [sys.executable, "-c", "print('post-commit verified')"],
                    "ok": True,
                    "returncode": 0,
                }
            ],
            "output": "post-commit verified",
        },
        "verifier_review": {"verdict": "pass", "notes": "Matches the criterion."},
        "changed_files": ["src/feature.txt"],
        "artifact": "Implemented and checked the criterion.",
    }


def failed_dispatch(output: str = "tests failed") -> dict[str, Any]:
    dispatch = passing_dispatch()
    dispatch["auto_verification"] = {
        "ok": False,
        "checks": [
            {
                "argv": [sys.executable, "-c", "raise SystemExit(1)"],
                "ok": False,
                "returncode": 1,
            }
        ],
        "output": output,
    }
    return dispatch


def config_for(tmp_path: Path, *, repairs: int = 3) -> KernelConfig:
    return KernelConfig(
        paths=PathConfig(
            hot_root=tmp_path / "brain",
            cold_root=tmp_path / "cold",
            data_dir=tmp_path / "data",
        ),
        ollama=OllamaConfig(
            base_url="http://127.0.0.1:11434",
            provider_policy="local_only",
            cloud_fallback="never",
            coding_agent_model="local-coder:latest",
        ),
        delegation=DelegationConfig(
            enabled=True,
            auto_invoke=True,
            engine="native",
        ),
        project_intake=ProjectIntakeConfig(
            enabled=True,
            autonomy_enabled=True,
            autonomy_max_workers=2,
            autonomy_lease_seconds=900,
            autonomy_repair_attempts=repairs,
            autonomy_reconcile_seconds=60,
        ),
    )


def make_project(
    db: KernelDatabase, config: KernelConfig, name: str, *, priority: str = "urgent"
) -> tuple[int, Path]:
    root = config.paths.project_intake_dir / name
    root.mkdir(parents=True)
    (root / "project.md").write_text(f"# {name}\n", encoding="utf-8")
    (root / "MVP.md").write_text("# MVP\nBuild both documented capabilities.\n", encoding="utf-8")
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "approved docs",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    project_id = db.upsert_project(
        canonical_path=str(root),
        name=name,
        product_type="mobile_application",
        distribution_targets=["google_play", "apple_app_store_eventual"],
        lifecycle_state="verified",
        repo_fingerprint=name,
        metadata={},
    )
    if priority != "normal":
        reporter = ProjectAutonomyReporter(db=db, bus=None)
        reporter.plan(project_id, criteria=CRITERIA, priority=priority, plan_revision="plan-revision")
    return project_id, root


def make_services(
    tmp_path: Path,
    *,
    planner: FakePlanner | None = None,
    delegation: FakeDelegation | None = None,
    repairs: int = 3,
) -> tuple[
    ProjectAutonomyOrchestrator,
    ProjectAutonomyReporter,
    KernelDatabase,
    KernelConfig,
    FakeDelegation,
]:
    config = config_for(tmp_path, repairs=repairs)
    db = KernelDatabase(config.paths.database_path)
    db.migrate()
    reporter = ProjectAutonomyReporter(db=db, bus=None)
    fake_delegation = delegation or FakeDelegation()
    orchestrator = ProjectAutonomyOrchestrator(
        config=config,
        db=db,
        reporter=reporter,
        planner=planner or FakePlanner(),
        delegation=fake_delegation,
        owner="test-orchestrator",
    )
    return orchestrator, reporter, db, config, fake_delegation


def test_verified_increment_immediately_queues_next_criterion(tmp_path: Path) -> None:
    orchestrator, reporter, db, config, delegation = make_services(tmp_path)
    project_id, root = make_project(db, config, "Same Ground")

    first = orchestrator.run_once()
    second = orchestrator.run_once()

    assert first["criterion_id"] == "mvp-one"
    assert first["status"] == "criterion_complete"
    assert second["criterion_id"] == "mvp-two"
    assert second["status"] == "mvp_complete"
    state = reporter.state(project_id)
    assert [item["status"] for item in state["mvp_criteria"]] == ["complete", "complete"]
    assert state["mvp_complete"] is True
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, check=True, capture_output=True, text=True
    ).stdout == ""
    assert all(call["directed"] is True for call in delegation.calls)
    assert all(call["auto_invoke"] is True for call in delegation.calls)


def test_unplanned_project_is_planned_before_first_increment(tmp_path: Path) -> None:
    planner = FakePlanner()
    orchestrator, reporter, db, config, _delegation = make_services(
        tmp_path, planner=planner
    )
    project_id, _root = make_project(db, config, "Same Ground", priority="normal")

    result = orchestrator.run_once()

    assert result["criterion_id"] == "mvp-one"
    assert planner.calls == [project_id]
    assert reporter.state(project_id)["plan_revision"] == "plan-revision"


def test_two_urgent_projects_claim_concurrently_but_never_twice_each(tmp_path: Path) -> None:
    orchestrator, _reporter, db, config, _delegation = make_services(tmp_path)
    same_ground, _ = make_project(db, config, "Same Ground")
    dark_index, _ = make_project(db, config, "The Dark Index")

    claims = orchestrator.claim_ready(limit=2)

    assert {item["project_id"] for item in claims} == {same_ground, dark_index}
    assert orchestrator.claim_ready(limit=2) == []
    for claim in claims:
        orchestrator.release_claim(claim)


def test_repairs_use_unique_work_keys_and_block_after_budget(tmp_path: Path) -> None:
    delegation = FakeDelegation([failed_dispatch(f"failure {index}") for index in range(4)])
    orchestrator, reporter, db, config, _ = make_services(
        tmp_path, delegation=delegation, repairs=3
    )
    project_id, _root = make_project(db, config, "Same Ground")

    result = orchestrator.run_once()

    assert result["status"] == "blocked"
    assert len(delegation.calls) == 4
    assert len({call["unique_key"] for call in delegation.calls}) == 4
    assert [call["unique_key"].rsplit(":", 1)[-1] for call in delegation.calls] == [
        "0",
        "1",
        "2",
        "3",
    ]
    state = reporter.state(project_id)
    assert state["phase"] == "blocked"
    assert state["blocking_type"] == "error"


def test_repair_can_pass_and_records_only_post_commit_evidence(tmp_path: Path) -> None:
    delegation = FakeDelegation([failed_dispatch(), passing_dispatch()])
    orchestrator, reporter, db, config, _ = make_services(
        tmp_path, delegation=delegation, repairs=3
    )
    project_id, root = make_project(db, config, "Same Ground")

    result = orchestrator.run_once()

    criterion = reporter.state(project_id)["mvp_criteria"][0]
    assert result["status"] == "criterion_complete"
    assert len(delegation.calls) == 2
    assert criterion["verification"]["ok"] is True
    assert criterion["commit"] == subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_planner_ambiguity_creates_ui_only_project_decision(tmp_path: Path) -> None:
    planner = FakePlanner(
        MvpPlanResult(
            criteria=[],
            external_boundaries=[],
            source_hash="ambiguous",
            plan_revision="ambiguous-plan",
            needs_decision={
                "question": "Which offline identity mode belongs in the MVP?",
                "recommendation": "Device-local profiles",
                "options": [
                    {"option": "Device-local profiles", "impact": "No account backend."},
                    {"option": "Email accounts", "impact": "Adds identity services."},
                ],
            },
        )
    )
    orchestrator, reporter, db, config, _delegation = make_services(
        tmp_path, planner=planner
    )
    project_id, _root = make_project(db, config, "Same Ground", priority="normal")

    result = orchestrator.run_once()

    assert result["status"] == "needs_decision"
    item = db.get_work_item(result["decision_id"])
    assert item is not None
    assert item.kind == "founder_decision"
    assert item.status == "approval_required"
    assert item.metadata["project_autonomy"] is True
    assert item.metadata["project_autonomy_resume_only"] is True
    assert reporter.state(project_id)["phase"] == "needs_decision"


def test_agent_decision_is_reclassified_into_canonical_project_ui(tmp_path: Path) -> None:
    dispatch = {
        "status": "needs_decision",
        "ok": True,
        "founder_question": {
            "question": "Use local profiles or email accounts?",
            "options": ["Local profiles", "Email accounts"],
        },
        "artifact": "Paused for a consequential product choice.",
    }
    delegation = FakeDelegation([dispatch])
    orchestrator, reporter, db, config, _ = make_services(
        tmp_path, delegation=delegation
    )
    project_id, _root = make_project(db, config, "Same Ground")

    result = orchestrator.run_once()

    assert result["status"] == "needs_decision"
    decision = db.get_work_item(result["decision_id"])
    assert decision is not None
    assert decision.metadata["project_id"] == project_id
    assert decision.metadata["project_autonomy"] is True
    assert reporter.state(project_id)["phase"] == "needs_decision"


def test_approval_boundary_is_classified_without_telegram_resolution(tmp_path: Path) -> None:
    dispatch = {
        "status": "approval_required",
        "ok": False,
        "boundary": "paid_services",
        "proposed_action": "Create a paid map service account",
        "reason": "The criterion cannot use this external paid service without approval.",
    }
    delegation = FakeDelegation([dispatch])
    orchestrator, reporter, db, config, _ = make_services(
        tmp_path, delegation=delegation
    )
    project_id, _root = make_project(db, config, "Same Ground")

    result = orchestrator.run_once()

    assert result["status"] == "approval_required"
    assert db.get_approval_request(result["approval_request_id"]) is not None
    assert reporter.state(project_id)["phase"] == "approval_required"


def test_recover_clears_expired_leases_without_duplicate_runs(tmp_path: Path) -> None:
    orchestrator, _reporter, db, config, _delegation = make_services(tmp_path)
    make_project(db, config, "Same Ground")
    claim = orchestrator.claim_ready(limit=1)[0]
    with db.connect() as conn:
        conn.execute(
            "UPDATE project_autonomy_leases SET expires_at = ? WHERE project_id = ?",
            ("2000-01-01T00:00:00+00:00", claim["project_id"]),
        )

    recovered = orchestrator.recover()

    assert recovered["expired_leases_cleared"] == 1
    assert recovered["unfinished_seen"] == 1
    assert recovered["duplicate_runs_created"] == 0
    orchestrator.shutdown(wait=False)
    assert orchestrator.status()["shutdown"] is True


def test_recovered_building_phase_can_repair_its_dirty_workspace(tmp_path: Path) -> None:
    orchestrator, reporter, db, config, _delegation = make_services(tmp_path)
    project_id, root = make_project(db, config, "Same Ground")
    reporter.begin_increment(project_id, criterion_id="mvp-one", run_id=17)
    (root / "interrupted.txt").write_text("unfinished prior run\n", encoding="utf-8")

    result = orchestrator.run_once()

    assert result["status"] == "criterion_complete"
    assert reporter.state(project_id)["mvp_criteria"][0]["status"] == "complete"


def test_non_native_or_cloud_fallback_policy_fails_closed(tmp_path: Path) -> None:
    orchestrator, _reporter, db, config, _delegation = make_services(tmp_path)
    make_project(db, config, "Same Ground")
    object.__setattr__(config.delegation, "engine", "bridge")

    result = orchestrator.run_once()

    assert result["status"] == "blocked"
    assert "native" in result["reason"].lower()
