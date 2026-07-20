import io
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

import cofounder_kernel.notify as notify_module
from cofounder_kernel.api import create_app
from cofounder_kernel.config import AppConfig, KernelConfig, OllamaConfig, PathConfig
from cofounder_kernel.db import KernelDatabase
from cofounder_kernel.notify import NotificationBus
from cofounder_kernel.notify import _in_quiet_hours
from cofounder_kernel.ollama import OllamaClient
from cofounder_kernel.project_autonomy import ProjectAutonomyReporter


def fake_health(self: OllamaClient) -> dict:
    return {"version": "test"}


def _config(tmp_path: Path) -> KernelConfig:
    return KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )


def test_default_channels_and_ui_delivery(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))

    channels = client.get("/notify/channels")
    sent = client.post(
        "/notify",
        json={"topic": "test.ping", "title": "Hello founder", "body": "Bus is alive."},
    )
    feed = client.get("/notifications")
    read = client.post(f"/notifications/{sent.json()['item']['id']}/read")
    unread = client.get("/notifications", params={"unread_only": True})
    inventory = client.get("/self-inventory")

    by_name = {item["channel"]: item for item in channels.json()["items"]}
    assert set(by_name) == {"ui", "voice", "sms", "telegram"}
    assert by_name["ui"]["enabled"] is True
    assert by_name["voice"]["enabled"] is False
    assert by_name["sms"]["enabled"] is False
    assert by_name["sms"]["min_severity"] == "critical"
    assert by_name["telegram"]["enabled"] is True
    assert by_name["telegram"]["min_severity"] == "warning"
    assert by_name["telegram"]["rate_limit_per_hour"] > 0
    assert sent.status_code == 200
    assert sent.json()["item"]["status"] == "delivered"
    deliveries = sent.json()["item"]["deliveries"]
    assert [d["channel"] for d in deliveries] == ["ui"]
    assert deliveries[0]["status"] == "delivered"
    assert feed.json()["items"][0]["title"] == "Hello founder"
    assert read.json()["item"]["read_at"]
    assert unread.json()["items"] == []
    assert "POST /notify" in inventory.json()["notification_layer"]["routes"]
    assert "sms" in inventory.json()["notification_layer"]["channels"]
    assert "telegram" in inventory.json()["notification_layer"]["channels"]


def test_telegram_callback_uses_bus_dedupe_and_hourly_rate_limit(tmp_path: Path) -> None:
    db = KernelDatabase(tmp_path / "kernel.sqlite")
    db.migrate()
    bus = NotificationBus(db=db)
    sent: list[str] = []

    def deliver(text: str):
        sent.append(text)
        return SimpleNamespace(status="delivered", detail="1 bound founder chat")

    bus.set_telegram_sender(deliver)
    bus.update_channel("telegram", {"rate_limit_per_hour": 1})

    first = bus.notify(
        topic="project.decision_required",
        title="Same Ground needs a decision",
        body="Choose storage",
        severity="warning",
        dedupe_key="project-decision:9",
    )
    duplicate = bus.notify(
        topic="project.decision_required",
        title="Same Ground needs a decision",
        body="Choose storage",
        severity="warning",
        dedupe_key="project-decision:9",
    )
    limited = bus.notify(
        topic="project.decision_required",
        title="The Dark Index needs a decision",
        body="Choose navigation",
        severity="warning",
        dedupe_key="project-decision:10",
    )

    assert sent == ["Same Ground needs a decision\n\nChoose storage"]
    assert duplicate["status"] == "suppressed"
    assert duplicate["suppressed_reason"] == "duplicate_within_window"
    first_telegram = next(item for item in first["deliveries"] if item["channel"] == "telegram")
    limited_telegram = next(item for item in limited["deliveries"] if item["channel"] == "telegram")
    assert first_telegram["status"] == "delivered"
    assert limited_telegram["status"] == "suppressed"
    assert limited_telegram["detail"] == "rate_limited"


