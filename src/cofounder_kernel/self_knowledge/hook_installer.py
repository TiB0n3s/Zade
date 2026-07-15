from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
from typing import Sequence


HOOK_START = "# BEGIN ZADE SELF-KNOWLEDGE HOOK"
HOOK_END = "# END ZADE SELF-KNOWLEDGE HOOK"


class HookInstallError(RuntimeError):
    """Raised when a pre-commit hook cannot be installed safely."""


def install_self_knowledge_hook(*, repo_root: Path, force: bool = False) -> dict[str, str]:
    root = Path(repo_root)
    hook_path = root / ".git" / "hooks" / "pre-commit"
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    managed_block = _managed_hook_block()

    if not hook_path.exists() or not hook_path.read_text(encoding="utf-8").strip():
        hook_path.write_text(f"#!/bin/sh\n\n{managed_block}", encoding="utf-8", newline="\n")
        _make_executable(hook_path)
        return {"status": "created", "path": str(hook_path)}

    existing = hook_path.read_text(encoding="utf-8")
    if HOOK_START in existing or HOOK_END in existing:
        updated = _replace_managed_block(existing, managed_block)
        status = "unchanged" if updated == existing else "updated"
        if updated != existing:
            hook_path.write_text(updated, encoding="utf-8", newline="\n")
        _make_executable(hook_path)
        return {"status": status, "path": str(hook_path)}

    if not force:
        raise HookInstallError(
            f"Refusing to modify foreign pre-commit hook at {hook_path}. "
            "Re-run with --force to append the managed self-knowledge block."
        )

    separator = "\n" if existing.endswith("\n") else "\n\n"
    hook_path.write_text(f"{existing}{separator}{managed_block}", encoding="utf-8", newline="\n")
    _make_executable(hook_path)
    return {"status": "updated", "path": str(hook_path)}


def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install Zade's self-knowledge pre-commit hook.")
    parser.add_argument("--repo-root", default=".", help="Repository root containing .git/hooks.")
    parser.add_argument("--force", action="store_true", help="Append to a foreign pre-commit hook.")
    args = parser.parse_args(argv)

    try:
        result = install_self_knowledge_hook(repo_root=Path(args.repo_root), force=args.force)
    except HookInstallError as exc:
        print(str(exc))
        return 1

    print(f"{result['status']}: {result['path']}")
    return 0


def main() -> None:
    raise SystemExit(run())


def _managed_hook_block() -> str:
    return (
        f"{HOOK_START}\n"
        "repo_root=$(git rev-parse --show-toplevel)\n"
        "cd \"$repo_root\"\n"
        "if [ -x \"$repo_root/.venv/Scripts/python.exe\" ]; then\n"
        "  zade_python=\"$repo_root/.venv/Scripts/python.exe\"\n"
        "elif [ -x \"$repo_root/.venv/bin/python\" ]; then\n"
        "  zade_python=\"$repo_root/.venv/bin/python\"\n"
        "else\n"
        "  zade_python=\"${PYTHON:-python}\"\n"
        "fi\n"
        "\"$zade_python\" -m cofounder_kernel self-knowledge --refresh --repo-root .\n"
        "git add context/self/zade.md\n"
        "\"$zade_python\" -m cofounder_kernel self-knowledge --check --repo-root .\n"
        f"{HOOK_END}\n"
    )


def _replace_managed_block(existing: str, managed_block: str) -> str:
    start = existing.find(HOOK_START)
    end = existing.find(HOOK_END)
    if start == -1 or end == -1 or end < start:
        raise HookInstallError("Existing managed hook markers are incomplete or malformed.")
    end += len(HOOK_END)
    while end < len(existing) and existing[end] in "\r\n":
        end += 1
    return f"{existing[:start]}{managed_block}{existing[end:]}"


def _make_executable(path: Path) -> None:
    if os.name == "nt":
        return
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


if __name__ == "__main__":
    main()
