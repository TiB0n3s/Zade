from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .db import ApprovalRequest, KernelDatabase
from .handlers import ActionHandlerRegistry


NON_APPROVABLE_TIERS = {"L4_IRREVERSIBLE", "DENIED"}


class ApprovalService:
    def __init__(
        self,
        *,
        db: KernelDatabase,
        handlers: ActionHandlerRegistry | None = None,
        typed_confirmation_phrase: str = "make the jump to hyperspace",
    ):
        self.db = db
        self.handlers = handlers
        self.typed_confirmation_phrase = typed_confirmation_phrase

    def list_requests(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return [_request_to_dict(item) for item in self.db.list_approval_requests(status=status, limit=limit)]

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
        request = self._load_pending_request(request_id)
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
            },
        )
        return {
            "request": _request_to_dict(resolved),
            "work_item": work_item,
            "audit_id": audit_id,
            "dispatch": dispatch_result["dispatch"] if dispatch_result else "not_dispatched",
            "dispatch_result": dispatch_result["result"] if dispatch_result else None,
            "note": (
                "Approval recorded and dispatched through a registered local handler."
                if dispatch_result
                else "Approval recorded. External or unmanaged actions still require a registered dispatcher."
            ),
        }

    def deny_request(self, request_id: int, *, resolved_by: str = "founder", note: str = "") -> dict[str, Any]:
        request = self._load_pending_request(request_id)
        resolved = self.db.resolve_approval_request(
            request_id,
            status="denied",
            resolved_by=resolved_by,
            resolution_note=note,
        )
        work_item = self._sync_work_item(resolved, status="denied", note=note)
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
            },
        )
        return {"request": _request_to_dict(resolved), "work_item": work_item, "audit_id": audit_id}

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

    def _load_pending_request(self, request_id: int) -> ApprovalRequest:
        request = self.db.get_approval_request(request_id)
        if not request:
            raise ValueError(f"Approval request not found: {request_id}")
        if request.status != "pending":
            raise ValueError(f"Approval request is already {request.status}.")
        return request

    def _request_for_work_item(self, item_id: int) -> ApprovalRequest:
        request = self.db.get_pending_approval_for_source(source_type="work_item", source_id=item_id)
        if request:
            return request
        item = self.db.get_work_item(item_id)
        if not item:
            raise ValueError(f"Work item not found: {item_id}")
        if item.status != "approval_required":
            raise ValueError(f"Work item is {item.status}, not approval_required.")
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
