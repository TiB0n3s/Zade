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


def test_authenticated_telegram_decision_reply_resumes_project(tmp_path: Path, monkeypatch) -> None:
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

    assert result["status"] == "project_decision_resolved"
    assert "Same Ground" in result["reply"]
    assert calls == [(77, "Use SQLite", "founder.telegram")]


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
