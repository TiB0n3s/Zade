from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import cofounder_kernel.delegation as delegation_module
from cofounder_kernel.api import create_app
from cofounder_kernel.config import AppConfig, DelegationConfig, KernelConfig, OllamaConfig, PathConfig
from cofounder_kernel.ollama import OllamaClient

PHRASE = "make the jump to hyperspace"


def fake_health(self: OllamaClient) -> dict:
    return {"version": "test"}


def fake_embed(self: OllamaClient, *, text: str, model=None) -> list[float]:
    return [1.0, 0.0]


def _config(tmp_path: Path, **delegation_kw) -> KernelConfig:
    return KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
        delegation=DelegationConfig(**delegation_kw) if delegation_kw else DelegationConfig(),
    )


def test_build_brief_is_scoped(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))
    brief = client.post(
        "/delegation/brief",
        json={"task": "Refactor the auth module", "acceptance": "All tests pass"},
    ).json()["brief"]
    assert "## Goal" in brief
    assert "Refactor the auth module" in brief
    assert "All tests pass" in brief


def test_bridge_engine_without_command_cannot_invoke(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    # Bridge engine with no command → can never auto-invoke; stays gated.
    client = TestClient(create_app(_config(tmp_path, engine="bridge")))
    result = client.post("/delegation/run", json={"task": "do a thing", "auto_invoke": True}).json()
    assert result["status"] == "approval_required"
    assert result["auto_invoked"] is False
    assert "engine cannot run" in result["reason"]


def test_hybrid_engine_prepares_local_assessment_without_work_item_approval(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    workspace = tmp_path / "hybrid-workspace"
    workspace.mkdir()
    app = create_app(_config(tmp_path, engine="hybrid", workspace_root=str(workspace)))

    class FakeBuildService:
        def __init__(self):
            self.calls = []

        def prepare(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "session": {"id": 12, "phase": "approval"},
                "assessment": {"recommended_tier": "small"},
                "approval_request_id": 34,
            }

        def status(self, _session_id):
            raise AssertionError("new item should prepare, not resume")

    fake = FakeBuildService()
    app.state.delegation.build_service = fake
    client = TestClient(app)

    result = client.post(
        "/delegation/run",
        json={"task": "build a small app", "acceptance": "tests pass"},
    ).json()

    assert result["auto_invoked"] is False
    assert result["dispatch"]["engine"] == "hybrid"
    assert result["dispatch"]["status"] == "approval_required"
    assert fake.calls[0]["workspace"] == str(workspace)
    approvals = app.state.db.list_approval_requests(status="pending", limit=100)
    assert not any(
        item.source_type == "work_item" and item.source_id == result["item_id"]
        for item in approvals
    )


def test_brief_engine_prepares_not_sends(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)
    client = TestClient(create_app(_config(tmp_path, engine="brief")))
    queued = client.post("/delegation/run", json={"task": "prep only", "auto_invoke": False}).json()
    approved = client.post(
        f"/work/items/{queued['item_id']}/approve",
        json={"resolved_by": "founder", "dispatch": True, "typed_confirmation": PHRASE},
    ).json()
    dispatch = approved["dispatch_result"]
    assert dispatch["status"] == "prepared"
    assert "prep only" in dispatch["brief"]


def test_auto_invoke_within_budget_dispatches_and_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)

    captured = {}

    def fake_run_agent(command, *, brief, timeout=600.0, max_output_chars=20000, cwd=None, env=None):
        captured["command"] = command
        captured["brief"] = brief
        return "PATCH: refactored the module, all green."

    monkeypatch.setattr(delegation_module, "run_agent", fake_run_agent)

    config = _config(
        tmp_path, enabled=True, auto_invoke=True, agent_command=("agent-cli",), daily_budget=25, engine="bridge"
    )
    app = create_app(config)
    client = TestClient(app)

    result = client.post("/delegation/run", json={"task": "Refactor auth", "auto_invoke": True}).json()

    assert result["auto_invoked"] is True
    dispatch = result["dispatch"]
    assert dispatch["ok"] is True
    assert "refactored the module" in dispatch["artifact"]
    assert captured["command"] == ["agent-cli"]
    assert "Refactor auth" in captured["brief"]

    # The artifact was filed as delegated-work evidence.
    evidence = client.get("/founder/evidence").json()["items"]
    assert any(item["evidence_type"] == "delegated_work" for item in evidence)


def test_over_budget_falls_back_to_gated(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = _config(
        tmp_path, enabled=True, auto_invoke=True, agent_command=("agent-cli",), daily_budget=0, engine="bridge"
    )
    client = TestClient(create_app(config))
    result = client.post("/delegation/run", json={"task": "x", "auto_invoke": True}).json()
    assert result["auto_invoked"] is False
    assert "budget" in result["reason"]


def test_gated_dispatch_runs_agent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)
    monkeypatch.setattr(
        delegation_module, "run_agent",
        lambda command, *, brief, timeout=600.0, max_output_chars=20000, cwd=None, env=None: "done via approval",
    )
    config = _config(tmp_path, enabled=True, auto_invoke=False, agent_command=("agent-cli",), engine="bridge")
    client = TestClient(create_app(config))

    queued = client.post("/delegation/run", json={"task": "gated task", "auto_invoke": False}).json()
    assert queued["auto_invoked"] is False
    approved = client.post(
        f"/work/items/{queued['item_id']}/approve",
        json={"resolved_by": "founder", "dispatch": True, "typed_confirmation": PHRASE},
    ).json()
    assert approved["dispatch_result"]["ok"] is True
    assert "done via approval" in approved["dispatch_result"]["artifact"]


def test_workspace_root_confines_agent_cwd(tmp_path: Path, monkeypatch) -> None:
    """A configured workspace_root is created and passed to the agent as cwd, so
    delegated builds land there instead of inside the kernel's own repo."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)

    captured = {}

    def fake_run_agent(command, *, brief, timeout=600.0, max_output_chars=20000, cwd=None, env=None):
        captured["cwd"] = cwd
        return "scaffolded."

    monkeypatch.setattr(delegation_module, "run_agent", fake_run_agent)
    workspace = tmp_path / "workspace" / "builds"
    config = _config(
        tmp_path,
        enabled=True,
        auto_invoke=True,
        agent_command=("agent-cli",),
        workspace_root=str(workspace),
        engine="bridge",
    )
    client = TestClient(create_app(config))

    result = client.post("/delegation/run", json={"task": "scaffold an app", "auto_invoke": True}).json()

    assert result["auto_invoked"] is True
    assert result["dispatch"]["ok"] is True
    assert captured["cwd"] == str(workspace)
    assert workspace.is_dir()


def test_bridge_environment_is_sanitized_to_loopback_ollama(tmp_path: Path, monkeypatch) -> None:
    """The compatibility bridge under a local policy: ANTHROPIC_BASE_URL is the
    loopback Ollama, the auth token is 'ollama', the real API key is STRIPPED,
    and the explicit local model is passed. Cloud is unreachable by env."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-sentinel-should-never-be-seen")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")

    captured = {}

    def fake_run_agent(command, *, brief, timeout=600.0, max_output_chars=20000, cwd=None, env=None):
        captured["env"] = env
        return "bridge artifact"

    monkeypatch.setattr(delegation_module, "run_agent", fake_run_agent)
    config = _config(
        tmp_path, enabled=True, auto_invoke=True, agent_command=("agent-cli",), engine="bridge"
    )
    client = TestClient(create_app(config))
    result = client.post("/delegation/run", json={"task": "bridge task", "auto_invoke": True}).json()

    assert result["dispatch"]["ok"] is True
    env = captured["env"]
    assert env is not None
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:1"  # the configured loopback Ollama
    assert env["ANTHROPIC_AUTH_TOKEN"] == "ollama"
    assert "ANTHROPIC_API_KEY" not in env
    assert env["ANTHROPIC_MODEL"] == "qwen2.5-coder:14b"  # configured local coding model
    note = result["dispatch"]["bridge"]
    assert note["mode"] == "local_compatibility_bridge"
    assert note["base_host"] == "127.0.0.1"
    assert note["anthropic_api_key_present"] is False


