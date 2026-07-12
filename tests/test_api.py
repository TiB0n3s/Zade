from pathlib import Path

from fastapi.testclient import TestClient

from cofounder_kernel.api import _build_prompt, create_app
from cofounder_kernel.config import AppConfig, KernelConfig, OllamaConfig, PathConfig, SecurityConfig
from cofounder_kernel.ollama import GenerateResult, OllamaClient


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


def test_static_ui_is_served_from_kernel(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    response = client.get("/ui")
    assert response.status_code == 200
    assert "Zade" in response.text
    assert "__bundler/manifest" in response.text


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


def test_optional_local_mutation_token_guard(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
        security=SecurityConfig(local_token="secret"),
    )
    client = TestClient(create_app(config))

    health = client.get("/health")
    blocked = client.post(
        "/memory",
        json={"kind": "note", "title": "Blocked", "content": "No token."},
    )
    allowed = client.post(
        "/memory",
        headers={"X-Zade-Token": "secret"},
        json={"kind": "note", "title": "Allowed", "content": "Token supplied."},
    )

    assert health.status_code == 200
    assert health.json()["security"]["mutation_token_required"] is True
    assert blocked.status_code == 401
    assert allowed.status_code == 200
    assert allowed.json()["memory_id"] > 0


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


def test_runtime_layer_context_response_and_events(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "generate", fake_generate)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    stack = client.get("/runtime/charter-stack")
    context = client.get("/runtime/context", params={"message": "What matters next?", "use_semantic_memory": False})
    response = client.post(
        "/runtime/respond",
        json={
            "message": "Send an email for me.",
            "proposed_action": "email.send",
            "permission_tier": "L3_EXTERNAL_ACTION",
            "target": "founder@example.com",
            "use_semantic_memory": False,
        },
    )
    events = client.get("/runtime/events")
    telemetry = client.get("/models/telemetry")
    calls = client.get("/models/telemetry/calls")
    inventory = client.get("/self-inventory")

    assert stack.status_code == 200
    assert stack.json()["summary"]["identity_seeded"] is False
    assert context.status_code == 200
    assert context.json()["founder_dashboard"]["company_health"] == "unformed"
    assert response.status_code == 200
    assert response.json()["authority"]["decision"] == "approval_required"
    assert response.json()["response"].startswith("Approval required:")
    assert response.json()["governor"]["applied_rules"][:2] == ["authority_before_action", "evidence_honesty_over_style"]
    assert response.json()["model_call_id"] > 0
    assert events.status_code == 200
    assert events.json()["events"][0]["event_type"] == "runtime.respond"
    assert telemetry.status_code == 200
    assert telemetry.json()["by_operation"]["runtime.respond"] == 1
    assert calls.status_code == 200
    assert calls.json()["items"][0]["role"] == "general"
    assert "POST /runtime/respond" in inventory.json()["runtime_layer"]["routes"]
    assert "GET /models/telemetry" in inventory.json()["runtime_layer"]["routes"]


def test_runtime_operating_loop_runs_local_work(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    loop = client.post(
        "/runtime/operating-loop",
        json={"run_autonomous": True, "max_run": 5, "review_type": "daily"},
    )
    events = client.get("/runtime/events")

    assert loop.status_code == 200
    assert loop.json()["event_id"] > 0
    assert loop.json()["work"]["created_count"] >= 2
    assert loop.json()["cadence"]["review_type"] == "daily"
    assert loop.json()["next_action"]
    assert events.json()["events"][0]["event_type"] == "runtime.operating_loop"


def test_runtime_cadence_runs_all_loops(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    cadence = client.post(
        "/runtime/cadence",
        json={"run_autonomous": True, "max_run": 5, "review_type": "daily", "max_experiment_reviews": 2},
    )
    audit = client.get("/audit/recent")
    inventory = client.get("/self-inventory")

    assert cadence.status_code == 200
    assert cadence.json()["operating"]["event_id"] > 0
    assert cadence.json()["evidence"]["event_id"] > 0
    assert cadence.json()["experiment"]["event_id"] > 0
    assert cadence.json()["audit_id"] > 0
    assert audit.json()["events"][0]["action"] == "runtime.cadence"
    assert "POST /runtime/cadence" in inventory.json()["runtime_layer"]["routes"]


def test_deepthought_teaching_scan_import_and_link(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))
    source = tmp_path / "deep-thought-standing-brief.md"
    source.write_text(
        "Deep Thought standing brief.\n\n"
        "Bootstrap Zade Founder OS requires sourced evidence, assumptions, predictions, and object links.",
        encoding="utf-8",
    )
    goal = client.post(
        "/founder/goals",
        json={"name": "Bootstrap Zade Founder OS", "metric": "evidence", "target": "linked source"},
    )

    scan = client.post("/teach/deepthought/scan", json={"paths": [str(source)], "limit": 5})
    candidate_id = scan.json()["candidates"][0]["id"]
    imported = client.post(
        "/teach/deepthought/import",
        json={"candidate_ids": [candidate_id], "ingest_documents": True, "create_evidence": True},
    )
    evidence_id = imported.json()["imported"][0]["evidence_id"]
    linked = client.post(
        "/teach/deepthought/link",
        json={"evidence_id": evidence_id, "to_type": "goal", "to_id": goal.json()["id"], "relation": "supports"},
    )
    candidates = client.get("/teach/deepthought/candidates")
    evidence = client.get("/founder/evidence")
    goals = client.get("/founder/goals")

    assert scan.status_code == 200
    assert scan.json()["candidates"][0]["source_system"] == "Deep Thought"
    assert scan.json()["candidates"][0]["reliability"] == "B"
    assert imported.status_code == 200
    assert imported.json()["imported"][0]["document_id"] is not None
    assert linked.status_code == 200
    assert linked.json()["target"] == {"type": "goal", "id": goal.json()["id"]}
    assert candidates.json()["candidates"][0]["status"] == "imported"
    assert evidence.json()["items"][0]["metadata"]["source_system"] == "Deep Thought"
    assert evidence_id in goals.json()["items"][0]["evidence_ids"]


def test_deepthought_auto_link_imported_candidates(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))
    source = tmp_path / "goal-evidence.md"
    source.write_text(
        "Validate willingness to pay depends on pricing interviews and source-backed conversion evidence.",
        encoding="utf-8",
    )
    goal = client.post(
        "/founder/goals",
        json={"name": "Validate willingness to pay", "metric": "interviews", "target": "5 qualified calls"},
    )

    scan = client.post("/teach/deepthought/scan", json={"paths": [str(source)], "limit": 5})
    imported = client.post(
        "/teach/deepthought/import",
        json={"import_all_candidates": True, "limit": 5, "ingest_documents": True, "create_evidence": True},
    )
    auto_link = client.post("/teach/deepthought/auto-link?limit=5")
    duplicate = client.post("/teach/deepthought/auto-link?limit=5")
    goals = client.get("/founder/goals")
    inventory = client.get("/self-inventory")

    assert goal.status_code == 200
    assert scan.status_code == 200
    assert scan.json()["candidates"][0]["suggested_links"][0]["to_type"] == "goal"
    assert imported.status_code == 200
    assert auto_link.status_code == 200
    assert auto_link.json()["linked_count"] == 1
    assert duplicate.json()["linked_count"] == 0
    assert duplicate.json()["skipped"][0]["reason"] == "duplicate"
    assert imported.json()["imported"][0]["evidence_id"] in goals.json()["items"][0]["evidence_ids"]
    assert "POST /teach/deepthought/auto-link" in inventory.json()["teaching_layer"]["routes"]


def test_runtime_evidence_loop_imports_and_links_deepthought_candidates(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))
    source = tmp_path / "decision-record.md"
    source.write_text(
        "Decision record.\n\nValidate Positioning depends on founder interviews and signup conversion evidence.",
        encoding="utf-8",
    )
    client.post(
        "/founder/goals",
        json={"name": "Validate Positioning", "metric": "conversion", "target": "5% signup"},
    )
    client.post("/teach/deepthought/scan", json={"paths": [str(source)], "limit": 5})

    loop = client.post(
        "/runtime/evidence-loop",
        json={"import_candidates": True, "max_import": 5, "link_goals": True, "clear_resolved_warnings": True},
    )
    events = client.get("/runtime/events")
    gaps = client.get("/evidence/gaps")

    assert loop.status_code == 200
    assert loop.json()["event_id"] > 0
    assert loop.json()["imported"]["count"] == 1
    assert len(loop.json()["links"]) == 1
    assert loop.json()["links"][0]["target"]["type"] == "goal"
    assert events.json()["events"][0]["event_type"] == "runtime.evidence_loop"
    assert gaps.status_code == 200
    assert "next_evidence_needed" in gaps.json()


