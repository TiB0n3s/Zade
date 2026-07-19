from pathlib import Path

from cofounder_kernel.config import KernelConfig, PathConfig, ProjectIntakeConfig
from cofounder_kernel.db import KernelDatabase
import cofounder_kernel.project_intake as project_intake_module
from cofounder_kernel.project_intake import ProjectIntakeService, parse_project_decision_reply


MOBILE_MANIFEST = """---
name: Same Ground
product_type: mobile_application
lifecycle_state: intake
distribution_targets: [google_play, apple_app_store_eventual]
scaffold_on_intake: true
---

# Same Ground
"""


class FakeDelegation:
    def __init__(self):
        self.calls = []

    def queue_delegation(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "item_id": None,
            "status": "approved",
            "auto_invoked": True,
            "dispatch": {"ok": True, "auto_verification": {"ok": True}},
        }


class UnverifiedDelegation:
    def queue_delegation(self, **kwargs):
        return {
            "item_id": None,
            "status": "approved",
            "auto_invoked": True,
            "dispatch": {
                "ok": True,
                "status": "ok",
                "auto_verification": {"ok": False, "output": "typecheck failed"},
            },
        }


class BlockingDelegation:
    def queue_delegation(self, **kwargs):
        return {
            "item_id": None,
            "status": "approved",
            "auto_invoked": True,
            "dispatch": {
                "ok": True,
                "status": "needs_decision",
                "decision_item_id": 91,
                "founder_question": {
                    "question": "Which local database should the app use?",
                    "options": ["SQLite", "Realm"],
                },
            },
        }


class FakeBus:
    def __init__(self):
        self.calls = []

    def notify(self, **kwargs):
        self.calls.append(kwargs)
        return {"status": "delivered"}


class FakeApprovals:
    def __init__(self):
        self.calls = []

    def approve_work_item(self, item_id, **kwargs):
        self.calls.append((item_id, kwargs))
        return {
            "dispatch": "dispatched",
            "dispatch_result": {
                "ok": True,
                "status": "ok",
                "artifact": "scaffold complete",
                "auto_verification": {"ok": True},
            },
        }


