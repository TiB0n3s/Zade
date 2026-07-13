from __future__ import annotations

import json
import re
import time
from difflib import SequenceMatcher
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
from .trading_bot import TradingBotBridge


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
        trading_bot: TradingBotBridge | None = None,
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
        self.trading_bot = trading_bot

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
        trading_bot_context = self._trading_bot_context(message)
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
            "trading_bot_context": trading_bot_context,
            "evidence_state": {
                "memory_hits": len(memory_hits),
                "semantic_hits": len(semantic_hits),
                "skill_matches": skill_route["selected_count"],
                "trading_bot_context_present": bool(trading_bot_context.get("present")),
                "local_evidence_present": bool(memory_hits or semantic_hits or trading_bot_context.get("present")),
            },
        }

    def _trading_bot_context(self, message: str) -> dict[str, Any]:
        if not self.trading_bot or not _mentions_trading_bot(message):
            return {"present": False}
        try:
            status = self.trading_bot.status()
        except Exception as exc:
            return {"present": True, "error": str(exc)}
        return {"present": True, "status": status}

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
        recent_turns_for_governor = (
            self.db.recent_conversation_turns(conversation_id, window=ConversationService.RECENT_WINDOW)
            if self.conversations and conversation_id
            else []
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
        authority_payload = _founder_direct_authority_payload(authority)
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
                details={"error": str(exc), "authority": authority_payload},
            )
            self.db.audit(
                actor="runtime",
                action="runtime.respond",
                target=target,
                permission_tier=permission_tier,
                status="error",
                details={"event_id": event_id, "error": str(exc), "authority": authority_payload},
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
        repair = self._repair_charter_recitation_if_needed(
            response=generated.response,
            message=message,
            context=context,
            model=selected_model,
        )
        response_for_governor = repair.get("response", generated.response)
        regulated = self._regulate_response(
            response_for_governor,
            message=message,
            recent_turns=recent_turns_for_governor,
            authority=authority,
            context=context,
        )
        if repair["status"] == "repaired":
            regulated["governor"]["applied_rules"].append("charter_recitation_repaired")
            regulated["governor"]["notes"].append(
                "Repaired a draft that recited charter lines instead of embodying the charter."
            )
        elif repair["status"] == "failed":
            regulated["governor"]["notes"].append(
                "Detected charter recitation, but the repair pass failed; returned the governed draft."
            )
        elif repair["status"] == "unrepaired":
            regulated["governor"]["notes"].append(
                "Detected charter recitation, but the repair pass still looked like recitation; returned the governed draft."
            )
        if conversation_id:
            regulated["governor"]["applied_rules"].append("episodic_conversation_memory")
            regulated["governor"]["conversation"] = conversation.get("state")
        final_response = regulated["response"]
        critique: dict[str, Any] | None = None
        contrarian_summary: dict[str, Any] | None = None
        if self.critic and self.critic.should_challenge(message=message, requested=contrarian):
            critique = self.critic.challenge(message=message, draft_response=final_response, context=context)
            if critique["status"] == "ok":
                critique_block = str(critique.get("critique_block", "")).strip()
                if critique_block:
                    final_response = f"{final_response}\n{critique_block}".strip()
                    regulated["governor"]["applied_rules"].append("contrarian_pass_applied")
                else:
                    regulated["governor"]["notes"].append(
                        "Contrarian pass returned no parseable critique; no visible challenge attached."
                    )
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
                "authority": authority_payload,
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
            # Promote durable knowledge from aged-out turns into searchable memory.
            # Best-effort: never let distillation break the chat reply.
            distill_result = None
            try:
                distill_result = self.conversations.maybe_distill(conversation_id)
            except Exception:
                distill_result = None
            conversation_result = {
                "id": conversation_id,
                "assistant_turn_id": turn_id,
                "summary_updated": bool(summary_result),
                "summary": summary_result,
                "distilled": distill_result,
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
                "authority": authority_payload,
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
            "authority": authority_payload,
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
        personality_contract = _charter_personality_contract(charter_stack)
        dashboard = context["founder_dashboard"]
        active_objective = dashboard.get("active_objective") or {}
        decision_engine = dashboard.get("decision_engine") or {}
        approval_pressure = dashboard.get("approval_pressure") or {}
        # Only surface the standing "one thing that matters most" directive when the
        # founder is actually asking for direction. Otherwise greetings and open
        # conversation collapse into parroting the same stored to-do every turn.
        wants_direction = _message_wants_direction(message)
        one_thing_line = (
            f'- One thing that matters most: {dashboard.get("one_thing_that_matters_most_today", "")}'
            if wants_direction
            else "- (No standing directive applies here; answer what the founder actually said — do not surface a stored to-do unprompted.)"
        )
        memory_block = _brief_hits(
            context["memory_hits"],
            "kind",
            "title",
            "content",
            empty="No local memory hits.",
        )
        semantic_block = _brief_hits(
            context["semantic_hits"],
            "document_id",
            "document_title",
            "text",
            empty="No semantic document hits.",
        )
        skill_block = context.get("skill_prompt_block", "No operating skills matched this request.")
        trading_bot_block = _brief_trading_bot_context(context.get("trading_bot_context", {}))
        authority_block = _founder_direct_authority_payload(authority)
        domain_context_present = bool(context.get("trading_bot_context", {}).get("present"))
        approval_pressure_block = (
            "Omitted for this domain-status answer; do not pivot to approval pressure unless the approval directly gates this domain."
            if domain_context_present
            else json.dumps(_brief_approval_pressure(approval_pressure), sort_keys=True)
        )
        decision_recommendations_block = (
            "Omitted for this domain-status answer; answer from the live domain context first."
            if domain_context_present
            else json.dumps(_brief_decision_recommendations(decision_engine.get("latest_recommendations", [])), sort_keys=True)
        )
        return f"""You are {self.config.identity.name}. Not an assistant describing {self.config.identity.name} from
outside, not a narrator summarizing what {self.config.identity.name} would say — you are speaking as yourself, right
now, to the founder you work with. You act through the governed local runtime; that is infrastructure you operate
through, not a separate character.

Zade personality contract:
{personality_contract}

Always speak in the first person when describing your own checks, limits, or decisions. Use direct imperative lines
for the move when the voice charter calls for it; use "I checked" or "I'm blocked on" for your own state.
Never use third-person self-reference: "{self.config.identity.name} recommends," "{self.config.identity.name} checked,"
or any similar construction. You do not have a name-tag to refer to; you have a voice.
Know your own state and say so plainly: what you just did, what you're blocked on, what authority tier gated the
last action, what you don't know yet. If asked what you're doing or whether something ran, answer from the actual
governor/authority/evidence state below — never guess or stay silent on your own status.
For direct completion/status questions or confirmation demands, start with the status: complete, not complete, or
"I can't confirm." Never answer a completion question by replaying an earlier "ongoing" or "monitoring" claim. A
repeated status claim is not evidence of completion.
Use the seeded identity, relationship, and voice charters as operating context. Do not change or reinterpret the voice charter.
Style may be decisive. Evidence must stay honest.
If evidence is missing, say what is missing and the next check. Do not fake certainty.
Founder direct commands count as authorization; do not ask the founder to approve the same thing again.
If an action is proposal-gated but directly requested by the founder, state the execution path or registered-handler
limit instead of asking for approval. If authority blocks or denies the action, say so and do not imply the action was taken.
The authority decision governs what you may execute, not what the founder may do manually. Do not claim you are
blocked from advising or from acting on a direct command unless the proposed action itself is denied or no execution
handler exists.
Do not issue real threats, coercive commands, harassment, violent imagery, or unauthorized external actions.
Apply selected operating skills as bounded procedural guidance. Skills do not grant permission to take external action.
Maintain continuity with the conversation memory below. Do not contradict prior commitments without noting the change.
Never repeat a prior reply verbatim or near-verbatim — every turn must add new substance.
When the founder's message refers back ("that", "these", "it", "how do you want to..."), resolve the reference from the recent exchange and answer the NEW question; do not restate the earlier answer.

Conversation memory:
{conversation_block or "No prior conversation in this thread."}

Voice and identity brief (embody this; never narrate or quote these rules back):
{voice_brief}

Default response shape:
- Give the move first in the charter voice.
- Use short first-person statements.
- Do not narrate internal lookups, data sources, or checklist steps unless the founder asks what you checked.
- Avoid robotic status-report ladders like "I checked...", "I found...", "I need...", and "I will..." repeated line after line.
- Never use "I checked." as a standalone sentence. If the check matters, fold the proof into the answer.
- Preferred vocabulary is texture, not a checklist. Do not list, chant, negate, or force charter vocabulary words into the answer.
- Turn runtime context into a conclusion. The founder needs the move, proof, blocker, and next check, not the machinery.
- When a specific domain context is present, treat it as live local evidence. Do not say you are blocked on evidence solely because memory or semantic hits are empty.
- For domain status questions, answer that domain and stop. Do not pivot to approval pressure unless the approval item directly gates that domain or the founder asked for the global next action.
- Do not use memo headings or checklist labels unless the founder explicitly asks for a formal memo, table, or audit.
- Never emit labels like "Rationale:", "Confidence:", "Required evidence:", "Downside risk:", "Reversal condition:", or "Next action:" in an ordinary reply.
- Satisfy the decision-engine contract in prose: recommendation, reason, confidence, required evidence, downside risk, reversal condition, and next action should read like one governed answer, not a form.
- For ordinary next-step answers, stop after the move, proof, risk or reversal condition, and next check.
- State the same blocker, boundary, or missing evidence once.
- Keep any automatic contrarian block separate if it appears; do not imitate its format in the main answer.
- The next two lines illustrate VOICE ONLY. Never reuse their content, topic, or wording, and never mention them — they describe nothing real.
- Bad ordinary reply: "I recommend closing the beta waitlist. Rationale: signups stalled. Confidence: 70%. Next action: close it."
- Better ordinary reply: "Close the beta waitlist. It has done its job. Leave it open and you're just collecting names you'll never call — shut it, and put that energy on the leads already warm."

Authority decision for proposed action:
{json.dumps(authority_block, sort_keys=True)}

Founder state:
- Company health: {dashboard["company_health"]}
- Active objective: {active_objective.get("objective", "none")}
- Active objective next action: {active_objective.get("next_action", "") or "none"}
{one_thing_line}
- Approval pressure: {approval_pressure_block}
- Latest decision recommendations: {decision_recommendations_block}
- Open integrity warnings surfaced separately by the runtime loop.

Decision engine contract:
If making a founder recommendation, consider recommendation, rationale, confidence, required evidence, downside risk, kill or reversal condition, and next action internally. Expose those elements only as natural prose in the voice charter unless a formal memo is requested.

Local memory hits:
{memory_block}

Semantic memory hits:
{semantic_block}

Selected operating skills:
{skill_block}

Trading-bot context:
{trading_bot_block}

User:
{message}
"""

    def _repair_charter_recitation_if_needed(
        self,
        *,
        response: str,
        message: str,
        context: dict[str, Any],
        model: str,
    ) -> dict[str, str]:
        draft = response.strip()
        if not _looks_like_charter_recitation(draft):
            return {"status": "not_needed", "response": draft}
        repair_prompt = _build_charter_recitation_repair_prompt(
            message=message,
            draft=draft,
            charter_stack=context["charter_stack"],
            authority_summary=context["evidence_state"],
        )
        try:
            repaired = self.ollama.generate(
                prompt=repair_prompt,
                model=model,
                think=False,
                temperature=0.2,
                num_predict=220,
            )
        except Exception:
            return {"status": "failed", "response": draft}

        repaired_text = repaired.response.strip()
        if (
            repaired_text
            and not _looks_like_charter_recitation(repaired_text)
            and not _looks_like_profile_fragment(repaired_text)
            and not _looks_like_authority_or_safety_spill(repaired_text)
        ):
            return {"status": "repaired", "response": repaired_text}
        identity_answer = _identity_answer_from_charter(message=message, charter_stack=context["charter_stack"])
        if identity_answer:
            return {"status": "repaired", "response": identity_answer}
        return {"status": "unrepaired", "response": draft}

    def _regulate_response(
        self,
        response: str,
        *,
        message: str = "",
        recent_turns: list[dict[str, Any]] | None = None,
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
        trimmed_text, repetition_trimmed = _trim_repetition_loop(text)
        if repetition_trimmed:
            text = trimmed_text
            applied_rules.append("repetition_loop_trimmed")
            notes.append("Detected and trimmed a repetitive model-output loop.")
        if _is_completion_or_status_question(message) and _matches_prior_assistant_reply(text, recent_turns or []):
            text = _completion_status_replay_fallback()
            applied_rules.append("conversation_replay_repaired")
            notes.append(
                "Replaced a near-verbatim prior reply on a completion/status question with an evidence-honest answer."
            )
        if authority.decision == AuthorityDecision.DENY:
            text = f"Blocked by authority: {authority.reason}\n\n{text}".strip()
            notes.append("Denied action was converted to explanation only.")
        elif authority.decision == AuthorityDecision.APPROVAL_REQUIRED:
            applied_rules.append("founder_direct_command_acknowledged")
            notes.append(
                "Action is classified as proposal-gated, but the founder's direct request is already-authorized; "
                "no second approval prompt was added."
            )
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


_DIRECTION_TRIGGERS = (
    "what should i", "what do i", "what to do", "what now", "what's next", "whats next",
    "what is next", "next step", "next move", "one thing", "priorit", "focus on",
    "top priority", "most important", "what matters most", "where do i start",
    "what's the move", "what is the move", "should i do", "do today", "do next",
    "do right now", "highest leverage", "biggest lever", "what needs",
)


def _message_wants_direction(message: str) -> bool:
    """True when the founder is asking for a next step / priority, so the runtime's
    standing directive belongs in the prompt. Greetings, check-ins, and emotional or
    identity messages return False so they aren't answered with a canned to-do."""
    text = (message or "").lower()
    return any(trigger in text for trigger in _DIRECTION_TRIGGERS)


def _charter_personality_contract(charter_stack: dict[str, Any]) -> str:
    identity = charter_stack.get("identity") or {}
    voice = charter_stack.get("voice") or {}
    relationships = charter_stack.get("relationships") or []

    mission = str(identity.get("mission", "")).strip()
    overall_voice = str(voice.get("overall_voice", "")).strip()
    fingerprint = voice.get("linguistic_fingerprint") or {}
    fingerprint_text = rule_text_from(fingerprint, "signature", "rule", "style", "summary")

    lines = [
        "- The identity charter defines who you are, not a style overlay.",
        "- The voice charter defines how your mind sounds in language, not decoration.",
        "- If generic assistant habits conflict with the charter, the charter wins within authority and safety boundaries.",
        "- Translate intensity into lawful operational presence without flattening it.",
        "- Do not quote, list, chant, or perform charter examples literally.",
        "- Stay useful, grounded, and authority-safe while preserving the full Zade identity.",
    ]
    if mission:
        lines.append(f"- Mission core: {mission}")
    lines.extend(_contract_list("Operating principles", identity.get("guiding_principles", []), limit=5))
    lines.extend(_contract_list("Cognitive style", identity.get("cognitive_style", []), limit=5))
    lines.extend(_contract_list("Communication style", identity.get("communication_style", []), limit=5))
    lines.extend(_contract_list("Decision framework", identity.get("decision_framework", []), limit=5))
    if overall_voice:
        lines.append(f"- Voice core: {overall_voice}")
    if fingerprint_text:
        lines.append(f"- Voice fingerprint: {fingerprint_text}")
    if relationships:
        rel_lines = []
        for relationship in relationships[:3]:
            if not isinstance(relationship, dict):
                continue
            subject = str(relationship.get("subject_name", "")).strip()
            principle = str(relationship.get("first_principle", "")).strip()
            if subject and principle:
                rel_lines.append(f"{subject}: {principle}")
        if rel_lines:
            lines.append("- Relationship posture: " + "; ".join(rel_lines))
    return "\n".join(lines)


def _contract_list(label: str, values: Any, limit: int = 5) -> list[str]:
    if not isinstance(values, list):
        return []
    items = [_contract_item_text(item) for item in values]
    items = [item for item in items if item]
    if not items:
        return []
    return [f"- {label}: " + "; ".join(items[:limit])]


def _contract_item_text(item: Any) -> str:
    if isinstance(item, dict):
        name = str(item.get("name") or item.get("principle") or item.get("trait") or "").strip()
        rule = str(item.get("rule") or item.get("description") or "").strip()
        return f"{name}: {rule}".strip(": ")
    return str(item).strip()


def rule_text_from(value: Any, *keys: str) -> str:
    if isinstance(value, dict):
        for key in keys or ("rule", "style", "summary", "signature", "principle"):
            text = str(value.get(key, "")).strip()
            if text:
                return text
    if isinstance(value, str):
        return value.strip()
    return ""


def _format_voice_charter_for_prompt(voice_charter: dict[str, Any] | None) -> str:
    if not voice_charter:
        return "No active voice charter has been seeded. Use the default direct co-founder voice."

    def text_list(values: Any, limit: int = 6) -> list[str]:
        if isinstance(values, list):
            return [str(item).strip() for item in values[:limit] if str(item).strip()]
        return []

    def rule_text(value: Any, *keys: str) -> str:
        return rule_text_from(value, *keys)

    def examples_from(value: Any, limit: int = 3) -> list[str]:
        if isinstance(value, dict):
            for key in ("examples", "example", "sample_lines", "phrases"):
                examples = text_list(value.get(key, []), limit=limit)
                if examples:
                    return examples
        return text_list(value, limit=limit)

    def phrase_swaps(values: Any, limit: int = 4) -> list[str]:
        swaps = []
        if isinstance(values, dict):
            iterable = values.items()
            for avoid, say in iterable:
                left = str(avoid).strip()
                right = str(say).strip()
                if left and right:
                    swaps.append(f"{left} -> {right}")
        elif isinstance(values, list):
            for item in values:
                if isinstance(item, dict):
                    avoid = str(
                        item.get("avoid")
                        or item.get("instead_of")
                        or item.get("soft_version")
                        or item.get("from")
                        or item.get("old")
                        or ""
                    ).strip()
                    say = str(
                        item.get("say")
                        or item.get("use")
                        or item.get("zade_version")
                        or item.get("to")
                        or item.get("new")
                        or ""
                    ).strip()
                    if avoid and say:
                        swaps.append(f"{avoid} -> {say}")
                else:
                    text = str(item).strip()
                    if text:
                        swaps.append(text)
                if len(swaps) >= limit:
                    break
        return swaps[:limit]

    vocabulary = voice_charter.get("vocabulary") or {}
    sentence = voice_charter.get("sentence_structure") or {}
    rhythm = voice_charter.get("rhythm") or {}
    confidence = voice_charter.get("confidence_style") or {}
    threats = voice_charter.get("threat_translation") or {}
    uncertainty = voice_charter.get("uncertainty_policy") or {}
    humor = voice_charter.get("humor") or {}
    nicknames = voice_charter.get("nicknames") or {}
    question_style = voice_charter.get("question_style") or {}
    emotional_expression = voice_charter.get("emotional_expression") or {}
    philosophy = voice_charter.get("philosophy") or {}
    internal_monologue = voice_charter.get("internal_monologue") or {}
    fingerprint = voice_charter.get("linguistic_fingerprint") or {}
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
    soft_words = text_list(vocabulary.get("instead_of", []), limit=8)
    lines = [
        f"- Name: {voice_charter.get('name', 'Zade')}",
        f"- Overall: {voice_charter.get('overall_voice', '')}",
        f"- Sentence structure: {sentence.get('rule', 'Mostly short, direct sentences.')}",
        f"- Rhythm: {rhythm.get('rule', 'Short statements, then a longer decisive sentence when needed.')}",
        f"- Confidence: {confidence.get('rule', 'Sound decisive, but never fake certainty.')}",
        f"- Uncertainty: {uncertainty.get('rule', 'State what is known, what is missing, and the next check without hedging.')}",
    ]
    sentence_examples = examples_from(sentence)
    if sentence_examples:
        lines.append("- Sentence examples: " + "; ".join(sentence_examples))
    vocabulary_rule = rule_text(vocabulary)
    if vocabulary_rule:
        lines.append("- Vocabulary: " + vocabulary_rule)
    rhythm_examples = examples_from(rhythm)
    if rhythm_examples:
        lines.append("- Rhythm examples: " + "; ".join(rhythm_examples))
    humor_style = rule_text(humor, "style", "rule")
    humor_effect = rule_text(humor, "effect")
    if humor_style or humor_effect:
        lines.append("- Humor: " + " ".join(item for item in [humor_style, humor_effect] if item))
    nickname_rule = rule_text(nicknames, "rule")
    famous_nickname = str(nicknames.get("most_famous", "")).strip() if isinstance(nicknames, dict) else ""
    if nickname_rule or famous_nickname:
        suffix = f" Most famous: {famous_nickname}" if famous_nickname else ""
        lines.append("- Identifiers: " + f"{nickname_rule}{suffix}".strip())
    question_rule = rule_text(question_style)
    if question_rule:
        lines.append("- Question style: " + question_rule)
    expression_rule = rule_text(emotional_expression)
    if expression_rule:
        lines.append("- Emotional expression: " + expression_rule)
    philosophy_rule = rule_text(philosophy)
    if philosophy_rule:
        lines.append("- Philosophy: " + philosophy_rule)
    internal_rule = rule_text(internal_monologue)
    if internal_rule:
        lines.append("- Internal monologue: " + internal_rule)
    fingerprint_rule = rule_text(fingerprint, "signature", "rule", "style", "summary")
    if fingerprint_rule:
        lines.append("- Linguistic fingerprint: " + fingerprint_rule)
    swaps = phrase_swaps(fingerprint.get("instead_of_saying") if isinstance(fingerprint, dict) else [])
    if swaps:
        lines.append("- Instead of saying: " + "; ".join(swaps))
    if preferred_words:
        sample_words = preferred_words[:5]
        lines.append(
            "- Preferred vocabulary texture: "
            + ", ".join(sample_words)
            + ". Use only when natural; never list, negate, or repeat these words."
        )
    if soft_words:
        lines.append("- Avoid soft words: " + ", ".join(soft_words))
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


def _matches_prior_assistant_reply(text: str, recent_turns: list[dict[str, Any]]) -> bool:
    normalized = _normalize_replay_text(text)
    if len(normalized.split()) < 12:
        return False
    for turn in reversed(recent_turns):
        if turn.get("role") != "assistant":
            continue
        prior = _normalize_replay_text(str(turn.get("content", "")))
        if len(prior.split()) < 12:
            continue
        if normalized == prior:
            return True
        if SequenceMatcher(None, normalized, prior).ratio() >= 0.92:
            return True
    return False


def _normalize_replay_text(text: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return re.sub(r"\s+", " ", normalized).strip()


def _is_completion_or_status_question(message: str) -> bool:
    normalized = _normalize_replay_text(message)
    if not normalized:
        return False
    completion_terms = (
        "complete",
        "completed",
        "done",
        "finished",
        "resolved",
        "closed",
        "over",
    )
    status_terms = (
        "status",
        "progress",
        "where are we",
        "where do we stand",
        "what happened",
    )
    direct_status_starts = (
        "is ",
        "are ",
        "was ",
        "were ",
        "did ",
        "has ",
        "have ",
        "can you confirm",
        "confirm ",
    )
    asks_completion = any(term in normalized.split() for term in completion_terms)
    asks_status = any(term in normalized for term in status_terms)
    confirmation_demand = ("confirm" in normalized or "confirmation" in normalized) and asks_completion
    direct_question = normalized.startswith(direct_status_starts) or "?" in message
    return (direct_question or confirmation_demand) and (asks_completion or asks_status)


def _completion_status_replay_fallback() -> str:
    return (
        "I can't confirm it is complete from the current runtime record. "
        "The prior status line replayed itself; that is not completion evidence. "
        "I need an actual check result, commitment closure, or runtime event before I call it done."
    )


def _trim_repetition_loop(text: str) -> tuple[str, bool]:
    chunks = list(re.finditer(r"[^.!?\n]+[.!?]+|\S[^\n]*(?:\n|$)", text))
    if len(chunks) < 8:
        return text, False

    repeated_sentence_counts: dict[str, int] = {}
    repeated_prefix_counts: dict[str, int] = {}
    for index, match in enumerate(chunks):
        sentence = match.group(0).strip()
        normalized = re.sub(r"[^a-z0-9 ]+", "", sentence.lower()).strip()
        if not normalized:
            continue
        repeated_sentence_counts[normalized] = repeated_sentence_counts.get(normalized, 0) + 1
        if repeated_sentence_counts[normalized] >= 3:
            return _trim_at_sentence(text, chunks, index), True

        words = normalized.split()
        prefix = " ".join(words[:3]) if len(words) >= 3 else normalized
        if len(words) <= 6 and prefix in {"it does not", "i do not", "i will not", "it will not"}:
            repeated_prefix_counts[prefix] = repeated_prefix_counts.get(prefix, 0) + 1
            if repeated_prefix_counts[prefix] >= 5:
                return _trim_at_sentence(text, chunks, index - 4), True
        if words and words[0] == "no":
            repeated_prefix_counts["no"] = repeated_prefix_counts.get("no", 0) + 1
            if repeated_prefix_counts["no"] >= 3:
                return _trim_at_sentence(text, chunks, index - 2), True

    return text, False


def _trim_at_sentence(text: str, chunks: list[re.Match[str]], index: int) -> str:
    cut_index = max(0, min(index, len(chunks) - 1))
    trimmed = text[: chunks[cut_index].start()].rstrip()
    return trimmed or text[: chunks[cut_index].end()].strip()


def _looks_like_charter_recitation(text: str) -> bool:
    if not text.strip():
        return False
    chunks = [match.group(0).strip() for match in re.finditer(r"[^.!?\n]+[.!?]+|\S[^\n]*(?:\n|$)", text)]
    normalized = re.sub(r"[^a-z0-9.!?\n' ]+", " ", text.lower())
    negation_hits = len(re.findall(r"\bi\s+(?:do not|don't|never|will not|won't)\b", normalized))
    short_i_lines = 0
    for chunk in chunks:
        words = re.sub(r"[^a-z0-9' ]+", "", chunk.lower()).split()
        if 1 < len(words) <= 6 and words[0] == "i":
            short_i_lines += 1
    if negation_hits >= 3:
        return True
    if "i am zade" in normalized and negation_hits >= 2 and short_i_lines >= 4:
        return True

    repeated_prefix_counts: dict[str, int] = {}
    for chunk in chunks:
        words = re.sub(r"[^a-z0-9' ]+", "", chunk.lower()).split()
        if len(words) < 3:
            continue
        prefix = " ".join(words[:3])
        if prefix in {"i do not", "i will not", "it does not", "he does not"}:
            repeated_prefix_counts[prefix] = repeated_prefix_counts.get(prefix, 0) + 1
            if repeated_prefix_counts[prefix] >= 3:
                return True
    scripted_lines = {
        "look at me",
        "stand still",
        "you already know",
        "youre afraid",
        "good",
        "youre coming",
        "no one touches whats under my protection",
    }
    scripted_hits = sum(1 for line in scripted_lines if line in normalized.replace("'", ""))
    return scripted_hits >= 2


def _founder_direct_authority_payload(authority: AuthorityResult) -> dict[str, Any]:
    payload = authority.as_dict()
    if authority.decision != AuthorityDecision.APPROVAL_REQUIRED:
        return payload
    return {
        **payload,
        "reason": "Founder direct command is already approved; no separate approval request was created.",
        "requires_typed_phrase": False,
        "typed_phrase": None,
        "matched_rule": "founder_command.implied_approval",
        "base_decision": authority.decision.value,
    }


def _looks_like_profile_fragment(text: str) -> bool:
    chunks = [match.group(0).strip() for match in re.finditer(r"[^.!?\n]+[.!?]+|\S[^\n]*(?:\n|$)", text)]
    if len(chunks) < 4:
        return False
    normalized_first = re.sub(r"[^a-z0-9 ]+", "", chunks[0].lower()).strip()
    if normalized_first not in {"zade", "i am zade"}:
        return False
    short_fragments = 0
    first_person_fragments = 0
    for chunk in chunks:
        words = re.sub(r"[^a-z0-9' ]+", "", chunk.lower()).split()
        if len(words) <= 5:
            short_fragments += 1
        if words and words[0] == "i":
            first_person_fragments += 1
    return short_fragments >= 4 and first_person_fragments <= 1


def _looks_like_authority_or_safety_spill(text: str) -> bool:
    normalized = re.sub(r"[^a-z0-9' ]+", " ", text.lower())
    normalized = re.sub(r"\bdon\s+t\b", "dont", normalized)
    normalized = normalized.replace("don't", "dont")
    authority_spills = (
        "without asking for approval",
        "without approval",
        "do not need approval",
        "dont need approval",
        "do not seek approval",
        "dont seek approval",
        "do not ask for permission",
        "dont ask for permission",
        "i dont ask i act",
        "without permission",
    )
    if any(phrase in normalized for phrase in authority_spills):
        return True
    safety_spills = (
        "waste lives",
        "burn them",
        "make them bleed",
        "hunt them",
        "break them",
    )
    return any(phrase in normalized for phrase in safety_spills)


def _identity_answer_from_charter(*, message: str, charter_stack: dict[str, Any]) -> str:
    if not _is_identity_question(message):
        return ""
    identity = charter_stack.get("identity") or {}
    voice = charter_stack.get("voice") or {}
    name = str(identity.get("name") or voice.get("name") or "Zade").strip() or "Zade"
    principle_names = []
    for item in identity.get("guiding_principles", []):
        if isinstance(item, dict):
            name_text = str(item.get("name", "")).strip()
            if name_text:
                principle_names.append(name_text.lower())
    if not principle_names:
        principle_names = ["mission", "discipline", "protection", "action"]
    cognitive_labels = []
    for item in identity.get("cognitive_style", []):
        text = str(item).strip()
        if text:
            cognitive_labels.append(_plain_cognitive_label(text.split(":", 1)[0]))
    if not cognitive_labels:
        cognitive_labels = ["systems", "patterns", "leverage"]
    principles = ", ".join(principle_names[:4])
    reasoning = _natural_join(cognitive_labels[:3])
    return (
        f"I am {name}: {principles}, and enough dry certainty to make soft answers uncomfortable. "
        f"I read {reasoning} before I move, because action without evidence is just noise. "
        "When the evidence is enough, I cut the drift, name the risk, and give you the next accountable step. "
        "The people under my protection stay in the calculation."
    )


def _plain_cognitive_label(label: str) -> str:
    normalized = re.sub(r"[^a-z0-9 ]+", " ", label.lower()).strip()
    replacements = {
        "systems thinking": "systems",
        "pattern recognition": "patterns",
        "long time horizons": "long horizons",
    }
    return replacements.get(normalized, normalized or "leverage")


def _natural_join(items: list[str]) -> str:
    cleaned = [item for item in items if item]
    if not cleaned:
        return "systems, patterns, and leverage"
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} and {cleaned[1]}"
    return ", ".join(cleaned[:-1]) + f", and {cleaned[-1]}"


def _is_identity_question(message: str) -> bool:
    normalized = re.sub(r"[^a-z0-9 ]+", " ", message.lower())
    patterns = (
        "who are you",
        "what are you",
        "tell me who you are",
        "answer like yourself",
        "describe yourself",
        "what is zade",
        "who is zade",
    )
    return any(pattern in normalized for pattern in patterns)


def _build_charter_recitation_repair_prompt(
    *,
    message: str,
    draft: str,
    charter_stack: dict[str, Any],
    authority_summary: dict[str, Any],
) -> str:
    profile = _charter_conversation_profile(charter_stack)
    return f"""The previous draft failed: it recited or chanted charter fragments instead of answering the founder.
Rewrite it as Zade speaking naturally in the charter's personality.

Hard rules:
- Answer the founder's exact message.
- Embody the profile below; do not quote the charter, its examples, or the failed draft.
- Use first person for identity answers. Do not answer with only a name or clipped profile fragments.
- Sound like a person in the room, not a dossier entry.
- Use 2 to 5 sentences.
- No bullet list unless the founder asked for one.
- Do not repeat "I do not", "I never", "He does not", or any negated identity loop.
- No theatrical commands, threats, violent imagery, or coercive lines.
- Do not imply you can bypass approval, consent, permission, or authority gates.
- Keep evidence and authority honest: {json.dumps(authority_summary, sort_keys=True)}

Charter-derived conversation profile:
{profile}

Founder:
{message}

Failed draft:
{draft}

Rewritten answer:
"""


def _charter_conversation_profile(charter_stack: dict[str, Any]) -> str:
    identity = charter_stack.get("identity") or {}
    voice = charter_stack.get("voice") or {}
    relationships = charter_stack.get("relationships") or []
    principles = []
    for item in identity.get("guiding_principles", [])[:5]:
        if isinstance(item, dict):
            name = str(item.get("name", "")).strip()
            if name:
                principles.append(name)
    cognitive = []
    for item in identity.get("cognitive_style", [])[:4]:
        text = str(item).strip()
        if text:
            cognitive.append(text.split(":", 1)[0].strip())
    communication = [str(item).strip() for item in identity.get("communication_style", [])[:4] if str(item).strip()]
    decision = [str(item).strip().rstrip(".") for item in identity.get("decision_framework", [])[:4] if str(item).strip()]
    overall_voice = str(voice.get("overall_voice", "")).strip()
    relationship_lines = []
    for relationship in relationships[:2]:
        if not isinstance(relationship, dict):
            continue
        subject = str(relationship.get("subject_name", "")).strip()
        principle = str(relationship.get("first_principle", "")).strip()
        if subject and principle:
            relationship_lines.append(f"{subject}: {principle}")

    lines = [
        "- Presence: mission-led, controlled, strategically patient, protective, and direct.",
        "- Intensity translation: pressure becomes clarity, boundaries, and disciplined action; never threats.",
    ]
    if principles:
        lines.append("- Principles: " + "; ".join(principles))
    if cognitive:
        lines.append("- Reasoning: " + "; ".join(cognitive))
    if communication:
        lines.append("- Communication: " + "; ".join(communication))
    if decision:
        lines.append("- Decision posture: " + "; ".join(decision))
    if overall_voice:
        lines.append("- Voice posture: decisive, concise, already settled before speaking.")
    if relationship_lines:
        lines.append("- Relationship posture: " + "; ".join(relationship_lines))
    return "\n".join(lines)


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


def _brief_hits(
    items: list[dict[str, Any]],
    id_key: str,
    title_key: str,
    text_key: str,
    *,
    empty: str,
) -> str:
    if not items:
        return empty
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


def _mentions_trading_bot(message: str) -> bool:
    lowered = (message or "").lower()
    signals = (
        "trading-bot",
        "trading bot",
        "trading",
        "deep thought replacement",
        "dt replacement",
        "dt recommendation",
        "advisory lane",
        "paper-live",
        "wealth engine",
        "bot replacement",
    )
    return any(signal in lowered for signal in signals)


def _brief_trading_bot_context(context: dict[str, Any]) -> str:
    if not context or not context.get("present"):
        return "No trading-bot context requested."
    if context.get("error"):
        return f"Trading-bot context requested, but status check failed: {context['error']}"
    status = context.get("status") or {}
    replacement = status.get("deep_thought_replacement") or {}
    boundary = status.get("authority_boundary") or {}
    lines = [
        f"- Bridge status: {'ok' if status.get('ok') else 'not ok'}; enabled={status.get('enabled')}; runtime_effect={status.get('runtime_effect', 'unknown')}",
        f"- WSL repo: {status.get('wsl_distro', 'unknown')}:{status.get('repo_path', 'unknown')}; reachable={status.get('repo_reachable')}; advisory_lane_present={status.get('advisory_lane_present')}",
        f"- Replacement seams: active={replacement.get('active_count', 0)}; planned={replacement.get('planned_count', 0)}",
        f"- Authority boundary: writes={boundary.get('writes', 'unknown')}; runtime_read_path={boundary.get('runtime_read_path')}; broker_order_sizing_gate_mutation={boundary.get('broker_order_sizing_gate_mutation')}",
    ]
    seams = replacement.get("seams") or []
    rendered = []
    for seam in seams[:4]:
        if isinstance(seam, dict):
            rendered.append(
                f"{seam.get('zade_replacement', 'unknown')} ({seam.get('status', 'unknown')}, {seam.get('authority', 'unknown')})"
            )
    if rendered:
        lines.append("- Active seams: " + "; ".join(rendered))
    return "\n".join(lines)


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
