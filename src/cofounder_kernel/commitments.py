from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from .db import KernelDatabase, utc_now


WHO_VALUES = {"founder", "zade"}
KIND_VALUES = {"do", "monitor", "decide", "deliver"}
OPEN_STATUS = "open"
CLOSE_STATUSES = {"done", "missed", "dropped"}
CADENCE_DAYS = {"daily": 1, "weekly": 7, "monthly": 30}
DRIFT_RENEGOTIATIONS = 2
DUE_SOON_HOURS = 48


class CommitmentLedger:
    """Track what was promised — by the founder and by Zade.

    A commitment is a promise with a due date (kind "do"/"decide"/"deliver") or
    a standing watch (kind "monitor" with a cadence). The ledger tracks misses,
    drift (repeated renegotiation), and follow-ups, and the check() pass feeds
    the surfacing layer and the notification bus. Closing a commitment as
    missed is an explicit founder act — the ledger surfaces, it never quietly
    rewrites history.
    """

    def __init__(self, *, db: KernelDatabase, bus: Any | None = None):
        self.db = db
        self.bus = bus

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        who = str(payload.get("who", "founder")).strip().lower()
        kind = str(payload.get("kind", "do")).strip().lower()
        if who not in WHO_VALUES:
            raise ValueError(f"Commitment 'who' must be one of: {', '.join(sorted(WHO_VALUES))}")
        if kind not in KIND_VALUES:
            raise ValueError(f"Commitment 'kind' must be one of: {', '.join(sorted(KIND_VALUES))}")
        cadence = str(payload.get("cadence", "")).strip().lower()
        if cadence and cadence not in CADENCE_DAYS:
            raise ValueError(f"Cadence must be one of: {', '.join(sorted(CADENCE_DAYS))}")
        if kind == "monitor" and not cadence and not payload.get("due_at"):
            raise ValueError("Monitor commitments need a cadence (daily/weekly/monthly) or a due_at.")
        now = utc_now()
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO commitments (
                  created_at, updated_at, who, kind, title, detail, due_at, cadence,
                  status, source_type, source_id, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
                """,
                (
                    now,
                    now,
                    who,
                    kind,
                    payload["title"],
                    payload.get("detail", ""),
                    payload.get("due_at"),
                    cadence,
                    payload.get("source_type", "manual"),
                    payload.get("source_id"),
                    json.dumps(payload.get("metadata", {}), sort_keys=True),
                ),
            )
            commitment_id = int(cur.lastrowid)
        self._record_event(commitment_id, event="created", note=payload.get("detail", "")[:400])
        self.db.audit(
            actor="commitments",
            action="commitments.create",
            target=f"commitment:{commitment_id}",
            permission_tier="L1_MEMORY_WRITE",
            status="ok",
            details={"who": who, "kind": kind, "due_at": payload.get("due_at"), "cadence": cadence},
        )
        return self.get(commitment_id)

    def get(self, commitment_id: int) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM commitments WHERE id = ?", (commitment_id,)).fetchone()
            events = conn.execute(
                "SELECT * FROM commitment_events WHERE commitment_id = ? ORDER BY id DESC LIMIT 25",
                (commitment_id,),
            ).fetchall()
        if not row:
            raise ValueError(f"Commitment not found: {commitment_id}")
        return _commitment_from_row(row) | {"events": [_event_from_row(event) for event in events]}

    def list(
        self,
        *,
        status: str | None = None,
        who: str | None = None,
        kind: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if who:
            clauses.append("who = ?")
            params.append(who)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM commitments {where} ORDER BY COALESCE(due_at, '9999') ASC, id DESC LIMIT ?",
                params,
            ).fetchall()
        return [_commitment_from_row(row) for row in rows]

    def close(
        self,
        commitment_id: int,
        *,
        status: str,
        note: str = "",
        evidence_id: int | None = None,
    ) -> dict[str, Any]:
        if status not in CLOSE_STATUSES:
            raise ValueError(f"Close status must be one of: {', '.join(sorted(CLOSE_STATUSES))}")
        commitment = self.get(commitment_id)
        if commitment["status"] != OPEN_STATUS:
            raise ValueError(f"Commitment is already {commitment['status']}.")
        now = utc_now()
        with self.db.connect() as conn:
            if evidence_id is not None:
                evidence_row = conn.execute("SELECT id FROM founder_evidence WHERE id = ?", (evidence_id,)).fetchone()
                if not evidence_row:
                    raise ValueError(f"Evidence not found: {evidence_id}")
            evidence_ids = commitment["evidence_ids"]
            if evidence_id is not None and evidence_id not in evidence_ids:
                evidence_ids.append(evidence_id)
            conn.execute(
                """
                UPDATE commitments
                SET updated_at = ?, status = ?, closed_at = ?, closed_note = ?, evidence_ids_json = ?
                WHERE id = ?
                """,
                (now, status, now, note, json.dumps(evidence_ids, sort_keys=True), commitment_id),
            )
        self._record_event(commitment_id, event=status, note=note)
        self.db.audit(
            actor="commitments",
            action=f"commitments.{status}",
            target=f"commitment:{commitment_id}",
            permission_tier="L1_MEMORY_WRITE",
            status="ok",
            details={"note": note[:400], "evidence_id": evidence_id, "was_overdue": _is_overdue(commitment, utc_now())},
        )
        return self.get(commitment_id)

    def renegotiate(self, commitment_id: int, *, due_at: str, note: str = "") -> dict[str, Any]:
        commitment = self.get(commitment_id)
        if commitment["status"] != OPEN_STATUS:
            raise ValueError(f"Commitment is already {commitment['status']}.")
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE commitments
                SET updated_at = ?, due_at = ?, renegotiation_count = renegotiation_count + 1
                WHERE id = ?
                """,
                (utc_now(), due_at, commitment_id),
            )
        self._record_event(
            commitment_id,
            event="renegotiated",
            note=note or f"Due date moved from {commitment['due_at']} to {due_at}.",
            metadata={"previous_due_at": commitment["due_at"], "new_due_at": due_at},
        )
        updated = self.get(commitment_id)
        if updated["renegotiation_count"] >= DRIFT_RENEGOTIATIONS:
            self._record_event(
                commitment_id,
                event="drift_detected",
                note=f"Renegotiated {updated['renegotiation_count']} time(s). This commitment is drifting.",
            )
        return updated

    def check(self) -> dict[str, Any]:
        """The accountability pass: flag overdue, due-soon, drifting, and monitor-due commitments.

        Records at most one follow-up per commitment per day and notifies on
        newly overdue promises. It never closes anything — misses are the
        founder's call to record.
        """
        now = utc_now()
        today = now[:10]
        open_items = self.list(status=OPEN_STATUS, limit=500)
        overdue: list[dict[str, Any]] = []
        due_soon: list[dict[str, Any]] = []
        drifting: list[dict[str, Any]] = []
        monitor_due: list[dict[str, Any]] = []
        follow_ups = 0
        for item in open_items:
            if item["renegotiation_count"] >= DRIFT_RENEGOTIATIONS:
                drifting.append(item)
            if _is_overdue(item, now):
                overdue.append(item)
                if self._needs_follow_up(item, today):
                    self._record_follow_up(item, note=f"Overdue since {item['due_at']}.")
                    follow_ups += 1
                    self._notify_overdue(item)
            elif _is_due_soon(item, now):
                due_soon.append(item)
            elif _monitor_is_due(item, now):
                monitor_due.append(item)
                if self._needs_follow_up(item, today):
                    self._record_follow_up(item, note=f"Monitoring pass due ({item['cadence']}).")
                    follow_ups += 1
        summary = {
            "generated_at": now,
            "open": len(open_items),
            "overdue": [_brief(item) for item in overdue],
            "due_soon": [_brief(item) for item in due_soon],
            "drifting": [_brief(item) for item in drifting],
            "monitor_due": [_brief(item) for item in monitor_due],
            "follow_ups_recorded": follow_ups,
        }
        self.db.audit(
            actor="commitments",
            action="commitments.check",
            target="commitments",
            permission_tier="L1_MEMORY_WRITE",
            status="ok",
            details={
                "open": len(open_items),
                "overdue": len(overdue),
                "due_soon": len(due_soon),
                "drifting": len(drifting),
                "monitor_due": len(monitor_due),
                "follow_ups_recorded": follow_ups,
            },
        )
        return summary

    def attention_items(self) -> dict[str, list[dict[str, Any]]]:
        """Raw material for the surfacing layer, computed without side effects."""
        now = utc_now()
        open_items = self.list(status=OPEN_STATUS, limit=500)
        return {
            "overdue": [item for item in open_items if _is_overdue(item, now)],
            "drifting": [item for item in open_items if item["renegotiation_count"] >= DRIFT_RENEGOTIATIONS],
        }

    def _needs_follow_up(self, item: dict[str, Any], today: str) -> bool:
        last = item.get("last_follow_up_at") or ""
        return not last.startswith(today)

    def _record_follow_up(self, item: dict[str, Any], *, note: str) -> None:
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE commitments
                SET updated_at = ?, follow_up_count = follow_up_count + 1, last_follow_up_at = ?
                WHERE id = ?
                """,
                (utc_now(), utc_now(), item["id"]),
            )
        self._record_event(item["id"], event="follow_up", note=note)

    def _notify_overdue(self, item: dict[str, Any]) -> None:
        if self.bus is None:
            return
        owner = "You" if item["who"] == "founder" else "Zade"
        try:
            self.bus.notify(
                topic="commitment.overdue",
                severity="warning",
                title=f"Commitment overdue: {item['title']}",
                body=f"{owner} committed to '{item['title']}' by {item['due_at']}. It is still open.",
                source="commitments",
                dedupe_key=f"commitment:{item['id']}:overdue:{utc_now()[:10]}",
            )
        except Exception:
            pass

    def _record_event(self, commitment_id: int, *, event: str, note: str = "", metadata: dict[str, Any] | None = None) -> None:
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO commitment_events (created_at, commitment_id, event, note, metadata_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (utc_now(), commitment_id, event, note, json.dumps(metadata or {}, sort_keys=True)),
            )


def _due_moment(due: str) -> datetime:
    """The instant a commitment is actually due. A date-only value ("2026-07-12")
    means end of that day, so a commitment due *today* is not overdue at 00:00."""
    text = str(due).strip()
    if len(text) == 10 and text[4:5] == "-" and text[7:8] == "-":
        return _parse(f"{text}T23:59:59+00:00")
    return _parse(text)


def _is_overdue(item: dict[str, Any], now: str) -> bool:
    due = item.get("due_at")
    if not due or item["status"] != OPEN_STATUS:
        return False
    try:
        return _parse(now) > _due_moment(str(due))
    except ValueError:
        return False


def _is_due_soon(item: dict[str, Any], now: str) -> bool:
    due = item.get("due_at")
    if not due:
        return False
    try:
        due_parsed = _due_moment(str(due))
        now_parsed = _parse(now)
    except ValueError:
        return False
    delta_hours = (due_parsed - now_parsed).total_seconds() / 3600
    return 0 <= delta_hours <= DUE_SOON_HOURS


def _monitor_is_due(item: dict[str, Any], now: str) -> bool:
    if item["kind"] != "monitor" or not item.get("cadence"):
        return False
    window_days = CADENCE_DAYS.get(str(item["cadence"]), 7)
    last = item.get("last_follow_up_at") or item["created_at"]
    try:
        last_parsed = _parse(str(last))
        now_parsed = _parse(now)
    except ValueError:
        return False
    return (now_parsed - last_parsed).total_seconds() >= window_days * 86400


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _brief(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item["id"],
        "who": item["who"],
        "kind": item["kind"],
        "title": item["title"],
        "due_at": item.get("due_at"),
        "cadence": item.get("cadence", ""),
        "renegotiation_count": item["renegotiation_count"],
        "follow_up_count": item["follow_up_count"],
    }


def _commitment_from_row(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["evidence_ids"] = json.loads(data.pop("evidence_ids_json") or "[]")
    data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
    return data


def _event_from_row(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
    return data
