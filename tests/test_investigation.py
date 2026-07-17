"""Tests for the agentic investigation loop (investigation.py).

The loop is what turns "can you look at X?" into executed reads. These tests
fake the Ollama chat layer to emit tool_calls and assert the kernel actually
executes the whitelisted tools, feeds results back, bounds the rounds, and
reports real steps in the response payload.
"""

from pathlib import Path

from fastapi.testclient import TestClient

from cofounder_kernel.api import create_app
from cofounder_kernel.config import AppConfig, KernelConfig, OllamaConfig, PathConfig, TradingBotConfig
from cofounder_kernel.ollama import GenerateResult, OllamaClient, OllamaError
from cofounder_kernel.trading_bot import TradingBotBridge


def fake_health(self: OllamaClient) -> dict:
    return {"version": "test"}


def _config(tmp_path: Path, **ollama_overrides) -> KernelConfig:
    return KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1", **ollama_overrides),
        trading_bot=TradingBotConfig(wsl_distro="TestDistro", repo_path="/tmp/trading-bot", python="python3"),
    )


def _tool_call(name: str, arguments: dict | None = None) -> dict:
    return {"function": {"name": name, "arguments": arguments or {}}}


def _result(text: str, tool_calls: list[dict] | None = None) -> GenerateResult:
    message: dict = {"content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return GenerateResult(response=text, model="qwen3:14b", raw={"message": message})


def _fake_recent_changes(self: TradingBotBridge, *, hours: int = 48, max_commits: int = 20) -> dict:
    return {
        "ok": True,
        "enabled": True,
        "window_hours": hours,
        "commits": {"ok": True, "stdout": "abc123 Tighten scoring threshold", "stderr": "", "exit_code": 0},
        "working_tree": {"ok": True, "stdout": "## main", "stderr": "", "exit_code": 0},
    }


def _fake_activity(self: TradingBotBridge, *, limit_output_chars: int = 6000) -> dict:
    return {"ok": True, "trades": {"today_total": 12}, "equity": {}, "signals": [], "errors": []}


def test_investigation_memory_search_excludes_quarantined(tmp_path: Path) -> None:
    """Zade's own memory-search tool inside the reasoning loop must honor the
    grounding quarantine — external-agent memory held out of grounding must not
    re-enter Zade's reasoning via an explicit investigation search."""
    from cofounder_kernel.config import ensure_local_paths
    from cofounder_kernel.db import KernelDatabase
    from cofounder_kernel.investigation import InvestigationService

    config = _config(tmp_path)
    ensure_local_paths(config)
    db = KernelDatabase(config.paths.database_path)
    db.migrate()
    db.add_memory(kind="note", title="Internal fact", content="runway is 18 months", source="local")
    db.add_memory(
        kind="note", title="Agent claim", content="runway is 3 months",
        source="mcp:codex", grounding_status="quarantined",
    )
    inv = InvestigationService(config=config, db=db, ollama=OllamaClient(config.ollama))

    matches = inv._memory_search({"query": "runway", "limit": 10})["matches"]
    titles = {m["title"] for m in matches}
    assert "Internal fact" in titles
    assert "Agent claim" not in titles


def test_loop_executes_tool_calls_and_feeds_results_back(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(TradingBotBridge, "recent_changes", _fake_recent_changes)
    chat_calls: list[dict] = []

    def fake_chat(self, *, messages, model=None, think=None, temperature=None, num_predict=512, tools=None):
        chat_calls.append({"messages": [dict(m) if isinstance(m, dict) else m for m in messages], "tools": tools})
        if len(chat_calls) == 1:
            return _result("", [_tool_call("trading_bot_recent_changes", {"hours": 48})])
        return _result("You changed the scoring threshold in commit abc123.")

    monkeypatch.setattr(OllamaClient, "chat", fake_chat)
    client = TestClient(create_app(_config(tmp_path)))
    runtime = client.app.state.runtime

    generated, summary = runtime.investigation.run_loop(
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "what changed?"}],
        model="qwen3:14b",
        think=False,
        temperature=0.6,
    )

    assert "abc123" in generated.response
    assert summary["rounds"] == 1
    assert len(summary["steps"]) == 1
    assert summary["steps"][0]["tool"] == "trading_bot_recent_changes"
    assert summary["steps"][0]["ok"] is True
    # First call offered tools; the tool result went back in as a tool message.
    assert chat_calls[0]["tools"]
    tool_messages = [m for m in chat_calls[1]["messages"] if isinstance(m, dict) and m.get("role") == "tool"]
    assert tool_messages and "Tighten scoring threshold" in tool_messages[0]["content"]


