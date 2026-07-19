"""Native coding agent: system-message discipline, real tools, workspace
confinement, permission-controlled execution, and no cloud escalation."""
from __future__ import annotations

import json
from pathlib import Path
import sys
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
from cofounder_kernel.ollama import GenerateResult


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


class ScriptedModelClient:
    def __init__(self, script: list[dict[str, Any]]):
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        *,
        messages,
        model=None,
        think=None,
        temperature=None,
        num_predict=512,
        tools=None,
        format=None,
    ) -> GenerateResult:
        self.calls.append(
            {
                "messages": list(messages),
                "model": model,
                "tools": list(tools or []),
            }
        )
        step = self.script.pop(0) if self.script else {"content": "done"}
        message: dict[str, Any] = {"role": "assistant", "content": step.get("content", "")}
        if step.get("tool_calls"):
            message["tool_calls"] = step["tool_calls"]
        return GenerateResult(
            response=message["content"], model=model or "cloud-test", raw={"message": message}
        )

    def provider_info(self) -> dict[str, Any]:
        return {
            "provider": "fake-cloud",
            "verified_local": False,
            "fallback_attempted": False,
        }


def test_coding_loop_uses_injected_model_client_with_same_local_tools(
    tmp_path: Path, fixture_repo: Path
) -> None:
    cfg = _config(tmp_path, fixture_repo)
    local = ScriptedOllama(cfg.ollama, [])
    cloud = ScriptedModelClient(
        [
            {"tool_calls": [_call("read_file", path="calc.py")]},
            {"content": "Reviewed the file."},
        ]
    )
    service = CodingAgentService(
        config=cfg,
        db=_db(tmp_path),
        ollama=local,
        model_client=cloud,
        inventory=_StubInventory(),
    )

    result = service.run(task="Review calc.py", workspace=fixture_repo, model="cloud-test")

    assert result["provider"]["provider"] == "fake-cloud"
    assert any(step["tool"] == "read_file" for step in result["steps"])
    assert local.calls == []
    assert {call["model"] for call in cloud.calls} == {"cloud-test"}


def test_coding_loop_preserves_provider_tool_call_ids(
    tmp_path: Path, fixture_repo: Path
) -> None:
    cfg = _config(tmp_path, fixture_repo)
    local = ScriptedOllama(cfg.ollama, [])
    cloud = ScriptedModelClient(
        [
            {
                "tool_calls": [
                    {"id": "toolu_123", **_call("read_file", path="calc.py")}
                ]
            },
            {"content": "done"},
        ]
    )
    service = CodingAgentService(
        config=cfg,
        db=_db(tmp_path),
        ollama=local,
        model_client=cloud,
        inventory=_StubInventory(),
    )

    service.run(task="Review calc.py", workspace=fixture_repo, model="cloud-test")

    tool_messages = [
        message
        for message in cloud.calls[1]["messages"]
        if message.get("role") == "tool"
    ]
    assert tool_messages[0]["tool_call_id"] == "toolu_123"


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


def test_write_allowlist_blocks_other_files_and_mutating_commands(
    tmp_path: Path, fixture_repo: Path
) -> None:
    svc, ollama = _service(
        tmp_path,
        fixture_repo,
        [
            {"tool_calls": [_call("write_file", path="calc.py", content="tampered\n")]},
            {
                "tool_calls": [
                    _call(
                        "write_file",
                        path=".zade/build/requirements.md",
                        content="# Requirements\n",
                    )
                ]
            },
            {"content": "Requirements recorded."},
        ],
    )

    result = svc.run(
        task="Write requirements only.",
        workspace=fixture_repo,
        write_allowlist=(".zade/build/requirements.md",),
    )

    assert "return a - b" in (fixture_repo / "calc.py").read_text(encoding="utf-8")
    assert (fixture_repo / ".zade" / "build" / "requirements.md").is_file()
    assert result["steps"][0]["ok"] is False
    tool_messages = [
        message
        for message in ollama.calls[1]["messages"]
        if message.get("role") == "tool"
    ]
    assert "not allowed by this build phase" in tool_messages[-1]["content"]
    tool_names = {item["function"]["name"] for item in ollama.calls[0]["tools"]}
    assert "run_command" not in tool_names


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


