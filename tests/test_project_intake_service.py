from pathlib import Path

from cofounder_kernel.config import KernelConfig, PathConfig, ProjectIntakeConfig
from cofounder_kernel.db import KernelDatabase
from cofounder_kernel.project_intake import ProjectIntakeService


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
        return {"item_id": None, "status": "approved", "auto_invoked": True, "dispatch": {"ok": True}}


def make_service(tmp_path: Path, *, delegation=None):
    hot = tmp_path / "brain"
    config = KernelConfig(
        paths=PathConfig(hot_root=hot, cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        project_intake=ProjectIntakeConfig(enabled=True, scaffold_on_intake=True),
    )
    db = KernelDatabase(config.paths.database_path)
    db.migrate()
    return ProjectIntakeService(config=config, db=db, delegation=delegation), config, db


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
