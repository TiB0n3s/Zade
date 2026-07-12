from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from .config import KernelConfig
from .db import KernelDatabase, utc_now
from .ollama import OllamaClient


ACTIVE_EXPERIMENT_STATUSES = ("active", "running", "revised")

INTEGRITY_SEVERITY_SCORES = {"red": 80, "orange": 70, "yellow": 56}
CONFLICT_SEVERITY_SCORES = {"red": 78, "orange": 68, "yellow": 62}


class SurfacingService:
    """Proactive attention surfacing for the founder.

    Deterministically scans the operating layer for signals that need founder
    attention, ranks them, and composes an initiated brief: what changed since
    the last brief, the one thing that matters most, and the risk that has been
    open the longest. Detection never calls a model, so the queue is testable
    and honest; narration is optional and clearly separated.
    """

    def __init__(self, *, config: KernelConfig, db: KernelDatabase, ollama: OllamaClient):
        self.config = config
        self.db = db
        self.ollama = ollama

    def scan(self) -> dict[str, Any]:
        since = self._last_brief_at()
        items: list[dict[str, Any]] = []
        items.extend(self._overdue_kill_criteria())
        items.extend(self._open_integrity_warnings())
        items.extend(self._experiments_needing_decision())
        items.extend(self._open_thesis_conflicts())
        items.extend(self._overdue_predictions())
        items.extend(self._decisions_due_for_revisit())
        items.extend(self._confidence_drops(since))
        items.extend(self._overrides_due_for_review())
        items.extend(self._assumptions_due_for_review())
        items.extend(self._experiments_needing_evidence())
        items.extend(self._pending_approvals())
        items.extend(self._pending_connector_items())
        items.sort(key=lambda item: (-item["score"], item["kind"], item.get("subject_id") or 0))
        one_thing = items[0] if items else None
        return {
            "generated_at": utc_now(),
            "count": len(items),
            "items": items,
            "one_thing": _item_headline(one_thing) if one_thing else "Nothing needs founder attention right now.",
            "underweighted": self._underweighted(items),
            "last_brief_at": since,
        }

    def brief(self, *, narrate: bool = False, force: bool = False) -> dict[str, Any]:
        since = self._last_brief_at()
        scan = self.scan()
        changes = self._changes_since(since)
        text = self._compose(scan, changes, since)
        narrative = self._narrate(text) if narrate else ""
        quiet = scan["count"] == 0
        memory_id = None
        if not quiet or force:
            content = text if not narrative else f"{text}\n\nExecutive read:\n{narrative}"
            memory_id = self.db.add_memory(
                kind="initiated_brief",
                title=f"{self.config.identity.name} Initiated Brief {utc_now()[:10]}",
                content=content,
                source="surfacing",
                metadata={"item_count": scan["count"], "one_thing": scan["one_thing"], "changes": changes},
            )
        event_id = self._log_event(
            response=scan["one_thing"],
            details={
                "count": scan["count"],
                "one_thing": scan["one_thing"],
                "underweighted": scan["underweighted"],
                "changes": changes,
                "memory_id": memory_id,
                "quiet": quiet,
                "kinds": sorted({item["kind"] for item in scan["items"]}),
            },
        )
        self.db.audit(
            actor="surfacing",
            action="surfacing.brief",
            target="founder_attention",
            permission_tier="L1_MEMORY_WRITE",
            status="quiet" if quiet else "ok",
            details={"event_id": event_id, "memory_id": memory_id, "item_count": scan["count"]},
        )
        return {
            "event_id": event_id,
            "memory_id": memory_id,
            "quiet": quiet,
            "generated_at": scan["generated_at"],
            "since_last_brief": since,
            "changes": changes,
            "count": scan["count"],
            "items": scan["items"],
            "one_thing": scan["one_thing"],
            "underweighted": scan["underweighted"],
            "brief": text,
            "narrative": narrative,
        }

    def _overdue_kill_criteria(self) -> list[dict[str, Any]]:
        today = utc_now()[:10]
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM kill_criteria
                WHERE status = 'active' AND by_date IS NOT NULL AND by_date < ?
                ORDER BY by_date ASC LIMIT 50
                """,
                (today,),
            ).fetchall()
        items = []
        for row in rows:
            age = _age_days(str(row["by_date"]))
            metric = str(row["metric"]) or f"{row['subject_type']}:{row['subject_id']}"
            items.append(
                _item(
                    kind="kill_criteria_overdue",
                    severity="red",
                    score=min(98, 90 + min(age // 7, 8)),
                    title=f"Kill criterion past its date: {metric} {row['threshold']}".strip(),
                    detail=(
                        f"You committed to a kill/keep decision on {row['subject_type']} {row['subject_id']} "
                        f"by {row['by_date']} ({age} day(s) ago). No decision is recorded."
                    ),
                    subject_type=str(row["subject_type"]),
                    subject_id=int(row["subject_id"]),
                    recommended_action="Decide now: kill, keep with evidence, or reset the criterion with a reason.",
                    opened_at=str(row["by_date"]),
                )
            )
        return items

    def _open_integrity_warnings(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM integrity_warnings WHERE status = 'open' ORDER BY id DESC LIMIT 100"
            ).fetchall()
        return [
            _item(
                kind="integrity_warning",
                severity=str(row["severity"]),
                score=INTEGRITY_SEVERITY_SCORES.get(str(row["severity"]), 55),
                title=str(row["message"]),
                detail=str(row["recommendation"]) or "Resolve the integrity gap.",
                subject_type=str(row["subject_type"]),
                subject_id=int(row["subject_id"]) if row["subject_id"] is not None else None,
                recommended_action=str(row["recommendation"]) or "Resolve the integrity gap.",
                opened_at=str(row["created_at"]),
            )
            for row in rows
        ]

    def _experiments_needing_decision(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM founder_experiments WHERE status = 'needs_decision' ORDER BY id ASC LIMIT 25"
            ).fetchall()
        return [
            _item(
                kind="experiment_needs_decision",
                severity="orange",
                score=74,
                title=f"Experiment awaiting decision: {row['title']}",
                detail=f"Decision rule: {row['decision_rule'] or 'not specified'}.",
                subject_type="experiment",
                subject_id=int(row["id"]),
                recommended_action="Make the continue/revise/kill call from collected evidence.",
                opened_at=str(row["updated_at"]),
            )
            for row in rows
        ]

    def _open_thesis_conflicts(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM thesis_conflicts WHERE status = 'open' ORDER BY id DESC LIMIT 50"
            ).fetchall()
        return [
            _item(
                kind="thesis_conflict",
                severity=str(row["severity"]) or "yellow",
                score=CONFLICT_SEVERITY_SCORES.get(str(row["severity"]), 62),
                title=f"Evidence contradicts an assumption: {row['affected_assumption'] or row['original_assumption']}",
                detail=str(row["new_evidence"]),
                subject_type="thesis_conflict",
                subject_id=int(row["id"]),
                recommended_action=str(row["recommended_response"]) or "Reconcile the conflict: revise the assumption or discount the evidence.",
                opened_at=str(row["created_at"]),
            )
            for row in rows
        ]

    def _overdue_predictions(self) -> list[dict[str, Any]]:
        now = utc_now()
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM founder_predictions
                WHERE result = 'open' AND due_at IS NOT NULL AND due_at <= ?
                ORDER BY due_at ASC LIMIT 25
                """,
                (now,),
            ).fetchall()
        return [
            _item(
                kind="prediction_overdue",
                severity="yellow",
                score=66,
                title=f"Prediction due for scoring: {row['prediction']}",
                detail=f"Stated at p={row['probability']}, due {row['due_at']}. Unscored predictions erode calibration.",
                subject_type="prediction",
                subject_id=int(row["id"]),
                recommended_action="Score it true/false and record the lesson.",
                opened_at=str(row["due_at"]),
            )
            for row in rows
        ]

    def _decisions_due_for_revisit(self) -> list[dict[str, Any]]:
        now = utc_now()
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM decision_memos
                WHERE status = 'open' AND revisit_date IS NOT NULL AND revisit_date <= ?
                ORDER BY revisit_date ASC LIMIT 25
                """,
                (now,),
            ).fetchall()
        return [
            _item(
                kind="decision_revisit_due",
                severity="yellow",
                score=60,
                title=f"Decision due for revisit: {row['problem']}",
                detail=f"Recommendation was '{row['recommendation']}' — revisit date {row['revisit_date']} has passed.",
                subject_type="decision_memo",
                subject_id=int(row["id"]),
                recommended_action="Confirm, revise, or close the decision with what you now know.",
                opened_at=str(row["revisit_date"]),
            )
            for row in rows
        ]

    def _confidence_drops(self, since: str | None) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            if since:
                rows = conn.execute(
                    """
                    SELECT * FROM confidence_events
                    WHERE created_at > ? AND previous_confidence IS NOT NULL AND new_confidence < previous_confidence
                    ORDER BY id DESC LIMIT 20
                    """,
                    (since,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM confidence_events
                    WHERE previous_confidence IS NOT NULL AND new_confidence < previous_confidence
                    ORDER BY id DESC LIMIT 20
                    """
                ).fetchall()
        items = []
        for row in rows:
            drop = int(row["previous_confidence"]) - int(row["new_confidence"])
            items.append(
                _item(
                    kind="confidence_drop",
                    severity="yellow",
                    score=min(70, 58 + drop // 2),
                    title=f"Confidence dropped {drop} points on {row['subject_type']} {row['subject_id']}",
                    detail=str(row["reason"]) or "Evidence lowered confidence.",
                    subject_type=str(row["subject_type"]),
                    subject_id=int(row["subject_id"]),
                    recommended_action="Check whether the invalidation signal is close and the object still deserves its priority.",
                    opened_at=str(row["created_at"]),
                )
            )
        return items

    def _overrides_due_for_review(self) -> list[dict[str, Any]]:
        now = utc_now()
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM founder_overrides
                WHERE review_date IS NOT NULL AND review_date <= ?
                ORDER BY review_date ASC LIMIT 25
                """,
                (now,),
            ).fetchall()
        return [
            _item(
                kind="override_review_due",
                severity="yellow",
                score=57,
                title=f"Override due for review: {row['founder_decision']}",
                detail=(
                    f"You overrode '{row['zade_recommendation']}' accepting risk: {row['risk_accepted'] or 'unstated'}. "
                    "Time to check who was right."
                ),
                subject_type="founder_override",
                subject_id=int(row["id"]),
                recommended_action="Review the outcome; record a missed-call review if the override went wrong.",
                opened_at=str(row["review_date"]),
            )
            for row in rows
        ]

    def _assumptions_due_for_review(self) -> list[dict[str, Any]]:
        now = utc_now()
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM founder_assumptions
                WHERE status = 'active' AND review_date IS NOT NULL AND review_date <= ?
                ORDER BY review_date ASC LIMIT 25
                """,
                (now,),
            ).fetchall()
        return [
            _item(
                kind="assumption_review_due",
                severity="yellow",
                score=54,
                title=f"Assumption due for review: {row['statement']}",
                detail=f"Confidence {row['confidence']}; invalidation signal: {row['invalidation_signal'] or 'unstated'}.",
                subject_type="assumption",
                subject_id=int(row["id"]),
                recommended_action="Re-check the assumption against the latest evidence.",
                opened_at=str(row["review_date"]),
            )
            for row in rows
        ]

    def _experiments_needing_evidence(self) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in ACTIVE_EXPERIMENT_STATUSES)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM founder_experiments WHERE status IN ({placeholders}) ORDER BY id ASC LIMIT 50",
                ACTIVE_EXPERIMENT_STATUSES,
            ).fetchall()
        items = []
        for row in rows:
            evidence_ids = json.loads(row["evidence_ids_json"] or "[]")
            minimum = max(1, int(row["minimum_evidence"] or 1))
            missing = minimum - len(evidence_ids)
            if missing <= 0:
                continue
            items.append(
                _item(
                    kind="experiment_needs_evidence",
                    severity="yellow",
                    score=50,
                    title=f"Experiment short on evidence: {row['title']}",
                    detail=f"{len(evidence_ids)}/{minimum} required evidence item(s) collected.",
                    subject_type="experiment",
                    subject_id=int(row["id"]),
                    recommended_action=f"Collect {missing} more evidence item(s) or revise the minimum.",
                    opened_at=str(row["updated_at"]),
                )
            )
        return items

    def _pending_approvals(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count, MIN(created_at) AS oldest FROM approval_requests WHERE status = 'pending'"
            ).fetchone()
        count = int(row["count"]) if row else 0
        if count <= 0:
            return []
        return [
            _item(
                kind="approvals_pending",
                severity="yellow",
                score=min(60, 48 + count * 4),
                title=f"{count} approval request(s) waiting on you",
                detail="Queued work is blocked until you approve or deny.",
                subject_type="approval_requests",
                subject_id=None,
                recommended_action="Review /approval-requests and clear the queue.",
                opened_at=str(row["oldest"]) if row["oldest"] else None,
            )
        ]

    def _pending_connector_items(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count, MIN(created_at) AS oldest FROM connector_items WHERE status = 'candidate'"
            ).fetchone()
        count = int(row["count"]) if row else 0
        if count <= 0:
            return []
        return [
            _item(
                kind="connector_items_staged",
                severity="yellow",
                score=min(56, 44 + count * 2),
                title=f"{count} external item(s) staged for evidence review",
                detail="Synced connector items are waiting to be imported as graded evidence or dismissed.",
                subject_type="connector_items",
                subject_id=None,
                recommended_action="Review /connectors/items and import the useful ones as evidence.",
                opened_at=str(row["oldest"]) if row["oldest"] else None,
            )
        ]

    def _underweighted(self, items: list[dict[str, Any]]) -> str:
        candidates = [item for item in items[1:] if item["score"] >= 54 and item.get("opened_at")]
        if not candidates:
            return ""
        oldest = min(candidates, key=lambda item: str(item["opened_at"]))
        return (
            f"Longest-open high-signal item: {oldest['title']} "
            f"(open since {oldest['opened_at']}). {oldest['recommended_action']}"
        )

    def _changes_since(self, since: str | None) -> dict[str, Any]:
        queries = {
            "new_evidence": "SELECT COUNT(*) AS count FROM founder_evidence",
            "new_integrity_warnings": "SELECT COUNT(*) AS count FROM integrity_warnings",
            "new_thesis_conflicts": "SELECT COUNT(*) AS count FROM thesis_conflicts",
            "new_confidence_events": "SELECT COUNT(*) AS count FROM confidence_events",
            "new_overrides": "SELECT COUNT(*) AS count FROM founder_overrides",
            "new_decisions": "SELECT COUNT(*) AS count FROM decision_memos",
        }
        changes: dict[str, Any] = {"first_brief": since is None, "since": since}
        with self.db.connect() as conn:
            for key, sql in queries.items():
                if since:
                    row = conn.execute(f"{sql} WHERE created_at > ?", (since,)).fetchone()
                else:
                    row = conn.execute(sql).fetchone()
                changes[key] = int(row["count"]) if row else 0
        changes["total"] = sum(value for key, value in changes.items() if key.startswith("new_"))
        return changes

    def _compose(self, scan: dict[str, Any], changes: dict[str, Any], since: str | None) -> str:
        name = self.config.identity.name
        lines = [f"{name} initiated brief generated at {scan['generated_at']}."]
        if changes["first_brief"]:
            lines.append("This is the first initiated brief for this operating layer.")
        else:
            lines.append(
                f"Since the last brief ({since}): {changes['new_evidence']} new evidence item(s), "
                f"{changes['new_integrity_warnings']} new integrity warning(s), "
                f"{changes['new_thesis_conflicts']} new thesis conflict(s), "
                f"{changes['new_confidence_events']} confidence change(s), "
                f"{changes['new_decisions']} new decision(s), "
                f"{changes['new_overrides']} new override(s)."
            )
        if not scan["items"]:
            lines.append("Nothing needs founder attention right now. Signals are clear.")
            return "\n".join(lines)
        lines.append(f"Attention queue ({scan['count']} item(s)):")
        for index, item in enumerate(scan["items"][:15], start=1):
            lines.append(
                f"{index}. [{item['kind']}/{item['severity']}/{item['score']}] {item['title']} -> {item['recommended_action']}"
            )
        if scan["count"] > 15:
            lines.append(f"...and {scan['count'] - 15} more item(s).")
        lines.append(f"The one thing that matters most: {scan['one_thing']}")
        if scan["underweighted"]:
            lines.append(f"What you may be underweighting: {scan['underweighted']}")
        return "\n".join(lines)

    def _narrate(self, brief_text: str) -> str:
        prompt = (
            f"You are {self.config.identity.name}, a decisive local co-founder. "
            "Rewrite the following attention brief as a 3-sentence executive read. "
            "State only facts present in the brief. No fake certainty, no invented numbers.\n\n"
            f"{brief_text}\n\nReturn only the 3 sentences."
        )
        try:
            generated = self.ollama.generate(
                prompt=prompt,
                model=self.config.ollama.chat_model,
                think=False,
                temperature=self.config.ollama.temperature,
            )
            return generated.response.strip()
        except Exception:
            return ""

    def _last_brief_at(self) -> str | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT created_at FROM runtime_events WHERE event_type = 'runtime.surfacing' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return str(row["created_at"]) if row else None

    def _log_event(self, *, response: str, details: dict[str, Any]) -> int:
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO runtime_events (
                  created_at, event_type, status, message, response, model,
                  authority_decision, details_json
                )
                VALUES (?, 'runtime.surfacing', 'ok', 'Proactive attention brief', ?, 'local-runtime', 'allow', ?)
                """,
                (utc_now(), response, json.dumps(details, sort_keys=True)),
            )
            return int(cur.lastrowid)


def _item(
    *,
    kind: str,
    severity: str,
    score: int,
    title: str,
    detail: str,
    subject_type: str,
    subject_id: int | None,
    recommended_action: str,
    opened_at: str | None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "severity": severity,
        "score": int(score),
        "title": title,
        "detail": detail,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "recommended_action": recommended_action,
        "opened_at": opened_at,
    }


def _item_headline(item: dict[str, Any]) -> str:
    return f"{item['title']} -> {item['recommended_action']}"


def _age_days(date_text: str) -> int:
    try:
        parsed = datetime.fromisoformat(date_text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return max(0, (datetime.now(UTC) - parsed).days)
    except ValueError:
        return 0
