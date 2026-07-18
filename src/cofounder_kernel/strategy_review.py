"""founder_brief → Anthropic: the first actual cloud-egress consumer.

Zade assembles a CURATED strategic brief (``founder.brief()`` — company health,
objectives, decisions, risks, knowledge gaps; never raw memory, the authority
policy, or secrets), then routes it through the egress gate as a
``founder_brief → anthropic`` per-request egress. The brief is HELD for founder
approval, and the founder sees the EXACT text that would leave before deciding —
so "curated" is enforced by review, not trust. On approval (typed phrase) the
gate is re-checked at execution, the brief is sent to Anthropic, and the returned
review is filed through the governed memory path.

This is a HELD-OPERATION flow (like the mcp_memory_write gate): the payload rides
on the approval request and executes on approval. It uses the egress gate's
primitives (``EgressPolicy.decide`` + ``EgressAuthorization``) as the
authorization check, so the matrix and provider_policy remain the authority — the
founder's approval mints the per-request grant, nothing self-mints it.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .anthropic_client import AnthropicClient, AnthropicNotConfigured
from .config import KernelConfig
from .db import KernelDatabase
from .egress import DataClass, Disposition, EgressAuthorization, EgressPolicy, EgressRequest, Verdict

SOURCE_TYPE = "strategy_review"
VENDOR = "anthropic"

_SYSTEM = (
    "You are an outside strategic advisor reviewing a founder's brief. Be direct and "
    "specific. Name the weakest assumption, the biggest risk, and the one move that "
    "matters most. Do not flatter."
)


class StrategyReviewService:
    def __init__(
        self,
        *,
        config: KernelConfig,
        db: KernelDatabase,
        founder: Any,
        ingestion: Any,
        anthropic: AnthropicClient | None = None,
        typed_confirmation_phrase: str = "make the jump to hyperspace",
    ):
        self.config = config
        self.db = db
        self.founder = founder
        self.ingestion = ingestion
        self.anthropic = anthropic or AnthropicClient(config.anthropic, provider_policy=config.ollama.provider_policy)
        self.typed_confirmation_phrase = typed_confirmation_phrase

    def _policy(self) -> EgressPolicy:
        return EgressPolicy.from_config(self.config)

    def _request_for(self, request_id: str) -> EgressRequest:
        return EgressRequest(
            request_id=request_id,
            data_class=DataClass.FOUNDER_BRIEF,
            vendor=VENDOR,
            purpose="strategic review",
        )

    # -- readiness readout (no send, no filing) -------------------------------
    def readiness(self) -> dict[str, Any]:
        """At-a-glance readiness of the ``founder_brief → anthropic`` cloud path:
        which of the standing preconditions hold and whether a review COULD reach
        the approval step. Booleans only — no secret is exposed. Pure inspection;
        nothing is sent and no grant is filed.

        The four preconditions mirror the send-time gates: ``[anthropic] enabled``,
        an API key in the env, ``provider_policy`` above ``local_only``, and the
        egress matrix cell not FORBIDDEN. ``ready`` here means those standing gates
        pass — a send STILL needs a per-request founder typed-phrase grant at
        approval time (``requires_per_request_grant``)."""
        info = self.anthropic.provider_info()
        enabled = bool(info.get("enabled"))
        key_present = bool(info.get("key_present"))
        policy = str(info.get("provider_policy") or "local_only")
        policy_allows_cloud = policy != "local_only"
        # Read the matrix cell DIRECTLY rather than via decide(): under local_only
        # decide() short-circuits at the policy overlay before consulting the cell,
        # which would hide the cell's true disposition. Inspection stays honest
        # regardless of the current provider_policy.
        egress_policy = self._policy()
        vendor = egress_policy.vendors.get(VENDOR)
        disposition = (
            egress_policy.matrix.get(DataClass.FOUNDER_BRIEF, {}).get(vendor.tier)
            if vendor
            else None
        )
        egress_cell = disposition.value if disposition else Disposition.FORBIDDEN.value
        egress_cell_ok = disposition in (Disposition.PER_REQUEST, Disposition.STANDING)

        blockers: list[str] = []
        if not enabled:
            blockers.append("[anthropic] enabled = false")
        if not key_present:
            blockers.append(f"{self.config.anthropic.api_key_env} is not set in the environment")
        if not policy_allows_cloud:
            blockers.append("provider_policy = local_only (raise it deliberately to enable cloud egress)")
        if not egress_cell_ok:
            blockers.append(f"egress matrix cell founder_brief → anthropic is {egress_cell}")

        return {
            "provider": VENDOR,
            "tier": vendor.tier.value if vendor else None,
            "model": info.get("model"),
            "endpoint_host": info.get("endpoint_host"),
            "enabled": enabled,
            "key_present": key_present,
            "provider_policy": policy,
            "policy_allows_cloud": policy_allows_cloud,
            "egress_cell": egress_cell,
            "egress_cell_ok": egress_cell_ok,
            "ready": enabled and key_present and policy_allows_cloud and egress_cell_ok,
            "requires_per_request_grant": True,
            "blockers": blockers,
        }

    # -- founder-facing: propose a review -------------------------------------
    def request_review(self, *, focus: str = "", question: str = "") -> dict[str, Any]:
        """Assemble the curated brief and HOLD it for founder approval. Nothing is
        sent. If the gate would DENY outright (e.g. provider_policy=local_only, or
        the cell is FORBIDDEN) the review is refused now, without filing."""
        brief = str(self.founder.brief().get("brief") or "").strip()
        if not brief:
            return {"status": "empty", "message": "No founder brief could be assembled."}
        question = question.strip() or "What is the weakest assumption, and the single most important move?"
        prompt = _compose_prompt(brief, focus=focus.strip(), question=question)

        request_id = f"review-{_stamp()}"
        # Gate pre-check with NO authorization: a per-request cell -> AUTH_REQUIRED
        # (fileable), a FORBIDDEN cell or local_only -> DENY (refuse now).
        decision = self._policy().decide(self._request_for(request_id))
        if decision.verdict is Verdict.DENY:
            self.db.audit(
                actor="strategy_review", action="strategy.review.refused", target=VENDOR,
                permission_tier="L3_EXTERNAL_ACTION", status="denied", details=decision.audit_record(),
            )
            return {"status": "denied", "reason": decision.reason, "matched_rule": decision.matched_rule}

        request, _created = self.db.ensure_approval_request(
            source_type=SOURCE_TYPE,
            source_id=None,
            title=f"Send strategic brief to Anthropic ({self.config.anthropic.model})",
            detail=(focus or question)[:200],
            action="egress.founder_brief",
            target=VENDOR,
            permission_tier="L3_EXTERNAL_ACTION",
            authority_decision="approval_required",
            authority={"reason": "founder_brief → anthropic is a per-request cloud egress."},
            requested_by="strategy_review",
            metadata={
                "request_id": request_id,
                "data_class": DataClass.FOUNDER_BRIEF.value,
                "vendor": VENDOR,
                "focus": focus,
                "question": question,
                "prompt": prompt,        # the EXACT text that would be sent — founder reviews this
                "model": self.config.anthropic.model,
                "byte_estimate": len(prompt),
            },
        )
        self.db.audit(
            actor="strategy_review", action="strategy.review.requested", target=VENDOR,
            permission_tier="L3_EXTERNAL_ACTION", status="pending",
            details={"approval_request_id": request.id, "byte_estimate": len(prompt)},
        )
        return {
            "status": "awaiting_approval",
            "approval_request_id": request.id,
            "preview": prompt,
            "byte_estimate": len(prompt),
            "message": "Curated brief held for founder approval; nothing sent to Anthropic yet.",
        }

    def list_pending(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for r in self.db.list_approval_requests(status="pending", limit=200):
            if r.source_type != SOURCE_TYPE:
                continue
            m = r.metadata or {}
            out.append(
                {
                    "approval_request_id": r.id,
                    "focus": m.get("focus"),
                    "question": m.get("question"),
                    "preview": m.get("prompt"),
                    "model": m.get("model"),
                    "byte_estimate": m.get("byte_estimate"),
                    "created_at": r.created_at,
                }
            )
        return out

    def _load_pending(self, request_id: int) -> Any:
        r = self.db.get_approval_request(request_id)
        if r is None or r.source_type != SOURCE_TYPE:
            raise ValueError(f"Not a strategy-review request: {request_id}")
        if r.status not in {"pending", "deferred"}:
            raise ValueError(f"Strategy-review request already {r.status}.")
        return r

    # -- founder-facing: approve (sends) / deny (discards) --------------------
    def approve(self, request_id: int, *, resolved_by: str = "founder", typed_phrase: str = "") -> dict[str, Any]:
        """Founder authorizes the send. Requires the typed phrase. Re-checks the
        egress gate at execution (so a policy that dropped to local_only after
        the request still blocks the send), then sends and files the review."""
        r = self._load_pending(request_id)
        if typed_phrase.strip() != self.typed_confirmation_phrase:
            raise ValueError(f"Sending the brief requires the typed confirmation phrase: {self.typed_confirmation_phrase}")
        m = r.metadata or {}
        egress_request = self._request_for(str(m.get("request_id")))
        # The founder's approval IS the per-request grant. Re-run the gate WITH it
        # to confirm the matrix + policy still permit this egress right now.
        grant = EgressAuthorization(
            request_id=egress_request.request_id, data_class=DataClass.FOUNDER_BRIEF,
            vendor=VENDOR, granted_by="founder", typed_phrase_ok=True,
        )
        decision = self._policy().decide(egress_request, authorization=grant)
        self.db.audit(
            actor="strategy_review", action="egress.decision", target=VENDOR,
            permission_tier="L3_EXTERNAL_ACTION", status=decision.verdict.value, details=decision.audit_record(),
        )
        if decision.verdict is not Verdict.ALLOW:
            self.db.resolve_approval_request(request_id, status="denied", resolved_by=resolved_by, resolution_note=f"blocked_at_execute:{decision.matched_rule}")
            return {"status": "blocked", "reason": decision.reason, "matched_rule": decision.matched_rule}

        try:
            review_text = self.anthropic.review(prompt=str(m.get("prompt") or ""), system=_SYSTEM)
        except AnthropicNotConfigured as exc:
            # Authorized, but Anthropic isn't set up — leave the request open to retry.
            self.db.audit(
                actor="strategy_review", action="strategy.review.unconfigured", target=VENDOR,
                permission_tier="L3_EXTERNAL_ACTION", status="error", details={"approval_request_id": request_id, "error": str(exc)},
            )
            raise
        # File the returned review through the governed memory path.
        save = self.ingestion.save_memory(
            kind="strategic_review",
            title=f"Strategic review: {m.get('focus') or m.get('question') or 'brief'}",
            content=review_text,
            source="anthropic:strategic-review",
            metadata={"approval_request_id": request_id, "model": m.get("model")},
        )
        self.db.resolve_approval_request(request_id, status="approved", resolved_by=resolved_by, resolution_note="brief sent; review filed")
        self.db.audit(
            actor="strategy_review", action="strategy.review.completed", target=VENDOR,
            permission_tier="L3_EXTERNAL_ACTION", status="ok",
            details={"approval_request_id": request_id, "memory": save.get("status"), "memory_id": save.get("memory_id"), "review_chars": len(review_text)},
        )
        return {"status": "completed", "review": review_text, "memory": save, "approval_request_id": request_id}

    def deny(self, request_id: int, *, resolved_by: str = "founder", note: str = "") -> dict[str, Any]:
        self._load_pending(request_id)
        self.db.resolve_approval_request(request_id, status="denied", resolved_by=resolved_by, resolution_note=note or "review denied")
        self.db.audit(
            actor="strategy_review", action="strategy.review.denied", target=VENDOR,
            permission_tier="L3_EXTERNAL_ACTION", status="denied", details={"approval_request_id": request_id},
        )
        return {"status": "denied", "approval_request_id": request_id}


def _compose_prompt(brief: str, *, focus: str, question: str) -> str:
    parts = [brief, "", "---"]
    if focus:
        parts.append(f"Founder focus: {focus}")
    parts.append(f"Question: {question}")
    return "\n".join(parts).strip()


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
