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
) -> GenerateResult:
    return GenerateResult(response="This is the next move.", model=model or "qwen3:14b", raw={"prompt": prompt})


def _config(tmp_path: Path) -> KernelConfig:
    return KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )


def test_governed_respond_persists_and_recalls_conversation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    prompts: list[str] = []

    def capturing_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512):
        prompts.append(prompt)
        return GenerateResult(response=f"Reply {len(prompts)}.", model=model or "qwen3:14b", raw={"prompt": prompt})

    monkeypatch.setattr(OllamaClient, "generate", capturing_generate)
    client = TestClient(create_app(_config(tmp_path)))

    created = client.post("/conversations", json={"title": ""})
    conversation_id = created.json()["conversation"]["id"]

    first = client.post(
        "/runtime/respond",
        json={
            "message": "We should price Zade at $99 per month for solo founders.",
            "conversation_id": conversation_id,
            "use_semantic_memory": False,
        },
    )
    second = client.post(
        "/runtime/respond",
        json={
            "message": "Remind me what price we landed on.",
            "conversation_id": conversation_id,
            "use_semantic_memory": False,
        },
    )
    loaded = client.get(f"/conversations/{conversation_id}")
    turns = client.get(f"/conversations/{conversation_id}/turns")

    assert created.status_code == 200
    assert first.status_code == 200
    assert first.json()["conversation"]["id"] == conversation_id
    assert first.json()["conversation"]["assistant_turn_id"] > 0
    assert "episodic_conversation_memory" in first.json()["governor"]["applied_rules"]
    assert second.status_code == 200

    # Turn 1 has no prior context; turn 2 must recall the first full exchange.
    assert "No recorded turns yet." in prompts[0]
    assert "Conversation memory:" in prompts[1]
    assert "$99 per month" in prompts[1]  # prior user turn recalled
    assert "Reply 1." in prompts[1]  # prior assistant turn recalled
    assert "Remind me what price we landed on." in prompts[1]  # current message

    # Four turns recorded in order: user, assistant, user, assistant.
    turn_list = turns.json()["turns"]
    assert [turn["role"] for turn in turn_list] == ["user", "assistant", "user", "assistant"]
    assert turn_list[0]["content"].startswith("We should price Zade")
    assert turn_list[1]["content"] == "Reply 1."
    assert loaded.json()["conversation"]["turn_count"] == 4
    # The title is derived from the first user message.
    assert loaded.json()["conversation"]["title"].startswith("We should price Zade")


def test_conversation_summary_rolls_over_older_turns(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "generate", fake_generate)
    app = create_app(_config(tmp_path))
    conversations = app.state.conversations

    conversation = conversations.create(title="Pricing thread")
    conversation_id = conversation["id"]
    for index in range(20):
        conversations.record_user_turn(conversation_id, content=f"user message {index}")
        conversations.record_assistant_turn(conversation_id, content=f"assistant message {index}")

    result = conversations.maybe_summarize(conversation_id)

    assert result is not None
    assert result["summarized_turns"] >= conversations.SUMMARY_MIN_OVERFLOW
    loaded = conversations.get(conversation_id)
    assert loaded["summary"] == "This is the next move."
    assert loaded["summary_through_turn_id"] == result["summary_through_turn_id"]

    # The prompt context shows the rolling summary and only the recent window verbatim.
    context = conversations.prompt_context(conversation_id)
    assert context["state"]["has_summary"] is True
    assert context["state"]["recent_turns_in_prompt"] == conversations.RECENT_WINDOW
    assert "Earlier summary: This is the next move." in context["block"]


def test_short_thread_does_not_summarize(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "generate", fake_generate)
    app = create_app(_config(tmp_path))
    conversations = app.state.conversations

    conversation = conversations.create()
    conversation_id = conversation["id"]
    conversations.record_user_turn(conversation_id, content="only one exchange")
    conversations.record_assistant_turn(conversation_id, content="acknowledged")

    assert conversations.maybe_summarize(conversation_id) is None
    assert conversations.get(conversation_id)["summary"] == ""


def test_unknown_conversation_id_is_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "generate", fake_generate)
    client = TestClient(create_app(_config(tmp_path)))

    missing = client.post(
        "/runtime/respond",
        json={"message": "hello", "conversation_id": 999, "use_semantic_memory": False},
    )
    missing_get = client.get("/conversations/999")
    inventory = client.get("/self-inventory")

    assert missing.status_code == 404
    assert missing_get.status_code == 404
    assert "POST /conversations" in inventory.json()["conversation_layer"]["routes"]
    assert "conversations" in inventory.json()["conversation_layer"]["artifacts"]
