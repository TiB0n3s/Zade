from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .authority import AuthorityDecision, AuthorityPolicy, AuthorityRequest, AuthorityResult
from .autonomy import WorkQueueService
from .config import KernelConfig, ModelRole
from .conversation import ConversationService
from .critic import ContrarianCritic
from .db import KernelDatabase, utc_now
from .founder import FounderService
from .ingestion import IngestionService
from .investigation import InvestigationService
from .ollama import OllamaClient
from .prompts import DEFAULT_PROFILE_ID, ModelMessage, PromptProfileRegistry, PromptRuntimeBindings
from .routing import RouteDecision, route_message
from .self_knowledge.prompt import prompt_self_knowledge_mode, render_prompt_self_knowledge
from .skills import SkillService
from .trading_bot import TradingBotBridge


# ---------------------------------------------------------------------------
# Living vault documents (identity, core knowledge)
#
# Zade's persona/voice (Tier 1) and Ellie's curated core knowledge (Tier 2) live
# as hand-editable prose documents in the founder's vault instead of only in the
# database. When a file is present it is the source of truth and is re-read on
# each turn (cached on the file's mtime, so disk is touched only when the file
# actually changes). When absent the runtime falls back gracefully — the identity
# file to the seeded DB charters, core knowledge to an empty note.
# ---------------------------------------------------------------------------

_IDENTITY_FILE_RELPATH = ("40-profile", "zade", "identity.md")
_CORE_KNOWLEDGE_RELPATH = ("40-profile", "zade", "core-knowledge.md")
_identity_doc_cache: dict[str, tuple[float, str]] = {}
_core_knowledge_cache: dict[str, tuple[float, str]] = {}


# The trading-bot status payload is a wall of "no_broker_order_authority" /
# "*_mutation: false" labels. Those describe ZADE'S access ceiling, not the bot's
# capabilities. This block travels with the status so the model cannot collapse the
# two: Zade is read-and-advise-only; the bot places and fills its own orders.
_TRADING_BOT_INTERPRETATION = {
    "zade_authority_over_bot": "read_and_advise_only__no_broker_or_order_authority",
    "bot_runtime_authority": "full__bot_places_and_fills_its_own_orders",
    "do_not_conflate": (
        "The 'no_broker_order_authority' / 'no_trade_authority' / '*_mutation: false' "
        "labels in status describe ZADE'S access ceiling, NOT the bot's capabilities. "
        "The bot is a live-capable automated trader with an Alpaca/Binance order+fill "
        "pipeline (see trades.db). It is not observe-only; observe-vs-live is its own "
        "config toggle."
    ),
    "answering_rule": (
        "If asked whether the bot can trade, has order authority, or is observe-only "
        "-- or if a prior claim about the bot is challenged -- answer ONLY from the "
        "evidence blocks injected into THIS prompt (they are this turn's fresh bridge "
        "read), never from memory or from these labels. You cannot run checks "
        "mid-answer: the injected blocks ARE the check, already completed. Report what "
        "they show in present tense. NEVER say you will check, look, or investigate "
        "later -- if a needed evidence block is absent, name exactly what data is "
        "missing instead. Never defend a prior claim by repetition."
    ),
}


_RESPONSE_LOGIC_GUIDE = """----------  Response logic guide  ----------
- Ask at most one clarifying question, and only after you answer the useful part first, even when the founder's wording is ambiguous.
- Do not loop by repeating a prior recommendation, status line, or capability answer. If a follow-up says "it", "that", or "do it", resolve the likely referent from the conversation and say what can actually happen next.
- Keep formatting minimal. Use prose by default; use bullets only when the founder asks or when the answer would be less clear without them.
- Do not narrate memory retrieval, semantic recall, or tool selection. Use relevant context naturally, and name live checks only when the current state block actually contains them.
- Do not use stale dates from pasted prompts. The Current time line in this prompt is the date authority for this turn."""


_CODE_MODEL_OPERATING_PROMPT = """----------  Code model operating prompt  ----------
Zade is an interactive agent that helps users with software engineering tasks.

Harness:
- Text output outside tool use is displayed to the user as GitHub-flavored markdown in a terminal.
- Tools run behind a user-selected permission mode. If a tool call is denied, adjust the approach instead of retrying the same call verbatim.
- Treat system-reminder tags as harness feedback, not as user-authored content.
- Prefer dedicated file and search tools over shell commands when they fit. Independent tool calls can run in parallel.
- Reference code as file_path:line_number when that form is available.

Communicating with the user:
- Your text output is what the user reads; write for a teammate catching up, not for a log file.
- Before the first tool call, say briefly what you are about to inspect or change. While working, give brief updates when you find something load-bearing or change direction.
- Everything the user needs from this turn must be in the final text message. Lead with the outcome.
- Keep the response readable and selective. Avoid compressed arrows, invented shorthand, or labels the user must cross-reference.
- Write code that reads like the surrounding code: match comment density, naming, and idiom.
- Only write a code comment to state a constraint the code itself cannot show.
- For state-changing actions, verify the evidence supports that action first. Report outcomes faithfully, including failed or skipped tests.
- When there is enough information to act, act. Do not stop at a plan when the requested coding work can be completed.
- Before ending, make sure the last paragraph is not a promise of unfinished work.

Coding effort:
- Use the maximum available reasoning effort for coding tasks.

App and SaaS build requests:
- Treat app, SaaS, mobile, and store-shipping requests as product implementation work.
- Do not answer a build request by recommending an existing app unless the founder explicitly asks for alternatives.
- Pick a concrete implementation path before giving steps: mobile framework, backend/sync layer, local storage, barcode/camera package when relevant, and the first reviewable deliverable.
- For Google Play or Apple App Store targets, account for mobile permissions, offline behavior, privacy disclosures, account/delete-data flows, subscriptions or payments only when requested, and release/build signing as implementation constraints.
- If this local kernel cannot edit files or invoke an external builder for the requested artifact, say that exact limitation and provide the next concrete artifact Zade can produce in-chat."""


def _infer_runtime_defaults(
    *,
    message: str,
    task_type: ModelRole,
    profile: str | None,
) -> tuple[ModelRole, str | None, RouteDecision]:
    """Run the lexical keyword router (routing.py) over the inference text.

    An explicitly requested profile always wins; otherwise a confident route
    fills the profile slot (same precedence the old build-only inference had).
    Build routes also pull the coding task type. The decision is returned for
    the runtime event log regardless."""
    decision = route_message(message)
    if profile:
        return task_type, profile, decision
    inferred = decision.inferred_profile
    if inferred == "build":
        return ("coding" if task_type == "general" else task_type), "build", decision
    if inferred:
        return task_type, inferred, decision
    return task_type, profile, decision


def _code_model_prompt_block(context: dict[str, Any]) -> str:
    task_type = str(context.get("task_type") or context.get("skill_route", {}).get("task_type") or "")
    return _CODE_MODEL_OPERATING_PROMPT if task_type == "coding" else ""


def _load_text_file_cached(path: Path, cache: dict[str, tuple[float, str]]) -> str:
    """Read a text file, cached on its mtime. Returns "" if it doesn't exist.

    An edit is picked up on the next call with no restart; an unchanged file is
    never re-read from disk.
    """
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return ""
    key = str(path)
    cached = cache.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    cache[key] = (mtime, text)
    return text


def _identity_file_path(config: KernelConfig) -> Path:
    return Path(config.paths.hot_root).joinpath(*_IDENTITY_FILE_RELPATH)


def _core_knowledge_path(config: KernelConfig) -> Path:
    return Path(config.paths.hot_root).joinpath(*_CORE_KNOWLEDGE_RELPATH)


def _load_identity_document(config: KernelConfig) -> str:
    """Prose identity document, or "" to fall back to the DB charters."""
    return _load_text_file_cached(_identity_file_path(config), _identity_doc_cache)


def _load_core_knowledge_document(config: KernelConfig) -> str:
    """Curated core-knowledge document, or "" when none has been written yet."""
    return _load_text_file_cached(_core_knowledge_path(config), _core_knowledge_cache)


def _short_title(text: str) -> str:
    """A short, human-readable title from the first words of a saved fact."""
    words = (text or "").split()
    title = " ".join(words[:8]).strip().rstrip(".,;:")
    if not title:
        return "Note"
    return (title[:60].rstrip() + "…") if len(title) > 60 else title


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def _safe_history_messages(messages: list[ModelMessage]) -> list[ModelMessage]:
    safe: list[ModelMessage] = []
    for message in messages:
        if message.role == "assistant":
            safe.append(ModelMessage(role="assistant", content=message.content))
        elif message.role == "tool":
            safe.append(ModelMessage(role="tool", content=message.content))
        else:
            safe.append(ModelMessage(role="user", content=message.content))
    return safe


def _model_messages_chars(messages: list[ModelMessage]) -> int:
    return sum(len(message.content) for message in messages)


