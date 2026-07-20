from pathlib import Path

from fastapi.testclient import TestClient

from cofounder_kernel.api import create_app
from cofounder_kernel.channel_auth import ChannelAuth
from cofounder_kernel.config import KernelConfig, OllamaConfig, PathConfig, ProjectIntakeConfig
from cofounder_kernel.ollama import OllamaClient
from cofounder_kernel.telegram_adapter import InboundTelegram


def fake_health(self: OllamaClient) -> dict:
    return {"version": "test"}


def test_project_intake_routes_register_and_expose_mobile_project(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        paths=PathConfig(hot_root=tmp_path / "brain", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
        project_intake=ProjectIntakeConfig(enabled=True, scaffold_on_intake=True),
    )
    root = config.paths.project_intake_dir / "The Dark Index"
    root.mkdir(parents=True)
    (root / "project.md").write_text(
        """---
name: The Dark Index
product_type: mobile_application
lifecycle_state: intake
distribution_targets: [google_play, apple_app_store_eventual]
scaffold_on_intake: false
---
""",
        encoding="utf-8",
    )
    app = create_app(config, run_boot_maintenance=False)
    client = TestClient(app)

    scanned = client.post("/project-intake/scan")
    listed = client.get("/project-intake/projects")
    project_id = scanned.json()["projects"][0]["id"]
    fetched = client.get(f"/project-intake/projects/{project_id}")
    inventory = client.get("/self-inventory")

    assert scanned.status_code == 200
    assert listed.json()["items"][0]["name"] == "The Dark Index"
    assert fetched.json()["project"]["product_type"] == "mobile_application"
    assert fetched.json()["project"]["distribution_targets"] == [
        "google_play",
        "apple_app_store_eventual",
    ]
    assert inventory.json()["project_intake_layer"]["root"] == str(config.paths.project_intake_dir)
    assert "The Dark Index [mobile_application]" in app.state.runtime._render_self_knowledge()
    assert "google_play" in app.state.runtime._render_self_knowledge()


def test_authenticated_telegram_decision_reply_requires_existing_ui(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        paths=PathConfig(hot_root=tmp_path / "brain", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
        project_intake=ProjectIntakeConfig(enabled=True, scaffold_on_intake=True),
    )
    app = create_app(config, run_boot_maintenance=False)
    enrollment = ChannelAuth(app.state.db).begin_enrollment("telegram")
    ChannelAuth(app.state.db).confirm_enrollment("telegram", "42", enrollment["code"])
    calls = []

    def resolve(decision_id: int, answer: str, *, resolved_by: str = "founder.telegram"):
        calls.append((decision_id, answer, resolved_by))
        return {"id": 1, "name": "Same Ground", "lifecycle_state": "building"}

    monkeypatch.setattr(app.state.project_intake, "resolve_decision", resolve)

    result = app.state.telegram_adapter._route(
        InboundTelegram(external_id="42", chat_id=42, text="decision 77: Use SQLite")
    )

    assert result["status"] == "project_decision_requires_ui"
    assert "Open Zade" in result["reply"]
    assert "approval" in result["reply"].lower()
    assert calls == []


def test_project_intake_verify_route_uses_non_mutating_scaffold_verifier(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        paths=PathConfig(
            hot_root=tmp_path / "brain",
            cold_root=tmp_path / "cold",
            data_dir=tmp_path / "data",
        ),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
        project_intake=ProjectIntakeConfig(enabled=True, scaffold_on_intake=True),
    )
    app = create_app(config, run_boot_maintenance=False)
    client = TestClient(app)
    calls: list[int] = []

    def verify_existing(project_id: int):
        calls.append(project_id)
        return {"id": project_id, "name": "Same Ground", "lifecycle_state": "verified"}

    monkeypatch.setattr(app.state.project_intake, "verify_existing", verify_existing)

    response = client.post("/project-intake/projects/7/verify")

    assert response.status_code == 200
    assert response.json()["project"]["lifecycle_state"] == "verified"
    assert calls == [7]


def test_ui_decision_resolution_resumes_same_autonomy_criterion(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        paths=PathConfig(
            hot_root=tmp_path / "brain",
            cold_root=tmp_path / "cold",
            data_dir=tmp_path / "data",
        ),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
        project_intake=ProjectIntakeConfig(enabled=True, scaffold_on_intake=True),
    )
    root = config.paths.project_intake_dir / "Same Ground"
    root.mkdir(parents=True)
    (root / "project.md").write_text(
        """---
name: Same Ground
product_type: mobile_application
lifecycle_state: verified
distribution_targets: [google_play, apple_app_store_eventual]
scaffold_on_intake: false
---
""",
        encoding="utf-8",
    )
    app = create_app(config, run_boot_maintenance=False)
    client = TestClient(app)
    project = client.post("/project-intake/scan").json()["projects"][0]
    reporter = app.state.project_autonomy
    reporter.plan(project["id"], criteria=[{"id": "mvp-1", "title": "Core flow"}])
    reporter.begin_increment(project["id"], criterion_id="mvp-1")
    decision_id, _created = app.state.db.enqueue_work_item(
        kind="founder_decision",
        title="Choose storage",
        detail="Choose local storage.",
        action="project.decision.resolve",
        target="Same Ground",
        permission_tier="L1_MEMORY_WRITE",
        metadata={
            "workspace": str(root.resolve()),
            "project_id": project["id"],
            "project_autonomy": True,
            "brief": "Continue the current criterion.",
            "founder_decision": True,
        },
        unique_key="test:ui-project-decision",
    )
    reporter.report_needs_decision(
        project["id"],
        decision_id=decision_id,
        question="Which storage engine?",
        recommendation="SQLite",
        options=[
            {"option": "SQLite", "impact": "local-first"},
            {"option": "Realm", "impact": "native dependency"},
        ],
    )
    requested_event = next(
        event
        for event in app.state.db.list_project_events(project["id"])
        if event["event_type"] == "decision_requested"
    )
    assert requested_event["work_item_id"] == decision_id

    monkeypatch.setattr(
        app.state.approvals,
        "approve_work_item",
        lambda *_args, **_kwargs: {
            "dispatch_result": {},
            "dispatch": "not_dispatched",
        },
    )
    response = client.post(
        f"/project-intake/decisions/{decision_id}/resolve",
        json={"note": "Use SQLite", "resolved_by": "founder.ui"},
    )

    assert response.status_code == 200
    state = reporter.state(project["id"])
    assert state["phase"] == "building"
    assert state["current_criterion_id"] == "mvp-1"
    assert state["decision_id"] is None


def test_ui_approval_resolution_resumes_or_blocks_project(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        paths=PathConfig(
            hot_root=tmp_path / "brain",
            cold_root=tmp_path / "cold",
            data_dir=tmp_path / "data",
        ),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    app = create_app(config, run_boot_maintenance=False)
    client = TestClient(app)
    root = tmp_path / "Same Ground"
    root.mkdir()
    project_id = app.state.db.upsert_project(
        canonical_path=str(root),
        name="Same Ground",
        product_type="mobile_application",
        distribution_targets=["google_play", "apple_app_store_eventual"],
        lifecycle_state="verified",
        repo_fingerprint="fp",
        metadata={},
    )
    reporter = app.state.project_autonomy
    reporter.plan(project_id, criteria=[{"id": "mvp-1", "title": "Core flow"}])
    reporter.begin_increment(project_id, criterion_id="mvp-1")

    def make_approval(unique: str):
        work_item_id, _created = app.state.db.enqueue_work_item(
            kind="external",
            title="Publish beta",
            detail="Publishing needs founder approval.",
            action="release.publish",
            target="Same Ground",
            permission_tier="L3_EXTERNAL_ACTION",
            unique_key=unique,
        )
        request, _request_created = app.state.db.ensure_approval_request(
            source_type="work_item",
            source_id=work_item_id,
            title="Publish beta",
            detail="Publishing needs founder approval.",
            action="release.publish",
            target="Same Ground",
            permission_tier="L3_EXTERNAL_ACTION",
            authority_decision="approval_required",
            authority={"decision": "approval_required"},
            requested_by="project_autonomy",
        )
        return request.id

    approved_id = make_approval("test:approve-project")
    reporter.report_approval_required(
        project_id,
        approval_request_id=approved_id,
        action="Publish beta",
        reason="Publishing boundary",
        boundary="publishing_deployment",
    )
    requested_event = next(
        event
        for event in app.state.db.list_project_events(project_id)
        if event["event_type"] == "approval_requested"
    )
    assert requested_event["approval_request_id"] == approved_id
    approved = client.post(
        f"/approval-requests/{approved_id}/approve",
        json={"resolved_by": "founder.ui", "note": "Approved"},
    )
    assert approved.status_code == 200
    assert reporter.state(project_id)["phase"] == "building"

    denied_id = make_approval("test:deny-project")
    reporter.report_approval_required(
        project_id,
        approval_request_id=denied_id,
        action="Publish beta",
        reason="Publishing boundary",
        boundary="publishing_deployment",
    )
    denied = client.post(
        f"/approval-requests/{denied_id}/deny",
        json={"resolved_by": "founder.ui", "note": "Not yet"},
    )
    assert denied.status_code == 200
    assert reporter.state(project_id)["phase"] == "blocked"
    assert reporter.state(project_id)["blocking_reason"] == "Not yet"


def test_approvals_ui_contains_project_decision_resolution_panel() -> None:
    html = (Path(__file__).parents[1] / "ui" / "approvals.html").read_text(
        encoding="utf-8"
    )

    assert "Project Decisions" in html
    assert "Telegram only tells you that a decision is waiting" in html
    assert "/project-intake/decisions/${encodeURIComponent(id)}/resolve" in html
    assert "resolved_by: 'founder.ui'" in html
    assert "project_autonomy" in html


def test_generic_approval_routes_cannot_bypass_project_decision_panel(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    app = create_app(
        KernelConfig(
            paths=PathConfig(
                hot_root=tmp_path / "brain",
                cold_root=tmp_path / "cold",
                data_dir=tmp_path / "data",
            ),
            ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
        ),
        run_boot_maintenance=False,
    )
    client = TestClient(app)
    work_item_id, _created = app.state.db.enqueue_work_item(
        kind="founder_decision",
        title="Choose storage",
        detail="Choose the local database.",
        action="project.decision.resolve",
        target="Same Ground",
        permission_tier="L3_EXTERNAL_ACTION",
        metadata={"founder_decision": True, "project_autonomy": True},
        unique_key="test:no-generic-decision-approval",
    )
    request, _request_created = app.state.db.ensure_approval_request(
        source_type="work_item",
        source_id=work_item_id,
        title="Choose storage",
        detail="Choose the local database.",
        action="project.decision.resolve",
        target="Same Ground",
        permission_tier="L3_EXTERNAL_ACTION",
        authority_decision="approval_required",
        authority={"decision": "approval_required"},
        requested_by="test",
    )

    approved = client.post(f"/approval-requests/{request.id}/approve", json={})
    denied = client.post(f"/approval-requests/{request.id}/deny", json={})

    assert approved.status_code == 400
    assert denied.status_code == 400
    assert "Project Decisions panel" in approved.json()["detail"]
    assert app.state.db.get_approval_request(request.id).status == "pending"