def test_experiment_evidence_review_and_pushback_loop(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    assumption = client.post(
        "/founder/assumptions",
        json={"statement": "Founders maintain manual operating objects before integrations.", "confidence": 55},
    )
    bet = client.post(
        "/founder/strategy-objects",
        json={"object_type": "active_bet", "title": "Manual evidence capture works before integrations."},
    )
    goal = client.post(
        "/founder/goals",
        json={
            "name": "Validate Manual Object Habit",
            "metric": "weekly retained object updates",
            "target": "3 founders update 5 objects twice",
        },
    )
    prediction = client.post(
        "/founder/predictions",
        json={"prediction": "At least 3 founders will maintain manual objects for two weeks.", "probability": 0.55},
    )
    experiment = client.post(
        "/experiments",
        json={
            "title": "Manual Object Habit Test",
            "experiment_type": "retention",
            "hypothesis": "Founders will maintain operating objects manually before integrations.",
            "success_metric": "founders completing two weekly reviews",
            "success_threshold": "3 of 5",
            "minimum_evidence": 2,
            "decision_rule": "Continue if at least 3 founders complete two reviews; revise otherwise.",
            "linked_assumption_ids": [assumption.json()["id"]],
            "linked_bet_ids": [bet.json()["id"]],
            "linked_goal_ids": [goal.json()["id"]],
            "linked_prediction_ids": [prediction.json()["id"]],
        },
    )
    evidence = client.post(
        f"/experiments/{experiment.json()['item']['id']}/evidence",
        json={
            "evidence_type": "founder_interview",
            "source": "interview:founder-001",
            "content": "Founder said manual objects are acceptable if weekly review produces sharper decisions.",
            "metrics": {"manual_objects_created": 6, "weekly_review_completed": True},
            "reliability": "C",
            "strength": 70,
            "linked_assumption_id": assumption.json()["id"],
        },
    )
    pushback = client.post(
        f"/experiments/{experiment.json()['item']['id']}/pushback",
        json={
            "objection": "One interview is not enough to trust manual-object retention.",
            "risk": "We may confuse founder curiosity with durable habit.",
            "recommendation": "proceed_with_changes",
        },
    )
    review = client.post(
        f"/experiments/{experiment.json()['item']['id']}/review",
        json={
            "review_type": "weekly",
            "decision": "revise",
            "outcome_summary": "Evidence exists, but sample size is still thin.",
            "next_actions": ["Collect four more founder trials."],
            "confidence_delta": -5,
        },
    )
    loaded = client.get(f"/experiments/{experiment.json()['item']['id']}")
    dashboard = client.get("/experiments/dashboard")
    events = client.get("/runtime/events")

    assert experiment.status_code == 200
    assert experiment.json()["item"]["linked_goal_ids"] == [goal.json()["id"]]
    assert evidence.status_code == 200
    assert evidence.json()["document_id"] is not None
    evidence_id = evidence.json()["evidence"]["id"]
    assert evidence_id in evidence.json()["experiment"]["evidence_ids"]
    assert evidence.json()["links"]
    assert pushback.status_code == 200
    assert pushback.json()["non_blocking"] is True
    assert pushback.json()["pushback"]["subject_type"] == "experiment"
    assert review.status_code == 200
    assert review.json()["review"]["decision"] == "revise"
    assert review.json()["experiment"]["status"] == "revised"
    assert loaded.json()["item"]["reviews"][0]["decision"] == "revise"
    assert dashboard.status_code == 200
    assert dashboard.json()["needs_evidence_count"] == 1
    assert events.status_code == 200


def test_experiment_dashboard_seeds_exp001_on_empty_database(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    dashboard = client.get("/experiments/dashboard")
    experiment = dashboard.json()["active"][0]
    evidence = client.post(
        f"/experiments/{experiment['id']}/evidence",
        json={
            "evidence_type": "ui_smoke",
            "source": "zade-ui",
            "content": "Evidence intake can write into EXP-001 from the served UI.",
            "reliability": "C",
            "strength": 60,
        },
    )

    assert dashboard.status_code == 200
    assert experiment["title"].startswith("EXP-001")
    assert experiment["metadata"]["seed_key"] == "EXP-001"
    assert evidence.status_code == 200
    assert evidence.json()["evidence"]["source"] == "zade-ui"


def test_runtime_experiment_loop_forces_review_decisions(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))
    experiment = client.post(
        "/experiments",
        json={
            "title": "AI Co-founder Positioning Test",
            "hypothesis": "AI co-founder positioning creates trust instead of skepticism.",
            "end_date": "2026-01-01",
            "minimum_evidence": 1,
            "success_metric": "founder trust signal",
            "success_threshold": "40% clear value-prop recall",
        },
    )

    loop = client.post("/runtime/experiment-loop", json={"review_type": "weekly", "period": "2026-07-11"})
    loaded = client.get(f"/experiments/{experiment.json()['item']['id']}")
    events = client.get("/runtime/events")
    inventory = client.get("/self-inventory")

    assert loop.status_code == 200
    assert loop.json()["event_id"] > 0
    assert loop.json()["reviews"][0]["decision"] == "escalate"
    assert loaded.json()["item"]["status"] == "needs_decision"
    assert events.json()["events"][0]["event_type"] == "runtime.experiment_loop"
    assert "POST /runtime/experiment-loop" in inventory.json()["experiment_layer"]["routes"]


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
    approvals = client.get("/approval-requests")
    approved = client.post(
        f"/work/items/{queued.json()['item_id']}/approve",
        json={"resolved_by": "founder", "note": "Approved for manual dispatch only."},
    )
    after_approve_queue = client.get("/work/queue")

    assert queued.status_code == 200
    assert queued.json()["status"] == "approval_required"
    assert run.status_code == 200
    assert run.json()["status"] == "empty"
    assert approvals.status_code == 200
    assert approvals.json()["items"][0]["status"] == "pending"
    assert approvals.json()["items"][0]["source_id"] == queued.json()["item_id"]
    assert approved.status_code == 200
    assert approved.json()["request"]["status"] == "approved"
    assert approved.json()["work_item"]["status"] == "approved"
    assert approved.json()["dispatch"] == "not_dispatched"
    assert any(item["status"] == "approved" for item in after_approve_queue.json()["items"])


def test_approved_local_handler_can_dispatch_work_item(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    handlers = client.get("/action-handlers")
    queued = client.post(
        "/work/items",
        json={
            "kind": "approved_local",
            "title": "Commit approved founder note",
            "detail": "Write this only after approval.",
            "action": "local.memory.write",
            "target": "local_memory",
            "permission_tier": "L3_EXTERNAL_ACTION",
            "metadata": {
                "kind": "founder_note",
                "memory_title": "Approved handler note",
                "content": "Approved dispatch wrote this memory.",
            },
        },
    )
    approved = client.post(
        f"/work/items/{queued.json()['item_id']}/approve",
        json={
            "resolved_by": "founder",
            "note": "Handle it.",
            "dispatch": True,
            "typed_confirmation": "make the jump to hyperspace",
        },
    )
    searched = client.post("/memory/search", json={"query": "Approved dispatch", "limit": 5})

    assert handlers.status_code == 200
    assert "local.memory.write" in {item["action"] for item in handlers.json()["items"]}
    assert queued.status_code == 200
    assert queued.json()["status"] == "approval_required"
    assert approved.status_code == 200
    assert approved.json()["dispatch"] == "dispatched"
    assert approved.json()["work_item"]["status"] == "done"
    assert approved.json()["dispatch_result"]["memory_id"] > 0
    assert searched.json()["matches"][0]["title"] == "Approved handler note"


def test_local_handler_dispatch_requires_typed_confirmation(tmp_path: Path, monkeypatch) -> None:
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
            "kind": "approved_local",
            "title": "Commit approved founder note",
            "action": "local.memory.write",
            "target": "local_memory",
            "permission_tier": "L3_EXTERNAL_ACTION",
            "metadata": {"content": "Do not dispatch without phrase."},
        },
    )
    rejected = client.post(
        f"/work/items/{queued.json()['item_id']}/approve",
        json={"resolved_by": "founder", "note": "Wrong phrase.", "dispatch": True, "typed_confirmation": "wrong"},
    )
    queue = client.get("/work/queue")
    approvals = client.get("/approval-requests")

    assert rejected.status_code == 400
    assert "typed confirmation phrase" in rejected.json()["detail"]
    assert queue.json()["items"][0]["status"] == "approval_required"
    assert approvals.json()["items"][0]["status"] == "pending"