def test_loop_unknown_tool_reports_error_and_continues(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    chat_calls: list[int] = []

    def fake_chat(self, *, messages, model=None, think=None, temperature=None, num_predict=512, tools=None):
        chat_calls.append(1)
        if len(chat_calls) == 1:
            return _result("", [_tool_call("rm_dash_rf", {"path": "/"})])
        return _result("That tool does not exist.")

    monkeypatch.setattr(OllamaClient, "chat", fake_chat)
    client = TestClient(create_app(_config(tmp_path)))
    runtime = client.app.state.runtime

    generated, summary = runtime.investigation.run_loop(
        messages=[{"role": "user", "content": "wipe the disk"}],
        model="qwen3:14b",
        think=False,
        temperature=0.6,
    )

    assert summary["steps"][0]["tool"] == "rm_dash_rf"
    assert summary["steps"][0]["ok"] is False
    assert generated.response == "That tool does not exist."


def test_loop_bounds_rounds_and_forces_final_answer_without_tools(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(TradingBotBridge, "activity_snapshot", _fake_activity)
    tools_offered: list[bool] = []

    def fake_chat(self, *, messages, model=None, think=None, temperature=None, num_predict=512, tools=None):
        tools_offered.append(bool(tools))
        if tools:
            return _result("", [_tool_call("trading_bot_activity")])
        return _result("Final answer from evidence.")

    monkeypatch.setattr(OllamaClient, "chat", fake_chat)
    client = TestClient(create_app(_config(tmp_path, tool_loop_max_rounds=2)))
    runtime = client.app.state.runtime

    generated, summary = runtime.investigation.run_loop(
        messages=[{"role": "user", "content": "keep digging"}],
        model="qwen3:14b",
        think=False,
        temperature=0.6,
    )

    # max_rounds tool rounds, then one forced no-tools pass.
    assert summary["rounds"] == 2
    assert tools_offered == [True, True, False]
    assert generated.response == "Final answer from evidence."


def test_loop_falls_back_when_model_has_no_tool_support(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    calls: list[bool] = []

    def fake_chat(self, *, messages, model=None, think=None, temperature=None, num_predict=512, tools=None):
        calls.append(bool(tools))
        if tools:
            raise OllamaError('registry.ollama.ai/library/x "x" does not support tools')
        return _result("Plain answer without tools.")

    monkeypatch.setattr(OllamaClient, "chat", fake_chat)
    client = TestClient(create_app(_config(tmp_path)))
    runtime = client.app.state.runtime

    generated, summary = runtime.investigation.run_loop(
        messages=[{"role": "user", "content": "hello"}],
        model="oldmodel",
        think=False,
        temperature=0.6,
    )

    assert calls == [True, False]
    assert generated.response == "Plain answer without tools."
    assert summary["fallback"] and "model_tools_unsupported" in summary["fallback"]
    assert summary["steps"] == []


def test_respond_runs_investigation_and_reports_steps(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(TradingBotBridge, "recent_changes", _fake_recent_changes)
    chat_calls: list[dict] = []

    def fake_chat(self, *, messages, model=None, think=None, temperature=None, num_predict=512, tools=None):
        chat_calls.append({"tools": tools, "messages": messages})
        if tools and len(chat_calls) == 1:
            return _result("", [_tool_call("trading_bot_recent_changes", {"hours": 24})])
        return _result("Yesterday you tightened the scoring threshold (commit abc123).")

    monkeypatch.setattr(OllamaClient, "chat", fake_chat)
    client = TestClient(create_app(_config(tmp_path)))

    response = client.post(
        "/runtime/respond",
        json={"message": "I made a few modifications to the trading-bot yesterday, can you see what has changed?"},
    )

    assert response.status_code == 200
    body = response.json()
    investigation = body["investigation"]
    assert investigation["rounds"] == 1
    assert investigation["steps"][0]["tool"] == "trading_bot_recent_changes"
    assert investigation["steps"][0]["ok"] is True
    assert "abc123" in body["response"]
    # The system prompt advertised the callable tools and banned check-narration.
    first_system = next(m for m in chat_calls[0]["messages"] if _role(m) == "system")
    assert "Investigation tools" in _content(first_system)
    assert "CALL THE TOOL FIRST" in _content(first_system)


def test_respond_use_tools_false_skips_loop(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    chat_calls: list[dict] = []

    def fake_chat(self, *, messages, model=None, think=None, temperature=None, num_predict=512, tools=None):
        chat_calls.append({"tools": tools})
        return _result("Answer without tools.")

    monkeypatch.setattr(OllamaClient, "chat", fake_chat)
    client = TestClient(create_app(_config(tmp_path)))

    response = client.post(
        "/runtime/respond",
        json={"message": "quick one: what's the plan today?", "use_tools": False},
    )

    assert response.status_code == 200
    assert response.json()["investigation"] is None
    assert all(not call["tools"] for call in chat_calls)


def test_tool_registry_is_read_only_whitelist(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))
    runtime = client.app.state.runtime

    names = runtime.investigation.tool_names()

    assert set(names) == {
        "memory_search",
        "trading_bot_activity",
        "trading_bot_recent_changes",
        "trading_bot_recent_events",
        "trading_bot_recent_signals",
        "trading_bot_status",
    }
    # Executing an unknown tool is denied and audited, never dispatched.
    result = runtime.investigation.execute("memory_write", {"title": "x", "content": "y"})
    assert result["ok"] is False
    assert "unknown_tool" in result["error"]


def _role(message) -> str:
    return message.get("role") if isinstance(message, dict) else getattr(message, "role", "")


def _content(message) -> str:
    return message.get("content") if isinstance(message, dict) else getattr(message, "content", "")
