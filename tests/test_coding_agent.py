"""Native coding agent: system-message discipline, real tools, workspace
confinement, permission-controlled execution, and no cloud escalation."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cofounder_kernel.coding_agent import CodingAgentService
from cofounder_kernel.config import (
    AppConfig,
    DelegationConfig,
    KernelConfig,
    OllamaConfig,
    PathConfig,
)
from cofounder_kernel.db import KernelDatabase
from cofounder_kernel.ollama import OllamaClient


def _config(tmp_path: Path, workspace: Path, **ollama_kw) -> KernelConfig:
    return KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1", coding_agent_model="fake-local:1b", **ollama_kw),
        delegation=DelegationConfig(workspace_root=str(workspace)),
    )


def _db(tmp_path: Path) -> KernelDatabase:
    db = KernelDatabase(tmp_path / "data" / "kernel.sqlite3")
    db.migrate()
    return db


class ScriptedOllama(OllamaClient):
    """OllamaClient whose chat() replays a scripted conversation. Each entry is
    either {'tool_calls': [...]} or {'content': 'final text'}. Records every
    call for assertions; only ever 'contacts' the loopback config it was built
    with (no network at all)."""

    def __init__(self, config, script: list[dict[str, Any]]):
        super().__init__(config)
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def chat(self, *, messages, model=None, think=None, temperature=None, num_predict=512, tools=None):
        self.calls.append(
            {
                "model": model,
                "messages": [dict(m) if isinstance(m, dict) else m for m in messages],
                "tools": list(tools or []),
            }
        )
        step = self.script.pop(0) if self.script else {"content": "done"}
        message: dict[str, Any] = {"role": "assistant", "content": step.get("content", "")}
        if step.get("tool_calls"):
            message["tool_calls"] = step["tool_calls"]
        from cofounder_kernel.ollama import GenerateResult

        return GenerateResult(
            response=message["content"], model=model or "fake-local:1b", raw={"message": message}
        )


def _call(name: str, **arguments: Any) -> dict[str, Any]:
    return {"function": {"name": name, "arguments": arguments}}


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    """A tiny repo with a known bug and a failing focused test."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "calc.py").write_text(
        "def add(a, b):\n    return a - b  # BUG: should be addition\n", encoding="utf-8"
    )
    (workspace / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8"
    )
    (workspace / "AGENTS.md").write_text(
        "Repository rule: keep diffs minimal; run test_calc.py after edits.", encoding="utf-8"
    )
    return workspace


def _service(tmp_path: Path, workspace: Path, script: list[dict[str, Any]]):
    cfg = _config(tmp_path, workspace)
    ollama = ScriptedOllama(cfg.ollama, script)
    svc = CodingAgentService(config=cfg, db=_db(tmp_path), ollama=ollama, inventory=_StubInventory())
    return svc, ollama


class _StubInventory:
    def resolve_coding_agent_model(self) -> str:
        return "fake-local:1b"


# 17-19. message discipline and real tool schemas -----------------------------------

def test_build_profile_is_system_message_and_task_is_user_role(tmp_path: Path, fixture_repo: Path) -> None:
    svc, ollama = _service(tmp_path, fixture_repo, [{"content": "done"}])
    svc.run(task="Fix the add() bug so test_calc.py passes.")
    first_call = ollama.calls[0]
    messages = first_call["messages"]
    assert messages[0]["role"] == "system"
    # The build profile text (or its fallback) is in the system message; the
    # workspace instruction file is folded in too.
    assert "workspace" in messages[0]["content"].lower()
    assert "AGENTS.md" in messages[0]["content"]
    assert messages[-1]["role"] == "user"
    assert "Fix the add() bug" in messages[-1]["content"]
    # The user task is NOT inside the system prompt.
    assert "Fix the add() bug" not in messages[0]["content"]
    # Real tool schemas were supplied.
    tool_names = {t["function"]["name"] for t in first_call["tools"]}
    assert {"list_files", "read_file", "search_files", "write_file", "replace_in_file",
            "run_command", "git_status", "git_diff"} <= tool_names


