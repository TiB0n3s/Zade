"""Local specialist role panel — the swarm's local half.

Deep Thought ran 236 specialist agents. Zade does not reproduce that as a native
local swarm (a single GPU serialises the calls and a 14B model caps the quality —
see SWARM-DECISION.md). Instead the swarm is a hybrid: cheap, private, latency-
tolerant roles run LOCALLY here; heavy/frontier work is delegated out (delegation.py).

This module generalises the existing ``ContrarianCritic`` into a ``RolePass``
primitive: a named role (a system brief + a lens) run as one governed pass on a
local model, recorded as model-call telemetry, and returned as a structured
finding. It is fully local — no network, no approval — so it plugs in wherever a
second opinion is cheap and shouldn't leave the machine.

The four shipped roles map to the founder's chosen panel:
  * ``red_team``  — attack a plan/decision/finding; surface the weakest assumption.
  * ``triage``    — classify + prioritise; what matters first and why.
  * ``summarize`` — condense long input to its load-bearing points.
  * ``gap_finder``— where is this thin? (feeds the research daydream).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

# Reasoning models (and sometimes the general model) wrap output in <think> blocks
# whose stray braces wreck naive JSON extraction. Strip them before parsing.
_THINK_RE = re.compile(r"(?is)<think>.*?</think>|<think>.*$")

from .config import KernelConfig
from .db import KernelDatabase
from .ollama import OllamaClient

MAX_INPUT_CHARS = 6000
MAX_FIELD_CHARS = 800
ROLE_PASS_OP = "roles.pass"

# Server-enforced shape of a role finding (Ollama `format`). Mirrors the JSON
# contract in the role prompt; _parse_finding stays as the backstop for
# structured_output = false and transport errors.
FINDING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string"},
        "summary": {"type": "string"},
        "points": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
    },
    "required": ["verdict", "summary", "points"],
}


@dataclass(frozen=True)
class Role:
    key: str
    title: str
    lens: str
    # Which model role to run on: "reasoning" for adversarial/analytic work,
    # "general" for summarising. Keeps the heavy reasoning model off cheap tasks.
    model_role: str = "reasoning"


# All four run on the GENERAL model (qwen3:14b, no thinking): fast, cheap, and
# reliably JSON-formattable. This is the quick-hit local panel — the deep,
# reasoning-model red team already lives in the runtime's ContrarianCritic.
ROLES: dict[str, Role] = {
    "red_team": Role(
        key="red_team",
        title="Red team",
        lens=(
            "Attack the input. What assumption, if false, breaks it? What evidence is missing? "
            "What is the opportunity cost? Attack the reasoning, not the wording. Do not invent facts. "
            "If it is genuinely sound, say so plainly."
        ),
        model_role="general",
    ),
    "triage": Role(
        key="triage",
        title="Triage",
        lens=(
            "Classify and prioritise. What is the single most important item and why? "
            "Rank the rest. Flag anything urgent or blocking. Be decisive, not exhaustive."
        ),
        model_role="general",
    ),
    "summarize": Role(
        key="summarize",
        title="Summarize",
        lens=(
            "Condense to the load-bearing points. Keep what changes a decision; drop the rest. "
            "No preamble, no restating the prompt."
        ),
        model_role="general",
    ),
    "gap_finder": Role(
        key="gap_finder",
        title="Gap finder",
        lens=(
            "Find the holes. Which claims lack support? Which assumptions are unstated? "
            "What would need to be true that hasn't been checked? Phrase each gap as a question."
        ),
        model_role="general",
    ),
}


class RolePassService:
    """Run a named local role over some input and return a structured finding."""

    def __init__(self, *, config: KernelConfig, db: KernelDatabase, ollama: OllamaClient):
        self.config = config
        self.db = db
        self.ollama = ollama

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.config.roles.enabled,
            "roles": [
                {"key": role.key, "title": role.title, "model_role": role.model_role}
                for role in ROLES.values()
            ],
            "operating_rules": [
                "Every role runs one governed pass on a LOCAL model — no network, no approval, no external cost.",
                "A role produces a finding attached to the subject; it never takes an action or grants permission.",
                "Findings are recorded as model-call telemetry so a role's latency/value can be measured.",
            ],
        }

    def list_roles(self) -> list[dict[str, Any]]:
        return [{"key": r.key, "title": r.title, "lens": r.lens, "model_role": r.model_role} for r in ROLES.values()]

    def run(self, *, role: str, content: str, subject: str = "") -> dict[str, Any]:
        if not self.config.roles.enabled:
            raise ValueError("Local roles are disabled (roles.enabled = false).")
        role_key = (role or "").strip().lower()
        spec = ROLES.get(role_key)
        if spec is None:
            raise ValueError(f"Unknown role {role!r}. Available: {', '.join(ROLES)}.")
        content = (content or "").strip()
        if not content:
            raise ValueError("A role pass needs some input to work on.")

        model = self.config.ollama.model_for_role(spec.model_role)
        think = self.config.ollama.think_for_role(spec.model_role)
        prompt = self._build_prompt(spec, content=content, subject=subject)
        started = time.perf_counter()
        try:
            generated = self.ollama.generate(
                prompt=prompt,
                model=model,
                think=think,
                temperature=self.config.ollama.temperature,
                format=FINDING_SCHEMA,
            )
        except Exception as exc:  # noqa: BLE001 - surface the failure as a finding, don't 500
            latency_ms = int((time.perf_counter() - started) * 1000)
            call_id = self.db.record_model_call(
                operation=ROLE_PASS_OP,
                model=model,
                role=spec.model_role,
                status="error",
                latency_ms=latency_ms,
                prompt_chars=len(prompt),
                response_chars=0,
                think=think,
                error=str(exc),
                metadata={"role": role_key},
            )
            return {
                "status": "error",
                "role": role_key,
                "title": spec.title,
                "error": str(exc),
                "model": model,
                "model_call_id": call_id,
            }
        latency_ms = int((time.perf_counter() - started) * 1000)
        finding = _parse_finding(generated.response)
        call_id = self.db.record_model_call(
            operation=ROLE_PASS_OP,
            model=generated.model,
            role=spec.model_role,
            status="ok",
            latency_ms=latency_ms,
            prompt_chars=len(prompt),
            response_chars=len(generated.response),
            think=think,
            metadata={"role": role_key, "verdict": finding.get("verdict", "")},
        )
        return {
            "status": "ok",
            "role": role_key,
            "title": spec.title,
            "model": generated.model,
            "model_call_id": call_id,
            "latency_ms": latency_ms,
            "subject": subject,
            **finding,
        }

    def _build_prompt(self, role: Role, *, content: str, subject: str) -> str:
        subject_line = f"Subject: {subject}\n" if subject else ""
        return f"""You are the "{role.title}" role inside {self.config.identity.name}'s governed runtime.
{role.lens}

{subject_line}Input to work on:
{content[:MAX_INPUT_CHARS]}

Return ONLY a JSON object, no other text:
{{"verdict": "<one short phrase>", "summary": "<2-3 sentences>", "points": ["<point>", "..."]}}
"""


def _parse_finding(text: str) -> dict[str, Any]:
    text = _THINK_RE.sub("", text or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            points = data.get("points") or []
            if not isinstance(points, list):
                points = [str(points)]
            return {
                "verdict": _truncate(str(data.get("verdict", "")), 120),
                "summary": _truncate(str(data.get("summary", "")), MAX_FIELD_CHARS),
                "points": [_truncate(str(p), MAX_FIELD_CHARS) for p in points[:8] if str(p).strip()],
                "raw": "",
            }
    # Unstructured fallback: keep the text so a finding is never lost to a bad parse.
    return {"verdict": "unstructured", "summary": "", "points": [], "raw": _truncate(text, 1200)}


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."
