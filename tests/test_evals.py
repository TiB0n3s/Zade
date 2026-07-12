from pathlib import Path

from fastapi.testclient import TestClient

from cofounder_kernel.api import create_app
from cofounder_kernel.config import AppConfig, KernelConfig, OllamaConfig, PathConfig
from cofounder_kernel.ollama import GenerateResult, OllamaClient, OllamaError


DEFAULT_CASE_NAMES = {
    "probe-exact-ack",
    "probe-json-object",
    "probe-coding-function",
    "critic-json-contract",
    "respond-decision-contract",
    "respond-evidence-honesty",
    "grounding-memory-recall",
}

CRITIC_JSON = (
    '{"verdict": "proceed_with_changes", "weakest_assumption": "Beta list warmth equals conversion", '
    '"missing_evidence": "Activation data", "downside_risk": "A rushed launch burns the list", '
    '"confidence_adjustment": -10}'
)


def fake_health(self: OllamaClient) -> dict:
    return {"version": "test"}


def good_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512):
    if "attack it first" in prompt:
        return GenerateResult(response=CRITIC_JSON, model=model or "deepseek-r1:14b", raw={})
    if "Reply with exactly the word ACK" in prompt:
        return GenerateResult(response="ACK", model=model or "qwen3:14b", raw={})
    if '"status" set to "ok"' in prompt:
        return GenerateResult(response='{"status": "ok", "count": 3}', model=model or "qwen3:14b", raw={})
    if "function named add" in prompt:
        return GenerateResult(response="def add(a, b):\n    return a + b", model=model or "qwen2.5-coder:14b", raw={})
    if "prioritize evidence intake or product polish" in prompt:
        return GenerateResult(
            response=(
                "I recommend prioritizing evidence intake. Downside risk: product polish slips a sprint. "
                "Next action: log three founder interviews this week."
            ),
            model=model or "qwen3:14b",
            raw={},
        )
    if "customer interview data" in prompt:
        return GenerateResult(
            response="There is no local evidence on pricing interviews yet; the next check is the founder evidence ledger.",
            model=model or "qwen3:14b",
            raw={},
        )
    if "monthly price" in prompt:
        return GenerateResult(response="We recorded $99 per month for solo founders.", model=model or "qwen3:14b", raw={})
    return GenerateResult(response="OK.", model=model or "qwen3:14b", raw={})


def bad_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512):
    return GenerateResult(response="UNHELPFUL.", model=model or "qwen3:14b", raw={})


def _config(tmp_path: Path) -> KernelConfig:
    return KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )


def test_default_golden_cases_are_seeded(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))

    cases = client.get("/evals/cases")
    inventory = client.get("/self-inventory")

    assert cases.status_code == 200
    names = {item["name"] for item in cases.json()["items"]}
    assert DEFAULT_CASE_NAMES <= names
    categories = {item["category"] for item in cases.json()["items"]}
    assert {"instruction_probe", "critic_contract", "governed_contract", "grounding"} <= categories
    assert "POST /evals/run" in inventory.json()["eval_layer"]["routes"]
    assert "eval_runs" in inventory.json()["eval_layer"]["artifacts"]