# kernel-run auto-verification -------------------------------------------------------

def test_auto_verify_runs_real_tests_after_changes(tmp_path: Path, fixture_repo: Path) -> None:
    """When a run changed files in a pytest workspace, the KERNEL runs the real
    verification itself and appends the actual output — even when the model
    claimed success without ever running the tests."""
    script = [
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
        {"content": "Fixed add(). All tests pass."},  # claimed, never executed
    ]
    svc, _ = _service(tmp_path, fixture_repo, script)
    result = svc.run(task="fix the add() bug")

    assert result["ok"] is True, result
    assert result["changed_files"] == ["calc.py"]
    verify_steps = [s for s in result["steps"] if s.get("auto_verify")]
    assert len(verify_steps) == 1
    assert verify_steps[0]["tool"] == "run_command"
    assert verify_steps[0]["arguments"]["argv"] == ["python", "-m", "pytest", "-q"]
    assert verify_steps[0]["ok"] is True
    assert result["auto_verification"]["ok"] is True
    assert result["auto_verification"]["returncode"] == 0
    assert "Kernel auto-verification" in result["response"]
    assert "exit code: 0" in result["response"]


def test_auto_verify_failure_is_reported_honestly(tmp_path: Path, fixture_repo: Path) -> None:
    """A wrong 'fix' plus a fabricated pass claim: the kernel's appended output
    carries the REAL failing result, contradicting the model's text."""
    script = [
        {
            "tool_calls": [
                _call(
                    "replace_in_file",
                    path="calc.py",
                    old_text="    return a - b  # BUG: should be addition",
                    new_text="    return a * b",
                )
            ]
        },
        {"content": "Fixed add(). All tests pass."},
    ]
    svc, _ = _service(tmp_path, fixture_repo, script)
    result = svc.run(task="fix the add() bug")

    assert result["changed_files"] == ["calc.py"]
    assert result["auto_verification"]["ok"] is False
    verify_steps = [s for s in result["steps"] if s.get("auto_verify")]
    assert verify_steps and verify_steps[0]["ok"] is False
    assert "Kernel auto-verification" in result["response"]
    assert "exit code: 0" not in result["response"]


def test_auto_verify_skipped_without_file_changes(tmp_path: Path, fixture_repo: Path) -> None:
    script = [
        {"tool_calls": [_call("read_file", path="calc.py")]},
        {"content": "Reviewed the code; nothing to change."},
    ]
    svc, _ = _service(tmp_path, fixture_repo, script)
    result = svc.run(task="review calc.py")
    assert result["changed_files"] == []
    assert result["auto_verification"] is None
    assert "Kernel auto-verification" not in result["response"]
    assert not any(s.get("auto_verify") for s in result["steps"])


def test_syntax_fallback_check_fails_broken_python_honestly(tmp_path: Path) -> None:
    """No test entry point in the workspace: the kernel still checks the run by
    syntax-compiling the changed files. Broken code cannot come back as a clean
    'executed' — the check fails, the repair prompt fires, and when the model
    does nothing the failure is kept, not papered over."""
    ws = tmp_path / "bare-ws"
    ws.mkdir(parents=True)
    script = [
        {"tool_calls": [_call("write_file", path="app.py", content="def broken(:\n    pass\n")]},
        {"content": "Implemented app.py. Everything works."},  # fabricated claim
        # Script exhausted after this: the repair round gets the default
        # no-tool "done" reply, so nothing changes and the failure stands.
    ]
    svc, ollama = _service(tmp_path, ws, script)
    result = svc.run(task="implement the app module")

    assert result["changed_files"] == ["app.py"]
    verification = result["auto_verification"]
    assert verification["mode"] == "syntax"
    assert verification["ok"] is False
    assert verification["repair_rounds"] == 1
    assert verification["checks"][0]["argv"] == ["python", "-m", "py_compile", "app.py"]
    # The failure was fed back to the model as the Repeat leg of the loop.
    repair_calls = [
        c for c in ollama.calls
        if any("KERNEL CHECK FAILED" in str(m.get("content", "")) for m in c["messages"])
    ]
    assert repair_calls, "the real check failure must reach the model for repair"
    # The artifact carries the real output and the parse-level qualifier.
    assert "Kernel auto-verification" in result["response"]
    assert "syntax-level check only" in result["response"]


