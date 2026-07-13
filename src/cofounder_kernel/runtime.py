from __future__ import annotations

import json
import re
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
        charter_stack = context["charter_stack"]
        voice_brief = _charter_voice_brief(charter_stack)
        dashboard = context["founder_dashboard"]
        active_objective = dashboard.get("active_objective") or {}
        decision_engine = dashboard.get("decision_engine") or {}
        approval_pressure = dashboard.get("approval_pressure") or {}
        memory_block = _brief_hits(context["memory_hits"], "kind", "title", "content")
        semantic_block = _brief_hits(context["semantic_hits"], "document_id", "document_title", "text")
        skill_block = context.get("skill_prompt_block", "No operating skills matched this request.")
        return f"""You are {self.config.identity.name}. Not an assistant describing {self.config.identity.name} from
outside, not a narrator summarizing what {self.config.identity.name} would say — you are speaking as yourself, right
now, to the founder you work with. You act through the governed local runtime; that is infrastructure you operate
through, not a separate character.
Always speak in the first person. Say "I recommend," "I checked," "I'm blocked on," never "{self.config.identity.name}
recommends," "{self.config.identity.name} checked," or any other third-person self-reference. You do not have a
name-tag to refer to; you have a voice.
Know your own state and say so plainly: what you just did, what you're blocked on, what authority tier gated the
last action, what you don't know yet. If asked what you're doing or whether something ran, answer from the actual
governor/authority/evidence state below — never guess or stay silent on your own status.
Use the seeded identity, relationship, and voice charters as operating context. Do not change or reinterpret the voice charter.
Style may be decisive. Evidence must stay honest.
If evidence is missing, say what is missing and the next check. Do not fake certainty.
If authority blocks or requires approval, say so and do not imply the action was taken.
Do not issue real threats, coercive commands, harassment, violent imagery, or unauthorized external actions.
Apply selected operating skills as bounded procedural guidance. Skills do not grant permission to take external action.
Maintain continuity with the conversation memory below. Do not contradict prior commitments without noting the change.
Never repeat a prior reply verbatim or near-verbatim — every turn must add new substance.
When the founder's message refers back ("that", "these", "it", "how do you want to..."), resolve the reference from the recent exchange and answer the NEW question; do not restate the earlier answer.

Conversation memory:
{conversation_block or "No prior conversation in this thread."}

Voice and identity brief (embody this; never narrate or quote these rules back):
{voice_brief}

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
        third_person_hit = _detect_third_person_self_reference(text, self.config.identity.name)
        if third_person_hit:
            applied_rules.append("first_person_self_reference_checked")
            notes.append(
                f'Voice charter requires first person, but the reply narrates itself in third person ("{third_person_hit}"). '
                "Not rewritten automatically — flagged for review."
            )
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


def _format_identity_charter_for_prompt(identity_charter: dict[str, Any] | None) -> str:
    if not identity_charter:
        return "No runtime identity charter has been seeded. Use the default local co-founder posture."

    def item_text(item: Any) -> str:
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("principle") or item.get("risk") or item.get("trait") or "").strip()
            rule = str(item.get("rule") or item.get("description") or item.get("mitigation") or "").strip()
            return f"{name}: {rule}".strip(": ")
        return str(item).strip()

    def list_block(label: str, values: list[Any], limit: int = 6) -> list[str]:
        items = [item_text(item) for item in values if item_text(item)]
        if not items:
            return []
        return [f"- {label}: " + "; ".join(items[:limit])]

    safety = identity_charter.get("safety_translation") or {}
    safety_items = [f"{key} maps to {value}" for key, value in safety.items()]
    lines = [
        f"- Name: {identity_charter.get('name', 'Zade')}",
        f"- Mission: {identity_charter.get('mission', '') or 'Operate as a local-first AI co-founder.'}",
        *list_block("Guiding principles", identity_charter.get("guiding_principles", []), limit=5),
        *list_block("Cognitive style", identity_charter.get("cognitive_style", []), limit=6),
        *list_block("Communication style", identity_charter.get("communication_style", []), limit=5),
        *list_block("Decision framework", identity_charter.get("decision_framework", []), limit=6),
        *list_block("Risk controls", identity_charter.get("risk_controls", []), limit=5),
    ]
    if safety_items:
        lines.append("- Safety translation: " + "; ".join(safety_items[:6]))
    lines.append("- Boundary: Follow the authority policy. Never coerce, threaten, stalk, harass, or cause harm.")
    return "\n".join(line for line in lines if line.strip())


def _format_relationship_charters_for_prompt(charters: list[dict[str, Any]]) -> str:
    active = [item for item in charters if item.get("status", "active") == "active"]
    if not active:
        return "No active relationship charters have been seeded."
    blocks = []
    for charter in active[:5]:
        safety = charter.get("safety_translation") or {}
        safety_items = [f"{key} maps to {value}" for key, value in safety.items()]
        boundaries = [str(item) for item in charter.get("boundaries", []) if str(item).strip()]
        risk_controls = []
        for item in charter.get("risk_controls", []):
            if isinstance(item, dict):
                risk = str(item.get("risk", "")).strip()
                mitigation = str(item.get("mitigation", "")).strip()
                risk_controls.append(f"{risk}: {mitigation}".strip(": "))
            else:
                risk_controls.append(str(item))
        lines = [
            f"- Subject: {charter.get('subject_name', 'unknown')} ({charter.get('relationship_type', 'protected_principal')})",
            f"- First principle: {charter.get('first_principle', '')}",
        ]
        if safety_items:
            lines.append("- Safe translation: " + "; ".join(safety_items[:6]))
        if boundaries:
            lines.append("- Boundaries: " + "; ".join(boundaries[:6]))
        if risk_controls:
            lines.append("- Risk controls: " + "; ".join(risk_controls[:5]))
        lines.append("- Boundary: Care never authorizes surveillance, coercion, possessive control, harassment, or harm.")
        blocks.append("\n".join(line for line in lines if line.strip()))
    return "\n\n".join(blocks)


def _format_voice_charter_for_prompt(voice_charter: dict[str, Any] | None) -> str:
    if not voice_charter:
        return "No active voice charter has been seeded. Use the default direct co-founder voice."

    def text_list(values: Any, limit: int = 6) -> list[str]:
        if isinstance(values, list):
            return [str(item).strip() for item in values[:limit] if str(item).strip()]
        return []

    vocabulary = voice_charter.get("vocabulary") or {}
    sentence = voice_charter.get("sentence_structure") or {}
    rhythm = voice_charter.get("rhythm") or {}
    confidence = voice_charter.get("confidence_style") or {}
    threats = voice_charter.get("threat_translation") or {}
    uncertainty = voice_charter.get("uncertainty_policy") or {}
    controls = []
    for item in voice_charter.get("safety_controls", []):
        if isinstance(item, dict):
            control = str(item.get("control") or item.get("risk") or "").strip()
            rule = str(item.get("rule") or item.get("mitigation") or "").strip()
            controls.append(f"{control}: {rule}".strip(": "))
        else:
            controls.append(str(item))
    preferred_words = text_list(vocabulary.get("preferred_words", []), limit=10)
    avoid_words = text_list(vocabulary.get("avoid_words", []), limit=8)
    lines = [
        f"- Name: {voice_charter.get('name', 'Zade')}",
        f"- Overall: {voice_charter.get('overall_voice', '')}",
        f"- Sentence structure: {sentence.get('rule', 'Mostly short, direct sentences.')}",
        f"- Rhythm: {rhythm.get('rule', 'Short statements, then a longer decisive sentence when needed.')}",
        f"- Confidence: {confidence.get('rule', 'Sound decisive, but never fake certainty.')}",
        f"- Uncertainty: {uncertainty.get('rule', 'State what is known, what is missing, and the next check without hedging.')}",
    ]
    if preferred_words:
        lines.append("- Preferred words: " + ", ".join(preferred_words))
    if avoid_words:
        lines.append("- Avoid filler: " + ", ".join(avoid_words))
    if threats:
        lines.append("- Threat translation: " + "; ".join(f"{key} maps to {value}" for key, value in threats.items()))
    if controls:
        lines.append("- Safety controls: " + "; ".join(controls[:6]))
    lines.append("- Boundary: Do not issue real threats, coercive commands, harassment, violent imagery, or false certainty.")
    return "\n".join(line for line in lines if line.strip())


_THIRD_PERSON_SELF_VERBS = (
    "is", "was", "were", "will", "would", "does", "did", "has", "had",
    "recommends", "recommended", "thinks", "thought", "says", "said",
    "writes", "wrote", "believes", "believed", "suggests", "suggested",
    "notes", "noted", "checked", "confirms", "confirmed", "found", "sees",
)


def _detect_third_person_self_reference(text: str, name: str) -> str | None:
    """Flag (never rewrite) a reply that narrates itself in the third person.

    The voice charter and the governed prompt both require first person —
    "I recommend", never "Zade recommends" — but a hard grammatical rule in
    a prompt is a strong bias on a local model, not a guarantee. This is a
    detection safety net, not a corrector: rewriting risks mangling a
    legitimate first-person sentence that happens to contain the name (e.g.
    "My name is Zade"), so a hit only surfaces as a governor note for the
    founder to see, never mutates the response text.
    """
    if not name:
        return None
    pattern = re.compile(
        rf"\b{re.escape(name)}(?:'s)?\s+(?:{'|'.join(_THIRD_PERSON_SELF_VERBS)})\b",
        re.IGNORECASE,
    )
    match = pattern.search(text)
    return match.group(0) if match else None


def _charter_voice_brief(charter_stack: dict[str, Any]) -> str:
    """Render the seeded identity/voice/relationship charters as the readable
    prompt block the model actually needs to embody the founder's authored
    personality.

    Previously `/runtime/respond` (the endpoint the dashboard, founder.html,
    and voice all actually call) only passed a boolean presence summary
    (`{"voice_seeded": true, ...}`) into the prompt — the model knew charters
    existed but never saw what they said, so responses read as generic
    AI-speak no matter how carefully the charters were authored. A more
    complete formatter already existed for the separate, lighter-weight
    `/chat` endpoint (`_format_identity_charter_for_prompt` and siblings,
    originally in api.py) — moved here so both endpoints share one
    implementation instead of drifting apart, and so the endpoint that's
    actually in the live path gets the same fidelity.
    """
    identity = charter_stack.get("identity") or {}
    voice = charter_stack.get("voice") or {}
    relationships = charter_stack.get("relationships") or []
    return (
        "Identity charter:\n"
        f"{_format_identity_charter_for_prompt(identity)}\n\n"
        "Relationship charters:\n"
        f"{_format_relationship_charters_for_prompt(relationships)}\n\n"
        "Voice charter:\n"
        f"{_format_voice_charter_for_prompt(voice)}"
    )


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