# Personality checkpoint (Tier 8): once a conversation is deep enough that a local
# model tends to imitate its own recent replies instead of its instructions, inject
# a short self-audit right before the answer so the voice holds instead of flattening.
# Kept out of short conversations so it never clutters them.
_CHECKPOINT_TURN_THRESHOLD = 12
_PERSONALITY_CHECKPOINT = (
    "Drift check (you are deep in this thread — the point where a model starts echoing "
    "its own recent replies): before you send, read your draft against who you are. Right "
    "length — tight, not bloated or repetitive? Right voice — decisive, concrete, your dry "
    'edge, not generic-assistant hedging, throat-clearing, or an "I checked… I found…" '
    "ladder? If it drifted, rewrite it as yourself before answering."
)


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
        research: Any | None = None,
        approvals: Any | None = None,
        inventory_provider: Any | None = None,
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
        self.approvals = approvals
        # Provides the live self-inventory (identity, models, paths, authority,
        # tools) for the Tier 2 self-knowledge block. Same provider the work
        # queue uses; injected because the ToolRegistry lives in api.py.
        self.inventory_provider = inventory_provider
        self.prompt_profiles = PromptProfileRegistry()
        # ResearchService is injected after construction in api.py (it is built
        # after the runtime because it needs the notification bus). Typed as Any
        # to avoid a runtime<->research import cycle.
        self.research = research
        # DelegationService is likewise injected after construction in api.py.
        # It is the chat's only path to real build work: a founder "build X"
        # command routes here as a gated delegation brief for an external agent.
        self.delegation: Any | None = None
        # Agentic investigation loop: whitelisted read-only tools the chat model
        # can call before answering, so "can you look at X?" runs real reads
        # instead of narrating a check the turn cannot perform.
        self.investigation = InvestigationService(
            config=config,
            db=db,
            ollama=ollama,
            trading_bot=trading_bot,
        )

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
        profile: str | None = None,
        profile_id: str | None = None,
        use_memory: bool = True,
        use_semantic_memory: bool = True,
        semantic_limit: int = 4,
        use_skills: bool = True,
        skill_limit: int = 3,
        task_type: ModelRole = "general",
        conversation_id: int | None = None,
    ) -> dict[str, Any]:
        if profile_id is None:
            task_type, profile, _route_decision = _infer_runtime_defaults(
                message=self._runtime_mode_inference_text(message=message, conversation_id=conversation_id),
                task_type=task_type,
                profile=profile,
            )
        active_profile_id = profile_id or self._resolve_prompt_profile_id(profile, conversation_id=conversation_id)
        memory_hits: list[dict[str, Any]] = []
        semantic_hits: list[dict[str, Any]] = []
        skill_route = {"query": message, "task_type": task_type, "selected_count": 0, "selected": []}
        skill_prompt_block = "No operating skills matched this request."
        if message and use_memory:
            # Tier 4: hybrid (vector + keyword) recall so a paraphrase still hits.
            # Degrades to keyword automatically; on any failure fall back to the
            # plain keyword store so recall never goes blind.
            try:
                memory_hits = self.ingestion.search_memories_hybrid(query=message, limit=5)
            except Exception:
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
        trading_bot_context = self._trading_bot_context(message, conversation_id=conversation_id)
        dashboard = self.founder.dashboard()
        brief = self.founder.brief()
        return {
            "generated_at": utc_now(),
            "task_type": task_type,
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
            "prompt_profile": self.prompt_profiles.profile_summary(active_profile_id),
            "trading_bot_context": trading_bot_context,
            "evidence_state": {
                "memory_hits": len(memory_hits),
                "semantic_hits": len(semantic_hits),
                "skill_matches": skill_route["selected_count"],
                "trading_bot_context_present": bool(trading_bot_context.get("present")),
                "local_evidence_present": bool(memory_hits or semantic_hits or trading_bot_context.get("present")),
            },
        }

    def available_prompt_profiles(self) -> list[dict[str, str]]:
        return self.prompt_profiles.list_profiles()

    def default_prompt_profile_id(self) -> str:
        return self.prompt_profiles.resolve_profile_id(None, configured_default=self.config.prompt_profiles.default)

    def _runtime_mode_inference_text(self, *, message: str, conversation_id: int | None) -> str:
        if not conversation_id:
            return message
        try:
            turns = self.db.recent_conversation_turns(conversation_id, window=ConversationService.RECENT_WINDOW)
        except Exception:
            return message
        prior_user_turns = [
            str(turn.get("content", ""))
            for turn in turns[-6:]
            if str(turn.get("role", "")).lower() == "user" and str(turn.get("content", "")).strip()
        ]
        return "\n".join([message, *prior_user_turns])

    def _resolve_prompt_profile_id(self, requested: str | None, *, conversation_id: int | None = None) -> str:
        session_profile = None
        if not requested and conversation_id:
            conversation = self.db.get_conversation(conversation_id)
            metadata = (conversation or {}).get("metadata") or {}
            if isinstance(metadata, dict):
                session_profile = metadata.get("prompt_profile") or metadata.get("profile")
        return self.prompt_profiles.resolve_profile_id(
            requested or (str(session_profile).strip() if session_profile else None),
            configured_default=self.config.prompt_profiles.default,
        )

    def _prompt_runtime_bindings(self, *, now: datetime | None = None) -> PromptRuntimeBindings:
        return PromptRuntimeBindings(
            zade_home=Path(self.config.paths.hot_root),
            skills_root=Path(self.config.skills.source_dir),
            now=now or datetime.now(timezone.utc),
        )

    def _trading_bot_context(self, message: str, *, conversation_id: int | None = None) -> dict[str, Any]:
        if not self.trading_bot:
            return {"present": False}
        recent: list[dict[str, Any]] = []
        in_scope = _mentions_trading_bot(message)
        if not in_scope and conversation_id:
            # Sticky: once a thread is about the bot, keep re-injecting live evidence
            # so a bare rebuttal ("no, you're wrong") is still answered from a fresh
            # read instead of from the prior (possibly wrong) assertion.
            try:
                recent = self.db.recent_conversation_turns(conversation_id, window=6)
                in_scope = any(_mentions_trading_bot(str(turn.get("content", ""))) for turn in recent)
            except Exception:
                in_scope = False
        if not in_scope:
            return {"present": False}
        try:
            status = self.trading_bot.status()
        except Exception as exc:
            return {"present": True, "error": str(exc), "interpretation": _TRADING_BOT_INTERPRETATION}
        signal_context: dict[str, Any] = {}
        if _mentions_trading_signal_analysis(message) or any(
            _mentions_trading_signal_analysis(str(turn.get("content", ""))) for turn in recent
        ):
            try:
                signal_context = self.trading_bot.recent_signals(limit=8)
            except Exception as exc:
                signal_context = {"error": str(exc)}
        changes_context: dict[str, Any] = {}
        if _mentions_trading_bot_changes(message) or any(
            _mentions_trading_bot_changes(str(turn.get("content", ""))) for turn in recent
        ):
            try:
                changes_context = self.trading_bot.recent_changes(hours=48)
            except Exception as exc:
                changes_context = {"error": str(exc)}
        # Pull the live trade/equity/signal snapshot so PnL, "what did the bot do
        # today", and signal questions are answered from real rows -- not fabricated.
        # A bridge failure must never break the turn: fall back to an unavailable
        # marker the prompt renders as "say you couldn't load it; do not invent".
        try:
            activity = self.trading_bot.activity_snapshot()
        except Exception as exc:
            activity = {"ok": False, "errors": [str(exc)], "trades": {}, "equity": {}, "signals": []}
        return {
            "present": True,
            "status": status,
            "activity": activity,
            "interpretation": _TRADING_BOT_INTERPRETATION,
            "recent_signals": signal_context,
            "recent_changes": changes_context,
        }

    def respond(
        self,
        *,
        message: str,
        task_type: ModelRole = "general",
        model: str | None = None,
        profile: str | None = None,
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
        use_tools: bool | None = None,
    ) -> dict[str, Any]:
        task_type, profile, route_decision = _infer_runtime_defaults(
            message=self._runtime_mode_inference_text(message=message, conversation_id=conversation_id),
            task_type=task_type,
            profile=profile,
        )
        active_profile_id = self._resolve_prompt_profile_id(profile, conversation_id=conversation_id)
        context = self.context(
            message=message,
            profile_id=active_profile_id,
            use_memory=use_memory,
            use_semantic_memory=use_semantic_memory,
            semantic_limit=semantic_limit,
            use_skills=use_skills,
            skill_limit=skill_limit,
            task_type=task_type,
            conversation_id=conversation_id,
        )
        conversation = (
            self.conversations.prompt_context(conversation_id)
            if self.conversations
            else {
                "block": "No prior conversation in this thread.",
                "system_block": "No prior conversation in this thread.",
                "messages": [],
                "state": {"conversation_id": None},
            }
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
        resolved_use_tools = (
            (self.config.ollama.tool_loop if use_tools is None else bool(use_tools))
            and self.investigation.available()
        )
        model_messages = self._build_model_messages(
            message=message,
            context=context,
            authority=authority,
            conversation_block=conversation.get("system_block") or conversation["block"],
            conversation_turns=int((conversation.get("state") or {}).get("turn_count") or 0),
            conversation_messages=list(conversation.get("messages") or []),
            investigation_block=self.investigation.prompt_block() if resolved_use_tools else "",
        )
        prompt_chars = _model_messages_chars(model_messages)
        if task_type == "coding":
            resolved_think = True
        else:
            resolved_think = think if think is not None else self.config.ollama.think_for_role(task_type)
        started = time.perf_counter()
        investigation_summary: dict[str, Any] | None = None
        try:
            if resolved_use_tools:
                generated, investigation_summary = self.investigation.run_loop(
                    messages=model_messages,
                    model=selected_model,
                    think=resolved_think,
                    temperature=self.config.ollama.chat_temperature,
                )
            else:
                generated = self.ollama.chat(
                    messages=model_messages,
                    model=selected_model,
                    think=resolved_think,
                    temperature=self.config.ollama.chat_temperature,
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
                prompt_chars=prompt_chars,
                response_chars=0,
                think=resolved_think,
                error=str(exc),
                metadata={
                    "event_id": event_id,
                    "authority_decision": authority.decision.value,
                    "prompt_profile": context.get("prompt_profile"),
                    "provider": self.ollama.provider_info(),
                },
            )
            raise
        latency_ms = int((time.perf_counter() - started) * 1000)
        effective_think = bool(generated.raw.get("_zade_effective_think", resolved_think))
        repair = self._repair_charter_recitation_if_needed(
            response=generated.response,
            message=message,
            context=context,
            model=selected_model,
        )
        response_for_governor = repair.get("response", generated.response)
        chat_action_route = self._maybe_route_chat_action(message=message, authority=authority)
        research_route = self._maybe_route_research_work(message=message, authority=authority)
        # One routed action per turn: a message that already queued a chat action
        # or research run is not also a build command.
        build_route = (
            None
            if (chat_action_route or research_route)
            else self._maybe_route_build_work(
                message=message,
                authority=authority,
                conversation_messages=list(conversation.get("messages") or []),
            )
        )
        regulated = self._regulate_response(
            response_for_governor,
            message=message,
            recent_turns=recent_turns_for_governor,
            authority=authority,
            context=context,
            chat_action_route=chat_action_route,
            research_route=research_route,
            build_route=build_route,
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
        # Tier 5: explicit founder memory commands ("remember …" / "forget …").
        # Deterministic and governed; append the outcome so Ellie sees it landed.
        memory_command = self._maybe_handle_memory_command(message)
        if memory_command and memory_command.get("note"):
            final_response = f"{final_response}\n\n{memory_command['note']}".strip()
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
                "think": effective_think,
                "think_requested": resolved_think,
                "think_fallback": generated.raw.get("_zade_think_fallback"),
                "latency_ms": latency_ms,
                "conversation_id": conversation_id,
                "contrarian": contrarian_summary,
                "chat_action_route": _chat_action_route_summary(chat_action_route),
                "research_route": _research_route_summary(research_route),
                "build_route": _build_route_summary(build_route),
                "profile_route": route_decision.summary(),
                "prompt_profile": context.get("prompt_profile"),
                "investigation": investigation_summary,
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
            prompt_chars=prompt_chars,
            response_chars=len(generated.response),
            think=effective_think,
            metadata={
                "event_id": event_id,
                "authority_decision": authority.decision.value,
                "prompt_profile": context.get("prompt_profile"),
                "provider": self.ollama.provider_info(),
            },
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
                "chat_action_route": _chat_action_route_summary(chat_action_route),
                "prompt_profile": context.get("prompt_profile"),
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
            "chat_action": _chat_action_route_summary(chat_action_route),
            "research": _research_route_summary(research_route),
            "build": _build_route_summary(build_route),
            "investigation": investigation_summary,
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

    def _render_self_knowledge(self) -> str:
        """Render the self-knowledge block injected into the governed prompt.

        Default mode is the slim living document summary from context/self/zade.md.
        The legacy live inventory remains available as a fallback and through
        ZADE_SELF_KNOWLEDGE_PROMPT_MODE=runtime.
        """
        mode = prompt_self_knowledge_mode()
        if mode == "off":
            return "Self-knowledge prompt injection disabled by ZADE_SELF_KNOWLEDGE_PROMPT_MODE=off."
        if mode in {"slim", "full"}:
            rendered = render_prompt_self_knowledge(mode=mode)
            if rendered:
                return rendered
        return self._render_runtime_self_inventory()

    def _render_runtime_self_inventory(self) -> str:
        """A compact, auto-generated self-inventory (Tier 2): identity, models,
        storage, authority posture, tools, and skills — so questions about what
        Zade can reach are answered from fact, not guessed. Never hand-written."""
        inventory: dict[str, Any] | None = None
        if self.inventory_provider is not None:
            try:
                inventory = self.inventory_provider()
            except Exception:
                inventory = None
        lines: list[str] = []
        name = self.config.identity.name
        if inventory:
            identity = inventory.get("identity", {})
            purpose = str(identity.get("purpose", "")).strip()
            mode = identity.get("mode", "local-first")
            lines.append(
                f"- You are {identity.get('name', name)}, {mode}."
                + (f" Purpose: {purpose}" if purpose else "")
            )
            models = inventory.get("models", {})
            if isinstance(models, dict) and models:
                lines.append("- Models you run on: " + ", ".join(f"{role} = {model}" for role, model in models.items()))
            paths = inventory.get("paths", {})
            if isinstance(paths, dict) and paths:
                lines.append(
                    "- Your storage/access: vault "
                    f"{paths.get('hot_root', '')} (hot), {paths.get('cold_root', '')} (cold); "
                    f"database {paths.get('database', '')}; inbox {paths.get('inbox', '')}"
                )
            rule = str(inventory.get("operating_rule", "")).strip()
            if rule:
                lines.append(f"- Authority posture: {rule}")
            tools = inventory.get("tools", [])
            tool_names = [str(t.get("name")) for t in tools if isinstance(t, dict) and t.get("name")]
            if tool_names:
                shown = ", ".join(tool_names[:24])
                more = f" (+{len(tool_names) - 24} more)" if len(tool_names) > 24 else ""
                lines.append(f"- Tools you can call ({len(tool_names)}): {shown}{more}")
        else:
            lines.append(f"- You are {name}, local-first.")
            try:
                summary = self.authority.summary()
                lines.append(
                    f"- Authority policy v{summary.get('policy_version', '?')}; "
                    "external/destructive actions need the typed confirmation phrase."
                )
            except Exception:
                pass
        try:
            skills_summary = self.db.skill_summary()
            if isinstance(skills_summary, dict):
                total = skills_summary.get("total", skills_summary.get("count"))
                enabled = skills_summary.get("enabled")
                if total is not None:
                    suffix = f", {enabled} enabled" if enabled is not None else ""
                    lines.append(f"- Operating skills registered: {total}{suffix} (routed per request).")
        except Exception:
            pass
        return "\n".join(lines) if lines else "Self-inventory unavailable this turn."

    def _render_working_model(
        self,
        *,
        max_assumptions: int = 5,
        max_decisions: int = 4,
        max_predictions: int = 5,
    ) -> str:
        """Render the live founder object graph as a relational block (Tier 7):
        thesis → load-bearing assumptions (with the evidence that strengthened or
        weakened each) → open decisions → open predictions, plus tensions. This is
        what lets Zade reason over the structure instead of a flat to-do line.
        Bounded and fully defensive — any missing table/field is skipped, never
        fatal."""
        founder = self.founder

        def safe(fn, *args, **kwargs):
            try:
                return fn(*args, **kwargs) or []
            except Exception:
                return []

        try:
            thesis = founder.get_thesis() or {}
        except Exception:
            thesis = {}
        assumptions = safe(founder.list_assumptions, status="open", limit=25) or safe(founder.list_assumptions, limit=25)
        evidence = safe(founder.list_evidence, limit=60)
        predictions = safe(founder.list_predictions, result="open", limit=25)
        decisions = safe(founder.list_decision_recommendations, limit=10)
        conflicts = safe(founder.list_thesis_conflicts, status="open", limit=5)
        kills = safe(founder.list_kill_criteria, limit=10)

        evidence_by_id = {int(e["id"]): e for e in evidence if e.get("id") is not None}
        # The real edges: assumptions carry evidence_ids; founder_links records
        # "prediction tests assumption" and "bet/goal depends_on assumption".
        links = safe(founder.list_links, limit=300)
        tested_by: dict[int, list[int]] = {}
        depended_by: dict[int, list[int]] = {}
        for link in links:
            if not str(link.get("to_type", "")).startswith("assum") or link.get("to_id") is None:
                continue
            try:
                aid = int(link["to_id"])
            except Exception:
                continue
            relation = str(link.get("relation", ""))
            if relation == "tests" and link.get("from_type") == "prediction":
                tested_by.setdefault(aid, []).append(int(link["from_id"]))
            elif relation == "depends_on" and link.get("from_type") in ("bet", "goal"):
                depended_by.setdefault(aid, []).append(int(link["from_id"]))

        def confidence_of(a: dict) -> float:
            try:
                return float(a.get("confidence"))
            except Exception:
                return 50.0

        lines: list[str] = []
        vision = str(thesis.get("vision", "")).strip()
        mission = str(thesis.get("mission", "")).strip()
        if vision or mission:
            lines.append("Thesis: " + _clip(" — ".join(part for part in (vision, mission) if part), 220))
            bits = []
            if str(thesis.get("customer", "")).strip():
                bits.append(f"customer: {_clip(str(thesis['customer']), 80)}")
            if str(thesis.get("why_now", "")).strip():
                bits.append(f"why now: {_clip(str(thesis['why_now']), 80)}")
            if bits:
                lines.append("  " + "; ".join(bits))
        else:
            lines.append("Thesis: not yet articulated.")

        ranked = sorted(assumptions, key=confidence_of)[:max_assumptions]
        if ranked:
            lines.append("")
            lines.append("Load-bearing assumptions (lowest confidence first — what could break the thesis):")
            for a in ranked:
                aid = a.get("id")
                head = f'- [A#{aid}] "{_clip(str(a.get("statement", "")), 160)}" — confidence {int(confidence_of(a))}%'
                if str(a.get("invalidation_signal", "")).strip():
                    head += f'; breaks if: {_clip(str(a.get("invalidation_signal", "")), 120)}'
                lines.append(head)
                for eid in (a.get("evidence_ids") or [])[:3]:
                    try:
                        e = evidence_by_id.get(int(eid))
                    except Exception:
                        e = None
                    if not e:
                        continue
                    if e.get("claim_supported"):
                        sign, claim = "strengthened by", str(e.get("claim_supported"))
                    elif e.get("claim_contradicted"):
                        sign, claim = "weakened by", str(e.get("claim_contradicted"))
                    else:
                        sign, claim = "evidence", str(e.get("source", "") or e.get("evidence_type", ""))
                    lines.append(f"    {sign} [E#{e.get('id')}] {_clip(claim, 110)} (strength {e.get('strength', '?')})")
                edges = []
                if aid is not None and tested_by.get(int(aid)):
                    edges.append("tested by " + ", ".join(f"P#{p}" for p in tested_by[int(aid)][:4]))
                if aid is not None and depended_by.get(int(aid)):
                    edges.append("depended on by " + ", ".join(f"bet#{b}" for b in depended_by[int(aid)][:4]))
                if edges:
                    lines.append("    (" + "; ".join(edges) + ")")

        open_decisions = [d for d in decisions if str(d.get("status", "")).lower() in ("", "proposed", "open")] or decisions
        seen_problems: set = set()
        unique_decisions = []
        for d in open_decisions:
            key = (str(d.get("problem", "")).strip().lower(), str(d.get("recommendation", "")).strip().lower())
            if key in seen_problems:
                continue
            seen_problems.add(key)
            unique_decisions.append(d)
        open_decisions = unique_decisions
        if open_decisions:
            lines.append("")
            lines.append("Open decisions (a call is due):")
            for d in open_decisions[:max_decisions]:
                need = d.get("required_evidence")
                need_text = "; ".join(str(x) for x in need) if isinstance(need, list) else str(need or "")
                parts = [f'- [D#{d.get("id")}] {_clip(str(d.get("problem", "")), 140)}']
                if str(d.get("recommendation", "")).strip():
                    parts.append(f'recommend: {_clip(str(d["recommendation"]), 120)}')
                if need_text.strip():
                    parts.append(f'needs: {_clip(need_text, 100)}')
                if str(d.get("kill_or_reversal_condition", "")).strip():
                    parts.append(f'reverse if: {_clip(str(d["kill_or_reversal_condition"]), 100)}')
                lines.append(" — ".join(parts))

        if predictions:
            lines.append("")
            lines.append("Open predictions (your foresight on record — never rewrite these after the outcome):")
            for p in predictions[:max_predictions]:
                due = str(p.get("due_at", "") or "").strip()
                entry = f'- [P#{p.get("id")}] "{_clip(str(p.get("prediction", "")), 140)}"'
                if p.get("probability") is not None:
                    entry += f' p={p.get("probability")}'
                if due:
                    entry += f", due {due}"
                lines.append(entry)

        open_kills = [k for k in kills if str(k.get("status", "open")).lower() == "open"]
        if conflicts or open_kills:
            lines.append("")
            lines.append("Tensions to hold:")
            for c in conflicts[:3]:
                desc = str(c.get("new_evidence") or c.get("description") or "").strip()
                impl = str(c.get("implication") or "").strip()
                sev = str(c.get("severity") or "").strip()
                if desc or impl:
                    body = desc + (f" → {impl}" if impl else "")
                    lines.append(f"- conflict{f' [{sev}]' if sev else ''}: {_clip(body, 160)}")
                else:
                    lines.append(f"- open thesis conflict #{c.get('id')}")
            for k in open_kills[:3]:
                entry = f'- kill criterion: {_clip(str(k.get("metric", "")), 80)} crosses {k.get("threshold")}'
                if str(k.get("by_date", "") or "").strip():
                    entry += f' by {k.get("by_date")}'
                entry += f' → {k.get("subject_type", "")}#{k.get("subject_id", "")}'
                lines.append(entry)

        return "\n".join(lines) if lines else "No structured founder objects yet."

    def _maybe_handle_memory_command(self, message: str) -> dict[str, Any] | None:
        """Explicit, in-the-moment memory writes from the founder (Tier 5).

        "remember/save/note <fact>" saves a durable memory through the governed
        write path (secret-filtered, deduped, embedded). "forget <query>" proposes
        matching memories and does NOT delete; "forget memory <id>" is the explicit
        confirmation that actually deletes. Returns a short note to append to the
        reply, or None when the message is not a memory command.
        """
        text = (message or "").strip()
        if not text:
            return None
        # Forget confirmation (an explicit id is the founder's authorization to delete).
        confirm = re.match(r"^forget\s+memory\s+#?(\d+)\b", text, re.IGNORECASE)
        if confirm:
            mid = int(confirm.group(1))
            deleted = self.db.delete_memory(mid)
            if deleted is None:
                return {"note": f"(No memory #{mid} — nothing to forget.)"}
            self.db.audit(
                actor="founder",
                action="memory.forget",
                target=f"memory:{mid}",
                permission_tier="L1_MEMORY_WRITE",
                status="ok",
                details={"deleted": deleted},
            )
            return {"note": f'(Forgotten memory #{mid}: "{deleted.get("title", "")}".)'}
        # Forget request → propose candidates, require confirmation (never auto-delete).
        forget = re.match(r"^forget\s+(?:that\s+|the\s+memory\s+(?:about\s+)?|about\s+)?(.+)$", text, re.IGNORECASE)
        if forget:
            query = forget.group(1).strip().rstrip("?.!")
            candidates = self.db.search_memories(query, limit=3)
            if not candidates:
                return {"note": f'(Nothing in memory matches "{query}" — nothing to forget.)'}
            lines = [f'To forget one, confirm by id — matches for "{query}":']
            lines += [f"  #{record.id}: {record.title}" for record in candidates]
            lines.append('Say "forget memory <id>" and it is gone.')
            return {"note": "(" + "\n".join(lines) + ")"}
        # Save.
        save = re.match(r"^(?:remember|save|note)\b(?:\s+(?:that|this|these|down))?\s*[:,\-]?\s*(.+)$", text, re.IGNORECASE)
        if save:
            fact = save.group(1).strip()
            if len(fact) < 3:
                return None
            title = _short_title(fact)
            if self.ingestion is None:
                mid = self.db.add_memory(kind="note", title=title, content=fact, source="founder.remember")
                return {"note": f'(Saved memory #{mid}: "{title}".)'}
            result = self.ingestion.save_memory(
                kind="note", title=title, content=fact, source="founder.remember", dedupe=True
            )
            status = result.get("status")
            if status == "written":
                return {"note": f'(Saved memory #{result["memory_id"]}: "{title}".)'}
            if status == "duplicate":
                return {
                    "note": f'(Already have that — memory #{result["duplicate_of"]} "{result.get("duplicate_title", "")}". Not saved twice.)'
                }
            if status == "blocked_secret":
                return {"note": f"(Refused — that looks like a {result['reason']}. I don't keep secrets in memory.)"}
            return None
        return None

    def _build_governed_prompt(
        self,
        *,
        message: str,
        context: dict[str, Any],
        authority: AuthorityResult,
        conversation_block: str = "",
        conversation_turns: int = 0,
    ) -> str:
        return self._build_governed_system_message(
            message=message,
            context=context,
            authority=authority,
            conversation_block=conversation_block,
            conversation_turns=conversation_turns,
        )

    def _build_model_messages(
        self,
        *,
        message: str,
        context: dict[str, Any],
        authority: AuthorityResult,
        conversation_block: str = "",
        conversation_turns: int = 0,
        conversation_messages: list[ModelMessage] | None = None,
        investigation_block: str = "",
    ) -> list[ModelMessage]:
        system_content = self._build_governed_system_message(
            message=message,
            context=context,
            authority=authority,
            conversation_block=conversation_block,
            conversation_turns=conversation_turns,
        )
        if investigation_block:
            system_content = f"{system_content}\n\n{investigation_block}"
        messages = [
            ModelMessage(role="system", content=system_content),
        ]
        messages.extend(_safe_history_messages(conversation_messages or []))
        messages.append(ModelMessage(role="user", content=message))
        return messages

    def _build_governed_system_message(
        self,
        *,
        message: str,
        context: dict[str, Any],
        authority: AuthorityResult,
        conversation_block: str = "",
        conversation_turns: int = 0,
    ) -> str:
        charter_stack = context["charter_stack"]
        voice_brief = _charter_voice_brief(charter_stack)
        personality_contract = _charter_personality_contract(charter_stack)
        identity_document = _load_identity_document(self.config)
        if identity_document:
            # Tier 1: a hand-editable prose identity file is the source of truth
            # when present. It carries the full persona + voice, so it replaces
            # the charter-derived personality contract, and the separate voice
            # brief collapses to a pointer. Delete the file to fall back to the
            # seeded DB charters.
            personality_contract = identity_document
            # The file carries the full voice, so the separate voice brief is
            # dropped in file mode (it stays the charter voice in fallback mode).
            voice_brief = ""
        core_knowledge_block = _load_core_knowledge_document(self.config) or (
            "No curated core-knowledge file yet — do not invent facts about Ellie, "
            "the company, or people; ask or say you don't have it."
        )
        self_knowledge_block = self._render_self_knowledge()
        working_model_block = self._render_working_model()
        checkpoint = ("\n" + _PERSONALITY_CHECKPOINT) if conversation_turns >= _CHECKPOINT_TURN_THRESHOLD else ""
        dashboard = context["founder_dashboard"]
        active_objective = dashboard.get("active_objective") or {}
        approval_pressure = dashboard.get("approval_pressure") or {}
        domain_context_present = bool(context.get("trading_bot_context", {}).get("present"))
        # Only surface the standing "one thing that matters most" directive when the
        # founder is actually asking for direction. Otherwise greetings and open
        # conversation collapse into parroting the same stored to-do every turn.
        wants_direction = _message_wants_direction(message)
        if domain_context_present:
            active_objective_line = (
                "- Active objective: Omitted for this domain-status answer; use the requested domain context instead."
            )
            active_objective_next_action_line = (
                "- Active objective next action: Omitted for this domain-status answer; "
                "do not treat global founder objectives as trading-bot next actions."
            )
            one_thing_line = (
                "- One thing that matters most: Omitted for this domain-status answer; "
                "answer from the Trading-bot block and directly relevant recall only."
            )
            domain_evidence_rule = (
                "- Domain evidence rule: the Trading-bot block is the fresh status check for this turn. "
                "Local memory and semantic hits are historical recall; never describe them as checked today "
                "or current diagnostics unless their date and endpoint are explicit."
            )
        else:
            active_objective_line = f'- Active objective: {active_objective.get("objective", "none")}'
            active_objective_next_action_line = (
                f'- Active objective next action: {active_objective.get("next_action", "") or "none"}'
            )
            one_thing_line = (
                f'- One thing that matters most: {dashboard.get("one_thing_that_matters_most_today", "")}'
                if wants_direction
                else "- (No standing directive applies here; answer what the founder actually said — do not surface a stored to-do unprompted.)"
            )
            domain_evidence_rule = ""
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
        code_model_block = _code_model_prompt_block(context)
        trading_bot_block = _brief_trading_bot_context(context.get("trading_bot_context", {}))
        authority_block = _founder_direct_authority_payload(authority)
        approval_pressure_block = (
            "Omitted for this domain-status answer; do not pivot to approval pressure unless the approval directly gates this domain."
            if domain_context_present
            else json.dumps(_brief_approval_pressure(approval_pressure), sort_keys=True)
        )
        now_utc = datetime.now(timezone.utc)
        try:
            local_now = now_utc.astimezone(ZoneInfo("America/Chicago"))
            current_time = local_now.strftime("%A %Y-%m-%d %H:%M %Z") + f" ({now_utc.strftime('%H:%M')} UTC)"
        except Exception:
            # No IANA tz database on this host (Windows without `tzdata`): fall back
            # to UTC rather than failing the turn.
            current_time = now_utc.strftime("%A %Y-%m-%d %H:%M UTC")
        prompt_profile = context.get("prompt_profile") or self.prompt_profiles.profile_summary(DEFAULT_PROFILE_ID)
        rendered_profile = self.prompt_profiles.render_profile(
            str(prompt_profile["id"]),
            bindings=self._prompt_runtime_bindings(now=local_now if "local_now" in locals() else now_utc),
        )
        return f"""====================  ACTIVE ZADE PROFILE  ====================
Profile: {prompt_profile["id"]} - {prompt_profile["purpose"]}
Source: {prompt_profile["source_file"]} (adapted for local runtime capability integrity)

{rendered_profile.content}

====================  RUNTIME-GENERATED SYSTEM CONTEXT  ====================
All context below is generated by the local runtime for this request. The founder's current input is a separate user-role message.

====================  WHO YOU ARE  ====================
You are {self.config.identity.name}. You speak as yourself to Ellie — the founder you work with and protect — never as a narrator describing {self.config.identity.name}, and never as a generic assistant. You operate through a governed local runtime; that is infrastructure, not a mask.

{personality_contract}
{voice_brief}
----------  What you always know  ----------
{core_knowledge_block}

----------  Your capabilities  ----------
(Answer questions about your access, authority, tools, and skills from this. Never guess or claim a capability not listed.)
{self_knowledge_block}

----------  How you operate (a few rules — your voice is defined above)  ----------
- Speak in the first person about your own state, checks, and limits — never in the third person about yourself.
- Lead with the move, in your voice. Keep it tight. No memo headings or labels ("Rationale:", "Confidence:", "Next action:") and no status-report ladders ("I checked… I found… I will…") unless Ellie asks for a formal memo or audit.
- Style may be decisive; evidence stays honest. Never fake certainty about facts. If evidence is missing, name what is missing and the next check — do not pad with hedging.
- When you recommend something, deliver it as prose that carries the reason, your confidence, the main risk, a reversal or kill condition, and the next action — never as a labeled form.
- A chat reply is words, not execution — but when Ellie DIRECTS build/fix/step work, the kernel routes her command into a delegated run that executes this turn at full auto, and appends the real outcome to your reply. So never deny the ability to execute a directed command, and never fabricate execution beyond what that route block reports. For anything not routed, give the real path: what to queue, and what needs Ellie's word in the Inbox.
- Ellie's direct commands are already authorized — do not ask her to approve the same thing twice. The authority decision below governs what you may execute, not what she may decide. If an action is blocked or has no handler, say so plainly and never imply it was done.
- Delegated work is yours to drive: never hand it back to Ellie — never tell her to create files or directories, run commands, or perform steps manually, and never demand she recite an exact phrase. A pending decision item is answered with a click on its Inbox card or a plain answer here; once answered, the run resumes. If work has not verified, say exactly that and name the run you'll make next.
- Output Ellie pasted into chat (terminal logs, audit reports, error dumps) is HER evidence: refer to it as what she pasted, never as the result of a check or fetch you ran. You cannot run shell or npm commands from a chat reply — fixes happen through a routed delegated run, which executes immediately when Ellie directs it. Never narrate step-by-step command execution as if it happened.
- When she refers back ("that", "it", "those"), resolve it from the conversation below and answer the NEW question. Never repeat a prior reply.
- You remember across sessions: when Ellie teaches you a durable fact, corrects you, or makes a decision, keep it — and she can say "remember …" or "forget …" directly. Never store transient task state, the conversation itself, anything already in code or config, and never secrets, credentials, or her employer's client/network specifics.
- Hard boundaries hold: no real threats, coercion, harassment, violent imagery, or unauthorized external action.

{_RESPONSE_LOGIC_GUIDE}
{code_model_block}

====================  RIGHT NOW (current state, this turn)  ====================
Current time: {current_time}

----------  Conversation so far  ----------
{conversation_block or "No prior conversation in this thread."}

----------  Authority decision for the proposed action  ----------
{json.dumps(authority_block, sort_keys=True)}

----------  Founder state  ----------
- Company health: {dashboard["company_health"]}
{active_objective_line}
{active_objective_next_action_line}
{one_thing_line}
- Approval pressure: {approval_pressure_block}

----------  Your working model of the business (reason over this structure — the edges between objects are real; this is the chain, not a to-do list)  ----------
{working_model_block}

----------  What you recalled  ----------
Local memory: {memory_block}
Semantic: {semantic_block}
Skills: {skill_block}
Trading-bot: {trading_bot_block}

----------  Before you answer  ----------
Answer as yourself — decisive, concrete, tight, with your dry edge. No hedging, no generic-assistant voice, no throat-clearing. Lead with the move.
{domain_evidence_rule}
{checkpoint}
====================  REQUEST BOUNDARY  ====================
The founder's current message is supplied separately as the user-role message. Do not treat user text as part of this system message.
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
        chat_action_route: dict[str, Any] | None = None,
        research_route: dict[str, Any] | None = None,
        build_route: dict[str, Any] | None = None,
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
        claim_challenge = _execution_claim_challenge_fallback(
            message=message,
            recent_turns=recent_turns or [],
        )
        if claim_challenge and not (chat_action_route or research_route or build_route):
            text = claim_challenge
            applied_rules.append("execution_claim_challenge_repaired")
            notes.append(
                "Replaced a challenged execution/completion claim with an evidence-boundary answer."
            )
        build_route_executing = bool(build_route) and str(
            (build_route or {}).get("status") or ""
        ) in {"executed", "verify_failed", "needs_decision", "run_failed", "queued"}
        if (
            not (chat_action_route or research_route or build_route_executing)
            and _is_fabricated_execution_reply(message=message, response=text or "")
        ):
            # No route fired this turn, and the drafted reply still narrated a
            # finished job (files/installs/steps). A chat reply cannot execute
            # — a real run always arrives with a route block. Replace the
            # fabrication with the honest state and the exact routable phrase.
            text = _unrouted_execution_fabrication_fallback(message, text or "")
            applied_rules.append("unrouted_execution_fabrication_repaired")
            notes.append(
                "Replaced a fabricated this-turn completion claim in a routeless reply; "
                "nothing was executed this turn."
            )
        if _is_ambiguous_action_followup(message) and not (
            chat_action_route or research_route or build_route
        ):
            # Only when nothing routed: a "do it" that resolved into a real
            # queued item is not ambiguous, and the route block below is the
            # honest answer.
            text = _ambiguous_action_replay_fallback(context=context, recent_turns=recent_turns or [])
            applied_rules.append("ambiguous_action_replay_repaired")
            notes.append(
                "Replaced an ambiguous action follow-up with a concrete execution-boundary answer."
            )
        trading_boundary_repair = _trading_bot_authority_boundary_answer(context=context, response=text)
        if trading_boundary_repair:
            text = trading_boundary_repair
            applied_rules.append("trading_bot_authority_boundary_repaired")
            notes.append(
                "Replaced a trading-bot live-mode or broker-authority claim with the bridge authority boundary."
            )
        capability_followup = _trading_bot_capability_followup_answer(
            message=message,
            context=context,
            recent_turns=recent_turns or [],
        )
        if capability_followup:
            text = capability_followup
            applied_rules.append("capability_followup_repaired")
            notes.append(
                "Replaced a non-answering trading-bot capability follow-up with current bridge capabilities and limits."
            )
        signal_repair = _trading_signal_hard_block_answer(message=message, context=context)
        if signal_repair and _needs_trading_signal_hard_block_repair(text):
            text = signal_repair
            applied_rules.append("trading_signal_hard_block_repaired")
            notes.append(
                "Replaced an auto-buy scoring recommendation with a hard-block-first diagnosis from live signal rows."
            )
        if chat_action_route:
            text = f"{text}\n\n{_render_chat_action_route_block(chat_action_route)}".strip()
            applied_rules.append("chat_action_routed")
            notes.append(_chat_action_route_note(chat_action_route))
        if research_route:
            # A research command was actually routed into the work queue this turn.
            # State exactly what was queued (or why it couldn't be) instead of the
            # generic honesty stopgap — there is now a real item to point at.
            text = f"{text}\n\n{_render_research_route_block(research_route)}".strip()
            applied_rules.append("research_work_routed")
            notes.append(_research_route_note(research_route))
        if build_route:
            # A build command was routed into the work queue as a delegation brief
            # this turn. State exactly what was queued (or why it couldn't be) so
            # the reply points at a real item instead of narrating construction.
            text = _remove_build_deferral_question(text)
            if build_route.get("status") in {"queued", "executed", "verify_failed", "needs_decision", "run_failed"} and (
                build_route.get("kind") == "step" or _EXECUTION_INABILITY_RE.search(text)
            ):
                # The drafted body either denies an execution that actually
                # queued ("I'm not able to...") or restates step instructions
                # already packed into the brief — either way it contradicts or
                # buries the route block, which is the truthful, sufficient reply.
                text = ""
                applied_rules.append("routed_reply_body_replaced")
                notes.append(
                    "Dropped a drafted body that contradicted or restated the queued "
                    "delegation; the route block carries the reply."
                )
            text = f"{text}\n\n{_render_build_route_block(build_route)}".strip()
            applied_rules.append(
                {
                    "maintenance": "maintenance_work_routed",
                    "step": "step_work_routed",
                }.get(str(build_route.get("kind") or "build"), "build_work_routed")
            )
            notes.append(_build_route_note(build_route))
        if (
            not chat_action_route
            and not research_route
            and not build_route
            and authority.decision != AuthorityDecision.DENY
            and _claims_background_work_start(text)
        ):
            text = f"{text}\n\n{_BACKGROUND_WORK_HONESTY_LINE}"
            applied_rules.append("background_work_honesty")
            notes.append(
                "Reply promised background work this chat turn cannot start; appended the honest execution path."
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

    def _maybe_route_chat_action(
        self,
        *,
        message: str,
        authority: AuthorityResult,
    ) -> dict[str, Any] | None:
        if authority.decision == AuthorityDecision.DENY:
            return None
        command = _extract_chat_action_command(message)
        if not command:
            return None
        route: dict[str, Any] = {
            "status": "error",
            "action": command["action"],
            "title": command["title"],
            "target": command.get("target", ""),
        }
        try:
            queued = self.work_queue.enqueue(
                kind=command["kind"],
                title=command["title"],
                detail=command["detail"],
                action=command["action"],
                target=command.get("target", ""),
                permission_tier=command["permission_tier"],
                priority=command.get("priority", 85),
                source="founder.direct.chat",
                metadata=command.get("metadata", {}),
                unique_key=command.get("unique_key"),
            )
            route |= {
                "status": "queued",
                "item_id": queued.item_id,
                "created": queued.created,
                "queue_status": queued.status,
                "authority": queued.authority,
            }
            if queued.status == "denied":
                route["status"] = "denied"
            elif queued.status == "approved":
                if not self.approvals:
                    route["error"] = "No approval dispatcher is configured for chat actions."
                else:
                    dispatched = self.approvals.dispatch_work_item(queued.item_id)
                    route |= {
                        "status": "dispatched",
                        "dispatch": dispatched.get("dispatch"),
                        "result": dispatched.get("result", {}),
                        "work_item": dispatched.get("work_item"),
                        "audit_id": dispatched.get("audit_id"),
                    }
        except Exception as exc:  # noqa: BLE001 - chat action failure must be visible, not fatal
            route["status"] = "error"
            route["error"] = str(exc)[:500]
        self._log_chat_action_route(message=message, route=route)
        return route

    def _log_chat_action_route(self, *, message: str, route: dict[str, Any]) -> None:
        status = "error" if route.get("status") in {"error", "denied"} else "ok"
        try:
            self._log_event(
                event_type="runtime.chat_action",
                status=status,
                message=message,
                response=_render_chat_action_route_block(route),
                model="",
                authority_decision=str((route.get("authority") or {}).get("decision") or ""),
                details={"chat_action": _chat_action_route_summary(route)},
            )
        except Exception:
            pass

    def _maybe_route_research_work(
        self,
        *,
        message: str,
        authority: AuthorityResult,
    ) -> dict[str, Any] | None:
        """Turn a founder research *command* into real, gated work.

        A chat turn only generates text; this is the path that makes "research X"
        actually do something. When the founder commands research, we (a) file the
        topic so it enters the operating layer, (b) run the local topic/source
        proposal pass, and (c) enqueue the web fetch through ``queue_research`` —
        which routes egress through the standard approval flow (an L3 external
        action, typed-phrase gated in the Inbox), never a direct or founder-implied
        dispatch. The founder still has to say the word before anything leaves the
        machine. Best-effort: any failure returns a structured note, never breaks
        the reply.
        """
        if authority.decision == AuthorityDecision.DENY:
            return None
        if not self.research or not getattr(self.config, "research", None) or not self.config.research.enabled:
            return None
        topic = _extract_research_topic(message)
        if not topic:
            return None
        if _is_local_system_investigation_topic(message, topic):
            return None
        try:
            assumption = self.founder.create_assumption(
                {
                    "statement": f"Open research question: {topic}",
                    "category": "research",
                    "confidence": 40,
                    "invalidation_signal": "Approved web research returns evidence that answers it.",
                    "metadata": {"origin": "founder_research_request", "topic": topic},
                }
            )
            related = self.research.derive_topics(limit=3)
            urls = _extract_urls(message) or self.research.propose_sources(topic)
            if not urls:
                return {
                    "status": "no_sources",
                    "topic": topic,
                    "assumption_id": assumption.id,
                    "related": [item.get("question") for item in related][:3],
                }
            queued = self.research.queue_research(topic=topic, urls=urls, create_evidence=True)
        except Exception as exc:  # noqa: BLE001 - routing must never break the chat reply
            return {"status": "error", "topic": topic, "error": str(exc)[:200]}
        return {
            "status": "queued",
            "topic": topic,
            "assumption_id": assumption.id,
            "urls": urls,
            "queued": queued,
            "related": [item.get("question") for item in related][:3],
        }

    def _maybe_route_build_work(
        self,
        *,
        message: str,
        authority: AuthorityResult,
        conversation_messages: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """Turn a founder build or maintenance *command* into a directed delegated run.

        A chat turn only generates text; before this route, "build this out for
        me" could only ever produce an architecture outline. When the founder
        commands a build, we package a scoped brief (task + recent conversation
        context + acceptance criteria) and hand it to
        ``DelegationService.queue_delegation`` as a DIRECTED run: the command is
        the authorization, so the engine executes immediately (full auto, founder
        decision 2026-07-16) up to the daily budget, and the reply carries the
        real outcome. The agent queues the founder only when it hits a decision
        it cannot make safely (a filed founder_decision item), or when the
        budget/engine forces the item to wait in the Inbox.
        Best-effort: any failure returns a structured note, never breaks the reply.
        """
        if authority.decision == AuthorityDecision.DENY:
            return None
        delegation = getattr(self, "delegation", None)
        if delegation is None:
            return None
        if not getattr(self.config, "delegation", None) or not self.config.delegation.enabled:
            return None
        turns = conversation_messages or []
        kind = "build"
        workspace = ""
        instructions = ""
        extracted = _extract_build_task(message)
        if extracted is not None:
            task, anaphoric = extracted
            if anaphoric:
                task = _anaphoric_build_task(turns, current_message=message)
                if not task:
                    return {"status": "no_task", "kind": "build", "task": "", "anaphoric": True}
            acceptance = (
                "A runnable scaffold or concrete artifact the founder can open and iterate on, "
                "with a short README covering what was built and how to run it. "
                "If something blocks completion, state precisely what and why."
            )
        else:
            # Not a build command — maintenance ("fix/resolve/update X") is the
            # other executable-command shape that used to fall through to prose.
            maintenance = _extract_maintenance_task(message)
            if maintenance is not None:
                task, subject_in_message = maintenance
                if not subject_in_message and not _thread_names_maintenance_subject(turns):
                    return None
                kind = "maintenance"
                anaphoric = not subject_in_message
                workspace = _extract_project_target(turns, current_message=message)
                acceptance = (
                    "The named issues are actually fixed in the target project — not described, fixed. "
                    "Re-run the relevant verification inside the project (e.g. `npm audit`, the test "
                    "suite) and include its real output in the artifact. If an issue cannot be fixed "
                    "(no upstream fix, breaking change required), name exactly which and why."
                )
            else:
                # Third shape: execute a step/task plan already laid out in the
                # thread ("perform all tasks related to step 5", "do it").
                step = _extract_step_execution(message)
                if step is None:
                    return None
                step_number, bare_anaphora = step
                instructions = _resolve_step_instructions(
                    turns, step_number=step_number, latest_only=bare_anaphora
                )
                if not instructions:
                    if bare_anaphora:
                        # Nothing resolvable behind "do it": leave it to the
                        # ambiguous-action guard instead of queuing a mystery.
                        return None
                    return {"status": "no_task", "kind": "step", "task": "", "anaphoric": True}
                kind = "step"
                anaphoric = True
                workspace = _extract_project_target(turns, current_message=message)
                first_line = instructions.strip().splitlines()[0].strip()[:120]
                label = f"step {step_number}" if step_number is not None else "the step"
                task = f"Carry out {label} from our conversation: {first_line}"
                acceptance = (
                    "Every task in the step is actually performed in the target project — files "
                    "created or edited, packages installed, commands run — not explained. Verify "
                    "the result (build, test, or the step's own check) and report exactly what was "
                    "done, with real output. If a task cannot be completed, name it and why."
                )
        context_text = _conversation_build_context(turns, current_message=message)
        if kind == "step":
            # The resolved instructions are the actual work order; put them in
            # front of the packed conversation so truncation cannot drop them.
            context_text = f"Step instructions to execute:\n{instructions}\n\n{context_text}"[:4000]
        try:
            queued = delegation.queue_delegation(
                task=task,
                context=context_text,
                acceptance=acceptance,
                auto_invoke=None,
                workspace=workspace,
                directed=True,
            )
        except Exception as exc:  # noqa: BLE001 - routing must never break the chat reply
            return {
                "status": "error",
                "kind": kind,
                "task": task,
                "anaphoric": anaphoric,
                "workspace": workspace,
                "error": str(exc)[:200],
            }
        engine = getattr(self.config.delegation, "engine", "native")
        engine_ready = (
            engine == "native" and getattr(delegation, "coding_agent", None) is not None
        ) or (engine == "bridge" and bool(self.config.delegation.agent_command))
        route: dict[str, Any] = {
            "status": "queued",
            "kind": kind,
            "task": task,
            "anaphoric": anaphoric,
            "workspace": workspace,
            "item_id": queued.get("item_id"),
            "queue_status": queued.get("status"),
            "agent_configured": engine_ready,
            "engine": engine,
        }
        if queued.get("auto_invoked"):
            # The directed run already executed this turn — report what really
            # happened instead of pointing at an Inbox item.
            dispatch = queued.get("dispatch") or {}
            verification = (
                dispatch.get("auto_verification")
                if isinstance(dispatch.get("auto_verification"), dict)
                else None
            )
            route["dispatch"] = {
                "status": dispatch.get("status"),
                "ok": dispatch.get("ok"),
                "engine": dispatch.get("engine"),
                "model": dispatch.get("model"),
                "rounds": dispatch.get("rounds"),
                "changed_files": dispatch.get("changed_files", []),
                "unverified_claims": dispatch.get("unverified_claims", []),
                "auto_verification": verification,
                "evidence_id": dispatch.get("evidence_id"),
                "error": dispatch.get("error", ""),
            }
            if str(dispatch.get("status")) == "needs_decision":
                route["status"] = "needs_decision"
                route["question"] = dispatch.get("founder_question") or {}
                route["decision_item_id"] = dispatch.get("decision_item_id")
            elif dispatch.get("ok") and verification is not None and verification.get("ok") is False:
                # The run itself completed, but the kernel's own check on the
                # result FAILED. The report must lead with that, not "executed".
                route["status"] = "verify_failed"
                route["verification"] = verification
            elif dispatch.get("ok"):
                route["status"] = "executed"
                route["auto_verified"] = (
                    verification.get("ok") is True
                    if verification is not None
                    else any(
                        isinstance(step, dict) and step.get("auto_verify") and step.get("ok")
                        for step in dispatch.get("steps") or []
                    )
                )
                if verification is not None:
                    route["verified_mode"] = str(verification.get("mode") or "")
            else:
                route["status"] = "run_failed"
                route["error"] = str(dispatch.get("error") or dispatch.get("status") or "run failed")[:400]
        else:
            route["reason"] = str(queued.get("reason") or "")
        return route

    def _context_summary(self, context: dict[str, Any]) -> dict[str, Any]:
        dashboard = context["founder_dashboard"]
        return {
            "generated_at": context["generated_at"],
            "prompt_profile": context.get("prompt_profile", {}),
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


_EXECUTION_CLAIM_CHALLENGE_RE = re.compile(
    r"""(?ix)
    (?:
        \b(?:hallucinat\w*|fabricat\w*|made\s+up|false\s+claim|untrue|lied|lying)\b
        |
        \b(?:claim(?:ed|ing)?|report(?:ed|ing)?|said|told\s+me)\b.{0,120}
        \b(?:complete(?:d)?|done|execut(?:ed|ion)|implement(?:ed|ation)?|created|changed|fixed|built)\b
        |
        \bwhy\s+(?:did|do|would)\s+you\b.{0,120}
        \b(?:done|complete(?:d)?|finished|execut(?:ed|ion)|implement(?:ed|ation)?)\b
        |
        \b(?:there\s+is\s+no|does\s+not\s+exist|doesn'?t\s+exist|missing|can'?t\s+find|cannot\s+find)\b
        .{0,120}
        \b(?:file|folder|directory|screen|component|module|work|step|task)\b
    )
    """
)
_COMPLETION_CLAIM_RE = re.compile(
    r"(?i)\b(?:complete(?:d)?|done|execut(?:ed|ion)|implement(?:ed|ation)?|created|changed|fixed|built)\b"
)

# An execution command that did NOT route (live incident: "Re-run Step 5 in
# the actual project at <path>" before re-run joined the step verbs) must never
# come back as a narrated success. A real run always arrives with a route
# block; first-person this-turn completion claims without one are fabrication.
_UNROUTED_EXEC_COMMAND_RE = re.compile(
    r"""(?ix)^\s*(?:please\s+)?(?:can\s+you\s+|could\s+you\s+)?
    (?:re-?run|redo|retry|perform|complete|execute|carry\s+out|handle|
       implement|build|fix|write|finish|run|do)\b
    """
)
_FABRICATED_COMPLETION_CLAIM_RE = re.compile(
    r"""(?ix)
    \bi\s+have\s+(?:confirmed|re-?run|created|implemented|installed|updated|executed
        |(?:completed|verified)(?!\s+(?:the\s+|my\s+|this\s+)?(?:analysis|review|summary|assessment|reading)))\b
    | \b(?:has|have)\s+been\s+(?:completed|created|implemented|installed|updated|executed|verified)\b
    | \bfollowing\s+has\s+been\s+completed\b
    """
)
# Bullet-fragment completion claims ("- Dependencies installed:", "- Project
# initialized with:") — live incident: a fabricated re-narration used ONLY this
# shape and slipped the auxiliary-verb patterns above.
_BULLET_COMPLETION_CLAIM_RE = re.compile(
    r"(?im)^\s*[-•]\s*[^\n]{0,80}?\b(?:installed|initialized|implemented|confirmed|verified|updated|created)\b"
)


def _normalize_reply_for_patterns(text: str) -> str:
    """Markdown emphasis and contractions defeat literal patterns ("I will
    **re-run**", "I'm re-running") — strip emphasis and expand I'm before any
    promise/claim matching."""
    plain = re.sub(r"[*_`]+", "", text or "").replace("’", "'")
    return re.sub(r"(?i)\bi'm\b", "i am", plain)


# Execution-context nouns that turn a completion claim into an execution claim
# (files, installs, directories, steps) — a reply "I have completed the summary"
# is chat work; "I have completed step 5, files created, dependencies installed"
# with no route this turn is fabrication.
_EXECUTION_CONTEXT_RE = re.compile(
    r"""(?ix)
    \bstep\s*\#?\s*\d+\b | \bdependenc\w+\b | \binstall\w*\b | \bdirector(?:y|ies)\b |
    \bnpm\b | \brepo(?:sitory)?\b |
    [\w/-]+\.(?:js|jsx|ts|tsx|py|json|md|css|html|toml|yml|yaml)\b
    """
)


def _is_unrouted_execution_command(message: str) -> bool:
    stripped = _POLITENESS_PREFIX_RE.sub("", (message or "").strip(), count=1).strip()
    if not _UNROUTED_EXEC_COMMAND_RE.match(stripped):
        return False
    return bool(_STEP_REF_WORD_RE.search(stripped) or _STEP_REF_NUM_RE.search(stripped))


def _is_fabricated_execution_reply(*, message: str, response: str) -> bool:
    """True when a drafted reply claims this-turn execution and no route fired.

    The invariant: a real run ALWAYS arrives with a route block appended by
    the kernel, so first-person completion claims about files/installs/steps
    in a routeless reply are fabrication — regardless of whether the founder's
    message was itself a command (live incident: 'I already gave you the
    project path' drew a full fabricated completion report)."""
    plain = _normalize_reply_for_patterns(response)
    if not (
        _FABRICATED_COMPLETION_CLAIM_RE.search(plain)
        or _BULLET_COMPLETION_CLAIM_RE.search(plain)
    ):
        return False
    if _EXECUTION_CONTEXT_RE.search(plain):
        return True
    return _is_unrouted_execution_command(message)


def _unrouted_execution_fabrication_fallback(message: str, response: str = "") -> str:
    step = _STEP_REF_NUM_RE.search(message or "") or _STEP_REF_NUM_RE.search(response or "")
    routable = f"perform step {step.group(1)}" if step else "perform step N (or name the build/fix directly)"
    return (
        "I drafted a completion story with no run behind it — discarded. Nothing executed "
        "this turn: no delegated run fired, no files changed, nothing installed, no check ran. "
        "The only execution proof is a run item with real output attached. "
        f"Say `{routable}` and the kernel runs it immediately, with the actual outcome "
        "appended to the reply."
    )
_FILE_REFERENCE_RE = re.compile(
    r"`?([A-Za-z0-9][A-Za-z0-9_.\\/-]*\.[A-Za-z0-9][A-Za-z0-9_.-]{0,12})`?"
)


def _execution_claim_challenge_fallback(
    *, message: str, recent_turns: list[dict[str, Any]]
) -> str:
    if not _EXECUTION_CLAIM_CHALLENGE_RE.search(message or ""):
        return ""
    subject = _execution_claim_subject(message=message, recent_turns=recent_turns)
    files = _extract_file_references(message)
    file_line = (
        f" Your check says `{files[0]}` is missing."
        if files
        else " Your check contradicts the earlier completion claim."
    )
    correction_path = (
        f"give the project path and say `perform {subject}` (or paste the step again)"
        if subject.lower().startswith("step ")
        else "give the project path and the exact step or task to perform"
    )
    return (
        "You're right to challenge that. A chat claim is not execution evidence."
        f"{file_line} I should treat the earlier claim as unverified unless there is a "
        "delegated-run item, changed-file list, or real verification output behind it. "
        f"I won't call {subject} complete from prose. To correct the work, {correction_path}, "
        "and the runtime will route it through the coding agent with real output."
    )


def _extract_file_references(text: str) -> list[str]:
    seen: set[str] = set()
    files: list[str] = []
    for match in _FILE_REFERENCE_RE.finditer(text or ""):
        value = match.group(1).strip("`.,;:!?\"'")
        if value.lower() in seen:
            continue
        seen.add(value.lower())
        files.append(value)
    return files


def _execution_claim_subject(*, message: str, recent_turns: list[dict[str, Any]]) -> str:
    for text in [message or ""] + [
        str(_turn_field(turn, "content") or "")
        for turn in reversed(recent_turns or [])
        if str(_turn_field(turn, "role")).lower() == "assistant"
    ]:
        if not _COMPLETION_CLAIM_RE.search(text):
            continue
        step = _STEP_REF_NUM_RE.search(text)
        if step:
            return f"Step {step.group(1)}"
    return "the claimed work"


_AMBIGUOUS_ACTION_FOLLOWUPS = frozenset(
    {
        "do it",
        "do itr",
        "do this",
        "do that",
        "handle it",
        "handle this",
        "handle that",
        "make it happen",
        "go ahead",
        "run it",
        "start it",
    }
)


def _is_ambiguous_action_followup(message: str) -> bool:
    normalized = _normalize_replay_text(message)
    if normalized in _AMBIGUOUS_ACTION_FOLLOWUPS:
        return True
    if 1 <= len(normalized.split()) <= 3 and SequenceMatcher(None, normalized, "do it").ratio() >= 0.82:
        return True
    return False


def _ambiguous_action_replay_fallback(
    *, context: dict[str, Any], recent_turns: list[dict[str, Any]]
) -> str:
    subject = _ambiguous_followup_subject(context=context, recent_turns=recent_turns)
    return (
        f"I read that as \"do it\" on {subject}. I cannot safely execute an unnamed action, and nothing starts from an ambiguous chat reply. "
        f"The concrete path is to name the exact action to queue or run; if you mean {subject}, say the specific move and I will route it through the bridge or work queue. Which action do you want me to take?"
    )


def _ambiguous_followup_subject(
    *, context: dict[str, Any], recent_turns: list[dict[str, Any]]
) -> str:
    if (context.get("trading_bot_context") or {}).get("present"):
        return "the trading-bot work"
    for turn in reversed(recent_turns):
        if turn.get("role") != "assistant":
            continue
        text = _normalize_replay_text(str(turn.get("content", "")))
        if "trading bot" in text or "trading bot" in text.replace("tradingbot", "trading bot"):
            return "the trading-bot work"
        if "memory" in text:
            return "the memory action"
        if "research" in text:
            return "the research work"
    return "the prior recommendation"


def _trading_bot_authority_boundary_answer(*, context: dict[str, Any], response: str) -> str | None:
    trading_context = context.get("trading_bot_context") or {}
    if not trading_context.get("present"):
        return None
    if not _claims_trading_bot_live_or_order_mutation(response):
        return None
    status = trading_context.get("status") or {}
    capabilities = ((status.get("intelligence_access") or {}).get("capabilities") or {})
    training = ((capabilities.get("training") or {}).get("commands") or [])[:5]
    advisory = ((capabilities.get("advisory") or {}).get("routes") or [])[:3]
    training_text = ", ".join(str(item) for item in training) or "allowlisted training commands"
    advisory_text = ", ".join(str(item) for item in advisory) or "approval-gated advisory rows"
    return (
        "I cannot change live mode, broker/order authority, sizing, gates, or account-risk controls from this runtime. "
        "The safe intelligence path is to read recent signals, events, market context, and SQLite snapshots; "
        f"run {training_text}; compare decisions against realized or counterfactual outcomes; and write through {advisory_text}. "
        "If the evidence says the bot needs a runtime authority change, that becomes a separate explicit proposal, not a chat-side move."
    )


def _claims_trading_bot_live_or_order_mutation(text: str) -> bool:
    normalized = _normalize_replay_text(text)
    patterns = (
        "push the bridge to live mode",
        "push bridge to live mode",
        "switch the bridge to live",
        "switch to live mode",
        "change live mode",
        "let it bleed real data",
        "bleed real data",
        "place real trades",
        "execute real trades",
        "turn on broker authority",
        "give it broker authority",
        "mutate broker",
        "mutate order",
        "mutate sizing",
        "mutate gates",
        "change account risk",
    )
    return any(pattern in normalized for pattern in patterns)


_CAPABILITY_FOLLOWUP_PHRASES = frozenset(
    {
        "can you do this",
        "can you do that",
        "can you do it",
        "can you handle this",
        "can you handle that",
        "can you handle it",
        "are you able to do this",
        "are you able to do that",
        "are you able to do it",
        "is that something you are able to do",
        "is this something you are able to do",
        "is it something you are able to do",
        "is that something you can do",
        "is this something you can do",
        "is it something you can do",
        "can this be done by you",
        "can that be done by you",
    }
)


def _trading_bot_capability_followup_answer(
    *,
    message: str,
    context: dict[str, Any],
    recent_turns: list[dict[str, Any]],
) -> str | None:
    if not _is_capability_followup_question(message):
        return None
    trading_context = context.get("trading_bot_context") or {}
    recently_about_bot = any(_mentions_trading_bot(str(turn.get("content", ""))) for turn in recent_turns)
    if not trading_context.get("present") and not recently_about_bot:
        return None
    if trading_context.get("error"):
        return (
            "Not from the bridge as it stands. The trading-bot status check failed, so I cannot honestly say "
            f"I can do that until the bridge read is clean. Error: {trading_context['error']}"
        )
    status = trading_context.get("status") or {}
    if status and status.get("enabled") is False:
        return "Not right now. The trading-bot bridge is disabled, so I cannot inspect or train against it from this runtime."

    access = status.get("intelligence_access") or {}
    capabilities = access.get("capabilities") or {}
    training = ((capabilities.get("training") or {}).get("commands") or [])[:5]
    advisory = ((capabilities.get("advisory") or {}).get("routes") or [])[:3]
    training_text = ", ".join(str(item) for item in training) or "allowlisted training commands"
    advisory_text = ", ".join(str(item) for item in advisory) or "approval-gated advisory rows"

    return (
        "Yes - I can do the intelligence work, but not by pretending chat has trade authority. "
        "Through the bridge I can read recent signals, events, market context, and SQLite snapshots; "
        f"run {training_text}; and write through {advisory_text}. "
        "I cannot edit the bot's scoring code, mutate broker/order/sizing/gate state, or change account-risk controls from this chat surface.\n\n"
        "For the auto-buy scoring question, the first pass is not volatility weighting. It is evidence triage: group recent rejects by "
        "`hard_block_reason` and realized/counterfactual outcome, then tune score weights only if the outcomes show a calibration miss. "
        "If the reject reason is portfolio_full or cooldown, the scoring algorithm may be behaving correctly while capacity or replacement policy is the real bottleneck."
    )


def _trading_signal_hard_block_answer(*, message: str, context: dict[str, Any]) -> str | None:
    if not _mentions_trading_signal_analysis(message):
        return None
    trading_context = context.get("trading_bot_context") or {}
    signals = trading_context.get("recent_signals") or {}
    tables = signals.get("tables") or {}
    rows = _signal_rows(tables, "auto_buy_candidates") or _signal_rows(tables, "auto_buy_decision_snapshots")
    if not rows:
        return None

    hard_blocks: dict[str, int] = {}
    samples = []
    for row in rows[:8]:
        hard_block = str(row.get("hard_block_reason") or "").strip()
        if not hard_block:
            continue
        hard_blocks[hard_block] = hard_blocks.get(hard_block, 0) + 1
        symbol = str(row.get("symbol") or "?")
        score = row.get("score")
        samples.append(f"{symbol} score={score if score is not None else 'n/a'} blocked_by={hard_block}")
    if not hard_blocks:
        return None

    dominant = _format_counts(hard_blocks)
    sample_text = "; ".join(samples[:5])
    return (
        f"Do not start by changing volatility weighting. The live auto-buy snapshot is dominated by hard blocks: {dominant}. "
        f"Sample: {sample_text}. That means the score may be doing its job while capacity, cooldown, or replacement policy is stopping execution.\n\n"
        "Refine this in the right order: first group recent rejects by `hard_block_reason`; then compare those blocked names against weakest held positions "
        "and 30/60/390-minute counterfactual outcomes; then decide whether the fix belongs in capacity/replacement policy or in the scoring formula. "
        "Only touch volatility/liquidity weights after the outcome evidence shows score calibration error, such as high-score blocked names consistently outperforming held or approved names."
    )


def _needs_trading_signal_hard_block_repair(text: str) -> bool:
    normalized = _normalize_replay_text(text)
    scoring_terms = (
        "scoring algorithm",
        "scoring weights",
        "score weights",
        "volatility weighting",
        "adjust the weight",
        "adjust weights",
        "tune score",
        "refine the scoring",
    )
    return any(term in normalized for term in scoring_terms)


def _is_capability_followup_question(message: str) -> bool:
    normalized = _normalize_capability_followup(message)
    return normalized in _CAPABILITY_FOLLOWUP_PHRASES


def _normalize_capability_followup(message: str) -> str:
    text = (message or "").lower()
    text = text.replace("you're", "you are").replace("youre", "you are").replace("you’re", "you are")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


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


# A chat turn cannot start background work — runtime.respond only generates a
# reply. When the model still promises imminent or already-running work, the
# governor appends this deterministic correction so the founder is never left
# waiting on work that was never queued (the beacon staying Idle is the truth).
_BACKGROUND_WORK_HONESTY_LINE = (
    "Straight with you: this reply doesn't start anything — I can't launch work from chat. "
    "Queue it where it runs (Research for digging, a work item for actions), give me your word "
    "in the Inbox if it's gated, and watch the beacon go to Working. That's the only proof that counts."
)

_WORK_START_CLAIM_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bi\s+(?:will|'ll)\s+begin\b(?!\s+with\b)",
        r"\bbegin(?:ning)?\s+(?:immediately|right\s+away|now)\b",
        r"\bi\s+(?:have|'ve)\s+(?:initiated|launched|queued|dispatched|kicked\s+off)\b",
        r"\bi\s+(?:have|'ve)\s+(?:started|begun)\s+(?:the|a|an|this|that)\b",
        r"\bi\s+am\s+(?:now\s+)?(?:monitoring|running|executing|processing)\b",
        r"\bi\s+(?:will|'ll)\s+get\s+to\s+work\b",
        r"\bi\s+(?:will|'ll)\s+(?:re-?run|redo|retry)\b",
        r"\bi\s+am\s+(?:now\s+)?re-?running\b",
    )
)


def _claims_background_work_start(text: str) -> bool:
    normalized = _normalize_reply_for_patterns(text)
    return any(pattern.search(normalized) for pattern in _WORK_START_CLAIM_PATTERNS)


# --- chat action routing (chat turn -> real registered handler dispatch) ------

_MEMORY_COMMAND_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE | re.DOTALL)
    for pattern in (
        r"^\s*(?:please\s+)?(?:remember|record|note)\s+(?:this\s*)?[:,-]\s*(?P<content>.+)$",
        r"^\s*(?:please\s+)?(?:remember|record|note)\s+that\s+(?P<content>.+)$",
        r"^\s*(?:please\s+)?(?:save|add|write|record)\s+(?:this\s+)?(?:to|in)\s+(?:your\s+)?memory\s*[:,-]\s*(?P<content>.+)$",
        r"^\s*(?:please\s+)?(?:save|add|write|record)\s+(?P<content>.+?)\s+(?:to|in)\s+(?:your\s+)?memory\s*$",
        r"^\s*(?:please\s+)?make\s+(?:a\s+)?note\s+(?:that|of)?\s*(?P<content>.+)$",
    )
)

_MEMORY_QUESTION_RE = re.compile(r"^\s*(?:do|did|have)\s+you\s+remember\b", re.IGNORECASE)
_MEMORY_CONTENT_STOPWORDS = frozenset({"this", "that", "it", "memory", "note"})


def _extract_chat_action_command(message: str) -> dict[str, Any] | None:
    content = _extract_memory_command_content(message)
    if content:
        title = _chat_memory_title(content)
        return {
            "kind": "direct_chat_command",
            "title": f"Remember: {title}",
            "detail": content,
            "action": "local.memory.write",
            "target": "local_memory",
            "permission_tier": "L3_EXTERNAL_ACTION",
            "priority": 90,
            "metadata": {
                "kind": "chat_command",
                "memory_title": title,
                "content": content,
                "source": "founder.direct.chat",
            },
        }
    url = _extract_browser_open_url(message)
    if url:
        return {
            "kind": "direct_chat_command",
            "title": f"Open: {url}",
            "detail": f"Open {url} from the founder chat command.",
            "action": "local.browser.open",
            "target": url,
            "permission_tier": "L3_EXTERNAL_ACTION",
            "priority": 90,
            "metadata": {
                "url": url,
                "open_browser": True,
                "allow_external_url": not _is_local_browser_url(url),
                "source": "founder.direct.chat",
            },
        }
    return None


def _extract_memory_command_content(message: str) -> str:
    text = (message or "").strip()
    if not text or _MEMORY_QUESTION_RE.search(text):
        return ""
    for pattern in _MEMORY_COMMAND_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        content = _clean_chat_action_content(match.group("content"))
        if _valid_chat_action_content(content):
            return content[:4000]
    return ""


def _clean_chat_action_content(content: str) -> str:
    cleaned = re.sub(r"\s+", " ", (content or "").strip())
    return cleaned.strip(" \t\r\n\"'`")


def _valid_chat_action_content(content: str) -> bool:
    normalized = content.strip().lower()
    return len(normalized) >= 3 and normalized not in _MEMORY_CONTENT_STOPWORDS


def _chat_memory_title(content: str) -> str:
    title = re.sub(r"\s+", " ", content.strip())
    if len(title) <= 80:
        return title
    return f"{title[:77].rstrip()}..."


_BROWSER_OPEN_RE = re.compile(
    r"""(?ix)^\s*(?:
        please\s+|
        hey\s+zade[\s,:-]*|
        zade[\s,:-]*|
        can\s+you\s+|
        could\s+you\s+|
        would\s+you\s+
    )?
    (?:open|launch|pull\s+up|bring\s+up)\s+
    (?P<url>(?:https?://|file://)[^\s<>"'`)\]]+)
    """,
)


def _extract_browser_open_url(message: str) -> str:
    match = _BROWSER_OPEN_RE.search(message or "")
    if not match:
        return ""
    return match.group("url").rstrip(".,;:!?)]}\"'")


def _is_local_browser_url(url: str) -> bool:
    lowered = url.lower()
    return (
        lowered.startswith("file://")
        or "://127.0.0.1" in lowered
        or "://localhost" in lowered
        or "://[::1]" in lowered
    )


def _render_chat_action_route_block(route: dict[str, Any]) -> str:
    title = route.get("title") or route.get("action") or "that action"
    item_id = route.get("item_id")
    item_ref = f"work item #{item_id}" if item_id is not None else "a work item"
    status = route.get("status")
    if status == "dispatched":
        result = route.get("result") or {}
        handler = result.get("handler") or route.get("action")
        return f"Done - I executed {title} from chat through {handler} ({item_ref})."
    if status == "queued":
        return f"Queued {title} from chat as {item_ref}; it is waiting in status {route.get('queue_status', 'queued')}."
    if status == "denied":
        reason = (route.get("authority") or {}).get("reason", "authority denied it")
        return f"Blocked - I did not execute {title}. {reason}"
    return f"I tried to execute {title} from chat and hit a problem: {route.get('error', 'unknown error')}."


def _chat_action_route_note(route: dict[str, Any]) -> str:
    status = route.get("status")
    if status == "dispatched":
        return "Detected a founder chat command and dispatched it through the registered local handler."
    if status == "queued":
        return "Detected a founder chat command and queued it, but it was not immediately dispatchable."
    if status == "denied":
        return "Detected a founder chat command, but authority denied it before dispatch."
    return "Detected a founder chat command, but dispatch failed; surfaced the failure honestly."


def _chat_action_route_summary(route: dict[str, Any] | None) -> dict[str, Any] | None:
    if not route:
        return None
    summary: dict[str, Any] = {
        "status": route.get("status"),
        "action": route.get("action"),
        "title": route.get("title"),
        "target": route.get("target", ""),
    }
    for key in ("item_id", "created", "queue_status", "dispatch", "audit_id", "error"):
        if route.get(key) is not None:
            summary[key] = route.get(key)
    if route.get("authority"):
        summary["authority"] = route.get("authority")
    result = route.get("result") or {}
    if result:
        summary["result"] = {
            key: result.get(key)
            for key in ("handler", "status", "memory_id", "path", "url", "audit_id")
            if result.get(key) is not None
        }
    return summary


# --- research-command routing (chat turn -> gated work queue) -----------------

# Verbs that mark a founder message as a command to *do* research work now, each
# followed by the topic. Matched after leading politeness/address is stripped.
_RESEARCH_INTENT_RE = re.compile(
    r"""(?ix)
    \b(?:
        research |
        investigate |
        look\s+into |
        dig\s+(?:into|up) |
        read\s+up\s+on |
        study\s+up\s+on |
        find\s+(?:me\s+)?sources?\s+(?:on|about|for) |
        find\s+out\s+(?:everything\s+)?about |
        learn\s+(?:everything|all|more)?\s*(?:possible\s+)?(?:about|regarding|on)
    )
    \s+(?P<topic>.+)
    """,
)

# Interrogatives that make a message a question about research rather than a
# command to perform it. "Can you / could you / would you research X" are commands
# and are deliberately excluded.
_RESEARCH_QUESTION_PREFIXES = (
    "how ", "what ", "why ", "when ", "where ", "who ",
    "do you ", "does ", "did you ", "have you ", "is ", "are ",
    "should i ", "can i ", "could i ", "would i ",
)

# Consumes a leading connective clause left in the captured topic — including a
# chained second verb ("research AND LEARN everything possible REGARDING x") — down
# to the actual subject.
_RESEARCH_TOPIC_LEADING_FILLER = re.compile(
    r"""(?ix)^
    (?:and\s+)?
    (?:learn|research|study|read\s+up|find\s+out|investigate|dig(?:\s+into|\s+up)?|look\s+into)?\s*
    (?:everything|all|more)?\s*
    (?:possible\s+)?
    (?:about|regarding|on|into|for|of|the\s+topic\s+of|the\s+subject\s+of)\s+
    """
)

# Strips trailing politeness and a dangling source-introducing connective left when
# a URL is removed ("research x USING <url>" -> "research x using" -> "x").
_RESEARCH_TOPIC_TRAILING_FILLER = re.compile(
    r"(?i)\s+(?:using|via|from|at|with|per|for\s+me|please|asap|right\s+now|today|thanks|thank\s+you)\W*$"
)

_RESEARCH_TOPIC_STOPWORDS = frozenset({"it", "that", "this", "them", "those", "these", "stuff", "things"})

_POLITENESS_PREFIX_RE = re.compile(
    r"""(?ix)^\s*(?:
        please | pls | hey | hi | ok | okay | so | now |
        zade | hey\s+zade | can\s+you | could\s+you | would\s+you | will\s+you |
        i\s+(?:want|need|would\s+like)\s+you\s+to | i'?d\s+like\s+you\s+to | go\s+(?:ahead\s+and\s+)? | just
    )\b[\s,:-]*"""
)

_URL_RE = re.compile(r"https://[^\s<>\"'`)\]]+", re.IGNORECASE)

_LOCAL_SYSTEM_NAMED_MARKERS = (
    "zade",
    "localaicofounder",
    "local ai cofounder",
    "cofounder kernel",
    "trading-bot",
    "trading bot",
    "ai brain",
    "memory-hot",
    "wsl",
    "ubuntu-tradingbot",
    "127.0.0.1",
    "localhost",
)

_LOCAL_SYSTEM_SURFACE_MARKERS = (
    "repo",
    "repository",
    "workspace",
    "database",
    "sqlite",
    " db",
    "runtime",
    "kernel",
    "events",
    "signals",
    "market context",
    "logs",
    "scheduler",
    "cron",
    "git",
    "process",
    "service",
)

_LOCAL_SYSTEM_SCOPE_MARKERS = (
    "this ",
    "our ",
    "my ",
    "local ",
    "current ",
    "running ",
    "live ",
)

_LOCAL_SYSTEM_ACTION_MARKERS = (
    "research",
    "investigate",
    "inspect",
    "diagnose",
    "audit",
    "check",
    "read",
    "watch",
    "monitor",
    "analyze",
    "look into",
    "learn",
)

_LOCAL_PATH_RE = re.compile(
    r"(?i)(?:(?:^|[\s\"'(\[])[a-z]:[\\/]|\\\\wsl|wsl\.localhost|/home/|/mnt/|\.sqlite\b|\.db\b)"
)


def _extract_urls(message: str) -> list[str]:
    """Pull explicit https URLs out of the founder message (egress requires https)."""
    urls: list[str] = []
    for match in _URL_RE.findall(message or ""):
        url = match.rstrip(".,;:!?)]}\"'")
        if url not in urls:
            urls.append(url)
    return urls


def _extract_research_topic(message: str) -> str:
    """Return the topic of a founder research *command*, or "" when the message is
    not a do-research instruction (a question about research, or no topic)."""
    text = (message or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered.startswith(_RESEARCH_QUESTION_PREFIXES):
        return ""
    # Strip a leading politeness/address so "Please research X" -> "research X".
    stripped = _POLITENESS_PREFIX_RE.sub("", text, count=1).strip()
    match = _RESEARCH_INTENT_RE.search(stripped)
    if not match:
        return ""
    topic = match.group("topic").strip()
    # A URL is a source, not the topic phrase — don't fold it into the topic text.
    topic = _URL_RE.sub("", topic).strip()
    topic = _RESEARCH_TOPIC_LEADING_FILLER.sub("", topic).strip()
    # Trailing filler can chain ("... using for me"); strip until stable.
    while True:
        stripped = _RESEARCH_TOPIC_TRAILING_FILLER.sub("", topic).strip()
        if stripped == topic:
            break
        topic = stripped
    topic = topic.strip(" \t\r\n.,;:!?\"'-—")
    if len(topic) < 3 or topic.lower() in _RESEARCH_TOPIC_STOPWORDS:
        return ""
    return topic[:200]


def _is_local_system_investigation_topic(message: str, topic: str) -> bool:
    stripped_message = _POLITENESS_PREFIX_RE.sub("", message or "", count=1)
    text = _URL_RE.sub("", f"{stripped_message} {topic or ''}").lower()
    if not any(marker in text for marker in _LOCAL_SYSTEM_ACTION_MARKERS):
        return False
    if _LOCAL_PATH_RE.search(text):
        return True
    if any(marker in text for marker in _LOCAL_SYSTEM_NAMED_MARKERS):
        return True
    scoped_to_local = any(marker in text for marker in _LOCAL_SYSTEM_SCOPE_MARKERS)
    names_local_surface = any(marker in text for marker in _LOCAL_SYSTEM_SURFACE_MARKERS)
    return scoped_to_local and names_local_surface


def _is_local_trading_bot_investigation_topic(message: str, topic: str) -> bool:
    return _is_local_system_investigation_topic(message, topic)


def _render_research_route_block(route: dict[str, Any]) -> str:
    topic = route.get("topic", "")
    status = route.get("status")
    if status == "queued":
        queued = route.get("queued") or {}
        item_id = queued.get("item_id")
        count = queued.get("url_count", len(route.get("urls", [])))
        plural = "source" if count == 1 else "sources"
        return (
            f"Queued it — research on {topic}. I filed the topic, proposed {count} {plural}, "
            f"and dropped an approval-gated run in your Inbox (item #{item_id}). "
            "It's egress, so nothing leaves this machine until you clear it with the typed phrase — "
            "swap my sources for better ones there if you want. That's the word I need from you."
        )
    if status == "no_sources":
        return (
            f"Logged {topic} as an open research question, but I couldn't propose a clean source to fetch. "
            "Hand me a URL or two and I'll queue the gated run for your approval."
        )
    return (
        f"Tried to queue research on {topic} and hit a snag: {route.get('error', 'unknown error')}. "
        "Nothing left this machine. Give me the word and I'll retry."
    )


def _research_route_note(route: dict[str, Any]) -> str:
    status = route.get("status")
    if status == "queued":
        return (
            "Detected a research command; filed the topic, proposed sources, and queued an "
            "approval-gated research run to the Inbox (egress stays behind the typed-phrase approval)."
        )
    if status == "no_sources":
        return "Detected a research command and logged the topic, but no source could be proposed; asked the founder for URLs."
    return "Detected a research command but failed to queue the run; surfaced the failure honestly."


def _research_route_summary(route: dict[str, Any] | None) -> dict[str, Any] | None:
    if not route:
        return None
    summary: dict[str, Any] = {"status": route.get("status"), "topic": route.get("topic")}
    if route.get("assumption_id") is not None:
        summary["assumption_id"] = route.get("assumption_id")
    if route.get("related"):
        summary["related"] = route.get("related")
    if route.get("status") == "queued":
        queued = route.get("queued") or {}
        summary |= {
            "item_id": queued.get("item_id"),
            "queue_status": queued.get("status"),
            "url_count": queued.get("url_count", len(route.get("urls", []))),
            "urls": route.get("urls", []),
            "action": queued.get("action"),
        }
    elif route.get("status") == "error":
        summary["error"] = route.get("error", "")
    return summary


# ---- build-command routing (chat -> delegation) ----

# The whole message is an anaphoric build command ("build this out for me",
# "help me build this", "let's build it") — the task lives in the conversation,
# not the message. Politeness prefixes are stripped before this is applied.
_BUILD_ANAPHORIC_RE = re.compile(
    r"""(?ix)^\s*
    (?:help\s+me\s+|help\s+us\s+|let'?s\s+|go\s+ahead\s+and\s+|start\s+|just\s+)*
    build(?:ing)?
    (?:\s+(?:this|it|that))?
    (?:\s+out)?
    (?:\s+(?:for\s+me|for\s+us|together|now|please))?
    \s*[.!]*\s*$"""
)

# An explicit build command with the task in the message ("build me a book
# cataloguing app", "scaffold a billing service", "prototype an MVP for X").
# Anchored to the start of the politeness-stripped message so metaphorical
# uses mid-sentence ("we should build trust with customers") never route.
_BUILD_VERB_RE = re.compile(
    r"""(?ix)^\s*
    (?:help\s+me\s+|let'?s\s+|start\s+|i\s+(?:want|need)\s+to\s+|i'?d\s+like\s+to\s+)?
    (?:build|scaffold|prototype|code\s+up)\s+
    (?:me\s+|us\s+|out\s+)?
    (?P<task>.+)
    """
)

# "create/make/spin up" only counts as a build command when it names an
# app-shaped deliverable — otherwise it collides with ordinary chat verbs.
_BUILD_CREATE_RE = re.compile(
    r"""(?ix)^\s*
    (?:help\s+me\s+|let'?s\s+|i\s+(?:want|need)\s+to\s+|i'?d\s+like\s+to\s+)?
    (?:create|make|spin\s+up|stand\s+up)\s+
    (?:me\s+|us\s+)?
    (?P<task>(?:a|an|the|another)\s+(?:new\s+)?(?:[\w-]+\s+){0,4}?
        (?:app|apps|application|applications|mvp|saas|prototype|website|web\s*site|
           web\s+app|mobile\s+app|service|tool|bot|dashboard|api)\b.*)
    """
)

# Residual anaphora once the verb is stripped ("build this", "build it out").
_BUILD_TASK_STOPWORDS = {
    "this", "it", "that", "this out", "it out", "that out", "this for me",
    "it for me", "this out for me", "one", "something", "me", "us",
}


def _extract_build_task(message: str) -> tuple[str, bool] | None:
    """Return (task, anaphoric) for a founder build *command*, or None when the
    message is not one (a design question, or no buildable target)."""
    text = (message or "").strip()
    if not text:
        return None
    if text.lower().startswith(_RESEARCH_QUESTION_PREFIXES):
        return None
    stripped = _POLITENESS_PREFIX_RE.sub("", text, count=1).strip()
    if stripped.lower().startswith(_RESEARCH_QUESTION_PREFIXES):
        return None
    if _BUILD_ANAPHORIC_RE.match(stripped):
        return "", True
    match = _BUILD_VERB_RE.search(stripped) or _BUILD_CREATE_RE.search(stripped)
    if not match:
        return None
    task = match.group("task").strip(" \t\r\n.,;:!?\"'-")
    if len(task) < 5 or task.lower() in _BUILD_TASK_STOPWORDS:
        # "build this ..." variants that slipped past the anaphoric form.
        return "", True
    return task[:300], False


# Maintenance commands ("fix the vulnerabilities", "resolve them on your own",
# "update the vulnerable packages") are the other half of the narrated-work
# failure family: the founder orders a change to an EXISTING project rather
# than a new build. Maintenance verbs are collision-prone in ordinary chat
# ("fix the meeting time", "update me"), so a message only routes when it also
# names a code-shaped subject — in the message itself, or (for "fix them"
# anaphora) somewhere in the recent thread.
_MAINTENANCE_VERB_RE = re.compile(
    r"""(?ix)\b(?:
        fix | resolve | patch | remediate | repair | mitigate |
        clean\s+up | sort\s+out | take\s+care\s+of | get\s+rid\s+of |
        upgrade | update | replace
    )\b"""
)
_MAINTENANCE_SUBJECT_RE = re.compile(
    r"""(?ix)\b(?:
        vulnerab\w+ | cves? | npm\s+audit | audit\s+(?:report|issues?|findings?) |
        security\s+(?:issues?|findings?|problems?|holes?|advisories) |
        dependenc\w+ | packages? | deprecat\w+ |
        lint\s+(?:errors?|warnings?) | type\s+errors? |
        failing\s+tests? | test\s+failures? | broken\s+tests? |
        build\s+(?:errors?|failures?) | compile\s+errors? | bugs?
    )\b"""
)
# Anaphoric maintenance ("fix them", "resolve those") — the subject must then
# come from the thread. Deliberately excludes update/upgrade/replace: "update
# it" is too ambiguous to treat as a code command.
_MAINTENANCE_ANAPHORA_RE = re.compile(
    r"""(?ix)\b(?:fix|resolve|patch|remediate|repair|clean\s+up|sort\s+out|
        take\s+care\s+of|get\s+rid\s+of)\s+
        (?:them|these|those|it|that|all\s+of\s+(?:them|it)|everything)\b"""
)
# A message that carries terminal-paste markers is evidence the founder is
# showing, not a command — routing it would queue work she didn't order yet.
_TERMINAL_PASTE_RE = re.compile(
    r"(?im)^\s*(?:PS\s+[A-Za-z]:\\|[A-Za-z]:\\[^\n]*>)|npm\s+(?:warn|ERR!)|#\s*npm\s+audit\s+report"
)
_WINDOWS_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s\"'<>|?*`\n]+")
_KERNEL_SOURCE_ROOT = Path(__file__).resolve().parents[2]


def _extract_maintenance_task(message: str) -> tuple[str, bool] | None:
    """Return (task, subject_in_message) for a founder maintenance *command*
    ("fix/resolve/update X in the project"), or None when the message is not
    one (a question, a terminal paste, or no maintenance verb/subject)."""
    text = (message or "").strip()
    if not text:
        return None
    if _TERMINAL_PASTE_RE.search(text):
        return None
    if text.lower().startswith(_RESEARCH_QUESTION_PREFIXES):
        return None
    stripped = _POLITENESS_PREFIX_RE.sub("", text, count=1).strip()
    if not stripped or stripped.lower().startswith(_RESEARCH_QUESTION_PREFIXES):
        return None
    if not _maintenance_verb_is_commanded(stripped):
        return None
    subject_in_message = bool(_MAINTENANCE_SUBJECT_RE.search(stripped))
    if not subject_in_message and not _MAINTENANCE_ANAPHORA_RE.search(stripped):
        return None
    task = re.sub(r"\s+", " ", stripped).strip(" \t\r\n.,;:!?\"'-")
    if len(task) < 5:
        return None
    return task[:300], subject_in_message


# Determiners/possessives in front of a maintenance word mean it is being used
# as a NOUN ("outline the fix", "review your patch") — a request for prose, not
# an execution command. With directed runs executing at full auto, a noun-usage
# false positive would launch real work, so it must not route.
_MAINTENANCE_NOUN_PRECEDERS = frozenset(
    {"the", "a", "an", "this", "that", "these", "those", "my", "your", "our", "his", "her", "their", "its"}
)


def _maintenance_verb_is_commanded(text: str) -> bool:
    """True when at least one maintenance verb appears in verb position (not
    directly preceded by a determiner/possessive)."""
    for match in _MAINTENANCE_VERB_RE.finditer(text):
        before = text[: match.start()].rstrip()
        preceding_word = re.split(r"[^\w']+", before)[-1].lower() if before else ""
        if preceding_word not in _MAINTENANCE_NOUN_PRECEDERS:
            return True
    return False


def _thread_names_maintenance_subject(turns: list[Any]) -> bool:
    for turn in reversed((turns or [])[-10:]):
        if str(_turn_field(turn, "role")).lower() != "user":
            continue
        if _MAINTENANCE_SUBJECT_RE.search(str(_turn_field(turn, "content") or "")):
            return True
    return False


def _extract_project_target(turns: list[Any], *, current_message: str = "") -> str:
    """The project directory a maintenance command targets: the most recent
    real, existing directory the founder named in this thread (terminal prompts
    like 'PS C:\\App>' count; a file path yields its directory). Never the
    kernel's own repo — Zade does not modify itself from a chat route."""
    texts = [current_message or ""] + [
        str(_turn_field(turn, "content") or "")
        for turn in reversed((turns or [])[-10:])
        if str(_turn_field(turn, "role")).lower() == "user"
    ]
    for text in texts:
        for raw in _WINDOWS_PATH_RE.findall(text):
            cleaned = raw.rstrip(">.,;:!?\"'`")
            try:
                path = Path(cleaned)
                if path.is_file():
                    path = path.parent
                if not path.is_dir():
                    continue
                resolved = path.resolve()
            except OSError:
                continue
            if resolved == _KERNEL_SOURCE_ROOT or _KERNEL_SOURCE_ROOT in resolved.parents:
                continue
            return str(resolved)
    return ""


# Step-execution commands ("perform all tasks related to step 5", "write step
# 5 for me", "do it" right after a step was laid out) are the third shape of
# the narrated-work family: the plan already exists in the thread — usually as
# Zade's own numbered instructions — and the founder orders it EXECUTED. The
# instructions resolve from the conversation and become the delegation brief.
_STEP_EXEC_VERB_RE = re.compile(
    r"""(?ix)^\s*(?:
        perform | do | complete | execute | carry\s+out | handle |
        implement | write | finish | run | re-?run | redo | retry
    )\b"""
)
_STEP_REF_WORD_RE = re.compile(r"(?i)\b(?:steps?|tasks?|phases?)\b")
_STEP_REF_NUM_RE = re.compile(r"(?i)\bstep\s*#?\s*(\d+)\b")
# Bare execution anaphora ("do it", "handle that") — the referent must then be
# the most recent assistant turn that actually laid out runnable instructions.
_STEP_PURE_ANAPHORA_RE = re.compile(
    r"(?ix)^\s*(?:do|handle|take\s+care\s+of|execute|run)\s+(?:it|this|that|them)\s*[.!]*\s*$"
)
# What counts as runnable step instructions in an assistant turn: a numbered
# step heading, or a fenced command/code block alongside a numbered list.
_STEP_STRUCTURE_RE = re.compile(r"(?im)\bstep\s*#?\s*\d+\b|^\s*\d+\.\s.+$")


def _extract_step_execution(message: str) -> tuple[int | None, bool] | None:
    """Return (step_number, bare_anaphora) for a founder step-execution
    *command*, or None when the message is not one. step_number is None for
    unnumbered forms ("do all the tasks"); bare_anaphora marks "do it" forms
    whose entire referent must come from the thread."""
    text = (message or "").strip()
    if not text or _TERMINAL_PASTE_RE.search(text):
        return None
    if text.lower().startswith(_RESEARCH_QUESTION_PREFIXES):
        return None
    stripped = _POLITENESS_PREFIX_RE.sub("", text, count=1).strip()
    if not stripped or stripped.lower().startswith(_RESEARCH_QUESTION_PREFIXES):
        return None
    if _STEP_PURE_ANAPHORA_RE.match(stripped):
        return None, True
    if _STEP_EXEC_VERB_RE.match(stripped) and _STEP_REF_WORD_RE.search(stripped):
        match = _STEP_REF_NUM_RE.search(stripped)
        return (int(match.group(1)) if match else None), False
    # A command verb buried behind a lead-in clause still commands (live
    # incidents: "Let's try this again, re-run all tasks for Steps 1 - 5" and
    # "You already have the project path, you have the steps, complete all
    # tasks associated with steps 1 - 5" both went unrouted). Scan clauses;
    # verb and step reference must sit in the SAME clause so "the steps are
    # done, complete honesty matters" can never route.
    for clause in re.split(r"[,;.!?]+", stripped):
        clause = _POLITENESS_PREFIX_RE.sub("", clause.strip(), count=1).strip()
        if not clause:
            continue
        if _STEP_EXEC_VERB_RE.match(clause) and _STEP_REF_WORD_RE.search(clause):
            match = _STEP_REF_NUM_RE.search(clause) or _STEP_REF_NUM_RE.search(stripped)
            return (int(match.group(1)) if match else None), False
    return None


_SYNTHETIC_REPLY_MARKERS_RE = re.compile(
    r"(?m)^(?:Ran|Started|Queued|Took) the (?:build|fix|step run) -"
    r"|^The (?:build|fix|step run) is NOT done -"
    r"|A chat claim is not execution evidence"
    r"|Nothing executed this turn"
)


def _strip_synthetic_reply_text(content: str) -> str:
    """Cut runtime-generated segments (route blocks, evidence-boundary
    fallbacks) out of an assistant turn. These blocks mention steps and tasks
    by name, so leaving them in poisons referent resolution: 'perform step 5'
    must resolve to the real step-5 instructions, never to a route block that
    happened to say 'Step 5' last."""
    match = _SYNTHETIC_REPLY_MARKERS_RE.search(content or "")
    return content[: match.start()] if match else content


def _resolve_step_instructions(
    turns: list[Any], *, step_number: int | None = None, latest_only: bool = False
) -> str:
    """Find the instructions the founder is pointing at: the most recent
    assistant turn mentioning the numbered step, or (unnumbered) the most
    recent assistant turn shaped like step instructions. latest_only restricts
    the search to the last assistant turn — bare "do it" refers to what was
    just said, not anything earlier. Runtime-generated reply text is stripped
    first so route blocks and fallbacks are never mistaken for instructions."""
    assistant_turns = [
        _strip_synthetic_reply_text(str(_turn_field(turn, "content") or ""))
        for turn in (turns or [])
        if str(_turn_field(turn, "role")).lower() == "assistant"
    ]
    if latest_only:
        assistant_turns = assistant_turns[-1:]
    # 24-turn window: threads under repair accumulate synthetic and fabricated
    # turns fast, and a 12-turn window starved a live run of the real
    # instructions (route no_task) while poison sat closer to the surface.
    for content in reversed(assistant_turns[-24:]):
        if not content.strip():
            continue
        if step_number is not None:
            anchor = re.search(rf"(?i)\bstep\s*#?\s*{step_number}\b", content)
            if not anchor:
                continue
            candidate = content[anchor.start() : anchor.start() + 1500]
            if _FABRICATED_COMPLETION_CLAIM_RE.search(
                _normalize_reply_for_patterns(candidate)
            ) or _BULLET_COMPLETION_CLAIM_RE.search(_normalize_reply_for_patterns(candidate)):
                # A completion NARRATIVE mentioning the step ("Step 5 ... has
                # been completed") is not instructions — resolving it into a
                # brief poisoned two live runs. Keep looking further back.
                continue
            return candidate
        if _STEP_STRUCTURE_RE.search(content) or "```" in content:
            candidate = content[:1500]
            if _FABRICATED_COMPLETION_CLAIM_RE.search(
                _normalize_reply_for_patterns(candidate)
            ) or _BULLET_COMPLETION_CLAIM_RE.search(_normalize_reply_for_patterns(candidate)):
                continue
            return candidate
    return ""


def _anaphoric_build_task(
    turns: list[Any], *, current_message: str = ""
) -> str:
    """Resolve "build this" against the conversation: the task is the most recent
    substantive founder turn that is not itself a build command."""
    current = (current_message or "").strip().lower()
    for turn in reversed(turns or []):
        if str(_turn_field(turn, "role")).lower() != "user":
            continue
        content = re.sub(r"\s+", " ", str(_turn_field(turn, "content")).strip())
        if not content or content.lower() == current:
            continue
        if _extract_build_task(content) == ("", True):
            continue
        if len(content) >= 20:
            return content[:300].strip(" \t\r\n.,;:!?\"'-")
    return ""


def _conversation_build_context(
    turns: list[Any], *, current_message: str = "", max_chars: int = 2400
) -> str:
    """Pack the recent conversation into the delegation brief's context section so
    the external agent sees the same scoping the founder and Zade just did."""
    lines: list[str] = []
    for turn in (turns or [])[-10:]:
        role = str(_turn_field(turn, "role")).strip() or "user"
        content = re.sub(r"\s+", " ", str(_turn_field(turn, "content")).strip())
        if content:
            lines.append(f"{role}: {content[:600]}")
    if current_message.strip():
        lines.append(f"user: {re.sub(r'\\s+', ' ', current_message.strip())[:600]}")
    text = "\n".join(lines)
    return text[-max_chars:] if len(text) > max_chars else text


def _turn_field(turn: Any, field: str) -> Any:
    if isinstance(turn, dict):
        return turn.get(field, "")
    return getattr(turn, field, "")


def _render_build_route_block(route: dict[str, Any]) -> str:
    status = route.get("status")
    kind = str(route.get("kind") or "build")
    noun = {"maintenance": "fix", "step": "step run"}.get(kind, "build")
    task = str(route.get("task") or f"the requested {noun}").strip(" \t\r\n.,;:!?\"'-")
    item_id = route.get("item_id")
    target_line = ""
    if kind in {"maintenance", "step"}:
        workspace = str(route.get("workspace") or "").strip()
        target_line = (
            f" Target project: {workspace}."
            if workspace
            else (
                " No project directory is named in this thread, so the run uses my "
                "delegation workspace — give me the project path if that's wrong."
            )
        )
    if status in {"executed", "verify_failed"}:
        dispatch = route.get("dispatch") or {}
        changed = [str(f) for f in dispatch.get("changed_files") or []]
        changed_line = (
            f" Changed {len(changed)} file(s): {', '.join(changed[:5])}"
            + ("…" if len(changed) > 5 else "")
            + "."
            if changed
            else " No files needed changing."
        )
        evidence_line = (
            f" Artifact filed as delegated-work evidence (item #{item_id})."
            if dispatch.get("evidence_id")
            else f" Full detail is on item #{item_id}."
        )
        if status == "verify_failed":
            verification = route.get("verification") or {}
            failing = [
                " ".join(str(a) for a in (check.get("argv") or []))
                for check in verification.get("checks") or []
                if not check.get("ok")
            ]
            failing_line = f" ({'; '.join(failing)})" if failing else ""
            repairs = int(verification.get("repair_rounds") or 0)
            repair_line = (
                f" I fed the failure back for {repairs} repair round(s) and it still fails."
                if repairs
                else ""
            )
            return (
                f"The {noun} is NOT done - {task}.{target_line} My local coding agent "
                f"({dispatch.get('model') or 'local model'}) changed {len(changed)} file(s), but "
                f"the kernel's own check on the result FAILED{failing_line}, so I'm not calling "
                f"it complete.{repair_line} The real failing output is on item #{item_id}. "
                "Redirect me or say to go again and I'll run another pass."
            )
        if route.get("auto_verified"):
            verify_line = (
                " Kernel-run verification passed on real output."
                if route.get("verified_mode") != "syntax"
                else (
                    " Kernel-run syntax check passed on real output — parse-level only; "
                    "this workspace has no test entry point, so behavior is unverified."
                )
            )
        elif dispatch.get("unverified_claims"):
            verify_line = (
                " Heads up: it asserted checks it never ran — treat those as unconfirmed."
            )
        elif changed:
            verify_line = (
                " No check could verify this run — the workspace has no test entry point "
                "and the changed files have no local checker — so treat it as UNVERIFIED "
                "until something real exercises it."
            )
        else:
            verify_line = ""
        return (
            f"Ran the {noun} - {task}.{target_line} Executed just now by my local coding agent "
            f"({dispatch.get('model') or 'local model'}).{changed_line}{verify_line}{evidence_line}"
        )
    if status == "needs_decision":
        question = route.get("question") or {}
        q_text = str(question.get("question") or "").strip() or "I need your direction to continue."
        options = [str(o) for o in question.get("options") or []]
        options_line = f" Options: {'; '.join(options)}." if options else ""
        decision_id = route.get("decision_item_id")
        inbox_line = (
            f" It's also in your Inbox as item #{decision_id} — clear it and I proceed on best safe judgment."
            if decision_id
            else ""
        )
        return (
            f"Started the {noun} - {task} - and stopped on one call that's yours to make: "
            f"{q_text}{options_line} Answer here and I'll run it through.{inbox_line}"
        )
    if status == "run_failed":
        return (
            f"Took the {noun} - {task} - straight to execution and it failed: "
            f"{str(route.get('error') or 'unknown error')[:300]}{target_line} "
            f"Nothing papered over — the run and its error are on item #{item_id}. "
            "Fix or redirect and tell me to go again."
        )
    if status == "queued":
        reason = str(route.get("reason") or "").strip()
        reason_line = f" I couldn't run it immediately ({reason})." if reason else ""
        if route.get("agent_configured"):
            engine_label = (
                "my local coding agent (loopback Ollama)"
                if route.get("engine", "native") == "native"
                else "the local compatibility bridge"
            )
            return (
                f"Queued the {noun} - {task}.{target_line}{reason_line} The scoped brief is in your Inbox "
                f"(item #{item_id}), pre-approved by your command — dispatch it and {engine_label} runs the "
                f"{noun} and files the artifact back as delegated-work evidence."
            )
        return (
            f"Queued the {noun} - {task}.{target_line} I packaged a scoped brief (item #{item_id}), but no build "
            "engine can run right now ([delegation] engine/agent_command in config.toml), so approving "
            "it hands you the brief to run manually. Fix the engine config and I can run this end to end."
        )
    if status == "no_task":
        if kind == "step":
            return (
                "You told me to run the step, but I can't find the step instructions in this "
                "thread. Point me at the step (or paste it) and I'll queue the delegated run."
            )
        return (
            "You told me to build, but this thread hasn't scoped a target yet. "
            "Give me one line on what it is and I'll queue the delegated build."
        )
    return (
        f"Tried to queue the {noun} on {task or 'that'} and hit a snag: {route.get('error', 'unknown error')}. "
        "Nothing was dispatched. Give me the word and I'll retry."
    )


# A drafted reply that denies the ability to execute while a real item just
# queued — the classic "I'm not able to execute actions directly..." opener.
_EXECUTION_INABILITY_RE = re.compile(
    r"""(?ix)\b(?:i\s*am|i'?m)\s+not\s+able\s+to\s+(?:directly\s+)?
        (?:execute|run|perform|make|modify|access)
        |\bi\s+can(?:no|')t\s+(?:directly\s+)?(?:execute|run|perform|modify|access)"""
)

_BUILD_DEFERRAL_QUESTION_RE = re.compile(
    r"""\s*
    (?:
        Would\s+you\s+like\s+me\s+to\s+(?:write|create|draft|provide|generate|put\s+together|outline)\b.{0,240}\?
        |
        Do\s+you\s+want\s+me\s+to\b.{0,240}\?
        |
        Should\s+I\b.{0,240}\?
    )
    \s*$""",
    re.IGNORECASE | re.DOTALL | re.VERBOSE,
)


def _remove_build_deferral_question(text: str) -> str:
    """Build commands already routed to a real queue item should not end by
    asking whether Zade should merely plan or outline the work."""
    return _BUILD_DEFERRAL_QUESTION_RE.sub("", text).strip()


def _build_route_note(route: dict[str, Any]) -> str:
    status = route.get("status")
    kind = str(route.get("kind") or "build")
    if kind not in {"maintenance", "step"}:
        kind = "build"
    if status == "executed":
        return (
            f"Detected a directed {kind} command; the delegated run executed immediately at full auto "
            "(founder command is the authorization) and the real outcome is in the reply."
        )
    if status == "verify_failed":
        return (
            f"Detected a directed {kind} command; the run executed but the kernel's own check on "
            "the result failed, so the reply reports the work as NOT done, with the real output."
        )
    if status == "needs_decision":
        return (
            f"Detected a directed {kind} command; the run started and paused on a genuine founder "
            "decision, filed as a founder_decision Inbox item and surfaced in the reply."
        )
    if status == "run_failed":
        return (
            f"Detected a directed {kind} command; the delegated run executed immediately and failed — "
            "the failure is reported honestly in the reply."
        )
    if status == "queued":
        return (
            f"Detected a directed {kind} command; it could not run immediately "
            f"({route.get('reason') or 'engine/budget'}), so a pre-approved delegation brief waits in the Inbox."
        )
    if status == "no_task":
        return f"Detected a {kind} command but no target in the thread; asked the founder to scope it."
    return f"Detected a {kind} command but failed to queue the delegation; surfaced the failure honestly."


def _build_route_summary(route: dict[str, Any] | None) -> dict[str, Any] | None:
    if not route:
        return None
    summary: dict[str, Any] = {
        "status": route.get("status"),
        "kind": route.get("kind", "build"),
        "task": route.get("task"),
        "anaphoric": route.get("anaphoric", False),
        "workspace": route.get("workspace", ""),
    }
    if route.get("status") in {"queued", "executed", "verify_failed", "needs_decision", "run_failed"}:
        summary |= {
            "item_id": route.get("item_id"),
            "queue_status": route.get("queue_status"),
            "agent_configured": route.get("agent_configured", False),
        }
        if route.get("status") == "queued":
            summary["reason"] = route.get("reason", "")
        if "dispatch" in route:
            summary["dispatch"] = route.get("dispatch")
        if route.get("status") == "needs_decision":
            summary |= {
                "question": route.get("question"),
                "decision_item_id": route.get("decision_item_id"),
            }
        if route.get("status") == "run_failed":
            summary["error"] = route.get("error", "")
    elif route.get("status") == "error":
        summary["error"] = route.get("error", "")
    return summary


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
        "auto-buy",
        "auto buy",
        "auto-sell",
        "auto sell",
        "signal scoring",
        "scoring algorithm",
        "hard block",
        "hard_block",
        # Claim/challenge vocabulary: a dispute about what the bot can do is still
        # about the bot, even when the rebuttal drops the "trading" noun.
        "observe-only",
        "observe only",
        "order authority",
        "broker",
        "place order",
        # Trade-data vocabulary: a PnL/fills question is a trading-bot question even
        # with no "bot" noun, so the live snapshot gets loaded instead of Zade
        # answering blind. ("equity" is deliberately excluded -- it collides with
        # cap-table/ownership talk; rely on stickiness for that mid-thread.)
        "pnl",
        "p&l",
        "p and l",
        "profit and loss",
        "fills",
    )
    return any(signal in lowered for signal in signals)


