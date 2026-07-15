from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import cofounder_kernel.screen as screen_module
from cofounder_kernel.api import create_app
from cofounder_kernel.config import AppConfig, KernelConfig, OllamaConfig, PathConfig, ScreenConfig
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


def test_textual_capture(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(
        screen_module, "list_windows", lambda: ("Zade — Home", ["Zade — Home", "Terminal", "Firefox"])
    )
    client = TestClient(create_app(_config(tmp_path)))

    result = client.post("/screen/capture", json={"snapshot": False}).json()
    assert result["focused_window"] == "Zade — Home"
    assert result["window_count"] == 3
    assert "Terminal" in result["windows"]
    assert result["snapshot"] is None


def test_snapshot_when_available(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(screen_module, "list_windows", lambda: ("W", ["W"]))
    monkeypatch.setattr(screen_module, "_mss_available", lambda: True)

    def fake_grab(path):
        Path(path).write_bytes(b"PNGDATA")
        return 1920, 1080

    monkeypatch.setattr(screen_module, "grab_screen", fake_grab)
    client = TestClient(create_app(_config(tmp_path)))

    result = client.post("/screen/capture", json={"snapshot": True}).json()
    snap = result["snapshot"]
    assert snap["status"] == "ok"
    assert snap["width"] == 1920 and snap["height"] == 1080
    assert Path(snap["path"]).exists()


def test_snapshot_unavailable_degrades(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(screen_module, "list_windows", lambda: ("W", ["W"]))
    monkeypatch.setattr(screen_module, "_mss_available", lambda: False)
    client = TestClient(create_app(_config(tmp_path)))

    result = client.post("/screen/capture", json={"snapshot": True}).json()
    assert result["snapshot"]["status"] == "unavailable"


def test_snapshot_prunes_to_keep_last(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(screen_module, "list_windows", lambda: ("W", ["W"]))
    monkeypatch.setattr(screen_module, "_mss_available", lambda: True)
    counter = {"n": 0}

    def fake_grab(path):
        counter["n"] += 1
        Path(path).write_bytes(b"x")
        return 10, 10

    monkeypatch.setattr(screen_module, "grab_screen", fake_grab)
    client = TestClient(create_app(_config(tmp_path, screen=ScreenConfig(keep_last=2))))

    for _ in range(4):
        client.post("/screen/capture", json={"snapshot": True})
    captures = list((tmp_path / "data" / "screen-captures").glob("capture-*.png"))
    assert len(captures) <= 2


def test_disabled_blocks_capture(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path, screen=ScreenConfig(enabled=False))))
    blocked = client.post("/screen/capture", json={"snapshot": False})
    assert blocked.status_code == 400 and "disabled" in blocked.json()["detail"]


def test_screen_layer_in_inventory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))
    inventory = client.get("/self-inventory").json()
    assert "POST /screen/capture" in inventory["screen_layer"]["routes"]
