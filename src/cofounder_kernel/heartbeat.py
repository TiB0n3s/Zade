"""Kernel heartbeat: periodic liveness checks + the scheduled morning brief.

Two silent-failure classes bit in the same week: the Telegram adapter came back
from a kernel restart with no token (``enabled=true, running=false``) and nothing
said so, and the cadence loop sat stale for seven days behind a green health
readout. Both share one root cause: the kernel had no periodic self-check that
NOTIFIES. This module is that ticker.

Three jobs, one daemon thread:

1. **Channel liveness** — an enabled Telegram adapter that is not running gets a
   warning notification (once per local day, after a startup grace period).
2. **Cadence staleness** — a ``runtime.cadence`` audit trail older than the
   threshold gets a warning notification (once per local day). A kernel that has
   never run cadence is not nagged — absence means "not configured", not "broke".
3. **Morning brief** — when ``[telegram] brief_enabled``, a digest composed from
   the founder dashboard is pushed once per local day at ``brief_time`` to every
   bound (founder-authenticated) Telegram chat.

Governance of the brief push
----------------------------
The digest is Zade-composed reply text addressed to the bound founder over the
founder-bound channel — the same artifact class as any governed conversational
reply, so it is gated as ``REPLY_TEXT -> telegram`` (the existing
``reply_text:telegram`` standing grant), NOT as ``FOUNDER_BRIEF`` (that class is
the curated advisor artifact for cloud review, and ``founder_brief -> CHANNEL``
stays FORBIDDEN). Recipients come only from active channel bindings, which by
construction are founder identities; no binding, no send. One attempt per day —
a refused or failed attempt is audited and notified, never retried into spam.
"""
from __future__ import annotations

import secrets
import threading
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any, Callable

from .config import KernelConfig
from .egress import DataClass, EgressPolicy, EgressRequest

TICK_SECONDS = 60.0
# Don't alert on a channel that is still connecting after boot.
STARTUP_GRACE_SECONDS = 300.0
CADENCE_MAX_AGE_HOURS = 30
# A brief that could not go out in the morning should not arrive at night.
BRIEF_WINDOW_HOURS = 6
BRIEF_AUDIT_ACTION = "brief.telegram"


def compose_digest(brief: dict[str, Any], *, now_local: datetime, max_chars: int) -> str:
    """Founder dashboard -> a chat-sized morning digest.

    Unlike ``founder.brief()`` (an advisor artifact that must frame absence),
    this is a founder-facing chat message: empty sections are dropped, not
    marked — the founder knows their own sparse data.
    """
    dashboard = brief.get("dashboard") or {}
    lines: list[str] = [f"Morning brief — {now_local:%a %b %d}"]

    health = dashboard.get("company_health")
    confidence = dashboard.get("overall_confidence")
    if health:
        summary = f"Health: {health}"
        if confidence is not None:
            summary += f" | confidence {confidence}"
        lines.append(summary)
    focus = dashboard.get("one_thing_that_matters_most_today")
    if focus:
        lines.append(f"Focus today: {focus}")

    def section(title: str, items: list[str]) -> None:
        cleaned = [i for i in items if i]
        if cleaned:
            lines.extend(["", title, *(f"- {item}" for item in cleaned[:3])])

    section(
        "Top objectives:",
        [
            str(o.get("objective") or "")
            + (f" (risk: {o['current_risk']})" if o.get("current_risk") else "")
            for o in dashboard.get("top_objectives") or []
        ],
    )
    section(
        "Decisions waiting:",
        [str(d.get("problem") or "") for d in dashboard.get("decisions_waiting") or []],
    )
    section(
        "Risks increasing:",
        [
            str(r.get("objective") or "")
            + (f" (risk: {r['current_risk']})" if r.get("current_risk") else "")
            for r in dashboard.get("critical_risks") or []
        ],
    )
    approval = (dashboard.get("approval_pressure") or {}).get("headline")
    if approval:
        lines.extend(["", f"Approvals: {approval}"])
    return "\n".join(lines).strip()[:max_chars]


