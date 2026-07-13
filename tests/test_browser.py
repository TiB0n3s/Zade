from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import cofounder_kernel.browser as browser_module
from cofounder_kernel.api import create_app
from cofounder_kernel.browser import BrowserService
from cofounder_kernel.config import AppConfig, BrowserConfig, KernelConfig, OllamaConfig, PathConfig
from cofounder_kernel.ollama import OllamaClient


PHRASE = "make the jump to hyperspace"


def fake_health(self: OllamaClient) -> dict:
    return {"version": "test"}


def _config(tmp_path: Path, *, allow_private: bool = True) -> KernelConfig:
    return KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
        # allow_private_navigation True keeps navigation validation off the DNS
        # path so the happy-path tests are hermetic; the rejection tests use an IP
        # literal (127.0.0.1) which resolves locally without network.
        browser=BrowserConfig(allow_private_navigation=allow_private),
    )


def _approve_and_dispatch(client: TestClient, item_id: int) -> dict:
    response = client.post(
        f"/work/items/{item_id}/approve",
        json={"resolved_by": "founder", "dispatch": True, "typed_confirmation": PHRASE},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_browser_run_queues_approval_then_dispatches_with_injected_runner(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path, allow_private=True)))

    captured: dict = {}

    def fake_run(steps, *, options):
        captured["steps"] = steps
        captured["options"] = options
        return {
            "ok": True,
            "failed_step": None,
            "error": "",
            "steps": [
                {"type": "navigate", "status": "ok", "url": "https://example.com/", "title": "Example"},
                {"type": "fill", "status": "ok", "selector": "#q", "value": "***redacted***", "source": "literal"},
                {"type": "read", "status": "ok", "selector": "", "text": "Example Domain"},
            ],
            "pages": [{"url": "https://example.com/", "title": "Example"}],
        }

    # Patched at the module level; run_from_work_item resolves it by name at call
    # time, so the already-constructed service picks up the fake.
    monkeypatch.setattr(browser_module, "run_browser_flow", fake_run)

    steps = [
        {"type": "navigate", "url": "https://example.com"},
        {"type": "fill", "selector": "#q", "value": "topsecret-typed-text"},
        {"type": "read"},
    ]
    queued = client.post("/browser/run", json={"steps": steps, "title": "Search flow"})
    handlers = client.get("/action-handlers")

    assert queued.status_code == 200, queued.text
    assert queued.json()["status"] == "approval_required"
    assert queued.json()["authority"]["decision"] == "approval_required"
    assert queued.json()["interactive"] is True
    assert queued.json()["step_count"] == 3
    assert "external.browser.run" in {item["action"] for item in handlers.json()["items"]}

    approved = _approve_and_dispatch(client, queued.json()["item_id"])
    result = approved["dispatch_result"]

    assert approved["dispatch"] == "dispatched"
    assert result["handler"] == "external.browser.run"
    assert result["ok"] is True
    assert result["interactive"] is True
    assert result["step_count"] == 3
    # The runner receives the real typed value for replay...
    assert captured["steps"][1]["value"] == "topsecret-typed-text"
    assert captured["options"]["headless"] is False
    assert captured["options"]["nav_timeout_ms"] == 30000
    # ...but the dispatched result never echoes it.
    assert "topsecret-typed-text" not in str(result["steps"])
    assert result["steps"][1]["value"] == "***redacted***"

    # And the audit trail records the action but never the typed text.
    audit = client.get("/audit/recent")
    assert "external.browser.run" in audit.text
    assert "topsecret-typed-text" not in audit.text


def test_browser_run_reports_flow_failure_without_raising(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path, allow_private=True)))

    def failing_run(steps, *, options):
        return {
            "ok": False,
            "failed_step": 1,
            "error": "Step 1 (click) failed: selector not found",
            "steps": [
                {"type": "navigate", "status": "ok", "url": "https://example.com/", "title": "Example"},
                {"type": "click", "status": "error", "error": "selector not found"},
            ],
            "pages": [{"url": "https://example.com/", "title": "Example"}],
        }

    monkeypatch.setattr(browser_module, "run_browser_flow", failing_run)

    queued = client.post(
        "/browser/run",
        json={"steps": [{"type": "navigate", "url": "https://example.com"}, {"type": "click", "selector": "#nope"}]},
    )
    approved = _approve_and_dispatch(client, queued.json()["item_id"])
    result = approved["dispatch_result"]

    # A failed flow still dispatches cleanly (like a nonzero-exit dev command):
    # ok is False and the failure is reported, not raised.
    assert approved["dispatch"] == "dispatched"
    assert result["ok"] is False
    assert result["failed_step"] == 1
    assert "selector not found" in result["error"]


