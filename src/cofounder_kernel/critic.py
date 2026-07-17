from __future__ import annotations

import json
import time
from typing import Any

from .config import KernelConfig
from .db import KernelDatabase
from .founder import FounderService
from .ollama import OllamaClient


VERDICTS = {"proceed", "proceed_with_changes", "do_not_proceed"}

RECOMMENDATION_SIGNALS = (
    "should we",
    "should i",
    "should zade",
    "recommend",
    "recommendation",
    "decide",
    "decision",
    "choose",
    "which option",
    "which path",
    "prioritize",
    "prioritise",
    "next move",
    "next step",
    "next action",
    "bet on",
    "worth it",
    "worth doing",
    "tradeoff",
    "trade-off",
    "go with",
    "pick between",
)

MAX_MESSAGE_CHARS = 2000
MAX_DRAFT_CHARS = 4000
MAX_FIELD_CHARS = 500

# Server-enforced shape of the critique (Ollama `format`). Mirrors the JSON
# contract in the attack prompt; _parse_critique stays as the backstop for
# structured_output = false and transport errors.
CRITIQUE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": sorted(VERDICTS)},
        "weakest_assumption": {"type": "string"},
        "missing_evidence": {"type": "string"},
        "downside_risk": {"type": "string"},
        "confidence_adjustment": {"type": "integer", "minimum": -50, "maximum": 0},
    },
    "required": ["verdict", "weakest_assumption", "missing_evidence", "downside_risk", "confidence_adjustment"],
}


class ContrarianCritic:
    """Automatic red-team pass for recommendation-shaped governed responses.

    The critic attacks the draft with the reasoning model before the founder
    sees it. It is non-blocking pushback: the challenge is attached visibly to
    the response, never silently rewritten into it. Every pass is persisted as
    a contrarian review so scrutiny lands in the founder operating layer.
    """

    def __init__(
        self,
        *,
        config: KernelConfig,
        db: KernelDatabase,
        ollama: OllamaClient,
        founder: FounderService,
    ):
        self.config = config
        self.db = db
        self.ollama = ollama
        self.founder = founder

    def should_challenge(self, *, message: str, requested: bool | None) -> bool:
        if requested is not None:
            return requested
        lowered = message.lower()
        return any(signal in lowered for signal in RECOMMENDATION_SIGNALS)

    def challenge(self, *, message: str, draft_response: str, context: dict[str, Any]) -> dict[str, Any]:
        model = self.config.ollama.reasoning_model
        think = self.config.ollama.think_for_role("reasoning")
        prompt = self._build_attack_prompt(message=message, draft_response=draft_response, context=context)
        started = time.perf_counter()
        try:
            generated = self.ollama.generate(
                prompt=prompt,
                model=model,
                think=think,
                temperature=self.config.ollama.temperature,
                format=CRITIQUE_SCHEMA,
            )
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            call_id = self.db.record_model_call(
                operation="runtime.contrarian",
                model=model,
                role="reasoning",
                status="error",
                latency_ms=latency_ms,
                prompt_chars=len(prompt),
                response_chars=0,
                think=think,
                error=str(exc),
            )
            return {
                "status": "error",
                "error": str(exc),
                "verdict": "",
                "model": model,
                "model_call_id": call_id,
                "critique_block": "",
            }
        latency_ms = int((time.perf_counter() - started) * 1000)
        critique = _parse_critique(generated.response)
        call_id = self.db.record_model_call(
            operation="runtime.contrarian",
            model=generated.model,
            role="reasoning",
            status="ok",
            latency_ms=latency_ms,
            prompt_chars=len(prompt),
            response_chars=len(generated.response),
            think=think,
            metadata={"verdict": critique["verdict"]},
        )
        return {
            "status": "ok",
            "model": generated.model,
            "model_call_id": call_id,
            "critique_block": _critique_block(critique),
            **critique,
        }

    def persist_review(
        self,
        critique: dict[str, Any],
        *,
        message: str,
        runtime_event_id: int,
    ) -> int | None:
        if critique.get("status") != "ok":
            return None
        top_risks = [item for item in [critique.get("downside_risk", "")] if item]
        blind_spots = [item for item in [critique.get("missing_evidence", "")] if item]
        review = self.founder.create_contrarian_review(
            {
                "subject_type": "runtime_event",
                "subject_id": runtime_event_id,
                "title": f"Auto contrarian pass: {_truncate(message, 160)}",
                "context": critique.get("weakest_assumption", "") or critique.get("critique_text", ""),
                "top_risks": top_risks,
                "blind_spots": blind_spots,
                "confidence_adjustment": int(critique.get("confidence_adjustment", 0)),
                "recommendation": critique["verdict"] if critique["verdict"] in VERDICTS else "proceed_with_changes",
                "metadata": {
                    "source": "runtime.contrarian",
                    "runtime_event_id": runtime_event_id,
                    "model_call_id": critique.get("model_call_id"),
                    "verdict": critique.get("verdict", ""),
                    "auto": True,
                },
            }
        )
        return review.id

    def _build_attack_prompt(self, *, message: str, draft_response: str, context: dict[str, Any]) -> str:
        dashboard = context.get("founder_dashboard", {})
        evidence_state = context.get("evidence_state", {})
        return f"""You are the contrarian reviewer inside {self.config.identity.name}'s governed runtime.
A draft recommendation is about to reach the founder. Your job is to attack it first.
Apply these lenses: red team (what assumptions break this?), skeptic (what evidence is missing?), economist (what is the opportunity cost?).
Attack the reasoning, not the style. Do not invent facts. If the draft is genuinely sound, say proceed.

Founder asked:
{_truncate(message, MAX_MESSAGE_CHARS)}

Draft response under review:
{_truncate(draft_response, MAX_DRAFT_CHARS)}

Operating context:
- Company health: {dashboard.get("company_health", "unknown")}
- One thing that matters most: {dashboard.get("one_thing_that_matters_most_today", "unknown")}
- Local evidence present for this request: {evidence_state.get("local_evidence_present", False)}

Return ONLY a JSON object, no other text:
{{"verdict": "proceed" | "proceed_with_changes" | "do_not_proceed", "weakest_assumption": "...", "missing_evidence": "...", "downside_risk": "...", "confidence_adjustment": <integer between -50 and 0>}}
"""


