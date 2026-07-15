from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from cofounder_kernel.api import create_app
from cofounder_kernel.config import AppConfig, KernelConfig, OllamaConfig, PathConfig, RolesConfig
from cofounder_kernel.ollama import OllamaClient


def fake_health(self: OllamaClient) -> dict:
    return {"version": "test"}


def _config(tmp_path: Path, **kw) -> KernelConfig:
    return KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
        **kw,
    )


def _fake_generate(response: str):
    def generate(self: OllamaClient, *, prompt: str, model=None, think=None, temperature=None, num_predict=512):
        return SimpleNamespace(response=response, model=model or "test-model")

    return generate


def test_list_and_status(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))

    roles = client.get("/roles").json()["roles"]
    keys = {r["key"] for r in roles}
    assert {"red_team", "triage", "summarize", "gap_finder"} <= keys

    status = client.get("/roles/status").json()
    assert status["enabled"] is True


def test_run_red_team_parses_finding(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(
        OllamaClient,
        "generate",
        _fake_generate('{"verdict": "proceed_with_changes", "summary": "The pricing assumption is thin.", "points": ["No churn data", "Untested at $99"]}'),
    )
    app = create_app(_config(tmp_path))
    client = TestClient(app)

    result = client.post(
        "/roles/run",
        json={"role": "red_team", "content": "We should raise the price to $99.", "subject": "pricing"},
    ).json()

    assert result["status"] == "ok"
    assert result["role"] == "red_team"
    assert result["verdict"] == "proceed_with_changes"
    assert "No churn data" in result["points"]
    assert result["subject"] == "pricing"
    # The pass was recorded as model-call telemetry.
    calls = client.get("/models/telemetry/calls").json()["items"]
    assert any(c["operation"] == "roles.pass" for c in calls)


def test_run_unstructured_falls_back_to_raw(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "generate", _fake_generate("no json here, just prose"))
    client = TestClient(create_app(_config(tmp_path)))

    result = client.post("/roles/run", json={"role": "summarize", "content": "long text"}).json()
    assert result["status"] == "ok"
    assert result["verdict"] == "unstructured"
    assert "prose" in result["raw"]


def test_unknown_role_is_400(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))
    bad = client.post("/roles/run", json={"role": "wizard", "content": "x"})
    assert bad.status_code == 400 and "Unknown role" in bad.json()["detail"]


def test_disabled_blocks_run(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path, roles=RolesConfig(enabled=False))))
    blocked = client.post("/roles/run", json={"role": "triage", "content": "x"})
    assert blocked.status_code == 400 and "disabled" in blocked.json()["detail"]


def test_roles_layer_in_inventory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))
    inventory = client.get("/self-inventory").json()
    assert "POST /roles/run" in inventory["roles_layer"]["routes"]
    assert "red_team" in inventory["roles_layer"]["roles"]
