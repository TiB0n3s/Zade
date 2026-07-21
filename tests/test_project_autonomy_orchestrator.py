import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from cofounder_kernel import project_autonomy_orchestrator as autonomy_orchestrator
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


def test_windows_post_commit_check_resolves_command_shim(monkeypatch) -> None:
    """The coding agent records `npm`, while Windows exposes npm.cmd."""
    monkeypatch.setattr(autonomy_orchestrator.os, "name", "nt")
    monkeypatch.setattr(
        autonomy_orchestrator.shutil,
        "which",
        lambda executable: r"C:\\Program Files\\nodejs\\npm.CMD"
        if executable == "npm"
        else None,
    )

    assert autonomy_orchestrator._resolve_check_argv(["npm", "exec", "--no"]) == [
        r"C:\\Program Files\\nodejs\\npm.CMD",
        "exec",
        "--no",
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


class SequencePlanner:
    def __init__(self, *results: MvpPlanResult):
        self.results = list(results)
        self.calls: list[int] = []

    def plan(self, project: dict[str, Any]) -> MvpPlanResult:
        self.calls.append(int(project["id"]))
        return self.results.pop(0)


class RaisingPlanner:
    def __init__(self, error: ValueError):
        self.error = error

    def plan(self, project: dict[str, Any]) -> MvpPlanResult:
        raise self.error


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
    assert state["mvp_criteria"] == []
    assert state["mvp_achieved"] is True
    assert state["milestones"][0]["criteria"] == ["mvp-one", "mvp-two"]
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, check=True, capture_output=True, text=True
    ).stdout == ""
    assert all(call["directed"] is True for call in delegation.calls)
    assert all(call["auto_invoke"] is True for call in delegation.calls)


def test_completed_mvp_automatically_executes_documented_continuation(
    tmp_path: Path,
) -> None:
    continuation = {
        **CRITERIA[0],
        "id": "ios-parity",
        "title": "iPhone parity",
        "source": "project.md",
    }
    planner = SequencePlanner(
        MvpPlanResult(
            criteria=[CRITERIA[0]],
            external_boundaries=["app_store_submission"],
            source_hash="mvp-source",
            plan_revision="mvp-plan",
            needs_decision=None,
        ),
        MvpPlanResult(
            criteria=[continuation],
            external_boundaries=["app_store_submission"],
            source_hash="continuation-source",
            plan_revision="continuation-plan",
            needs_decision=None,
        ),
    )
    orchestrator, reporter, db, config, delegation = make_services(tmp_path, planner=planner)
    project_id, _root = make_project(db, config, "Same Ground", priority="normal")

    first = orchestrator.run_once()
    second = orchestrator.run_once()

    assert first["status"] == "mvp_complete"
    assert second["status"] == "awaiting_external_boundary"
    assert second["criterion_id"] == "ios-parity"
    assert reporter.state(project_id)["scope_kind"] == "continuation"
    assert reporter.state(project_id)["phase"] == "awaiting_external_boundary"
    assert [milestone["kind"] for milestone in reporter.state(project_id)["milestones"]] == [
        "mvp",
        "continuation",
    ]
    assert reporter.state(project_id)["milestones"][-1]["criteria"] == ["ios-parity"]
    assert len(delegation.calls) == 2
    assert delegation.calls[-1]["task"] == (
        "Complete documented continuation criterion ios-parity: iPhone parity"
    )


def test_stale_empty_continuation_building_state_recovers_before_planning(
    tmp_path: Path,
) -> None:
    continuation = {
        **CRITERIA[0],
        "id": "ios-parity",
        "title": "iPhone parity",
        "source": "project.md",
    }
    planner = SequencePlanner(
        MvpPlanResult(
            criteria=[CRITERIA[0]],
            external_boundaries=["app_store_submission"],
            source_hash="mvp-source",
            plan_revision="mvp-plan",
            needs_decision=None,
        ),
        MvpPlanResult(
            criteria=[continuation],
            external_boundaries=["app_store_submission"],
            source_hash="continuation-source",
            plan_revision="continuation-plan",
            needs_decision=None,
        ),
    )
    orchestrator, reporter, _db, _config, delegation = make_services(
        tmp_path, planner=planner
    )
    project_id, _root = make_project(
        orchestrator.db, orchestrator.config, "Same Ground", priority="normal"
    )

    orchestrator.run_once()
    stale = reporter._state_for_project(reporter.get_project(project_id))
    stale.update({"phase": "building", "current_criterion_id": None, "active_run_id": None})
    reporter._transition(
        reporter.get_project(project_id),
        stale,
        event={"event_type": "stale_continuation_building_fixture"},
    )

    result = orchestrator.run_once()

    assert result["status"] == "awaiting_external_boundary"
    assert reporter.state(project_id)["phase"] == "awaiting_external_boundary"
    assert len(delegation.calls) == 2
    assert delegation.calls[-1]["task"] == (
        "Complete documented continuation criterion ios-parity: iPhone parity"
    )