def _parse_critique(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            verdict = str(data.get("verdict", "")).strip().lower().replace(" ", "_")
            if verdict not in VERDICTS:
                verdict = "proceed_with_changes"
            return {
                "verdict": verdict,
                "weakest_assumption": _truncate(str(data.get("weakest_assumption", "")), MAX_FIELD_CHARS),
                "missing_evidence": _truncate(str(data.get("missing_evidence", "")), MAX_FIELD_CHARS),
                "downside_risk": _truncate(str(data.get("downside_risk", "")), MAX_FIELD_CHARS),
                "confidence_adjustment": _clamp_adjustment(data.get("confidence_adjustment"), verdict),
                "critique_text": "",
            }
    return {
        "verdict": "unparsed",
        "weakest_assumption": "",
        "missing_evidence": "",
        "downside_risk": "",
        "confidence_adjustment": -10,
        "critique_text": _truncate(text, 800),
    }


def _clamp_adjustment(value: Any, verdict: str) -> int:
    try:
        adjustment = int(value)
    except (TypeError, ValueError):
        adjustment = 0 if verdict == "proceed" else -10
    # The contrarian pass is a red team: it may only lower or hold confidence,
    # never raise it. Clamp to [-50, 0] regardless of what the model returns.
    return max(-50, min(0, adjustment))


def _critique_block(critique: dict[str, Any]) -> str:
    if critique["verdict"] == "unparsed":
        critique_text = str(critique.get("critique_text", "")).strip()
        if not critique_text or _looks_like_malformed_json_fragment(critique_text):
            return ""
    verdict = str(critique["verdict"])
    if verdict == "unparsed":
        verdict = "unstructured response; treat as proceed_with_changes until rerun"
    lines = ["", "---", "Contrarian check (reasoning-model red team):", f"- Verdict: {verdict}"]
    if critique.get("critique_text"):
        lines.append(f"- Critique: {critique['critique_text']}")
    if critique.get("weakest_assumption"):
        lines.append(f"- Weakest assumption: {critique['weakest_assumption']}")
    if critique.get("missing_evidence"):
        lines.append(f"- Missing evidence: {critique['missing_evidence']}")
    if critique.get("downside_risk"):
        lines.append(f"- Downside risk: {critique['downside_risk']}")
    lines.append(f"- Confidence adjustment: {critique['confidence_adjustment']}")
    return "\n".join(lines)


def _looks_like_malformed_json_fragment(text: str) -> bool:
    stripped = text.strip()
    if stripped.startswith("```"):
        return True
    probe = stripped[:240].lower()
    return stripped.startswith("{") or '"verdict"' in probe or '"weakest_assumption"' in probe


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."