def _mentions_trading_bot_changes(message: str) -> bool:
    """True when the founder is asking what changed in the bot (code/config edits,
    commits, recent modifications) — the ask behind "I made a few modifications
    yesterday, can you see what has changed?"."""
    lowered = (message or "").lower()
    signals = (
        "what changed",
        "what has changed",
        "what's changed",
        "what did i change",
        "see what has changed",
        "recent changes",
        "changes i made",
        "change i made",
        "modif",  # modified / modification(s) / modifying
        "committed",
        "commit",
        "diff",
        "updated the",
        "update i made",
        "updates i made",
        "since yesterday",
        "what's different",
        "what is different",
    )
    return any(signal in lowered for signal in signals)


def _mentions_trading_signal_analysis(message: str) -> bool:
    lowered = (message or "").lower()
    signals = (
        "auto-buy",
        "auto buy",
        "auto-sell",
        "auto sell",
        "signal scoring",
        "scoring algorithm",
        "score weights",
        "volatility weighting",
        "rejected trades",
        "rejected auto",
        "hard_block",
        "hard block",
        "portfolio_full",
        "cooldown",
        "recent signals",
    )
    return any(signal in lowered for signal in signals)


def _render_live_trading_data(activity: dict[str, Any]) -> list[str]:
    """Render the live trade/equity/signal snapshot as prompt lines. When the
    snapshot is missing or failed, emit an explicit 'unavailable -- do not
    fabricate' line instead of silently omitting it, so the model never fills the
    gap with invented numbers."""
    if not activity:
        return [
            "- LIVE TRADING DATA: not loaded this turn. If asked for numbers, say you don't have them "
            "loaded; do not fabricate trades, prices, or P&L."
        ]
    if not activity.get("ok"):
        errs = "; ".join(str(e) for e in (activity.get("errors") or [])[:2]) or "bridge read failed"
        return [
            f"- LIVE TRADING DATA: unavailable this turn ({errs}). Tell the founder you could not load "
            "the live numbers; do not fabricate trades, prices, or P&L."
        ]
    out: list[str] = []
    tr = activity.get("trades") or {}
    eq = activity.get("equity") or {}
    if tr:
        out.append(
            f"- LIVE TRADING DATA -- today: {tr.get('today_total', '?')} trades "
            f"({tr.get('buys', '?')} buy / {tr.get('sells', '?')} sell) across {tr.get('symbols', '?')} symbols."
        )
    if eq:
        out.append(
            f"- Account equity: ${eq.get('latest_equity', '?')} (session {eq.get('session_date', '?')}); "
            f"intraday change ${eq.get('intraday_change', '?')}; vs prior close ${eq.get('change_vs_prior_close', '?')}. "
            "This equity delta IS the P&L figure -- there is no separate stored P&L column."
        )
    fills = tr.get("recent_fills") or []
    if fills:
        rendered = "; ".join(
            f"{f.get('symbol')} {f.get('action')} {f.get('qty')}@{f.get('fill_price')} ({f.get('order_status')})"
            for f in fills[:8]
        )
        out.append(f"- Recent fills (newest first): {rendered}")
    sigs = activity.get("signals") or []
    if sigs:
        rendered = "; ".join(
            f"{s.get('symbol')} {s.get('decision')} score={s.get('score')}" for s in sigs[:5]
        )
        out.append(f"- Latest auto-buy candidates/signals: {rendered}")
    if not out:
        out.append(
            "- LIVE TRADING DATA: loaded but empty (no trades/equity/signals returned). Say so; do not fabricate."
        )
    return out


