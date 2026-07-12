from pathlib import Path

from fastapi.testclient import TestClient

from cofounder_kernel.api import create_app
from cofounder_kernel.config import AppConfig, KernelConfig, OllamaConfig, PathConfig
from cofounder_kernel.ollama import OllamaClient


PHRASE = "make the jump to hyperspace"


def fake_health(self: OllamaClient) -> dict:
    return {"version": "test"}


def _config(tmp_path: Path) -> KernelConfig:
    return KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )


def test_manual_plan_lifecycle_records_evidence(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))

    created = client.post(
        "/action-plans",
        json={
            "title": "Validate pricing with five founders",
            "objective": "Get real willingness-to-pay signal.",
            "steps": [
                {"title": "Draft the interview script", "detail": "Five questions max."},
                {"title": "Run the five calls", "detail": "Record willingness-to-pay quotes."},
            ],
        },
    )
    plan_id = created.json()["item"]["id"]
    advanced = client.post(f"/action-plans/{plan_id}/advance")
    step_one = advanced.json()["plan"]["steps"][0]
    completed = client.post(
        f"/action-plans/{plan_id}/steps/{step_one['id']}/complete",
        json={"result": "Script drafted with five questions."},
    )
    advanced_again = client.post(f"/action-plans/{plan_id}/advance")
    step_two = advanced_again.json()["plan"]["steps"][1]
    client.post(
        f"/action-plans/{plan_id}/steps/{step_two['id']}/complete",
        json={"result": "Five calls done; three would pay $99."},
    )
    final = client.get(f"/action-plans/{plan_id}")
    evidence = client.get("/founder/evidence")

    assert created.status_code == 200
    assert created.json()["item"]["status"] == "active"
    assert step_one["status"] == "running"
    assert step_one["authority_decision"] == "allow"
    assert completed.status_code == 200
    assert completed.json()["item"]["steps"][0]["status"] == "done"
    # Every completed step landed grade-A runtime evidence in the founder ledger.
    assert completed.json()["item"]["steps"][0]["evidence_ids"]
    assert final.json()["item"]["status"] == "done"
    outcome_items = [item for item in evidence.json()["items"] if item["evidence_type"] == "action_step_outcome"]
    assert len(outcome_items) == 2
    assert all(item["reliability"] == "A" for item in outcome_items)


def test_recommendation_converts_into_action_plan(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))

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
    rec_id = recommendation.json()["item"]["id"]
    plan = client.post(f"/action-plans/from-recommendation/{rec_id}")
    missing = client.post("/action-plans/from-recommendation/999")

    assert recommendation.status_code == 200
    assert plan.status_code == 200
    item = plan.json()["item"]
    assert item["source_type"] == "decision_recommendation"
    assert item["source_id"] == rec_id
    assert item["title"].startswith("Act on:")
    assert len(item["steps"]) == 1
    recs = client.get("/founder/decision-recommendations")
    assert any(rec["id"] == rec_id and rec["status"] == "planned" for rec in recs.json()["items"])
    assert missing.status_code == 404


def test_step_authority_gates_and_blocks(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))

    plan = client.post(
        "/action-plans",
        json={
            "title": "Outreach wave",
            "steps": [
                {"title": "Send follow-up emails", "action": "email.send", "permission_tier": "L3_EXTERNAL_ACTION"},
            ],
        },
    )
    plan_id = plan.json()["item"]["id"]
    step = plan.json()["item"]["steps"][0]
    advanced = client.post(f"/action-plans/{plan_id}/advance")
    complete_without_approval = client.post(f"/action-plans/{plan_id}/steps/{step['id']}/complete", json={})
    approved = client.post(f"/action-plans/{plan_id}/steps/{step['id']}/approve", json={})
    advanced_after = client.post(f"/action-plans/{plan_id}/advance")

    assert step["authority_decision"] == "approval_required"
    assert step["status"] == "approval_required"
    assert "needs founder approval" in advanced.json()["note"]
    assert complete_without_approval.status_code == 400
    assert approved.status_code == 200
    assert approved.json()["item"]["steps"][0]["status"] == "approved"
    assert advanced_after.json()["plan"]["steps"][0]["status"] == "running"

    denied_plan = client.post(
        "/action-plans",
        json={
            "title": "Bad idea",
            "steps": [{"title": "Place the trade", "action": "broker.place_order", "permission_tier": "L3_EXTERNAL_ACTION"}],
        },
    )
    attention = client.get("/surface/attention")

    assert denied_plan.status_code == 200
    assert denied_plan.json()["item"]["status"] == "blocked"
    assert denied_plan.json()["item"]["steps"][0]["status"] == "blocked"
    assert any(item["kind"] == "action_plan_stalled" for item in attention.json()["items"])


def test_work_queue_step_executes_through_approval_dispatch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))

    plan = client.post(
        "/action-plans",
        json={
            "title": "Machine step plan",
            "steps": [
                {
                    "title": "Record a no-op dispatch",
                    "action": "local.noop",
                    "permission_tier": "L3_EXTERNAL_ACTION",
                    "execution": "work_queue",
                }
            ],
        },
    )
    plan_id = plan.json()["item"]["id"]
    advanced = client.post(f"/action-plans/{plan_id}/advance")
    step = advanced.json()["plan"]["steps"][0]

    assert step["status"] == "approval_required"
    assert step["work_item_id"] is not None

    dispatched = client.post(
        f"/work/items/{step['work_item_id']}/approve",
        json={"resolved_by": "founder", "dispatch": True, "typed_confirmation": PHRASE},
    )
    synced = client.get(f"/action-plans/{plan_id}")

    assert dispatched.status_code == 200, dispatched.text
    final_step = synced.json()["item"]["steps"][0]
    assert final_step["status"] == "done"
    assert final_step["evidence_ids"]
    assert synced.json()["item"]["status"] == "done"


def test_failed_step_fails_plan_and_notifies(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))

    plan = client.post(
        "/action-plans",
        json={"title": "Fragile plan", "steps": [{"title": "Try the thing"}]},
    )
    plan_id = plan.json()["item"]["id"]
    client.post(f"/action-plans/{plan_id}/advance")
    step = client.get(f"/action-plans/{plan_id}").json()["item"]["steps"][0]
    failed = client.post(
        f"/action-plans/{plan_id}/steps/{step['id']}/fail",
        json={"error": "Vendor said no."},
    )
    notifications = client.get("/notifications", params={"topic": "action_plan.step_failed"})

    assert failed.status_code == 200
    assert failed.json()["item"]["status"] == "failed"
    assert failed.json()["item"]["steps"][0]["status"] == "failed"
    assert failed.json()["item"]["steps"][0]["evidence_ids"]
    assert notifications.json()["items"]
    assert notifications.json()["items"][0]["severity"] == "warning"
