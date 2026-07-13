import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from cofounder_kernel.api import create_app
from cofounder_kernel.config import AppConfig, DevToolsConfig, KernelConfig, OllamaConfig, PathConfig
from cofounder_kernel.db import KernelDatabase
from cofounder_kernel.devtools import DevToolsHandlers
from cofounder_kernel.handlers import ActionHandlerRegistry
from cofounder_kernel.ollama import OllamaClient


PHRASE = "make the jump to hyperspace"


def fake_health(self: OllamaClient) -> dict:
    return {"version": "test"}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "workspace"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo), check=True, capture_output=True, text=True)
    _git(repo, "config", "user.email", "zade@example.com")
    _git(repo, "config", "user.name", "Zade Test")
    (repo / "README.md").write_text("# Workspace\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial")
    return repo


def _config(tmp_path: Path, repo: Path) -> KernelConfig:
    return KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
        devtools=DevToolsConfig(workspace_root=repo, default_branch="main"),
    )


def _bench(tmp_path: Path, repo: Path):
    """A registry + devtools bound to a real repo, and a work-item factory."""
    config = _config(tmp_path, repo)
    config.paths.data_dir.mkdir(parents=True, exist_ok=True)
    db = KernelDatabase(config.paths.database_path)
    db.migrate()
    registry = ActionHandlerRegistry(db=db, config=config)
    DevToolsHandlers(db=db, config=config, workspace_root=repo, python_executable=sys.executable).register_into(registry)

    def work_item(action: str, metadata: dict, tier: str = "L3_EXTERNAL_ACTION"):
        item_id, _created = db.enqueue_work_item(
            kind="action_step", title=action, detail="", action=action, target="",
            permission_tier=tier, priority=50, source="test", metadata=metadata,
        )
        return db.get_work_item(item_id)

    return registry, db, work_item


def test_handlers_registered_and_command_allowlist(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    registry, _db, work_item = _bench(tmp_path, repo)

    actions = {h["action"] for h in registry.list_handlers()}
    assert {"dev.command.run", "dev.git.branch", "dev.git.commit", "dev.draft.write"} <= actions

    ok = registry.dispatch(work_item("dev.command.run", {"command": "git-status"}))
    assert ok["ok"] is True
    assert ok["exit_code"] == 0
    assert ok["command"] == "git-status"

    version = registry.dispatch(work_item("dev.command.run", {"command": "python-version"}))
    assert version["ok"] is True
    assert "Python" in version["stdout_tail"]

    try:
        registry.dispatch(work_item("dev.command.run", {"command": "rm-rf"}))
        assert False, "expected disallowed command to raise"
    except ValueError as exc:
        assert "not allowed" in str(exc)

    try:
        registry.dispatch(work_item("dev.command.run", {"command": "git-log", "args": ["../../../etc/passwd"]}))
        assert False, "expected path-traversal arg to raise"
    except ValueError as exc:
        assert "traversal" in str(exc) or "absolute" in str(exc)


def test_git_branch_and_commit_flow(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    registry, _db, work_item = _bench(tmp_path, repo)

    # Refuse to commit on the default branch.
    (repo / "feature.txt").write_text("work in progress\n", encoding="utf-8")
    try:
        registry.dispatch(work_item("dev.git.commit", {"message": "on main"}))
        assert False, "expected default-branch refusal"
    except ValueError as exc:
        assert "default branch" in str(exc)

    branched = registry.dispatch(work_item("dev.git.branch", {"name": "feature/pricing"}))
    assert branched["current_branch"] == "feature/pricing"

    committed = registry.dispatch(work_item("dev.git.commit", {"message": "Add pricing WIP"}))
    assert committed["status"] == "ok"
    assert len(committed["sha"]) == 40
    assert committed["branch"] == "feature/pricing"
    assert "feature.txt" in committed["files"]
    # The commit really landed in the repo.
    log = subprocess.run(["git", "log", "--oneline", "-1"], cwd=str(repo), capture_output=True, text=True)
    assert "Add pricing WIP" in log.stdout

    # Nothing left to commit -> clear error.
    try:
        registry.dispatch(work_item("dev.git.commit", {"message": "empty"}))
        assert False, "expected nothing-to-commit"
    except ValueError as exc:
        assert "Nothing staged" in str(exc)

    # Bad branch name rejected.
    try:
        registry.dispatch(work_item("dev.git.branch", {"name": "bad name;rm"}))
        assert False, "expected invalid branch name"
    except ValueError as exc:
        assert "safe branch name" in str(exc)


def test_draft_write_never_sends(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    registry, db, work_item = _bench(tmp_path, repo)

    result = registry.dispatch(work_item("dev.draft.write", {
        "kind": "email",
        "title": "Pilot follow-up",
        "content": "Thanks for the call. Here is the pricing we discussed.",
        "to": "founder@customerco.com",
    }))

    assert result["status"] == "ok"
    assert result["sent"] is False
    path = Path(result["path"])
    assert path.is_file()
    body = path.read_text(encoding="utf-8")
    assert "EMAIL draft: Pilot follow-up" in body
    assert "To: founder@customerco.com" in body
    assert "has NOT sent it" in body
    # Memory record marks it unsent.
    memory = db.search_memories("Pilot follow-up", limit=3)
    assert any(m.kind == "draft" for m in memory)

    try:
        registry.dispatch(work_item("dev.draft.write", {"kind": "telegram", "content": "x"}))
        assert False, "expected bad draft kind"
    except ValueError as exc:
        assert "Draft kind" in str(exc)


def test_dev_action_runs_through_approval_dispatch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    repo = _make_repo(tmp_path)
    client = TestClient(create_app(_config(tmp_path, repo)))

    handlers = client.get("/action-handlers")
    inventory = client.get("/self-inventory")
    queued = client.post("/work/items", json={
        "kind": "action_step",
        "title": "Run git status in the workspace",
        "action": "dev.command.run",
        "permission_tier": "L3_EXTERNAL_ACTION",
        "source": "zade.proposal",
        "metadata": {"command": "git-status"},
    })
    # Machine dev action is approval-gated, never autonomous.
    assert queued.json()["status"] == "approval_required"

    dispatched = client.post(
        f"/work/items/{queued.json()['item_id']}/approve",
        json={"resolved_by": "founder", "dispatch": True, "typed_confirmation": PHRASE},
    )

    assert "dev.command.run" in {h["action"] for h in handlers.json()["items"]}
    assert "dev.command.run" in inventory.json()["devtools_layer"]["actions"]
    assert "git-status" in inventory.json()["devtools_layer"]["allowed_commands"]
    assert dispatched.status_code == 200, dispatched.text
    assert dispatched.json()["dispatch"] == "dispatched"
    assert dispatched.json()["dispatch_result"]["ok"] is True
    assert dispatched.json()["dispatch_result"]["command"] == "git-status"

    # Dispatch without the typed phrase is rejected.
    queued2 = client.post("/work/items", json={
        "kind": "action_step", "title": "again", "action": "dev.command.run",
        "permission_tier": "L3_EXTERNAL_ACTION", "source": "zade.proposal", "metadata": {"command": "python-version"},
    })
    no_phrase = client.post(
        f"/work/items/{queued2.json()['item_id']}/approve",
        json={"resolved_by": "founder", "dispatch": True, "typed_confirmation": "wrong"},
    )
    assert no_phrase.status_code == 400