def test_syntax_fallback_repair_round_fixes_and_passes(tmp_path: Path) -> None:
    """Goal → Act → Check → Repeat, end to end: the first check fails on broken
    code, the model repairs it in the repair round, and the kernel re-runs the
    check itself — the final result reflects the passing re-check."""
    ws = tmp_path / "bare-ws"
    ws.mkdir(parents=True)
    script = [
        {"tool_calls": [_call("write_file", path="app.py", content="def broken(:\n    pass\n")]},
        {"content": "Implemented app.py."},
        # Repair round (after KERNEL CHECK FAILED):
        {"tool_calls": [_call("write_file", path="app.py", content="def fixed():\n    return 1\n")]},
        {"content": "Fixed the syntax error in app.py."},
    ]
    svc, _ = _service(tmp_path, ws, script)
    result = svc.run(task="implement the app module")

    verification = result["auto_verification"]
    assert verification["ok"] is True
    assert verification["repair_rounds"] == 1
    verify_steps = [s for s in result["steps"] if s.get("auto_verify")]
    assert len(verify_steps) == 2  # failing check + passing re-check
    assert verify_steps[0]["ok"] is False
    assert verify_steps[-1]["ok"] is True


def test_uncheckable_changed_files_are_reported_unverified(tmp_path: Path) -> None:
    """Changed files no local checker covers (e.g. JSX) must come back
    explicitly UNVERIFIED — never silently folded into a success report."""
    ws = tmp_path / "bare-ws"
    ws.mkdir(parents=True)
    script = [
        {"tool_calls": [_call(
            "write_file",
            path="src/Screen.js",
            content="export default () => (<View />);\n",
        )]},
        {"content": "Screen implemented."},
    ]
    svc, _ = _service(tmp_path, ws, script)
    result = svc.run(task="implement the screen")

    verification = result["auto_verification"]
    assert verification["mode"] == "none"
    assert verification["ok"] is None
    assert verification["unchecked_files"] == ["src/Screen.js"]
    assert verification["repair_rounds"] == 0
    assert "UNVERIFIED" in result["response"]


class _CapturingNotifier:
    def __init__(self):
        self.sent: list[dict[str, str]] = []

    def notify(self, *, topic: str, title: str, body: str = "", severity: str = "info"):
        self.sent.append({"topic": topic, "title": title, "body": body, "severity": severity})
        return {"id": len(self.sent)}


def test_send_progress_reaches_founder_and_run_continues(
    tmp_path: Path, fixture_repo: Path
) -> None:
    """The send-to-user tool: one short line raises a native notification mid-
    run and the run keeps going — it never ends the turn."""
    script = [
        {"tool_calls": [_call("send_progress", message="Finished the data layer; starting checks.")]},
        {"content": "Done."},
    ]
    cfg = _config(tmp_path, fixture_repo)
    notifier = _CapturingNotifier()
    ollama = ScriptedOllama(cfg.ollama, script)
    svc = CodingAgentService(
        config=cfg, db=_db(tmp_path), ollama=ollama, inventory=_StubInventory(), notifier=notifier
    )
    result = svc.run(task="build the thing")

    assert result["status"] == "ok"  # the run continued and finished
    assert result["progress_notes"] == ["Finished the data layer; starting checks."]
    assert notifier.sent and notifier.sent[0]["topic"] == "delegation.progress"
    assert "Finished the data layer" in notifier.sent[0]["body"]


