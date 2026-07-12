from pathlib import Path

from fastapi.testclient import TestClient

from cofounder_kernel.api import create_app
from cofounder_kernel.config import AppConfig, KernelConfig, OllamaConfig, PathConfig
from cofounder_kernel.ollama import OllamaClient


def fake_health(self: OllamaClient) -> dict:
    return {"version": "test"}


def fake_embed(self: OllamaClient, *, text: str, model: str | None = None) -> list[float]:
    return [1.0, 0.0]


def _config(tmp_path: Path) -> KernelConfig:
    return KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )


def test_check_flags_overdue_and_monitor_commitments_once_per_day(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))

    overdue = client.post(
        "/commitments",
        json={
            "who": "founder",
            "kind": "do",
            "title": "Send the pilot pricing proposal",
            "due_at": "2020-01-01T00:00:00+00:00",
        },
    )
    monitor = client.post(
        "/commitments",
        json={"who": "zade", "kind": "monitor", "title": "Watch weekly activation", "cadence": "weekly"},
    )
    first_check = client.post("/commitments/check")
    second_check = client.post("/commitments/check")
    detail = client.get(f"/commitments/{overdue.json()['item']['id']}")
    notifications = client.get("/notifications", params={"topic": "commitment.overdue"})
    attention = client.get("/surface/attention")

    assert overdue.status_code == 200
    assert monitor.status_code == 200
    payload = first_check.json()
    assert payload["open"] == 2
    assert len(payload["overdue"]) == 1
    assert payload["overdue"][0]["title"] == "Send the pilot pricing proposal"
    # Zade's weekly monitor was created just now, so it is not yet due.
    assert payload["monitor_due"] == []
    assert payload["follow_ups_recorded"] == 1
    # Follow-ups are throttled to one per commitment per day.
    assert second_check.json()["follow_ups_recorded"] == 0
    events = [event["event"] for event in detail.json()["item"]["events"]]
    assert "follow_up" in events
    assert detail.json()["item"]["follow_up_count"] == 1
    # The overdue promise went through the notification bus...
    assert notifications.json()["items"]
    assert notifications.json()["items"][0]["severity"] == "warning"
    # ...and shows up in the attention queue.
    assert any(item["kind"] == "commitment_overdue" for item in attention.json()["items"])


def test_commitment_lifecycle_done_miss_drop_and_validation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))

    evidence = client.post(
        "/founder/evidence",
        json={"evidence_type": "note", "source": "manual", "claim_supported": "Proposal sent.", "strength": 70},
    )
    done = client.post("/commitments", json={"title": "Ship proposal", "due_at": "2030-01-01"})
    missed = client.post("/commitments", json={"title": "Call the lawyer", "due_at": "2020-01-01"})
    dropped = client.post("/commitments", json={"title": "Old idea"})

    done_result = client.post(
        f"/commitments/{done.json()['item']['id']}/done",
        json={"note": "Sent this morning.", "evidence_id": evidence.json()["id"]},
    )
    miss_result = client.post(f"/commitments/{missed.json()['item']['id']}/miss", json={"note": "It slipped."})
    drop_result = client.post(f"/commitments/{dropped.json()['item']['id']}/drop", json={"note": "No longer relevant."})
    double_close = client.post(f"/commitments/{done.json()['item']['id']}/miss", json={})
    bad_who = client.post("/commitments", json={"who": "assistant", "title": "x"})
    monitor_without_cadence = client.post("/commitments", json={"kind": "monitor", "title": "watch things"})

    assert done_result.status_code == 200
    assert done_result.json()["item"]["status"] == "done"
    assert done_result.json()["item"]["evidence_ids"] == [evidence.json()["id"]]
    assert miss_result.json()["item"]["status"] == "missed"
    assert drop_result.json()["item"]["status"] == "dropped"
    assert double_close.status_code == 400
    assert bad_who.status_code == 400
    assert monitor_without_cadence.status_code == 400


def test_renegotiation_drift_is_detected_and_surfaced(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))

    commitment = client.post(
        "/commitments",
        json={"title": "Finish the deck", "due_at": "2030-01-01"},
    )
    commitment_id = commitment.json()["item"]["id"]
    first = client.post(f"/commitments/{commitment_id}/renegotiate", json={"due_at": "2030-02-01"})
    second = client.post(f"/commitments/{commitment_id}/renegotiate", json={"due_at": "2030-03-01"})
    detail = client.get(f"/commitments/{commitment_id}")
    attention = client.get("/surface/attention")

    assert first.status_code == 200
    assert first.json()["item"]["renegotiation_count"] == 1
    assert second.json()["item"]["renegotiation_count"] == 2
    events = [event["event"] for event in detail.json()["item"]["events"]]
    assert events.count("renegotiated") == 2
    assert "drift_detected" in events
    drifting = [item for item in attention.json()["items"] if item["kind"] == "commitment_drifting"]
    assert drifting
    assert "Renegotiated 2 time(s)" in drifting[0]["detail"]


def test_cadence_runs_the_commitment_check(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)
    client = TestClient(create_app(_config(tmp_path)))
    client.post("/commitments", json={"title": "Overdue thing", "due_at": "2020-01-01"})

    cadence = client.post(
        "/runtime/cadence",
        json={"run_autonomous": True, "max_run": 5, "review_type": "daily"},
    )
    inventory = client.get("/self-inventory")

    assert cadence.status_code == 200
    assert cadence.json()["commitments"]["open"] == 1
    assert len(cadence.json()["commitments"]["overdue"]) == 1
    assert cadence.json()["commitments"]["follow_ups_recorded"] == 1
    assert "POST /commitments/check" in inventory.json()["commitment_layer"]["routes"]
    assert "commitments" in inventory.json()["surfacing_layer"]["signal_sources"]
