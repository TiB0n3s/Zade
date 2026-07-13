from pathlib import Path

from fastapi.testclient import TestClient

from cofounder_kernel.api import create_app
from cofounder_kernel.config import AppConfig, KernelConfig, OllamaConfig, PathConfig
from cofounder_kernel.ollama import GenerateResult, OllamaClient


def fake_health(self: OllamaClient) -> dict:
    return {"version": "test"}


def fake_embed(self: OllamaClient, *, text: str, model: str | None = None) -> list[float]:
    return [1.0, 0.0]


def fake_generate(
    self: OllamaClient,
    *,
    prompt: str,
    model: str | None = None,
    think: bool | None = None,
    temperature: float | None = None,
    num_predict: int = 512,
) -> GenerateResult:
    return GenerateResult(response="Executive read.", model=model or "qwen3:14b", raw={"prompt": prompt})


def _config(tmp_path: Path) -> KernelConfig:
    return KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )


def _seed_signals(client: TestClient) -> dict:
    bet = client.post(
        "/founder/strategy-objects",
        json={"object_type": "active_bet", "title": "Start with solo founders", "confidence": 68},
    )
    kill = client.post(
        "/founder/kill-criteria",
        json={
            "subject_type": "bet",
            "subject_id": bet.json()["id"],
            "metric": "weekly activation",
            "threshold": "< 20%",
            "by_date": "2020-01-01",
        },
    )
    prediction = client.post(
        "/founder/predictions",
        json={"prediction": "Activation clears 20% in 30 days.", "probability": 0.6, "due_at": "2020-06-01"},
    )
    assumption = client.post(
        "/founder/assumptions",
        json={"statement": "Solo founders pay $99/month.", "category": "pricing", "confidence": 70},
    )
    evidence = client.post(
        "/founder/evidence",
        json={
            "evidence_type": "customer interview",
            "source": "five founder calls",
            "reliability": "C",
            "claim_contradicted": "Willingness to pay clusters near $29/month.",
            "strength": 80,
            "linked_assumption_id": assumption.json()["id"],
        },
    )
    goal = client.post("/founder/goals", json={"name": "Validate willingness to pay"})
    integrity = client.post("/founder/integrity-check")
    approval = client.post(
        "/work/items",
        json={
            "kind": "external",
            "title": "Send follow-up email",
            "action": "email.send",
            "target": "founder@example.com",
            "permission_tier": "L3_EXTERNAL_ACTION",
            "source": "zade.proposal",
        },
    )
    assert bet.status_code == 200
    assert kill.status_code == 200
    assert prediction.status_code == 200
    assert assumption.status_code == 200
    assert evidence.status_code == 200
    assert goal.status_code == 200
    assert integrity.status_code == 200
    assert integrity.json()["count"] >= 1
    assert approval.status_code == 200
    return {"bet_id": bet.json()["id"], "assumption_id": assumption.json()["id"]}


def test_empty_state_is_quiet_and_force_still_writes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))

    scan = client.get("/surface/attention")
    quiet = client.post("/surface/brief", json={})
    events = client.get("/runtime/events")
    forced = client.post("/surface/brief", json={"force": True})
    searched = client.post("/memory/search", json={"query": "Initiated Brief", "limit": 5})

    assert scan.status_code == 200
    assert scan.json()["count"] == 0
    assert scan.json()["one_thing"] == "Nothing needs founder attention right now."
    assert quiet.status_code == 200
    assert quiet.json()["quiet"] is True
    assert quiet.json()["memory_id"] is None
    assert quiet.json()["changes"]["first_brief"] is True
    assert events.json()["events"][0]["event_type"] == "runtime.surfacing"
    assert forced.json()["memory_id"] is not None and forced.json()["memory_id"] > 0
    assert forced.json()["changes"]["first_brief"] is False
    assert searched.json()["matches"]


