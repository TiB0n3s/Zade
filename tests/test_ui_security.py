"""UI-pass hardening: RC1 token bootstrap + strict security response headers.

Note: the autouse ``_no_bootstrap_token`` fixture in conftest neuters minting so
the functional suite runs unauthenticated. Bootstrap-minting is tested here by
calling ``_resolve_local_token`` directly (imported reference is unaffected by
the fixture's module-attribute patch); protection/headers are tested by
configuring an explicit token, which the fixture passes through unchanged.
"""
from pathlib import Path

from fastapi.testclient import TestClient

from cofounder_kernel.api import _resolve_local_token, create_app
from cofounder_kernel.config import AppConfig, KernelConfig, OllamaConfig, PathConfig, SecurityConfig
from cofounder_kernel.ollama import OllamaClient


def fake_health(self: OllamaClient) -> dict:
    return {"version": "test"}


def _config(tmp_path: Path, **security) -> KernelConfig:
    return KernelConfig(
        app=security.pop("app", AppConfig()),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
        security=SecurityConfig(**security) if security else SecurityConfig(),
    )


def test_bootstrap_mints_persists_and_is_stable(tmp_path: Path) -> None:
    cfg = _config(tmp_path)  # defaults: protect_mutations on, no token configured
    cfg.paths.data_dir.mkdir(parents=True, exist_ok=True)

    first = _resolve_local_token(cfg)
    token_file = cfg.paths.data_dir / "local_token"

    assert first and len(first) >= 32           # a real random token was minted
    assert token_file.read_text(encoding="utf-8").strip() == first  # persisted
    assert _resolve_local_token(cfg) == first   # stable across restarts (re-read, not re-minted)


def test_bootstrap_respects_explicit_token_and_opt_out(tmp_path: Path) -> None:
    explicit = _config(tmp_path, local_token="chosen-by-founder")
    explicit.paths.data_dir.mkdir(parents=True, exist_ok=True)
    assert _resolve_local_token(explicit) == "chosen-by-founder"
    assert not (explicit.paths.data_dir / "local_token").exists()  # no mint when configured

    disabled = _config(tmp_path, protect_mutations=False)
    disabled.paths.data_dir.mkdir(parents=True, exist_ok=True)
    assert _resolve_local_token(disabled) == ""  # opted out → stays open by choice


def test_security_headers_present_on_every_response(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))

    resp = client.get("/health")
    csp = resp.headers.get("content-security-policy", "")

    assert "default-src 'self'" in csp        # no external origin is loadable
    assert "connect-src 'self'" in csp         # the browser cannot exfiltrate off-origin
    assert "object-src 'none'" in csp
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("x-frame-options") == "DENY"
    assert resp.headers.get("referrer-policy") == "no-referrer"


def test_session_token_serves_loopback_and_gates_networked_binds(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)

    # Loopback bind + explicit token: the UI can bootstrap it.
    local = TestClient(create_app(_config(tmp_path, local_token="secret")))
    served = local.get("/session/token")
    assert served.status_code == 200
    assert served.json()["token"] == "secret"
    assert served.json()["required"] is True

    # Networked bind (0.0.0.0): the token is NOT surrendered to remote clients.
    networked = TestClient(create_app(_config(tmp_path, app=AppConfig(host="0.0.0.0"), local_token="secret")))
    assert networked.get("/session/token").status_code == 403


def test_mutation_gate_and_401_also_carries_headers(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path, local_token="secret")))

    blocked = client.post("/memory", json={"kind": "note", "title": "x", "content": "y"})
    allowed = client.post(
        "/memory", headers={"X-Zade-Token": "secret"},
        json={"kind": "note", "title": "x", "content": "y"},
    )

    assert blocked.status_code == 401
    assert "content-security-policy" in {k.lower() for k in blocked.headers}  # 401 is hardened too
    assert allowed.status_code == 200
