"""Conversation -> memory distillation: durable knowledge from chat is promoted
into the searchable memory store, incrementally and idempotently."""
from pathlib import Path

from fastapi.testclient import TestClient

from cofounder_kernel.api import create_app
from cofounder_kernel.config import AppConfig, KernelConfig, OllamaConfig, PathConfig
from cofounder_kernel.ollama import GenerateResult, OllamaClient


def fake_health(self: OllamaClient) -> dict:
    return {"version": "test"}


def _config(tmp_path: Path) -> KernelConfig:
    return KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )


def _make(tmp_path: Path, monkeypatch, holder: dict):
    """App whose model returns whatever ``holder['text']`` currently holds, so a
    test can script the extraction output (and change it mid-test)."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)

    def scripted_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512):
        return GenerateResult(response=holder["text"], model=model or "qwen3:14b", raw={})

    monkeypatch.setattr(OllamaClient, "generate", scripted_generate)
    return create_app(_config(tmp_path))


def _seed(conversations, conversation_id: int, n: int = 3) -> None:
    for index in range(n):
        conversations.record_user_turn(conversation_id, content=f"user {index}: we settled the plan")
        conversations.record_assistant_turn(conversation_id, content=f"assistant {index}: understood")


def test_distill_promotes_turns_into_searchable_memory(tmp_path: Path, monkeypatch) -> None:
    holder = {
        "text": (
            '[{"kind":"decision","title":"Pilot price is 99/mo",'
            '"content":"Zade is priced at 99 per month for solo founders."},'
            '{"kind":"preference","title":"Outcome pricing preferred",'
            '"content":"The founder prefers outcome pricing over seat pricing."}]'
        )
    }
    app = _make(tmp_path, monkeypatch, holder)
    conversations = app.state.conversations
    db = conversations.db
    cid = conversations.create(title="Pricing")["id"]
    _seed(conversations, cid, n=3)

    result = conversations.distill(cid, min_turns=1, only_aged_out=False)
    assert result is not None and result["status"] == "ok"
    assert result["count"] == 2

    memories = db.list_memories_by_source(f"conversation:{cid}")
    assert sorted(m.kind for m in memories) == ["chat_decision", "chat_preference"]
    assert all(m.source == f"conversation:{cid}" for m in memories)

    # Promoted knowledge is searchable via full-text memory search.
    hits = db.search_memories("outcome", limit=10)
    assert any("outcome pricing" in h.content.lower() for h in hits)

    # Cursor advanced to the last processed turn.
    assert conversations.get(cid)["distilled_through_turn_id"] == result["distilled_through_turn_id"]

    # Idempotent: re-running with no new turns is a no-op and writes nothing new.
    assert conversations.distill(cid, min_turns=1, only_aged_out=False) is None
    assert len(db.list_memories_by_source(f"conversation:{cid}")) == 2


def test_extraction_failure_leaves_cursor_for_retry(tmp_path: Path, monkeypatch) -> None:
    holder = {"text": "Sorry, I can't produce that."}  # no JSON array -> parse failure
    app = _make(tmp_path, monkeypatch, holder)
    conversations = app.state.conversations
    db = conversations.db
    cid = conversations.create()["id"]
    _seed(conversations, cid, n=2)

    failed = conversations.distill(cid, min_turns=1, only_aged_out=False)
    assert failed is not None and failed["status"] == "extraction_failed"
    assert conversations.get(cid)["distilled_through_turn_id"] is None
    assert db.list_memories_by_source(f"conversation:{cid}") == []

    # A later call (model now cooperating) promotes the same turns — nothing lost.
    holder["text"] = '[{"kind":"fact","title":"Meridian is a pilot","content":"Meridian is an active pilot customer."}]'
    ok = conversations.distill(cid, min_turns=1, only_aged_out=False)
    assert ok is not None and ok["count"] == 1
    assert conversations.get(cid)["distilled_through_turn_id"] is not None


def test_duplicate_titles_are_not_written_twice(tmp_path: Path, monkeypatch) -> None:
    holder = {"text": '[{"kind":"decision","title":"Pilot price is 99/mo","content":"first wording"}]'}
    app = _make(tmp_path, monkeypatch, holder)
    conversations = app.state.conversations
    db = conversations.db
    cid = conversations.create()["id"]
    _seed(conversations, cid, n=1)
    assert conversations.distill(cid, min_turns=1, only_aged_out=False)["count"] == 1

    # New turns, but the model surfaces the SAME title -> deduped against memory.
    _seed(conversations, cid, n=1)
    holder["text"] = '[{"kind":"decision","title":"Pilot price is 99/mo","content":"second wording"}]'
    assert conversations.distill(cid, min_turns=1, only_aged_out=False)["count"] == 0
    assert len(db.list_memories_by_source(f"conversation:{cid}")) == 1


def test_distill_endpoint_promotes_and_404s_unknown(tmp_path: Path, monkeypatch) -> None:
    holder = {
        "text": '[{"kind":"commitment","title":"Send pilot proposal","content":"Founder will send the pilot pricing proposal."}]'
    }
    app = _make(tmp_path, monkeypatch, holder)
    conversations = app.state.conversations
    cid = conversations.create()["id"]
    _seed(conversations, cid, n=1)

    client = TestClient(app)
    resp = client.post(f"/conversations/{cid}/distill")
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["count"] == 1
    assert body["written"][0]["kind"] == "chat_commitment"

    assert client.post("/conversations/999999/distill").status_code == 404
