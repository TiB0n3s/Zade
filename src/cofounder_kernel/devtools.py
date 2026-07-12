from __future__ import annotations

import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import KernelConfig
from .db import KernelDatabase, WorkItem
from .handlers import ActionHandlerRegistry, _resolve_allowed_path, _slug, _work_item_summary


OUTPUT_TAIL_CHARS = 4000
MAX_ARGS = 20
MAX_ARG_LEN = 200
BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,120}$")
DRAFT_KINDS = {"email", "pr", "message", "note"}


def allowed_commands(python: str) -> dict[str, list[str]]:
    """Allowlisted developer commands. Base argv is fixed; only extra args vary.

    Every command is either read-only diagnostics or a local test/lint run.
    Nothing here mutates external state or the repository.
    """
    return {
        "pytest": [python, "-m", "pytest", "-q"],
        "ruff-check": [python, "-m", "ruff", "check"],
        "ruff-format-check": [python, "-m", "ruff", "format", "--check"],
        "git-status": ["git", "status", "--short", "--branch"],
        "git-diff": ["git", "diff"],
        "git-diff-staged": ["git", "diff", "--staged"],
        "git-log": ["git", "log", "--oneline", "-20"],
        "python-version": [python, "--version"],
    }


class DevToolsHandlers:
    """Concrete co-founder action handlers on the approved-dispatch substrate.

    These let Zade actually do developer work — run tests and lint, inspect and
    branch and commit a repo, and draft outbound messages — instead of only
    advising. Every action reaches this code only through a work item that the
    founder approved with the typed confirmation phrase, so the existing
    authority and approval gates are the security boundary. On top of that:
    commands are allowlisted, execution is confined to the configured
    workspace, commits refuse the default branch by default, and drafts are
    written but never sent.
    """

    def __init__(
        self,
        *,
        db: KernelDatabase,
        config: KernelConfig,
        workspace_root: Path | None = None,
        python_executable: str | None = None,
    ):
        self.db = db
        self.config = config
        self.workspace_root = Path(workspace_root or config.devtools.workspace_root).resolve(strict=False)
        self.python = python_executable or _default_python()

    def register_into(self, registry: ActionHandlerRegistry) -> list[str]:
        actions = {
            "dev.command.run": ("Run an allowlisted local dev command (tests, lint, git diagnostics) in the workspace.", self.run_command),
            "dev.git.branch": ("Create or switch to a git branch in the workspace.", self.git_branch),
            "dev.git.commit": ("Stage and commit local changes in the workspace (refuses the default branch by default).", self.git_commit),
            "dev.draft.write": ("Write an email/PR/message draft under the local drafts folder. Never sends.", self.write_draft),
        }
        for action, (description, handler) in actions.items():
            registry.register(action, description, handler)
        return sorted(actions)

    # ---- handlers ----
    def run_command(self, item: WorkItem) -> dict[str, Any]:
        metadata = item.metadata or {}
        name = str(metadata.get("command", "")).strip()
        allowed = allowed_commands(self.python)
        if name not in allowed:
            raise ValueError(f"Command not allowed: {name!r}. Allowed: {', '.join(sorted(allowed))}")
        extra = _validate_args(metadata.get("args", []))
        argv = allowed[name] + extra
        completed = self._run(argv)
        ok = completed.returncode == 0
        result = {
            "handler": "dev.command.run",
            "status": "ok",
            "command": name,
            "argv": argv,
            "exit_code": completed.returncode,
            "ok": ok,
            "stdout_tail": _tail(completed.stdout),
            "stderr_tail": _tail(completed.stderr),
        }
        self.db.audit(
            actor="approved-handler",
            action="dev.command.run",
            target=name,
            permission_tier=item.permission_tier,
            status="ok" if ok else "nonzero_exit",
            details={"work_item": _work_item_summary(item), "exit_code": completed.returncode, "argv": argv},
        )
        return result

    def git_branch(self, item: WorkItem) -> dict[str, Any]:
        self._require_git_repo()
        metadata = item.metadata or {}
        name = str(metadata.get("name", "")).strip()
        if not BRANCH_NAME_RE.match(name) or ".." in name:
            raise ValueError("dev.git.branch requires a safe branch name (letters, digits, . _ / -).")
        checkout = bool(metadata.get("checkout", True))
        base = str(metadata.get("base", "")).strip()
        if base and (not BRANCH_NAME_RE.match(base) or ".." in base):
            raise ValueError("Invalid base ref.")
        argv = ["git", "checkout", "-b", name] if checkout else ["git", "branch", name]
        if base:
            argv.append(base)
        completed = self._run(argv)
        if completed.returncode != 0:
            raise ValueError(f"git branch failed: {_tail(completed.stderr) or _tail(completed.stdout)}")
        current = self._current_branch()
        self.db.audit(
            actor="approved-handler",
            action="dev.git.branch",
            target=name,
            permission_tier=item.permission_tier,
            status="ok",
            details={"work_item": _work_item_summary(item), "checkout": checkout, "current_branch": current},
        )
        return {"handler": "dev.git.branch", "status": "ok", "branch": name, "checked_out": checkout, "current_branch": current}

    def git_commit(self, item: WorkItem) -> dict[str, Any]:
        self._require_git_repo()
        metadata = item.metadata or {}
        message = str(metadata.get("message", "")).strip()
        if not message:
            raise ValueError("dev.git.commit requires a commit message.")
        branch = self._current_branch()
        allow_default = bool(metadata.get("allow_default_branch", False))
        if branch == self.config.devtools.default_branch and not allow_default:
            raise ValueError(
                f"Refusing to commit on the default branch '{branch}'. "
                "Create a branch first, or set metadata.allow_default_branch."
            )
        paths = metadata.get("paths")
        add_argv = ["git", "add"] + (_validate_args(paths) if paths else ["-A"])
        add = self._run(add_argv)
        if add.returncode != 0:
            raise ValueError(f"git add failed: {_tail(add.stderr) or _tail(add.stdout)}")
        staged = self._run(["git", "diff", "--staged", "--name-only"])
        if not staged.stdout.strip():
            raise ValueError("Nothing staged to commit.")
        commit = self._run(["git", "commit", "-m", message])
        if commit.returncode != 0:
            raise ValueError(f"git commit failed: {_tail(commit.stderr) or _tail(commit.stdout)}")
        sha = self._run(["git", "rev-parse", "HEAD"]).stdout.strip()
        files = [line for line in staged.stdout.splitlines() if line.strip()]
        self.db.audit(
            actor="approved-handler",
            action="dev.git.commit",
            target=sha,
            permission_tier=item.permission_tier,
            status="ok",
            details={"work_item": _work_item_summary(item), "branch": branch, "files": files, "message": message[:200]},
        )
        return {
            "handler": "dev.git.commit",
            "status": "ok",
            "sha": sha,
            "branch": branch,
            "files": files,
            "message": message,
        }

    def write_draft(self, item: WorkItem) -> dict[str, Any]:
        metadata = item.metadata or {}
        kind = str(metadata.get("kind", "message")).strip().lower()
        if kind not in DRAFT_KINDS:
            raise ValueError(f"Draft kind must be one of: {', '.join(sorted(DRAFT_KINDS))}")
        title = str(metadata.get("title") or item.title).strip()
        content = str(metadata.get("content") or item.detail).strip()
        if not content:
            raise ValueError("dev.draft.write requires draft content.")
        recipient = str(metadata.get("to", "")).strip()
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        drafts_dir = self.config.paths.hot_root / "Zade" / "drafts"
        path = _resolve_allowed_path(str(drafts_dir / f"{stamp}-{kind}-{_slug(title)}.md"), self.config)
        path.parent.mkdir(parents=True, exist_ok=True)
        header = f"# {kind.upper()} draft: {title}\n\n"
        if recipient:
            header += f"To: {recipient}\n\n"
        boundary = (
            "\n\n---\n"
            "DRAFT ONLY. Zade prepared this and has NOT sent it. "
            "Sending is a separate human action outside the local kernel.\n"
        )
        body = header + content + boundary
        path.write_text(body, encoding="utf-8")
        memory_id = self.db.add_memory(
            kind="draft",
            title=f"{kind} draft: {title}",
            content=body,
            source="approved-handler",
            metadata={"path": str(path), "draft_kind": kind, "to": recipient, "work_item_id": item.id, "sent": False},
        )
        self.db.audit(
            actor="approved-handler",
            action="dev.draft.write",
            target=str(path),
            permission_tier=item.permission_tier,
            status="ok",
            details={"work_item": _work_item_summary(item), "kind": kind, "sent": False},
        )
        return {"handler": "dev.draft.write", "status": "ok", "path": str(path), "kind": kind, "sent": False, "memory_id": memory_id}

    # ---- internals ----
    def _run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        if not self.workspace_root.is_dir():
            raise ValueError(f"Workspace root does not exist: {self.workspace_root}")
        try:
            return subprocess.run(  # noqa: S603 - allowlisted argv, no shell, confined cwd
                argv,
                cwd=str(self.workspace_root),
                capture_output=True,
                text=True,
                timeout=self.config.devtools.command_timeout_seconds,
                shell=False,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ValueError(f"Executable not found for dev action: {argv[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ValueError(
                f"Dev command timed out after {self.config.devtools.command_timeout_seconds:.0f}s."
            ) from exc

    def _require_git_repo(self) -> None:
        if not (self.workspace_root / ".git").exists():
            raise ValueError(f"Workspace is not a git repository: {self.workspace_root}")

    def _current_branch(self) -> str:
        completed = self._run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
        return completed.stdout.strip() or "(unknown)"


def _validate_args(args: Any) -> list[str]:
    if args in (None, ""):
        return []
    if not isinstance(args, (list, tuple)):
        raise ValueError("args must be a list of strings.")
    if len(args) > MAX_ARGS:
        raise ValueError(f"Too many args (max {MAX_ARGS}).")
    validated = []
    for raw in args:
        arg = str(raw)
        if len(arg) > MAX_ARG_LEN or "\n" in arg or "\r" in arg:
            raise ValueError("Invalid arg (too long or contains a newline).")
        if ".." in arg or arg.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", arg):
            raise ValueError(f"Refusing path-traversal or absolute path in arg: {arg!r}")
        # Keep the "read-only diagnostics" guarantee honest: reject flags that
        # make otherwise-read-only tools (pytest, ruff, git) write files.
        lowered = arg.lower()
        if lowered.startswith("-") and any(flag in lowered for flag in _WRITE_FLAG_FRAGMENTS):
            raise ValueError(f"Refusing a file-writing flag in a read-only dev command: {arg!r}")
        validated.append(arg)
    return validated


# Flag fragments that make an allowlisted read-only command produce output files.
_WRITE_FLAG_FRAGMENTS = (
    "output",
    "junit",
    "junitxml",
    "basetemp",
    "resultlog",
    "result-log",
    "report",
    "outfile",
    "logfile",
    "log-file",
    "cov-report",
    "export",
    "write",
    "-o=",
)


def _tail(text: str | None) -> str:
    text = (text or "").strip()
    if len(text) <= OUTPUT_TAIL_CHARS:
        return text
    return "...(truncated)...\n" + text[-OUTPUT_TAIL_CHARS:]


def _default_python() -> str:
    import sys

    return sys.executable or "python"
