import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from cofounder_kernel.api import create_app
from cofounder_kernel.config import KernelConfig, OllamaConfig, PathConfig, ProjectIntakeConfig
from cofounder_kernel.db import utc_now
from cofounder_kernel.ollama import GenerateResult, OllamaClient
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


def fresh_verification(root: Path, head: str) -> dict:
    return {
        "ok": True,
        "project_path": str(root.resolve()),
        "repo_head": head,
        "repo_status": "",
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
        subprocess.run(
            ["git", "init", "--initial-branch=main"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(["git", "add", "project.md"], cwd=root, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.email=test@test",
                "-c",
                "user.name=test",
                "commit",
                "-m",
                "initial project",
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
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


def test_project_api_bounds_captured_command_output(tmp_path: Path, monkeypatch) -> None:
    app, client, ids = make_client(tmp_path, monkeypatch)
    project = app.state.db.get_project(ids["Same Ground"])
    metadata = dict(project["metadata"])
    metadata["last_check"] = {"output": "x" * 600, "returncode": 0}
    app.state.db.upsert_project(
        canonical_path=project["canonical_path"],
        name=project["name"],
        product_type=project["product_type"],
        distribution_targets=project["distribution_targets"],
        lifecycle_state=project["lifecycle_state"],
        repo_fingerprint=project["repo_fingerprint"],
        metadata=metadata,
    )

    payload = client.get(
        f"/project-intake/projects/{ids['Same Ground']}"
    ).json()["project"]

    assert payload["metadata"]["last_check"]["returncode"] == 0
    assert payload["metadata"]["last_check"]["output"] == ("x" * 240) + "…"


def test_api_projects_every_phase(tmp_path: Path, monkeypatch) -> None:
    app, client, ids = make_client(tmp_path, monkeypatch)
    reporter = app.state.project_autonomy
    reporter.bus = None  # projection test only; no notifications leave the process
    project_id = ids["Same Ground"]
    root = Path(app.state.db.get_project(project_id)["canonical_path"])
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    criteria = [{"id": "auth", "title": "Sign-in works"}, {"id": "feed", "title": "Feed renders"}]

    def phase() -> str:
        return client.get(f"/project-intake/projects/{project_id}").json()["project"]["autonomy"]["phase"]

    reporter.plan(project_id, criteria=criteria)
    assert phase() == "planning"
    reporter.begin_increment(project_id, criterion_id="auth")
    assert phase() == "building"
    reporter.begin_verification(project_id)
    assert phase() == "verifying"
    reporter.record_increment(
        project_id,
        summary="increment done",
        verification=fresh_verification(root, head),
    )
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
    reporter.begin_increment(building, criterion_id="auth", run_id=1)
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
    assert status["totals"]["paused"] == 0
    by_name = {item["name"]: item["status"] for item in status["projects"]}
    assert by_name["Same Ground"] == "scaffold_verified"
    assert by_name["The Dark Index"] == "actively_building"
    assert all("autonomy" in item for item in status["projects"])


def test_planning_and_ready_are_not_actively_building(tmp_path: Path, monkeypatch) -> None:
    app, client, ids = make_client(tmp_path, monkeypatch)
    reporter = app.state.project_autonomy
    reporter.bus = None
    project_id = ids["Same Ground"]
    root = Path(app.state.db.get_project(project_id)["canonical_path"])
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    reporter.plan(project_id, criteria=[{"id": "auth", "title": "Sign-in works"}])

    planned = client.get("/project-intake/status").json()["projects"][0]
    assert planned["status"] == "planned"

    reporter.begin_increment(project_id, criterion_id="auth", run_id=7)
    reporter.begin_verification(project_id, run_id=7)
    reporter.record_increment(
        project_id,
        summary="increment complete",
        verification=fresh_verification(root, head),
    )
    ready = client.get("/project-intake/status").json()["projects"][0]
    assert ready["status"] == "ready_for_next_increment"
    assert ready["status"] != "actively_building"


def test_runtime_and_portfolio_report_same_active_phase(tmp_path: Path, monkeypatch) -> None:
    app, client, ids = make_client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        OllamaClient,
        "chat",
        lambda self, **kwargs: GenerateResult(
            response="All projects are verified.", model="test", raw={}
        ),
    )
    project_id = ids["Same Ground"]
    reporter = app.state.project_autonomy
    reporter.bus = None
    reporter.plan(project_id, criteria=[{"id": "auth", "title": "Sign-in works"}])
    reporter.begin_increment(project_id, criterion_id="auth", run_id=11)

    portfolio = client.get("/project-intake/status").json()
    reply = client.post(
        "/runtime/respond",
        json={
            "message": "What is the status of my projects?",
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "use_tools": False,
            "contrarian": False,
        },
    ).json()["response"]

    assert portfolio["projects"][0]["status"] == "actively_building"
    assert "status: actively_building" in reply
    assert "phase: building" in reply
    assert "state: verified" not in reply
