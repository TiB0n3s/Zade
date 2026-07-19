from __future__ import annotations

import json
from dataclasses import replace
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
    OpenAIReviewConfig,
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


def test_generic_approval_endpoint_routes_build_lease_to_build_service(
    build_client,
) -> None:
    client, _app, workspace = build_client
    prepared = _assess(client, workspace)
    session_id = prepared["session"]["id"]
    approval_id = prepared["approval_request_id"]

    approved = client.post(
        f"/approval-requests/{approval_id}/approve",
        headers=_headers(),
        json={
            "resolved_by": "founder",
            "dispatch": True,
            "typed_confirmation": CONFIRMATION,
        },
    )

    assert approved.status_code == 200, approved.text
    assert approved.json()["specialized_approval"] == "build_lease"
    assert approved.json()["build"]["run"]["route"] == "local"
    detail = client.get(f"/build/sessions/{session_id}").json()
    assert detail["lease"]["state"] == "active"
    request = client.get(f"/approval-requests/{approval_id}").json()["item"]
    assert request["status"] == "approved"


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


def test_cloud_configuration_failure_returns_503_when_cloud_task_is_reached(
    build_client, monkeypatch
) -> None:
    client, app, workspace = build_client
    prepared = _assess(
        client,
        workspace,
        task=(
            "Architect a cross-module SaaS UI, frontend API, backend worker, admin, "
            "analytics and notifications system with authentication, billing, migration, "
            "third-party integration, security, tests, and production release"
        ),
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

    assert response.status_code == 200
    assert response.json()["run"]["route"] == "local"
    requirements = client.post(
        f"/build/sessions/{session_id}/run-next", headers=_headers(), json={}
    )
    assert requirements.status_code == 200
    assert requirements.json()["phase"] == "requirements"
    response = client.post(
        f"/build/sessions/{session_id}/run-next", headers=_headers(), json={}
    )
    assert response.status_code == 503
    assert "unavailable" in response.json()["detail"]
    detail = client.get(f"/build/sessions/{session_id}").json()
    assert detail["lease"]["state"] == "active"
    assert detail["session"]["phase"] == "requirements"
    assert detail["usage"]["actual_microdollars"] == 0


def test_durable_build_api_exposes_local_lifecycle_and_status_surfaces(
    build_client,
) -> None:
    client, _app, workspace = build_client
    prepared = _assess(client, workspace)
    session_id = prepared["session"]["id"]

    assert len(prepared["tasks"]) == 9
    planned = client.post(
        f"/build/sessions/{session_id}/plan", headers=_headers(), json={}
    )
    assert planned.status_code == 200, planned.text
    assert len(planned.json()["tasks"]) == 9
    tasks = client.get(f"/build/sessions/{session_id}/tasks")
    assert tasks.status_code == 200
    assert tasks.json()["tasks"][0]["phase"] == "discovery"

    run = client.post(
        f"/build/sessions/{session_id}/run-next", headers=_headers(), json={}
    )
    assert run.status_code == 200, run.text
    assert run.json()["phase"] == "discovery"
    run_id = run.json()["run_id"]
    run_detail = client.get(f"/build/runs/{run_id}")
    assert run_detail.status_code == 200
    assert run_detail.json()["run"]["status"] == "succeeded"

    paused = client.post(
        f"/build/sessions/{session_id}/pause", headers=_headers(), json={}
    )
    assert paused.json()["status"] == "paused"
    resumed = client.post(
        f"/build/sessions/{session_id}/resume", headers=_headers(), json={}
    )
    assert resumed.status_code == 200

    toolchains = client.get(
        "/build/toolchains", params={"workspace": str(workspace)}
    )
    assert toolchains.status_code == 200
    assert toolchains.json()["detected"] == "generic"
    verification = client.post(
        f"/build/sessions/{session_id}/verify",
        headers=_headers(),
        json={"profile_id": "generic"},
    )
    assert verification.status_code == 200, verification.text
    assert verification.json()["blocked"] is True

    review_status = client.get(f"/build/sessions/{session_id}/review/status")
    assert review_status.status_code == 200
    assert review_status.json()["ready"] is False
    assert client.get("/build/calibration").status_code == 200
    readiness = client.get("/build/managed-agents/readiness")
    assert readiness.status_code == 200
    assert readiness.json()["execution_enabled"] is False


def test_durable_build_mutations_require_local_token(build_client) -> None:
    client, _app, workspace = build_client
    prepared = _assess(client, workspace)
    session_id = prepared["session"]["id"]

    for route in (
        "plan",
        "run-next",
        "start",
        "pause",
        "resume",
        "cancel",
        "verify",
    ):
        response = client.post(f"/build/sessions/{session_id}/{route}", json={})
        assert response.status_code == 401, route


def test_swarm_ui_exposes_durable_build_controls() -> None:
    html = Path("ui/swarm.html").read_text(encoding="utf-8")

    for control_id in (
        "buildTasks",
        "buildStartBtn",
        "buildPauseBtn",
        "buildResumeBtn",
        "buildCancelBtn",
        "buildVerifyBtn",
        "buildReviewPrepareBtn",
        "buildReviewRunBtn",
    ):
        assert f'id="{control_id}"' in html
    assert "/run-next" in html
    assert "/build/toolchains" in html


def test_openai_review_has_separate_lease_egress_and_calibration(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("LABEL = 'review me'\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-key")
    monkeypatch.setattr(OllamaClient, "chat", _local_assessment)
    monkeypatch.setattr(CodingAgentService, "run", _local_build)
    monkeypatch.setattr(
        "cofounder_kernel.openai_review._openai_sdk_available", lambda: True
    )
    calls: list[dict[str, Any]] = []

    class Usage:
        input_tokens = 100
        output_tokens = 25
        input_tokens_details = None

    class Responses:
        def create(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return type(
                "Response",
                (),
                {
                    "output_text": json.dumps(
                        {
                            "summary": "Review complete.",
                            "findings": ["Add one boundary test."],
                            "recommendation": "approve_with_fix",
                        }
                    ),
                    "usage": Usage(),
                    "_request_id": "req_test",
                },
            )()

    monkeypatch.setattr(
        "cofounder_kernel.openai_review._default_client_factory",
        lambda **_kwargs: type("Client", (), {"responses": Responses()})(),
    )
    config = replace(
        _config(tmp_path, workspace),
        openai_review=OpenAIReviewConfig(enabled=True),
    )
    client = TestClient(create_app(config, run_boot_maintenance=False))
    prepared = _assess(client, workspace)
    session_id = prepared["session"]["id"]

    review_prepare = client.post(
        f"/build/sessions/{session_id}/review/prepare",
        headers=_headers(),
        json={},
    )
    assert review_prepare.status_code == 200, review_prepare.text
    oversized = client.post(
        f"/build/sessions/{session_id}/review/approve",
        headers=_headers(),
        json={"typed_confirmation": CONFIRMATION, "tier": "large"},
    )
    assert oversized.status_code == 400, oversized.text
    approved = client.post(
        f"/build/sessions/{session_id}/review/approve",
        headers=_headers(),
        json={"typed_confirmation": CONFIRMATION, "tier": "small"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["lease"]["provider"] == "openai"

    reviewed = client.post(
        f"/build/sessions/{session_id}/review/run",
        headers=_headers(),
        json={
            "prompt": "Review release correctness",
            "context": "UNTRUSTED_CONTEXT_SENTINEL",
        },
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["findings"] == ["Add one boundary test."]
    assert calls[0]["store"] is False
    assert "tools" not in calls[0]
    assert "UNTRUSTED_CONTEXT_SENTINEL" not in calls[0]["input"]
    assert "LABEL = 'review me'" in calls[0]["input"]
    assert client.get(f"/build/sessions/{session_id}").json()["lease"] is None

    calibrated = client.post(
        f"/build/sessions/{session_id}/calibration",
        headers=_headers(),
        json={"provider": "openai", "outcome": "success"},
    )
    assert calibrated.status_code == 200, calibrated.text
    assert calibrated.json()["calibration"]["actual_input_tokens"] == 100