def test_native_engine_dispatches_coding_agent_without_subprocess(tmp_path: Path, monkeypatch) -> None:
    """Engine 'native' (the default) runs Zade's own coding agent: no external
    process is launched even when an agent command is configured, and the
    artifact is filed as native delegated-work evidence."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)

    def forbidden_run_agent(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("external agent subprocess must not launch under engine=native")

    monkeypatch.setattr(delegation_module, "run_agent", forbidden_run_agent)

    from cofounder_kernel.coding_agent import CodingAgentService

    def fake_agent_run(self, *, task, workspace=None, context="", max_rounds=None, model=None, verify_always=False):
        return {
            "ok": True,
            "status": "ok",
            "error": "",
            "model": "qwen3:14b",
            "provider": {"provider": "ollama", "endpoint_host": "127.0.0.1", "verified_local": True},
            "workspace": str(tmp_path),
            "rounds": 2,
            "used_tools": True,
            "steps": [{"tool": "read_file", "ok": True}],
            "changed_files": ["src/fix.py"],
            "response": "Fixed the bug; focused test passes.",
        }

    monkeypatch.setattr(CodingAgentService, "run", fake_agent_run)
    config = _config(
        tmp_path, enabled=True, auto_invoke=True, agent_command=("agent-cli",), engine="native"
    )
    client = TestClient(create_app(config))
    result = client.post("/delegation/run", json={"task": "fix the bug", "auto_invoke": True}).json()

    dispatch = result["dispatch"]
    assert dispatch["ok"] is True
    assert dispatch["engine"] == "native"
    assert dispatch["model"] == "qwen3:14b"
    assert dispatch["changed_files"] == ["src/fix.py"]
    evidence = client.get("/founder/evidence").json()["items"]
    assert any("native coding" in (item.get("notes") or "").lower() or
               item.get("source") == "delegation:native-coding-agent" for item in evidence)


def test_native_capability_error_never_escalates_to_bridge(tmp_path: Path, monkeypatch) -> None:
    """A native capability failure is returned as a LOCAL failure. It must not
    fall back to the bridge CLI or any cloud provider."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)

    def forbidden_run_agent(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("no fallback to external agent on native failure")

    monkeypatch.setattr(delegation_module, "run_agent", forbidden_run_agent)

    from cofounder_kernel.coding_agent import CodingAgentService

    def failing_agent_run(self, *, task, workspace=None, context="", max_rounds=None, model=None, verify_always=False):
        return {
            "ok": False,
            "status": "capability_error",
            "error": "No configured local model passed the native tool-call probe.",
            "model": "",
            "rounds": 0,
            "steps": [],
            "changed_files": [],
            "response": "",
        }

    monkeypatch.setattr(CodingAgentService, "run", failing_agent_run)
    config = _config(
        tmp_path, enabled=True, auto_invoke=True, agent_command=("agent-cli",), engine="native"
    )
    client = TestClient(create_app(config))
    result = client.post("/delegation/run", json={"task": "fix it", "auto_invoke": True}).json()

    dispatch = result["dispatch"]
    assert dispatch["ok"] is False
    assert dispatch["engine"] == "native"
    assert "tool-call probe" in dispatch["error"]


def test_native_unverified_claim_is_flagged_in_evidence(tmp_path: Path, monkeypatch) -> None:
    """Work item #43 regression: the agent's artifact claims a tsc type check
    that appears nowhere in its audited step list (npx isn't even allowlisted).
    The claim must be flagged in the dispatch result and the filed evidence."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)

    from cofounder_kernel.coding_agent import CodingAgentService

    def fake_agent_run(self, *, task, workspace=None, context="", max_rounds=None, model=None, verify_always=False):
        return {
            "ok": True,
            "status": "ok",
            "error": "",
            "model": "qwen3:14b",
            "provider": {"provider": "ollama", "endpoint_host": "127.0.0.1", "verified_local": True},
            "workspace": str(tmp_path),
            "rounds": 2,
            "used_tools": True,
            "steps": [
                {"tool": "read_file", "arguments": {"path": "src/app.ts"}, "ok": True},
                {"tool": "write_file", "arguments": {"path": "src/app.ts"}, "ok": True},
            ],
            "changed_files": ["src/app.ts"],
            "response": "Done. Passed TypeScript type checks with `npx tsc --noEmit`.",
        }

    monkeypatch.setattr(CodingAgentService, "run", fake_agent_run)
    config = _config(tmp_path, enabled=True, auto_invoke=True, engine="native")
    client = TestClient(create_app(config))
    result = client.post("/delegation/run", json={"task": "harden the app", "auto_invoke": True}).json()

    dispatch = result["dispatch"]
    assert dispatch["ok"] is True
    assert dispatch["unverified_claims"], "the tsc claim has no matching executed step"
    assert any("tsc" in claim.lower() for claim in dispatch["unverified_claims"])
    evidence = client.get("/founder/evidence").json()["items"]
    filed = next(item for item in evidence if item["evidence_type"] == "delegated_work")
    assert "UNVERIFIED CLAIM" in (filed.get("notes") or "")
    assert "tsc" in (filed.get("notes") or "").lower()


def test_native_claim_backed_by_audited_step_is_not_flagged(tmp_path: Path, monkeypatch) -> None:
    """A verification claim whose command really ran as an audited ok step is
    NOT marked unverified — the cross-check keys on executed argv contents."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)

    from cofounder_kernel.coding_agent import CodingAgentService

    def fake_agent_run(self, *, task, workspace=None, context="", max_rounds=None, model=None, verify_always=False):
        return {
            "ok": True,
            "status": "ok",
            "error": "",
            "model": "qwen3:14b",
            "provider": {"provider": "ollama", "endpoint_host": "127.0.0.1", "verified_local": True},
            "workspace": str(tmp_path),
            "rounds": 3,
            "used_tools": True,
            "steps": [
                {"tool": "replace_in_file", "arguments": {"path": "src/lib.js"}, "ok": True},
                {"tool": "run_command", "arguments": {"argv": ["npm", "test"]}, "ok": True},
            ],
            "changed_files": ["src/lib.js"],
            "response": "Fixed the parser; npm test passed with all tests green.",
        }

    monkeypatch.setattr(CodingAgentService, "run", fake_agent_run)
    config = _config(tmp_path, enabled=True, auto_invoke=True, engine="native")
    client = TestClient(create_app(config))
    result = client.post("/delegation/run", json={"task": "fix the parser", "auto_invoke": True}).json()

    dispatch = result["dispatch"]
    assert dispatch["ok"] is True
    assert dispatch["unverified_claims"] == []
    evidence = client.get("/founder/evidence").json()["items"]
    filed = next(item for item in evidence if item["evidence_type"] == "delegated_work")
    assert "UNVERIFIED CLAIM" not in (filed.get("notes") or "")


def test_bridge_artifact_verification_claims_are_marked_unverified(tmp_path: Path, monkeypatch) -> None:
    """An external agent returns no audited step list, so its verification
    claims are unverifiable by construction and filed with the marker."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)
    monkeypatch.setattr(
        delegation_module, "run_agent",
        lambda command, *, brief, timeout=600.0, max_output_chars=20000, cwd=None, env=None:
            "Refactor complete. All tests green and npm audit is clean.",
    )
    config = _config(
        tmp_path, enabled=True, auto_invoke=True, agent_command=("agent-cli",), engine="bridge"
    )
    client = TestClient(create_app(config))
    result = client.post("/delegation/run", json={"task": "refactor", "auto_invoke": True}).json()

    dispatch = result["dispatch"]
    assert dispatch["ok"] is True
    assert dispatch["unverified_claims"]
    evidence = client.get("/founder/evidence").json()["items"]
    filed = next(item for item in evidence if item["evidence_type"] == "delegated_work")
    assert "UNVERIFIED CLAIM" in (filed.get("notes") or "")
    assert "no audited step list" in (filed.get("notes") or "")


def test_disabled_blocks_and_unregisters(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path, enabled=False)))
    handlers = {h["action"] for h in client.get("/action-handlers").json()["items"]}
    assert "external.delegation.run" not in handlers
    blocked = client.post("/delegation/run", json={"task": "x"})
    assert blocked.status_code == 400 and "disabled" in blocked.json()["detail"]


def test_delegation_layer_in_inventory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))
    inventory = client.get("/self-inventory").json()
    assert "POST /delegation/run" in inventory["delegation_layer"]["routes"]
    assert inventory["delegation_layer"]["dispatch_action"] == "external.delegation.run"


def test_workspace_target_forces_gated_and_is_recorded(tmp_path: Path, monkeypatch) -> None:
    """A run aimed at a founder-named project directory never auto-invokes,
    even with auto-invoke on and budget available; the target is recorded in
    the item metadata and shown in the brief the founder approves."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    target = tmp_path / "SomeProject"
    target.mkdir()
    config = _config(
        tmp_path, enabled=True, auto_invoke=True, agent_command=("agent-cli",), engine="bridge"
    )
    client = TestClient(create_app(config))

    result = client.post(
        "/delegation/run",
        json={"task": "fix the audit findings", "auto_invoke": True, "workspace": str(target)},
    ).json()

    assert result["auto_invoked"] is False
    queued = client.get("/work/queue", params={"status": "approval_required"}).json()["items"]
    item = next(entry for entry in queued if entry["id"] == result["item_id"])
    assert item["metadata"]["workspace"] == str(target)
    assert "## Target project" in item["metadata"]["brief"]
    assert str(target) in item["metadata"]["brief"]


def test_workspace_target_dispatch_uses_target_as_cwd(tmp_path: Path, monkeypatch) -> None:
    """On dispatch, a bridge run executes inside the target project directory
    instead of the default delegation workspace."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)
    target = tmp_path / "SomeProject"
    target.mkdir()

    captured = {}

    def fake_run_agent(command, *, brief, timeout=600.0, max_output_chars=20000, cwd=None, env=None):
        captured["cwd"] = cwd
        return "audit clean."

    monkeypatch.setattr(delegation_module, "run_agent", fake_run_agent)
    config = _config(
        tmp_path,
        enabled=True,
        auto_invoke=False,
        agent_command=("agent-cli",),
        workspace_root=str(tmp_path / "default-workspace"),
        engine="bridge",
    )
    client = TestClient(create_app(config))

    queued = client.post(
        "/delegation/run",
        json={"task": "fix the audit findings", "workspace": str(target)},
    ).json()
    approved = client.post(
        f"/work/items/{queued['item_id']}/approve",
        json={"resolved_by": "founder", "dispatch": True, "typed_confirmation": PHRASE},
    ).json()

    assert approved["dispatch_result"]["ok"] is True
    assert captured["cwd"] == str(target)


def test_workspace_target_native_engine_passes_workspace(tmp_path: Path, monkeypatch) -> None:
    """Under engine=native the coding agent runs confined to the target project."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)
    target = tmp_path / "SomeProject"
    target.mkdir()

    from cofounder_kernel.coding_agent import CodingAgentService

    captured = {}

    def fake_agent_run(self, *, task, workspace=None, context="", max_rounds=None, model=None, verify_always=False):
        captured["workspace"] = workspace
        return {
            "ok": True,
            "status": "ok",
            "error": "",
            "model": "qwen3:14b",
            "provider": {"provider": "ollama", "endpoint_host": "127.0.0.1", "verified_local": True},
            "workspace": str(workspace),
            "rounds": 1,
            "used_tools": True,
            "steps": [],
            "changed_files": ["package.json"],
            "response": "Vulnerabilities resolved; npm audit reports 0.",
        }

    monkeypatch.setattr(CodingAgentService, "run", fake_agent_run)
    config = _config(tmp_path, enabled=True, auto_invoke=False, engine="native")
    client = TestClient(create_app(config))

    queued = client.post(
        "/delegation/run",
        json={"task": "resolve the vulnerabilities", "workspace": str(target)},
    ).json()
    approved = client.post(
        f"/work/items/{queued['item_id']}/approve",
        json={"resolved_by": "founder", "dispatch": True, "typed_confirmation": PHRASE},
    ).json()

    assert approved["dispatch_result"]["ok"] is True
    assert captured["workspace"] == str(target)


def test_workspace_target_dispatch_refuses_bad_targets(tmp_path: Path, monkeypatch) -> None:
    """Dispatch fails closed on a missing target directory, and on any target
    inside the kernel's own repository (delegated runs may not modify the kernel)."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)

    def forbidden_run_agent(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("agent must not launch for an invalid target workspace")

    monkeypatch.setattr(delegation_module, "run_agent", forbidden_run_agent)
    kernel_root = Path(delegation_module.__file__).resolve().parents[2]

    for index, (bad_target, expected) in enumerate(
        (
            (str(tmp_path / "does-not-exist"), "does not exist"),
            (str(kernel_root), "kernel"),
        )
    ):
        # Fresh app per case: queue_delegation's unique_key is second-resolution,
        # so two same-second enqueues in one app would dedup onto one item.
        config = _config(
            tmp_path / f"case-{index}",
            enabled=True,
            auto_invoke=False,
            agent_command=("agent-cli",),
            engine="bridge",
        )
        client = TestClient(create_app(config))
        queued = client.post(
            "/delegation/run",
            json={"task": "fix things", "workspace": bad_target},
        ).json()
        approved = client.post(
            f"/work/items/{queued['item_id']}/approve",
            json={"resolved_by": "founder", "dispatch": True, "typed_confirmation": PHRASE},
        ).json()
        dispatch = approved["dispatch_result"]
        assert dispatch["ok"] is False, bad_target
        assert dispatch["status"] == "flow_error"
        assert expected in dispatch["error"]


# ---- directed runs: full auto on a founder command ------------------------------

def _fake_native_ok(workspace_holder: dict):
    def fake_agent_run(self, *, task, workspace=None, context="", max_rounds=None, model=None, verify_always=False):
        workspace_holder["workspace"] = workspace
        return {
            "ok": True,
            "status": "ok",
            "error": "",
            "founder_question": None,
            "model": "qwen3:14b",
            "provider": {"provider": "ollama", "endpoint_host": "127.0.0.1", "verified_local": True},
            "workspace": str(workspace or ""),
            "rounds": 2,
            "used_tools": True,
            "steps": [{"tool": "write_file", "arguments": {"path": "src/app.py"}, "ok": True}],
            "changed_files": ["src/app.py"],
            "response": "Built it.",
        }

    return fake_agent_run


def test_directed_workspace_run_auto_invokes_and_completes_item(tmp_path: Path, monkeypatch) -> None:
    """A DIRECTED founder command runs at full auto even into a founder-named
    project workspace, and the work item is closed with the outcome — no stale
    approval entry for work that already ran."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)
    target = tmp_path / "SomeProject"
    target.mkdir()

    from cofounder_kernel.coding_agent import CodingAgentService

    captured: dict = {}
    monkeypatch.setattr(CodingAgentService, "run", _fake_native_ok(captured))
    config = _config(tmp_path, enabled=True, auto_invoke=True, engine="native")
    client = TestClient(create_app(config))

    result = client.post(
        "/delegation/run",
        json={"task": "build the feature", "workspace": str(target), "directed": True},
    ).json()

    assert result["auto_invoked"] is True
    assert result["dispatch"]["ok"] is True
    assert captured["workspace"] == str(target)
    # The item is finalized, and nothing waits on an approval.
    done = client.get("/work/queue", params={"status": "done"}).json()["items"]
    assert any(item["id"] == result["item_id"] for item in done)
    assert not client.get("/work/queue", params={"status": "approval_required"}).json()["items"]


def test_directed_delegation_unique_key_never_dispatches_completed_item_twice(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)
    target = tmp_path / "SomeProject"
    target.mkdir()

    from cofounder_kernel.coding_agent import CodingAgentService

    calls = {"count": 0}

    def fake_agent_run(
        self, *, task, workspace=None, context="", max_rounds=None, model=None, verify_always=False
    ):
        calls["count"] += 1
        return {
            "ok": True,
            "status": "ok",
            "error": "",
            "founder_question": None,
            "model": "qwen3:14b",
            "provider": {
                "provider": "ollama",
                "endpoint_host": "127.0.0.1",
                "verified_local": True,
            },
            "workspace": str(workspace or ""),
            "rounds": 1,
            "used_tools": True,
            "steps": [],
            "changed_files": [],
            "response": "Already complete.",
        }

    monkeypatch.setattr(CodingAgentService, "run", fake_agent_run)
    app = create_app(_config(tmp_path, enabled=True, auto_invoke=True, engine="native"))
    service = app.state.delegation

    first = service.queue_delegation(
        task="build the feature",
        workspace=str(target),
        directed=True,
        unique_key="project-autonomy:1:plan:criterion:1:0",
    )
    second = service.queue_delegation(
        task="build the feature",
        workspace=str(target),
        directed=True,
        unique_key="project-autonomy:1:plan:criterion:1:0",
    )

    assert first["auto_invoked"] is True
    assert second["existing"] is True
    assert second["dispatch"]["ok"] is True
    assert calls["count"] == 1


def test_undirected_workspace_run_still_waits_for_founder(tmp_path: Path, monkeypatch) -> None:
    """Without the directed flag, a workspace-targeted run keeps the conservative
    posture: queued for the founder, never auto-dispatched."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    target = tmp_path / "SomeProject"
    target.mkdir()
    config = _config(tmp_path, enabled=True, auto_invoke=True, engine="native")
    client = TestClient(create_app(config))

    result = client.post(
        "/delegation/run",
        json={"task": "build the feature", "auto_invoke": True, "workspace": str(target)},
    ).json()

    assert result["auto_invoked"] is False


def test_directed_needs_decision_files_founder_question(tmp_path: Path, monkeypatch) -> None:
    """When the agent stops on a genuine decision, the run comes back as
    needs_decision (not an error) and a founder_decision item carrying the
    question and a resume brief lands in the Inbox."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)

    from cofounder_kernel.coding_agent import CodingAgentService

    def fake_agent_run(self, *, task, workspace=None, context="", max_rounds=None, model=None, verify_always=False):
        return {
            "ok": False,
            "status": "needs_decision",
            "error": "",
            "founder_question": {
                "question": "SQLite or Postgres for persistence?",
                "options": ["SQLite", "Postgres"],
            },
            "model": "qwen3:14b",
            "provider": {"provider": "ollama", "endpoint_host": "127.0.0.1", "verified_local": True},
            "workspace": str(workspace or ""),
            "rounds": 1,
            "used_tools": True,
            "steps": [{"tool": "list_files", "arguments": {}, "ok": True}],
            "changed_files": [],
            "response": "Paused: persistence engine is the founder's call.",
        }

    monkeypatch.setattr(CodingAgentService, "run", fake_agent_run)
    config = _config(tmp_path, enabled=True, auto_invoke=True, engine="native")
    client = TestClient(create_app(config))

    result = client.post(
        "/delegation/run", json={"task": "add persistence", "directed": True}
    ).json()

    assert result["auto_invoked"] is True
    dispatch = result["dispatch"]
    assert dispatch["status"] == "needs_decision"
    assert dispatch["ok"] is True  # the run's outcome IS the filed question
    assert dispatch["founder_question"]["question"] == "SQLite or Postgres for persistence?"
    assert dispatch["decision_item_id"]

    queued = client.get("/work/queue", params={"status": "approval_required"}).json()["items"]
    decision = next(item for item in queued if item["id"] == dispatch["decision_item_id"])
    assert decision["kind"] == "founder_decision"
    assert "SQLite or Postgres" in decision["detail"]
    # The resume brief keeps the original task and surfaces the question.
    assert "## Founder decision" in decision["metadata"]["brief"]
    assert decision["metadata"]["task"] == "add persistence"
    # The parent remains visibly blocked until the founder answers the filed
    # question; filing a decision is not completing the delegated work.
    parent = next(
        item
        for item in client.get("/work/queue", params={"status": "blocked"}).json()["items"]
        if item["id"] == result["item_id"]
    )
    assert parent["result"]["status"] == "needs_decision"
    assert "needs_decision" in parent["last_error"]


def test_approved_delegation_needs_decision_keeps_parent_blocked(tmp_path: Path, monkeypatch) -> None:
    """The approved handler path applies the same non-completion lifecycle
    when it files a founder decision."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)

    from cofounder_kernel.coding_agent import CodingAgentService

    def fake_agent_run(self, *, task, workspace=None, context="", max_rounds=None, model=None, verify_always=False):
        return {
            "ok": False,
            "status": "needs_decision",
            "founder_question": {"question": "Use SQLite or Postgres?", "options": ["SQLite", "Postgres"]},
            "model": "qwen3:14b",
            "steps": [],
            "changed_files": [],
            "response": "Paused for the founder.",
        }

    monkeypatch.setattr(CodingAgentService, "run", fake_agent_run)
    client = TestClient(create_app(_config(tmp_path, enabled=True, auto_invoke=False, engine="native")))
    queued = client.post("/delegation/run", json={"task": "add persistence"}).json()

    approved = client.post(
        f"/work/items/{queued['item_id']}/approve",
        json={"resolved_by": "founder", "dispatch": True, "typed_confirmation": PHRASE},
    ).json()

    assert approved["dispatch_result"]["status"] == "needs_decision"
    parent = next(
        item
        for item in client.get("/work/queue", params={"status": "blocked"}).json()["items"]
        if item["id"] == queued["item_id"]
    )
    assert parent["status"] == "blocked"
    assert parent["result"]["status"] == "needs_decision"
    assert "needs_decision" in parent["last_error"]


def test_autonomous_queue_dispatches_registered_delegation_handler_and_blocks_parent(
    tmp_path: Path, monkeypatch
) -> None:
    """The autonomous queue reaches the registered delegation handler and
    keeps the parent result when that handler asks the founder a question."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)

    from cofounder_kernel.authority import AuthorityDecision, AuthorityResult
    from cofounder_kernel.coding_agent import CodingAgentService

    def fake_agent_run(self, *, task, workspace=None, context="", max_rounds=None, model=None, verify_always=False):
        return {
            "ok": False,
            "status": "needs_decision",
            "founder_question": {"question": "Use SQLite or Postgres?", "options": ["SQLite", "Postgres"]},
            "model": "qwen3:14b",
            "steps": [],
            "changed_files": [],
            "response": "Paused for the founder.",
        }

    monkeypatch.setattr(CodingAgentService, "run", fake_agent_run)
    app = create_app(_config(tmp_path, enabled=True, auto_invoke=False, engine="native"))
    monkeypatch.setattr(
        app.state.work_queue.authority,
        "evaluate",
        lambda _request: AuthorityResult(
            decision=AuthorityDecision.ALLOW,
            reason="test autonomous delegation grant",
            matched_rule="test.allow",
        ),
    )

    queued = app.state.delegation.queue_delegation(task="add persistence", auto_invoke=False)
    run = app.state.work_queue.run_next()

    assert queued["status"] == "pending"
    assert run.status == "blocked"
    assert run.result["status"] == "needs_decision"
    parent = app.state.db.get_work_item(queued["item_id"])
    assert parent is not None
    assert parent.status == "blocked"
    assert parent.result["status"] == "needs_decision"
    assert "needs_decision" in parent.last_error


def test_directed_delegation_failed_verification_keeps_parent_non_complete(tmp_path: Path, monkeypatch) -> None:
    """A handler success cannot close the parent when its mandatory check failed."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)

    from cofounder_kernel.coding_agent import CodingAgentService

    def fake_agent_run(self, *, task, workspace=None, context="", max_rounds=None, model=None, verify_always=False):
        return {
            "ok": True,
            "status": "ok",
            "model": "qwen3:14b",
            "steps": [],
            "changed_files": ["src/app.py"],
            "auto_verification": {"ok": False, "error": "pytest failed"},
            "response": "Implemented the change, but pytest failed.",
        }

    monkeypatch.setattr(CodingAgentService, "run", fake_agent_run)
    client = TestClient(create_app(_config(tmp_path, enabled=True, auto_invoke=True, engine="native")))

    result = client.post("/delegation/run", json={"task": "fix the app", "directed": True}).json()

    assert result["dispatch"]["status"] == "ok"
    parent = next(
        item
        for item in client.get("/work/queue", params={"status": "error"}).json()["items"]
        if item["id"] == result["item_id"]
    )
    assert parent["result"]["auto_verification"]["ok"] is False
    assert "failed verification" in parent["last_error"]


def test_resolved_founder_decision_reconciles_original_parent_on_success(tmp_path: Path, monkeypatch) -> None:
    """A completed resume closes the original blocked delegation parent with
    the resumed handler result, rather than leaving a stale block behind."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)

    from cofounder_kernel.coding_agent import CodingAgentService

    outcomes = iter(
        (
            {
                "ok": False,
                "status": "needs_decision",
                "founder_question": {"question": "Use SQLite or Postgres?", "options": ["SQLite", "Postgres"]},
                "model": "qwen3:14b",
                "steps": [],
                "changed_files": [],
                "response": "Paused for the founder.",
            },
            {
                "ok": True,
                "status": "ok",
                "model": "qwen3:14b",
                "steps": [],
                "changed_files": ["src/app.py"],
                "response": "Implemented the founder's choice.",
            },
        )
    )
    monkeypatch.setattr(
        CodingAgentService,
        "run",
        lambda self, *, task, workspace=None, context="", max_rounds=None, model=None, verify_always=False: next(outcomes),
    )
    client = TestClient(create_app(_config(tmp_path, enabled=True, auto_invoke=True, engine="native")))

    initial = client.post("/delegation/run", json={"task": "add persistence", "directed": True}).json()
    decision_id = initial["dispatch"]["decision_item_id"]
    decision = next(
        item
        for item in client.get("/work/queue", params={"status": "approval_required"}).json()["items"]
        if item["id"] == decision_id
    )
    assert decision["metadata"]["parent_work_item_id"] == initial["item_id"]

    resumed = client.post(
        f"/work/items/{decision_id}/approve",
        json={"resolved_by": "founder", "dispatch": True},
    ).json()

    assert resumed["dispatch_result"]["status"] == "ok"
    parent = next(
        item
        for item in client.get("/work/queue", params={"status": "done"}).json()["items"]
        if item["id"] == initial["item_id"]
    )
    assert parent["result"]["status"] == "ok"
    assert parent["last_error"] == ""


def test_resolved_founder_decision_reconciles_original_parent_on_error(tmp_path: Path, monkeypatch) -> None:
    """A failed resume makes the original parent visibly error with the
    resumed result instead of leaving it blocked forever."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)

    from cofounder_kernel.coding_agent import CodingAgentService

    outcomes = iter(
        (
            {
                "ok": False,
                "status": "needs_decision",
                "founder_question": {"question": "Use SQLite or Postgres?", "options": ["SQLite", "Postgres"]},
                "model": "qwen3:14b",
                "steps": [],
                "changed_files": [],
                "response": "Paused for the founder.",
            },
            {
                "ok": False,
                "status": "flow_error",
                "error": "The resumed implementation failed.",
                "model": "qwen3:14b",
                "steps": [],
                "changed_files": [],
                "response": "Could not implement the chosen approach.",
            },
        )
    )
    monkeypatch.setattr(
        CodingAgentService,
        "run",
        lambda self, *, task, workspace=None, context="", max_rounds=None, model=None, verify_always=False: next(outcomes),
    )
    client = TestClient(create_app(_config(tmp_path, enabled=True, auto_invoke=True, engine="native")))

    initial = client.post("/delegation/run", json={"task": "add persistence", "directed": True}).json()
    decision_id = initial["dispatch"]["decision_item_id"]
    resumed = client.post(
        f"/work/items/{decision_id}/approve",
        json={"resolved_by": "founder", "dispatch": True},
    ).json()

    assert resumed["dispatch_result"]["status"] == "flow_error"
    parent = next(
        item
        for item in client.get("/work/queue", params={"status": "error"}).json()["items"]
        if item["id"] == initial["item_id"]
    )
    assert parent["result"]["status"] == "flow_error"
    assert parent["last_error"] == "The resumed implementation failed."


def test_founder_decision_item_resolves_without_typed_phrase(tmp_path: Path, monkeypatch) -> None:
    """Answering Zade's question is the approval: the decision card advertises
    requires_typed_phrase=False and approve+dispatch works with NO phrase."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "embed", fake_embed)

    from cofounder_kernel.coding_agent import CodingAgentService

    def fake_agent_run(self, *, task, workspace=None, context="", max_rounds=None, model=None, verify_always=False):
        return {
            "ok": False,
            "status": "needs_decision",
            "error": "",
            "founder_question": {"question": "SQLite or Postgres?", "options": []},
            "model": "qwen3:14b",
            "provider": {"provider": "ollama", "endpoint_host": "127.0.0.1", "verified_local": True},
            "workspace": str(workspace or ""),
            "rounds": 1,
            "used_tools": True,
            "steps": [],
            "changed_files": [],
            "response": "Paused on the persistence choice.",
        }

    monkeypatch.setattr(CodingAgentService, "run", fake_agent_run)
    config = _config(tmp_path, enabled=True, auto_invoke=True, engine="native")
    client = TestClient(create_app(config))

    result = client.post(
        "/delegation/run", json={"task": "add persistence", "directed": True}
    ).json()
    decision_id = result["dispatch"]["decision_item_id"]

    # The console card says no phrase is needed for this one.
    console = client.get("/approval-console", params={"status": "pending"}).json()["items"]
    card = next(c for c in console if (c.get("work_item") or {}).get("id") == decision_id)
    assert card["authority_tier"]["requires_typed_phrase"] is False
    assert card["authority_tier"]["matched_rule"] == "founder_decision.answer_is_approval"

    # Approve + dispatch with NO typed confirmation — the founder's click is her word.
    approved = client.post(
        f"/work/items/{decision_id}/approve",
        json={"resolved_by": "founder", "dispatch": True},
    ).json()
    # The fake agent pauses again, which proves the resume actually dispatched
    # (a phrase failure would have been a 400 before any dispatch).
    assert approved["dispatch_result"]["status"] == "needs_decision"

    # A non-decision delegation item still demands the phrase. Fresh app:
    # queue_delegation's unique_key is second-resolution, so a same-second
    # enqueue in the same app would dedup onto the item that already ran.
    plain_client = TestClient(create_app(_config(tmp_path / "plain", enabled=True, auto_invoke=False, engine="native")))
    plain = plain_client.post("/delegation/run", json={"task": "another build"}).json()
    denied = plain_client.post(
        f"/work/items/{plain['item_id']}/approve",
        json={"resolved_by": "founder", "dispatch": True},
    )
    assert denied.status_code == 400
    assert "typed confirmation" in denied.json()["detail"]
