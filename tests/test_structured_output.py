"""Tests for server-side structured output (Ollama `format`).

The JSON-contract calls — contrarian critic, role passes, conversation
distillation — send their JSON schema as Ollama's `format` field so the shape
is grammar-enforced at sampling time. [ollama] structured_output is the kill
switch: off, the request body carries no format and the prompt contract +
tolerant parsers carry the load exactly as before.
"""

from pathlib import Path

from fastapi.testclient import TestClient

from cofounder_kernel.api import create_app
from cofounder_kernel.config import AppConfig, KernelConfig, OllamaConfig, PathConfig
from cofounder_kernel.critic import CRITIQUE_SCHEMA, VERDICTS
from cofounder_kernel.ollama import GenerateResult, OllamaClient
from cofounder_kernel.roles import FINDING_SCHEMA


def fake_health(self: OllamaClient) -> dict:
    return {"version": "test"}


def _client(structured_output: bool = True) -> OllamaClient:
    return OllamaClient(OllamaConfig(base_url="http://127.0.0.1:1", structured_output=structured_output))


def _capture_post(monkeypatch, client: OllamaClient) -> list[dict]:
    bodies: list[dict] = []

    def fake_post(self, path, body):
        bodies.append(body)
        return {"response": "{}", "message": {"content": "{}"}}

    monkeypatch.setattr(type(client), "_post_json", fake_post)
    return bodies


# ------------------------------------------------------------------ client


def test_generate_sends_schema_as_format(monkeypatch) -> None:
    client = _client()
    bodies = _capture_post(monkeypatch, client)
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    client.generate(prompt="p", model="qwen3:14b", format=schema)
    assert bodies[0]["format"] == schema


def test_generate_accepts_json_string_format(monkeypatch) -> None:
    client = _client()
    bodies = _capture_post(monkeypatch, client)
    client.generate(prompt="p", model="qwen3:14b", format="json")
    assert bodies[0]["format"] == "json"


def test_generate_without_format_sends_none(monkeypatch) -> None:
    client = _client()
    bodies = _capture_post(monkeypatch, client)
    client.generate(prompt="p", model="qwen3:14b")
    assert "format" not in bodies[0]


def test_structured_output_off_is_a_kill_switch(monkeypatch) -> None:
    client = _client(structured_output=False)
    bodies = _capture_post(monkeypatch, client)
    client.generate(prompt="p", model="qwen3:14b", format={"type": "object"})
    client.chat(
        messages=[{"role": "user", "content": "hi"}],
        model="qwen3:14b",
        format={"type": "object"},
    )
    assert all("format" not in body for body in bodies)


def test_chat_sends_schema_as_format(monkeypatch) -> None:
    client = _client()
    bodies = _capture_post(monkeypatch, client)
    client.chat(
        messages=[{"role": "user", "content": "hi"}],
        model="qwen3:14b",
        format={"type": "object"},
    )
    assert bodies[0]["format"] == {"type": "object"}


# ------------------------------------------------------------------ schemas


def test_critique_schema_matches_parser_contract() -> None:
    assert set(CRITIQUE_SCHEMA["properties"]["verdict"]["enum"]) == VERDICTS
    adjustment = CRITIQUE_SCHEMA["properties"]["confidence_adjustment"]
    # The red team may only lower or hold confidence — schema and clamp agree.
    assert adjustment["minimum"] == -50 and adjustment["maximum"] == 0
    assert set(CRITIQUE_SCHEMA["required"]) == set(CRITIQUE_SCHEMA["properties"])


def test_finding_schema_matches_parser_contract() -> None:
    assert FINDING_SCHEMA["properties"]["points"]["maxItems"] == 8
    assert set(FINDING_SCHEMA["required"]) == set(FINDING_SCHEMA["properties"])


# ------------------------------------------------------------- call sites


def _config(tmp_path: Path) -> KernelConfig:
    return KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )


def _app_state(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    return TestClient(create_app(_config(tmp_path))).app.state


def _capture_generate(monkeypatch, response: str) -> list[dict]:
    calls: list[dict] = []

    def fake_generate(self, **kwargs):
        calls.append(kwargs)
        return GenerateResult(response=response, model=str(kwargs.get("model") or "qwen3:14b"), raw={})

    monkeypatch.setattr(OllamaClient, "generate", fake_generate)
    return calls


def test_critic_passes_critique_schema(tmp_path: Path, monkeypatch) -> None:
    state = _app_state(tmp_path, monkeypatch)
    calls = _capture_generate(
        monkeypatch,
        '{"verdict": "proceed", "weakest_assumption": "", "missing_evidence": "", '
        '"downside_risk": "", "confidence_adjustment": 0}',
    )
    critique = state.critic.challenge(message="Should we launch?", draft_response="Launch.", context={})
    assert calls[0]["format"] == CRITIQUE_SCHEMA
    assert critique["verdict"] == "proceed"


def test_role_pass_passes_finding_schema(tmp_path: Path, monkeypatch) -> None:
    state = _app_state(tmp_path, monkeypatch)
    calls = _capture_generate(
        monkeypatch, '{"verdict": "solid", "summary": "Holds up.", "points": ["one"]}'
    )
    finding = state.roles.run(role="red_team", content="Plan: ship it.")
    assert calls[0]["format"] == FINDING_SCHEMA
    assert finding["verdict"] == "solid"


def test_distillation_passes_kind_constrained_array_schema(tmp_path: Path, monkeypatch) -> None:
    state = _app_state(tmp_path, monkeypatch)
    calls = _capture_generate(
        monkeypatch,
        '[{"kind": "decision", "title": "Pricing", "content": "Keep annual pricing."}]',
    )
    conversation_id = state.conversations.create()["id"]
    for index in range(3):
        state.conversations.record_user_turn(conversation_id, content=f"user {index}: pricing talk")
        state.conversations.record_assistant_turn(conversation_id, content=f"assistant {index}: noted")
    result = state.conversations.distill(conversation_id, min_turns=1, only_aged_out=False)
    schema = calls[0]["format"]
    assert schema["type"] == "array"
    assert schema["items"]["properties"]["kind"]["enum"] == list(state.conversations.DISTILL_KINDS)
    assert schema["items"]["required"] == ["kind", "title", "content"]
    assert result["count"] == 1