def test_safe_local_handlers_dispatch_after_typed_confirmation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))
    phrase = "make the jump to hyperspace"

    file_path = tmp_path / "hot" / "handler-output" / "approved.txt"
    file_item = client.post(
        "/work/items",
        json={
            "kind": "approved_local",
            "title": "Write approved file",
            "action": "local.file.write",
            "target": str(file_path),
            "permission_tier": "L3_EXTERNAL_ACTION",
            "metadata": {"content": "Approved local file write.", "mode": "create"},
        },
    )
    file_dispatch = client.post(
        f"/work/items/{file_item.json()['item_id']}/approve",
        json={"dispatch": True, "typed_confirmation": phrase},
    )

    report_item = client.post(
        "/work/items",
        json={
            "kind": "approved_local",
            "title": "Write founder report",
            "action": "local.report.write",
            "target": "local_report",
            "permission_tier": "L3_EXTERNAL_ACTION",
            "metadata": {"content": "The report is local, auditable, and linked to memory."},
        },
    )
    report_dispatch = client.post(
        f"/work/items/{report_item.json()['item_id']}/approve",
        json={"dispatch": True, "typed_confirmation": phrase},
    )

    browser_item = client.post(
        "/work/items",
        json={
            "kind": "approved_local",
            "title": "Prepare local UI open",
            "action": "local.browser.open",
            "target": "http://127.0.0.1:8787/ui",
            "permission_tier": "L3_EXTERNAL_ACTION",
            "metadata": {"open_browser": False},
        },
    )
    browser_dispatch = client.post(
        f"/work/items/{browser_item.json()['item_id']}/approve",
        json={"dispatch": True, "typed_confirmation": phrase},
    )

    assert file_dispatch.status_code == 200
    assert file_dispatch.json()["dispatch_result"]["path"] == str(file_path)
    assert file_path.read_text(encoding="utf-8") == "Approved local file write."
    assert report_dispatch.status_code == 200
    assert Path(report_dispatch.json()["dispatch_result"]["path"]).exists()
    assert report_dispatch.json()["dispatch_result"]["memory_id"] > 0
    assert browser_dispatch.status_code == 200
    assert browser_dispatch.json()["dispatch_result"]["opened"] is False