def test_external_only_continuation_waits_without_fake_delegation(tmp_path: Path) -> None:
    planner = SequencePlanner(
        MvpPlanResult(
            criteria=[CRITERIA[0]],
            external_boundaries=["app_store_submission"],
            source_hash="mvp-source",
            plan_revision="mvp-plan",
            needs_decision=None,
        ),
        MvpPlanResult(
            criteria=[],
            external_boundaries=["app_store_submission"],
            source_hash="continuation-source",
            plan_revision="continuation-plan",
            needs_decision=None,
        ),
    )
    orchestrator, reporter, db, config, delegation = make_services(tmp_path, planner=planner)
    project_id, _root = make_project(db, config, "Same Ground", priority="normal")

    orchestrator.run_once()
    result = orchestrator.run_once()

    assert result["status"] == "awaiting_external_boundary"
    assert reporter.state(project_id)["phase"] == "awaiting_external_boundary"
    assert len(delegation.calls) == 1


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


def test_unplanned_project_with_prior_autonomy_state_is_planned(tmp_path: Path) -> None:
    planner = FakePlanner()
    orchestrator, reporter, db, config, _delegation = make_services(
        tmp_path, planner=planner
    )
    project_id, _root = make_project(db, config, "The Dark Index", priority="normal")
    reporter.set_priority(project_id, "high")

    result = orchestrator.run_once()

    assert result["status"] == "criterion_complete"
    assert reporter.state(project_id)["plan_revision"] == "plan-revision"


def test_invalid_planner_dependency_becomes_a_durable_block(tmp_path: Path) -> None:
    orchestrator, reporter, db, config, _ = make_services(tmp_path)
    project_id, _root = make_project(db, config, "The Dark Index", priority="normal")
    orchestrator.planner = RaisingPlanner(
        ValueError(
            "Criterion mvp-cloud-backup-mvp depends on unknown criterion "
            "mvp-account-creation."
        )
    )

    result = orchestrator.run_once()

    assert result["status"] == "blocked"
    assert "depends on unknown criterion" in result["reason"]
    assert reporter.state(project_id)["phase"] == "blocked"


def test_repeated_planning_decision_is_blocked_without_a_new_work_item(
    tmp_path: Path,
) -> None:
    repeated_decision = {
        "question": "Should the MVP stick to the current native ABIs?",
        "recommendation": "Stick to the current ABIs.",
        "options": [
            {
                "option": "Stick to current ABIs",
                "impact": "Keeps the existing native integration contracts.",
            },
            {
                "option": "Introduce a new ABI layer",
                "impact": "Changes the native integration contracts.",
            },
        ],
    }
    planner = FakePlanner(
        MvpPlanResult(
            criteria=[],
            external_boundaries=[],
            source_hash="repeated-decision",
            plan_revision="repeated-decision-plan",
            needs_decision=repeated_decision,
        )
    )
    orchestrator, reporter, db, config, _delegation = make_services(
        tmp_path, planner=planner
    )
    project_id, _root = make_project(db, config, "Same Ground", priority="normal")
    db.append_project_event(
        project_id,
        event_type="decision_applied",
        detail="Stick to current ABIs.",
        metadata={"decision_id": 41},
    )
    for index in range(101):
        db.append_project_event(
            project_id,
            event_type="source_updated",
            detail=f"Later non-decision project event {index}",
        )

    result = orchestrator.run_once()

    assert result["status"] == "blocked"
    assert "already resolved" in result["reason"]
    assert reporter.state(project_id)["phase"] == "blocked"
    assert [item for item in db.list_work_items() if item.kind == "founder_decision"] == []
    blocked_events = [
        event
        for event in db.list_project_events(project_id)
        if event["event_type"] == "build_blocked"
    ]
    assert len(blocked_events) == 1
    assert blocked_events[0]["metadata"]["criterion_id"] is None
    with db.connect() as conn:
        outbox = conn.execute(
            "SELECT body FROM project_autonomy_outbox WHERE project_id = ?",
            (project_id,),
        ).fetchone()
    assert outbox is not None
    assert (
        '"question": "Should the MVP stick to the current native ABIs?"'
        in outbox["body"]
    )


