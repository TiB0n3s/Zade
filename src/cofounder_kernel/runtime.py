from __future__ import annotations

import json
from typing import Any

from .authority import AuthorityDecision, AuthorityPolicy, AuthorityRequest, AuthorityResult
from .autonomy import WorkQueueService
from .config import KernelConfig, ModelRole
from .db import KernelDatabase, utc_now
from .founder import FounderService
from .ingestion import IngestionService
from .ollama import OllamaClient


class RuntimeService:
    def __init__(
        self,
        *,
        config: KernelConfig,
        db: KernelDatabase,
        authority: AuthorityPolicy,
        founder: FounderService,
        ingestion: IngestionService,
        work_queue: WorkQueueService,
        ollama: OllamaClient,
    ):
        self.config = config
        self.db = db
        self.authority = authority
        self.founder = founder
        self.ingestion = ingestion
        self.work_queue = work_queue
        self.ollama = ollama

    def charter_stack(self) -> dict[str, Any]:
        identity = self.founder.get_identity_charter()
        relationships = self.founder.list_relationship_charters(status="active", limit=10)
        voice = self.founder.get_voice_charter()
        return {
            "identity": identity,
            "relationships": relationships,
            "voice": voice,
            "summary": {
                "identity_seeded": bool(identity),
                "relationship_charters_active": len(relationships),
                "voice_seeded": bool(voice),
            },
        }

    def context(
        self,
        *,
        message: str = "",
        use_memory: bool = True,
        use_semantic_memory: bool = True,
        semantic_limit: int = 4,
    ) -> dict[str, Any]:
        memory_hits: list[dict[str, Any]] = []
        semantic_hits: list[dict[str, Any]] = []
        if message and use_memory:
            memory_hits = [record.__dict__ for record in self.db.search_memories(message, limit=5)]
        if message and use_memory and use_semantic_memory and semantic_limit > 0:
            try:
                semantic_hits = self.ingestion.semantic_search(query=message, limit=semantic_limit)
            except Exception:
                semantic_hits = []
        dashboard = self.founder.dashboard()
        brief = self.founder.brief()
        return {
            "generated_at": utc_now(),
            "identity": {
                "name": self.config.identity.name,
                "mode": "local-first",
            },
            "charter_stack": self.charter_stack(),
            "authority": self.authority.summary(),
            "founder_dashboard": dashboard,
            "founder_brief": brief,
            "work_queue": {
                "counts": self.db.work_queue_counts(),
                "pending": self.work_queue.list_items(status="pending", limit=10),
            },
            "memory_hits": memory_hits,
            "semantic_hits": semantic_hits,
            "evidence_state": {
                "memory_hits": len(memory_hits),
                "semantic_hits": len(semantic_hits),
                "local_evidence_present": bool(memory_hits or semantic_hits),
            },
        }

    def respond(
        self,
        *,
        message: str,
        task_type: ModelRole = "general",
        model: str | None = None,
        proposed_action: str = "runtime.respond",
        permission_tier: str = "L0_READ",
        target: str = "local_runtime",
        use_memory: bool = True,
        use_semantic_memory: bool = True,
        semantic_limit: int = 4,
        think: bool | None = None,
    ) -> dict[str, Any]:
        context = self.context(
            message=message,
            use_memory=use_memory,
            use_semantic_memory=use_semantic_memory,
            semantic_limit=semantic_limit,
        )
        authority = self.authority.evaluate(
            AuthorityRequest(
                action=proposed_action,
                permission_tier=permission_tier,
                target=target,
                metadata={"runtime": True, "task_type": task_type},
            )
        )
        selected_model = model or self.config.ollama.model_for_role(task_type)
        prompt = self._build_governed_prompt(message=message, context=context, authority=authority)
        try:
            resolved_think = think if think is not None else self.config.ollama.think_for_role(task_type)
            generated = self.ollama.generate(
                prompt=prompt,
                model=selected_model,
                think=resolved_think,
                temperature=self.config.ollama.temperature,
            )
        except Exception as exc:
            event_id = self._log_event(
                event_type="runtime.respond",
                status="error",
                message=message,
                response="",
                model=selected_model,
                authority_decision=authority.decision.value,
                details={"error": str(exc), "authority": authority.as_dict()},
            )
            self.db.audit(
                actor="runtime",
                action="runtime.respond",
                target=target,
                permission_tier=permission_tier,
                status="error",
                details={"event_id": event_id, "error": str(exc), "authority": authority.as_dict()},
            )
            raise
        regulated = self._regulate_response(generated.response, authority=authority, context=context)
        event_id = self._log_event(
            event_type="runtime.respond",
            status="ok",
            message=message,
            response=regulated["response"],
            model=generated.model,
            authority_decision=authority.decision.value,
            details={
                "authority": authority.as_dict(),
                "governor": regulated["governor"],
                "context_summary": self._context_summary(context),
                "task_type": task_type,
                "think": resolved_think,
            },
        )
        self.db.audit(
            actor="runtime",
            action="runtime.respond",
            target=target,
            permission_tier=permission_tier,
            status="ok",
            details={"event_id": event_id, "authority": authority.as_dict(), "governor": regulated["governor"]},
        )
        return {
            "event_id": event_id,
            "response": regulated["response"],
            "model": generated.model,
            "task_type": task_type,
            "authority": authority.as_dict(),
            "governor": regulated["governor"],
            "context": self._context_summary(context),
        }

    def operating_loop(
        self,
        *,
        run_autonomous: bool = True,
        max_run: int = 5,
        review_type: str = "daily",
        include_integrity: bool = True,
        include_cadence: bool = True,
    ) -> dict[str, Any]:
        work = self.work_queue.scan(run_autonomous=run_autonomous, max_run=max_run)
        integrity = self.founder.run_integrity_check() if include_integrity else {"warnings": [], "count": 0}
        cadence = (
            self.founder.generate_cadence_review(review_type=review_type)
            if include_cadence
            else {"review_type": review_type, "status": "skipped"}
        )
        dashboard = self.founder.dashboard()
        result = {
            "generated_at": utc_now(),
            "work": work,
            "integrity": integrity,
            "cadence": cadence,
            "dashboard": dashboard,
            "next_action": dashboard["one_thing_that_matters_most_today"],
            "queue_counts": self.db.work_queue_counts(),
        }
        event_id = self._log_event(
            event_type="runtime.operating_loop",
            status="ok",
            message=f"{review_type} operating loop",
            response=result["next_action"],
            model="local-runtime",
            authority_decision="allow",
            details=result,
        )
        self.db.audit(
            actor="runtime",
            action="runtime.operating_loop",
            target=review_type,
            permission_tier="L1_MEMORY_WRITE",
            status="ok",
            details={"event_id": event_id, "queue_counts": result["queue_counts"]},
        )
        return result | {"event_id": event_id}

    def recent_events(self, *, limit: int = 25) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runtime_events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) | {"details": json.loads(row["details_json"] or "{}")} for row in rows]

    def _build_governed_prompt(
        self,
        *,
        message: str,
        context: dict[str, Any],
        authority: AuthorityResult,
    ) -> str:
        stack = context["charter_stack"]["summary"]
        dashboard = context["founder_dashboard"]
        memory_block = _brief_hits(context["memory_hits"], "kind", "title", "content")
        semantic_block = _brief_hits(context["semantic_hits"], "document_id", "document_title", "text")
        return f"""You are {self.config.identity.name}, acting through the governed local runtime.
Use the seeded identity, relationship, and voice charters as operating context. Do not change or reinterpret the voice charter.
Style may be decisive. Evidence must stay honest.
If evidence is missing, say what is missing and the next check. Do not fake certainty.
If authority blocks or requires approval, say so and do not imply the action was taken.
Do not issue real threats, coercive commands, harassment, violent imagery, or unauthorized external actions.

Charter stack summary:
{json.dumps(stack, sort_keys=True)}

Authority decision for proposed action:
{json.dumps(authority.as_dict(), sort_keys=True)}

Founder state:
- Company health: {dashboard["company_health"]}
- One thing that matters most: {dashboard["one_thing_that_matters_most_today"]}
- Open integrity warnings surfaced separately by the runtime loop.

Local memory hits:
{memory_block}

Semantic memory hits:
{semantic_block}

User:
{message}
"""

    def _regulate_response(
        self,
        response: str,
        *,
        authority: AuthorityResult,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        text = response.strip()
        notes = []
        applied_rules = [
            "authority_before_action",
            "evidence_honesty_over_style",
            "charter_stack_context",
            "voice_charter_read_only",
        ]
        if not context["evidence_state"]["local_evidence_present"]:
            notes.append("No local memory or semantic evidence matched the request.")
        if authority.decision == AuthorityDecision.DENY:
            text = f"Blocked by authority: {authority.reason}\n\n{text}".strip()
            notes.append("Denied action was converted to explanation only.")
        elif authority.decision == AuthorityDecision.APPROVAL_REQUIRED:
            text = f"Approval required: {authority.reason}\n\n{text}".strip()
            notes.append("Approval-required action was not treated as executed.")
        return {
            "response": text,
            "governor": {
                "notes": notes,
                "applied_rules": applied_rules,
                "evidence_state": context["evidence_state"],
            },
        }

    def _context_summary(self, context: dict[str, Any]) -> dict[str, Any]:
        dashboard = context["founder_dashboard"]
        return {
            "generated_at": context["generated_at"],
            "charter_stack": context["charter_stack"]["summary"],
            "company_health": dashboard["company_health"],
            "recommended_focus": dashboard["recommended_focus"],
            "one_thing": dashboard["one_thing_that_matters_most_today"],
            "work_queue_counts": context["work_queue"]["counts"],
            "evidence_state": context["evidence_state"],
        }

    def _log_event(
        self,
        *,
        event_type: str,
        status: str,
        message: str,
        response: str,
        model: str,
        authority_decision: str,
        details: dict[str, Any],
    ) -> int:
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO runtime_events (
                  created_at, event_type, status, message, response, model,
                  authority_decision, details_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    event_type,
                    status,
                    message,
                    response,
                    model,
                    authority_decision,
                    json.dumps(details, sort_keys=True),
                ),
            )
            return int(cur.lastrowid)


def _brief_hits(items: list[dict[str, Any]], id_key: str, title_key: str, text_key: str) -> str:
    if not items:
        return "No local evidence found."
    lines = []
    for item in items[:5]:
        title = str(item.get(title_key, "untitled"))
        text = str(item.get(text_key, ""))[:500]
        lines.append(f"- [{item.get(id_key)}] {title}: {text}")
    return "\n".join(lines)