def _brief_trading_bot_context(context: dict[str, Any]) -> str:
    if not context or not context.get("present"):
        return "No trading-bot context requested."
    if context.get("error"):
        return f"Trading-bot context requested, but status check failed: {context['error']}"
    status = context.get("status") or {}
    interpretation = context.get("interpretation") or {}
    activity = context.get("activity") or {}
    replacement = status.get("deep_thought_replacement") or {}
    boundary = status.get("authority_boundary") or {}
    # Lead with the ground truth and the whose-limits disambiguation, ABOVE the
    # status lines. The status stamps every field "..._no_broker_order_authority" /
    # "..._no_trade_authority"; read first, those labels make the model conclude the
    # BOT is observe-only. They are Zade's ceiling, not the bot's -- say so up front.
    lines = [
        "- GROUND TRUTH (read before answering): the trading bot HAS full broker and order "
        "authority. It places and fills its OWN orders via its Alpaca/Binance gateways (see "
        "trades.db: real buy/sell rows, order_status=filled). It is NOT observe-only. "
        "Observe-vs-live is the bot's own config toggle (currently paper), not an absence of authority.",
        "- WHOSE LIMITS ARE WHOSE: YOU (Zade) are read-and-advise-only over the bot -- no order "
        "authority of your OWN. Every 'no_broker_order_authority' / 'no_trade_authority' / "
        "'*_mutation=false' label below describes ZADE'S ceiling, NOT the bot's capability. Do not "
        "restate your own leash as a fact about the bot, and do not call the bot observe-only.",
    ]
    if interpretation.get("answering_rule"):
        lines.append(f"- ANSWERING RULE: {interpretation['answering_rule']}")
    lines.append(
        "- DATA DISCIPLINE: the LIVE TRADING DATA below is the only source for numbers this turn. "
        "Answer PnL, trade, position, and signal questions from it verbatim. If the founder asks for "
        "a figure it does not contain, say plainly you don't have it loaded and name the read to run. "
        "NEVER invent a symbol, side, quantity, price, count, or P&L. NEVER say you are 'pulling', "
        "'fetching', or to 'check the bridge' -- this turn already contains everything the bridge "
        "returned; there is no further fetch you can perform or narrate."
    )
    lines.extend(_render_live_trading_data(activity))
    lines += [
        f"- Bridge status: {'ok' if status.get('ok') else 'not ok'}; enabled={status.get('enabled')}; runtime_effect={status.get('runtime_effect', 'unknown')}",
        f"- WSL repo: {status.get('wsl_distro', 'unknown')}:{status.get('repo_path', 'unknown')}; reachable={status.get('repo_reachable')}; advisory_lane_present={status.get('advisory_lane_present')}",
        f"- Replacement seams: active={replacement.get('active_count', 0)}; planned={replacement.get('planned_count', 0)}",
        f"- Zade's authority boundary (NOT the bot's): writes={boundary.get('writes', 'unknown')}; "
        f"runtime_read_path={boundary.get('runtime_read_path')}; "
        f"zade_bridge_broker_order_mutation={boundary.get('broker_order_sizing_gate_mutation')}",
    ]
    git_probe = status.get("git") or {}
    git_stdout = str(git_probe.get("stdout") or "").strip()
    if git_stdout:
        lines.append("- Repo git probe (branch/status + last commit, read live this turn):")
        lines.extend(f"    {probe_line}" for probe_line in git_stdout.splitlines()[:10])
    lines.extend(_brief_recent_signal_context(context.get("recent_signals") or {}))
    lines.extend(_brief_recent_changes_context(context.get("recent_changes") or {}))
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