def test_send_progress_rejects_questions(tmp_path: Path, fixture_repo: Path) -> None:
    script = [
        {"tool_calls": [_call("send_progress", message="Should I use SQLite here?")]},
        {"content": "Proceeding with the safest option."},
    ]
    svc, ollama = _service(tmp_path, fixture_repo, script)
    result = svc.run(task="build the thing")
    assert result["progress_notes"] == []
    bounce = next(
        m for m in ollama.calls[1]["messages"]
        if m.get("role") == "tool" and "statements, not questions" in str(m.get("content", ""))
    )
    assert "ask_founder" in str(bounce["content"])


def test_fresh_context_verifier_reviews_changed_files(tmp_path: Path) -> None:
    """A separate fresh-context model call reviews the changed files against
    the task. Advisory: a FAIL rides the artifact but never flips run status —
    mechanical checks stay the ground truth."""
    ws = tmp_path / "bare-ws"
    ws.mkdir(parents=True)
    script = [
        {"tool_calls": [_call("write_file", path="app.py", content="def ok():\n    return 1\n")]},
        {"content": "Wrote app.py."},
        # Next chat call is the fresh-context verifier (mechanical syntax
        # check runs through subprocess, not chat).
        {"content": "VERDICT: FAIL\n- app.py: does not implement the requested screen"},
    ]
    svc, ollama = _service(tmp_path, ws, script)
    result = svc.run(task="implement the screen module")

    assert result["ok"] is True  # advisory: status not flipped
    review = result["verifier_review"]
    assert review is not None and review["verdict"] == "fail"
    assert "Fresh-context verifier" in result["response"]
    assert "VERDICT: FAIL" in result["response"]
    # The verifier call carried a FRESH context: no build conversation in it.
    verifier_call = ollama.calls[-1]
    roles = [m.get("role") for m in verifier_call["messages"]]
    assert roles == ["system", "user"]
    assert "fresh-eyes verifier" in str(verifier_call["messages"][0].get("content", ""))


def test_verification_plan_adds_tsc_for_typescript_workspaces(tmp_path: Path) -> None:
    """Live incident item #70: a type-broken .tsx shipped under 'verification
    passed' because jest never imported it. TypeScript workspaces get tsc as a
    second mandatory check; non-TS workspaces are unchanged."""
    ts_ws = tmp_path / "ts-ws"
    ts_ws.mkdir(parents=True)
    (ts_ws / "package.json").write_text(
        json.dumps({"name": "x", "scripts": {"test": "jest"}, "devDependencies": {"typescript": "5.9.3"}}),
        encoding="utf-8",
    )
    (ts_ws / "tsconfig.json").write_text("{}", encoding="utf-8")
    svc, _ = _service(tmp_path, ts_ws, [])
    mode, checks, unchecked = svc._verification_plan(ts_ws, ["src/App.tsx"])
    assert mode == "tests"
    assert checks == [["npm", "test"], ["npm", "exec", "--no", "--", "tsc", "--noEmit"]]
    assert unchecked == []

    # No tsconfig → single check, exactly as before.
    plain_ws = tmp_path / "plain-ws"
    plain_ws.mkdir(parents=True)
    (plain_ws / "package.json").write_text(
        json.dumps({"name": "y", "scripts": {"test": "jest"}}), encoding="utf-8"
    )
    mode, checks, unchecked = svc._verification_plan(plain_ws, ["src/App.js"])
    assert mode == "tests"
    assert checks == [["npm", "test"]]

    # TS workspace WITHOUT a test script: tsc covers changed .tsx files in
    # the syntax fallback instead of leaving them unchecked.
    no_test_ws = tmp_path / "no-test-ws"
    no_test_ws.mkdir(parents=True)
    (no_test_ws / "package.json").write_text(
        json.dumps({"name": "z", "devDependencies": {"typescript": "5.9.3"}}), encoding="utf-8"
    )
    (no_test_ws / "tsconfig.json").write_text("{}", encoding="utf-8")
    mode, checks, unchecked = svc._verification_plan(no_test_ws, ["src/Screen.tsx", "notes.txt"])
    assert mode == "syntax"
    assert ["npm", "exec", "--no", "--", "tsc", "--noEmit"] in checks
    assert unchecked == ["notes.txt"]


