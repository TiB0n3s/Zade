from pathlib import Path

from fastapi.testclient import TestClient

from cofounder_kernel.api import create_app
from cofounder_kernel.config import AppConfig, KernelConfig, OllamaConfig, PathConfig
from cofounder_kernel.critic import RECOMMENDATION_SIGNALS, _parse_critique
from cofounder_kernel.ollama import GenerateResult, OllamaClient, OllamaError


REASONING_MODEL = "deepseek-r1:14b"

CRITIC_JSON = (
    '{"verdict": "proceed_with_changes", "weakest_assumption": "Manual habits stick", '
    '"missing_evidence": "Retention data from week two", "downside_risk": "Two weeks lost polishing UI", '
    '"confidence_adjustment": -15}'
)


def fake_health(self: OllamaClient) -> dict:
    return {"version": "test"}


def _config(tmp_path: Path) -> KernelConfig:
    return KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )


def _generate_stub(critic_response: str, calls: list[dict]):
    def fake_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512):
        calls.append({"prompt": prompt, "model": model, "think": think})
        if model == REASONING_MODEL:
            return GenerateResult(response=critic_response, model=model, raw={})
        return GenerateResult(response="Prioritize evidence intake.", model=model or "qwen3:14b", raw={})

    return fake_generate


def _messages_to_prompt(messages: object) -> str:
    return "\n\n".join(str(getattr(message, "content", "")) for message in messages)


def _chat_from_generate(generate_func):
    def fake_chat(self, *, messages, model=None, think=None, temperature=None, num_predict=512, tools=None):
        return generate_func(
            self,
            prompt=_messages_to_prompt(messages),
            model=model,
            think=think,
            temperature=temperature,
            num_predict=num_predict,
        )

    return fake_chat


def patch_ollama_model(monkeypatch, generate_func) -> None:
    monkeypatch.setattr(OllamaClient, "generate", generate_func)
    monkeypatch.setattr(OllamaClient, "chat", _chat_from_generate(generate_func))