def test_project_autonomy_outbox_retries_quiet_hours_then_delivers_once(
    tmp_path: Path, monkeypatch
) -> None:
    db = KernelDatabase(tmp_path / "kernel.sqlite")
    db.migrate()
    root = tmp_path / "project-intake" / "Same Ground"
    root.mkdir(parents=True)
    project_id = db.upsert_project(
        canonical_path=str(root),
        name="Same Ground",
        product_type="mobile_application",
        distribution_targets=["google_play", "apple_app_store_eventual"],
        lifecycle_state="verified",
        repo_fingerprint="fp",
        metadata={},
    )
    decision_id, _created = db.enqueue_work_item(
        kind="founder_decision",
        title="Choose storage",
        detail="Choose the local database.",
        action="project.decision.resolve",
        target="Same Ground",
        permission_tier="L1_MEMORY_WRITE",
        metadata={"workspace": str(root.resolve()), "project_id": project_id},
        unique_key="decision:same-ground:storage",
    )
    bus = NotificationBus(db=db)
    sent: list[str] = []

    def deliver(text: str):
        sent.append(text)
        return SimpleNamespace(status="delivered", detail="founder notified")

    bus.set_telegram_sender(deliver)
    bus.update_channel(
        "telegram", {"quiet_start": "22:00", "quiet_end": "07:00"}
    )
    monkeypatch.setattr(notify_module, "_local_hhmm", lambda: "23:30")
    reporter = ProjectAutonomyReporter(db=db, bus=bus)
    reporter.plan(project_id, criteria=[{"id": "mvp-1", "title": "Core flow"}])
    reporter.begin_increment(project_id, criterion_id="mvp-1")

    reporter.report_needs_decision(
        project_id,
        decision_id=decision_id,
        question="Which local database should the app use?",
        recommendation="SQLite",
        options=[
            {"option": "SQLite", "impact": "stays local"},
            {"option": "Realm", "impact": "adds a dependency"},
        ],
    )

    with db.connect() as conn:
        waiting = conn.execute(
            "SELECT * FROM project_autonomy_outbox WHERE project_id = ?", (project_id,)
        ).fetchone()
        conn.execute(
            "UPDATE project_autonomy_outbox SET next_attempt_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", waiting["id"]),
        )
    assert waiting["status"] == "retry"
    assert waiting["attempts"] == 1
    assert sent == []

    bus.update_channel("telegram", {"quiet_start": "", "quiet_end": ""})
    delivered = reporter.deliver_due_notifications()
    again = reporter.deliver_due_notifications()

    assert delivered == {"seen": 1, "delivered": 1, "retried": 0}
    assert again == {"seen": 0, "delivered": 0, "retried": 0}
    assert len(sent) == 1
    assert "Open Zade" in sent[0]
    assert "Reply exactly" not in sent[0]
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM project_autonomy_outbox WHERE project_id = ?", (project_id,)
        ).fetchone()
    assert row["status"] == "delivered"
    assert row["attempts"] == 2
    event = next(
        item
        for item in db.list_project_events(project_id)
        if item["event_type"] == "decision_requested"
    )
    assert event["work_item_id"] == decision_id


def test_dedupe_rate_limit_and_severity_rules(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))

    first = client.post("/notify", json={"topic": "t", "title": "Once", "dedupe_key": "same-key"})
    duplicate = client.post("/notify", json={"topic": "t", "title": "Twice", "dedupe_key": "same-key"})

    assert first.json()["item"]["status"] == "delivered"
    assert duplicate.json()["item"]["status"] == "suppressed"
    assert duplicate.json()["item"]["suppressed_reason"] == "duplicate_within_window"

    # Tighten the UI rate limit to 3/hour; two are already delivered above? No —
    # only 'Once' delivered. Send until the limit trips.
    client.post("/notify/channels/ui", json={"rate_limit_per_hour": 2})
    client.post("/notify", json={"topic": "t", "title": "Second delivered"})
    limited = client.post("/notify", json={"topic": "t", "title": "Third should be limited"})

    assert limited.json()["item"]["status"] == "suppressed"
    assert limited.json()["item"]["deliveries"][0]["status"] == "suppressed"
    assert limited.json()["item"]["deliveries"][0]["detail"] == "rate_limited"

    # Raise min severity: info no longer qualifies for the only enabled channel.
    client.post("/notify/channels/ui", json={"rate_limit_per_hour": 100, "min_severity": "warning"})
    info = client.post("/notify", json={"topic": "t", "title": "Just info", "severity": "info"})
    warning = client.post("/notify", json={"topic": "t", "title": "A warning", "severity": "warning"})

    assert info.json()["item"]["status"] == "suppressed"
    assert info.json()["item"]["suppressed_reason"] == "no_channel_delivered"
    assert warning.json()["item"]["status"] == "delivered"

    bad_severity = client.post("/notify", json={"topic": "t", "title": "x", "severity": "loud"})
    bad_channel = client.post("/notify/channels/pager", json={"enabled": True})
    bad_quiet = client.post("/notify/channels/ui", json={"quiet_start": "25:99"})
    assert bad_severity.status_code == 400
    assert bad_channel.status_code == 400
    assert bad_quiet.status_code == 400