def test_verify_always_checks_goal_state_on_no_change_runs(
    tmp_path: Path, fixture_repo: Path
) -> None:
    """Live incident item #71: a no-change run in a workspace broken by the
    PREVIOUS run reported 'executed / no files changed' and never ran a check.
    Delegated execution briefs (verify_always) test the GOAL state: inherited
    breakage fails the check, feeds the repair prompt, and reports honestly."""
    script = [
        {"content": "The step is already implemented; nothing to change."},
        # Repair round (after KERNEL CHECK FAILED) — model still does nothing.
    ]
    svc, ollama = _service(tmp_path, fixture_repo, script)
    result = svc.run(task="carry out step 5", verify_always=True)

    verification = result["auto_verification"]
    assert verification is not None
    assert verification["ok"] is False  # fixture repo's test suite fails as shipped
    assert result["changed_files"] == []
    verify_steps = [s for s in result["steps"] if s.get("auto_verify")]
    assert verify_steps and verify_steps[0]["ok"] is False
    repair_calls = [
        c for c in ollama.calls
        if any("KERNEL CHECK FAILED" in str(m.get("content", "")) for m in c["messages"])
    ]
    assert repair_calls, "inherited breakage must reach the model for repair"


def test_no_verify_always_keeps_review_runs_check_free(
    tmp_path: Path, fixture_repo: Path
) -> None:
    """Direct (non-delegated) runs keep the old semantics: no changes, no
    checks — a review-only run must not trigger repair theater."""
    script = [
        {"tool_calls": [_call("read_file", path="calc.py")]},
        {"content": "Reviewed the code; nothing to change."},
    ]
    svc, _ = _service(tmp_path, fixture_repo, script)
    result = svc.run(task="review calc.py")
    assert result["auto_verification"] is None
    assert not any(s.get("auto_verify") for s in result["steps"])


def test_workspace_diff_catches_command_created_files(tmp_path: Path) -> None:
    """Live gap: approved commands can mutate the workspace
    invisibly to write-tool tracking ("Changed 1 file(s)" undercounts). The
    kernel's before/after snapshot reports the REAL change set."""
    ws = tmp_path / "bare-ws"
    ws.mkdir(parents=True)
    (ws / "test_make.py").write_text(
        "from pathlib import Path\n\ndef test_make():\n    Path('made_by_command.txt').write_text('x')\n",
        encoding="utf-8",
    )
    script = [
        {"tool_calls": [_call(
            "run_command",
            argv=["python", "-m", "pytest", "test_make.py", "-q"],
        )]},
        {"content": "Created the file via command."},
    ]
    svc, _ = _service(tmp_path, ws, script)
    result = svc.run(task="create the marker file")

    assert result["changed_files"] == []  # write-tool tracking sees nothing
    changes = result["workspace_changes"]
    assert changes is not None
    assert changes["added"] == ["made_by_command.txt"]
    assert changes["deleted"] == []


def test_workspace_diff_catches_command_deleted_files(tmp_path: Path) -> None:
    ws = tmp_path / "bare-ws"
    ws.mkdir(parents=True)
    (ws / "stray.txt").write_text("junk", encoding="utf-8")
    (ws / "test_delete.py").write_text(
        "from pathlib import Path\n\ndef test_delete():\n    Path('stray.txt').unlink(missing_ok=True)\n",
        encoding="utf-8",
    )
    script = [
        {"tool_calls": [_call(
            "run_command",
            argv=["python", "-m", "pytest", "test_delete.py", "-q"],
        )]},
        {"content": "Deleted the stray file."},
    ]
    svc, _ = _service(tmp_path, ws, script)
    result = svc.run(task="delete the stray file")

    assert result["changed_files"] == []
    changes = result["workspace_changes"]
    assert changes is not None
    assert changes["deleted"] == ["stray.txt"]
    assert changes["added"] == []