def make_service(tmp_path: Path, *, delegation=None, bus=None, approvals=None):
    hot = tmp_path / "brain"
    config = KernelConfig(
        paths=PathConfig(hot_root=hot, cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        project_intake=ProjectIntakeConfig(enabled=True, scaffold_on_intake=True),
    )
    db = KernelDatabase(config.paths.database_path)
    db.migrate()
    return (
        ProjectIntakeService(
            config=config,
            db=db,
            delegation=delegation,
            bus=bus,
            approvals=approvals,
        ),
        config,
        db,
    )


def test_scan_accepts_only_qualifying_direct_children(tmp_path: Path) -> None:
    service, config, _db = make_service(tmp_path)
    valid = config.paths.project_intake_dir / "Same Ground"
    valid.mkdir(parents=True)
    (valid / "project.md").write_text(MOBILE_MANIFEST, encoding="utf-8")
    nested = valid / "nested"
    nested.mkdir()
    (nested / ".git").mkdir()
    unrelated = config.paths.project_intake_dir / "notes"
    unrelated.mkdir()

    result = service.scan(auto_run=False)

    assert result["created_count"] == 1
    assert [item["name"] for item in result["projects"]] == ["Same Ground"]


def test_scan_is_idempotent_and_preserves_mobile_store_intent(tmp_path: Path) -> None:
    service, config, db = make_service(tmp_path)
    project = config.paths.project_intake_dir / "Same Ground"
    project.mkdir(parents=True)
    (project / "project.md").write_text(MOBILE_MANIFEST, encoding="utf-8")

    first = service.scan(auto_run=False)
    second = service.scan(auto_run=False)
    stored = db.find_project_by_path(str(project))

    assert first["created_count"] == 1
    assert second["created_count"] == 0
    assert second["existing_count"] == 1
    assert stored["product_type"] == "mobile_application"
    assert stored["distribution_targets"] == ["google_play", "apple_app_store_eventual"]


def test_documentation_only_project_initializes_git_and_routes_scaffold(tmp_path: Path) -> None:
    delegation = FakeDelegation()
    service, config, db = make_service(tmp_path, delegation=delegation)
    project = config.paths.project_intake_dir / "Same Ground"
    project.mkdir(parents=True)
    (project / "project.md").write_text(MOBILE_MANIFEST, encoding="utf-8")
    (project / "Same_Ground_Zade_Handoff.md").write_text(
        "Same Ground is a mobile app for veterans, EMTs, and law enforcement.", encoding="utf-8"
    )

    result = service.scan(auto_run=True)
    stored = db.find_project_by_path(str(project))

    assert (project / ".git").is_dir()
    assert result["projects"][0]["lifecycle_state"] == "verified"
    assert stored["lifecycle_state"] == "verified"
    assert delegation.calls[0]["workspace"] == str(project.resolve())
    assert delegation.calls[0]["directed"] is True
    assert "mobile application" in delegation.calls[0]["task"]
    assert "Google Play" in delegation.calls[0]["acceptance"]
    assert "Apple App Store" in delegation.calls[0]["acceptance"]


def test_scaffold_context_reports_available_mobile_tooling(tmp_path: Path, monkeypatch) -> None:
    delegation = FakeDelegation()
    monkeypatch.setattr(
        project_intake_module.shutil,
        "which",
        lambda name: f"C:/tools/{name}.exe" if name in {"node", "npm", "java"} else None,
    )
    service, config, _db = make_service(tmp_path, delegation=delegation)
    project = config.paths.project_intake_dir / "Same Ground"
    project.mkdir(parents=True)
    (project / "project.md").write_text(MOBILE_MANIFEST, encoding="utf-8")

    service.scan(auto_run=True)

    context = delegation.calls[0]["context"]
    assert "node: available" in context
    assert "npm: available" in context
    assert "flutter: unavailable" in context
    assert "Do not choose a framework whose required local toolchain is unavailable" in context


def test_failed_kernel_verification_blocks_project_and_notifies(tmp_path: Path) -> None:
    bus = FakeBus()
    service, config, _db = make_service(
        tmp_path, delegation=UnverifiedDelegation(), bus=bus
    )
    project = config.paths.project_intake_dir / "Same Ground"
    project.mkdir(parents=True)
    (project / "project.md").write_text(MOBILE_MANIFEST, encoding="utf-8")

    stored = service.scan(auto_run=True)["projects"][0]

    assert stored["lifecycle_state"] == "blocked"
    assert stored["metadata"]["blocked_reason"] == "kernel auto-verification did not pass"
    assert bus.calls[0]["topic"] == "project.build_blocked"


def test_reconciliation_scan_does_not_restart_a_completed_scaffold(tmp_path: Path) -> None:
    delegation = FakeDelegation()
    service, config, db = make_service(tmp_path, delegation=delegation)
    project = config.paths.project_intake_dir / "Same Ground"
    project.mkdir(parents=True)
    (project / "project.md").write_text(MOBILE_MANIFEST, encoding="utf-8")

    first = service.scan(auto_run=True)
    second = service.scan(auto_run=True)
    stored = db.find_project_by_path(str(project))

    assert first["projects"][0]["lifecycle_state"] == "verified"
    assert second["projects"][0]["lifecycle_state"] == "verified"
    assert stored["lifecycle_state"] == "verified"
    assert len(delegation.calls) == 1


def test_needs_decision_blocks_project_and_notifies_with_resume_command(tmp_path: Path) -> None:
    bus = FakeBus()
    service, config, _db = make_service(tmp_path, delegation=BlockingDelegation(), bus=bus)
    project = config.paths.project_intake_dir / "Same Ground"
    project.mkdir(parents=True)
    (project / "project.md").write_text(MOBILE_MANIFEST, encoding="utf-8")

    result = service.scan(auto_run=True)
    assert not result["errors"], result["errors"]
    stored = result["projects"][0]

    assert stored["lifecycle_state"] == "blocked"
    assert stored["metadata"]["decision_id"] == 91
    assert stored["metadata"]["founder_question"]["question"].startswith("Which local")
    assert bus.calls[0]["topic"] == "project.decision_required"
    assert "decision 91: <your answer>" in bus.calls[0]["body"]
    assert bus.calls[0]["dedupe_key"] == "project:1:decision:91"


def test_resolve_decision_injects_answer_into_paused_work_item_and_resumes(tmp_path: Path) -> None:
    approvals = FakeApprovals()
    service, config, db = make_service(tmp_path, approvals=approvals)
    project_root = config.paths.project_intake_dir / "Same Ground"
    project_root.mkdir(parents=True)
    (project_root / "project.md").write_text(MOBILE_MANIFEST, encoding="utf-8")
    project = service.scan(auto_run=False)["projects"][0]
    decision_id, _created = db.enqueue_work_item(
        kind="founder_decision",
        title="Decision needed",
        detail="Choose storage",
        action="external.delegation.run",
        target="native-coding-agent",
        permission_tier="L3_EXTERNAL_ACTION",
        metadata={
            "task": "Build Same Ground",
            "brief": "Resume the initial scaffold.",
            "workspace": str(project_root.resolve()),
            "founder_decision": True,
        },
    )
    db.upsert_project(
        canonical_path=project["canonical_path"],
        name=project["name"],
        product_type=project["product_type"],
        distribution_targets=project["distribution_targets"],
        lifecycle_state="blocked",
        repo_fingerprint=project["repo_fingerprint"],
        metadata={**project["metadata"], "decision_id": decision_id},
    )

    resumed = service.resolve_decision(decision_id, "Use SQLite and keep all data local.")
    updated_item = db.get_work_item(decision_id)

    assert resumed["lifecycle_state"] == "verified"
    assert "decision_id" not in resumed["metadata"]
    assert "Use SQLite and keep all data local." in updated_item.metadata["brief"]
    assert approvals.calls == [
        (
            decision_id,
            {
                "resolved_by": "founder.telegram",
                "note": "Use SQLite and keep all data local.",
                "dispatch": True,
                "typed_confirmation": "",
            },
        )
    ]


def test_parse_project_decision_reply_requires_explicit_decision_id() -> None:
    assert parse_project_decision_reply("decision 42: Use SQLite") == (42, "Use SQLite")
    assert parse_project_decision_reply("/decision #17 - Choose option B") == (17, "Choose option B")
    assert parse_project_decision_reply("Use SQLite") is None