def test_corrected_repeated_decision_continues_with_the_mvp_plan(tmp_path: Path) -> None:
    repeated_decision = {
        "question": "Should the MVP stick to the current native ABIs?",
        "recommendation": "Stick to the current ABIs.",
        "options": [
            {"option": "Stick to current ABIs", "impact": "Keeps native contracts."},
            {"option": "Introduce a new ABI layer", "impact": "Changes native contracts."},
        ],
    }
    planner = SequencePlanner(
        MvpPlanResult([], [], "duplicate", "duplicate-plan", repeated_decision),
        MvpPlanResult(CRITERIA, [], "corrected", "corrected-plan", None),
    )
    orchestrator, reporter, db, config, _delegation = make_services(
        tmp_path, planner=planner  # type: ignore[arg-type]
    )
    project_id, _root = make_project(db, config, "The Dark Index", priority="normal")
    db.append_project_event(
        project_id,
        event_type="decision_applied",
        detail="Stick to current ABIs.",
        metadata={"decision_id": 41},
    )

    result = orchestrator.run_once()

    assert result["status"] == "criterion_complete"
    assert planner.calls == [project_id, project_id]
    assert reporter.state(project_id)["plan_revision"] == "corrected-plan"


def test_repeated_single_token_planning_decision_is_blocked_without_a_new_work_item(
    tmp_path: Path,
) -> None:
    planner = FakePlanner(
        MvpPlanResult(
            criteria=[],
            external_boundaries=[],
            source_hash="repeated-sqlite-decision",
            plan_revision="repeated-sqlite-decision-plan",
            needs_decision={
                "question": "Which database should the MVP use?",
                "recommendation": "SQLite",
                "options": [
                    {
                        "option": "SQLite",
                        "impact": "Keeps persistence local and embedded.",
                    },
                    {
                        "option": "PostgreSQL",
                        "impact": "Adds a separately operated database service.",
                    },
                ],
            },
        )
    )
    orchestrator, reporter, db, config, _delegation = make_services(
        tmp_path, planner=planner
    )
    project_id, _root = make_project(db, config, "Same Ground", priority="normal")
    db.append_project_event(
        project_id,
        event_type="decision_applied",
        detail="SQLite",
        metadata={"decision_id": 42},
    )

    result = orchestrator.run_once()

    assert result["status"] == "blocked"
    assert "already resolved" in result["reason"]
    assert reporter.state(project_id)["phase"] == "blocked"
    assert [item for item in db.list_work_items() if item.kind == "founder_decision"] == []


def test_two_urgent_projects_claim_concurrently_but_never_twice_each(tmp_path: Path) -> None:
    orchestrator, _reporter, db, config, _delegation = make_services(tmp_path)
    same_ground, _ = make_project(db, config, "Same Ground")
    dark_index, _ = make_project(db, config, "The Dark Index")

    claims = orchestrator.claim_ready(limit=2)

    assert {item["project_id"] for item in claims} == {same_ground, dark_index}
    assert orchestrator.claim_ready(limit=2) == []
    for claim in claims:
        orchestrator.release_claim(claim)


def test_repairs_use_unique_work_keys_and_requeue_after_budget(tmp_path: Path) -> None:
    delegation = FakeDelegation([failed_dispatch(f"failure {index}") for index in range(4)])
    orchestrator, reporter, db, config, _ = make_services(
        tmp_path, delegation=delegation, repairs=3
    )
    project_id, root = make_project(db, config, "Same Ground")

    result = orchestrator.run_once()

    assert result["status"] == "repair_pending"
    assert len(delegation.calls) == 4
    assert len({call["unique_key"] for call in delegation.calls}) == 4
    assert [call["unique_key"].rsplit(":", 1)[-1] for call in delegation.calls] == [
        "0",
        "1",
        "2",
        "3",
    ]
    state = reporter.state(project_id)
    assert state["phase"] == "ready_for_next_increment"
    assert state["blocking_type"] is None
    assert state["blocking_reason"] is None
    assert "automated repair" in state["next_action"]
    assert subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout == ""
    assert not list((root / "src").glob("increment-*.txt"))
    quarantined = config.paths.data_dir / "project-autonomy-failed-attempts" / str(project_id)
    assert len(list(quarantined.glob("*/untracked/src/increment-*.txt"))) == 4
    assert not [
        event
        for event in db.list_project_events(project_id, limit=None)
        if event["event_type"] == "build_blocked"
    ]
    repair_events = [
        event
        for event in db.list_project_events(project_id, limit=None)
        if event["event_type"] == "mechanical_repair_requeued"
    ]
    assert len(repair_events) == 1