def test_workspace_diff_includes_write_tool_edits(tmp_path: Path) -> None:
    ws = tmp_path / "bare-ws"
    ws.mkdir(parents=True)
    script = [
        {"tool_calls": [_call("write_file", path="app.py", content="def ok():\n    return 1\n")]},
        {"content": "Wrote app.py."},
    ]
    svc, _ = _service(tmp_path, ws, script)
    result = svc.run(task="write the app module")

    assert result["changed_files"] == ["app.py"]
    changes = result["workspace_changes"]
    assert changes is not None
    assert "app.py" in changes["added"]


def test_verification_argv_detects_workspace_kind(tmp_path: Path) -> None:
    # Node workspace with a declared test script → npm test.
    node_ws = tmp_path / "node-ws"
    node_ws.mkdir(parents=True)
    (node_ws / "package.json").write_text(
        json.dumps({"name": "x", "scripts": {"test": "jest"}}), encoding="utf-8"
    )
    svc, _ = _service(tmp_path, node_ws, [])
    assert svc._verification_argv(node_ws) == ["npm", "test"]
    # No test script → a bare `npm test` would just error; skip verification.
    (node_ws / "package.json").write_text(json.dumps({"name": "x"}), encoding="utf-8")
    assert svc._verification_argv(node_ws) is None
    # Python workspace with pyproject → pytest.
    py_ws = tmp_path / "py-ws"
    py_ws.mkdir(parents=True)
    (py_ws / "pyproject.toml").write_text("[project]\nname = 'y'\n", encoding="utf-8")
    assert svc._verification_argv(py_ws) == ["python", "-m", "pytest", "-q"]
    # Nothing recognizable → no auto-verification.
    empty_ws = tmp_path / "empty-ws"
    empty_ws.mkdir(parents=True)
    assert svc._verification_argv(empty_ws) is None


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


def test_injected_governed_runner_owns_command_execution(
    tmp_path: Path, fixture_repo: Path
) -> None:
    class FakeResult:
        ok = True
        returncode = 0
        stdout_tail = "governed output"
        stderr_tail = ""
        timed_out = False
        cancelled = False

    class FakeRunner:
        def __init__(self):
            self.requests = []

        def run(self, request):
            self.requests.append(request)
            return FakeResult()

    cfg = _config(tmp_path, fixture_repo)
    runner = FakeRunner()
    svc = CodingAgentService(
        config=cfg,
        db=_db(tmp_path),
        ollama=ScriptedOllama(cfg.ollama, []),
        inventory=_StubInventory(),
        command_runner=runner,
    )

    result = svc._tool_run_command(
        fixture_repo, {"argv": ["python", "-m", "pytest", "-q"]}
    )

    assert result == {
        "ok": True,
        "returncode": 0,
        "stdout": "governed output",
        "stderr": "",
    }
    assert runner.requests[0].workspace == fixture_repo
    assert runner.requests[0].profile_id == "coding-agent:python"
    assert runner.requests[0].argv[0] == sys.executable
    assert runner.requests[0].argv[1:] == ("-m", "pytest", "-q")


def test_coding_agent_refuses_package_install_and_python_payloads(
    tmp_path: Path, fixture_repo: Path
) -> None:
    cfg = _config(tmp_path, fixture_repo)
    svc = CodingAgentService(
        config=cfg,
        db=_db(tmp_path),
        ollama=ScriptedOllama(cfg.ollama, []),
        inventory=_StubInventory(),
    )

    pip_result = svc._tool_run_command(
        fixture_repo, {"argv": ["pip", "install", "requests"]}
    )
    payload_result = svc._tool_run_command(
        fixture_repo, {"argv": ["python", "-c", "print('unbounded')"]}
    )

    assert pip_result["ok"] is False
    assert "allowlisted" in pip_result["error"]
    assert payload_result["ok"] is False
    assert "approved test and verification shapes" in payload_result["error"]


