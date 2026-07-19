"""Kernel heartbeat: enabled-but-down channel alerts, cadence-staleness alerts,
and the once-a-day egress-gated morning brief.

Telegram sends are faked; the egress gate and the audit trail run for real
against a KernelConfig and a real (tmp) KernelDatabase.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from cofounder_kernel.config import (
    AppConfig,
    EgressConfig,
    KernelConfig,
    OllamaConfig,
    TelegramConfig,
)
from cofounder_kernel.db import KernelDatabase
from cofounder_kernel.heartbeat import (
    BRIEF_AUDIT_ACTION,
    KernelHeartbeat,
    compose_digest,
)

_DEFAULT = object()


def make_db(tmp_path: Path) -> KernelDatabase:
    db = KernelDatabase(tmp_path / "kernel.sqlite")
    db.migrate()
    return db


def make_config(
    *,
    enabled: bool = True,
    brief: bool = True,
    grant: bool = True,
    policy: str = "local_preferred",
    brief_time: str = "07:30",
) -> KernelConfig:
    grants = ("reply_text:telegram",) if grant else ()
    return KernelConfig(
        app=AppConfig(),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1", provider_policy=policy),
        telegram=TelegramConfig(enabled=enabled, brief_enabled=brief, brief_time=brief_time),
        egress=EgressConfig(standing_grants=grants),
    )


class FakeNotify:
    def __init__(self):
        self.sent: list[dict] = []

    def notify(self, **kw):
        self.sent.append(kw)
        return kw


class FakeFounder:
    def brief(self) -> dict:
        return {
            "brief": "unused-advisor-artifact",
            "dashboard": {
                "company_health": "at_risk",
                "overall_confidence": 0.62,
                "one_thing_that_matters_most_today": "Ship the morning brief",
                "top_objectives": [{"objective": "Land first customer", "current_risk": "high"}],
                "decisions_waiting": [{"problem": "Pick pricing model"}],
                "critical_risks": [],
                "approval_pressure": {"headline": "No approval blockers.", "items": []},
                "decision_engine": {"latest_recommendations": []},
            },
        }


def make_heartbeat(
    config: KernelConfig,
    db: KernelDatabase,
    *,
    running: bool = True,
    chats: tuple[int, ...] = (42,),
    send: object = _DEFAULT,
    notify: FakeNotify | None = None,
) -> tuple[KernelHeartbeat, list[tuple[int, str]]]:
    sent: list[tuple[int, str]] = []

    def _send(chat_id: int, text: str) -> None:
        sent.append((chat_id, text))

    hb = KernelHeartbeat(
        config,
        db=db,
        founder=FakeFounder(),
        notify=notify or FakeNotify(),
        telegram_running=lambda: running,
        telegram_chat_ids=lambda: list(chats),
        send_telegram=_send if send is _DEFAULT else send,  # type: ignore[arg-type]
    )
    return hb, sent


def in_window_today(hour: int = 7, minute: int = 45) -> datetime:
    return datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)


# ---- digest composition -----------------------------------------------------
def test_digest_composes_from_dashboard_and_drops_empty_sections() -> None:
    text = compose_digest(
        FakeFounder().brief(), now_local=datetime(2026, 7, 20, 7, 30), max_chars=4000
    )
    assert text.startswith("Morning brief — Mon Jul 20")
    assert "Health: at_risk | confidence 0.62" in text
    assert "Focus today: Ship the morning brief" in text
    assert "- Land first customer (risk: high)" in text
    assert "- Pick pricing model" in text
    assert "Approvals: No approval blockers." in text
    assert "Risks increasing" not in text  # empty section dropped, not marked


def test_digest_respects_max_chars() -> None:
    text = compose_digest(FakeFounder().brief(), now_local=datetime(2026, 7, 20, 7, 30), max_chars=50)
    assert len(text) <= 50


# ---- morning brief ----------------------------------------------------------
def test_brief_sends_once_per_day_and_audits(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    hb, sent = make_heartbeat(make_config(), db)
    now = in_window_today()

    hb.tick(now)
    hb.tick(now + timedelta(minutes=5))
    assert len(sent) == 1
    chat_id, text = sent[0]
    assert chat_id == 42 and text.startswith("Morning brief")

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_events WHERE action = ?", (BRIEF_AUDIT_ACTION,)
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "ok"
    assert rows[0]["actor"] == "heartbeat"

    # A different (next) local day sends again.
    hb.tick(now + timedelta(days=1))
    assert len(sent) == 2


def test_brief_only_inside_window(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    hb, sent = make_heartbeat(make_config(), db)
    hb.tick(in_window_today(hour=7, minute=0))  # before 07:30
    assert sent == []
    hb.tick(in_window_today(hour=14, minute=0))  # past 07:30 + 6h window
    assert sent == []


def test_brief_fail_closed_without_grant_one_attempt_per_day(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    hb, sent = make_heartbeat(make_config(grant=False), db)
    now = in_window_today()
    hb.tick(now)
    hb.tick(now + timedelta(minutes=5))
    assert sent == []  # nothing left the machine
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_events WHERE action = ?", (BRIEF_AUDIT_ACTION,)
        ).fetchall()
    assert len(rows) == 1  # refused once, not retried into spam
    assert rows[0]["status"] == "refused"


def test_brief_blocked_under_local_only_policy(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    hb, sent = make_heartbeat(make_config(policy="local_only"), db)
    hb.tick(in_window_today())
    assert sent == []


def test_brief_not_resent_after_kernel_restart_same_day(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    hb1, sent1 = make_heartbeat(make_config(), db)
    hb1.tick(in_window_today())
    assert len(sent1) == 1

    # Fresh heartbeat over the same DB (a restart): the audit trail is the memory.
    hb2, sent2 = make_heartbeat(make_config(), db)
    hb2.tick(in_window_today(minute=50))
    assert sent2 == []


def test_brief_skipped_without_bound_chats(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    hb, sent = make_heartbeat(make_config(), db, chats=())
    hb.tick(in_window_today())
    assert sent == []
    assert hb.last_brief is not None and hb.last_brief["status"] == "skipped"


def test_brief_without_client_notifies(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    notify = FakeNotify()
    hb, _ = make_heartbeat(make_config(), db, send=None, notify=notify)
    hb.tick(in_window_today())
    assert any("brief" in n["title"].lower() for n in notify.sent)


def test_send_now_bypasses_daily_gate_not_egress(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    hb, sent = make_heartbeat(make_config(), db)
    hb.tick(in_window_today())
    assert len(sent) == 1
    result = hb.send_now()  # founder-triggered: sends again the same day
    assert len(sent) == 2
    assert result is not None and result["status"] == "ok"

    hb2, sent2 = make_heartbeat(make_config(grant=False), db)
    hb2.send_now()  # ...but never bypasses the egress gate
    assert sent2 == []
    assert hb2.last_brief is not None and hb2.last_brief["status"] == "refused"


# ---- liveness + cadence alerts ---------------------------------------------
def test_telegram_down_alerts_once_per_day(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    notify = FakeNotify()
    hb, _ = make_heartbeat(make_config(brief=False), db, running=False, notify=notify)
    now = in_window_today()
    hb.tick(now)
    hb.tick(now + timedelta(minutes=5))
    down = [n for n in notify.sent if n["title"] == "Telegram channel is down"]
    assert len(down) == 1
    assert down[0]["severity"] == "warning"

    hb.tick(now + timedelta(days=1))
    assert len([n for n in notify.sent if n["title"] == "Telegram channel is down"]) == 2


def test_no_liveness_alert_when_running_or_disabled(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    notify = FakeNotify()
    hb, _ = make_heartbeat(make_config(brief=False), db, running=True, notify=notify)
    hb.tick(in_window_today())
    assert notify.sent == []

    notify2 = FakeNotify()
    hb2, _ = make_heartbeat(make_config(enabled=False, brief=False), db, running=False, notify=notify2)
    hb2.tick(in_window_today())
    assert notify2.sent == []


def _insert_cadence_audit(db: KernelDatabase, *, age: timedelta) -> None:
    created = (datetime.now(UTC) - age).isoformat(timespec="seconds")
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO audit_events (created_at, actor, action, target, permission_tier, status, details_json)
            VALUES (?, 'kernel', 'runtime.cadence', 'cadence', 'L1_LOCAL_WRITE', 'ok', '{}')
            """,
            (created,),
        )


def test_cadence_stale_alerts_once_per_day(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    notify = FakeNotify()
    hb, _ = make_heartbeat(make_config(brief=False), db, notify=notify)
    _insert_cadence_audit(db, age=timedelta(days=7))
    now = in_window_today()
    hb.tick(now)
    hb.tick(now + timedelta(minutes=5))
    stale = [n for n in notify.sent if n["title"] == "Cadence loop is stale"]
    assert len(stale) == 1


def test_cadence_fresh_or_never_run_does_not_alert(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    notify = FakeNotify()
    hb, _ = make_heartbeat(make_config(brief=False), db, notify=notify)
    hb.tick(in_window_today())  # no cadence audit at all -> not configured, no nag
    _insert_cadence_audit(db, age=timedelta(hours=2))
    hb.tick(in_window_today(minute=50))
    assert [n for n in notify.sent if n["title"] == "Cadence loop is stale"] == []
