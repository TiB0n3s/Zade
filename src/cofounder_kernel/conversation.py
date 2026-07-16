from __future__ import annotations

import json
from typing import Any

from .config import KernelConfig
from .db import KernelDatabase, utc_now
from .ollama import OllamaClient
from .prompts import ModelMessage


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

    # Distillation: promote durable knowledge from chat into searchable memory.
    DISTILL_MIN_TURNS = 8          # auto path waits for this many aged-out turns
    DISTILL_MAX_ITEMS = 8          # cap memories written per distillation pass
    DISTILL_TURN_CAP = 40          # cap turns fed to one extraction call
    DISTILL_KINDS = ("decision", "commitment", "preference", "fact", "lesson")

    def __init__(self, *, config: KernelConfig, db: KernelDatabase, ollama: OllamaClient, ingestion: Any | None = None):
        self.config = config
        self.db = db
        self.ollama = ollama
        # Governed memory write path (secret filter + semantic dedupe + embedding).
        # When present, distillation writes through it instead of db.add_memory.
        self.ingestion = ingestion

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

    def end_session(self, conversation_id: int) -> dict[str, Any]:
        """Close a thread (Tier 8): run a final distillation of everything not yet
        promoted, then mark it 'ended' so a later boot starts a fresh session
        instead of piling onto it. Best-effort on the distill; idempotent."""
        conversation = self.db.get_conversation(conversation_id)
        if not conversation:
            raise ValueError(f"Conversation not found: {conversation_id}")
        try:
            distilled = self.distill(conversation_id, min_turns=1, only_aged_out=False)
        except Exception:
            distilled = None
        self.db.update_conversation_status(conversation_id, status="ended")
        self.db.audit(
            actor="conversation",
            action="conversation.end",
            target=f"conversation:{conversation_id}",
            permission_tier="L1_MEMORY_WRITE",
            status="ok",
            details={"distilled": distilled or {"status": "nothing_to_distill"}},
        )
        return {"ended": conversation_id, "distilled": distilled or {"status": "nothing_to_distill", "count": 0}}

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
        empty = {
            "block": "No prior conversation in this thread.",
            "system_block": "No prior conversation in this thread.",
            "messages": [],
            "state": {"conversation_id": None},
            "conversation": None,
        }
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
            f"Conversation continuity (id {conversation['id']}, {conversation['turn_count']} prior turn(s)):",
            f"Earlier summary: {summary or 'No earlier summary yet.'}",
        ]
        legacy_lines = [
            f'Conversation continuity (id {conversation["id"]} — "{conversation["title"] or "untitled"}", '
            f"{conversation['turn_count']} prior turn(s)):",
            f"Earlier summary: {summary or 'No earlier summary yet.'}",
        ]
        if recent:
            lines.append("Recent exchange is supplied as structured provider messages below.")
            legacy_lines.append("Recent exchange:")
            for turn in recent:
                speaker = "Founder" if turn["role"] == "user" else assistant_name
                text = _truncate(turn["content"], self.TURN_PROMPT_CHARS)
                legacy_lines.append(f"[{speaker}] {text}")
        else:
            lines.append("No recorded turns yet.")
            legacy_lines.append("No recorded turns yet.")
        return {
            "block": "\n".join(legacy_lines),
            "system_block": "\n".join(lines),
            "messages": _turn_messages(recent, self.TURN_PROMPT_CHARS),
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

    def maybe_distill(
        self,
        conversation_id: int,
        *,
        window: int | None = None,
        min_turns: int | None = None,
    ) -> dict[str, Any] | None:
        """Promote turns that have aged out of the recent window into memory.

        Mirrors ``maybe_summarize``: a no-op until enough un-distilled older turns
        exist, so short threads never pay for extraction. Each turn is promoted at
        most once (tracked by ``distilled_through_turn_id``).
        """
        return self.distill(
            conversation_id,
            window=window,
            min_turns=min_turns or self.DISTILL_MIN_TURNS,
            only_aged_out=True,
        )

    def distill(
        self,
        conversation_id: int,
        *,
        window: int | None = None,
        min_turns: int = 1,
        only_aged_out: bool = False,
    ) -> dict[str, Any] | None:
        """Extract durable knowledge (decisions, commitments, preferences, facts,
        lessons) from not-yet-distilled turns and write it into searchable memory.

        Idempotent: advances the distillation cursor only when extraction
        succeeds, and never re-promotes a turn. On extraction failure the cursor is
        left untouched so a later call retries. Returns None when there is nothing
        to do.
        """
        window = window or self.RECENT_WINDOW
        conversation = self.db.get_conversation(conversation_id)
        if not conversation:
            raise ValueError(f"Conversation not found: {conversation_id}")
        turns = self.db.list_conversation_turns(conversation_id, limit=100_000)
        through = conversation.get("distilled_through_turn_id")
        candidates = [turn for turn in turns if through is None or turn["id"] > through]
        if only_aged_out:
            aged_ids = {turn["id"] for turn in turns[:-window]} if len(turns) > window else set()
            candidates = [turn for turn in candidates if turn["id"] in aged_ids]
        if len(candidates) < max(1, min_turns):
            return None
        # Bound how many turns feed one extraction call; oldest-first order preserved.
        batch = candidates[: self.DISTILL_TURN_CAP]
        items = self._extract_durable_items(batch, conversation.get("summary", ""))
        if items is None:
            # Extraction failed (model or parse error). Leave the cursor for a retry.
            return {"status": "extraction_failed", "written": [], "count": 0, "candidates": len(batch)}
        source = f"conversation:{conversation_id}"
        existing_titles = {record.title.strip().lower() for record in self.db.list_memories_by_source(source)}
        written: list[dict[str, Any]] = []
        skipped_duplicate = 0
        turn_range = [batch[0]["id"], batch[-1]["id"]]
        for item in items:
            title = item["title"]
            if title.strip().lower() in existing_titles:
                continue
            meta = {
                "conversation_id": conversation_id,
                "category": item["kind"],
                "distilled_from_turns": turn_range,
                "distilled_at": utc_now(),
            }
            if self.ingestion is not None:
                # Governed write: semantic dedupe + secret filter + embedding.
                result = self.ingestion.save_memory(
                    kind=f"chat_{item['kind']}",
                    title=title,
                    content=item["content"],
                    source=source,
                    metadata=meta,
                    dedupe=True,
                )
                if result.get("status") != "written":
                    if result.get("status") == "duplicate":
                        skipped_duplicate += 1
                    continue
                memory_id = result["memory_id"]
            else:
                memory_id = self.db.add_memory(
                    kind=f"chat_{item['kind']}", title=title, content=item["content"], source=source, metadata=meta
                )
            existing_titles.add(title.strip().lower())
            written.append({"memory_id": memory_id, "kind": f"chat_{item['kind']}", "title": title})
        last_turn_id = batch[-1]["id"]
        self.db.update_conversation_distilled(conversation_id, distilled_through_turn_id=last_turn_id)
        self.db.audit(
            actor="conversation",
            action="conversation.distill",
            target=f"conversation:{conversation_id}",
            permission_tier="L1_MEMORY_WRITE",
            status="ok",
            details={"written": len(written), "candidates": len(batch), "distilled_through_turn_id": last_turn_id},
        )
        return {
            "status": "ok",
            "written": written,
            "count": len(written),
            "skipped_duplicate": skipped_duplicate,
            "candidates": len(batch),
            "distilled_through_turn_id": last_turn_id,
        }

    def _extract_durable_items(
        self, turns: list[dict[str, Any]], existing_summary: str
    ) -> list[dict[str, str]] | None:
        """Return durable {kind, title, content} items, or None if the model/parse
        failed (so the caller can retry later without losing turns)."""
        assistant_name = self.config.identity.name
        transcript_lines = []
        for turn in turns:
            speaker = "Founder" if turn["role"] == "user" else assistant_name
            transcript_lines.append(f"[{speaker}] {_truncate(turn['content'], self.TURN_PROMPT_CHARS)}")
        transcript = "\n".join(transcript_lines)
        kinds = ", ".join(self.DISTILL_KINDS)
        prompt = (
            f"You extract durable, reusable knowledge from a founder's conversation with {assistant_name}, "
            "so it can be recalled later in unrelated conversations.\n"
            "Return ONLY a JSON array (no prose, no code fences). Each element is an object: "
            '{"kind": <one of [' + kinds + ']>, "title": <short label>, "content": <one or two factual sentences>}.\n'
            "kind meanings — decision: a choice that was made; commitment: something someone said they will do; "
            "preference: a stated like/dislike or working style; fact: a durable fact about the business, product, "
            "market, or people; lesson: an insight or principle learned.\n"
            "Only include things still worth remembering weeks from now. Skip greetings, small talk, transient "
            "status, and anything already covered by the existing summary. Never extract secrets, credentials, API "
            "keys, tokens, or Ellie's employer's client or network specifics. Do not invent anything not supported by "
            "the transcript. If nothing durable was said, return [].\n\n"
            f"Existing long-term summary (do not repeat):\n{existing_summary or 'None yet.'}\n\n"
            f"Transcript:\n{transcript}\n\n"
            "JSON array:"
        )
        try:
            generated = self.ollama.generate(
                prompt=prompt,
                model=self.config.ollama.chat_model,
                think=False,
                temperature=self.config.ollama.temperature,
                num_predict=800,
            )
        except Exception:
            return None
        return _parse_distilled_items(generated.response, self.DISTILL_KINDS, self.DISTILL_MAX_ITEMS)


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _turn_messages(turns: list[dict[str, Any]], limit: int) -> list[ModelMessage]:
    messages: list[ModelMessage] = []
    for turn in turns:
        role = str(turn.get("role") or "").strip()
        content = _truncate(str(turn.get("content", "")), limit)
        if role == "assistant":
            messages.append(ModelMessage(role="assistant", content=content))
        elif role == "tool":
            messages.append(ModelMessage(role="tool", content=content))
        else:
            messages.append(ModelMessage(role="user", content=content))
    return messages


def _fallback_summary(existing_summary: str, turns: list[dict[str, Any]], assistant_name: str) -> str:
    """Deterministic summary used when the local model is unavailable."""
    parts = []
    if existing_summary.strip():
        parts.append(existing_summary.strip())
    for turn in turns:
        speaker = "Founder" if turn["role"] == "user" else assistant_name
        parts.append(f"{speaker}: {_truncate(turn['content'], 160)}")
    return " | ".join(parts)


def _parse_distilled_items(
    text: str, allowed_kinds: tuple[str, ...], cap: int
) -> list[dict[str, str]] | None:
    """Pull a JSON array of durable items out of a model response.

    Returns ``[]`` for a valid-but-empty array (nothing durable — success), and
    ``None`` when no parseable array is present (a failure the caller should retry).
    Tolerant of surrounding prose/code fences by slicing the outermost brackets.
    """
    if not text:
        return None
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    items: list[dict[str, str]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind", "")).strip().lower()
        title = str(entry.get("title", "")).strip()
        content = str(entry.get("content", "")).strip()
        if kind not in allowed_kinds or not title or not content:
            continue
        items.append({"kind": kind, "title": title, "content": content})
        if len(items) >= cap:
            break
    return items
