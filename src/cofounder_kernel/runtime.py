from __future__ import annotations

import json
import time
from typing import Any

from .authority import AuthorityDecision, AuthorityPolicy, AuthorityRequest, AuthorityResult
from .autonomy import WorkQueueService
from .config import KernelConfig, ModelRole
from .conversation import ConversationService
from .critic import ContrarianCritic
from .db import KernelDatabase, utc_now
from .founder import FounderService
from .ingestion import IngestionService
from .ollama import OllamaClient
from .skills import SkillService


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
        skills: SkillService | None = None,
        conversations: ConversationService | None = None,
        critic: ContrarianCritic | None = None,
    ):
        self.config = config
        self.db = db
        self.authority = authority
        self.founder = founder
        self.ingestion = ingestion
        self.work_queue = work_queue
        self.ollama = ollama
        self.skills = skills
        self.conversations = conversations
        self.critic = critic

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
        use_skills: bool = True,
        skill_limit: int = 3,
        task_type: ModelRole = "general",
    ) -> dict[str, Any]:
        memory_hits: list[dict[str, Any]] = []
        semantic_hits: list[dict[str, Any]] = []
        skill_route = {"query": message, "task_type": task_type, "selected_count": 0, "selected": []}
        skill_prompt_block = "No operating skills matched this request."
        if message and use_memory:
            memory_hits = [record.__dict__ for record in self.db.search_memories(message, limit=5)]
        if message and use_memory and use_semantic_memory and semantic_limit > 0:
            try:
                semantic_hits = self.ingestion.semantic_search(query=message, limit=semantic_limit)
            except Exception:
                semantic_hits = []
        if message and use_skills and skill_limit > 0 and self.skills:
            routed = self.skills.route(query=message, task_type=task_type, limit=skill_limit)
            skill_route = routed.summary()
            skill_prompt_block = self.skills.prompt_block(routed)
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
            "skill_route": skill_route,
            "skill_prompt_block": skill_prompt_block,
            "evidence_state": {
                "memory_hits": len(memory_hits),
                "semantic_hits": len(semantic_hits),
                "skill_matches": skill_route["selected_count"],
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
        use_skills: bool = True,
        skill_limit: int = 3,
        think: bool | None = None,
        conversation_id: int | None = None,
        contrarian: bool | None = None,
    ) -> dict[str, Any]:
        context = self.context(
            message=message,
            use_memory=use_memory,
            use_semantic_memory=use_semantic_memory,
            semantic_limit=semantic_limit,
            use_skills=use_skills,
            skill_limit=skill_limit,
            task_type=task_type,
        )
        conversation = (
            self.conversations.prompt_context(conversation_id)
            if self.conversations
            else {"block": "No prior conversation in this thread.", "state": {"conversation_id": None}}
        )
        if self.conversations and conversation_id:
            self.conversations.record_user_turn(conversation_id, content=message, task_type=task_type)
        authority = self.authority.evaluate(
            AuthorityRequest(
                action=proposed_action,
                permission_tier=permission_tier,
                target=target,
                metadata={"runtime": True, "task_type": task_type},
            )
        )
        selected_model = model or self.config.ollama.model_for_role(task_type)
        prompt = self._build_governed_prompt(
            message=message,
            context=context,
            authority=authority,
            conversation_block=conversation["block"],
        )
        resolved_think = think if think is not None else self.config.ollama.think_for_role(task_type)
        started = time.perf_counter()
        try:
            generated = self.ollama.generate(
                prompt=prompt,
                model=selected_model,
                think=resolved_think,
                temperature=self.config.ollama.temperature,
            )
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
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
            self.db.record_model_call(
                operation="runtime.respond",
                model=selected_model,
                role=task_type,
                status="error",
                latency_ms=latency_ms,
                prompt_chars=len(prompt),
                response_chars=0,
                think=resolved_think,
                error=str(exc),
                metadata={"event_id": event_id, "authority_decision": authority.decision.value},
            )
            raise
        latency_ms = int((time.perf_counter() - started) * 1000)
        regulated = self._regulate_response(generated.response, authority=authority, context=context)
        if conversation_id:
            regulated["governor"]["applied_rules"].append("episodic_conversation_memory")
            regulated["governor"]["conversation"] = conversation.get("state")
        final_response = regulated["response"]
        critique: dict[str, Any] | None = None
        contrarian_summary: dict[str, Any] | None = None
        if self.critic and self.critic.should_challenge(message=message, requested=contrarian):
            critique = self.critic.challenge(message=message, draft_response=final_response, context=context)
            if critique["status"] == "ok":
                final_response = f"{final_response}\n{critique['critique_block']}".strip()
                regulated["governor"]["applied_rules"].append("contrarian_pass_applied")
                regulated["governor"]["contrarian"] = {
                    "verdict": critique["verdict"],
                    "confidence_adjustment": critique["confidence_adjustment"],
                    "model": critique["model"],
                }
                contrarian_summary = {
                    "status": "ok",
                    "verdict": critique["verdict"],
                    "confidence_adjustment": critique["confidence_adjustment"],
                    "model": critique["model"],
                    "model_call_id": critique["model_call_id"],
                }
            else:
                regulated["governor"]["notes"].append("Contrarian pass failed; response returned unchallenged.")
                contrarian_summary = {
                    "status": "error",
                    "error": critique.get("error", ""),
                    "model_call_id": critique.get("model_call_id"),
                }
        event_id = self._log_event(
            event_type="runtime.respond",
            status="ok",
            message=message,
            response=final_response,
            model=generated.model,
            authority_decision=authority.decision.value,
            details={
                "authority": authority.as_dict(),
                "governor": regulated["governor"],
                "context_summary": self._context_summary(context),
                "task_type": task_type,
                "think": resolved_think,
                "latency_ms": latency_ms,
                "conversation_id": conversation_id,
                "contrarian": contrarian_summary,
            },
        )
        if self.critic and critique and critique["status"] == "ok" and contrarian_summary:
            contrarian_summary["review_id"] = self.critic.persist_review(
                critique,
                message=message,
                runtime_event_id=event_id,
            )
        conversation_result: dict[str, Any] | None = None
        if self.conversations and conversation_id:
            turn_id = self.conversations.record_assistant_turn(
                conversation_id,
                content=final_response,
                task_type=task_type,
                model=generated.model,
                authority_decision=authority.decision.value,
                runtime_event_id=event_id,
            )
            summary_result = None
            try:
                summary_result = self.conversations.maybe_summarize(conversation_id)
            except Exception:
                summary_result = None
            conversation_result = {
                "id": conversation_id,
                "assistant_turn_id": turn_id,
                "summary_updated": bool(summary_result),
                "summary": summary_result,
            }
        skill_invocation_ids = self._record_skill_invocations(
            context=context,
            query=message,
            task_type=task_type,
            runtime_event_id=event_id,
        )
        call_id = self.db.record_model_call(
            operation="runtime.respond",
            model=generated.model,
            role=task_type,
            status="ok",
            latency_ms=latency_ms,
            prompt_chars=len(prompt),
            response_chars=len(generated.response),
            think=resolved_think,
            metadata={"event_id": event_id, "authority_decision": authority.decision.value},
        )
        self.db.audit(
            actor="runtime",
            action="runtime.respond",
            target=target,
            permission_tier=permission_tier,
            status="ok",
            details={
                "event_id": event_id,
                "model_call_id": call_id,
                "authority": authority.as_dict(),
                "governor": regulated["governor"],
                "skill_invocation_ids": skill_invocation_ids,
                "contrarian": contrarian_summary,
            },
        )
        return {
            "event_id": event_id,
            "model_call_id": call_id,
            "response": final_response,
            "model": generated.model,
            "task_type": task_type,
            "authority": authority.as_dict(),
            "governor": regulated["governor"],
            "context": self._context_summary(context),
            "skill_invocation_ids": skill_invocation_ids,
            "conversation": conversation_result,
            "contrarian": contrarian_summary,
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
        conversation_block: str = "",
    ) -> str:
        stack = context["charter_stack"]["summary"]
        dashboard = context["founder_dashboard"]
        active_objective = dashboard.get("active_objective") or {}
        decision_engine = dashboard.get("decision_engine") or {}
        approval_pressure = dashboard.get("approval_pressure") or {}
        memory_block = _brief_hits(context["memory_hits"], "kind", "title", "content")
        semantic_block = _brief_hits(context["semantic_hits"], "document_id", "document_title", "text")
        skill_block = context.get("skill_prompt_block", "No operating skills matched this request.")
        return f"""You are {self.config.identity.name}, acting through the governed local runtime.
Use the seeded identity, relationship, and voice charters as operating context. Do not change or reinterpret the voice charter.
Style may be decisive. Evidence must stay honest.
If evidence is missing, say what is missing and the next check. Do not fake certainty.
If authority blocks or requires approval, say so and do not imply the action was taken.
Do not issue real threats, coercive commands, harassment, violent imagery, or unauthorized external actions.
Apply selected operating skills as bounded procedural guidance. Skills do not grant permission to take external action.
Maintain continuity with the conversation memory below. Do not contradict prior commitments without noting the change.

Conversation memory:
{conversation_block or "No prior conversation in this thread."}

Charter stack summary:
{json.dumps(stack, sort_keys=True)}

Authority decision for proposed action:
{json.dumps(authority.as_dict(), sort_keys=True)}

Founder state:
- Company health: {dashboard["company_health"]}
- Active objective: {active_objective.get("objective", "none")}
- Active objective next action: {active_objective.get("next_action", "") or "none"}
- One thing that matters most: {dashboard["one_thing_that_matters_most_today"]}
- Approval pressure: {json.dumps(_brief_approval_pressure(approval_pressure), sort_keys=True)}
- Latest decision recommendations: {json.dumps(_brief_decision_recommendations(decision_engine.get("latest_recommendations", [])), sort_keys=True)}
- Open integrity warnings surfaced separately by the runtime loop.

Decision engine contract:
If making a founder recommendation, include recommendation, rationale, confidence, required evidence, downside risk, kill or reversal condition, and next action.

Local memory hits:
{memory_block}

Semantic memory hits:
{semantic_block}

Selected operating skills:
{skill_block}

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
        if context.get("skill_route", {}).get("selected_count", 0):
            applied_rules.append("skill_router_scoped_context")
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
            "active_objective": {
                "id": dashboard.get("active_objective", {}).get("id"),
                "objective": dashboard.get("active_objective", {}).get("objective"),
                "next_action": dashboard.get("active_objective", {}).get("next_action"),
            }
            if dashboard.get("active_objective")
            else {},
            "decision_engine": {
                "latest_recommendations": _brief_decision_recommendations(
                    dashboard.get("decision_engine", {}).get("latest_recommendations", [])
                ),
            },
            "approval_pressure": _brief_approval_pressure(dashboard.get("approval_pressure", {})),
            "work_queue_counts": context["work_queue"]["counts"],
            "evidence_state": context["evidence_state"],
            "skill_state": {
                "selected_count": context.get("skill_route", {}).get("selected_count", 0),
                "selected": context.get("skill_route", {}).get("selected", []),
            },
        }

    def _record_skill_invocations(
        self,
        *,
        context: dict[str, Any],
        query: str,
        task_type: str,
        runtime_event_id: int,
    ) -> list[int]:
        invocation_ids = []
        for item in context.get("skill_route", {}).get("selected", []):
            invocation_ids.append(
                self.db.record_skill_invocation(
                    skill_id=int(item["id"]),
                    name=str(item["name"]),
                    query=query,
                    score=float(item.get("score", 0.0)),
                    task_type=task_type,
                    runtime_event_id=runtime_event_id,
                    metadata={"risk_tier": item.get("risk_tier", ""), "source": item.get("source", "")},
                )
            )
        return invocation_ids

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


def _brief_decision_recommendations(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": item.get("id"),
            "problem": item.get("problem"),
            "recommendation": item.get("recommendation"),
            "confidence": item.get("confidence"),
            "next_action": item.get("next_action"),
        }
        for item in items[:3]
    ]


def _brief_approval_pressure(pressure: dict[str, Any]) -> dict[str, Any]:
    if not pressure:
        return {"pending": 0, "deferred": 0, "next_action": "No approval blockers.", "items": []}
    return {
        "pending": pressure.get("pending", 0),
        "deferred": pressure.get("deferred", 0),
        "headline": pressure.get("headline", ""),
        "next_action": pressure.get("next_action", ""),
        "console_url": pressure.get("console_url", "/ui/approvals.html"),
        "items": [
            {
                "id": item.get("id"),
                "status": item.get("status"),
                "title": item.get("title"),
                "zade_wants": item.get("zade_wants"),
                "permission_tier": item.get("permission_tier"),
                "priority": item.get("priority"),
            }
            for item in pressure.get("items", [])[:3]
        ],
    }