def test_recommendation_message_triggers_contrarian_pass(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    calls: list[dict] = []
    patch_ollama_model(monkeypatch, _generate_stub(CRITIC_JSON, calls))
    client = TestClient(create_app(_config(tmp_path)))

    response = client.post(
        "/runtime/respond",
        json={
            "message": "Should we prioritize evidence intake or dashboard polish next?",
            "use_semantic_memory": False,
        },
    )
    reviews = client.get("/founder/contrarian-reviews")
    telemetry = client.get("/models/telemetry")

    assert response.status_code == 200
    payload = response.json()
    # Two model calls: draft on the chat model, attack on the reasoning model with think enabled.
    assert len(calls) == 2
    assert calls[1]["model"] == REASONING_MODEL
    assert calls[1]["think"] is True
    assert "attack it first" in calls[1]["prompt"]
    assert "Prioritize evidence intake." in calls[1]["prompt"]
    # The challenge is attached visibly, never silently rewritten.
    assert payload["response"].startswith("Prioritize evidence intake.")
    assert "Contrarian check (reasoning-model red team):" in payload["response"]
    assert "- Verdict: proceed_with_changes" in payload["response"]
    assert "- Weakest assumption: Manual habits stick" in payload["response"]
    assert "- Confidence adjustment: -15" in payload["response"]
    assert "contrarian_pass_applied" in payload["governor"]["applied_rules"]
    assert payload["contrarian"]["status"] == "ok"
    assert payload["contrarian"]["verdict"] == "proceed_with_changes"
    assert payload["contrarian"]["review_id"] > 0
    # The pass persists into the founder operating layer.
    review = reviews.json()["items"][0]
    assert review["subject_type"] == "runtime_event"
    assert review["subject_id"] == payload["event_id"]
    assert review["recommendation"] == "proceed_with_changes"
    assert review["confidence_adjustment"] == -15
    assert review["metadata"]["auto"] is True
    assert "red_team" in review["roles"]
    assert telemetry.json()["by_operation"]["runtime.contrarian"] == 1
    assert telemetry.json()["by_operation"]["runtime.respond"] == 1


def test_plain_message_skips_contrarian_pass(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    calls: list[dict] = []
    patch_ollama_model(monkeypatch, _generate_stub(CRITIC_JSON, calls))
    client = TestClient(create_app(_config(tmp_path)))

    response = client.post(
        "/runtime/respond",
        json={"message": "Summarize the current memory state.", "use_semantic_memory": False},
    )

    assert response.status_code == 200
    assert len(calls) == 1
    assert "Contrarian check" not in response.json()["response"]
    assert response.json()["contrarian"] is None
    assert "contrarian_pass_applied" not in response.json()["governor"]["applied_rules"]


def test_explicit_flag_overrides_heuristic_both_ways(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    calls: list[dict] = []
    patch_ollama_model(monkeypatch, _generate_stub(CRITIC_JSON, calls))
    client = TestClient(create_app(_config(tmp_path)))

    forced = client.post(
        "/runtime/respond",
        json={"message": "Summarize the current memory state.", "contrarian": True, "use_semantic_memory": False},
    )
    suppressed = client.post(
        "/runtime/respond",
        json={
            "message": "Should we prioritize evidence intake next?",
            "contrarian": False,
            "use_semantic_memory": False,
        },
    )

    assert forced.status_code == 200
    assert forced.json()["contrarian"]["status"] == "ok"
    assert "Contrarian check" in forced.json()["response"]
    assert suppressed.status_code == 200
    assert suppressed.json()["contrarian"] is None
    assert "Contrarian check" not in suppressed.json()["response"]


def test_critic_failure_is_non_blocking(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)

    def failing_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512):
        if model == REASONING_MODEL:
            raise OllamaError("reasoning model offline")
        return GenerateResult(response="Prioritize evidence intake.", model=model or "qwen3:14b", raw={})

    patch_ollama_model(monkeypatch, failing_generate)
    client = TestClient(create_app(_config(tmp_path)))

    response = client.post(
        "/runtime/respond",
        json={"message": "Should we prioritize evidence intake next?", "use_semantic_memory": False},
    )
    reviews = client.get("/founder/contrarian-reviews")
    error_calls = client.get("/models/telemetry/calls", params={"status": "error"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["response"] == "Prioritize evidence intake."
    assert "Contrarian check" not in payload["response"]
    assert payload["contrarian"]["status"] == "error"
    assert "reasoning model offline" in payload["contrarian"]["error"]
    assert "review_id" not in payload["contrarian"]
    assert "Contrarian pass failed; response returned unchallenged." in payload["governor"]["notes"]
    assert reviews.json()["items"] == []
    assert error_calls.json()["items"][0]["operation"] == "runtime.contrarian"


def test_unparseable_critique_attaches_raw_text(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    calls: list[dict] = []
    patch_ollama_model(monkeypatch, _generate_stub("The draft is directionally fine but thin on retention proof.", calls))
    client = TestClient(create_app(_config(tmp_path)))

    response = client.post(
        "/runtime/respond",
        json={"message": "Should we prioritize evidence intake next?", "use_semantic_memory": False},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["contrarian"]["verdict"] == "unparsed"
    assert "- Verdict: unstructured response; treat as proceed_with_changes until rerun" in payload["response"]
    assert "- Critique: The draft is directionally fine but thin on retention proof." in payload["response"]
    assert "- Confidence adjustment: -10" in payload["response"]


def test_empty_unparseable_critique_is_not_attached_to_response(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    calls: list[dict] = []
    patch_ollama_model(monkeypatch, _generate_stub("", calls))
    client = TestClient(create_app(_config(tmp_path)))

    response = client.post(
        "/runtime/respond",
        json={"message": "Should we prioritize evidence intake next?", "use_semantic_memory": False},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["response"] == "Prioritize evidence intake."
    assert payload["contrarian"]["verdict"] == "unparsed"
    assert "Contrarian check" not in payload["response"]
    assert "contrarian_pass_applied" not in payload["governor"]["applied_rules"]
    assert "Contrarian pass returned no parseable critique; no visible challenge attached." in payload["governor"]["notes"]


def test_malformed_json_critique_fragment_is_not_attached_to_response(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    calls: list[dict] = []
    patch_ollama_model(
        monkeypatch,
        _generate_stub('```json\n{"verdict": "proceed_with_changes", "weakest_assumption": "cut off', calls),
    )
    client = TestClient(create_app(_config(tmp_path)))

    response = client.post(
        "/runtime/respond",
        json={"message": "Should we prioritize evidence intake next?", "use_semantic_memory": False},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["response"] == "Prioritize evidence intake."
    assert payload["contrarian"]["verdict"] == "unparsed"
    assert "Contrarian check" not in payload["response"]
    assert "```json" not in payload["response"]
    assert "contrarian_pass_applied" not in payload["governor"]["applied_rules"]
    assert "Contrarian pass returned no parseable critique; no visible challenge attached." in payload["governor"]["notes"]


def test_parse_critique_normalizes_and_clamps() -> None:
    parsed = _parse_critique(
        '<think>reasoning...</think> {"verdict": "Do Not Proceed", "weakest_assumption": "a", '
        '"missing_evidence": "b", "downside_risk": "c", "confidence_adjustment": -400}'
    )
    assert parsed["verdict"] == "do_not_proceed"
    assert parsed["confidence_adjustment"] == -50

    unknown = _parse_critique('{"verdict": "maybe", "confidence_adjustment": "not a number"}')
    assert unknown["verdict"] == "proceed_with_changes"
    assert unknown["confidence_adjustment"] == -10

    # Heuristic sanity: recommendation-shaped phrases are covered.
    assert "should we" in RECOMMENDATION_SIGNALS
    assert "recommend" in RECOMMENDATION_SIGNALS