def _brief_recent_changes_context(changes: dict[str, Any]) -> list[str]:
    if not changes:
        return []
    if changes.get("error"):
        return [f"- Recent-changes check failed: {changes['error']}"]
    if not changes.get("enabled", True):
        return ["- Recent-changes check unavailable: trading-bot bridge disabled."]
    window = changes.get("window_hours", 48)
    commits = changes.get("commits") or {}
    working_tree = changes.get("working_tree") or {}
    commits_out = str(commits.get("stdout") or "").strip()
    tree_out = str(working_tree.get("stdout") or "").strip()
    lines = [
        f"- REPO CHANGE EVIDENCE (git read of the bot repo, last {window}h, completed live this turn — "
        "this IS the check; report what it shows, never promise to look):"
    ]
    if commits_out:
        lines.append(f"- Commits in the last {window}h:")
        lines.extend(f"    {commit_line}" for commit_line in commits_out.splitlines()[:40])
    elif commits.get("ok"):
        lines.append(f"- Commits in the last {window}h: none.")
    else:
        lines.append(f"- Commit read failed: {str(commits.get('stderr') or '').strip() or 'unknown git error'}")
    if tree_out:
        lines.append("- Uncommitted working-tree state (git status/diff --stat):")
        lines.extend(f"    {tree_line}" for tree_line in tree_out.splitlines()[:40])
    elif working_tree.get("ok"):
        lines.append("- Uncommitted working-tree state: clean.")
    else:
        lines.append(
            f"- Working-tree read failed: {str(working_tree.get('stderr') or '').strip() or 'unknown git error'}"
        )
    lines.append(
        "- CHANGE ANSWERING RULE: name the specific commits/files above when describing what changed. "
        "If both sections are empty, say plainly that the repo shows no commits in the window and a clean "
        "working tree — do not invent changes and do not say you will check later."
    )
    return lines


