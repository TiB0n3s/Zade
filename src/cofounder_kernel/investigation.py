"""Agentic investigation loop for chat turns.

This is what makes "can you look at X?" actually look at X. The single-shot
respond() flow can only answer from pre-injected context; when the founder's
ask needs a live read the model didn't get, the model either narrates a check
it cannot perform ("I'll check...") or invents an answer. Here the model is
given a whitelisted, read-only tool belt and a bounded loop: it may call
tools, the kernel executes them (audited, L0_READ), the results go back into
the conversation, and the loop repeats until the model answers or the round
budget runs out — then a final no-tools call forces a plain answer.

Authority posture: every tool is a read. Nothing here can write memory, touch
files, queue work, or reach the network beyond the existing bridge reads. Each
execution is audited individually, and the executed steps are returned so the
UI can show the founder what was actually investigated.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from .config import KernelConfig
from .db import KernelDatabase
from .ollama import GenerateResult, OllamaClient, OllamaError

# Hard bounds so a confused model cannot spin the loop or flood the prompt.
MAX_CALLS_PER_ROUND = 4
MAX_RESULT_CHARS = 6000
_MAX_STRING_CHARS = 1200
_MAX_LIST_ITEMS = 12


@dataclass(frozen=True)
class InvestigationTool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], dict[str, Any]]


class InvestigationService:
    """Read-only tool belt + bounded tool-calling loop for runtime.respond."""

    def __init__(
        self,
        *,
        config: KernelConfig,
        db: KernelDatabase,
        ollama: OllamaClient,
        trading_bot: Any | None = None,
    ):
        self.config = config
        self.db = db
        self.ollama = ollama
        self.trading_bot = trading_bot
        self._tools: dict[str, InvestigationTool] = {}
        self._register_tools()

    # ------------------------------------------------------------- registry

    def available(self) -> bool:
        return bool(self._tools)

    def tool_names(self) -> list[str]:
        return sorted(self._tools)

    def tool_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in sorted(self._tools.values(), key=lambda item: item.name)
        ]

    def prompt_block(self) -> str:
        """System-prompt section telling the model the tools are real and that
        narrating a check instead of calling one is forbidden."""
        lines = [
            "----------  Investigation tools (live, this turn)  ----------",
            "You have callable tools this turn. They run REAL local reads and return real data:",
        ]
        for tool in sorted(self._tools.values(), key=lambda item: item.name):
            lines.append(f"- {tool.name}: {tool.description}")
        lines.append(
            "When the founder asks you to look, check, investigate, verify, or see what "
            "changed/happened — and a tool above covers it — CALL THE TOOL FIRST, then answer "
            "from its results with specifics. Never reply that you WILL check or look: either "
            "call the tool now, or say plainly that no available tool covers the ask and name "
            "what data is missing. Tool results arrive as tool messages; treat them as this "
            "turn's ground truth."
        )
        lines.append(
            "These tools are READ-ONLY: none of them can run shell commands, edit files, "
            "install or update packages, or change anything on the machine. If the founder "
            "asks you to fix, resolve, install, update, or run something, say plainly that "
            "chat cannot execute it and that the real path is a delegated run she approves "
            "in the Inbox. Never present output she pasted (or that you remember) as "
            "something you just fetched, and never narrate an action as performed."
        )
        lines.append(
            "Ground the final answer in this turn's tool results: every number, count, "
            "date, and status fact must appear in a tool result from this turn, and the "
            "answer names which read it came from. If the results come back without the "
            "asked-for fact, say the read did not return it and name exactly what is "
            "missing — do not fill the gap from earlier conversation or general knowledge. "
            "A partial answer grounded in real reads beats a complete-sounding one that "
            "is not."
        )
        return "\n".join(lines)

    def _register_tools(self) -> None:
        bridge = self.trading_bot
        if bridge is not None:
            self._tools["trading_bot_status"] = InvestigationTool(
                name="trading_bot_status",
                description=(
                    "Read the trading-bot bridge status: repo reachability, advisory lane, "
                    "authority boundary, replacement seams, and a short git branch/last-commit probe."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
                handler=lambda args: bridge.status(),
            )
            self._tools["trading_bot_recent_changes"] = InvestigationTool(
                name="trading_bot_recent_changes",
                description=(
                    "Read what changed in the trading-bot repo recently: commits inside the window "
                    "(git log --stat) plus uncommitted working-tree changes. Use for 'what did I "
                    "modify / what changed / recent commits' questions."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "hours": {
                            "type": "integer",
                            "description": "Look-back window in hours (default 48, max 336).",
                        }
                    },
                    "required": [],
                },
                handler=lambda args: bridge.recent_changes(
                    hours=int(args.get("hours") or 48)
                ),
            )
            self._tools["trading_bot_activity"] = InvestigationTool(
                name="trading_bot_activity",
                description=(
                    "Read today's live trading data: trade counts, buys/sells, recent fills, "
                    "account equity + intraday change (the P&L figure), and latest auto-buy "
                    "candidates. Use for PnL / 'what did the bot do today' / position questions."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
                handler=lambda args: bridge.activity_snapshot(),
            )
            self._tools["trading_bot_recent_signals"] = InvestigationTool(
                name="trading_bot_recent_signals",
                description=(
                    "Read recent auto-buy/auto-sell signal decisions with scores, reasons, and "
                    "hard-block causes. Use for signal scoring / rejected trade questions."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "Rows per table (default 8)."},
                        "symbol": {"type": "string", "description": "Optional single ticker filter."},
                    },
                    "required": [],
                },
                handler=lambda args: bridge.recent_signals(
                    limit=int(args.get("limit") or 8),
                    symbol=(str(args["symbol"]).strip() if args.get("symbol") else None),
                ),
            )
            self._tools["trading_bot_recent_events"] = InvestigationTool(
                name="trading_bot_recent_events",
                description=(
                    "Read recent trading-bot runtime events (signals, orders, errors) from the "
                    "bot's event log. Use for 'what happened / any errors' questions."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "Max events (default 20)."},
                        "symbol": {"type": "string", "description": "Optional single ticker filter."},
                    },
                    "required": [],
                },
                handler=lambda args: bridge.recent_events(
                    limit=int(args.get("limit") or 20),
                    symbol=(str(args["symbol"]).strip() if args.get("symbol") else None),
                ),
            )
        self._tools["memory_search"] = InvestigationTool(
            name="memory_search",
            description=(
                "Full-text search Zade's local memory records (decisions, notes, distilled "
                "conversation knowledge). Use when the answer may already be on file."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search terms."},
                    "limit": {"type": "integer", "description": "Max matches (default 8)."},
                },
                "required": ["query"],
            },
            handler=self._memory_search,
        )

    def _memory_search(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        if not query:
            return {"ok": False, "error": "query is required"}
        # include_quarantined=False: this tool's results flow back into Zade's own
        # reasoning loop, so it must honor the grounding quarantine — external-agent
        # memory held out of grounding must not re-enter via Zade's explicit recall.
        records = self.db.search_memories(
            query, max(1, min(25, int(args.get("limit") or 8))), include_quarantined=False
        )
        return {
            "ok": True,
            "matches": [
                {
                    "id": record.id,
                    "kind": record.kind,
                    "title": record.title,
                    "content": record.content,
                }
                for record in records
            ],
        }

    # ------------------------------------------------------------- execution

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            self.db.audit(
                actor="runtime.investigation",
                action="investigation.tool_call",
                target=name,
                permission_tier="L0_READ",
                status="denied",
                details={"reason": "unknown_tool", "arguments": arguments},
            )
            return {"ok": False, "error": f"unknown_tool: {name}", "available_tools": self.tool_names()}
        try:
            data = tool.handler(arguments or {})
            if not isinstance(data, dict):
                data = {"ok": True, "value": data}
            ok = bool(data.get("ok", True))
        except Exception as exc:
            data = {"ok": False, "error": str(exc)}
            ok = False
        self.db.audit(
            actor="runtime.investigation",
            action="investigation.tool_call",
            target=name,
            permission_tier="L0_READ",
            status="ok" if ok else "error",
            details={"arguments": arguments, "ok": ok},
        )
        return data

    # ------------------------------------------------------------- the loop

    def run_loop(
        self,
        *,
        messages: list[Any],
        model: str,
        think: bool,
        temperature: float,
        num_predict: int = 512,
    ) -> tuple[GenerateResult, dict[str, Any]]:
        """Run the bounded tool-calling loop.

        Returns (final GenerateResult, investigation summary). The summary's
        `steps` list is what the UI renders as the real "how I got here".
        """
        # Keep the caller's message objects untouched (the Ollama client accepts
        # both dataclasses and dicts); only the appended tool-round messages are
        # dicts, because they need tool_calls/tool_name fields.
        convo: list[Any] = list(messages)
        schemas = self.tool_schemas()
        max_rounds = max(1, int(getattr(self.config.ollama, "tool_loop_max_rounds", 3)))
        steps: list[dict[str, Any]] = []
        rounds = 0
        fallback: str | None = None
        generated: GenerateResult | None = None

        for round_index in range(max_rounds + 1):
            # The final pass runs without tools so the model must answer in prose
            # instead of requesting yet another read.
            allow_tools = round_index < max_rounds
            try:
                generated = self.ollama.chat(
                    messages=convo,
                    model=model,
                    think=think,
                    temperature=temperature,
                    num_predict=num_predict,
                    tools=schemas if allow_tools else None,
                )
            except OllamaError as exc:
                if round_index == 0 and _tools_unsupported(exc):
                    fallback = f"model_tools_unsupported: {str(exc)[:200]}"
                    generated = self.ollama.chat(
                        messages=convo,
                        model=model,
                        think=think,
                        temperature=temperature,
                        num_predict=num_predict,
                    )
                    break
                raise
            tool_calls = _extract_tool_calls(generated.raw) if allow_tools else []
            if not tool_calls:
                break
            rounds += 1
            convo.append(
                {
                    "role": "assistant",
                    "content": generated.response or "",
                    "tool_calls": tool_calls,
                }
            )
            for call in tool_calls[:MAX_CALLS_PER_ROUND]:
                name, arguments = _parse_tool_call(call)
                started = time.perf_counter()
                result = self.execute(name, arguments)
                latency_ms = int((time.perf_counter() - started) * 1000)
                steps.append(
                    {
                        "tool": name,
                        "arguments": arguments,
                        "ok": bool(result.get("ok", True)) and "error" not in result,
                        "latency_ms": latency_ms,
                        "round": rounds,
                    }
                )
                convo.append(
                    {
                        "role": "tool",
                        "tool_name": name,
                        "content": _render_tool_result(result),
                    }
                )
            dropped = len(tool_calls) - min(len(tool_calls), MAX_CALLS_PER_ROUND)
            if dropped > 0:
                convo.append(
                    {
                        "role": "tool",
                        "tool_name": "kernel",
                        "content": json.dumps(
                            {"note": f"{dropped} additional tool call(s) skipped: per-round cap is {MAX_CALLS_PER_ROUND}."}
                        ),
                    }
                )

        assert generated is not None  # loop always executes at least once
        summary = {
            "enabled": True,
            "rounds": rounds,
            "steps": steps,
            "fallback": fallback,
            "tools_offered": self.tool_names(),
        }
        return generated, summary


# ------------------------------------------------------------------ helpers


def _tools_unsupported(exc: OllamaError) -> bool:
    return "does not support tools" in str(exc).lower()


def _extract_tool_calls(raw: dict[str, Any]) -> list[dict[str, Any]]:
    message = raw.get("message") or {}
    if not isinstance(message, dict):
        return []
    calls = message.get("tool_calls") or []
    return [call for call in calls if isinstance(call, dict)]


def _parse_tool_call(call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    function = call.get("function") or {}
    if not isinstance(function, dict):
        return "", {}
    name = str(function.get("name") or "").strip()
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    return name, arguments


def _render_tool_result(result: dict[str, Any]) -> str:
    rendered = json.dumps(_shrink(result), default=str)
    if len(rendered) <= MAX_RESULT_CHARS:
        return rendered
    return rendered[:MAX_RESULT_CHARS] + '... [truncated]'


def _shrink(value: Any, depth: int = 0) -> Any:
    """Bound strings and lists recursively so one verbose bridge payload cannot
    blow up the model's context."""
    if depth > 6:
        return "..."
    if isinstance(value, str):
        return value if len(value) <= _MAX_STRING_CHARS else value[:_MAX_STRING_CHARS] + "...[truncated]"
    if isinstance(value, dict):
        return {str(key): _shrink(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        items = [_shrink(item, depth + 1) for item in list(value)[:_MAX_LIST_ITEMS]]
        if len(value) > _MAX_LIST_ITEMS:
            items.append(f"...[{len(value) - _MAX_LIST_ITEMS} more]")
        return items
    return value
