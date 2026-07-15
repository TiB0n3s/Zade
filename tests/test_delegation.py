from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import cofounder_kernel.delegation as delegation_module
from cofounder_kernel.api import create_app
from cofounder_kernel.config import AppConfig, DelegationConfig, KernelConfig, OllamaConfig, PathConfig
from cofounder_kernel.ollama import OllamaClient

PHRASE = "make the jump to hyperspace"


def fake_health(self: OllamaClient) -> dict:
    return {"version": "test"}


def fake_embed(self: OllamaClient, *, text: str, model=None) -> list[float]:
    return [1.0, 0.0]


def _config(tmp_path: Path, **delegation_kw) -> KernelConfig:
    return KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
        delegation=DelegationConfig(**delegation_kw) if delegation_kw else DelegationConfig(),
    )


def test_build_brief_is_scoped(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))
    brief = client.post(
        "/delegation/brief",
        json={"task": "Refactor the auth module", "acceptance": "All tests pass"},
    ).json()["brief"]
    assert "## Goal" in brief
    assert "Refactor the auth module" in brief
    assert "All tests pass" in brief


def test_brief_only_when_no_agent_configured(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    # Default config: agent_command empty → can never auto-invoke.
    client = TestClient(create_app(_config(tmp_path)))
    result = client.post("/delegation/run", json={"task": "do a thing", "auto_invoke": True}).json()
    assert result["status"] == "approval_required"
    assert result["auto_invoked"] is False
    assert "no agent command" in result["reason"]


def test_auto_invoke_within_budget_dispatches_and_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)

    captured = {}

    def fake_run_agent(command, *, brief, timeout=600.0, max_output_chars=20000):
        captured["command"] = command
        captured["brief"] = brief
        return "PATCH: refactored the module, all green."

    monkeypatch.setattr(delegation_module, "run_agent", fake_run_agent)

    config = _config(tmp_path, enabled=True, auto_invoke=True, agent_command=("agent-cli",), daily_budget=25)
    app = create_app(config)
    client = TestClient(app)

    result = client.post("/delegation/run", json={"task": "Refactor auth", "auto_invoke": True}).json()

    assert result["auto_invoked"] is True
    dispatch = result["dispatch"]
    assert dispatch["ok"] is True
    assert "refactored the module" in dispatch["artifact"]
    assert captured["command"] == ["agent-cli"]
    assert "Refactor auth" in captured["brief"]

    # The artifact was filed as delegated-work evidence.
    evidence = client.get("/founder/evidence").json()["items"]
    assert any(item["evidence_type"] == "delegated_work" for item in evidence)


def test_over_budget_falls_back_to_gated(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = _config(tmp_path, enabled=True, auto_invoke=True, agent_command=("agent-cli",), daily_budget=0)
    client = TestClient(create_app(config))
    result = client.post("/delegation/run", json={"task": "x", "auto_invoke": True}).json()
    assert result["auto_invoked"] is False
    assert "budget" in result["reason"]


def test_gated_dispatch_runs_agent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)
    monkeypatch.setattr(
        delegation_module, "run_agent",
        lambda command, *, brief, timeout=600.0, max_output_chars=20000: "done via approval",
    )
    config = _config(tmp_path, enabled=True, auto_invoke=False, agent_command=("agent-cli",))
    client = TestClient(create_app(config))

    queued = client.post("/delegation/run", json={"task": "gated task", "auto_invoke": False}).json()
    assert queued["auto_invoked"] is False
    approved = client.post(
        f"/work/items/{queued['item_id']}/approve",
        json={"resolved_by": "founder", "dispatch": True, "typed_confirmation": PHRASE},
    ).json()
    assert approved["dispatch_result"]["ok"] is True
    assert "done via approval" in approved["dispatch_result"]["artifact"]


def test_disabled_blocks_and_unregisters(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path, enabled=False)))
    handlers = {h["action"] for h in client.get("/action-handlers").json()["items"]}
    assert "external.delegation.run" not in handlers
    blocked = client.post("/delegation/run", json={"task": "x"})
    assert blocked.status_code == 400 and "disabled" in blocked.json()["detail"]


def test_delegation_layer_in_inventory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))
    inventory = client.get("/self-inventory").json()
    assert "POST /delegation/run" in inventory["delegation_layer"]["routes"]
    assert inventory["delegation_layer"]["dispatch_action"] == "external.delegation.run"
