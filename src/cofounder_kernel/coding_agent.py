"""Native local coding agent — Zade's own tool-calling build loop on Ollama.

This replaces the external Claude Code CLI as the default engine for delegated
build work. The selected LOCAL model (resolved by the inventory service via a
real tool-call probe) receives the Zade build profile as a system message, the
target workspace's own instruction files, and a small belt of REAL tools backed
by the kernel: list/read/search files, controlled edits, an allowlisted local
command runner, and git status/diff. The loop is bounded, every tool execution
is validated at the boundary and audited, and every path is confined to the
workspace root.

Provider posture: the only model this loop can ever call is the loopback Ollama
client it was constructed with. If the model cannot drive the tools, the run
returns a local capability error naming eligible installed models — it never
escalates to a cloud provider.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import KernelConfig
from .db import KernelDatabase
from .inventory import ModelInventoryError, ModelInventoryService
from .ollama import OllamaClient, OllamaError
from .prompts import PromptProfileRegistry, PromptRuntimeBindings

# Bounds: a confused model must not be able to spin the loop, flood the prompt,
# or hold a subprocess open indefinitely.
DEFAULT_MAX_ROUNDS = 12
MAX_CALLS_PER_ROUND = 4
MAX_RESULT_CHARS = 8000
MAX_FILE_READ_CHARS = 24_000
MAX_FILE_WRITE_CHARS = 200_000
MAX_LIST_ENTRIES = 400
MAX_SEARCH_MATCHES = 80
COMMAND_TIMEOUT_SECONDS = 180.0
MAX_COMMAND_OUTPUT_CHARS = 12_000

# Allowlisted first-argv tokens for run_command. Deliberately small: enough to
# run tests and inspect a Python workspace. Everything else is refused at the
# execution boundary regardless of what the prompt or model claims.
COMMAND_ALLOWLIST = ("python", "python3", "py", "pytest", "pip", "uv", "git")

_INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", "Claude.md", "claude.md", "README.md")
_MAX_INSTRUCTION_CHARS = 4000

_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache"}


class CodingAgentError(RuntimeError):
    pass


class WorkspaceViolation(CodingAgentError):
    """A tool argument tried to escape the workspace boundary."""


@dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], dict[str, Any]]
    writes: bool = False


class CodingAgentService:
    """Bounded, audited, workspace-confined coding loop on the local model."""

    def __init__(
        self,
        *,
        config: KernelConfig,
        db: KernelDatabase,
        ollama: OllamaClient,
        inventory: ModelInventoryService | None = None,
    ):
        self.config = config
        self.db = db
        self.ollama = ollama
        self.inventory = inventory or ModelInventoryService(config=config, ollama=ollama)
        self.prompt_profiles = PromptProfileRegistry()

    # ---- public ------------------------------------------------------------
    def available(self) -> bool:
        return True

    def resolve_model(self) -> str:
        return self.inventory.resolve_coding_agent_model()

    def run(
        self,
        *,
        task: str,
        workspace: str | Path | None = None,
        context: str = "",
        max_rounds: int | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Run the coding loop. Returns a structured result; raises only on
        programmer error. Model/tool failures come back as status fields so the
        caller (delegation dispatch) can file them without a 500."""
        task = (task or "").strip()
        if not task:
            raise ValueError("The coding agent needs a task.")
        root = self._workspace_root(workspace)
        try:
            selected_model = (model or "").strip() or self.resolve_model()
        except ModelInventoryError as exc:
            return {
                "ok": False,
                "status": "capability_error",
                "error": str(exc),
                "model": "",
                "rounds": 0,
                "steps": [],
                "changed_files": [],
                "response": "",
            }
        tools = self._build_tools(root)
        schemas = _tool_schemas(tools)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_message(root, tools)},
        ]
        if context.strip():
            messages.append(
                {"role": "user", "content": f"Context from the founder's conversation:\n{context.strip()[:6000]}"}
            )
            messages.append(
                {"role": "assistant", "content": "Understood. I have the context. Give me the task."}
            )
        messages.append({"role": "user", "content": task})

        rounds_cap = max(1, int(max_rounds or DEFAULT_MAX_ROUNDS))
        steps: list[dict[str, Any]] = []
        changed_files: set[str] = set()
        rounds = 0
        final_text = ""
        status = "ok"
        error = ""
        used_tools = False

        for round_index in range(rounds_cap + 1):
            allow_tools = round_index < rounds_cap
            try:
                generated = self.ollama.chat(
                    messages=messages,
                    model=selected_model,
                    think=self.config.ollama.think_for_role("coding"),
                    temperature=0.1,
                    num_predict=2048,
                    tools=schemas if allow_tools else None,
                )
            except OllamaError as exc:
                if round_index == 0 and "does not support tools" in str(exc).lower():
                    return {
                        "ok": False,
                        "status": "capability_error",
                        "error": (
                            f"Model {selected_model!r} rejected native tools: {str(exc)[:200]}. "
                            "No cloud escalation was attempted. Set [ollama] coding_agent_model "
                            "to a tool-capable installed model."
                        ),
                        "model": selected_model,
                        "rounds": rounds,
                        "steps": steps,
                        "changed_files": sorted(changed_files),
                        "response": "",
                    }
                status = "model_error"
                error = str(exc)[:400]
                break
            final_text = generated.response or final_text
            tool_calls = _extract_tool_calls(generated.raw) if allow_tools else []
            if not tool_calls:
                break
            used_tools = True
            rounds += 1
            messages.append(
                {"role": "assistant", "content": generated.response or "", "tool_calls": tool_calls}
            )
            for call in tool_calls[:MAX_CALLS_PER_ROUND]:
                name, arguments = _parse_tool_call(call)
                started = time.perf_counter()
                result = self._execute(tools, name, arguments)
                latency_ms = int((time.perf_counter() - started) * 1000)
                if result.pop("_changed_file", None):
                    changed_files.add(str(result.get("path", "")))
                steps.append(
                    {
                        "tool": name,
                        "arguments": _redact_arguments(arguments),
                        "ok": bool(result.get("ok", True)),
                        "latency_ms": latency_ms,
                        "round": rounds,
                    }
                )
                messages.append(
                    {"role": "tool", "tool_name": name, "content": _render_result(result)}
                )
            dropped = len(tool_calls) - min(len(tool_calls), MAX_CALLS_PER_ROUND)
            if dropped > 0:
                messages.append(
                    {
                        "role": "tool",
                        "tool_name": "kernel",
                        "content": json.dumps(
                            {"note": f"{dropped} tool call(s) skipped: per-round cap is {MAX_CALLS_PER_ROUND}."}
                        ),
                    }
                )

        result = {
            "ok": status == "ok",
            "status": status,
            "error": error,
            "model": selected_model,
            "provider": self.ollama.provider_info(),
            "workspace": str(root),
            "rounds": rounds,
            "used_tools": used_tools,
            "steps": steps,
            "changed_files": sorted(changed_files),
            "response": final_text.strip(),
        }
        # Redacted per-run telemetry: role, provider, model, endpoint, outcome —
        # never prompts, file contents, or secrets.
        try:
            self.db.record_model_call(
                operation="coding_agent.run",
                model=selected_model,
                role="coding",
                status="ok" if result["ok"] else "error",
                latency_ms=0,
                prompt_chars=len(task),
                response_chars=len(final_text),
                think=None,
                error=error,
                metadata={
                    "provider": result["provider"],
                    "rounds": rounds,
                    "used_tools": used_tools,
                    "changed_files": result["changed_files"],
                    "workspace": str(root),
                },
            )
        except Exception:  # noqa: BLE001 - telemetry must not break the run
            pass
        return result

    # ---- workspace ------------------------------------------------------------
    def _workspace_root(self, workspace: str | Path | None) -> Path:
        configured = getattr(self.config.delegation, "workspace_root", "") or ""
        raw = workspace or configured
        if not str(raw).strip():
            raise CodingAgentError(
                "No workspace configured for the coding agent. Set [delegation] workspace_root "
                "or pass a workspace path."
            )
        root = Path(str(raw)).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _resolve_in_workspace(self, root: Path, raw_path: str) -> Path:
        candidate = (raw_path or "").strip()
        if not candidate:
            raise WorkspaceViolation("A file path is required.")
        path = Path(candidate)
        resolved = (path if path.is_absolute() else root / path).resolve()
        if resolved != root and root not in resolved.parents:
            raise WorkspaceViolation(
                f"Path {candidate!r} escapes the workspace root. Use paths inside the workspace."
            )
        return resolved

    # ---- system message -------------------------------------------------------
    def _system_message(self, root: Path, tools: dict[str, AgentTool]) -> str:
        profile_text = ""
        try:
            rendered = self.prompt_profiles.render_profile(
                "build",
                bindings=PromptRuntimeBindings(
                    zade_home=Path(self.config.paths.hot_root),
                    skills_root=Path(self.config.skills.source_dir),
                    now=datetime.now(timezone.utc),
                ),
            )
            profile_text = rendered.content
        except Exception:
            profile_text = (
                "You are Zade's local build engineer: precise, minimal-diff, test-driven."
            )
        instructions = self._workspace_instructions(root)
        tool_lines = "\n".join(
            f"- {tool.name}: {tool.description}" for tool in sorted(tools.values(), key=lambda t: t.name)
        )
        parts = [
            profile_text,
            "----------  Local coding agent run  ----------",
            f"You are working INSIDE the workspace directory: {root}",
            "All file paths are relative to this workspace. You cannot read or write outside it.",
            "You have callable tools this run. They execute REAL local operations:",
            tool_lines,
            (
                "Work in small verified steps: read before you edit, prefer replace_in_file for "
                "surgical changes, run the focused test after an edit, and finish with a short "
                "summary of what changed and the test result. Never claim an action you did not "
                "perform with a tool. When you are done, reply with plain text and no tool calls."
            ),
        ]
        if instructions:
            parts.append("----------  Workspace instructions  ----------\n" + instructions)
        return "\n\n".join(part for part in parts if part.strip())

    def _workspace_instructions(self, root: Path) -> str:
        chunks: list[str] = []
        for name in _INSTRUCTION_FILES:
            candidate = root / name
            try:
                if candidate.is_file():
                    text = candidate.read_text(encoding="utf-8", errors="replace").strip()
                    if text:
                        chunks.append(f"# {name}\n{text[:_MAX_INSTRUCTION_CHARS]}")
            except OSError:
                continue
        return "\n\n".join(chunks)[: 2 * _MAX_INSTRUCTION_CHARS]

    # ---- tools -----------------------------------------------------------------
    def _build_tools(self, root: Path) -> dict[str, AgentTool]:
        tools: dict[str, AgentTool] = {}

        def add(tool: AgentTool) -> None:
            tools[tool.name] = tool

        add(
            AgentTool(
                name="list_files",
                description="List files under the workspace (relative paths), optionally under a subdirectory.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Optional subdirectory (default workspace root)."}
                    },
                    "required": [],
                },
                handler=lambda args: self._tool_list_files(root, args),
            )
        )
        add(
            AgentTool(
                name="read_file",
                description="Read a workspace file and return its text content.",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "Relative file path."}},
                    "required": ["path"],
                },
                handler=lambda args: self._tool_read_file(root, args),
            )
        )
        add(
            AgentTool(
                name="search_files",
                description="Search workspace file contents for a regex/substring; returns file, line number, and line.",
                parameters={
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "Regex (falls back to literal on invalid regex)."},
                        "path": {"type": "string", "description": "Optional subdirectory to search."},
                    },
                    "required": ["pattern"],
                },
                handler=lambda args: self._tool_search(root, args),
            )
        )
        add(
            AgentTool(
                name="write_file",
                description="Create or overwrite a workspace file with the given content.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative file path."},
                        "content": {"type": "string", "description": "Full new file content."},
                    },
                    "required": ["path", "content"],
                },
                handler=lambda args: self._tool_write_file(root, args),
                writes=True,
            )
        )
        add(
            AgentTool(
                name="replace_in_file",
                description=(
                    "Surgical edit: replace an EXACT existing text snippet in a workspace file with new text. "
                    "The old text must appear exactly once."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative file path."},
                        "old_text": {"type": "string", "description": "Exact text to replace (must match once)."},
                        "new_text": {"type": "string", "description": "Replacement text."},
                    },
                    "required": ["path", "old_text", "new_text"],
                },
                handler=lambda args: self._tool_replace(root, args),
                writes=True,
            )
        )
        add(
            AgentTool(
                name="run_command",
                description=(
                    "Run an allowlisted local command inside the workspace (argv array, no shell). "
                    f"Allowed programs: {', '.join(COMMAND_ALLOWLIST)}. Use this to run tests "
                    "(e.g. [\"python\", \"-m\", \"pytest\", \"tests/test_x.py\", \"-q\"])."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "argv": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Command as an argv array, e.g. [\"python\",\"-m\",\"pytest\",\"-q\"].",
                        }
                    },
                    "required": ["argv"],
                },
                handler=lambda args: self._tool_run_command(root, args),
                writes=True,
            )
        )
        add(
            AgentTool(
                name="git_status",
                description="Show git status --porcelain for the workspace (empty when not a git repo).",
                parameters={"type": "object", "properties": {}, "required": []},
                handler=lambda args: self._tool_git(root, ["status", "--porcelain"]),
            )
        )
        add(
            AgentTool(
                name="git_diff",
                description="Show the current git diff for the workspace (empty when not a git repo).",
                parameters={"type": "object", "properties": {}, "required": []},
                handler=lambda args: self._tool_git(root, ["diff"]),
            )
        )
        return tools

    def _execute(self, tools: dict[str, AgentTool], name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = tools.get(name)
        if tool is None:
            self._audit(name, ok=False, writes=False, details={"reason": "unknown_tool"})
            return {"ok": False, "error": f"unknown_tool: {name}", "available_tools": sorted(tools)}
        try:
            result = tool.handler(arguments or {})
            if not isinstance(result, dict):
                result = {"ok": True, "value": result}
            ok = bool(result.get("ok", True))
        except WorkspaceViolation as exc:
            result = {"ok": False, "error": str(exc)}
            ok = False
        except Exception as exc:  # noqa: BLE001 - a tool failure feeds back to the model
            result = {"ok": False, "error": str(exc)[:400]}
            ok = False
        self._audit(name, ok=ok, writes=tool.writes, details={"arguments": _redact_arguments(arguments)})
        return result

    def _audit(self, tool_name: str, *, ok: bool, writes: bool, details: dict[str, Any]) -> None:
        try:
            self.db.audit(
                actor="coding_agent",
                action="coding_agent.tool_call",
                target=tool_name,
                permission_tier="L2_FILE_WRITE" if writes else "L0_READ",
                status="ok" if ok else "error",
                details=details,
            )
        except Exception:  # noqa: BLE001 - auditing must not break the loop
            pass

    # ---- tool implementations ---------------------------------------------------
    def _tool_list_files(self, root: Path, args: dict[str, Any]) -> dict[str, Any]:
        base = self._resolve_in_workspace(root, str(args.get("path") or "."))
        if not base.exists():
            return {"ok": False, "error": f"not found: {args.get('path')}"}
        entries: list[str] = []
        for path in sorted(base.rglob("*")):
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            if path.is_file():
                entries.append(str(path.relative_to(root)).replace("\\", "/"))
            if len(entries) >= MAX_LIST_ENTRIES:
                entries.append(f"...[truncated at {MAX_LIST_ENTRIES} entries]")
                break
        return {"ok": True, "files": entries}

    def _tool_read_file(self, root: Path, args: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_in_workspace(root, str(args.get("path") or ""))
        if not path.is_file():
            return {"ok": False, "error": f"not a file: {args.get('path')}"}
        text = path.read_text(encoding="utf-8", errors="replace")
        truncated = len(text) > MAX_FILE_READ_CHARS
        return {
            "ok": True,
            "path": str(path.relative_to(root)).replace("\\", "/"),
            "content": text[:MAX_FILE_READ_CHARS],
            "truncated": truncated,
        }

    def _tool_search(self, root: Path, args: dict[str, Any]) -> dict[str, Any]:
        pattern = str(args.get("pattern") or "")
        if not pattern:
            return {"ok": False, "error": "pattern is required"}
        try:
            regex = re.compile(pattern)
        except re.error:
            regex = re.compile(re.escape(pattern))
        base = self._resolve_in_workspace(root, str(args.get("path") or "."))
        matches: list[dict[str, Any]] = []
        for path in sorted(base.rglob("*")):
            if any(part in _SKIP_DIRS for part in path.parts) or not path.is_file():
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for index, line in enumerate(lines, start=1):
                if regex.search(line):
                    matches.append(
                        {
                            "file": str(path.relative_to(root)).replace("\\", "/"),
                            "line": index,
                            "text": line.strip()[:300],
                        }
                    )
                    if len(matches) >= MAX_SEARCH_MATCHES:
                        return {"ok": True, "matches": matches, "truncated": True}
        return {"ok": True, "matches": matches, "truncated": False}

    def _tool_write_file(self, root: Path, args: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_in_workspace(root, str(args.get("path") or ""))
        content = str(args.get("content") or "")
        if len(content) > MAX_FILE_WRITE_CHARS:
            return {"ok": False, "error": f"content exceeds {MAX_FILE_WRITE_CHARS} chars"}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        rel = str(path.relative_to(root)).replace("\\", "/")
        return {"ok": True, "path": rel, "bytes": len(content.encode('utf-8')), "_changed_file": True}

    def _tool_replace(self, root: Path, args: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_in_workspace(root, str(args.get("path") or ""))
        if not path.is_file():
            return {"ok": False, "error": f"not a file: {args.get('path')}"}
        old_text = str(args.get("old_text") or "")
        new_text = str(args.get("new_text") or "")
        if not old_text:
            return {"ok": False, "error": "old_text is required"}
        text = path.read_text(encoding="utf-8", errors="replace")
        occurrences = text.count(old_text)
        if occurrences == 0:
            return {"ok": False, "error": "old_text not found in file (must match exactly)"}
        if occurrences > 1:
            return {"ok": False, "error": f"old_text appears {occurrences} times; make it unique"}
        path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        rel = str(path.relative_to(root)).replace("\\", "/")
        return {"ok": True, "path": rel, "_changed_file": True}

    def _tool_run_command(self, root: Path, args: dict[str, Any]) -> dict[str, Any]:
        argv = args.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            return {"ok": False, "error": "argv must be a non-empty array of strings"}
        program = Path(argv[0]).name.lower()
        program = program[:-4] if program.endswith(".exe") else program
        if program not in COMMAND_ALLOWLIST:
            return {
                "ok": False,
                "error": f"program {argv[0]!r} is not allowlisted. Allowed: {', '.join(COMMAND_ALLOWLIST)}",
            }
        # Pin Python-family programs to the kernel's own interpreter so the run
        # sees the kernel venv (pytest included) instead of whatever bare
        # 'python' resolves to on PATH. git stays git.
        resolved = list(argv)
        if program in {"python", "python3", "py"}:
            resolved[0] = sys.executable
        elif program == "pytest":
            resolved = [sys.executable, "-m", "pytest", *argv[1:]]
        elif program == "pip":
            resolved = [sys.executable, "-m", "pip", *argv[1:]]
        try:
            completed = subprocess.run(
                resolved,
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=COMMAND_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"command timed out after {COMMAND_TIMEOUT_SECONDS}s"}
        except FileNotFoundError:
            return {"ok": False, "error": f"program not found: {argv[0]!r}"}
        stdout = (completed.stdout or "")[:MAX_COMMAND_OUTPUT_CHARS]
        stderr = (completed.stderr or "")[:MAX_COMMAND_OUTPUT_CHARS]
        return {
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "stdout": stdout,
            "stderr": stderr,
        }

    def _tool_git(self, root: Path, git_args: list[str]) -> dict[str, Any]:
        return self._tool_run_command(root, {"argv": ["git", *git_args]})


# ---- helpers (shared shapes with investigation.py) ----------------------------


def _tool_schemas(tools: dict[str, AgentTool]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
        }
        for t in sorted(tools.values(), key=lambda item: item.name)
    ]


def _extract_tool_calls(raw: dict[str, Any]) -> list[dict[str, Any]]:
    message = raw.get("message") or {}
    if not isinstance(message, dict):
        return []
    calls = message.get("tool_calls") or []
    return [call for call in calls if isinstance(call, dict)]


def _parse_tool_call(call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    function = call.get("function") or {}
    if not isinstance(function, dict):
        return "", {}
    name = str(function.get("name") or "").strip()
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    return name, arguments


def _render_result(result: dict[str, Any]) -> str:
    rendered = json.dumps(result, default=str)
    if len(rendered) <= MAX_RESULT_CHARS:
        return rendered
    return rendered[:MAX_RESULT_CHARS] + "... [truncated]"


def _redact_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Telemetry-safe argument summary: keep shapes and paths, drop bulk content."""
    redacted: dict[str, Any] = {}
    for key, value in (arguments or {}).items():
        if isinstance(value, str) and len(value) > 200:
            redacted[key] = f"<{len(value)} chars>"
        else:
            redacted[key] = value
    return redacted