def test_attention_scan_ranks_overdue_kill_criteria_first(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))
    _seed_signals(client)

    scan = client.get("/surface/attention")
    payload = scan.json()
    kinds = {item["kind"] for item in payload["items"]}

    assert scan.status_code == 200
    assert payload["count"] >= 5
    assert payload["items"][0]["kind"] == "kill_criteria_overdue"
    assert payload["items"][0]["severity"] == "red"
    assert payload["one_thing"].startswith("Kill criterion past its date")
    assert {
        "kill_criteria_overdue",
        "integrity_warning",
        "thesis_conflict",
        "prediction_overdue",
        "confidence_drop",
        "approvals_pending",
    } <= kinds
    # Scores are ranked descending.
    scores = [item["score"] for item in payload["items"]]
    assert scores == sorted(scores, reverse=True)
    # The longest-open secondary signal is called out as underweighted.
    assert payload["underweighted"]


def test_attention_items_include_routable_ui_destinations(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))
    _seed_signals(client)

    payload = client.get("/surface/attention").json()
    hrefs_by_kind = {item["kind"]: item["href"] for item in payload["items"]}

    assert hrefs_by_kind["thesis_conflict"] == "/ui/ledger.html#sec-thesis-conflicts"
    assert hrefs_by_kind["approvals_pending"] == "/ui/inbox.html#decisions"
    assert all(item["href"].startswith("/ui/") for item in payload["items"])


def test_brief_persists_and_tracks_changes_between_briefs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "generate", fake_generate)
    app = create_app(_config(tmp_path))
    client = TestClient(app)
    seeded = _seed_signals(client)

    first = client.post("/surface/brief", json={"narrate": True})
    second = client.post("/surface/brief", json={})

    assert first.status_code == 200
    assert first.json()["quiet"] is False
    assert first.json()["memory_id"] > 0
    assert first.json()["changes"]["first_brief"] is True
    assert "Attention queue" in first.json()["brief"]
    assert "The one thing that matters most: Kill criterion past its date" in first.json()["brief"]
    assert first.json()["narrative"] == "Executive read."
    assert second.status_code == 200
    assert second.json()["changes"]["first_brief"] is False
    assert second.json()["changes"]["total"] == 0

    # Backdate prior briefs, land new evidence, and the delta must pick it up.
    with app.state.db.connect() as conn:
        conn.execute(
            "UPDATE runtime_events SET created_at = '2020-01-01T00:00:00+00:00' WHERE event_type = 'runtime.surfacing'"
        )
    added = client.post(
        "/founder/evidence",
        json={
            "evidence_type": "metric",
            "source": "weekly funnel",
            "reliability": "B",
            "claim_supported": "Activation is trending up.",
            "strength": 70,
            "linked_assumption_id": seeded["assumption_id"],
        },
    )
    third = client.post("/surface/brief", json={})

    assert added.status_code == 200
    assert third.json()["changes"]["new_evidence"] >= 1
    assert third.json()["changes"]["total"] >= 1
    assert "Since the last brief" in third.json()["brief"]


def test_cadence_generates_initiated_brief_and_drives_next_action(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)
    client = TestClient(create_app(_config(tmp_path)))

    cadence = client.post(
        "/runtime/cadence",
        json={"run_autonomous": True, "max_run": 5, "review_type": "daily"},
    )
    events = client.get("/runtime/events")
    inventory = client.get("/self-inventory")

    assert cadence.status_code == 200
    surfacing = cadence.json()["surfacing"]
    # The experiment loop seeds EXP-001 (minimum_evidence 10), so at least one
    # attention item exists and the initiated brief is persisted.
    assert surfacing["count"] >= 1
    assert surfacing["memory_id"] > 0
    assert cadence.json()["next_action"] == surfacing["one_thing"]
    assert any(event["event_type"] == "runtime.surfacing" for event in events.json()["events"])
    assert "GET /surface/attention" in inventory.json()["surfacing_layer"]["routes"]
    assert "POST /surface/brief" in inventory.json()["surfacing_layer"]["routes"]
    assert "kill_criteria" in inventory.json()["surfacing_layer"]["signal_sources"]