class KernelHeartbeat:
    """Once-a-minute self-check loop; see module docstring for the three jobs."""

    def __init__(
        self,
        config: KernelConfig,
        *,
        db: Any,
        founder: Any,
        notify: Any,
        telegram_running: Callable[[], bool],
        telegram_chat_ids: Callable[[], list[int]],
        send_telegram: Callable[[int, str], None] | None,
    ):
        self.config = config
        self.tg = config.telegram
        self.db = db
        self.founder = founder
        self.notify = notify
        self._telegram_running = telegram_running
        self._telegram_chat_ids = telegram_chat_ids
        self._send_telegram = send_telegram
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_monotonic: float | None = None
        self._alerted_telegram_on: date | None = None
        self._alerted_cadence_on: date | None = None
        self._brief_attempted_on: date | None = None
        self.last_brief: dict[str, Any] | None = None

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        self._stop.clear()
        self._started_monotonic = time.monotonic()
        self._thread = threading.Thread(target=self._run_forever, name="kernel-heartbeat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                pass  # a failing check must not kill the ticker
            self._stop.wait(TICK_SECONDS)

    # -- checks ------------------------------------------------------------
    def tick(self, now_local: datetime | None = None) -> None:
        now_local = now_local or datetime.now()
        self._check_telegram_liveness(now_local)
        self._check_cadence_staleness(now_local)
        self._maybe_send_brief(now_local)

    def _past_grace(self) -> bool:
        if self._started_monotonic is None:
            return True  # direct tick() (tests, manual) — no boot grace to honor
        return time.monotonic() - self._started_monotonic >= STARTUP_GRACE_SECONDS

    def _check_telegram_liveness(self, now_local: datetime) -> None:
        if not self.tg.enabled or self._telegram_running() or not self._past_grace():
            return
        today = now_local.date()
        if self._alerted_telegram_on == today:
            return
        self._alerted_telegram_on = today
        self.notify.notify(
            topic="channels",
            severity="warning",
            title="Telegram channel is down",
            body=(
                "[telegram] is enabled but the adapter is not running — the bot "
                "token is missing from the kernel's environment or the connection "
                "keeps failing. Inbound founder messages are NOT being received. "
                "If the token was set after the kernel's parent process started, "
                "restart the kernel from a fresh shell."
            ),
            source="heartbeat",
            dedupe_key=f"heartbeat.telegram_down:{today.isoformat()}",
        )

    def _check_cadence_staleness(self, now_local: datetime) -> None:
        latest = self._latest_audit("runtime.cadence")
        if not latest:
            return  # never ran -> not configured; nothing to nag about
        created_at = str(latest["created_at"])
        try:
            parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            return
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        age_hours = (datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds() / 3600
        if age_hours <= CADENCE_MAX_AGE_HOURS:
            return
        today = now_local.date()
        if self._alerted_cadence_on == today:
            return
        self._alerted_cadence_on = today
        self.notify.notify(
            topic="ops",
            severity="warning",
            title="Cadence loop is stale",
            body=(
                f"Last cadence run was {age_hours / 24:.1f} days ago "
                f"(threshold {CADENCE_MAX_AGE_HOURS}h). The 'Zade Local Cadence' "
                "scheduled task may not be firing — run Run-Zade-Cadence.cmd and "
                "check the task's trigger."
            ),
            source="heartbeat",
            dedupe_key=f"heartbeat.cadence_stale:{today.isoformat()}",
        )

    # -- morning brief ------------------------------------------------------
    def send_now(self) -> dict[str, Any] | None:
        """Founder-triggered push: skips the schedule/once-per-day gate, keeps
        every governance gate (bound chats only, egress decision, audit)."""
        self._send_brief(datetime.now())
        return self.last_brief

    def _maybe_send_brief(self, now_local: datetime) -> None:
        if not (self.tg.enabled and self.tg.brief_enabled):
            return
        if not self._in_brief_window(now_local):
            return
        today = now_local.date()
        if self._brief_attempted_on == today:
            return
        if self._brief_attempted_today_in_audit(today):
            self._brief_attempted_on = today
            return
        # One attempt per day, whatever the outcome — failures notify, not retry.
        self._brief_attempted_on = today
        self._send_brief(now_local)

    def _in_brief_window(self, now_local: datetime) -> bool:
        try:
            hour, minute = (int(part) for part in self.tg.brief_time.split(":", 1))
        except ValueError:
            return False
        scheduled = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return scheduled <= now_local < scheduled + timedelta(hours=BRIEF_WINDOW_HOURS)

    def _send_brief(self, now_local: datetime) -> None:
        chat_ids = self._telegram_chat_ids()
        if not chat_ids:
            self._record_brief("skipped", "no bound founder chats", chat_count=0)
            return
        if self._send_telegram is None:
            self._record_brief("error", "no telegram client (token missing)", chat_count=0)
            self.notify.notify(
                topic="channels",
                severity="warning",
                title="Morning brief could not be sent",
                body="brief_enabled is on but the Telegram client has no token.",
                source="heartbeat",
                dedupe_key=f"heartbeat.brief_failed:{now_local.date().isoformat()}",
            )
            return
        decision = EgressPolicy.from_config(self.config).decide(
            EgressRequest(
                request_id=secrets.token_hex(8),
                data_class=DataClass.REPLY_TEXT,
                vendor="telegram",
                purpose="telegram.morning_brief",
            )
        )
        if not decision.allowed:
            self._record_brief("refused", "egress denied", chat_count=0, egress=decision.audit_record())
            return
        text = compose_digest(
            self.founder.brief(), now_local=now_local, max_chars=self.tg.max_reply_chars
        )
        sent = 0
        error = ""
        for chat_id in chat_ids:
            try:
                self._send_telegram(chat_id, text)
                sent += 1
            except Exception as exc:
                error = str(exc)[:200]
        status = "ok" if sent else "error"
        self._record_brief(
            status,
            error or f"delivered to {sent} chat(s)",
            chat_count=sent,
            egress=decision.audit_record(),
            chars=len(text),
        )
        if not sent:
            self.notify.notify(
                topic="channels",
                severity="warning",
                title="Morning brief failed to send",
                body=f"Telegram sendMessage failed for all bound chats: {error}",
                source="heartbeat",
                dedupe_key=f"heartbeat.brief_failed:{now_local.date().isoformat()}",
            )

    def _record_brief(self, status: str, detail: str, **details: Any) -> None:
        self.last_brief = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "status": status, "detail": detail}
        try:
            self.db.audit(
                actor="heartbeat",
                action=BRIEF_AUDIT_ACTION,
                target="telegram",
                permission_tier="L3_EXTERNAL_ACTION",
                status=status,
                details={"detail": detail, **details},
            )
        except Exception:
            pass

    def _brief_attempted_today_in_audit(self, today: date) -> bool:
        """Survive a kernel restart without double-sending: any brief attempt
        audited today (local) counts, whatever its outcome."""
        latest = self._latest_audit(BRIEF_AUDIT_ACTION)
        if not latest:
            return False
        try:
            parsed = datetime.fromisoformat(str(latest["created_at"]).replace("Z", "+00:00"))
        except ValueError:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone().date() == today

    def _latest_audit(self, action: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM audit_events WHERE action = ? ORDER BY id DESC LIMIT 1",
                (action,),
            ).fetchone()
        return dict(row) if row else None
