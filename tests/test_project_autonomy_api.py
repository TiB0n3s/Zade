from pathlib import Path

from fastapi.testclient import TestClient

from cofounder_kernel.api import create_app
from cofounder_kernel.config import KernelConfig, OllamaConfig, PathConfig, ProjectIntakeConfig
from cofounder_kernel.db import utc_now
from cofounder_kernel.ollama import OllamaClient
from cofounder_kernel.project_autonomy import AUTONOMY_PROJECTION_KEYS


MANIFEST = """---
name: {name}
product_type: mobile_application
lifecycle_state: intake
distribution_targets: [google_play, apple_app_store_eventual]
scaffold_on_intake: false
---
"""


def fake_health(self: OllamaClient) -> dict:
    return {"version": "test"}


def fresh_verification() -> dict:
    return {
        "ok": True,
        "checked_at": utc_now(),
        "checks": [{"argv": ["npm", "test"], "ok": True, "returncode": 0, "output": "passed"}],
    }


def make_client(tmp_path: Path, monkeypatch, names: tuple[str, ...] = ("Same Ground",)):
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        paths=PathConfig(hot_root=tmp_path / "brain", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
        project_intake=ProjectIntakeConfig(enabled=True, scaffold_on_intake=True),
    )
    for name in names:
        root = config.paths.project_intake_dir / name
        root.mkdir(parents=True)
        (root / "project.md").write_text(MANIFEST.format(name=name), encoding="utf-8")
    app = create_app(config, run_boot_maintenance=False)
    client = TestClient(app)
    scanned = client.post("/project-intake/scan").json()
    ids = {item["name"]: item["id"] for item in scanned["projects"]}
    return app, client, ids


def test_project_endpoints_expose_full_autonomy_projection(tmp_path: Path, monkeypatch) -> None:
    _app, client, ids = make_client(tmp_path, monkeypatch)
    project_id = ids["Same Ground"]

    listed = client.get("/project-intake/projects").json()["items"][0]
    fetched = client.get(f"/project-intake/projects/{project_id}").json()["project"]

    for payload in (listed, fetched):
        assert set(AUTONOMY_PROJECTION_KEYS) <= set(payload["autonomy"])
        assert payload["autonomy"]["mvp_complete"] is False
    # Existing representation is preserved alongside the new projection.
    assert fetched["product_type"] == "mobile_application"
    assert fetched["distribution_targets"] == ["google_play", "apple_app_store_eventual"]


def test_api_projects_every_phase(tmp_path: Path, monkeypatch) -> None:
    app, client, ids = make_client(tmp_path, monkeypatch)
    reporter = app.state.project_autonomy
    reporter.bus = None  # projection test only; no notifications leave the process
    project_id = ids["Same Ground"]
    criteria = [{"id": "auth", "title": "Sign-in works"}, {"id": "feed", "title": "Feed renders"}]

    def phase() -> str:
        return client.get(f"/project-intake/projects/{project_id}").json()["project"]["autonomy"]["phase"]

    reporter.plan(project_id, criteria=criteria)
    assert phase() == "planning"
    reporter.begin_increment(project_id, criterion_id="auth")
    assert phase() == "building"
    reporter.begin_verification(project_id)
    assert phase() == "verifying"
    reporter.record_increment(project_id, summary="increment done")
    assert phase() == "ready_for_next_increment"
    reporter.report_needs_decision(
        project_id,
        decision_id=31,
        question="q?",
        recommendation="r",
        options=[{"option": "a", "impact": "x"}, {"option": "b", "impact": "y"}],
    )
    assert phase() == "needs_decision"
    reporter.resume_after_decision(31)
    assert phase() == "building"
    reporter.report_approval_required(
        project_id,
        approval_request_id=32,
        action="publish",
        reason="publishing boundary",
        boundary="publishing_deployment",
    )
    assert phase() == "approval_required"
    reporter.resume_after_approval(32, approved=True)
    assert phase() == "building"
    reporter.report_blocked(project_id, reason="verification failed twice")
    assert phase() == "blocked"

    events = client.get(f"/project-intake/projects/{project_id}/events").json()["items"]
    assert [item["event_type"] for item in events[:2]] == ["build_blocked", "approval_resolved"]


def test_portfolio_status_endpoint_distinguishes_buckets(tmp_path: Path, monkeypatch) -> None:
    app, client, ids = make_client(tmp_path, monkeypatch, names=("Same Ground", "The Dark Index"))
    reporter = app.state.project_autonomy
    reporter.bus = None
    building = ids["The Dark Index"]
    reporter.plan(building, criteria=[{"id": "auth", "title": "Sign-in works"}])
    reporter.begin_increment(building, criterion_id="auth")
    # Same Ground stays lifecycle-verified with no autonomy plan.
    same_ground = app.state.db.get_project(ids["Same Ground"])
    app.state.db.upsert_project(
        canonical_path=same_ground["canonical_path"],
        name=same_ground["name"],
        product_type=same_ground["product_type"],
        distribution_targets=same_ground["distribution_targets"],
        lifecycle_state="verified",
        repo_fingerprint=same_ground["repo_fingerprint"],
        metadata=same_ground["metadata"],
    )

    status = client.get("/project-intake/status").json()

    assert status["totals"]["scaffold_verified"] == 1
    assert status["totals"]["actively_building"] == 1
    assert status["totals"]["mvp_complete"] == 0
    by_name = {item["name"]: item["status"] for item in status["projects"]}
    assert by_name["Same Ground"] == "scaffold_verified"
    assert by_name["The Dark Index"] == "actively_building"
    assert all("autonomy" in item for item in status["projects"])