def test_quiet_hours_suppress_but_critical_bypasses(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(notify_module, "_local_hhmm", lambda: "23:30")
    client = TestClient(create_app(_config(tmp_path)))
    client.post("/notify/channels/ui", json={"quiet_start": "22:00", "quiet_end": "07:00"})

    info = client.post("/notify", json={"topic": "t", "title": "Late info", "severity": "info"})
    critical = client.post("/notify", json={"topic": "t", "title": "Kernel down", "severity": "critical"})

    assert info.json()["item"]["status"] == "suppressed"
    assert info.json()["item"]["deliveries"][0]["detail"] == "quiet_hours"
    assert critical.json()["item"]["status"] == "delivered"

    # Overnight window math is unit-tested for both directions.
    assert _in_quiet_hours("23:30", "22:00", "07:00") is True
    assert _in_quiet_hours("06:59", "22:00", "07:00") is True
    assert _in_quiet_hours("12:00", "22:00", "07:00") is False
    assert _in_quiet_hours("12:00", "09:00", "17:00") is True
    assert _in_quiet_hours("08:59", "09:00", "17:00") is False


def test_sms_channel_whitelist_and_gateway(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))

    # Enabled but unconfigured: fails loudly, never silently.
    client.post("/notify/channels/sms", json={"enabled": True, "min_severity": "info", "quiet_start": "", "quiet_end": ""})
    unconfigured = client.post("/notify", json={"topic": "t", "title": "No gateway yet"})
    sms_delivery = [d for d in unconfigured.json()["item"]["deliveries"] if d["channel"] == "sms"][0]
    assert sms_delivery["status"] == "failed"
    assert "gateway not configured" in sms_delivery["detail"]

    # Configured recipient must be on the whitelist.
    client.post(
        "/notify/channels/sms",
        json={"config": {"gateway_url": "http://127.0.0.1:9999/send", "to": "+15551234567"}, "recipients": ["+15550000000"]},
    )
    not_whitelisted = client.post("/notify", json={"topic": "t", "title": "Blocked recipient"})
    sms_delivery = [d for d in not_whitelisted.json()["item"]["deliveries"] if d["channel"] == "sms"][0]
    assert sms_delivery["status"] == "suppressed"
    assert "not in whitelist" in sms_delivery["detail"]

    # Whitelisted recipient goes out through the founder-configured gateway.
    calls = {}

    class FakeResponse(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=10):
        calls["url"] = request.full_url
        calls["data"] = request.data
        return FakeResponse(b"ok")

    monkeypatch.setattr(notify_module.urllib.request, "urlopen", fake_urlopen)
    client.post("/notify/channels/sms", json={"recipients": ["+15551234567"]})
    delivered = client.post("/notify", json={"topic": "t", "title": "Real page", "body": "Kernel needs you."})
    sms_delivery = [d for d in delivered.json()["item"]["deliveries"] if d["channel"] == "sms"][0]

    assert sms_delivery["status"] == "delivered"
    assert calls["url"] == "http://127.0.0.1:9999/send"
    assert b"+15551234567" in calls["data"]
    assert b"Real page" in calls["data"]


def test_voice_channel_without_tts_fails_loudly_but_ui_still_delivers(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    # No [voice] config: TTS is not configured, which is the default install state.
    client = TestClient(create_app(_config(tmp_path)))

    client.post("/notify/channels/voice", json={"enabled": True, "min_severity": "info"})
    sent = client.post("/notify", json={"topic": "t", "title": "Spoken?", "severity": "warning"})

    deliveries = {d["channel"]: d for d in sent.json()["item"]["deliveries"]}
    # The notification still reaches the founder through the UI feed...
    assert sent.json()["item"]["status"] == "delivered"
    assert deliveries["ui"]["status"] == "delivered"
    # ...and the voice attempt is recorded as a loud, explained failure.
    assert deliveries["voice"]["status"] == "failed"
    assert "not configured" in deliveries["voice"]["detail"]
    assert "tts_command" in deliveries["voice"]["detail"]


def test_surfacing_brief_announces_through_the_bus(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))
    # Seed one attention signal: an overdue commitment.
    client.post("/commitments", json={"title": "Overdue promise", "due_at": "2020-01-01"})

    brief = client.post("/surface/brief", json={})
    notifications = client.get("/notifications", params={"topic": "surfacing.brief"})

    assert brief.status_code == 200
    assert brief.json()["quiet"] is False
    assert brief.json()["notification_id"] is not None
    assert notifications.json()["items"]
    assert "need founder attention" in notifications.json()["items"][0]["title"]


def test_producers_cannot_bypass_channel_severity_or_enablement(tmp_path: Path) -> None:
    db = KernelDatabase(tmp_path / "kernel.sqlite")
    db.migrate()
    bus = NotificationBus(db=db)
    sent: list[str] = []

    def deliver(text: str):
        sent.append(text)
        return SimpleNamespace(status="delivered", detail="1 bound founder chat")

    bus.set_telegram_sender(deliver)
    bus.update_channel("telegram", {"quiet_start": "", "quiet_end": ""})

    gated = bus.notify(topic="project.mvp_complete", title="MVP done", body="a", severity="info")
    warning = bus.notify(
        topic="project.mvp_complete",
        title="MVP done",
        body="b",
        severity="warning",
        dedupe_key="project:1:mvp:abc",
    )
    bus.update_channel("telegram", {"enabled": False})
    disabled = bus.notify(
        topic="project.mvp_complete",
        title="MVP done",
        body="c",
        severity="warning",
        dedupe_key="project:2:mvp:def",
    )

    assert [d["channel"] for d in gated["deliveries"]] == ["ui"]
    assert {d["channel"] for d in warning["deliveries"]} == {"ui", "telegram"}
    assert len(sent) == 1
    assert [d["channel"] for d in disabled["deliveries"]] == ["ui"]
