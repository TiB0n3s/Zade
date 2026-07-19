"""Direct Telegram Bot API adapter: message projection, the governed-flow route,
and the egress-gated reply.

The Telegram HTTP calls are faked (a stub client), so no network and no real bot
token are needed. The egress gate runs for real against a KernelConfig.
"""
from __future__ import annotations

import json

from cofounder_kernel.config import (
    AppConfig,
    EgressConfig,
    KernelConfig,
    OllamaConfig,
    TelegramConfig,
)
from cofounder_kernel.telegram_adapter import (
    TelegramAdapter,
    TelegramClient,
    project_update,
)


def make_config(*, grant: bool, policy: str = "local_preferred") -> KernelConfig:
    grants = ("reply_text:telegram",) if grant else ()
    return KernelConfig(
        app=AppConfig(),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1", provider_policy=policy),
        telegram=TelegramConfig(enabled=True, token_env="TELEGRAM_TEST_TOKEN"),
        egress=EgressConfig(standing_grants=grants),
    )


class FakeClient:
    """Stub TelegramClient: hands back queued updates, records sends."""

    def __init__(self, updates: list[dict] | None = None):
        self._updates = list(updates or [])
        self.sent: list[tuple[int, str]] = []

    def get_updates(self, offset, *, timeout):
        out, self._updates = self._updates, []
        return out

    def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


class FakeDB:
    def __init__(self):
        self.audits: list[dict] = []

    def audit(self, **kw):
        self.audits.append(kw)


def make_adapter(config: KernelConfig, route, updates=None):
    adapter = TelegramAdapter(config, route_message=route, db=FakeDB(), token="test-token")
    adapter._client = FakeClient(updates)  # type: ignore[assignment]
    return adapter


# ---- projection -------------------------------------------------------------
def test_project_update_extracts_text_message() -> None:
    inbound = project_update(
        {"update_id": 1, "message": {"text": "  /bind abc123 ", "from": {"id": 777, "is_bot": False}, "chat": {"id": 777}}}
    )
    assert inbound is not None
    assert inbound.external_id == "777"
    assert inbound.chat_id == 777
    assert inbound.text == "/bind abc123"


def test_project_update_skips_non_message_and_bot_and_nontext() -> None:
    assert project_update({"update_id": 2, "edited_message": {"text": "x"}}) is None
    assert project_update({"update_id": 3, "message": {"from": {"id": 1}, "chat": {"id": 1}, "sticker": {}}}) is None
    assert (
        project_update({"update_id": 4, "message": {"text": "x", "from": {"id": 1, "is_bot": True}, "chat": {"id": 1}}})
        is None
    )


# ---- routing + reply --------------------------------------------------------
def test_bound_message_routes_and_replies_when_granted() -> None:
    seen: list = []

    def route(inbound):
        seen.append(inbound)
        return {"status": "ok", "reply": "Here is my answer."}

    adapter = make_adapter(make_config(grant=True), route, updates=[
        {"update_id": 10, "message": {"text": "what's next?", "from": {"id": 42, "is_bot": False}, "chat": {"id": 42}}}
    ])
    adapter._poll_once()

    assert seen and seen[0].external_id == "42"
    assert adapter._client.sent == [(42, "Here is my answer.")]  # type: ignore[attr-defined]
    # offset advanced past the processed update
    assert adapter._offset == 11


def test_reply_is_fail_closed_without_standing_grant() -> None:
    """No reply_text:telegram grant -> inbound still routes (binds happen) but NO
    reply leaves. The egress decision is audited as refused."""
    routed: list = []

    def route(inbound):
        routed.append(inbound)
        return {"status": "ok", "reply": "secret strategy"}

    adapter = make_adapter(make_config(grant=False), route, updates=[
        {"update_id": 20, "message": {"text": "hi", "from": {"id": 5, "is_bot": False}, "chat": {"id": 5}}}
    ])
    adapter._poll_once()

    assert routed  # inbound was processed
    assert adapter._client.sent == []  # type: ignore[attr-defined]  # nothing left the machine
    audit = adapter.db.audits[-1]
    assert audit["action"] == "egress.decision"
    assert audit["status"] == "refused"


def test_unbound_peer_gets_silence_not_a_reply() -> None:
    """An unbound peer's ordinary message returns 'unauthenticated'; Zade does not
    egress a reply to a stranger who merely found the bot."""
    def route(inbound):
        return {"status": "unauthenticated", "reply": "This channel is not authorized."}

    adapter = make_adapter(make_config(grant=True), route, updates=[
        {"update_id": 30, "message": {"text": "hello?", "from": {"id": 9, "is_bot": False}, "chat": {"id": 9}}}
    ])
    adapter._poll_once()
    assert adapter._client.sent == []  # type: ignore[attr-defined]


def test_bind_confirmation_and_bind_failure_do_reply() -> None:
    replies = {"/bind good": {"status": "bound", "reply": "This channel is now bound to the founder."},
               "/bind bad": {"status": "bind_failed", "reply": "No matching pending enrollment for that code."}}

    def route(inbound):
        return replies[inbound.text]

    adapter = make_adapter(make_config(grant=True), route, updates=[
        {"update_id": 40, "message": {"text": "/bind good", "from": {"id": 1, "is_bot": False}, "chat": {"id": 1}}},
        {"update_id": 41, "message": {"text": "/bind bad", "from": {"id": 2, "is_bot": False}, "chat": {"id": 2}}},
    ])
    adapter._poll_once()
    assert adapter._client.sent == [  # type: ignore[attr-defined]
        (1, "This channel is now bound to the founder."),
        (2, "No matching pending enrollment for that code."),
    ]


def test_local_only_policy_blocks_reply_even_with_grant() -> None:
    """Under provider_policy=local_only the egress gate denies every non-local
    destination regardless of standing grants — the reply is withheld."""
    def route(inbound):
        return {"status": "ok", "reply": "answer"}

    adapter = make_adapter(make_config(grant=True, policy="local_only"), route, updates=[
        {"update_id": 50, "message": {"text": "hi", "from": {"id": 3, "is_bot": False}, "chat": {"id": 3}}}
    ])
    adapter._poll_once()
    assert adapter._client.sent == []  # type: ignore[attr-defined]


# ---- client request construction -------------------------------------------
def test_client_builds_token_scoped_json_requests(monkeypatch) -> None:
    import cofounder_kernel.telegram_adapter as mod

    calls: list[tuple[str, dict]] = []

    class FakeResp:
        def __init__(self, payload):
            self._p = json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self._p

    def fake_urlopen(request, timeout=0):
        body = json.loads(request.data.decode())
        calls.append((request.full_url, body))
        if request.full_url.endswith("/sendMessage"):
            return FakeResp({"ok": True, "result": {"message_id": 1}})
        return FakeResp({"ok": True, "result": []})

    monkeypatch.setattr(mod.netguard, "assert_allowed", lambda *a, **k: None)
    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)

    client = TelegramClient(TelegramConfig(), token="123:ABC")
    client.get_updates(7, timeout=25)
    client.send_message(555, "hi there")

    get_url, get_body = calls[0]
    assert get_url == "https://api.telegram.org/bot123:ABC/getUpdates"
    assert get_body["offset"] == 7 and get_body["allowed_updates"] == ["message"]
    send_url, send_body = calls[1]
    assert send_url == "https://api.telegram.org/bot123:ABC/sendMessage"
    assert send_body == {"chat_id": 555, "text": "hi there"}
