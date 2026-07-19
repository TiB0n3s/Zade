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
from cofounder_kernel.build_types import BuildTaskKind, BuildTaskStatus
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
    context = str(kwargs.get("context") or "")
    workspace = Path(str(kwargs.get("workspace") or ""))
    phase_artifacts = {
        "Build phase: requirements": ".zade/build/requirements.md",
        "Build phase: architecture": ".zade/build/architecture.md",
        "Build phase: planning": ".zade/build/plan.md",
    }
    artifact = next(
        (path for marker, path in phase_artifacts.items() if marker in context), ""
    )
    if artifact:
        target = workspace / artifact
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# Test build artifact\n", encoding="utf-8")
    return {
        "ok": True,
        "status": "ok",
        "error": "",
        "model": kwargs.get("model") or "local-coder",
        "provider": {"provider": "ollama", "fallback_attempted": False},
        "workspace": str(workspace),
        "rounds": 1,
        "used_tools": False,
        "steps": [],
        "changed_files": [artifact] if artifact else [],
        "workspace_changes": {
            "added": [artifact] if artifact else [],
            "modified": [],
            "deleted": [],
        },
        "auto_verification": {"ok": True, "command": ["pytest"]},
        "verifier_review": {"verdict": "pass", "notes": ""},
        "progress_notes": [],
        "response": "Completed locally.",
    }


@pytest.fixture
def build_client(tmp_path: Path, monkeypatch):
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    workspace = workspace_root / "project"
    workspace.mkdir()
    (workspace / "app.py").write_text("LABEL = 'old'\n", encoding="utf-8")
    monkeypatch.setattr(OllamaClient, "chat", _local_assessment)
    monkeypatch.setattr(CodingAgentService, "run", _local_build)
    app = create_app(_config(tmp_path, workspace_root), run_boot_maintenance=False)
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


def test_build_assess_rejects_the_non_repository_workspace_container(
    build_client,
) -> None:
    client, app, workspace = build_client
    container = workspace.parent

    response = client.post(
        "/build/assess",
        headers=_headers(),
        json={
            "task": "Build the product",
            "acceptance": "Tests pass",
            "workspace": str(container),
        },
    )

    assert response.status_code == 400
    assert "project directory" in response.json()["detail"]
    assert app.state.build_store.list_sessions() == []


def test_build_assess_rejects_a_workspace_outside_the_configured_root(
    build_client, tmp_path: Path,
) -> None:
    client, _app, _workspace = build_client
    outside = tmp_path / "outside"
    outside.mkdir()

    response = client.post(
        "/build/assess",
        headers=_headers(),
        json={"task": "Build the product", "workspace": str(outside)},
    )

    assert response.status_code == 400
    assert "outside the configured build workspace root" in response.json()["detail"]


def test_build_session_can_be_quarantined_without_deleting_audit_history(
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
    assert approved.status_code == 200

    response = client.post(
        f"/build/sessions/{session_id}/quarantine",
        headers=_headers(),
        json={"reason": "Incorrect project boundary selected."},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["session"]["status"] == "quarantined"
    assert body["session"]["checkpoint"]["quarantine"]["reason"] == "Incorrect project boundary selected."
    assert body["lease"]["state"] == "paused"
    assert client.post(
        f"/build/sessions/{session_id}/run-next", headers=_headers(), json={}
    ).json()["status"] == "quarantined"
    start = client.post(
        f"/build/sessions/{session_id}/start", headers=_headers(), json={}
    ).json()
    assert start == {
        "started": False,
        "status": "quarantined",
        "session_id": session_id,
    }


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
        "quarantine",
        "verify",
    ):
        response = client.post(f"/build/sessions/{session_id}/{route}", json={})
        assert response.status_code == 401, route


def test_failed_local_task_can_be_retried_with_audited_reason(build_client) -> None:
    client, app, workspace = build_client
    prepared = _assess(client, workspace)
    session_id = prepared["session"]["id"]
    task = app.state.build_store.create_task(
        session_id,
        phase="verification",
        kind=BuildTaskKind.VERIFICATION,
        title="Transient verification",
        idempotency_key="transient-verification",
    )
    run = app.state.build_store.claim_task(
        task.id, worker_id="worker", backend="local"
    )
    app.state.build_store.finish_task_run(
        run.id,
        status=BuildTaskStatus.INTERRUPTED,
        error="kernel restarted",
    )

    unauthorized = client.post(
        f"/build/sessions/{session_id}/tasks/{task.id}/retry",
        json={"reason": "Retry after transient kernel restart"},
    )
    assert unauthorized.status_code == 401

    response = client.post(
        f"/build/sessions/{session_id}/tasks/{task.id}/retry",
        headers=_headers(),
        json={"reason": "Retry after transient kernel restart"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["task"]["status"] in {"pending", "running"}


def test_swarm_ui_exposes_durable_build_controls() -> None:
    html = Path("ui/swarm.html").read_text(encoding="utf-8")

    for control_id in (
        "buildTasks",
        "buildStartBtn",
        "buildPauseBtn",
        "buildResumeBtn",
        "buildCancelBtn",
        "buildQuarantineBtn",
        "buildVerifyBtn",
        "buildReviewPrepareBtn",
        "buildReviewRunBtn",
    ):
        assert f'id="{control_id}"' in html
    assert "/tasks/${taskId}/retry" in html
    assert "/run-next" in html
    assert "/quarantine" in html
    assert "/build/toolchains" in html


def test_openai_review_has_separate_lease_egress_and_calibration(
    tmp_path: Path, monkeypatch
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    workspace = workspace_root / "project"
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
        _config(tmp_path, workspace_root),
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
