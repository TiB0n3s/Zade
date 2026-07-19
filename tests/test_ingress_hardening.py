"""Channel ingress hardening + egress ledger + MCP work.status.

Covers the 2026-07-18 batch: per-binding conversation continuity, per-binding
HMAC signing, the founder-facing egress ledger rollup, and the work.status read
tool on the governed agent surface.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path

from fastapi.testclient import TestClient

from cofounder_kernel.api import create_app
from cofounder_kernel.config import AppConfig, KernelConfig, OllamaConfig, PathConfig
from cofounder_kernel.ollama import GenerateResult, OllamaClient


def fake_health(self: OllamaClient) -> dict:
    return {"version": "test"}


def fake_generate(
    self: OllamaClient,
    *,
    prompt: str,
    model: str | None = None,
    think: bool | None = None,
    temperature: float | None = None,
    num_predict: int = 512,
    format: dict | str | None = None,
) -> GenerateResult:
    # A structured (`format`) call is a distillation request: return a valid empty
    # array so end_session's final distill parses (status ok, nothing to write)
    # and the thread ends cleanly instead of staying loss-safe-active.
    response = "[]" if format is not None else "Acknowledged."
    return GenerateResult(response=response, model=model or "qwen3:14b", raw={})


def _messages_to_prompt(messages: object) -> str:
    return "\n\n".join(str(getattr(message, "content", "")) for message in messages)


def patch_ollama_model(monkeypatch) -> None:
    def fake_chat(self, *, messages, model=None, think=None, temperature=None, num_predict=512, tools=None):
        return fake_generate(self, prompt=_messages_to_prompt(messages), model=model)

    monkeypatch.setattr(OllamaClient, "generate", fake_generate)
    monkeypatch.setattr(OllamaClient, "chat", fake_chat)


def make_client(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    return TestClient(create_app(config))


def _bind(client: TestClient, channel: str = "telegram", external_id: str = "u-777") -> int:
    enroll = client.post("/channels/enroll", json={"channel": channel, "label": "founder phone"})
    code = enroll.json()["code"]
    bound = client.post(
        "/channels/message", json={"channel": channel, "external_id": external_id, "text": f"/bind {code}"}
    )
    assert bound.json()["status"] == "bound"
    return int(bound.json()["binding_id"])


def _sign(key: str, ts: str, text: str) -> str:
    return hmac.new(key.encode(), f"{ts}\n{text}".encode(), hashlib.sha256).hexdigest()


def test_channel_messages_share_one_durable_conversation(tmp_path: Path, monkeypatch) -> None:
    """Messages from a bound identity land in ONE thread — before this, every
    channel message was a standalone amnesiac turn. A second binding gets its
    own thread; an ended thread is replaced, not resurrected."""
    client = make_client(tmp_path, monkeypatch)
    _bind(client)

    first = client.post("/channels/message", json={"channel": "telegram", "external_id": "u-777", "text": "hello"})
    second = client.post("/channels/message", json={"channel": "telegram", "external_id": "u-777", "text": "again"})

    assert first.json()["status"] == "ok"
    assert first.json()["conversation_id"] == second.json()["conversation_id"]
    conversation_id = first.json()["conversation_id"]
    turns = client.get(f"/conversations/{conversation_id}").json()["conversation"]["turns"]
    contents = [t["content"] for t in turns]
    assert "hello" in " ".join(contents) and "again" in " ".join(contents)

    # ended thread -> a fresh one is bound instead of piling onto the corpse
    client.post(f"/conversations/{conversation_id}/end")
    third = client.post("/channels/message", json={"channel": "telegram", "external_id": "u-777", "text": "new day"})
    assert third.json()["status"] == "ok"
    assert third.json()["conversation_id"] != conversation_id


def test_hmac_binding_requires_valid_fresh_monotonic_signature(tmp_path: Path, monkeypatch) -> None:
    """Once a binding carries a signing key, every message must be signed, fresh,
    and strictly newer than the last accepted one — a captured frame cannot be
    replayed. Clearing the key restores the unsigned human path."""
    client = make_client(tmp_path, monkeypatch)
    binding_id = _bind(client)

    issued = client.post(f"/channels/bindings/{binding_id}/hmac")
    key = issued.json()["hmac_key"]
    assert issued.status_code == 200 and len(key) == 64

    base = {"channel": "telegram", "external_id": "u-777"}

    # unsigned message now refused
    unsigned = client.post("/channels/message", json=base | {"text": "hi"})
    assert unsigned.json()["status"] == "unauthenticated"

    # correctly signed message accepted
    ts1 = f"{time.time():.3f}"
    ok = client.post("/channels/message", json=base | {"text": "hi", "ts": ts1, "signature": _sign(key, ts1, "hi")})
    assert ok.json()["status"] == "ok"

    # exact replay refused (same ts), and an older ts refused
    replay = client.post("/channels/message", json=base | {"text": "hi", "ts": ts1, "signature": _sign(key, ts1, "hi")})
    assert replay.json()["status"] == "unauthenticated"

    # tampered text refused
    ts2 = f"{time.time() + 1:.3f}"
    tampered = client.post(
        "/channels/message", json=base | {"text": "send funds", "ts": ts2, "signature": _sign(key, ts2, "hi")}
    )
    assert tampered.json()["status"] == "unauthenticated"

    # stale timestamp refused even when correctly signed
    old_ts = f"{time.time() - 3600:.3f}"
    stale = client.post(
        "/channels/message", json=base | {"text": "hi", "ts": old_ts, "signature": _sign(key, old_ts, "hi")}
    )
    assert stale.json()["status"] == "unauthenticated"

    # clearing the key restores the unsigned path
    client.post(f"/channels/bindings/{binding_id}/hmac/clear")
    plain = client.post("/channels/message", json=base | {"text": "back to human"})
    assert plain.json()["status"] == "ok"


def test_egress_ledger_rolls_up_decisions_and_grants(tmp_path: Path, monkeypatch) -> None:
    """The ledger answers the founder questions the raw audit buries: what left,
    what was blocked, and the grant lifecycle — built purely from audit rows."""
    from cofounder_kernel.db import KernelDatabase
    from cofounder_kernel.egress import (
        DataClass,
        EgressPolicy,
        EgressRequest,
        approve_egress_grant,
        authorize_egress,
        consume_grant,
        egress_ledger,
    )

    db = KernelDatabase(tmp_path / "ledger.sqlite")
    db.migrate()
    gate = EgressPolicy(provider_policy="local_preferred")

    request = EgressRequest(request_id="r-1", data_class=DataClass.FOUNDER_BRIEF, vendor="anthropic")
    first = authorize_egress(db, gate, request, preview="quarterly brief")
    assert first.verdict.value == "auth_required"
    pending = [r for r in db.list_approval_requests(status="pending", limit=10)]
    approve_egress_grant(db, pending[0].id, typed_phrase="make the jump to hyperspace")
    allowed = authorize_egress(db, gate, request)
    assert allowed.allowed
    consume_grant(db, request)
    refused = authorize_egress(db, gate, EgressRequest(request_id="r-2", data_class=DataClass.FOUNDER_STATE, vendor="anthropic"))
    assert not refused.allowed

    ledger = egress_ledger(db)

    assert ledger["summary"]["left_the_machine"] == 1
    assert ledger["summary"]["blocked"] >= 1
    send = ledger["left_the_machine"][0]
    assert send["vendor"] == "anthropic"
    assert send["data_class"] == "founder_brief"
    assert send["grant_request_id"] == pending[0].id
    assert any(b["data_class"] == "founder_state" for b in ledger["blocked"])
    grants = ledger["grants"]
    assert grants and grants[0]["status"] == "consumed"
    assert grants[0]["preview"] == "quarterly brief" or grants[0]["preview"]  # preview text present


def test_work_status_tool_is_exposed_read_only(tmp_path: Path, monkeypatch) -> None:
    """work.status joins the governed surface: readable internally and via the
    agent surface, counts + titles only, and it is read-only (L0)."""
    from cofounder_kernel.agent_surface import AgentSurface
    from cofounder_kernel.authority import AuthorityPolicy
    from cofounder_kernel.db import KernelDatabase
    from cofounder_kernel.mcp_server import LIVE_EXPOSED
    from cofounder_kernel.tools import ToolRegistry

    db = KernelDatabase(tmp_path / "tools.sqlite")
    db.migrate()
    db.enqueue_work_item(
        kind="build",
        title="wire the adapter",
        detail="",
        action="dev.command.run",
        target="workspace",
        permission_tier="L2_FILE_WRITE",
        priority=70,
        source="test",
    )
    registry = ToolRegistry(db, AuthorityPolicy(hot_root=tmp_path, cold_root=tmp_path, data_dir=tmp_path))

    result = registry.call("work.status", {"limit": 5})
    assert result.ok
    assert result.data["counts"].get("pending") == 1
    assert result.data["items"][0]["title"] == "wire the adapter"
    assert "metadata" not in result.data["items"][0]  # titles/states only, no bodies

    surface = AgentSurface(registry)
    manifest_names = {t.name for t in surface.manifest()}
    assert "work.status" in manifest_names
    assert "work.status" in LIVE_EXPOSED
    surfaced = surface.call("work.status", {"limit": 5}, client="claude-code")
    assert surfaced.ok
    assert surfaced.data["items"][0]["title"] == "wire the adapter"