# 20-22. a full read -> edit -> test cycle through real local tools ------------------

def test_agent_reads_edits_and_tests_fixture_repo(tmp_path: Path, fixture_repo: Path) -> None:
    script = [
        {"tool_calls": [_call("read_file", path="calc.py")]},
        {
            "tool_calls": [
                _call(
                    "replace_in_file",
                    path="calc.py",
                    old_text="    return a - b  # BUG: should be addition",
                    new_text="    return a + b",
                )
            ]
        },
        {"tool_calls": [_call("run_command", argv=["python", "-m", "pytest", "test_calc.py", "-q"])]},
        {"content": "Fixed add() and test_calc.py passes."},
    ]
    svc, ollama = _service(tmp_path, fixture_repo, script)
    result = svc.run(task="Fix the add() bug so test_calc.py passes.")

    assert result["ok"] is True, result
    assert result["model"] == "fake-local:1b"
    assert result["changed_files"] == ["calc.py"]
    assert (fixture_repo / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"
    # The focused test really ran and passed (tool result went back to the model).
    tool_messages = [
        m for call in ollama.calls for m in call["messages"]
        if isinstance(m, dict) and m.get("role") == "tool"
    ]
    pytest_results = [m for m in tool_messages if "returncode" in str(m.get("content", ""))]
    assert pytest_results, "pytest tool result was not fed back to the model"
    payload = json.loads(pytest_results[-1]["content"])
    assert payload["ok"] is True and payload["returncode"] == 0
    # Tool results returned to the SAME local model on every round.
    assert {call["model"] for call in ollama.calls} == {"fake-local:1b"}
    assert result["provider"]["verified_local"] is True


# security boundaries -----------------------------------------------------------------

def test_path_traversal_is_blocked(tmp_path: Path, fixture_repo: Path) -> None:
    script = [
        {"tool_calls": [_call("read_file", path="../outside.txt")]},
        {"content": "done"},
    ]
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")
    svc, ollama = _service(tmp_path, fixture_repo, script)
    result = svc.run(task="read something outside")
    step = result["steps"][0]
    assert step["ok"] is False
    tool_messages = [
        m for call in ollama.calls for m in call["messages"]
        if isinstance(m, dict) and m.get("role") == "tool"
    ]
    assert "escapes the workspace" in str(tool_messages[0]["content"])


def test_absolute_path_outside_workspace_is_blocked(tmp_path: Path, fixture_repo: Path) -> None:
    target = tmp_path / "outside2.txt"
    script = [
        {"tool_calls": [_call("write_file", path=str(target), content="pwned")]},
        {"content": "done"},
    ]
    svc, _ = _service(tmp_path, fixture_repo, script)
    result = svc.run(task="write outside")
    assert result["steps"][0]["ok"] is False
    assert not target.exists()


def test_unallowlisted_command_is_refused(tmp_path: Path, fixture_repo: Path) -> None:
    script = [
        {"tool_calls": [_call("run_command", argv=["curl", "https://api.anthropic.com"])]},
        {"content": "done"},
    ]
    svc, ollama = _service(tmp_path, fixture_repo, script)
    result = svc.run(task="try to reach the network")
    assert result["steps"][0]["ok"] is False
    tool_messages = [
        m for call in ollama.calls for m in call["messages"]
        if isinstance(m, dict) and m.get("role") == "tool"
    ]
    assert "not allowlisted" in str(tool_messages[0]["content"])


def test_npx_stays_off_the_allowlist(tmp_path: Path, fixture_repo: Path) -> None:
    # npm/node are allowlisted for JS-project maintenance, but npx executes
    # arbitrary packages by design and must stay refused.
    script = [
        {"tool_calls": [_call("run_command", argv=["npx", "some-arbitrary-package"])]},
        {"content": "done"},
    ]
    svc, _ = _service(tmp_path, fixture_repo, script)
    result = svc.run(task="try npx")
    assert result["steps"][0]["ok"] is False


def test_node_command_runs_when_installed(tmp_path: Path, fixture_repo: Path) -> None:
    import shutil

    if shutil.which("node") is None:
        pytest.skip("node is not installed on this machine")
    script = [
        {"tool_calls": [_call("run_command", argv=["node", "--version"])]},
        {"content": "done"},
    ]
    svc, ollama = _service(tmp_path, fixture_repo, script)
    result = svc.run(task="check node")
    assert result["steps"][0]["ok"] is True
    tool_messages = [
        m for call in ollama.calls for m in call["messages"]
        if isinstance(m, dict) and m.get("role") == "tool"
    ]
    assert "v" in str(tool_messages[0]["content"])


def test_unknown_tool_is_refused_not_invented(tmp_path: Path, fixture_repo: Path) -> None:
    script = [
        {"tool_calls": [_call("delete_everything", path=".")]},
        {"content": "done"},
    ]
    svc, ollama = _service(tmp_path, fixture_repo, script)
    result = svc.run(task="misbehave")
    assert result["steps"][0]["ok"] is False
    tool_messages = [
        m for call in ollama.calls for m in call["messages"]
        if isinstance(m, dict) and m.get("role") == "tool"
    ]
    assert "unknown_tool" in str(tool_messages[0]["content"])


def test_iteration_limit_bounds_the_loop(tmp_path: Path, fixture_repo: Path) -> None:
    # A model that always asks for another read: the loop must stop at the cap
    # and force a final no-tools answer.
    script = [{"tool_calls": [_call("read_file", path="calc.py")]} for _ in range(50)]
    script.append({"content": "forced final"})
    svc, ollama = _service(tmp_path, fixture_repo, script)
    result = svc.run(task="loop forever", max_rounds=3)
    assert result["rounds"] == 3
    # Final call carries no tools.
    assert ollama.calls[-1]["tools"] == []


# 23. capability failure without cloud escalation ---------------------------------------

def test_capability_error_lists_candidates_and_never_calls_cloud(tmp_path: Path, fixture_repo: Path) -> None:
    from cofounder_kernel.inventory import ModelInventoryError

    class FailingInventory:
        def resolve_coding_agent_model(self) -> str:
            raise ModelInventoryError(
                "No configured local model passed the native tool-call probe for the coding agent "
                "(tried: ['qwen2.5-coder:14b', 'qwen3:14b']). Installed models: ['a', 'b']. "
                "Set [ollama] coding_agent_model to an installed model that supports native tool calls."
            )

    cfg = _config(tmp_path, fixture_repo)
    ollama = ScriptedOllama(cfg.ollama, [])
    svc = CodingAgentService(config=cfg, db=_db(tmp_path), ollama=ollama, inventory=FailingInventory())
    result = svc.run(task="fix it")
    assert result["ok"] is False
    assert result["status"] == "capability_error"
    assert "Installed models" in result["error"]
    assert "coding_agent_model" in result["error"]
    assert ollama.calls == []  # no model call at all, local or otherwise


def test_tool_executions_are_audited(tmp_path: Path, fixture_repo: Path) -> None:
    script = [
        {"tool_calls": [_call("read_file", path="calc.py")]},
        {"content": "done"},
    ]
    cfg = _config(tmp_path, fixture_repo)
    db = _db(tmp_path)
    ollama = ScriptedOllama(cfg.ollama, script)
    svc = CodingAgentService(config=cfg, db=db, ollama=ollama, inventory=_StubInventory())
    svc.run(task="read the calc module")
    events = [e for e in db.recent_audit_events(limit=20) if e.get("action") == "coding_agent.tool_call"]
    assert events, "tool executions must be audited"
    assert events[0]["actor"] == "coding_agent"