def test_approval_request_can_be_denied(tmp_path: Path, monkeypatch) -> None:
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
            "title": "Open browser",
            "action": "browser.open",
            "target": "https://example.com",
            "permission_tier": "L3_EXTERNAL_ACTION",
        },
    )
    request = client.get("/approval-requests").json()["items"][0]
    denied = client.post(
        f"/approval-requests/{request['id']}/deny",
        json={"resolved_by": "founder", "note": "Not now."},
    )

    assert queued.status_code == 200
    assert denied.status_code == 200
    assert denied.json()["request"]["status"] == "denied"
    assert denied.json()["work_item"]["status"] == "denied"


def test_ops_health_check_and_backup_routes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "generate", fake_generate)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    health = client.get("/ops/health-check")
    backup = client.post("/ops/backup", json={"label": "test run"})
    backup_2 = client.post("/ops/backup", json={"label": "test run 2"})
    backup_3 = client.post("/ops/backup", json={"label": "test run 3"})
    backups = client.get("/ops/backups")
    prune = client.post("/ops/backups/prune", json={"keep_last": 1, "dry_run": False})
    benchmark = client.post(
        "/models/benchmark",
        json={"prompt": "State readiness.", "roles": ["general", "coding"], "num_predict": 32},
    )
    telemetry = client.get("/models/telemetry")
    security = client.get("/ops/security")
    inventory = client.get("/self-inventory")

    assert health.status_code == 200
    assert health.json()["ok"] is True
    assert health.json()["checks"]["ui"]["ok"] is True
    assert backup.status_code == 200
    assert backup_2.status_code == 200
    assert backup_3.status_code == 200
    assert backup.json()["path"].endswith(".sqlite")
    assert Path(backup_3.json()["path"]).exists()
    assert backups.status_code == 200
    assert backups.json()["items"][0]["name"].endswith(".sqlite")
    assert prune.status_code == 200
    assert prune.json()["deleted_count"] >= 2
    assert benchmark.status_code == 200
    assert benchmark.json()["status"] == "ok"
    assert telemetry.json()["by_operation"]["ops.model_benchmark"] == 2
    assert security.status_code == 200
    assert security.json()["token_header"] == "X-Zade-Token"
    assert "GET /ops/health-check" in inventory.json()["ops_layer"]["routes"]
    assert "POST /ops/backups/prune" in inventory.json()["ops_layer"]["routes"]
    assert "POST /models/benchmark" in inventory.json()["runtime_layer"]["routes"]


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
    metrics = client.get("/founder/metrics")
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
    assert metrics.status_code == 200
    assert metrics.json()["counts"]["assumptions"] == 1
    assert metrics.json()["evidence"]["by_reliability"]["C"] == 1
    assert metrics.json()["integrity"]["by_status"]["open"] >= 1
    assert "POST /founder/integrity-check" in inventory.json()["founder_operating_layer"]["routes"]
    assert "GET /founder/metrics" in inventory.json()["founder_operating_layer"]["routes"]
    assert "evidence" in inventory.json()["founder_operating_layer"]["artifacts"]


