from __future__ import annotations

import json
from typing import Any

from .authority import AuthorityDecision, AuthorityPolicy, AuthorityRequest
from .autonomy import WorkQueueService
from .db import KernelDatabase, utc_now
from .founder import FounderService


PLAN_STATUSES = {"active", "blocked", "done", "failed", "abandoned"}
STEP_TERMINAL = {"done", "failed", "skipped"}
EXECUTION_MODES = {"manual", "work_queue"}

# Work item status -> step status, for steps executing through the work queue.
WORK_ITEM_STEP_STATUS = {
    "pending": "queued",
    "running": "running",
    "done": "done",
    "error": "failed",
    "denied": "blocked",
    "approval_required": "approval_required",
    "approved": "approved",
}


class ActionPipelineService:
    """Decision-to-action pipeline: recommendations become plans, plans become steps.

    Every step carries its own authority evaluation. Machine steps execute
    through the existing work queue (so approvals, typed confirmation, and
    registered handlers all apply); manual steps are founder work the pipeline
    tracks honestly. Step outcomes land in the evidence ledger, so execution
    history is auditable business memory, not a task list.
    """

    def __init__(
        self,
        *,
        db: KernelDatabase,
        authority: AuthorityPolicy,
        founder: FounderService,
        work_queue: WorkQueueService,
        bus: Any | None = None,
    ):
        self.db = db
        self.authority = authority
        self.founder = founder
        self.work_queue = work_queue
        self.bus = bus

    def create_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        steps = payload.get("steps") or []
        if not steps:
            raise ValueError("An action plan needs at least one step.")
        now = utc_now()
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO action_plans (
                  created_at, updated_at, title, objective, source_type, source_id,
                  status, priority, owner, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    now,
                    now,
                    payload["title"],
                    payload.get("objective", ""),
                    payload.get("source_type", "manual"),
                    payload.get("source_id"),
                    int(payload.get("priority", 50)),
                    payload.get("owner", "founder"),
                    json.dumps(payload.get("metadata", {}), sort_keys=True),
                ),
            )
            plan_id = int(cur.lastrowid)
        for index, step in enumerate(steps):
            self._insert_step(plan_id, index, step)
        self._recompute_plan_status(plan_id)
        self.db.audit(
            actor="actions",
            action="actions.plan.create",
            target=f"action_plan:{plan_id}",
            permission_tier="L1_MEMORY_WRITE",
            status="ok",
            details={"title": payload["title"], "steps": len(steps), "source_type": payload.get("source_type", "manual")},
        )
        return self.get_plan(plan_id)

    def create_plan_from_recommendation(
        self,
        recommendation_id: int,
        *,
        steps: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM decision_recommendations WHERE id = ?",
                (recommendation_id,),
            ).fetchone()
        if not row:
            raise ValueError(f"Decision recommendation not found: {recommendation_id}")
        recommendation = dict(row)
        plan_steps = steps or []
        if not plan_steps:
            next_action = str(recommendation.get("next_action") or "").strip()
            plan_steps = [
                {
                    "title": next_action or f"Execute: {recommendation['recommendation']}",
                    "detail": str(recommendation.get("rationale") or ""),
                }
            ]
        plan = self.create_plan(
            {
                "title": f"Act on: {recommendation['recommendation']}",
                "objective": str(recommendation.get("problem") or ""),
                "source_type": "decision_recommendation",
                "source_id": recommendation_id,
                "steps": plan_steps,
                "metadata": {
                    "recommendation": recommendation.get("recommendation"),
                    "confidence": recommendation.get("confidence"),
                    "kill_or_reversal_condition": recommendation.get("kill_or_reversal_condition"),
                },
            }
        )
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE decision_recommendations SET updated_at = ?, status = 'planned' WHERE id = ?",
                (utc_now(), recommendation_id),
            )
        return plan

    def list_plans(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM action_plans WHERE status = ? ORDER BY priority DESC, id DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM action_plans ORDER BY priority DESC, id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [_plan_from_row(row) for row in rows]

    def get_plan(self, plan_id: int) -> dict[str, Any]:
        self._sync_work_queue_steps(plan_id)
        self._recompute_plan_status(plan_id)
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM action_plans WHERE id = ?", (plan_id,)).fetchone()
            steps = conn.execute(
                "SELECT * FROM action_steps WHERE plan_id = ? ORDER BY step_index ASC",
                (plan_id,),
            ).fetchall()
        if not row:
            raise ValueError(f"Action plan not found: {plan_id}")
        return _plan_from_row(row) | {"steps": [_step_from_row(step) for step in steps]}

    def advance(self, plan_id: int) -> dict[str, Any]:
        """Move the plan forward one step: dispatch or surface whatever is next."""
        plan = self.get_plan(plan_id)
        if plan["status"] in {"done", "failed", "abandoned"}:
            return {"plan": plan, "advanced": False, "note": f"Plan is {plan['status']}; nothing to advance."}
        ready = _next_step(plan["steps"])
        if ready is None:
            plan = self._recompute_plan_status(plan_id)
            return {"plan": self.get_plan(plan_id), "advanced": False, "note": "All steps are terminal."}
        note = ""
        if ready["status"] == "blocked":
            note = f"Step {ready['step_index'] + 1} is blocked: {ready['authority_reason'] or ready['error'] or 'blocked'}"
        elif ready["status"] == "approval_required":
            note = f"Step {ready['step_index'] + 1} needs founder approval before it can run."
        elif ready["status"] in {"queued", "running"}:
            note = f"Step {ready['step_index'] + 1} is already {ready['status']}."
        elif ready["status"] in {"pending", "approved"}:
            if ready["execution"] == "work_queue":
                note = self._dispatch_work_queue_step(plan_id, ready)
            else:
                required_approval = ready["authority_decision"] == "approval_required" and ready["status"] != "approved"
                if required_approval:
                    self._update_step(ready["id"], status="approval_required")
                    note = f"Step {ready['step_index'] + 1} needs founder approval before it can run."
                else:
                    self._update_step(ready["id"], status="running")
                    note = f"Step {ready['step_index'] + 1} is now running (manual execution by {plan['owner']})."
        self._recompute_plan_status(plan_id)
        self.db.audit(
            actor="actions",
            action="actions.plan.advance",
            target=f"action_plan:{plan_id}",
            permission_tier="L1_MEMORY_WRITE",
            status="ok",
            details={"step_id": ready["id"], "step_status_note": note},
        )
        return {"plan": self.get_plan(plan_id), "advanced": True, "note": note}

    def approve_step(self, plan_id: int, step_id: int, *, approved_by: str = "founder") -> dict[str, Any]:
        step = self._get_step(plan_id, step_id)
        if step["status"] != "approval_required":
            raise ValueError(f"Step is {step['status']}, not approval_required.")
        if step["execution"] == "work_queue":
            raise ValueError(
                "Work-queue steps are approved through the approval request flow "
                "(/work/items/{id}/approve), not the plan endpoint."
            )
        self._update_step(step_id, status="approved", approved_by=approved_by)
        self._recompute_plan_status(plan_id)
        return self.get_plan(plan_id)

    def complete_step(
        self,
        plan_id: int,
        step_id: int,
        *,
        result: str = "",
        note: str = "",
        create_evidence: bool = True,
    ) -> dict[str, Any]:
        step = self._get_step(plan_id, step_id)
        if step["execution"] == "work_queue":
            raise ValueError("Work-queue steps complete through dispatch, not manually.")
        if step["status"] in STEP_TERMINAL:
            raise ValueError(f"Step is already {step['status']}.")
        if step["authority_decision"] == "approval_required" and not step["approved_by"]:
            raise ValueError("Step requires approval before it can be completed.")
        self._update_step(step_id, status="done", result=result, error="")
        if create_evidence:
            self._record_step_evidence(plan_id, step, outcome="done", detail=result or note)
        self._recompute_plan_status(plan_id)
        self.db.audit(
            actor="actions",
            action="actions.step.complete",
            target=f"action_plan:{plan_id}:step:{step_id}",
            permission_tier="L1_MEMORY_WRITE",
            status="ok",
            details={"result": result[:400], "note": note[:400]},
        )
        return self.get_plan(plan_id)

    def fail_step(self, plan_id: int, step_id: int, *, error: str, create_evidence: bool = True) -> dict[str, Any]:
        step = self._get_step(plan_id, step_id)
        if step["status"] in STEP_TERMINAL:
            raise ValueError(f"Step is already {step['status']}.")
        self._update_step(step_id, status="failed", error=error)
        if create_evidence:
            self._record_step_evidence(plan_id, step, outcome="failed", detail=error)
        self._recompute_plan_status(plan_id)
        self._notify(
            topic="action_plan.step_failed",
            severity="warning",
            title=f"Action step failed: {step['title']}",
            body=f"Plan {plan_id} step {step['step_index'] + 1} failed: {error}",
            dedupe_key=f"action_step_failed:{step_id}",
        )
        return self.get_plan(plan_id)

    def skip_step(self, plan_id: int, step_id: int, *, note: str = "") -> dict[str, Any]:
        step = self._get_step(plan_id, step_id)
        if step["status"] in STEP_TERMINAL:
            raise ValueError(f"Step is already {step['status']}.")
        self._update_step(step_id, status="skipped", result=note)
        self._recompute_plan_status(plan_id)
        return self.get_plan(plan_id)

    def attach_evidence(self, plan_id: int, step_id: int, *, evidence_id: int) -> dict[str, Any]:
        step = self._get_step(plan_id, step_id)
        with self.db.connect() as conn:
            row = conn.execute("SELECT id FROM founder_evidence WHERE id = ?", (evidence_id,)).fetchone()
            if not row:
                raise ValueError(f"Evidence not found: {evidence_id}")
        self._append_step_evidence(step_id, evidence_id)
        self.founder.create_link(
            {
                "from_type": "evidence",
                "from_id": evidence_id,
                "relation": "documents",
                "to_type": "action_step",
                "to_id": step_id,
                "strength": 60,
                "metadata": {"source": "actions.attach_evidence", "plan_id": plan_id},
            }
        )
        return self._get_step(plan_id, step_id)

    def attention_items(self) -> list[dict[str, Any]]:
        """Plans that are stuck — consumed by the surfacing layer."""
        stalled = self.list_plans(status="blocked", limit=25) + self.list_plans(status="failed", limit=25)
        return [
            {
                "plan_id": plan["id"],
                "title": plan["title"],
                "status": plan["status"],
                "opened_at": plan["updated_at"],
            }
            for plan in stalled
        ]

    def _insert_step(self, plan_id: int, index: int, step: dict[str, Any]) -> int:
        execution = str(step.get("execution", "manual")).strip().lower()
        if execution not in EXECUTION_MODES:
            raise ValueError(f"Step execution must be one of: {', '.join(sorted(EXECUTION_MODES))}")
        action = str(step.get("action", "founder.task")).strip() or "founder.task"
        tier = str(step.get("permission_tier", "L1_MEMORY_WRITE")).strip() or "L1_MEMORY_WRITE"
        target = str(step.get("target", ""))
        authority = self.authority.evaluate(
            AuthorityRequest(action=action, permission_tier=tier, target=target, metadata={"action_plan": plan_id})
        )
        if authority.decision == AuthorityDecision.DENY:
            status = "blocked"
        elif authority.decision == AuthorityDecision.APPROVAL_REQUIRED and execution == "manual":
            status = "approval_required"
        else:
            status = "pending"
        now = utc_now()
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO action_steps (
                  created_at, updated_at, plan_id, step_index, title, detail, action, target,
                  permission_tier, execution, authority_decision, authority_reason, status, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    now,
                    plan_id,
                    index,
                    step["title"],
                    step.get("detail", ""),
                    action,
                    target,
                    tier,
                    execution,
                    authority.decision.value,
                    authority.reason,
                    status,
                    json.dumps(step.get("metadata", {}), sort_keys=True),
                ),
            )
            return int(cur.lastrowid)

    def _dispatch_work_queue_step(self, plan_id: int, step: dict[str, Any]) -> str:
        result = self.work_queue.enqueue(
            kind="action_step",
            title=step["title"],
            detail=step["detail"] or f"Action plan {plan_id} step {step['step_index'] + 1}.",
            action=step["action"],
            target=step["target"],
            permission_tier=step["permission_tier"],
            priority=85,
            source="actions",
            metadata={"action_plan_id": plan_id, "action_step_id": step["id"], **step.get("metadata", {})},
            unique_key=f"action_step:{step['id']}",
        )
        mapped = WORK_ITEM_STEP_STATUS.get(result.status, "queued")
        self._update_step(step["id"], status=mapped, work_item_id=result.item_id)
        if mapped == "approval_required":
            return (
                f"Step {step['step_index'] + 1} queued as work item {result.item_id}; "
                "founder approval + typed confirmation required to dispatch."
            )
        return f"Step {step['step_index'] + 1} queued as work item {result.item_id} ({mapped})."

    def _sync_work_queue_steps(self, plan_id: int) -> None:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, work_item_id, status FROM action_steps
                WHERE plan_id = ? AND work_item_id IS NOT NULL AND status NOT IN ('done', 'failed', 'skipped')
                """,
                (plan_id,),
            ).fetchall()
        for row in rows:
            item = self.db.get_work_item(int(row["work_item_id"]))
            if not item:
                continue
            mapped = WORK_ITEM_STEP_STATUS.get(item.status)
            if not mapped or mapped == str(row["status"]):
                continue
            if mapped == "done":
                self._update_step(int(row["id"]), status="done", result=json.dumps(item.result, sort_keys=True)[:800])
                step = self._get_step_by_id(int(row["id"]))
                self._record_step_evidence(plan_id, step, outcome="done", detail=str(item.result)[:400])
            elif mapped == "failed":
                self._update_step(int(row["id"]), status="failed", error=item.last_error)
                step = self._get_step_by_id(int(row["id"]))
                self._record_step_evidence(plan_id, step, outcome="failed", detail=item.last_error)
            else:
                self._update_step(int(row["id"]), status=mapped)

    def _record_step_evidence(self, plan_id: int, step: dict[str, Any], *, outcome: str, detail: str) -> None:
        payload = {
            "evidence_type": "action_step_outcome",
            "source": f"action_plan:{plan_id}:step:{step['step_index'] + 1}",
            "reliability": "A",
            "strength": 80,
            "notes": "Recorded automatically by the decision-to-action pipeline.",
            "metadata": {
                "action_plan_id": plan_id,
                "action_step_id": step["id"],
                "outcome": outcome,
                "entity_boundary": "Verified runtime outcome of an executed step.",
            },
        }
        if outcome == "done":
            payload["claim_supported"] = f"Step '{step['title']}' completed. {detail}".strip()
        else:
            payload["claim_contradicted"] = f"Step '{step['title']}' failed. {detail}".strip()
        evidence = self.founder.create_evidence(payload)
        self._append_step_evidence(step["id"], evidence.id)

    def _append_step_evidence(self, step_id: int, evidence_id: int) -> None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT evidence_ids_json FROM action_steps WHERE id = ?", (step_id,)).fetchone()
            evidence_ids = json.loads(row["evidence_ids_json"] or "[]") if row else []
            if evidence_id not in evidence_ids:
                evidence_ids.append(evidence_id)
            conn.execute(
                "UPDATE action_steps SET updated_at = ?, evidence_ids_json = ? WHERE id = ?",
                (utc_now(), json.dumps(evidence_ids, sort_keys=True), step_id),
            )

    def _recompute_plan_status(self, plan_id: int) -> dict[str, Any]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT status FROM action_steps WHERE plan_id = ? ORDER BY step_index ASC",
                (plan_id,),
            ).fetchall()
            statuses = [str(row["status"]) for row in rows]
            if statuses and all(status in {"done", "skipped"} for status in statuses):
                plan_status = "done"
            elif any(status == "failed" for status in statuses):
                plan_status = "failed"
            elif any(status == "blocked" for status in statuses):
                plan_status = "blocked"
            else:
                plan_status = "active"
            conn.execute(
                "UPDATE action_plans SET updated_at = ?, status = ? WHERE id = ? AND status != 'abandoned'",
                (utc_now(), plan_status, plan_id),
            )
        return {"status": plan_status}

    def _get_step(self, plan_id: int, step_id: int) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM action_steps WHERE id = ? AND plan_id = ?",
                (step_id, plan_id),
            ).fetchone()
        if not row:
            raise ValueError(f"Action step not found: plan {plan_id}, step {step_id}")
        return _step_from_row(row)

    def _get_step_by_id(self, step_id: int) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM action_steps WHERE id = ?", (step_id,)).fetchone()
        if not row:
            raise ValueError(f"Action step not found: {step_id}")
        return _step_from_row(row)

    def _update_step(self, step_id: int, **fields: Any) -> None:
        allowed = {"status", "work_item_id", "approved_by", "result", "error"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self.db.connect() as conn:
            conn.execute(
                f"UPDATE action_steps SET updated_at = ?, {assignments} WHERE id = ?",
                (utc_now(), *updates.values(), step_id),
            )

    def _notify(self, *, topic: str, severity: str, title: str, body: str, dedupe_key: str = "") -> None:
        if self.bus is None:
            return
        try:
            self.bus.notify(topic=topic, severity=severity, title=title, body=body, source="actions", dedupe_key=dedupe_key)
        except Exception:
            pass


def _next_step(steps: list[dict[str, Any]]) -> dict[str, Any] | None:
    for step in steps:
        if step["status"] not in STEP_TERMINAL:
            return step
    return None


def _plan_from_row(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
    return data


def _step_from_row(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["evidence_ids"] = json.loads(data.pop("evidence_ids_json") or "[]")
    data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
    return data
