from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .authority import AuthorityDecision, AuthorityPolicy, AuthorityRequest
from .db import ApprovalRequest, KernelDatabase
from .handlers import ActionHandlerRegistry


NON_APPROVABLE_TIERS = {"L4_IRREVERSIBLE", "DENIED"}


class ApprovalService:
    def __init__(
        self,
        *,
        db: KernelDatabase,
        handlers: ActionHandlerRegistry | None = None,
        authority: AuthorityPolicy | None = None,
        typed_confirmation_phrase: str = "make the jump to hyperspace",
    ):
        self.db = db
        self.handlers = handlers
        self.authority = authority
        self.typed_confirmation_phrase = typed_confirmation_phrase

    def list_requests(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return [_request_to_dict(item) for item in self.db.list_approval_requests(status=status, limit=limit)]

    def list_console(self, *, status: str | None = None, limit: int = 50) -> dict[str, Any]:
        requests = self.db.list_approval_requests(status=status, limit=limit)
        return {
            "items": [self._console_item(request) for request in requests],
            "summary": self.console_summary(),
        }

    def get_console_item(self, request_id: int) -> dict[str, Any]:
        request = self.db.get_approval_request(request_id)
        if not request:
            raise ValueError(f"Approval request not found: {request_id}")
        return self._console_item(request)

    def list_training_events(
        self,
        *,
        approval_request_id: int | None = None,
        outcome: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return [asdict(item) for item in self.db.list_approval_training_events(
            approval_request_id=approval_request_id,
            outcome=outcome,
            limit=limit,
        )]

    def console_summary(self) -> dict[str, Any]:
        pending = self.db.list_approval_requests(status="pending", limit=500)
        deferred = self.db.list_approval_requests(status="deferred", limit=500)
        recent_training = self.db.list_approval_training_events(limit=500)
        by_outcome: dict[str, int] = {}
        for event in recent_training:
            by_outcome[event.outcome] = by_outcome.get(event.outcome, 0) + 1
        return {
            "pending": len(pending),
            "deferred": len(deferred),
            "training_events": len(recent_training),
            "training_by_outcome": by_outcome,
        }

    def list_handlers(self) -> list[dict[str, str]]:
        return self.handlers.list_handlers() if self.handlers else []

    def get_request(self, request_id: int) -> dict[str, Any]:
        request = self.db.get_approval_request(request_id)
        if not request:
            raise ValueError(f"Approval request not found: {request_id}")
        return _request_to_dict(request)

    def approve_request(
        self,
        request_id: int,
        *,
        resolved_by: str = "founder",
        note: str = "",
        dispatch: bool = False,
        typed_confirmation: str = "",
    ) -> dict[str, Any]:
        request = self._load_open_request(request_id)
        if request.permission_tier in NON_APPROVABLE_TIERS or request.authority_decision == "deny":
            raise ValueError("Denied or irreversible boundaries cannot be approved through this endpoint.")
        if dispatch:
            self._validate_dispatch_request(request)
            self._validate_typed_confirmation(typed_confirmation)
        resolved = self.db.resolve_approval_request(
            request_id,
            status="approved",
            resolved_by=resolved_by,
            resolution_note=note,
        )
        work_item = self._sync_work_item(resolved, status="approved", note=note)
        dispatch_result = (
            self.dispatch_work_item(resolved.source_id, typed_confirmation=typed_confirmation)
            if dispatch and resolved.source_id
            else None
        )
        if dispatch_result and dispatch_result.get("work_item"):
            work_item = dispatch_result["work_item"]
        training_event_id = self._record_training_event(
            resolved,
            event_type="approval_resolution",
            outcome="approved",
            actor=resolved_by,
            note=note,
            work_item=work_item,
            metadata={"dispatch": dispatch_result["dispatch"] if dispatch_result else "not_dispatched"},
        )
        audit_id = self.db.audit(
            actor="approval",
            action="approval.approve",
            target=resolved.action,
            permission_tier=resolved.permission_tier,
            status="approved",
            details={
                "approval_request_id": resolved.id,
                "source_type": resolved.source_type,
                "source_id": resolved.source_id,
                "work_item_status": work_item["status"] if work_item else None,
                "dispatch": dispatch_result["dispatch"] if dispatch_result else "not_dispatched",
                "note": note,
                "training_event_id": training_event_id,
            },
        )
        return {
            "request": _request_to_dict(resolved),
            "work_item": work_item,
            "audit_id": audit_id,
            "training_event_id": training_event_id,
            "dispatch": dispatch_result["dispatch"] if dispatch_result else "not_dispatched",
            "dispatch_result": dispatch_result["result"] if dispatch_result else None,
            "note": (
                "Approval recorded and dispatched through a registered local handler."
                if dispatch_result
                else "Approval recorded. External or unmanaged actions still require a registered dispatcher."
            ),
        }

    def deny_request(self, request_id: int, *, resolved_by: str = "founder", note: str = "") -> dict[str, Any]:
        request = self._load_open_request(request_id)
        resolved = self.db.resolve_approval_request(
            request_id,
            status="denied",
            resolved_by=resolved_by,
            resolution_note=note,
        )
        work_item = self._sync_work_item(resolved, status="denied", note=note)
        training_event_id = self._record_training_event(
            resolved,
            event_type="approval_resolution",
            outcome="denied",
            actor=resolved_by,
            note=note,
            work_item=work_item,
        )
        audit_id = self.db.audit(
            actor="approval",
            action="approval.deny",
            target=resolved.action,
            permission_tier=resolved.permission_tier,
            status="denied",
            details={
                "approval_request_id": resolved.id,
                "source_type": resolved.source_type,
                "source_id": resolved.source_id,
                "work_item_status": work_item["status"] if work_item else None,
                "note": note,
                "training_event_id": training_event_id,
            },
        )
        return {
            "request": _request_to_dict(resolved),
            "work_item": work_item,
            "audit_id": audit_id,
            "training_event_id": training_event_id,
        }

    def defer_request(
        self,
        request_id: int,
        *,
        resolved_by: str = "founder",
        note: str = "",
        defer_until: str | None = None,
    ) -> dict[str, Any]:
        request = self._load_open_request(request_id)
        metadata = {"defer_until": defer_until} if defer_until else {}
        deferred = self.db.update_approval_request(
            request_id,
            status="deferred",
            resolved_by=resolved_by,
            resolved_at=self.db_now(),
            resolution_note=note,
            metadata=metadata,
        )
        work_item = self._sync_work_item(deferred, status="deferred", note=note)
        if deferred.source_type == "work_item" and deferred.source_id is not None and defer_until:
            updated = self.db.update_work_item_proposal(deferred.source_id, due_at=defer_until)
            work_item = asdict(updated)
        training_event_id = self._record_training_event(
            deferred,
            event_type="approval_resolution",
            outcome="deferred",
            actor=resolved_by,
            note=note,
            work_item=work_item,
            metadata=metadata,
        )
        audit_id = self.db.audit(
            actor="approval",
            action="approval.defer",
            target=deferred.action,
            permission_tier=deferred.permission_tier,
            status="deferred",
            details={
                "approval_request_id": deferred.id,
                "source_type": deferred.source_type,
                "source_id": deferred.source_id,
                "defer_until": defer_until,
                "training_event_id": training_event_id,
            },
        )
        return {
            "request": _request_to_dict(deferred),
            "work_item": work_item,
            "audit_id": audit_id,
            "training_event_id": training_event_id,
        }

    def defer_work_item(
        self,
        item_id: int,
        *,
        resolved_by: str = "founder",
        note: str = "",
        defer_until: str | None = None,
    ) -> dict[str, Any]:
        request = self._request_for_work_item(item_id)
        return self.defer_request(request.id, resolved_by=resolved_by, note=note, defer_until=defer_until)

    def edit_request(
        self,
        request_id: int,
        *,
        edited_by: str = "founder",
        note: str = "",
        title: str | None = None,
        detail: str | None = None,
        action: str | None = None,
        target: str | None = None,
        permission_tier: str | None = None,
        priority: int | None = None,
        evidence: list[Any] | None = None,
        risks: list[Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request = self.db.get_approval_request(request_id)
        if not request:
            raise ValueError(f"Approval request not found: {request_id}")
        if request.status not in {"pending", "deferred"}:
            raise ValueError(f"Approval request is already {request.status}.")

        old_request = _request_to_dict(request)
        old_work_item = self._work_item_for_request(request)
        new_action = action if action is not None else request.action
        new_target = target if target is not None else request.target
        new_tier = permission_tier if permission_tier is not None else request.permission_tier
        request_metadata = dict(metadata or {})
        if evidence is not None:
            request_metadata["evidence"] = evidence
        if risks is not None:
            request_metadata["risks"] = risks

        authority = self._evaluate(new_action, new_tier, new_target, request_metadata)
        new_status = "denied" if authority["decision"] == AuthorityDecision.DENY.value else request.status
        work_status = "denied" if authority["decision"] == AuthorityDecision.DENY.value else (
            "deferred" if request.status == "deferred" else "approval_required"
        )
        updated = self.db.update_approval_request(
            request_id,
            title=title,
            detail=detail,
            action=action,
            target=target,
            permission_tier=permission_tier,
            authority_decision=authority["decision"],
            authority=authority,
            status=new_status,
            metadata=request_metadata if request_metadata else None,
        )

        work_item = None
        if updated.source_type == "work_item" and updated.source_id is not None:
            updated_work = self.db.update_work_item_proposal(
                updated.source_id,
                title=title,
                detail=detail,
                action=action,
                target=target,
                permission_tier=permission_tier,
                priority=priority,
                metadata=request_metadata if request_metadata else None,
                status=work_status,
                authority_decision=authority["decision"],
                result={"approval_status": work_status, "approval_request_id": updated.id},
                error=authority.get("reason", "") if work_status == "denied" else "",
            )
            work_item = asdict(updated_work)

        edits = {
            "before": {
                "request": old_request,
                "work_item": old_work_item,
            },
            "after": {
                "title": title,
                "detail": detail,
                "action": action,
                "target": target,
                "permission_tier": permission_tier,
                "priority": priority,
                "evidence": evidence,
                "risks": risks,
                "metadata": metadata,
            },
        }
        training_event_id = self._record_training_event(
            updated,
            event_type="approval_edit",
            outcome="edited",
            actor=edited_by,
            note=note,
            work_item=work_item,
            edits=edits,
            metadata={"status_after": new_status},
        )
        audit_id = self.db.audit(
            actor="approval",
            action="approval.edit",
            target=updated.action,
            permission_tier=updated.permission_tier,
            status=new_status,
            details={
                "approval_request_id": updated.id,
                "work_item_id": updated.source_id if updated.source_type == "work_item" else None,
                "authority": authority,
                "training_event_id": training_event_id,
            },
        )
        return {
            "request": _request_to_dict(updated),
            "work_item": work_item,
            "audit_id": audit_id,
            "training_event_id": training_event_id,
            "console_item": self._console_item(updated),
        }

    def edit_work_item(self, item_id: int, **kwargs: Any) -> dict[str, Any]:
        request = self._request_for_work_item(item_id)
        return self.edit_request(request.id, **kwargs)

    def approve_work_item(
        self,
        item_id: int,
        *,
        resolved_by: str = "founder",
        note: str = "",
        dispatch: bool = False,
        typed_confirmation: str = "",
    ) -> dict[str, Any]:
        request = self._request_for_work_item(item_id)
        return self.approve_request(
            request.id,
            resolved_by=resolved_by,
            note=note,
            dispatch=dispatch,
            typed_confirmation=typed_confirmation,
        )

    def deny_work_item(self, item_id: int, *, resolved_by: str = "founder", note: str = "") -> dict[str, Any]:
        request = self._request_for_work_item(item_id)
        return self.deny_request(request.id, resolved_by=resolved_by, note=note)

    def dispatch_work_item(self, item_id: int, *, typed_confirmation: str = "") -> dict[str, Any]:
        if self.handlers is None:
            raise ValueError("No action handler registry is configured.")
        self._validate_typed_confirmation(typed_confirmation)
        item = self.db.get_work_item(item_id)
        if not item:
            raise ValueError(f"Work item not found: {item_id}")
        if item.status != "approved":
            raise ValueError(f"Work item is {item.status}, not approved.")
        if item.permission_tier in NON_APPROVABLE_TIERS or item.authority_decision == "deny":
            raise ValueError("Denied or irreversible boundaries cannot be dispatched.")
        if not self.handlers.can_dispatch(item.action):
            raise ValueError(f"No approved local handler registered for action: {item.action}")

        self.db.update_work_item(item.id, status="running", authority_decision=item.authority_decision)
        try:
            result = self.handlers.dispatch(item)
        except Exception as exc:
            self.db.update_work_item(
                item.id,
                status="error",
                authority_decision=item.authority_decision,
                result={},
                error=str(exc),
            )
            self.db.audit(
                actor="approval",
                action="approval.dispatch",
                target=item.action,
                permission_tier=item.permission_tier,
                status="error",
                details={"work_item_id": item.id, "error": str(exc)},
            )
            raise

        result = {"approval_dispatch": True, **result}
        self.db.update_work_item(
            item.id,
            status="done",
            authority_decision=item.authority_decision,
            result=result,
            error="",
        )
        done = self.db.get_work_item(item.id)
        audit_id = self.db.audit(
            actor="approval",
            action="approval.dispatch",
            target=item.action,
            permission_tier=item.permission_tier,
            status="done",
            details={"work_item_id": item.id, "result": result},
        )
        return {
            "dispatch": "dispatched",
            "result": result,
            "work_item": asdict(done) if done else None,
            "audit_id": audit_id,
        }

    def _load_open_request(self, request_id: int) -> ApprovalRequest:
        """Load a request the founder can still act on: pending or deferred.

        Deferred is parked, not decided — it must remain approvable, deniable,
        and re-deferrable. Requiring exactly 'pending' here made every deferred
        request permanently unresolvable (approve/deny 400'd both from the
        approvals console and the work-queue routes; only edit accepted it).
        """
        request = self.db.get_approval_request(request_id)
        if not request:
            raise ValueError(f"Approval request not found: {request_id}")
        if request.status not in {"pending", "deferred"}:
            raise ValueError(f"Approval request is already {request.status}.")
        return request

    def _request_for_work_item(self, item_id: int) -> ApprovalRequest:
        request = self.db.get_pending_approval_for_source(source_type="work_item", source_id=item_id)
        if request:
            return request
        item = self.db.get_work_item(item_id)
        if not item:
            raise ValueError(f"Work item not found: {item_id}")
        # Backfill a request for any item whose founder decision is still open:
        # approval_required (explicit), deferred (parked, undecided), or pending
        # (queued; approving early records the call, denying kills queued work).
        # Resolved states (done/denied/approved/failed) stay unreachable here.
        if item.status not in {"approval_required", "deferred", "pending"}:
            raise ValueError(f"Work item is {item.status}; only open items (approval_required/deferred/pending) can be resolved.")
        request, _created = self.db.ensure_approval_request(
            source_type="work_item",
            source_id=item.id,
            title=item.title,
            detail=item.detail,
            action=item.action,
            target=item.target,
            permission_tier=item.permission_tier,
            authority_decision=item.authority_decision,
            authority={},
            requested_by=item.source,
            metadata={"backfilled": True, **item.metadata},
        )
        return request

    def _validate_dispatch_request(self, request: ApprovalRequest) -> None:
        if request.source_type != "work_item" or request.source_id is None:
            raise ValueError("Only work-item approval requests can be dispatched.")
        if self.handlers is None:
            raise ValueError("No action handler registry is configured.")
        if not self.handlers.can_dispatch(request.action):
            raise ValueError(f"No approved local handler registered for action: {request.action}")

    def _validate_typed_confirmation(self, typed_confirmation: str) -> None:
        if typed_confirmation.strip() != self.typed_confirmation_phrase:
            raise ValueError(f"Dispatch requires typed confirmation phrase: {self.typed_confirmation_phrase}")

    def _work_item_for_request(self, request: ApprovalRequest) -> dict[str, Any] | None:
        if request.source_type != "work_item" or request.source_id is None:
            return None
        item = self.db.get_work_item(request.source_id)
        return asdict(item) if item else None

    def _evaluate(self, action: str, permission_tier: str, target: str, metadata: dict[str, Any]) -> dict[str, Any]:
        if self.authority is None:
            return {
                "decision": "approval_required",
                "reason": "No authority policy configured for edit re-evaluation.",
                "policy_version": "",
                "requires_typed_phrase": False,
                "typed_phrase": None,
                "matched_rule": "approval.console.no_authority_policy",
            }
        return self.authority.evaluate(
            AuthorityRequest(action=action, permission_tier=permission_tier, target=target, metadata=metadata)
        ).as_dict()

    def _console_item(self, request: ApprovalRequest) -> dict[str, Any]:
        request_dict = _request_to_dict(request)
        work_item = self._work_item_for_request(request)
        merged_metadata = {**(work_item.get("metadata", {}) if work_item else {}), **request.metadata}
        authority = request.authority or {}
        can_dispatch = (
            request.source_type == "work_item"
            and request.source_id is not None
            and self.handlers is not None
            and self.handlers.can_dispatch(request.action)
        )
        return {
            "id": request.id,
            "status": request.status,
            "zade_wants": _proposal_sentence(request),
            "request": request_dict,
            "work_item": work_item,
            "evidence": _normalize_evidence(merged_metadata),
            "risk": _normalize_risks(merged_metadata, authority),
            "authority_tier": {
                "permission_tier": request.permission_tier,
                "authority_decision": request.authority_decision,
                "reason": authority.get("reason", ""),
                "matched_rule": authority.get("matched_rule", ""),
                "requires_typed_phrase": authority.get("requires_typed_phrase", False),
                "policy_version": authority.get("policy_version", ""),
            },
            "available_actions": {
                "approve": request.status == "pending",
                "deny": request.status == "pending",
                "defer": request.status == "pending",
                "edit": request.status in {"pending", "deferred"},
                "dispatch": can_dispatch,
            },
        }

    def _record_training_event(
        self,
        request: ApprovalRequest,
        *,
        event_type: str,
        outcome: str,
        actor: str,
        note: str = "",
        work_item: dict[str, Any] | None = None,
        edits: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        item = work_item if work_item is not None else self._work_item_for_request(request)
        return self.db.record_approval_training_event(
            approval_request_id=request.id,
            work_item_id=request.source_id if request.source_type == "work_item" else None,
            event_type=event_type,
            outcome=outcome,
            actor=actor,
            note=note,
            action=request.action,
            target=request.target,
            permission_tier=request.permission_tier,
            authority_decision=request.authority_decision,
            authority=request.authority,
            request_snapshot=_request_to_dict(request),
            work_item_snapshot=item or {},
            edits=edits,
            metadata=metadata,
        )

    @staticmethod
    def db_now() -> str:
        from .db import utc_now

        return utc_now()

    def _sync_work_item(self, request: ApprovalRequest, *, status: str, note: str) -> dict[str, Any] | None:
        if request.source_type != "work_item" or request.source_id is None:
            return None
        result = {
            "approval_request_id": request.id,
            "approval_status": status,
            "resolution_note": note,
            "dispatch": "not_dispatched",
        }
        self.db.update_work_item(
            request.source_id,
            status=status,
            authority_decision=request.authority_decision,
            result=result,
            error="" if status == "approved" else note,
        )
        item = self.db.get_work_item(request.source_id)
        return asdict(item) if item else None


def _request_to_dict(request: ApprovalRequest) -> dict[str, Any]:
    return asdict(request)


def _proposal_sentence(request: ApprovalRequest) -> str:
    if request.target:
        return f"Zade wants to {request.action} -> {request.target}"
    return f"Zade wants to {request.action}"


def _normalize_evidence(metadata: dict[str, Any]) -> dict[str, Any]:
    values = []
    for key in ("evidence", "evidence_items", "required_evidence"):
        raw = metadata.get(key)
        if isinstance(raw, list):
            values.extend(raw)
        elif raw:
            values.append(raw)
    evidence_ids = metadata.get("evidence_ids", [])
    if not isinstance(evidence_ids, list):
        evidence_ids = [evidence_ids]
    return {
        "items": values,
        "evidence_ids": evidence_ids,
        "summary": metadata.get("evidence_summary", ""),
    }


def _normalize_risks(metadata: dict[str, Any], authority: dict[str, Any]) -> dict[str, Any]:
    values = []
    for key in ("risk", "risks", "downside_risk", "downside_risks"):
        raw = metadata.get(key)
        if isinstance(raw, list):
            values.extend(raw)
        elif raw:
            values.append(raw)
    reason = authority.get("reason", "")
    if reason:
        values.append({"authority_reason": reason})
    return {
        "items": values,
        "summary": metadata.get("risk_summary", ""),
    }