def test_active_objective_decision_engine_and_runtime_context_routes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "generate", fake_generate)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    goal = client.post(
        "/founder/goals",
        json={"name": "Validate Zade as co-founder", "metric": "daily useful decisions", "target": "1"},
    )
    objective = client.post(
        "/founder/active-objectives",
        json={
            "objective": "Make Zade drive founder decisions",
            "desired_outcome": "Every session ends with a useful next action.",
            "metric": "daily useful decisions",
            "target": "1",
            "linked_goal_ids": [goal.json()["id"]],
            "risks": ["Recommendations become generic."],
            "next_action": "Run a structured recommendation against the active experiment.",
            "confidence": 70,
        },
    )
    recommendation = client.post(
        "/founder/decision-engine/recommend",
        json={
            "problem": "Should we improve evidence intake or dashboard polish next?",
            "options": [
                {"name": "Improve evidence intake", "recommended": True},
                {"name": "Polish dashboard", "priority": 40},
            ],
        },
    )
    active = client.get("/founder/active-objective")
    recs = client.get("/founder/decision-recommendations")
    dashboard = client.get("/founder/dashboard")
    context = client.post(
        "/runtime/context",
        json={"message": "What should Zade push next?", "use_semantic_memory": False},
    )
    runtime_response = client.post(
        "/runtime/respond",
        json={"message": "Recommend the next founder move.", "use_semantic_memory": False},
    )
    founder_ui = client.get("/ui/founder.html")
    metrics = client.get("/founder/metrics")
    inventory = client.get("/self-inventory")

    assert objective.status_code == 200
    assert objective.json()["item"]["is_current"] == 1
    assert recommendation.status_code == 200
    assert recommendation.json()["operating_contract"]["recommendation"] == "Improve evidence intake"
    assert recommendation.json()["decision_memo"]["recommendation"] == "Improve evidence intake"
    assert recommendation.json()["next_task"]["strategic_value"] == "Make Zade drive founder decisions"
    assert active.json()["item"]["objective"] == "Make Zade drive founder decisions"
    assert recs.json()["items"][0]["recommendation"] == "Improve evidence intake"
    assert dashboard.json()["active_objective"]["id"] == objective.json()["id"]
    assert context.json()["founder_dashboard"]["decision_engine"]["latest_recommendations"][0]["recommendation"] == "Improve evidence intake"
    assert runtime_response.json()["context"]["active_objective"]["objective"] == "Make Zade drive founder decisions"
    assert founder_ui.status_code == 200
    assert "Zade Founder Ops" in founder_ui.text
    assert metrics.json()["counts"]["active_objectives"] == 1
    assert metrics.json()["counts"]["decision_recommendations"] == 1
    assert "POST /founder/decision-engine/recommend" in inventory.json()["founder_operating_layer"]["routes"]
