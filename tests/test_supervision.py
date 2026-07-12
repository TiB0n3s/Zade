import json
from pathlib import Path

from fastapi.testclient import TestClient

from cofounder_kernel.api import create_app
from cofounder_kernel.config import AppConfig, KernelConfig, OllamaConfig, PathConfig
from cofounder_kernel.ollama import OllamaClient


def fake_health(self: OllamaClient) -> dict:
    return {"version": "test"}


def _config(tmp_path: Path) -> KernelConfig:
    return KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )


def test_supervision_reports_uptime_with_no_log(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))

    supervision = client.get("/ops/supervision")
    health = client.get("/health")
    inventory = client.get("/self-inventory")

    assert supervision.status_code == 200
    payload = supervision.json()
    assert payload["kernel"]["uptime_seconds"] >= 0
    assert payload["kernel"]["started_at"]
    assert payload["log_exists"] is False
    assert payload["events"] == []
    assert payload["last_event"] is None
    assert "supervision" in payload["log_path"]
    assert "Zade Local Supervisor" in payload["expected_tasks"]
    assert health.json()["uptime_seconds"] >= 0
    assert "GET /ops/supervision" in inventory.json()["ops_layer"]["routes"]
    assert "supervision_log" in inventory.json()["ops_layer"]["artifacts"]


def test_supervision_parses_supervisor_log_history(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = _config(tmp_path)
    client = TestClient(create_app(config))
    log_path = config.paths.data_dir / "supervision" / "supervisor-log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"timestamp": "2026-07-11T08:00:00+00:00", "event": "healthy", "ok": True}),
        json.dumps({"timestamp": "2026-07-11T09:00:00+00:00", "event": "unreachable", "ok": False}),
        "this line is not json",
        json.dumps({"timestamp": "2026-07-11T09:01:00+00:00", "event": "started", "ok": True, "detail": "Kernel recovered by supervisor."}),
        json.dumps({"timestamp": "2026-07-12T08:00:00+00:00", "event": "healthy", "ok": True}),
    ]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    supervision = client.get("/ops/supervision", params={"limit": 3})

    assert supervision.status_code == 200
    payload = supervision.json()
    assert payload["log_exists"] is True
    assert payload["malformed_lines"] == 1
    assert payload["counts"] == {"healthy": 2, "unreachable": 1, "started": 1}
    # Newest first, limited.
    assert len(payload["events"]) == 3
    assert payload["events"][0]["event"] == "healthy"
    assert payload["events"][0]["timestamp"] == "2026-07-12T08:00:00+00:00"
    assert payload["events"][1]["event"] == "started"
    assert payload["last_event"]["event"] == "healthy"
    assert payload["last_event"]["ok"] is True
