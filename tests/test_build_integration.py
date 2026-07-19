from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cofounder_kernel.api import create_app
from cofounder_kernel.anthropic_client import AnthropicNotConfigured
from cofounder_kernel.coding_agent import CodingAgentService
from cofounder_kernel.config import (
    AnthropicConfig,
    AppConfig,
    DelegationConfig,
    KernelConfig,
    OllamaConfig,
    PathConfig,
    SecurityConfig,
)
from cofounder_kernel.ollama import GenerateResult, OllamaClient


CONFIRMATION = "make the jump to hyperspace"
TOKEN = "build-test-token"


def _config(tmp_path: Path, workspace: Path) -> KernelConfig:
    return KernelConfig(
        app=AppConfig(),
        paths=PathConfig(
            hot_root=tmp_path / "hot",
            cold_root=tmp_path / "cold",
            data_dir=tmp_path / "data",
        ),
        ollama=OllamaConfig(
            provider_policy="local_preferred",
            coding_agent_model="local-coder",
        ),
        security=SecurityConfig(local_token=TOKEN),
        anthropic=AnthropicConfig(enabled=True, model="claude-opus-4-8"),
        delegation=DelegationConfig(
            engine="hybrid", workspace_root=str(workspace), auto_invoke=False
        ),
    )


def _local_assessment(
    self: OllamaClient,
    *,
    messages: Any,
    model: str | None = None,
    think: bool | None = None,
    temperature: float | None = None,
    num_predict: int = 512,
    format: str | dict[str, Any] | None = None,
    tools: Any = None,
) -> GenerateResult:
    del self, messages, think, temperature, num_predict, format, tools
    return GenerateResult(
        response=json.dumps(
            {
                "score_adjustment": 0,
                "confidence": 0.9,
                "reasons": [],
                "unknowns": [],
            }
        ),
        model=model or "local-coder",
        raw={},
    )


def _local_build(self: CodingAgentService, **kwargs: Any) -> dict[str, Any]:
    del self
    return {
        "ok": True,
        "status": "ok",
        "error": "",
        "model": kwargs.get("model") or "local-coder",
        "provider": {"provider": "ollama", "fallback_attempted": False},
        "workspace": str(kwargs.get("workspace") or ""),
        "rounds": 1,
        "used_tools": False,
        "steps": [],
        "changed_files": [],
        "workspace_changes": {"added": [], "modified": [], "deleted": []},
        "auto_verification": {"ok": True, "command": ["pytest"]},
        "verifier_review": None,
        "progress_notes": [],
        "response": "Completed locally.",
    }


@pytest.fixture
def build_client(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("LABEL = 'old'\n", encoding="utf-8")
    monkeypatch.setattr(OllamaClient, "chat", _local_assessment)
    monkeypatch.setattr(CodingAgentService, "run", _local_build)
    app = create_app(_config(tmp_path, workspace), run_boot_maintenance=False)
    return TestClient(app), app, workspace


def _headers() -> dict[str, str]:
    return {"X-Zade-Token": TOKEN}


def _assess(client: TestClient, workspace: Path, *, task: str = "Rename one label") -> dict[str, Any]:
    response = client.post(
        "/build/assess",
        headers=_headers(),
        json={
            "task": task,
            "acceptance": "Local tests pass",
            "workspace": str(workspace),
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_build_assess_is_protected_and_never_constructs_cloud_client(
    build_client, monkeypatch
) -> None:
    client, _app, workspace = build_client

    unauthorized = client.post(
        "/build/assess",
        json={"task": "Build auth API", "workspace": str(workspace)},
    )
    assert unauthorized.status_code == 401

    def cloud_forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("assessment must not construct an Anthropic SDK client")

    monkeypatch.setattr(
        "cofounder_kernel.anthropic_client.create_sdk_client", cloud_forbidden
    )
    body = _assess(client, workspace, task="Build authentication and billing API")

    assert body["assessment"]["recommended_tier"] in {"medium", "large"}
    assert body["session"]["phase"] == "approval"
    assert body["approval_request_id"]
    assert body["recommended_limits"]["dollar_micro"] >= 1_000_000
    assert body["usage"]["actual_microdollars"] == 0


def test_build_session_routes_approve_local_work_and_report_remaining_budget(
    build_client,
) -> None:
    client, _app, workspace = build_client
    prepared = _assess(client, workspace)
    session_id = prepared["session"]["id"]

    approved = client.post(
        f"/build/sessions/{session_id}/approve",
        headers=_headers(),
        json={"typed_confirmation": CONFIRMATION},
    )

    assert approved.status_code == 200, approved.text
    assert approved.json()["run"]["route"] == "local"
    detail = client.get(f"/build/sessions/{session_id}").json()
    assert detail["lease"]["state"] == "active"
    assert detail["usage"]["authorized_microdollars"] == 1_000_000
    assert detail["usage"]["actual_microdollars"] == 0
    assert detail["usage"]["remaining_microdollars"] == 1_000_000
    assert detail["usage"]["remaining_cloud_turns"] == 6
    assert detail["route_counts"] == {"local": 1, "cloud": 0, "founder": 0}

    listed = client.get("/build/sessions").json()
    assert listed["sessions"][0]["session"]["id"] == session_id


def test_build_session_can_be_denied_and_missing_sessions_are_404(build_client) -> None:
    client, _app, workspace = build_client
    prepared = _assess(client, workspace)
    session_id = prepared["session"]["id"]

    denied = client.post(
        f"/build/sessions/{session_id}/deny",
        headers=_headers(),
        json={"note": "Keep this build local only."},
    )

    assert denied.status_code == 200, denied.text
    assert denied.json()["session"]["phase"] == "complete"
    assert denied.json()["session"]["checkpoint"]["denied"] is True
    assert client.get("/build/sessions/999999").status_code == 404
    assert (
        client.post(
            "/build/sessions/999999/run", headers=_headers(), json={}
        ).status_code
        == 404
    )


def test_delegation_status_includes_recent_build_summary(build_client) -> None:
    client, _app, workspace = build_client
    prepared = _assess(client, workspace)

    status = client.get("/delegation/status").json()

    assert status["engine"] == "hybrid"
    assert status["build"]["enabled"] is True
    assert status["build"]["recent_sessions"][0]["session"]["id"] == prepared["session"]["id"]
    assert status["build"]["active_session_count"] == 1


def test_cloud_configuration_failure_returns_503_and_preserves_lease(
    build_client, monkeypatch
) -> None:
    client, app, workspace = build_client
    prepared = _assess(
        client, workspace, task="Review authentication security before release"
    )
    session_id = prepared["session"]["id"]

    def missing_sdk() -> Any:
        raise AnthropicNotConfigured("Anthropic test transport is unavailable")

    monkeypatch.setattr(app.state.anthropic_build_transport, "sdk_client", missing_sdk)
    response = client.post(
        f"/build/sessions/{session_id}/approve",
        headers=_headers(),
        json={"typed_confirmation": CONFIRMATION},
    )

    assert response.status_code == 503
    assert "unavailable" in response.json()["detail"]
    detail = client.get(f"/build/sessions/{session_id}").json()
    assert detail["lease"]["state"] == "active"
    assert detail["session"]["phase"] == "planning"
    assert detail["usage"]["actual_microdollars"] == 0
