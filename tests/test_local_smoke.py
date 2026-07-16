"""REAL local coding smoke test — live Ollama, loopback sockets only.

Run explicitly with:  ZADE_LOCAL_SMOKE=1 pytest tests/test_local_smoke.py -q

A temp repository gets a known bug, a failing focused test, and repository
instructions. The native coding agent (build profile as system message, real
tool schemas, the probed local model) must read the source, apply a controlled
edit, run the focused test, and leave it passing — with every socket connection
confined to loopback and sentinel cloud keys ignored.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from cofounder_kernel.coding_agent import CodingAgentService
from cofounder_kernel.config import load_config
from cofounder_kernel.db import KernelDatabase
from cofounder_kernel.inventory import ModelInventoryService
from cofounder_kernel.ollama import OllamaClient

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}

pytestmark = pytest.mark.skipif(
    os.environ.get("ZADE_LOCAL_SMOKE") != "1",
    reason="live-Ollama smoke test; set ZADE_LOCAL_SMOKE=1 to run",
)


def test_native_agent_fixes_bug_in_real_repo_loopback_only(tmp_path: Path, monkeypatch) -> None:
    # Sentinel cloud keys present the whole time; they must change nothing.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SENTINEL")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-SENTINEL")
    monkeypatch.setenv("OLLAMA_NO_CLOUD", "1")

    # Loopback-only socket guard: any non-loopback connection fails the test.
    attempts: list[str] = []
    real_connect = socket.socket.connect

    def guarded(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else str(address)
        attempts.append(str(host))
        if str(host) not in _LOOPBACK:
            raise AssertionError(f"non-loopback socket connection attempted: {address!r}")
        return real_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded)

    # The fixture repository: a known bug + a failing focused test + instructions.
    workspace = tmp_path / "smoke-repo"
    workspace.mkdir()
    (workspace / "calc.py").write_text(
        "def add(a, b):\n"
        "    return a - b  # BUG: subtraction instead of addition\n",
        encoding="utf-8",
    )
    (workspace / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    (workspace / "AGENTS.md").write_text(
        "Fix bugs with the smallest possible diff. Always run test_calc.py after editing.",
        encoding="utf-8",
    )

    # Real config (real loopback Ollama), but a test-local DB and workspace.
    cfg = load_config(Path(__file__).resolve().parents[1] / "config.toml")
    ollama = OllamaClient(cfg.ollama)
    try:
        ollama.health()
    except Exception:
        pytest.skip("local Ollama is not reachable")
    db = KernelDatabase(tmp_path / "smoke.sqlite3")
    db.migrate()
    inventory = ModelInventoryService(config=cfg, ollama=ollama)
    agent = CodingAgentService(config=cfg, db=db, ollama=ollama, inventory=inventory)

    resolved_model = inventory.resolve_coding_agent_model()
    result = agent.run(
        task=(
            "The file calc.py has a bug: add(a, b) returns a - b but must return a + b. "
            "Read calc.py, fix the bug with a minimal edit, then run the focused test "
            "(python -m pytest test_calc.py -q) and confirm it passes."
        ),
        workspace=workspace,
    )
    print(json.dumps({k: v for k, v in result.items() if k != "response"}, indent=2, default=str))
    print("RESPONSE:", result["response"][:800])

    # The model that ran is the probed local model, over loopback, verified local.
    assert result["model"] == resolved_model
    assert result["provider"]["verified_local"] is True
    assert result["provider"]["endpoint_host"] in _LOOPBACK
    assert result["used_tools"] is True, "the model must have used the real tools"
    assert "calc.py" in result["changed_files"], result

    # Ground truth: the bug is actually fixed and the focused test passes NOW.
    fixed = (workspace / "calc.py").read_text(encoding="utf-8")
    assert "a + b" in fixed
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "test_calc.py", "-q"],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

    # Every socket connection the process made during the run was loopback.
    assert attempts, "expected real loopback connections to Ollama"
    assert all(host in _LOOPBACK for host in attempts)
