from pathlib import Path

from fastapi.testclient import TestClient

import cofounder_kernel.connectors as connectors_module
from cofounder_kernel.api import create_app
from cofounder_kernel.config import AppConfig, KernelConfig, OllamaConfig, PathConfig
from cofounder_kernel.connectors import parse_ics_events
from cofounder_kernel.ollama import OllamaClient


PHRASE = "make the jump to hyperspace"

ICS_TEXT = "\r\n".join(
    [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "BEGIN:VEVENT",
        "UID:evt-001@example.com",
        "SUMMARY:Founder pricing call",
        "DTSTART:20260715T140000Z",
        "DTEND:20260715T150000Z",
        "ORGANIZER:mailto:alex@example.com",
        "DESCRIPTION:Discuss the $99/month plan with two",
        "  solo founders.",
        "LOCATION:Video call",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "UID:evt-002@example.com",
        "SUMMARY:Weekly founder review",
        "DTSTART:20260717",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
)


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


def _approve_and_dispatch(client: TestClient, item_id: int) -> dict:
    response = client.post(
        f"/work/items/{item_id}/approve",
        json={"resolved_by": "founder", "dispatch": True, "typed_confirmation": PHRASE},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_connector_creation_rejects_secrets_and_bad_types(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))

    with_secret = client.post(
        "/connectors",
        json={
            "name": "inbox",
            "connector_type": "imap",
            "config": {"host": "imap.example.com", "username": "z", "password": "hunter2"},
        },
    )
    bad_type = client.post("/connectors", json={"name": "x1", "connector_type": "webhook", "config": {}})
    missing_env = client.post(
        "/connectors",
        json={"name": "inbox", "connector_type": "imap", "config": {"host": "h", "username": "u"}},
    )
    valid = client.post(
        "/connectors",
        json={
            "name": "inbox",
            "connector_type": "imap",
            "config": {"host": "imap.example.com", "username": "zade", "password_env": "ZADE_TEST_IMAP_PW"},
        },
    )
    duplicate = client.post(
        "/connectors",
        json={
            "name": "inbox",
            "connector_type": "imap",
            "config": {"host": "imap.example.com", "username": "zade", "password_env": "ZADE_TEST_IMAP_PW"},
        },
    )
    inventory = client.get("/self-inventory")

    assert with_secret.status_code == 400
    assert "must not contain secrets" in with_secret.json()["detail"]
    assert bad_type.status_code == 400
    assert missing_env.status_code == 400
    assert "password_env" in missing_env.json()["detail"]
    assert valid.status_code == 200
    assert valid.json()["item"]["connector_type"] == "imap"
    assert duplicate.status_code == 400
    assert "POST /connectors/{name}/sync" in inventory.json()["connector_layer"]["routes"]
    assert "connector_items" in inventory.json()["surfacing_layer"]["signal_sources"]


def test_ics_sync_requires_approval_and_stages_candidates(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)
    config = _config(tmp_path)
    client = TestClient(create_app(config))
    ics_path = config.paths.hot_root / "calendar-export.ics"
    ics_path.write_text(ICS_TEXT, encoding="utf-8", newline="")

    created = client.post(
        "/connectors",
        json={"name": "founder-calendar", "connector_type": "ics", "config": {"path": str(ics_path)}},
    )
    queued = client.post("/connectors/founder-calendar/sync")
    handlers = client.get("/action-handlers")
    items_before = client.get("/connectors/items")

    assert created.status_code == 200
    assert queued.status_code == 200
    assert queued.json()["status"] == "approval_required"
    assert queued.json()["authority"]["decision"] == "approval_required"
    assert "external.connector.sync" in {item["action"] for item in handlers.json()["items"]}
    assert items_before.json()["items"] == []  # nothing synced before approval

    approved = _approve_and_dispatch(client, queued.json()["item_id"])
    items = client.get("/connectors/items")
    connector = client.get("/connectors/founder-calendar")
    attention = client.get("/surface/attention")

    assert approved["dispatch"] == "dispatched"
    assert approved["dispatch_result"]["fetched"] == 2
    assert approved["dispatch_result"]["created"] == 2
    assert approved["dispatch_result"]["read_only"] is True
    staged = items.json()["items"]
    assert len(staged) == 2
    by_title = {item["title"]: item for item in staged}
    call = by_title["Founder pricing call"]
    assert call["item_type"] == "calendar_event"
    assert call["sender"] == "alex@example.com"
    assert call["occurred_at"] == "2026-07-15T14:00:00+00:00"
    assert "$99/month plan with two solo founders" in call["excerpt"]
    assert call["status"] == "candidate"
    assert connector.json()["item"]["last_sync_status"] == "ok"
    assert any(item["kind"] == "connector_items_staged" for item in attention.json()["items"])

    # A second sync on the same content dedups instead of duplicating.
    requeued = client.post("/connectors/founder-calendar/sync")
    second_item_id = requeued.json()["item_id"]
    if requeued.json()["created"]:
        second = _approve_and_dispatch(client, second_item_id)
        assert second["dispatch_result"]["created"] == 0
        assert second["dispatch_result"]["unchanged"] == 2


def test_import_and_dismiss_turn_candidates_into_graded_evidence(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)
    config = _config(tmp_path)
    client = TestClient(create_app(config))
    ics_path = config.paths.hot_root / "calendar.ics"
    ics_path.write_text(ICS_TEXT, encoding="utf-8", newline="")
    client.post("/connectors", json={"name": "cal", "connector_type": "ics", "config": {"path": str(ics_path)}})
    queued = client.post("/connectors/cal/sync")
    _approve_and_dispatch(client, queued.json()["item_id"])
    staged = client.get("/connectors/items").json()["items"]
    first_id = staged[0]["id"]
    second_id = staged[1]["id"]

    imported = client.post(
        "/connectors/items/import",
        json={"item_ids": [first_id], "reliability": "B", "strength": 70},
    )
    dismissed = client.post(f"/connectors/items/{second_id}/dismiss", json={"reason": "not business-relevant"})
    re_import = client.post("/connectors/items/import", json={"item_ids": [first_id]})
    evidence = client.get("/founder/evidence")
    candidates_left = client.get("/connectors/items", params={"status": "candidate"})

    assert imported.status_code == 200
    assert imported.json()["count"] == 1
    evidence_id = imported.json()["imported"][0]["evidence_id"]
    assert evidence_id is not None
    assert imported.json()["imported"][0]["document_id"] is not None
    item = evidence.json()["items"][0]
    assert item["id"] == evidence_id
    assert item["evidence_type"] == "connector_calendar_event"
    assert item["reliability"] == "B"
    assert item["strength"] == 70
    assert item["metadata"]["entity_boundary"] == "External source says; Zade records as evidence."
    assert dismissed.status_code == 200
    assert dismissed.json()["item"]["status"] == "dismissed"
    assert re_import.json()["count"] == 0
    assert re_import.json()["skipped"][0]["reason"] == "status is imported"
    assert candidates_left.json()["items"] == []


def test_imap_sync_uses_env_credentials_and_mocked_fetcher(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))
    client.post(
        "/connectors",
        json={
            "name": "founder-inbox",
            "connector_type": "imap",
            "config": {"host": "imap.example.com", "username": "zade", "password_env": "ZADE_TEST_IMAP_PW"},
        },
    )

    # Without the credential env var, dispatch fails with a clear error and no crash.
    queued = client.post("/connectors/founder-inbox/sync")
    missing_env = client.post(
        f"/work/items/{queued.json()['item_id']}/approve",
        json={"dispatch": True, "typed_confirmation": PHRASE},
    )
    assert missing_env.status_code == 400
    assert "ZADE_TEST_IMAP_PW" in missing_env.json()["detail"]

    seen = {}

    def fake_fetch(config, password, *, limit=25):
        seen["password"] = password
        return [
            {
                "external_id": "<msg-1@example.com>",
                "item_type": "email",
                "title": "Re: pilot pricing",
                "sender": "founder@customerco.com",
                "occurred_at": "2026-07-11T09:00:00+00:00",
                "excerpt": "We would sign at $99/month if onboarding takes under a day.",
                "metadata": {"mailbox": "INBOX"},
            }
        ]

    monkeypatch.setenv("ZADE_TEST_IMAP_PW", "app-password")
    monkeypatch.setattr(connectors_module, "fetch_imap_items", fake_fetch)
    requeued = client.post("/connectors/founder-inbox/sync")
    # The failed dispatch marked the earlier item; a fresh queue entry may reuse it or create one.
    item_id = requeued.json()["item_id"]
    work_item = client.get("/work/queue").json()["items"]
    target = next(item for item in work_item if item["id"] == item_id)
    if target["status"] == "approval_required":
        dispatched = _approve_and_dispatch(client, item_id)
        assert dispatched["dispatch_result"]["created"] == 1
    else:
        # Item from the failed dispatch: queue a direct new sync via a fresh work item.
        fresh = client.post(
            "/work/items",
            json={
                "kind": "connector_sync",
                "title": "Sync founder-inbox again",
                "action": "external.connector.sync",
                "target": "founder-inbox",
                "permission_tier": "L3_EXTERNAL_ACTION",
                "source": "zade.proposal",
            },
        )
        dispatched = _approve_and_dispatch(client, fresh.json()["item_id"])
        assert dispatched["dispatch_result"]["created"] == 1

    items = client.get("/connectors/items", params={"connector": "founder-inbox"})
    assert seen["password"] == "app-password"
    assert items.json()["items"][0]["item_type"] == "email"
    assert items.json()["items"][0]["title"] == "Re: pilot pricing"


def test_parse_ics_events_unfolds_and_normalizes() -> None:
    events = parse_ics_events(ICS_TEXT)

    assert len(events) == 2
    assert events[0]["UID"] == "evt-001@example.com"
    assert events[0]["SUMMARY"] == "Founder pricing call"
    # Folded DESCRIPTION line is joined.
    assert events[0]["DESCRIPTION"].endswith("two solo founders.")
    assert events[1]["DTSTART"] == "20260717"