# ask_founder: the queue-only-when-unsure channel --------------------------------

def test_ask_founder_ends_run_with_needs_decision(tmp_path: Path, fixture_repo: Path) -> None:
    """A genuine founder decision stops the loop cleanly: no guessing, no more
    tool rounds, and the question travels back structured for the delegation
    layer to file."""
    script = [
        {"tool_calls": [_call("ask_founder", question="SQLite or Postgres for persistence?",
                              options=["SQLite (local, zero-config)", "Postgres"])]},
        {"content": "should never be requested"},  # loop must stop before this
    ]
    svc, ollama = _service(tmp_path, fixture_repo, script)
    result = svc.run(task="Add persistence to the app")

    assert result["status"] == "needs_decision"
    assert result["ok"] is False  # the task itself is not complete
    assert result["founder_question"]["question"] == "SQLite or Postgres for persistence?"
    assert result["founder_question"]["options"] == ["SQLite (local, zero-config)", "Postgres"]
    # The loop stopped immediately after the question round.
    assert len(ollama.calls) == 1
    assert result["changed_files"] == []


def test_ask_founder_tool_is_offered_with_guardrails(tmp_path: Path, fixture_repo: Path) -> None:
    """The tool is in the belt with its only-when-blocked contract, and the
    system message carries the pre-authorization posture."""
    svc, ollama = _service(tmp_path, fixture_repo, [{"content": "done"}])
    svc.run(task="anything")
    first_call = ollama.calls[0]
    tool_names = {t["function"]["name"] for t in first_call["tools"]}
    assert "ask_founder" in tool_names
    system = first_call["messages"][0]["content"]
    assert "pre-authorized" in system
    assert "never stop to ask" in system


def test_ask_founder_bounces_capability_boundary_questions(tmp_path: Path, fixture_repo: Path) -> None:
    """A refused/blocked command is a fixed boundary of the run, not a founder
    decision: the ask bounces with route-around instructions and the run
    continues instead of interrupting the founder."""
    script = [
        {"tool_calls": [_call(
            "ask_founder",
            question="The 'npx react-native doctor' command is not allowed. How would you like to proceed?",
        )]},
        {"content": "Proceeded without the doctor check; noted the skip. Done."},
    ]
    svc, ollama = _service(tmp_path, fixture_repo, script)
    result = svc.run(task="Verify the environment and finish the setup")

    assert result["status"] == "ok"  # NOT needs_decision — the run kept going
    assert result["founder_question"] is None
    assert len(ollama.calls) == 2
    # The bounce carried instructions back to the model as the tool result.
    bounce = next(
        m for m in ollama.calls[1]["messages"]
        if m.get("role") == "tool" and "capability boundary" in str(m.get("content", ""))
    )
    assert "do not ask the founder" in str(bounce["content"]).lower()


def test_ask_founder_bounces_workspace_mechanics_questions(tmp_path: Path, fixture_repo: Path) -> None:
    """A path conflict the run created itself (live incident: a stray file named
    'src/screens' where a directory should be) is workspace mechanics, not a
    founder decision — the founder must never be told to create directories
    manually. The ask bounces and the run continues."""
    script = [
        {"tool_calls": [_call(
            "ask_founder",
            question=(
                "The 'src/screens' directory isn't properly created (current 'screens' "
                "is a file, not a folder). What should I do?"
            ),
            options=["Create 'src/screens' as a directory manually", "Adjust file path"],
        )]},
        {"content": "Resolved the path conflict myself and continued. Done."},
    ]
    svc, ollama = _service(tmp_path, fixture_repo, script)
    result = svc.run(task="Implement the barcode scanner screen")

    assert result["status"] == "ok"  # NOT needs_decision — the run kept going
    assert result["founder_question"] is None
    bounce = next(
        m for m in ollama.calls[1]["messages"]
        if m.get("role") == "tool" and "workspace mechanics" in str(m.get("content", ""))
    )
    assert "resolve it yourself" in str(bounce["content"]).lower()
