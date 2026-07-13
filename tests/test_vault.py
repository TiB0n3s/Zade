from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from cofounder_kernel.api import create_app
from cofounder_kernel.config import AppConfig, KernelConfig, OllamaConfig, PathConfig, VaultConfig
from cofounder_kernel.ollama import OllamaClient


PHRASE = "make the jump to hyperspace"


def fake_health(self: OllamaClient) -> dict:
    return {"version": "test"}


def _config(tmp_path: Path, *, sub: str = "") -> KernelConfig:
    base = tmp_path / sub if sub else tmp_path
    return KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=base / "hot", cold_root=base / "cold", data_dir=base / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )


def _approve_and_dispatch(client: TestClient, item_id: int) -> dict:
    response = client.post(
        f"/work/items/{item_id}/approve",
        json={"resolved_by": "founder", "dispatch": True, "typed_confirmation": PHRASE},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _write(path: Path, text: str = "content") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_vault_list_and_search_surface_flags_and_instructions(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = _config(tmp_path)
    hot = config.paths.hot_root
    _write(hot / "Project" / "notes.md", "hello world")
    _write(hot / "Project" / "01-raw" / "source.md", "raw source")
    _write(hot / "Project" / ".zade-instructions.md", "Do not reorganize the raw folder.")
    (hot / "Secret").mkdir(parents=True, exist_ok=True)
    _write(hot / "Secret" / ".zade-protected", "")
    client = TestClient(create_app(config))

    listing = client.get("/vault/list", params={"path": str(hot / "Project")})
    root_list = client.get("/vault/list")
    search = client.get("/vault/search", params={"query": "notes"})

    assert listing.status_code == 200, listing.text
    entries = {e["name"]: e for e in listing.json()["entries"]}
    assert entries["notes.md"]["type"] == "file"
    assert entries["01-raw"]["guarded"] is True
    assert "Do not reorganize" in (listing.json()["governing_instructions"] or "")

    root_entries = {e["name"]: e for e in root_list.json()["entries"]}
    assert root_entries["Project"]["top_level"] is True
    assert root_entries["Secret"]["protected_marker"] is True

    assert search.json()["count"] >= 1
    assert any(m["name"] == "notes.md" for m in search.json()["matches"])


def test_vault_delete_dry_run_then_trash_then_restore(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = _config(tmp_path)
    hot = config.paths.hot_root
    target = _write(hot / "Project" / "drop.md", "bye")
    client = TestClient(create_app(config))

    preview = client.post("/vault/delete", json={"path": str(target)})  # dry_run defaults True
    assert preview.status_code == 200
    assert preview.json()["dry_run"] is True
    assert preview.json()["file_count"] == 1
    assert preview.json()["guards_passed"] is True
    assert target.exists()  # preview changed nothing

    queued = client.post("/vault/delete", json={"path": str(target), "dry_run": False})
    assert queued.json()["status"] == "approval_required"
    approved = _approve_and_dispatch(client, queued.json()["item_id"])
    result = approved["dispatch_result"]

    assert result["handler"] == "local.vault.delete"
    assert result["restorable"] is True
    assert not target.exists()  # moved out of the vault

    trash = client.get("/vault/trash")
    assert trash.json()["count"] == 1
    trash_id = trash.json()["items"][0]["trash_id"]

    restored = client.post("/vault/restore", json={"trash_id": trash_id})
    assert restored.status_code == 200
    assert restored.json()["restored"] == str(target)
    assert target.exists() and target.read_text(encoding="utf-8") == "bye"


def test_vault_move_trashes_clobbered_destination(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = _config(tmp_path)
    hot = config.paths.hot_root
    src = _write(hot / "Project" / "a.md", "AAA")
    dst = _write(hot / "Project" / "b.md", "BBB")
    client = TestClient(create_app(config))

    # Clobber without overwrite is refused up front.
    blocked = client.post("/vault/move", json={"src": str(src), "dst": str(dst), "dry_run": False})
    assert blocked.status_code == 400
    assert "exists" in blocked.json()["detail"]

    preview = client.post("/vault/move", json={"src": str(src), "dst": str(dst), "overwrite": True})
    assert preview.json()["will_trash_destination"] is True

    queued = client.post("/vault/move", json={"src": str(src), "dst": str(dst), "overwrite": True, "dry_run": False})
    approved = _approve_and_dispatch(client, queued.json()["item_id"])
    result = approved["dispatch_result"]

    assert not src.exists()
    assert dst.read_text(encoding="utf-8") == "AAA"  # source moved over destination
    assert result["trashed_destination"]  # the old BBB was snapshotted, not lost

    # Moving to a fresh nested path needs no overwrite.
    nested = hot / "Project" / "sub" / "c.md"
    q2 = client.post("/vault/move", json={"src": str(dst), "dst": str(nested), "dry_run": False})
    _approve_and_dispatch(client, q2.json()["item_id"])
    assert nested.read_text(encoding="utf-8") == "AAA"
    assert not dst.exists()


def test_vault_guards_block_dangerous_operations(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = _config(tmp_path)
    hot = config.paths.hot_root
    _write(hot / "Trading Project" / "01-raw" / "x.md", "raw")
    _write(hot / "TopFolder" / "y.md", "y")
    _write(hot / "Protected" / ".zade-protected", "")
    _write(hot / "Protected" / "z.md", "z")
    client = TestClient(create_app(config))

    raw = client.post("/vault/delete", json={"path": str(hot / "Trading Project" / "01-raw" / "x.md"), "dry_run": False})
    top = client.post("/vault/delete", json={"path": str(hot / "TopFolder"), "dry_run": False})
    top_confirmed = client.post(
        "/vault/delete", json={"path": str(hot / "TopFolder"), "dry_run": False, "allow_top_level": True}
    )
    protected = client.post("/vault/delete", json={"path": str(hot / "Protected" / "z.md"), "dry_run": False})
    outside = client.post("/vault/delete", json={"path": "C:/Windows/System32/drivers/etc/hosts", "dry_run": False})
    root = client.post("/vault/delete", json={"path": str(hot), "dry_run": False, "allow_top_level": True})

    assert raw.status_code == 400 and "protected raw/source folder" in raw.json()["detail"]
    assert top.status_code == 400 and "top-level" in top.json()["detail"]
    assert top_confirmed.status_code == 200 and top_confirmed.json()["status"] == "approval_required"
    assert protected.status_code == 400 and "protects this location" in protected.json()["detail"]
    assert outside.status_code == 400 and "outside configured local roots" in outside.json()["detail"]
    assert root.status_code == 400 and "root directory" in root.json()["detail"]


def test_vault_status_inventory_and_disabled_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path, sub="a")))

    status = client.get("/vault/status")
    inventory = client.get("/self-inventory")

    assert status.json()["enabled"] is True
    assert "01-raw" in status.json()["guard_segments"]
    layer = inventory.json()["vault_layer"]
    assert "POST /vault/delete" in layer["routes"]
    assert "local.vault.delete" in inventory.json()["work_queue"]["approved_local_dispatch_handlers"]

    disabled_config = _config(tmp_path, sub="b")
    disabled_config = KernelConfig(
        app=disabled_config.app,
        paths=disabled_config.paths,
        ollama=disabled_config.ollama,
        vault=VaultConfig(enabled=False),
    )
    client2 = TestClient(create_app(disabled_config))
    handlers = client2.get("/action-handlers")
    disabled = client2.post(
        "/vault/delete", json={"path": str(disabled_config.paths.hot_root / "x.md"), "dry_run": False}
    )

    assert "local.vault.delete" not in {item["action"] for item in handlers.json()["items"]}
    assert disabled.status_code == 400 and "disabled" in disabled.json()["detail"]
