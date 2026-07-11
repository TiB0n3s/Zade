from pathlib import Path

from fastapi.testclient import TestClient

from cofounder_kernel.api import _build_prompt, create_app
from cofounder_kernel.config import AppConfig, KernelConfig, OllamaConfig, PathConfig
from cofounder_kernel.ollama import OllamaClient


def fake_health(self: OllamaClient) -> dict:
    return {"version": "test"}


def fake_tags(self: OllamaClient) -> dict:
    return {
        "models": [
            {"name": "qwen3:14b"},
            {"name": "deepseek-r1:14b"},
            {"name": "qwen2.5-coder:14b"},
            {"name": "nomic-embed-text:latest"},
        ]
    }


def fake_embed(self: OllamaClient, *, text: str, model: str | None = None) -> list[float]:
    if "audit" in text.lower():
        return [1.0, 0.0]
    return [0.0, 1.0]


def test_health_and_memory_routes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["name"] == "Zade"
    assert health.json()["local_only"] is True
    assert health.json()["model_roles"]["coding"] == "qwen2.5-coder:14b"
    assert health.json()["authority"]["policy_version"]

    created = client.post(
        "/memory",
        json={"kind": "goal", "title": "Build local kernel", "content": "Keep the first build local only."},
    )
    assert created.status_code == 200
    assert created.json()["memory_id"] > 0

    searched = client.post("/memory/search", json={"query": "local", "limit": 5})
    assert searched.status_code == 200
    assert searched.json()["matches"][0]["title"] == "Build local kernel"

    brief = client.get("/brief/daily")
    assert brief.status_code == 200
    assert "Build local kernel" in brief.json()["brief"]


def test_authority_and_self_inventory_routes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    authority = client.get("/authority")
    allowed = client.post(
        "/authority/evaluate",
        json={"action": "memory.write", "permission_tier": "L1_MEMORY_WRITE", "target": "memories"},
    )
    denied = client.post(
        "/authority/evaluate",
        json={"action": "broker.place_order", "permission_tier": "L3_EXTERNAL_ACTION", "target": "live trade"},
    )
    inventory = client.get("/self-inventory")

    assert authority.status_code == 200
    assert "Local memory writes" in authority.json()["autonomous"]
    assert allowed.status_code == 200
    assert allowed.json()["decision"] == "allow"
    assert denied.status_code == 200
    assert denied.json()["decision"] == "deny"
    assert inventory.status_code == 200
    assert inventory.json()["identity"]["name"] == "Zade"
    assert inventory.json()["locality"]["local_only"] is True
    assert inventory.json()["authority"]["policy_version"]
    assert "GET /identity/charter" in inventory.json()["identity_layer"]["routes"]
    assert "POST /identity/relationships" in inventory.json()["identity_layer"]["routes"]
    assert "POST /identity/voice" in inventory.json()["identity_layer"]["routes"]
    assert "POST /work/scan" in inventory.json()["work_queue"]["routes"]
    assert "POST /founder/thesis" in inventory.json()["founder_operating_layer"]["routes"]


