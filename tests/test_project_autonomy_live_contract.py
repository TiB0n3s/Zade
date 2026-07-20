from pathlib import Path

from fastapi.testclient import TestClient

from cofounder_kernel.api import create_app
from cofounder_kernel.config import (
    KernelConfig,
    OllamaConfig,
    PathConfig,
    ProjectIntakeConfig,
    SecurityConfig,
)
from cofounder_kernel.ollama import OllamaClient


MANIFEST = """---
name: Same Ground
product_type: mobile_application
lifecycle_state: intake
distribution_targets: [google_play, apple_app_store_eventual]
scaffold_on_intake: true
---
# Same Ground
"""


def fake_health(self: OllamaClient) -> dict:
    return {"version": "test"}


def make_app(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        paths=PathConfig(
            hot_root=tmp_path / "brain",
            cold_root=tmp_path / "cold",
            data_dir=tmp_path / "data",
        ),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
        security=SecurityConfig(protect_mutations=False),
        project_intake=ProjectIntakeConfig(
            enabled=True,
            scaffold_on_intake=True,
            autonomy_enabled=True,
            autonomy_max_workers=2,
            autonomy_reconcile_seconds=60,
        ),
    )
    root = config.paths.project_intake_dir / "Same Ground"
    root.mkdir(parents=True)
    (root / "project.md").write_text(MANIFEST, encoding="utf-8")
    (root / "MVP.md").write_text("# MVP\nSearch resources offline.\n", encoding="utf-8")
    return create_app(config, run_boot_maintenance=False), root


def test_scan_registers_and_wakes_each_project_without_direct_build(
    tmp_path: Path, monkeypatch
) -> None:
    app, _root = make_app(tmp_path, monkeypatch)
    calls: list[tuple[int | None, str]] = []
    monkeypatch.setattr(
        app.state.project_autonomy_orchestrator,
        "wake",
        lambda project_id=None, *, reason: calls.append((project_id, reason))
        or {"accepted": True, "project_id": project_id, "reason": reason},
    )
    monkeypatch.setattr(
        app.state.delegation,
        "queue_delegation",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("scan dispatched a build")),
    )
    client = TestClient(app)

    response = client.post("/project-intake/scan")

    assert response.status_code == 200
    payload = response.json()
    assert payload["created_count"] == 1
    assert payload["autonomy_wake_count"] == 1
    assert len({project_id for project_id, _reason in calls}) == 1
    assert calls[0][0] == payload["projects"][0]["id"]


def test_pause_resume_priority_and_status_use_shared_backend_truth(
    tmp_path: Path, monkeypatch
) -> None:
    app, _root = make_app(tmp_path, monkeypatch)
    client = TestClient(app)
    project_id = client.post("/project-intake/scan").json()["projects"][0]["id"]

    priority = client.post(
        f"/project-intake/projects/{project_id}/autonomy/priority",
        json={"priority": "urgent"},
    )
    paused = client.post(
        f"/project-intake/projects/{project_id}/autonomy/pause",
        json={"reason": "Founder is reviewing scope"},
    )
    status = client.get("/project-intake/autonomy/status").json()

    assert priority.status_code == 200
    assert priority.json()["project"]["autonomy"]["priority"] == "urgent"
    assert paused.json()["project"]["status"] == "paused"
    assert status["portfolio"]["totals"]["paused"] == 1
    assert status["portfolio"]["projects"][0]["autonomy"]["pause_reason"] == (
        "Founder is reviewing scope"
    )

    resumed = client.post(
        f"/project-intake/projects/{project_id}/autonomy/resume", json={}
    )
    assert resumed.status_code == 200
    assert resumed.json()["project"]["autonomy"]["paused"] is False


def test_project_decision_resolution_resumes_orchestrator_without_dispatching_item(
    tmp_path: Path, monkeypatch
) -> None:
    app, _root = make_app(tmp_path, monkeypatch)
    client = TestClient(app)
    project_id = client.post("/project-intake/scan").json()["projects"][0]["id"]
    project = app.state.project_autonomy.get_project(project_id)
    decision = {
        "question": "Use device-local profiles or email accounts?",
        "recommendation": "Device-local profiles",
        "options": [
            {"option": "Device-local profiles", "impact": "No external identity service."},
            {"option": "Email accounts", "impact": "Adds account infrastructure."},
        ],
    }
    decision_id = app.state.project_autonomy_orchestrator._file_decision(
        project,
        decision,
        plan_revision="plan-1",
        criterion_id=None,
    )
    app.state.project_autonomy.report_needs_decision(
        project_id,
        decision_id=decision_id,
        question=decision["question"],
        recommendation=decision["recommendation"],
        options=decision["options"],
    )
    wakes: list[int | None] = []
    monkeypatch.setattr(
        app.state.project_autonomy_orchestrator,
        "wake",
        lambda project_id=None, **kwargs: wakes.append(project_id)
        or {"accepted": True},
    )
    monkeypatch.setattr(
        app.state.delegation,
        "run_from_work_item",
        lambda item: (_ for _ in ()).throw(AssertionError("decision item dispatched")),
    )

    response = client.post(
        f"/project-intake/decisions/{decision_id}/resolve",
        json={"resolved_by": "founder.ui", "note": "Use device-local profiles"},
    )

    assert response.status_code == 200
    assert app.state.project_autonomy.state(project_id)["phase"] == "building"
    assert app.state.db.get_work_item(decision_id).status == "done"
    assert wakes == [project_id]


def test_lifespan_starts_and_stops_project_autonomy_workers(
    tmp_path: Path, monkeypatch
) -> None:
    app, _root = make_app(tmp_path, monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(
        app.state.project_autonomy_orchestrator,
        "start",
        lambda: calls.append("start"),
    )
    monkeypatch.setattr(
        app.state.project_autonomy_orchestrator,
        "shutdown",
        lambda wait=False: calls.append(f"shutdown:{wait}"),
    )

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

    assert calls == ["start", "shutdown:False"]