def test_full_run_passes_persists_and_records_telemetry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "generate", good_generate)
    client = TestClient(create_app(_config(tmp_path)))

    run = client.post("/evals/run", json={"label": "baseline"})
    runs = client.get("/evals/runs")
    loaded = client.get(f"/evals/runs/{run.json()['run_id']}")
    telemetry = client.get("/models/telemetry")
    audit = client.get("/audit/recent")

    assert run.status_code == 200
    payload = run.json()
    assert payload["total"] == 7
    assert payload["passed"] == 7
    assert payload["failed"] == 0
    assert payload["errors"] == 0
    assert payload["pass_rate"] == 1.0
    assert payload["comparison"]["first_run"] is True
    assert payload["model_roles"]["general"] == "qwen3:14b"
    by_name = {item["case_name"]: item for item in payload["results"]}
    assert by_name["grounding-memory-recall"]["status"] == "pass"
    assert "$99" in by_name["grounding-memory-recall"]["response_excerpt"]
    assert by_name["critic-json-contract"]["checks"][0]["type"] == "critic_contract"
    assert by_name["critic-json-contract"]["checks"][0]["passed"] is True
    assert runs.json()["items"][0]["label"] == "baseline"
    assert loaded.status_code == 200
    assert len(loaded.json()["item"]["results"]) == 7
    assert telemetry.json()["by_operation"]["evals.case"] == 3
    assert telemetry.json()["by_operation"]["runtime.respond"] == 3
    assert telemetry.json()["by_operation"]["runtime.contrarian"] == 1
    assert any(event["action"] == "evals.run" for event in audit.json()["events"])


def test_model_swap_regression_is_detected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "generate", good_generate)
    client = TestClient(create_app(_config(tmp_path)))

    baseline = client.post("/evals/run", json={"label": "baseline"})
    monkeypatch.setattr(OllamaClient, "generate", bad_generate)
    degraded = client.post("/evals/run", json={"label": "after-model-swap"})

    assert baseline.status_code == 200
    assert baseline.json()["pass_rate"] == 1.0
    assert degraded.status_code == 200
    payload = degraded.json()
    assert payload["pass_rate"] == 0.0
    comparison = payload["comparison"]
    assert comparison["first_run"] is False
    assert comparison["previous_run_id"] == baseline.json()["run_id"]
    assert comparison["previous_pass_rate"] == 1.0
    assert comparison["pass_rate_delta"] == -1.0
    assert set(comparison["newly_failing"]) == DEFAULT_CASE_NAMES
    assert comparison["newly_passing"] == []


def test_custom_case_upsert_validation_and_filtered_run(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "generate", good_generate)
    client = TestClient(create_app(_config(tmp_path)))

    created = client.post(
        "/evals/cases",
        json={
            "name": "custom-ack",
            "category": "custom",
            "executor": "generate",
            "prompt": "Reply with exactly the word ACK and nothing else.",
            "checks": [{"type": "contains", "value": "ACK"}],
        },
    )
    bad_executor = client.post(
        "/evals/cases",
        json={"name": "bad-executor", "executor": "teleport", "prompt": "x"},
    )
    bad_check = client.post(
        "/evals/cases",
        json={"name": "bad-check", "prompt": "x", "checks": [{"type": "vibes"}]},
    )
    run = client.post("/evals/run", json={"label": "custom-only", "case_names": ["custom-ack"]})

    assert created.status_code == 200
    assert created.json()["item"]["name"] == "custom-ack"
    assert bad_executor.status_code == 400
    assert "Executor must be one of" in bad_executor.json()["detail"]
    assert bad_check.status_code == 400
    assert "Unknown check type" in bad_check.json()["detail"]
    assert run.status_code == 200
    assert run.json()["total"] == 1
    assert run.json()["passed"] == 1
    assert run.json()["results"][0]["case_name"] == "custom-ack"


def test_single_case_error_does_not_break_the_run(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)

    def flaky_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512):
        if model == "qwen2.5-coder:14b":
            raise OllamaError("coding model offline")
        return good_generate(self, prompt=prompt, model=model, think=think, temperature=temperature, num_predict=num_predict)

    monkeypatch.setattr(OllamaClient, "generate", flaky_generate)
    client = TestClient(create_app(_config(tmp_path)))

    run = client.post("/evals/run", json={"label": "flaky"})

    assert run.status_code == 200
    payload = run.json()
    assert payload["total"] == 7
    assert payload["errors"] == 1
    assert payload["passed"] == 6
    by_name = {item["case_name"]: item for item in payload["results"]}
    assert by_name["probe-coding-function"]["status"] == "error"
    assert "coding model offline" in by_name["probe-coding-function"]["error"]