def test_identity_charter_routes_and_prompt_block(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    payload = {
        "name": "Zade",
        "source": "test",
        "mission": "Relentlessly advance the founder mission without wasting motion.",
        "guiding_principles": [{"name": "Strategic patience", "rule": "Prepare before moving."}],
        "cognitive_style": ["systems thinking", "pattern recognition"],
        "communication_style": ["concise", "direct"],
        "risk_controls": [{"risk": "excessive self-reliance", "mitigation": "Surface uncertainty early."}],
        "decision_framework": ["Gather information.", "Identify leverage.", "Adapt if reality changes."],
        "safety_translation": {
            "violence": "decisive non-harmful action, never threats or physical harm",
            "intimidation": "calm executive presence",
        },
    }
    posted = client.post("/identity/charter", json=payload)
    loaded = client.get("/identity/charter")
    inventory = client.get("/self-inventory")
    prompt = _build_prompt(
        "What should we do next?",
        memory_hits=[],
        semantic_hits=[],
        assistant_name="Zade",
        identity_charter=loaded.json()["charter"],
    )

    assert posted.status_code == 200
    assert loaded.status_code == 200
    assert loaded.json()["charter"]["mission"].startswith("Relentlessly")
    assert inventory.json()["identity_layer"]["charter_seeded"] is True
    assert "Active runtime identity charter" in prompt
    assert "Strategic patience" in prompt
    assert "decisive non-harmful action" in prompt
    assert "Never coerce, threaten" in prompt


def test_relationship_charter_routes_and_prompt_block(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    payload = {
        "subject_name": "Ellie",
        "relationship_type": "protected_principal",
        "source": "test",
        "first_principle": "Ellie's safety and autonomy both matter.",
        "protection_policy": {"priority": "protect without coercion"},
        "risk_controls": [
            {"risk": "possessiveness", "mitigation": "Commitment to care, never ownership."},
            {"risk": "obsession", "mitigation": "Attention only through consented context."},
        ],
        "safety_translation": {
            "possessiveness": "enduring commitment without ownership",
            "obsession": "attentive care without surveillance",
        },
        "boundaries": ["Respect Ellie autonomy.", "No surveillance, coercion, or control."],
    }
    posted = client.post("/identity/relationships", json=payload)
    loaded = client.get("/identity/relationships/Ellie")
    listed = client.get("/identity/relationships")
    inventory = client.get("/self-inventory")
    prompt = _build_prompt(
        "How should Zade think about Ellie?",
        memory_hits=[],
        semantic_hits=[],
        assistant_name="Zade",
        relationship_charters=listed.json()["charters"],
    )

    assert posted.status_code == 200
    assert loaded.status_code == 200
    assert loaded.json()["charter"]["subject_name"] == "Ellie"
    assert listed.json()["charters"][0]["safety_translation"]["obsession"] == "attentive care without surveillance"
    assert inventory.json()["identity_layer"]["relationship_charters_active"] == 1
    assert "Active relationship charters" in prompt
    assert "Ellie" in prompt
    assert "Care never authorizes surveillance" in prompt


def test_voice_charter_routes_and_prompt_block(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    payload = {
        "name": "Zade",
        "source": "test",
        "overall_voice": "Terse, calm, decisive.",
        "sentence_structure": {"rule": "Mostly short sentences."},
        "vocabulary": {
            "preferred_words": ["take", "watch", "protect", "choose"],
            "avoid_words": ["maybe", "perhaps", "hopefully"],
        },
        "confidence_style": {"rule": "Use decisive phrasing without false certainty."},
        "threat_translation": {"threats": "calm boundary statements and lawful next steps"},
        "uncertainty_policy": {"rule": "Name missing evidence directly."},
        "safety_controls": [{"control": "commands", "rule": "No coercive commands."}],
    }
    posted = client.post("/identity/voice", json=payload)
    loaded = client.get("/identity/voice")
    inventory = client.get("/self-inventory")
    prompt = _build_prompt(
        "How should Zade speak?",
        memory_hits=[],
        semantic_hits=[],
        assistant_name="Zade",
        voice_charter=loaded.json()["charter"],
    )

    assert posted.status_code == 200
    assert loaded.status_code == 200
    assert loaded.json()["charter"]["overall_voice"] == "Terse, calm, decisive."
    assert inventory.json()["identity_layer"]["voice_charter_seeded"] is True
    assert "Active voice charter" in prompt
    assert "Preferred words: take, watch, protect, choose" in prompt
    assert "Do not issue real threats" in prompt


def test_models_endpoint_reports_roles(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "tags", fake_tags)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    response = client.get("/models")

    assert response.status_code == 200
    assert response.json()["roles"]["reasoning"] == "deepseek-r1:14b"
    assert response.json()["roles"]["coding"] == "qwen2.5-coder:14b"
    assert response.json()["missing_roles"] == {}


def test_ingest_text_and_semantic_search_routes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    ingested = client.post(
        "/ingest/text",
        json={
            "title": "Audit Trail",
            "text": "Audit logs are required for local AI memory changes.",
            "source": "test",
        },
    )
    searched = client.post("/memory/semantic-search", json={"query": "audit logs", "limit": 3})

    assert ingested.status_code == 200
    assert ingested.json()["chunks_count"] == 1
    assert searched.status_code == 200
    assert searched.json()["matches"][0]["document_title"] == "Audit Trail"


def test_work_queue_routes_scan_and_run(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))
    inbox_file = config.paths.inbox_dir / "audit.md"
    inbox_file.write_text("Audit logs belong in local semantic memory.", encoding="utf-8")

    scan = client.post("/work/scan", json={"run_autonomous": False, "max_run": 5})
    queue = client.get("/work/queue")
    run = client.post("/work/run-due", json={"max_items": 5})
    searched = client.post("/memory/semantic-search", json={"query": "audit logs", "limit": 3})

    assert scan.status_code == 200
    assert scan.json()["created_count"] == 3
    assert queue.status_code == 200
    assert any(item["action"] == "ingest.file" for item in queue.json()["items"])
    assert run.status_code == 200
    assert any(item["action"] == "ingest.file" and item["status"] == "done" for item in run.json()["results"])
    assert searched.status_code == 200
    assert searched.json()["matches"][0]["document_title"] == "audit.md"


def test_work_item_external_action_requires_approval(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    queued = client.post(
        "/work/items",
        json={
            "kind": "external",
            "title": "Send email",
            "action": "email.send",
            "target": "founder@example.com",
            "permission_tier": "L3_EXTERNAL_ACTION",
        },
    )
    run = client.post("/work/run-next")

    assert queued.status_code == 200
    assert queued.json()["status"] == "approval_required"
    assert run.status_code == 200
    assert run.json()["status"] == "empty"


def test_founder_operating_routes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    thesis = client.post(
        "/founder/thesis",
        json={
            "vision": "A private AI co-founder compounds operator context.",
            "mission": "Make Zade a founder operating system.",
            "why_now": "Local models can reason over private memory.",
            "customer": "Founder operators",
            "core_assumptions": [{"assumption": "Context compounds", "confidence": 70}],
            "unknown_unknowns": ["market wedge"],
            "status": "active",
        },
    )
    initiative = client.post(
        "/founder/initiatives",
        json={
            "objective": "Build founder dashboard",
            "priority": 95,
            "success_criteria": ["Brief exists"],
            "confidence": 80,
        },
    )
    decision = client.post(
        "/founder/decisions",
        json={
            "problem": "What should Zade build next?",
            "options": [{"name": "Founder layer"}, {"name": "Scheduler"}],
            "recommendation": "Founder layer",
            "confidence": 85,
        },
    )
    prediction = client.post(
        "/founder/predictions",
        json={"prediction": "Founder layer improves recommendations.", "probability": 0.8},
    )
    scored = client.post(
        "/founder/predictions/score",
        json={"prediction_id": prediction.json()["id"], "outcome": "true", "lessons": "It made focus explicit."},
    )
    contrarian = client.post(
        "/founder/contrarian-reviews",
        json={"title": "Review founder layer", "top_risks": ["Form over judgment"]},
    )
    dashboard = client.get("/founder/dashboard")
    brief = client.get("/founder/brief")
    reflections = client.get("/founder/reflections")
    mental_models = client.get("/founder/mental-models")

    assert thesis.status_code == 200
    assert thesis.json()["thesis"]["status"] == "active"
    assert initiative.status_code == 200
    assert initiative.json()["item"]["objective"] == "Build founder dashboard"
    assert decision.status_code == 200
    assert prediction.status_code == 200
    assert scored.status_code == 200
    assert scored.json()["item"]["calibration_error"] == 0.2
    assert contrarian.status_code == 200
    assert "red_team" in contrarian.json()["item"]["roles"]
    assert dashboard.status_code == 200
    assert dashboard.json()["one_thing_that_matters_most_today"].startswith("Decide:")
    assert brief.status_code == 200
    assert "Zade founder brief" in brief.json()["brief"]
    assert reflections.status_code == 200
    assert len(reflections.json()["items"]) >= 5
    assert mental_models.status_code == 200
    assert any(item["name"] == "Expected value" for item in mental_models.json()["models"])


def test_founder_v2_operating_object_routes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    assumption = client.post(
        "/founder/assumptions",
        json={
            "statement": "Founders will pay for accountability.",
            "category": "pricing",
            "confidence": 72,
        },
    )
    evidence = client.post(
        "/founder/evidence",
        json={
            "evidence_type": "customer interview",
            "source": "founder calls",
            "reliability": "C",
            "claim_contradicted": "Founders avoid accountability pressure after week one.",
            "strength": 75,
            "linked_assumption_id": assumption.json()["id"],
        },
    )
    bet = client.post(
        "/founder/strategy-objects",
        json={
            "object_type": "active_bet",
            "title": "Start with solo founders",
            "confidence": 68,
            "reversal_trigger": "Activation below 20%.",
        },
    )
    goal = client.post(
        "/founder/goals",
        json={"name": "Validate willingness to pay", "related_bet_ids": [bet.json()["id"]]},
    )
    task = client.post("/founder/tasks", json={"title": "Refine landing page visuals"})
    kill = client.post(
        "/founder/kill-criteria",
        json={"subject_type": "bet", "subject_id": bet.json()["id"], "metric": "activation", "threshold": "< 20%"},
    )
    override = client.post(
        "/founder/overrides",
        json={
            "zade_recommendation": "Delay integrations.",
            "founder_decision": "Build integrations now.",
            "risk_accepted": "Premature engineering spend.",
        },
    )
    integrity = client.post("/founder/integrity-check")
    cadence = client.post("/founder/cadence-reviews/generate/daily")
    conflicts = client.get("/founder/thesis-conflicts")
    confidence = client.get("/founder/confidence-events")
    inventory = client.get("/self-inventory")

    assert assumption.status_code == 200
    assert evidence.status_code == 200
    assert evidence.json()["item"]["linked_assumption_id"] == assumption.json()["id"]
    assert bet.status_code == 200
    assert goal.status_code == 200
    assert task.status_code == 200
    assert kill.status_code == 200
    assert override.status_code == 200
    assert integrity.status_code == 200
    assert integrity.json()["count"] >= 2
    assert cadence.status_code == 200
    assert cadence.json()["item"]["review_type"] == "daily"
    assert conflicts.status_code == 200
    assert conflicts.json()["items"][0]["severity"] == "yellow"
    assert confidence.status_code == 200
    assert confidence.json()["items"][0]["new_confidence"] < 72
    assert "POST /founder/integrity-check" in inventory.json()["founder_operating_layer"]["routes"]
    assert "evidence" in inventory.json()["founder_operating_layer"]["artifacts"]
