from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .authority import AuthorityDecision, AuthorityPolicy, AuthorityRequest
from .brief import build_daily_brief
from .config import KernelConfig
from .db import KernelDatabase, WorkItem, utc_now
from .ingestion import IngestionService, SUPPORTED_TEXT_EXTENSIONS


InventoryProvider = Callable[[], dict[str, Any]]


@dataclass(frozen=True)
class QueueResult:
    item_id: int
    created: bool
    status: str
    authority: dict[str, Any]
    action: str
    title: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "created": self.created,
            "status": self.status,
            "authority": self.authority,
            "action": self.action,
            "title": self.title,
        }


@dataclass(frozen=True)
class RunResult:
    item_id: int | None
    status: str
    action: str = ""
    authority: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "status": self.status,
            "action": self.action,
            "authority": self.authority or {},
            "result": self.result or {},
            "error": self.error,
        }


class WorkQueueService:
    def __init__(
        self,
        *,
        config: KernelConfig,
        db: KernelDatabase,
        authority: AuthorityPolicy,
        ingestion: IngestionService,
        inventory_provider: InventoryProvider | None = None,
    ):
        self.config = config
        self.db = db
        self.authority = authority
        self.ingestion = ingestion
        self.inventory_provider = inventory_provider

    def enqueue(
        self,
        *,
        kind: str,
        title: str,
        detail: str,
        action: str,
        target: str = "",
        permission_tier: str = "L0_READ",
        priority: int = 50,
        source: str = "work.queue",
        due_at: str | None = None,
        metadata: dict[str, Any] | None = None,
        unique_key: str | None = None,
    ) -> QueueResult:
        authority = self.authority.evaluate(
            AuthorityRequest(action=action, permission_tier=permission_tier, target=target, metadata=metadata or {})
        )
        metadata = metadata or {}
        authority_dict = authority.as_dict()
        founder_implied_approval = (
            authority.decision == AuthorityDecision.APPROVAL_REQUIRED
            and _source_is_founder_command(source, metadata)
        )
        if founder_implied_approval:
            authority_dict = {
                **authority_dict,
                "reason": "Founder direct command is already approved; no separate approval request was created.",
                "requires_typed_phrase": False,
                "typed_phrase": None,
                "matched_rule": "founder_command.implied_approval",
                "base_decision": authority.decision.value,
            }
        item_id, created = self.db.enqueue_work_item(
            kind=kind,
            title=title,
            detail=detail,
            action=action,
            target=target,
            permission_tier=permission_tier,
            priority=priority,
            source=source,
            due_at=due_at,
            metadata=metadata,
            unique_key=unique_key,
        )
        status = "approved" if founder_implied_approval else _queue_status_for_decision(authority.decision)
        if created:
            result = (
                {
                    "approval_status": "approved_by_founder_command",
                    "founder_command": True,
                    "dispatch": "not_dispatched",
                }
                if founder_implied_approval
                else {}
            )
            self.db.update_work_item(
                item_id,
                status=status,
                authority_decision=authority.decision.value,
                result=result,
                error="",
            )
            if authority.decision == AuthorityDecision.APPROVAL_REQUIRED and not founder_implied_approval:
                approval, approval_created = self.db.ensure_approval_request(
                    source_type="work_item",
                    source_id=item_id,
                    title=title,
                    detail=detail,
                    action=action,
                    target=target,
                    permission_tier=permission_tier,
                    authority_decision=authority.decision.value,
                    authority=authority_dict,
                    requested_by=source,
                    metadata={"work_item_unique_key": unique_key, **(metadata or {})},
                )
            else:
                approval = None
                approval_created = False
            self.db.audit(
                actor="work.queue",
                action="work.enqueue",
                target=action,
                permission_tier=permission_tier,
                status=status,
                details={
                    "item_id": item_id,
                    "authority": authority_dict,
                    "unique_key": unique_key,
                    "approval_request_id": approval.id if approval else None,
                    "approval_request_created": approval_created,
                    "founder_implied_approval": founder_implied_approval,
                },
            )
        return QueueResult(
            item_id=item_id,
            created=created,
            status=status,
            authority=authority_dict,
            action=action,
            title=title,
        )

    def scan(self, *, run_autonomous: bool = True, max_run: int = 5) -> dict[str, Any]:
        queued: list[dict[str, Any]] = []
        queued.append(self._queue_daily_brief().as_dict())
        queued.append(self._queue_inventory_snapshot().as_dict())
        queued.extend(item.as_dict() for item in self._queue_inbox_ingestion())
        queued.extend(item.as_dict() for item in self._queue_goal_reviews())

        run_results: list[dict[str, Any]] = []
        if run_autonomous:
            run_results = [result.as_dict() for result in self.run_due(max_items=max_run)]

        return {
            "queued": queued,
            "created_count": sum(1 for item in queued if item["created"]),
            "existing_count": sum(1 for item in queued if not item["created"]),
            "run": run_results,
            "queue_counts": self.db.work_queue_counts(),
        }

    def list_items(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return [work_item_to_dict(item) for item in self.db.list_work_items(status=status, limit=limit)]

    def run_due(self, *, max_items: int = 5) -> list[RunResult]:
        results = []
        for _ in range(max(0, max_items)):
            result = self.run_next()
            if result.status == "empty":
                break
            results.append(result)
        return results

    def run_next(self) -> RunResult:
        # Atomic claim (pending -> running) so a concurrent scheduler + API run
        # can never both pick up and dispatch the same item.
        item = self.db.claim_next_work_item()
        if item is None:
            return RunResult(item_id=None, status="empty")

        authority = self.authority.evaluate(
            AuthorityRequest(
                action=item.action,
                permission_tier=item.permission_tier,
                target=item.target,
                metadata=item.metadata,
            )
        )
        if authority.decision != AuthorityDecision.ALLOW:
            status = _queue_status_for_decision(authority.decision)
            self.db.update_work_item(
                item.id,
                status=status,
                authority_decision=authority.decision.value,
                result={},
                error=authority.reason,
            )
            self.db.audit(
                actor="work.queue",
                action="work.run",
                target=item.action,
                permission_tier=item.permission_tier,
                status=status,
                details={"item_id": item.id, "authority": authority.as_dict()},
            )
            return RunResult(item_id=item.id, status=status, action=item.action, authority=authority.as_dict())

        self.db.update_work_item(item.id, status="running", authority_decision=authority.decision.value)
        try:
            result = self._dispatch(item)
            result_failure = _work_action_result_failure(result)
            if result_failure:
                self.db.update_work_item(
                    item.id,
                    status="error",
                    authority_decision=authority.decision.value,
                    result=result,
                    error=result_failure,
                )
                self.db.audit(
                    actor="work.queue",
                    action="work.run",
                    target=item.action,
                    permission_tier=item.permission_tier,
                    status="error",
                    details={
                        "item_id": item.id,
                        "authority": authority.as_dict(),
                        "result": result,
                        "error": result_failure,
                    },
                )
                return RunResult(
                    item_id=item.id,
                    status="error",
                    action=item.action,
                    authority=authority.as_dict(),
                    result=result,
                    error=result_failure,
                )
            self.db.update_work_item(
                item.id,
                status="done",
                authority_decision=authority.decision.value,
                result=result,
                error="",
            )
            self.db.audit(
                actor="work.queue",
                action="work.run",
                target=item.action,
                permission_tier=item.permission_tier,
                status="done",
                details={"item_id": item.id, "authority": authority.as_dict(), "result": result},
            )
            return RunResult(
                item_id=item.id,
                status="done",
                action=item.action,
                authority=authority.as_dict(),
                result=result,
            )
        except Exception as exc:
            self.db.update_work_item(
                item.id,
                status="error",
                authority_decision=authority.decision.value,
                result={},
                error=str(exc),
            )
            self.db.audit(
                actor="work.queue",
                action="work.run",
                target=item.action,
                permission_tier=item.permission_tier,
                status="error",
                details={"item_id": item.id, "authority": authority.as_dict(), "error": str(exc)},
            )
            return RunResult(
                item_id=item.id,
                status="error",
                action=item.action,
                authority=authority.as_dict(),
                error=str(exc),
            )

    def _dispatch(self, item: WorkItem) -> dict[str, Any]:
        if item.action == "brief.daily.prepare":
            return self._prepare_daily_brief(item)
        if item.action == "self.inventory.snapshot":
            return self._snapshot_inventory(item)
        if item.action == "ingest.file":
            result = self.ingestion.ingest_file(path=item.target, metadata={"work_item_id": item.id, **item.metadata})
            return result.__dict__
        if item.action == "goal.review":
            memory_id = self.db.add_memory(
                kind="goal_review",
                title=item.title,
                content=item.detail,
                source="work.queue",
                metadata={"work_item_id": item.id, **item.metadata},
            )
            return {"memory_id": memory_id}
        raise ValueError(f"No handler registered for work action: {item.action}")

    def _prepare_daily_brief(self, item: WorkItem) -> dict[str, Any]:
        brief = build_daily_brief(self.db)
        memory_id = self.db.add_memory(
            kind="brief",
            title=f"{self.config.identity.name} Daily Brief {date.today().isoformat()}",
            content=brief["brief"],
            source="work.queue",
            metadata={"work_item_id": item.id, "inputs": list(brief["inputs"].keys())},
        )
        return {"memory_id": memory_id, "sections": list(brief["inputs"].keys())}

    def _snapshot_inventory(self, item: WorkItem) -> dict[str, Any]:
        inventory = self.inventory_provider() if self.inventory_provider else {}
        memory_id = self.db.add_memory(
            kind="system_snapshot",
            title=f"{self.config.identity.name} Self Inventory {date.today().isoformat()}",
            content=json.dumps(inventory, indent=2, sort_keys=True),
            source="work.queue",
            metadata={"work_item_id": item.id},
        )
        return {"memory_id": memory_id, "keys": sorted(inventory.keys())}

    def _queue_daily_brief(self) -> QueueResult:
        today = date.today().isoformat()
        return self.enqueue(
            kind="brief",
            title=f"Prepare {self.config.identity.name} daily brief",
            detail="Prepare a local daily brief from recent memories, goals, decisions, and disagreements.",
            action="brief.daily.prepare",
            target="local_memory",
            permission_tier="L1_MEMORY_WRITE",
            priority=80,
            unique_key=f"brief.daily.prepare:{today}",
        )

    def _queue_inventory_snapshot(self) -> QueueResult:
        today = date.today().isoformat()
        return self.enqueue(
            kind="system_snapshot",
            title=f"Snapshot {self.config.identity.name} self-inventory",
            detail="Capture the current local model, path, authority, and tool posture.",
            action="self.inventory.snapshot",
            target="self-inventory",
            permission_tier="L1_MEMORY_WRITE",
            priority=70,
            unique_key=f"self.inventory.snapshot:{today}",
        )

    def _queue_inbox_ingestion(self) -> list[QueueResult]:
        inbox = self.config.paths.inbox_dir
        if not inbox.exists():
            return []
        items = []
        for path in sorted(inbox.rglob("*"))[:100]:
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_TEXT_EXTENSIONS:
                continue
            stat = path.stat()
            items.append(
                self.enqueue(
                    kind="ingestion",
                    title=f"Ingest inbox file: {path.name}",
                    detail=f"Import {path} into local semantic memory and archive the original by content hash.",
                    action="ingest.file",
                    target=str(path),
                    permission_tier="L1_MEMORY_WRITE",
                    priority=90,
                    metadata={"path": str(path), "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns},
                    unique_key=f"ingest.file:{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}",
                )
            )
        return items

    def _queue_goal_reviews(self) -> list[QueueResult]:
        due_goals = []
        now = utc_now()
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, title, review_after, notes
                FROM goals
                WHERE status = 'active' AND (review_after IS NULL OR review_after <= ?)
                ORDER BY id DESC
                LIMIT 10
                """,
                (now,),
            ).fetchall()
            due_goals = [dict(row) for row in rows]

        today = date.today().isoformat()
        items = []
        for goal in due_goals:
            goal_id = int(goal["id"])
            items.append(
                self.enqueue(
                    kind="goal_review",
                    title=f"Review goal: {goal['title']}",
                    detail=(
                        f"Goal review due for '{goal['title']}'. "
                        "Check whether it still matters, needs evidence, or should be converted into a next action."
                    ),
                    action="goal.review",
                    target=f"goal:{goal_id}",
                    permission_tier="L1_MEMORY_WRITE",
                    priority=60,
                    metadata={"goal_id": goal_id, "review_after": goal["review_after"], "notes": goal["notes"]},
                    unique_key=f"goal.review:{goal_id}:{today}",
                )
            )
        return items


def work_item_to_dict(item: WorkItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "kind": item.kind,
        "title": item.title,
        "detail": item.detail,
        "action": item.action,
        "target": item.target,
        "permission_tier": item.permission_tier,
        "authority_decision": item.authority_decision,
        "status": item.status,
        "priority": item.priority,
        "source": item.source,
        "due_at": item.due_at,
        "last_error": item.last_error,
        "result": item.result,
        "metadata": item.metadata,
        "unique_key": item.unique_key,
    }


def _queue_status_for_decision(decision: AuthorityDecision) -> str:
    if decision == AuthorityDecision.ALLOW:
        return "pending"
    if decision == AuthorityDecision.DENY:
        return "denied"
    return "approval_required"


def _work_action_result_failure(result: dict[str, Any]) -> str:
    if not isinstance(result, dict):
        return ""
    if result.get("ok") is False:
        return "Work action returned ok=false."
    status = str(result.get("status") or "").strip().lower()
    if status in {"error", "failed", "failure", "flow_error"}:
        error = str(result.get("error") or "").strip()
        return f"Work action returned status={status}: {error}" if error else f"Work action returned status={status}."
    return ""


def _source_is_founder_command(source: str, metadata: dict[str, Any]) -> bool:
    normalized = source.strip().lower().replace("_", ".").replace("-", ".")
    if normalized in {"founder", "founder.direct", "founder.command", "user.direct", "ui.direct", "voice.direct"}:
        return True
    if normalized.startswith("founder."):
        return True
    explicit = metadata.get("founder_command") or metadata.get("direct_founder_command")
    return bool(explicit)
