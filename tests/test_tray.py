from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from cofounder_kernel.api import create_app
from cofounder_kernel.config import AppConfig, KernelConfig, OllamaConfig, PathConfig
from cofounder_kernel.ollama import OllamaClient
from cofounder_kernel.tray import STATUS_COLORS, compute_view


def fake_health_ok(self: OllamaClient) -> dict:
    return {"version": "test"}


def fake_health_down(self: OllamaClient) -> dict:
    raise RuntimeError("ollama unreachable")


def _config(tmp_path: Path) -> KernelConfig:
    return KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )


def test_tray_state_ok_when_idle_and_healthy(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health_ok)
    client = TestClient(create_app(_config(tmp_path)))

    state = client.get("/tray/state").json()

    assert state["status"] == "ok"
    assert state["ollama_ok"] is True
    assert state["pending_approvals"] == 0
    assert state["unread_notifications"] == 0
    assert state["identity"] == "Zade"
    assert state["ui_url"].endswith("/ui/")
    assert "all clear" in state["tooltip"]


def test_tray_state_flags_attention_for_approvals_and_notifications(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health_ok)
    app = create_app(_config(tmp_path))
    client = TestClient(app)

    app.state.bus.notify(topic="pilot", title="Pilot signed", body="CustomerCo signed the pilot.", severity="info")
    client.post(
        "/work/items",
        json={
            "kind": "external",
            "title": "Sync inbox",
            "action": "external.connector.sync",
            "target": "inbox",
            "permission_tier": "L3_EXTERNAL_ACTION",
            "source": "zade.proposal",
        },
    )

    state = client.get("/tray/state").json()

    assert state["status"] == "attention"
    assert state["pending_approvals"] >= 1
    assert state["unread_notifications"] >= 1
    assert any(note["title"] == "Pilot signed" for note in state["notifications"])
    assert "approval" in state["tooltip"] or "unread" in state["tooltip"]


def test_tray_state_reports_error_when_model_down(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health_down)
    client = TestClient(create_app(_config(tmp_path)))

    state = client.get("/tray/state").json()

    assert state["status"] == "error"
    assert state["ollama_ok"] is False
    assert "offline" in state["tooltip"]


def test_tray_layer_in_inventory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health_ok)
    client = TestClient(create_app(_config(tmp_path)))

    inventory = client.get("/self-inventory").json()

    assert "GET /tray/state" in inventory["tray_layer"]["routes"]
    assert inventory["tray_layer"]["console_script"] == "zade-tray"


def test_compute_view_toasts_each_notification_once() -> None:
    state = {
        "status": "attention",
        "tooltip": "Zade — 1 unread",
        "pending_approvals": 0,
        "unread_notifications": 1,
        "notifications": [{"id": 7, "title": "Pilot signed", "body": "CustomerCo signed."}],
    }

    view, seen = compute_view(state, set())
    assert view.status == "attention"
    assert len(view.toasts) == 1
    assert view.toasts[0]["title"] == "Pilot signed"
    assert 7 in seen

    # A second poll with the same notification already seen raises no new toast.
    view2, seen2 = compute_view(state, seen)
    assert view2.toasts == []
    assert seen2 == seen


def test_compute_view_maps_status_to_color_and_menu() -> None:
    view, _seen = compute_view(
        {"status": "error", "pending_approvals": 2, "unread_notifications": 3, "notifications": []}, set()
    )

    assert view.color == STATUS_COLORS["error"]
    assert "Approvals: 2" in view.menu
    assert "Unread: 3" in view.menu
    assert "Open Zade" in view.menu
