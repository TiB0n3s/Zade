from __future__ import annotations

import json
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any

from .db import KernelDatabase, utc_now


SEVERITIES = ("info", "warning", "critical")
SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}
DEDUPE_WINDOW_MINUTES = 60
BODY_MAX_CHARS = 2000

DEFAULT_CHANNELS = [
    {"channel": "ui", "enabled": True, "min_severity": "info", "rate_limit_per_hour": 120},
    {"channel": "voice", "enabled": False, "min_severity": "warning", "rate_limit_per_hour": 6},
    {
        "channel": "sms",
        "enabled": False,
        "min_severity": "critical",
        "rate_limit_per_hour": 10,
        "quiet_start": "22:00",
        "quiet_end": "07:00",
    },
]


class NotificationBus:
    """One internal notify() for every producer; channels and rules decide egress.

    Producers (surfacing, commitments, action plans, anything else) call
    notify() and never talk to a channel directly. Channel rules — enabled,
    minimum severity, quiet hours, hourly rate limits, and a recipient
    whitelist for outbound channels — are founder configuration. Enabling an
    outbound channel (sms) is a standing founder grant, bounded by those rules;
    critical notifications bypass quiet hours but never the whitelist or rate
    limit. Every suppression is recorded, never silent.
    """

    def __init__(self, *, db: KernelDatabase, voice: Any | None = None):
        self.db = db
        self.voice = voice
        self.ensure_default_channels()

    def ensure_default_channels(self) -> None:
        now = utc_now()
        with self.db.connect() as conn:
            for spec in DEFAULT_CHANNELS:
                existing = conn.execute(
                    "SELECT id FROM notification_channels WHERE channel = ?",
                    (spec["channel"],),
                ).fetchone()
                if existing:
                    continue
                conn.execute(
                    """
                    INSERT INTO notification_channels (
                      created_at, updated_at, channel, enabled, min_severity, quiet_start,
                      quiet_end, rate_limit_per_hour, recipients_json, config_json, metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, '[]', '{}', '{}')
                    """,
                    (
                        now,
                        now,
                        spec["channel"],
                        int(spec.get("enabled", False)),
                        spec.get("min_severity", "info"),
                        spec.get("quiet_start", ""),
                        spec.get("quiet_end", ""),
                        int(spec.get("rate_limit_per_hour", 30)),
                    ),
                )

    def notify(
        self,
        *,
        topic: str,
        title: str,
        body: str = "",
        severity: str = "info",
        source: str = "kernel",
        dedupe_key: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        severity = severity.strip().lower()
        if severity not in SEVERITY_RANK:
            raise ValueError(f"Severity must be one of: {', '.join(SEVERITIES)}")
        now = utc_now()
        if dedupe_key and self._recent_duplicate(dedupe_key, now):
            notification_id = self._insert_notification(
                topic=topic,
                severity=severity,
                title=title,
                body=body,
                source=source,
                dedupe_key=dedupe_key,
                status="suppressed",
                suppressed_reason="duplicate_within_window",
                metadata=metadata,
            )
            return self.get(notification_id)
        notification_id = self._insert_notification(
            topic=topic,
            severity=severity,
            title=title,
            body=body,
            source=source,
            dedupe_key=dedupe_key,
            status="queued",
            metadata=metadata,
        )
        delivered_any = False
        for channel in self.list_channels():
            outcome, detail = self._deliver(channel, severity=severity, title=title, body=body)
            if outcome is None:
                continue  # channel disabled or below severity: not part of this notification's story
            self._record_delivery(notification_id, channel["channel"], outcome, detail)
            if outcome == "delivered":
                delivered_any = True
        status = "delivered" if delivered_any else "suppressed"
        suppressed_reason = "" if delivered_any else "no_channel_delivered"
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE notifications SET status = ?, suppressed_reason = ? WHERE id = ?",
                (status, suppressed_reason, notification_id),
            )
        return self.get(notification_id)

    def get(self, notification_id: int) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM notifications WHERE id = ?", (notification_id,)).fetchone()
            deliveries = conn.execute(
                "SELECT * FROM notification_deliveries WHERE notification_id = ? ORDER BY id ASC",
                (notification_id,),
            ).fetchall()
        if not row:
            raise ValueError(f"Notification not found: {notification_id}")
        return _notification_from_row(row) | {"deliveries": [dict(item) for item in deliveries]}

    def list(
        self,
        *,
        status: str | None = None,
        topic: str | None = None,
        unread_only: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if topic:
            clauses.append("topic = ?")
            params.append(topic)
        if unread_only:
            clauses.append("read_at IS NULL")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM notifications {where} ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()
        return [_notification_from_row(row) for row in rows]

    def mark_read(self, notification_id: int) -> dict[str, Any]:
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE notifications SET read_at = ? WHERE id = ? AND read_at IS NULL",
                (utc_now(), notification_id),
            )
        return self.get(notification_id)

    def list_channels(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM notification_channels ORDER BY channel ASC").fetchall()
        return [_channel_from_row(row) for row in rows]

    def update_channel(self, channel: str, payload: dict[str, Any]) -> dict[str, Any]:
        existing = next((item for item in self.list_channels() if item["channel"] == channel), None)
        if not existing:
            raise ValueError(f"Unknown notification channel: {channel}")
        min_severity = payload.get("min_severity")
        if min_severity is not None and str(min_severity) not in SEVERITY_RANK:
            raise ValueError(f"min_severity must be one of: {', '.join(SEVERITIES)}")
        for field in ("quiet_start", "quiet_end"):
            value = payload.get(field)
            if value:
                _parse_hhmm(str(value))
        updates: dict[str, Any] = {}
        if payload.get("enabled") is not None:
            updates["enabled"] = int(bool(payload["enabled"]))
        if min_severity is not None:
            updates["min_severity"] = str(min_severity)
        for field in ("quiet_start", "quiet_end"):
            if payload.get(field) is not None:
                updates[field] = str(payload[field])
        if payload.get("rate_limit_per_hour") is not None:
            updates["rate_limit_per_hour"] = max(1, int(payload["rate_limit_per_hour"]))
        if payload.get("recipients") is not None:
            updates["recipients_json"] = json.dumps([str(item) for item in payload["recipients"]], sort_keys=True)
        if payload.get("config") is not None:
            updates["config_json"] = json.dumps(payload["config"], sort_keys=True)
        if updates:
            assignments = ", ".join(f"{key} = ?" for key in updates)
            with self.db.connect() as conn:
                conn.execute(
                    f"UPDATE notification_channels SET updated_at = ?, {assignments} WHERE channel = ?",
                    (utc_now(), *updates.values(), channel),
                )
        self.db.audit(
            actor="notify",
            action="notify.channel.update",
            target=channel,
            permission_tier="L1_MEMORY_WRITE",
            status="ok",
            details={"updated_fields": sorted(updates.keys())},
        )
        return next(item for item in self.list_channels() if item["channel"] == channel)

    def _deliver(self, channel: dict[str, Any], *, severity: str, title: str, body: str) -> tuple[str | None, str]:
        if not channel["enabled"]:
            return None, ""
        if SEVERITY_RANK[severity] < SEVERITY_RANK.get(channel["min_severity"], 0):
            return None, ""
        if severity != "critical" and _in_quiet_hours(_local_hhmm(), channel["quiet_start"], channel["quiet_end"]):
            return "suppressed", "quiet_hours"
        if self._delivered_last_hour(channel["channel"]) >= channel["rate_limit_per_hour"]:
            return "suppressed", "rate_limited"
        name = channel["channel"]
        if name == "ui":
            return "delivered", "notification feed"
        if name == "voice":
            return self._deliver_voice(title=title, body=body)
        if name == "sms":
            return self._deliver_sms(channel, title=title, body=body)
        return "failed", f"no adapter for channel {name}"

    def _deliver_voice(self, *, title: str, body: str) -> tuple[str, str]:
        if self.voice is None:
            return "failed", "voice service not wired"
        try:
            spoken = self.voice.speak(text=f"{title}. {body}".strip()[:BODY_MAX_CHARS])
            return "delivered", str(spoken.get("audio_path", "spoken"))
        except Exception as exc:
            return "failed", str(exc)[:200]

    def _deliver_sms(self, channel: dict[str, Any], *, title: str, body: str) -> tuple[str, str]:
        config = channel["config"]
        gateway_url = str(config.get("gateway_url", "")).strip()
        recipient = str(config.get("to", "")).strip()
        if not gateway_url:
            return "failed", "sms gateway not configured (set config.gateway_url)"
        if not recipient:
            return "failed", "sms recipient not configured (set config.to)"
        if recipient not in channel["recipients"]:
            return "suppressed", f"recipient not in whitelist: {recipient}"
        payload = json.dumps({"to": recipient, "title": title, "body": body[:BODY_MAX_CHARS]}).encode("utf-8")
        request = urllib.request.Request(
            gateway_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - founder-configured gateway
                return "delivered", f"gateway HTTP {response.status}"
        except Exception as exc:
            return "failed", str(exc)[:200]

    def _delivered_last_hour(self, channel: str) -> int:
        cutoff = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count FROM notification_deliveries
                WHERE channel = ? AND status = 'delivered' AND created_at > ?
                """,
                (channel, cutoff),
            ).fetchone()
        return int(row["count"]) if row else 0

    def _recent_duplicate(self, dedupe_key: str, now: str) -> bool:
        cutoff = (datetime.now(UTC) - timedelta(minutes=DEDUPE_WINDOW_MINUTES)).isoformat(timespec="seconds")
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT id FROM notifications
                WHERE dedupe_key = ? AND created_at > ? AND status != 'suppressed'
                ORDER BY id DESC LIMIT 1
                """,
                (dedupe_key, cutoff),
            ).fetchone()
        return row is not None

    def _insert_notification(
        self,
        *,
        topic: str,
        severity: str,
        title: str,
        body: str,
        source: str,
        dedupe_key: str,
        status: str,
        suppressed_reason: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> int:
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO notifications (
                  created_at, topic, severity, title, body, source, dedupe_key,
                  status, suppressed_reason, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    topic,
                    severity,
                    title,
                    body[:BODY_MAX_CHARS],
                    source,
                    dedupe_key,
                    status,
                    suppressed_reason,
                    json.dumps(metadata or {}, sort_keys=True),
                ),
            )
            return int(cur.lastrowid)

    def _record_delivery(self, notification_id: int, channel: str, status: str, detail: str) -> None:
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO notification_deliveries (created_at, notification_id, channel, status, detail)
                VALUES (?, ?, ?, ?, ?)
                """,
                (utc_now(), notification_id, channel, status, detail[:400]),
            )


def _local_hhmm() -> str:
    return datetime.now().strftime("%H:%M")


def _in_quiet_hours(now_hhmm: str, start: str, end: str) -> bool:
    if not start or not end:
        return False
    now_minutes = _parse_hhmm(now_hhmm)
    start_minutes = _parse_hhmm(start)
    end_minutes = _parse_hhmm(end)
    if start_minutes == end_minutes:
        return False
    if start_minutes < end_minutes:
        return start_minutes <= now_minutes < end_minutes
    # Overnight window, e.g. 22:00 -> 07:00.
    return now_minutes >= start_minutes or now_minutes < end_minutes


def _parse_hhmm(value: str) -> int:
    parts = value.strip().split(":")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise ValueError(f"Time must be HH:MM, got: {value!r}")
    hours, minutes = int(parts[0]), int(parts[1])
    if hours > 23 or minutes > 59:
        raise ValueError(f"Time must be HH:MM, got: {value!r}")
    return hours * 60 + minutes


def _notification_from_row(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
    return data


def _channel_from_row(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["enabled"] = bool(data["enabled"])
    data["recipients"] = json.loads(data.pop("recipients_json") or "[]")
    data["config"] = json.loads(data.pop("config_json") or "{}")
    data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
    return data
