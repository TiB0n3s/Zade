from __future__ import annotations

from typing import Any

from .config import KernelConfig
from .db import KernelDatabase
from .ollama import OllamaClient


class ConversationService:
    """Durable episodic memory for the governed runtime.

    A conversation is an ordered thread of turns. Recent turns are folded into
    the governed prompt verbatim; older turns roll into a compact rolling
    summary so a thread can grow indefinitely without unbounded prompt size.
    """

    RECENT_WINDOW = 12
    SUMMARY_MIN_OVERFLOW = 8
    SUMMARY_MAX_CHARS = 1200
    TURN_PROMPT_CHARS = 700

    def __init__(self, *, config: KernelConfig, db: KernelDatabase, ollama: OllamaClient):
        self.config = config
        self.db = db
        self.ollama = ollama

    def create(self, *, title: str = "", metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        conversation_id = self.db.create_conversation(title=title, metadata=metadata)
        self.db.audit(
            actor="conversation",
            action="conversation.create",
            target=f"conversation:{conversation_id}",
            permission_tier="L1_MEMORY_WRITE",
            status="ok",
            details={"title": title},
        )
        return self.get(conversation_id)

    def get(self, conversation_id: int, *, turn_limit: int = 50) -> dict[str, Any]:
        conversation = self.db.get_conversation(conversation_id)
        if not conversation:
            raise ValueError(f"Conversation not found: {conversation_id}")
        conversation["turns"] = self.db.list_conversation_turns(conversation_id, limit=turn_limit)
        return conversation

    def list(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return self.db.list_conversations(status=status, limit=limit)

    def list_turns(self, conversation_id: int, *, limit: int = 100) -> list[dict[str, Any]]:
        if not self.db.get_conversation(conversation_id):
            raise ValueError(f"Conversation not found: {conversation_id}")
        return self.db.list_conversation_turns(conversation_id, limit=limit)

    def record_user_turn(self, conversation_id: int, *, content: str, task_type: str = "") -> int:
        return self.db.add_conversation_turn(
            conversation_id=conversation_id,
            role="user",
            content=content,
            task_type=task_type,
        )

    def record_assistant_turn(
        self,
        conversation_id: int,
        *,
        content: str,
        task_type: str = "",
        model: str = "",
        authority_decision: str = "",
        runtime_event_id: int | None = None,
    ) -> int:
        return self.db.add_conversation_turn(
            conversation_id=conversation_id,
            role="assistant",
            content=content,
            task_type=task_type,
            model=model,
            authority_decision=authority_decision,
            runtime_event_id=runtime_event_id,
        )

    def prompt_context(self, conversation_id: int | None, *, window: int | None = None) -> dict[str, Any]:
        """Build the conversation-history block injected into the governed prompt.

        Returns an empty block when no conversation is in play, so callers can
        always splice ``result["block"]`` into the prompt unconditionally.
        """
        empty = {"block": "No prior conversation in this thread.", "state": {"conversation_id": None}, "conversation": None}
        if not conversation_id:
            return empty
        conversation = self.db.get_conversation(conversation_id)
        if not conversation:
            raise ValueError(f"Conversation not found: {conversation_id}")
        window = window or self.RECENT_WINDOW
        recent = self.db.recent_conversation_turns(conversation_id, window=window)
        summary = conversation["summary"].strip()
        assistant_name = self.config.identity.name
        lines = [
            f'Conversation continuity (id {conversation["id"]} — "{conversation["title"] or "untitled"}", '
            f"{conversation['turn_count']} prior turn(s)):",
            f"Earlier summary: {summary or 'No earlier summary yet.'}",
        ]
        if recent:
            lines.append("Recent exchange:")
            for turn in recent:
                speaker = "Founder" if turn["role"] == "user" else assistant_name
                text = _truncate(turn["content"], self.TURN_PROMPT_CHARS)
                lines.append(f"[{speaker}] {text}")
        else:
            lines.append("No recorded turns yet.")
        return {
            "block": "\n".join(lines),
            "state": {
                "conversation_id": conversation["id"],
                "title": conversation["title"],
                "turn_count": conversation["turn_count"],
                "recent_turns_in_prompt": len(recent),
                "has_summary": bool(summary),
            },
            "conversation": conversation,
        }

    def maybe_summarize(
        self,
        conversation_id: int,
        *,
        window: int | None = None,
        min_overflow: int | None = None,
    ) -> dict[str, Any] | None:
        """Fold turns older than the recent window into the rolling summary.

        No-op until at least ``min_overflow`` un-summarized older turns exist, so
        short threads never pay for summarization.
        """
        window = window or self.RECENT_WINDOW
        min_overflow = min_overflow or self.SUMMARY_MIN_OVERFLOW
        conversation = self.db.get_conversation(conversation_id)
        if not conversation:
            raise ValueError(f"Conversation not found: {conversation_id}")
        turns = self.db.list_conversation_turns(conversation_id, limit=100_000)
        if len(turns) <= window:
            return None
        older = turns[:-window]
        through = conversation["summary_through_turn_id"]
        pending = [turn for turn in older if through is None or turn["id"] > through]
        if len(pending) < min_overflow:
            return None
        new_summary = self._summarize(conversation["summary"], pending)
        last_turn_id = pending[-1]["id"]
        self.db.update_conversation_summary(
            conversation_id,
            summary=new_summary,
            summary_through_turn_id=last_turn_id,
        )
        self.db.audit(
            actor="conversation",
            action="conversation.summarize",
            target=f"conversation:{conversation_id}",
            permission_tier="L1_MEMORY_WRITE",
            status="ok",
            details={"summarized_turns": len(pending), "summary_through_turn_id": last_turn_id},
        )
        return {
            "summary": new_summary,
            "summary_through_turn_id": last_turn_id,
            "summarized_turns": len(pending),
        }

    def _summarize(self, existing_summary: str, turns: list[dict[str, Any]]) -> str:
        assistant_name = self.config.identity.name
        transcript_lines = []
        for turn in turns:
            speaker = "Founder" if turn["role"] == "user" else assistant_name
            transcript_lines.append(f"[{speaker}] {_truncate(turn['content'], self.TURN_PROMPT_CHARS)}")
        transcript = "\n".join(transcript_lines)
        prompt = (
            f"You maintain a rolling memory of a founder's conversation with {assistant_name}. "
            "Update the running summary so future turns keep continuity. "
            "Keep decisions, open questions, commitments, and stated preferences. "
            "Be terse and factual. Do not invent details.\n\n"
            f"Existing summary:\n{existing_summary or 'None yet.'}\n\n"
            f"New turns to fold in:\n{transcript}\n\n"
            "Return only the updated summary."
        )
        try:
            generated = self.ollama.generate(
                prompt=prompt,
                model=self.config.ollama.chat_model,
                think=False,
                temperature=self.config.ollama.temperature,
            )
            summary = generated.response.strip()
        except Exception:
            summary = _fallback_summary(existing_summary, turns, assistant_name)
        if not summary:
            summary = _fallback_summary(existing_summary, turns, assistant_name)
        return _truncate(summary, self.SUMMARY_MAX_CHARS)


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _fallback_summary(existing_summary: str, turns: list[dict[str, Any]], assistant_name: str) -> str:
    """Deterministic summary used when the local model is unavailable."""
    parts = []
    if existing_summary.strip():
        parts.append(existing_summary.strip())
    for turn in turns:
        speaker = "Founder" if turn["role"] == "user" else assistant_name
        parts.append(f"{speaker}: {_truncate(turn['content'], 160)}")
    return " | ".join(parts)
