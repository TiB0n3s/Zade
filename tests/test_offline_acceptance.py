"""Offline acceptance harness.

Every non-loopback socket connection is rejected at the socket layer for the
duration of these tests, sentinel cloud API keys sit in the environment, and a
recording Ollama transport captures every model request. We then drive the
kernel through chat, a coding (build-profile) request, planning, critique, a
tool-assisted code edit, and a focused test run — and prove that only loopback
model connections were recorded, the configured local model answered each one,
and no fallback or cloud provider was ever touched."""
from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cofounder_kernel.api import create_app
from cofounder_kernel.config import (
    AppConfig,
    DelegationConfig,
    KernelConfig,
    OllamaConfig,
    PathConfig,
)
from cofounder_kernel.ollama import OllamaClient

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture
def loopback_only_sockets(monkeypatch):
    """Reject every socket connection to a non-loopback address."""
    real_connect = socket.socket.connect
    attempts: list[str] = []

    def guarded(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else str(address)
        attempts.append(str(host))
        if str(host) not in _LOOPBACK:
            raise AssertionError(f"non-loopback socket connection attempted: {address!r}")
        return real_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    return attempts


@pytest.fixture
def sentinel_cloud_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SENTINEL-must-never-be-used")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-SENTINEL-must-never-be-used")
    monkeypatch.setenv("OLLAMA_NO_CLOUD", "1")


class RecordingTransport:
    """Replaces OllamaClient._get_json/_post_json: records (base_url, path,
    model) and serves canned local responses, including a scripted coding
    conversation for the agent loop."""

    def __init__(self):
        self.requests: list[dict[str, Any]] = []
        self.chat_script: list[dict[str, Any]] = []

    def record(self, client: OllamaClient, path: str, model: str = "") -> None:
        self.requests.append(
            {"base_url": client.config.base_url, "path": path, "model": model}
        )

    def get(self, client: OllamaClient, path: str) -> dict[str, Any]:
        self.record(client, path)
        if path == "/api/tags":
            return {
                "models": [
                    {"name": "local-brain:1b", "details": {"family": "test"}},
                ]
            }
        return {"version": "offline-test"}

    def post(self, client: OllamaClient, path: str, body: dict[str, Any]) -> dict[str, Any]:
        model = str(body.get("model", ""))
        self.record(client, path, model)
        if path == "/api/show":
            return {"capabilities": ["completion", "tools"], "model_info": {}}
        if path == "/api/embed":
            return {"embeddings": [[0.1, 0.2]]}
        if path == "/api/generate":
            return {"model": model, "response": f"[{model}] local generate answer", "done": True}
        if path == "/api/chat":
            messages = [m for m in body.get("messages", []) if isinstance(m, dict)]
            # The inventory capability probe: always answer with NATIVE tool_calls.
            if any("Use the read_file tool" in str(m.get("content", "")) for m in messages):
                return {
                    "model": model,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {"function": {"name": "read_file", "arguments": {"path": "config.py"}}}
                        ],
                    },
                    "done": True,
                }
            # The coding agent (marked by its system message) replays the script.
            is_coding_agent = any(
                m.get("role") == "system" and "Local coding agent run" in str(m.get("content", ""))
                for m in messages
            )
            if is_coding_agent and body.get("tools") and self.chat_script:
                step = self.chat_script.pop(0)
                message = {"role": "assistant", "content": step.get("content", "")}
                if step.get("tool_calls"):
                    message["tool_calls"] = step["tool_calls"]
                return {"model": model, "message": message, "done": True}
            return {
                "model": model,
                "message": {"role": "assistant", "content": f"[{model}] local chat answer"},
                "done": True,
            }
        raise AssertionError(f"unexpected POST {path}")


def _call(name: str, **arguments: Any) -> dict[str, Any]:
    return {"function": {"name": name, "arguments": arguments}}


def test_offline_acceptance_all_operations_are_loopback_local(
    tmp_path: Path, monkeypatch, loopback_only_sockets, sentinel_cloud_keys
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (workspace / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8"
    )

    transport = RecordingTransport()
    transport.chat_script = [
        {"tool_calls": [_call("read_file", path="calc.py")]},
        {
            "tool_calls": [
                _call(
                    "replace_in_file",
                    path="calc.py",
                    old_text="    return a - b",
                    new_text="    return a + b",
                )
            ]
        },
        {"tool_calls": [_call("run_command", argv=["python", "-m", "pytest", "test_calc.py", "-q"])]},
        {"content": "Bug fixed; the focused test passes."},
    ]
    monkeypatch.setattr(OllamaClient, "_get_json", lambda self, path: transport.get(self, path))
    monkeypatch.setattr(
        OllamaClient, "_post_json", lambda self, path, body: transport.post(self, path, body)
    )

    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(
            base_url="http://127.0.0.1:11434",
            chat_model="local-brain:1b",
            reasoning_model="local-brain:1b",
            coding_model="local-brain:1b",
            embedding_model="local-brain:1b",
            coding_agent_model="local-brain:1b",
        ),
        delegation=DelegationConfig(workspace_root=str(workspace), engine="native", auto_invoke=True),
    )
    client = TestClient(create_app(config))

    # 1. normal chat
    chat = client.post(
        "/runtime/respond",
        json={
            "message": "Give me the one thing that matters today.",
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )
    assert chat.status_code == 200, chat.text
    assert "[local-brain:1b]" in chat.json()["response"]

    # 2. build-profile coding request (respond with the build profile + coding role)
    coding_chat = client.post(
        "/runtime/respond",
        json={
            "message": "Outline the fix for the calc bug.",
            "task_type": "coding",
            "profile": "build",
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
            "contrarian": False,
        },
    )
    assert coding_chat.status_code == 200, coding_chat.text

    # 3. planning request (roles panel) and 7. subagent-style task
    planned = client.post("/roles/run", json={"role": "triage", "content": "Plan the calc fix."})
    assert planned.status_code == 200, planned.text

    # 4. critique request
    critique = client.post(
        "/runtime/respond",
        json={
            "message": "Should we ship the calc fix today? Challenge me.",
            "contrarian": True,
            "use_memory": False,
            "use_semantic_memory": False,
            "use_skills": False,
        },
    )
    assert critique.status_code == 200, critique.text

    # 5+6. one tool-assisted code edit + focused test execution via the native agent
    build = client.post("/delegation/run", json={"task": "Fix add() so test_calc.py passes", "auto_invoke": True})
    assert build.status_code == 200, build.text
    dispatch = build.json()["dispatch"]
    assert dispatch["ok"] is True, dispatch
    assert dispatch["engine"] == "native"
    assert dispatch["model"] == "local-brain:1b"
    assert dispatch["changed_files"] == ["calc.py"]
    assert (workspace / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"

    # ---- the proof ----
    hosts = {r["base_url"] for r in transport.requests}
    assert hosts == {"http://127.0.0.1:11434"}, hosts
    models = {r["model"] for r in transport.requests if r["model"]}
    assert models == {"local-brain:1b"}, models
    joined = json.dumps(transport.requests)
    for forbidden in ("anthropic", "openai", "ollama.com"):
        assert forbidden not in joined
    # No socket ever reached beyond loopback (TestClient is in-process; any
    # stray SDK or fallback would have tripped the guard).
    assert all(host in _LOOPBACK for host in loopback_only_sockets)
    # The sentinel keys were present the whole time and changed nothing.
    status = client.get("/providers/status").json()
    assert status["indicator"] == "LOCAL"
    assert status["provider_policy"] == "local_only"
