from pathlib import Path

import pytest

from cofounder_kernel.self_knowledge.hook_installer import HookInstallError, install_self_knowledge_hook


def test_installer_creates_managed_pre_commit_hook(tmp_path: Path) -> None:
    result = install_self_knowledge_hook(repo_root=tmp_path)

    hook_path = tmp_path / ".git" / "hooks" / "pre-commit"
    hook = hook_path.read_text(encoding="utf-8")
    assert result["status"] == "created"
    assert result["path"] == str(hook_path)
    assert "BEGIN ZADE SELF-KNOWLEDGE HOOK" in hook
    assert "python.exe" in hook
    assert "cofounder_kernel self-knowledge --refresh" in hook
    assert "--repo-root ." in hook
    assert "git add context/self/zade.md" in hook
    assert "cofounder_kernel self-knowledge --check" in hook


def test_installer_is_idempotent_for_managed_hook(tmp_path: Path) -> None:
    install_self_knowledge_hook(repo_root=tmp_path)
    first = (tmp_path / ".git" / "hooks" / "pre-commit").read_text(encoding="utf-8")

    result = install_self_knowledge_hook(repo_root=tmp_path)
    second = (tmp_path / ".git" / "hooks" / "pre-commit").read_text(encoding="utf-8")

    assert result["status"] == "unchanged"
    assert second == first
    assert second.count("BEGIN ZADE SELF-KNOWLEDGE HOOK") == 1


def test_installer_refuses_foreign_hook_without_force(tmp_path: Path) -> None:
    hook_path = tmp_path / ".git" / "hooks" / "pre-commit"
    hook_path.parent.mkdir(parents=True)
    hook_path.write_text("#!/bin/sh\necho foreign\n", encoding="utf-8")

    with pytest.raises(HookInstallError, match="foreign pre-commit hook"):
        install_self_knowledge_hook(repo_root=tmp_path)

    assert hook_path.read_text(encoding="utf-8") == "#!/bin/sh\necho foreign\n"


def test_installer_appends_to_foreign_hook_with_force(tmp_path: Path) -> None:
    hook_path = tmp_path / ".git" / "hooks" / "pre-commit"
    hook_path.parent.mkdir(parents=True)
    hook_path.write_text("#!/bin/sh\necho foreign\n", encoding="utf-8")

    result = install_self_knowledge_hook(repo_root=tmp_path, force=True)
    hook = hook_path.read_text(encoding="utf-8")

    assert result["status"] == "updated"
    assert hook.startswith("#!/bin/sh\necho foreign\n")
    assert hook.count("BEGIN ZADE SELF-KNOWLEDGE HOOK") == 1