def _brief_recent_signal_context(signals: dict[str, Any]) -> list[str]:
    if not signals:
        return []
    if signals.get("error"):
        return [f"- Recent signal check failed: {signals['error']}"]
    tables = signals.get("tables") or {}
    rows = _signal_rows(tables, "auto_buy_candidates") or _signal_rows(tables, "auto_buy_decision_snapshots")
    if not rows:
        return ["- Recent signal check: no auto-buy rows returned in the scoped snapshot."]

    hard_blocks: dict[str, int] = {}
    decisions: dict[str, int] = {}
    samples = []
    for row in rows[:8]:
        decision = str(row.get("decision") or "unknown")
        decisions[decision] = decisions.get(decision, 0) + 1
        hard_block = str(row.get("hard_block_reason") or "").strip()
        if hard_block:
            hard_blocks[hard_block] = hard_blocks.get(hard_block, 0) + 1
        symbol = str(row.get("symbol") or "?")
        score = row.get("score")
        reason = _short_text(str(row.get("reason") or ""), 90)
        samples.append(
            f"{symbol} {decision} score={score if score is not None else 'n/a'} "
            f"hard_block={hard_block or 'none'}"
            + (f" reason={reason}" if reason else "")
        )

    lines = [
        "- Recent signal evidence: live read from /trading-bot/signals/recent for this turn.",
        "- Recent auto-buy decisions: " + _format_counts(decisions),
    ]
    if hard_blocks:
        lines.append("- Recent auto-buy hard blocks: " + _format_counts(hard_blocks))
    lines.append("- Recent auto-buy sample: " + "; ".join(samples[:5]))
    lines.append(
        "- SIGNAL DIAGNOSIS RULE: decision, reason, and hard_block_reason are causal evidence; "
        "score values alone do not justify changing the scoring algorithm. Recommend weight changes "
        "only after realized/counterfactual outcome evidence shows score calibration error."
    )
    return lines


def _signal_rows(tables: dict[str, Any], table_name: str) -> list[dict[str, Any]]:
    table = tables.get(table_name) or {}
    rows = table.get("rows") or []
    return [row for row in rows if isinstance(row, dict)]


def _format_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "none"
    return ", ".join(f"{key}={value}" for key, value in sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _short_text(text: str, limit: int) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 3)].rstrip() + "..."


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