def test_browser_run_rejects_unsafe_flows(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    # Default config: private navigation is refused.
    client = TestClient(create_app(_config(tmp_path, allow_private=False)))

    not_navigate_first = client.post(
        "/browser/run", json={"steps": [{"type": "read"}]}
    )
    unknown_type = client.post(
        "/browser/run",
        json={"steps": [{"type": "navigate", "url": "https://example.com"}, {"type": "teleport"}]},
    )
    private_host = client.post(
        "/browser/run", json={"steps": [{"type": "navigate", "url": "http://127.0.0.1/admin"}]}
    )
    bad_scheme = client.post(
        "/browser/run", json={"steps": [{"type": "navigate", "url": "file:///C:/secrets.txt"}]}
    )
    # A public IP literal resolves locally (no DNS) and is not private, so the
    # navigate step passes and validation reaches the step under test.
    public_ip = "http://93.184.216.34/"
    screenshot_escape = client.post(
        "/browser/run",
        json={
            "steps": [
                {"type": "navigate", "url": public_ip},
                {"type": "screenshot", "path": "C:/Windows/Temp/escape.png"},
            ]
        },
    )
    fill_without_value = client.post(
        "/browser/run",
        json={"steps": [{"type": "navigate", "url": public_ip}, {"type": "fill", "selector": "#q"}]},
    )

    assert not_navigate_first.status_code == 400
    assert "must start with a 'navigate' step" in not_navigate_first.json()["detail"]
    assert unknown_type.status_code == 400
    assert "unknown type" in unknown_type.json()["detail"]
    assert private_host.status_code == 400
    assert "private/internal" in private_host.json()["detail"]
    assert bad_scheme.status_code == 400
    assert "http/https" in bad_scheme.json()["detail"]
    assert screenshot_escape.status_code == 400
    assert "outside configured local roots" in screenshot_escape.json()["detail"]
    assert fill_without_value.status_code == 400
    assert "needs a value or value_env" in fill_without_value.json()["detail"]


def test_browser_status_and_inventory_expose_the_layer(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))

    status = client.get("/browser/status")
    inventory = client.get("/self-inventory")

    assert status.status_code == 200
    body = status.json()
    assert body["enabled"] is True
    assert body["headless"] is False
    assert "navigate" in body["step_types"] and "click" in body["step_types"]
    assert isinstance(body["playwright_available"], bool)

    layer = inventory.json()["browser_layer"]
    assert "POST /browser/run" in layer["routes"]
    assert layer["dispatch_action"] == "external.browser.run"
    assert "external.browser.run" in inventory.json()["work_queue"]["approved_local_dispatch_handlers"]


def test_disabled_browser_does_not_register_or_queue(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
        browser=BrowserConfig(enabled=False),
    )
    client = TestClient(create_app(config))

    handlers = client.get("/action-handlers")
    queued = client.post("/browser/run", json={"steps": [{"type": "navigate", "url": "https://example.com"}]})

    assert "external.browser.run" not in {item["action"] for item in handlers.json()["items"]}
    assert queued.status_code == 400
    assert "disabled" in queued.json()["detail"]


# ---- unit-level checks that need no server ----
def _service(tmp_path: Path, *, allow_private: bool = True) -> BrowserService:
    return BrowserService(config=_config(tmp_path, allow_private=allow_private), db=None, work_queue=None)


def test_validate_steps_classifies_and_describes_without_leaking_values(tmp_path: Path) -> None:
    svc = _service(tmp_path, allow_private=True)

    read_only = svc._validate_steps(
        [{"type": "navigate", "url": "https://x.com"}, {"type": "read"}, {"type": "screenshot"}]
    )
    interactive = svc._validate_steps(
        [{"type": "navigate", "url": "https://x.com"}, {"type": "fill", "selector": "#p", "value": "hunter2"}]
    )

    assert svc._is_interactive(read_only) is False
    assert svc._is_interactive(interactive) is True

    description = svc._describe(interactive, interactive=True)
    assert "hunter2" not in description
    assert "***redacted***" in description


def test_validate_steps_supports_secret_env_and_blocks_private_ip(tmp_path: Path) -> None:
    # allow_private keeps the https navigate off the DNS path so this is hermetic.
    svc_open = _service(tmp_path, allow_private=True)
    env_steps = svc_open._validate_steps(
        [
            {"type": "navigate", "url": "https://example.com"},
            {"type": "fill", "selector": "#pw", "value_env": "ZADE_TEST_SECRET"},
        ]
    )
    # A secret typed via env var is kept as a reference, not an inline value.
    assert env_steps[1]["value_env"] == "ZADE_TEST_SECRET"
    assert "value" not in env_steps[1]

    # Private IP navigation is refused when allow_private_navigation is off.
    svc_closed = _service(tmp_path, allow_private=False)
    try:
        svc_closed._validate_steps([{"type": "navigate", "url": "http://127.0.0.1/admin"}])
        assert False, "expected private host to be refused"
    except ValueError as exc:
        assert "private/internal" in str(exc)