def test_destructive_manifest_rewrite_is_quarantined_without_retry(tmp_path: Path) -> None:
    class ManifestReplacingDelegation:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def queue_delegation(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(kwargs)
            root = Path(kwargs["workspace"])
            (root / "pubspec.yaml").write_text(
                "name: replaced_app\ndependencies:\n  firebase_core: ^4.0.0\n",
                encoding="utf-8",
            )
            return {"dispatch": passing_dispatch()}

    delegation = ManifestReplacingDelegation()
    orchestrator, reporter, db, config, _ = make_services(
        tmp_path, delegation=delegation, repairs=3  # type: ignore[arg-type]
    )
    project_id, root = make_project(db, config, "The Dark Index")
    original = (
        "name: the_dark_index\n"
        "description: Private-first collection\n"
        "publish_to: none\n"
        "version: 0.1.0+1\n"
        "environment:\n"
        "  sdk: ^3.12.2\n"
        "dependencies:\n"
        "  flutter:\n"
        "    sdk: flutter\n"
        "  sqflite: ^2.4.3\n"
        "  path: ^1.9.1\n"
        "dev_dependencies:\n"
        "  flutter_test:\n"
        "    sdk: flutter\n"
        "flutter:\n"
        "  uses-material-design: true\n"
    )
    (root / "pubspec.yaml").write_text(original, encoding="utf-8")
    subprocess.run(["git", "add", "pubspec.yaml"], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "add manifest",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )

    result = orchestrator.run_once()

    assert result["status"] == "blocked"
    assert "protected dependency manifest" in result["reason"]
    assert len(delegation.calls) == 1
    assert reporter.state(project_id)["phase"] == "blocked"
    assert (root / "pubspec.yaml").read_text(encoding="utf-8") == original
    assert subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout == ""
    events = db.list_project_events(project_id, limit=None)
    quarantines = [
        event
        for event in events
        if event["event_type"] == "autonomy_attempt_quarantined"
        and event.get("metadata", {}).get("failure_class")
        == "protected_manifest_rewrite"
    ]
    assert len(quarantines) == 1


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
    assert (root / "src" / "increment-1.txt").is_file()
    assert (root / "src" / "increment-2.txt").is_file()
    quarantined = config.paths.data_dir / "project-autonomy-failed-attempts" / str(project_id)
    assert not quarantined.exists()
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


def test_reversible_local_dependency_choice_is_applied_without_founder_interruption(
    tmp_path: Path,
) -> None:
    dispatch = {
        "status": "needs_decision",
        "ok": True,
        "founder_question": {
            "question": (
                "Should I add the sqflite package for the local database, or use "
                "another local storage method?"
            ),
            "recommendation": "Add sqflite for on-device SQLite storage.",
            "options": [
                "Add sqflite package",
                "Use SharedPreferences",
            ],
        },
        "artifact": "Paused for a reversible local dependency choice.",
    }
    delegation = FakeDelegation([dispatch, passing_dispatch()])
    orchestrator, reporter, db, config, _ = make_services(
        tmp_path, delegation=delegation
    )
    project_id, _root = make_project(db, config, "The Dark Index")

    result = orchestrator.run_once()

    assert result["status"] == "criterion_complete"
    assert len(delegation.calls) == 2
    assert "Add sqflite for on-device SQLite storage." in delegation.calls[1]["brief"]
    assert "add it through the ecosystem's package manager" in delegation.calls[1]["brief"]
    assert reporter.state(project_id)["phase"] == "ready_for_next_increment"
    assert any(
        event["event_type"] == "local_implementation_choice_applied"
        and "sqflite" in event["detail"]
        for event in db.list_project_events(project_id, limit=None)
    )


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


def test_worker_records_traceback_for_unexpected_failure(tmp_path: Path) -> None:
    orchestrator, _reporter, _db, _config, _delegation = make_services(tmp_path)
    calls = 0

    def run_once() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TypeError("planner output was malformed")
        orchestrator._shutdown = True
        return {"status": "shutdown"}

    orchestrator.run_once = run_once  # type: ignore[method-assign]
    orchestrator._worker_loop()

    result = orchestrator.status()["recent_results"][-2]
    assert result["status"] == "worker_error"
    assert "TypeError: planner output was malformed" in result["traceback"]


def test_recovered_building_phase_can_repair_its_dirty_workspace(tmp_path: Path) -> None:
    orchestrator, reporter, db, config, _delegation = make_services(tmp_path)
    project_id, root = make_project(db, config, "Same Ground")
    reporter.begin_increment(project_id, criterion_id="mvp-one", run_id=17)
    (root / "interrupted.txt").write_text("unfinished prior run\n", encoding="utf-8")

    result = orchestrator.run_once()

    assert result["status"] == "criterion_complete"
    assert reporter.state(project_id)["mvp_criteria"][0]["status"] == "complete"
    assert not (root / "interrupted.txt").exists()
    quarantined = config.paths.data_dir / "project-autonomy-failed-attempts" / str(project_id)
    assert len(list(quarantined.glob("*/untracked/interrupted.txt"))) == 1


def test_non_native_or_cloud_fallback_policy_fails_closed(tmp_path: Path) -> None:
    orchestrator, _reporter, db, config, _delegation = make_services(tmp_path)
    make_project(db, config, "Same Ground")
    object.__setattr__(config.delegation, "engine", "bridge")

    result = orchestrator.run_once()

    assert result["status"] == "blocked"
    assert "native" in result["reason"].lower()
