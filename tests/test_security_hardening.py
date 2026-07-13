"""Stage 1 (P0) security-hardening tests: the deny branches the audit found untested."""
from pathlib import Path

from fastapi.testclient import TestClient

from cofounder_kernel.api import create_app
from cofounder_kernel.config import AppConfig, KernelConfig, OllamaConfig, PathConfig
from cofounder_kernel.connectors import _fetch_ics_over_http, _host_is_private
from cofounder_kernel.ollama import OllamaClient


PHRASE = "make the jump to hyperspace"


def fake_health(self: OllamaClient) -> dict:
    return {"version": "test"}


def _config(tmp_path: Path) -> KernelConfig:
    return KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "hot" / "state"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )


def test_file_write_cannot_touch_kernel_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = _config(tmp_path)
    client = TestClient(create_app(config))
    db_path = str(config.paths.database_path)  # sits inside hot_root/state (nested under hot_root)

    queued = client.post("/work/items", json={
        "kind": "external", "title": "overwrite the DB", "action": "local.file.write",
        "permission_tier": "L3_EXTERNAL_ACTION",
        "source": "zade.proposal",
        "metadata": {"path": db_path, "content": "corrupt", "mode": "overwrite"},
    })
    dispatched = client.post(
        f"/work/items/{queued.json()['item_id']}/approve",
        json={"dispatch": True, "typed_confirmation": PHRASE},
    )

    assert dispatched.status_code == 400
    assert "kernel state" in dispatched.json()["detail"]
    # Writing to a legitimate hot_root location still works.
    ok = client.post("/work/items", json={
        "kind": "external", "title": "write a note", "action": "local.file.write",
        "permission_tier": "L3_EXTERNAL_ACTION",
        "source": "zade.proposal",
        "metadata": {"path": str(config.paths.hot_root / "Zade" / "note.txt"), "content": "hi", "mode": "overwrite"},
    })
    ok_dispatch = client.post(
        f"/work/items/{ok.json()['item_id']}/approve",
        json={"dispatch": True, "typed_confirmation": PHRASE},
    )
    assert ok_dispatch.status_code == 200
    assert ok_dispatch.json()["dispatch"] == "dispatched"


def test_ingest_refuses_kernel_state_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = _config(tmp_path)
    client = TestClient(create_app(config))

    outside = client.post("/ingest/file", json={"path": str(tmp_path / "elsewhere" / "secrets.txt")})
    state = client.post("/ingest/file", json={"path": str(config.paths.database_path)})

    assert outside.status_code == 400
    assert "outside allowed memory roots" in outside.json()["detail"]
    assert state.status_code == 400
    assert "kernel state directory" in state.json()["detail"]


def test_ics_ssrf_guards() -> None:
    # Private / loopback / link-local hosts are detected without network.
    assert _host_is_private("10.0.0.1") is True
    assert _host_is_private("192.168.1.10") is True
    assert _host_is_private("169.254.169.254") is True  # cloud metadata
    assert _host_is_private("::1") is True
    assert _host_is_private("8.8.8.8") is False

    def rejects(url: str) -> bool:
        try:
            _fetch_ics_over_http(url)
        except ValueError:
            return True
        return False

    # The old startswith bypass is closed: a look-alike host is not loopback.
    assert rejects("http://localhost.attacker.com/x.ics")
    assert rejects("http://127.0.0.1.attacker.com/x.ics")
    # Non-https external, private targets, and bad schemes all refused.
    assert rejects("http://example.com/x.ics")
    assert rejects("https://10.0.0.1/x.ics")
    assert rejects("https://169.254.169.254/latest/meta-data")
    assert rejects("ftp://example.com/x.ics")


def test_browser_open_rejects_lookalike_and_external_hosts(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    from cofounder_kernel.db import KernelDatabase
    from cofounder_kernel.handlers import ActionHandlerRegistry

    config = _config(tmp_path)
    config.paths.data_dir.mkdir(parents=True, exist_ok=True)
    db = KernelDatabase(config.paths.database_path)
    db.migrate()
    registry = ActionHandlerRegistry(db=db, config=config)

    def item(url: str):
        item_id, _ = db.enqueue_work_item(
            kind="external", title="open", detail="", action="local.browser.open", target="",
            permission_tier="L3_EXTERNAL_ACTION", priority=50, source="test",
            metadata={"url": url},  # note: open_browser not set, so nothing actually launches
        )
        return db.get_work_item(item_id)

    # Look-alike host that beat the old startswith check is now external → refused.
    for bad in ("http://localhost.evil.com/", "http://127.0.0.1.evil.com/", "http://example.com/"):
        try:
            registry.dispatch(item(bad))
            assert False, f"expected refusal for {bad}"
        except ValueError as exc:
            assert "localhost/file" in str(exc)

    # A genuine loopback URL is allowed (does not launch, open_browser unset).
    result = registry.dispatch(item("http://127.0.0.1:8787/ui"))
    assert result["status"] == "ok"
    assert result["opened"] is False


def test_audit_details_redact_secrets(tmp_path: Path) -> None:
    """Secrets that slip into an audit payload must never be persisted in the
    clear — the audit log is a plaintext-readable table."""
    from cofounder_kernel.db import KernelDatabase, _redact_secrets

    # Pure function: redacts secret-named keys at any depth, leaves *_env pointers.
    cleaned = _redact_secrets(
        {
            "api_key": "sk-live-123",
            "deepgram_api_key_env": "DEEPGRAM_API_KEY",  # a pointer, not a secret
            "nested": {"access_token": "abc", "user": "founder"},
            "list": [{"password": "hunter2"}],
            "count": 3,
        }
    )
    assert cleaned["api_key"] == "[redacted]"
    assert cleaned["deepgram_api_key_env"] == "DEEPGRAM_API_KEY"  # exempt
    assert cleaned["nested"]["access_token"] == "[redacted]"
    assert cleaned["nested"]["user"] == "founder"  # non-secret preserved
    assert cleaned["list"][0]["password"] == "[redacted]"
    assert cleaned["count"] == 3

    # End-to-end: what actually lands in the audit table is already redacted.
    config = _config(tmp_path)
    config.paths.data_dir.mkdir(parents=True, exist_ok=True)
    db = KernelDatabase(config.paths.database_path)
    db.migrate()
    db.audit(
        actor="test", action="connector.sync", target="ics", permission_tier="L3_EXTERNAL_ACTION",
        status="ok", details={"secret": "top-secret", "endpoint": "https://cal.example.com/x.ics"},
    )
    stored = db.recent_audit_events(1)[0]["details"]
    assert stored["secret"] == "[redacted]"
    assert stored["endpoint"] == "https://cal.example.com/x.ics"
