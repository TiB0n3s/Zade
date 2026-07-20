"""Zade's bounded tool-calling build loop.

This replaces the external Claude Code CLI as the default engine for delegated
build work. The selected LOCAL model (resolved by the inventory service via a
real tool-call probe) receives the Zade build profile as a system message, the
target workspace's own instruction files, and a small belt of REAL tools backed
by the kernel: list/read/search files, controlled edits, an allowlisted local
command runner, and git status/diff. The loop is bounded, every tool execution
is validated at the boundary and audited, and every path is confined to the
workspace root.

Provider posture: Ollama remains the default. An explicitly injected model
client can drive the same confined tool loop, but this service never selects a
cloud provider or falls back between providers on its own.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import KernelConfig
from .command_runner import (
    CommandPolicyError,
    CommandRequest,
    normalize_coding_agent_command,
)
from .db import KernelDatabase
from .inventory import ModelInventoryError, ModelInventoryService
from .model_client import CodingModelClient, CodingModelError
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
COMMAND_TIMEOUT_SECONDS = 420.0
MAX_COMMAND_OUTPUT_CHARS = 12_000
MAX_VERIFY_OUTPUT_CHARS = 4000

# Goal → Act → Check → Repeat: when the kernel's own check fails, the real
# failing output is fed back to the model for at most this many repair rounds
# before the run reports, honestly, that the work did not verify.
MAX_REPAIR_ROUNDS = 2

# Workspace snapshots bound: beyond this many files (outside skip dirs) the
# before/after diff is skipped rather than made expensive, and the report
# falls back to write-tool tracking.
MAX_SNAPSHOT_FILES = 5000
# Syntax-fallback verification never fans out over more than this many
# changed files (command-driven churn can be large).
MAX_VERIFY_TARGETS = 40

# Allowlisted first-argv tokens for run_command. Deliberately small: enough to
# run offline checks in Python, Node, and Flutter workspaces. npx stays OFF the list —
# it executes arbitrary packages by design. Everything else is refused at the
# execution boundary regardless of what the prompt or model claims.
COMMAND_ALLOWLIST = (
    "python",
    "python3",
    "py",
    "pytest",
    "uv",
    "git",
    "npm",
    "node",
    "flutter",
)

_INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", "Claude.md", "claude.md", "README.md")
_MAX_INSTRUCTION_CHARS = 4000

_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache"}

# ask_founder questions that are really about this run's own tool limits — a
# refused command, a missing tool — are bounced back with instructions to
# route around the boundary instead of interrupting the founder.
_CAPABILITY_BOUNDARY_QUESTION_RE = re.compile(
    r"""(?ix)
    (?:\b(?:command|tool|program|npx|npm|binary|executable)\b
        [^?]{0,80}?
        \b(?:not\s+(?:allowed|allowlisted|permitted|available|supported)|blocked|refused|unavailable|disallowed|forbidden)\b)
    |
    (?:\b(?:not\s+(?:allowed|allowlisted|permitted)|blocked|refused|disallowed|forbidden)\b
        [^?]{0,40}?
        \b(?:command|tool|program|run(?:ning)?)\b)
    """
)

# Workspace mechanics — a path conflict, a stray file where a directory should
# be, "already exists" — are the run's own state, not founder decisions. Kept
# deliberately narrow: overwrite/delete questions about the founder's real
# content still go through.
_WORKSPACE_MECHANICS_QUESTION_RE = re.compile(
    r"""(?ix)
    \b(?:director(?:y|ies)|folder|file|path)\b
    [^?]{0,100}?
    (?:
        \bis\s+a\s+file\b
        | \bnot\s+a\s+(?:folder|directory)\b
        | \bisn'?t\s+(?:properly\s+)?(?:created|a\s+(?:folder|directory))\b
        | \balready\s+exists\b
        | \bconflict\w*\b
    )
    """
)


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
    """Bounded, audited, workspace-confined coding loop."""

    def __init__(
        self,
        *,
        config: KernelConfig,
        db: KernelDatabase,
        ollama: OllamaClient,
        model_client: CodingModelClient | None = None,
        inventory: ModelInventoryService | None = None,
        notifier: Any | None = None,
        command_runner: Any | None = None,
    ):
        self.config = config
        self.db = db
        self.ollama = ollama
        self.model_client = model_client or ollama
        self.inventory = inventory or ModelInventoryService(config=config, ollama=ollama)
        self.prompt_profiles = PromptProfileRegistry()
        # Optional NotificationBus: send_progress raises native toasts so the
        # founder sees milestones during long runs without the run ending.
        self.notifier = notifier
        self.command_runner = command_runner

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
        verify_always: bool = False,
        write_allowlist: tuple[str, ...] | None = None,
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
        except (ModelInventoryError, OllamaError) as exc:
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
        question_box: dict[str, Any] = {}
        progress_notes: list[str] = []
        tools = self._build_tools(
            root,
            question_box=question_box,
            progress_notes=progress_notes,
            write_allowlist=write_allowlist,
        )
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
        state = {"rounds": 0, "final_text": "", "status": "ok", "error": "", "used_tools": False}
        before_snapshot = _workspace_snapshot(root)

        def real_change_targets() -> list[str]:
            """Everything actually changed so far — write-tool edits plus
            command-driven mutations visible in the workspace diff."""
            targets = set(changed_files)
            if before_snapshot is not None:
                now = _workspace_snapshot(root)
                if now is not None:
                    diff = _diff_snapshots(before_snapshot, now)
                    targets |= set(diff["added"]) | set(diff["modified"])
            return sorted(targets)[:MAX_VERIFY_TARGETS]

        def advance(cap: int) -> None:
            """Drive model rounds until the model stops calling tools, raises a
            founder decision, errors, or the cap elapses. Mutates the shared
            run state so the verify/repair phase can call it again."""
            for round_index in range(cap + 1):
                allow_tools = round_index < cap
                try:
                    generated = self.model_client.chat(
                        messages=messages,
                        model=selected_model,
                        think=self.config.ollama.think_for_role("coding"),
                        temperature=0.1,
                        num_predict=2048,
                        tools=schemas if allow_tools else None,
                    )
                except (CodingModelError, OllamaError) as exc:
                    if (
                        not state["used_tools"]
                        and state["rounds"] == 0
                        and "does not support tools" in str(exc).lower()
                    ):
                        state["status"] = "capability_error"
                        if self.model_client is self.ollama:
                            state["error"] = (
                                f"Model {selected_model!r} rejected native tools: {str(exc)[:200]}. "
                                "No cloud escalation was attempted. Set [ollama] coding_agent_model "
                                "to a tool-capable installed model."
                            )
                        else:
                            state["error"] = (
                                f"Model {selected_model!r} rejected native tools: {str(exc)[:200]}. "
                                "No provider fallback was attempted."
                            )
                        return
                    state["status"] = "model_error"
                    state["error"] = str(exc)[:400]
                    return
                state["final_text"] = generated.response or state["final_text"]
                tool_calls = _extract_tool_calls(generated.raw) if allow_tools else []
                if not tool_calls:
                    return
                state["used_tools"] = True
                state["rounds"] += 1
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
                            "round": state["rounds"],
                        }
                    )
                    tool_message = {
                        "role": "tool",
                        "tool_name": name,
                        "content": _render_result(result),
                    }
                    if call.get("id"):
                        tool_message["tool_call_id"] = str(call["id"])
                    messages.append(tool_message)
                if question_box.get("question"):
                    # The model raised a genuine founder decision: stop cleanly.
                    # The delegation layer files the question; nothing is guessed.
                    state["status"] = "needs_decision"
                    return
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

        advance(rounds_cap)
        if state["status"] == "capability_error":
            return {
                "ok": False,
                "status": "capability_error",
                "error": state["error"],
                "model": selected_model,
                "rounds": state["rounds"],
                "steps": steps,
                "changed_files": sorted(changed_files),
                "response": "",
            }

        # Kernel-run check (Goal → Act → Check → Repeat): when the run changed
        # files, the KERNEL checks the result itself through the same
        # allowlisted/audited run_command path — the workspace's real test
        # entry point when one exists, otherwise syntax checks on the changed
        # files where a trustworthy local checker exists. The model cannot skip
        # it and cannot fake it. Files no checker covers are reported as
        # UNVERIFIED, never silently passed. On failure the real output is fed
        # back to the model for bounded repair rounds; the result carries the
        # LAST check's outcome, not the model's claim.
        auto_verification: dict[str, Any] | None = None
        verify_targets = real_change_targets() if state["status"] == "ok" else []
        # verify_always (set for delegated execution briefs): the Check leg
        # tests the GOAL state, not just this run's delta — a no-change run in
        # a workspace whose checks fail is NOT done (live incident item #71:
        # a broken .tsx inherited from the previous run went unchecked because
        # this run changed nothing).
        if state["status"] == "ok" and (verify_targets or verify_always):
            auto_verification = self._run_verification(
                tools,
                root,
                verify_targets,
                steps=steps,
                rounds=state["rounds"],
                force_workspace=verify_always,
            )
            repairs = 0
            while (
                auto_verification.get("ok") is False
                and repairs < MAX_REPAIR_ROUNDS
                and state["status"] == "ok"
            ):
                repairs += 1
                steps_before = len(steps)
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "KERNEL CHECK FAILED — the work is not done. Real output:\n"
                            f"{str(auto_verification.get('output') or '')[:MAX_VERIFY_OUTPUT_CHARS]}\n\n"
                            "You are in the Repeat leg of Goal → Act → Check → Repeat: fix the "
                            "failure with tools now, then finish with a plain-text summary. Do "
                            "not claim the check passes — the kernel re-runs it itself."
                        ),
                    }
                )
                advance(rounds_cap)
                if state["status"] != "ok" or len(steps) == steps_before:
                    # Nothing new happened (or the run stopped): re-checking
                    # would reproduce the same failure; keep the honest result.
                    break
                auto_verification = self._run_verification(
                    tools,
                    root,
                    real_change_targets(),
                    steps=steps,
                    rounds=state["rounds"],
                    force_workspace=verify_always,
                )
            auto_verification["repair_rounds"] = repairs
            rendered = str(auto_verification.pop("rendered", "") or "")
            if rendered:
                state["final_text"] = (state["final_text"].strip() + "\n\n" + rendered).strip()

        # The run's REAL change set: before/after workspace diff, catching
        # command-driven mutations invisible to write-tool tracking.
        workspace_changes: dict[str, Any] | None = None
        if before_snapshot is not None:
            final_snapshot = _workspace_snapshot(root)
            if final_snapshot is not None:
                workspace_changes = _diff_snapshots(before_snapshot, final_snapshot)

        # Fresh-context verifier (advisory): a separate model call with NO
        # memory of how the work was produced judges the changed files against
        # the task. Fresh-context review outperforms self-critique; it never
        # flips run status — mechanical checks are the ground truth — but its
        # verdict rides the artifact and the route flags a FAIL.
        verifier_review: dict[str, Any] | None = None
        if state["status"] == "ok":
            review_targets = sorted(
                set(changed_files)
                | set((workspace_changes or {}).get("added") or [])
                | set((workspace_changes or {}).get("modified") or [])
            )
            if review_targets:
                verifier_review = self._fresh_context_verifier(
                    task=task, root=root, targets=review_targets, model=selected_model
                )
                if verifier_review is not None:
                    state["final_text"] = (
                        str(state["final_text"]).strip()
                        + "\n\n--- Fresh-context verifier (advisory; fresh eyes, not the builder) ---\n"
                        + f"VERDICT: {verifier_review['verdict'].upper()}\n"
                        + str(verifier_review.get("notes") or "")
                    ).strip()

        status = state["status"]
        rounds = state["rounds"]
        used_tools = state["used_tools"]
        error = state["error"]
        final_text = state["final_text"]

        result = {
            "ok": status == "ok",
            "status": status,
            "error": error,
            "founder_question": (
                {
                    "question": str(question_box.get("question") or ""),
                    "options": [str(o) for o in (question_box.get("options") or [])],
                }
                if status == "needs_decision"
                else None
            ),
            "model": selected_model,
            "provider": self.model_client.provider_info(),
            "workspace": str(root),
            "rounds": rounds,
            "used_tools": used_tools,
            "steps": steps,
            "changed_files": sorted(changed_files),
            "workspace_changes": workspace_changes,
            "auto_verification": auto_verification,
            "verifier_review": verifier_review,
            "progress_notes": progress_notes,
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

    # ---- fresh-context verifier -------------------------------------------------
    def _fresh_context_verifier(
        self, *, task: str, root: Path, targets: list[str], model: str
    ) -> dict[str, Any] | None:
        """One fresh-context model call reviewing the changed files against the
        task. Returns {"verdict": "pass"|"fail", "notes": ...} or None (no
        reviewable files, unparseable verdict, or model error). Advisory only."""
        excerpts: list[str] = []
        review_targets = [
            rel for rel in targets if not _is_generated_review_target(rel)
        ]
        for rel in review_targets[:6]:
            path = root / rel
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            excerpts.append(f"--- {rel} ---\n{text[:4000]}")
        if not excerpts:
            return None
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a fresh-eyes verifier with no memory of how this work was "
                    "produced. Judge ONLY what is in front of you against the task. "
                    "First line: exactly 'VERDICT: PASS' or 'VERDICT: FAIL'. Then at "
                    "most five short bullets naming concrete issues (file + problem). "
                    "No praise, no restating the task, no suggestions beyond defects."
                ),
            },
            {
                "role": "user",
                "content": f"Task:\n{task[:1200]}\n\nChanged files:\n" + "\n\n".join(excerpts),
            },
        ]
        try:
            generated = self.model_client.chat(
                messages=messages,
                model=model,
                think=self.config.ollama.think_for_role("coding"),
                temperature=0.1,
                num_predict=700,
            )
        except (CodingModelError, OllamaError):
            return None
        text = (generated.response or "").strip()
        if re.search(r"(?im)^\s*verdict:\s*fail\b", text):
            verdict = "fail"
        elif re.search(r"(?im)^\s*verdict:\s*pass\b", text):
            verdict = "pass"
        else:
            return None
        return {"verdict": verdict, "notes": text[:1500]}

    # ---- auto-verification ------------------------------------------------------
    def _verification_argv(self, root: Path) -> list[str] | None:
        """Pick the workspace's real verification command, or None when the
        workspace has no recognizable test entry point. Node workspaces only
        qualify when package.json actually declares a test script (a bare
        ``npm test`` would just error); Python workspaces qualify on
        pyproject.toml, a tests/ directory, or root-level test_*.py files."""
        package = root / "package.json"
        if package.is_file():
            try:
                manifest = json.loads(package.read_text(encoding="utf-8", errors="replace"))
            except (json.JSONDecodeError, OSError):
                manifest = {}
            scripts = manifest.get("scripts") if isinstance(manifest, dict) else {}
            if isinstance(scripts, dict) and str(scripts.get("test") or "").strip():
                return ["npm", "test"]
            return None
        if (root / "pyproject.toml").is_file() or (root / "tests").is_dir() or any(root.glob("test_*.py")):
            return ["python", "-m", "pytest", "-q"]
        return None

    def _typescript_check_argv(self, root: Path) -> list[str] | None:
        """``tsc --noEmit`` for TypeScript workspaces (tsconfig.json plus a
        typescript dependency). A jest suite that never imports a new file
        passes while the file is type-broken — live incident item #70 shipped
        nine tsc errors under 'verification passed'."""
        if not (root / "tsconfig.json").is_file():
            return None
        package = root / "package.json"
        try:
            manifest = json.loads(package.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError):
            return None
        deps: dict[str, Any] = {}
        if isinstance(manifest, dict):
            for key in ("devDependencies", "dependencies"):
                block = manifest.get(key)
                if isinstance(block, dict):
                    deps |= block
        if "typescript" not in deps:
            return None
        # --no: never install on the fly; the local tsc binary or nothing.
        return ["npm", "exec", "--no", "--", "tsc", "--noEmit"]

    def _verification_plan(
        self,
        root: Path,
        changed: list[str],
        *,
        force_workspace: bool = False,
    ) -> tuple[str, list[list[str]], list[str]]:
        """The kernel's check plan for this run: (mode, check argvs, unchecked
        files). Prefer the workspace's real test entry point (plus tsc for
        TypeScript workspaces); without one, fall back to syntax checks on the
        changed files where a trustworthy local checker exists (.py via
        py_compile, .json via json.tool, .ts/.tsx via tsc). Files with no
        reliable checker come back as unchecked — they are reported as
        unverified, never silently passed."""
        flutter_workspace = (root / "pubspec.yaml").is_file() and (
            root / "lib"
        ).is_dir()
        changed_product_files = any(
            not _is_build_phase_artifact(relative_path) for relative_path in changed
        )
        if flutter_workspace and (force_workspace or changed_product_files):
            return (
                "tests",
                [
                    ["flutter", "analyze", "--no-pub"],
                    ["flutter", "test", "--no-pub"],
                ],
                [],
            )
        argv = self._verification_argv(root)
        tsc_argv = self._typescript_check_argv(root)
        if argv is not None:
            checks = [list(argv)]
            if tsc_argv is not None:
                checks.append(tsc_argv)
            return "tests", checks, []
        # Delegated execution verifies the desired workspace state even when
        # the model reports no diff. A TypeScript app may intentionally have
        # no test script but still expose a reliable local type check.
        if force_workspace and tsc_argv is not None:
            return "tests", [tsc_argv], []
        py_files = [f for f in changed if f.lower().endswith(".py")]
        json_files = [f for f in changed if f.lower().endswith(".json")]
        ts_files = (
            [f for f in changed if f.lower().endswith((".ts", ".tsx"))]
            if tsc_argv is not None
            else []
        )
        checks: list[list[str]] = []
        if py_files:
            checks.append(["python", "-m", "py_compile", *py_files])
        for f in json_files:
            checks.append(["python", "-m", "json.tool", f])
        if ts_files:
            checks.append(list(tsc_argv or []))
        covered = set(py_files) | set(json_files) | set(ts_files)
        unchecked = [f for f in changed if f not in covered]
        return ("syntax" if checks else "none"), checks, unchecked

    def _run_verification(
        self,
        tools: dict[str, AgentTool],
        root: Path,
        changed: list[str],
        *,
        steps: list[dict[str, Any]],
        rounds: int,
        force_workspace: bool = False,
    ) -> dict[str, Any]:
        """Execute the check plan through the audited run_command path and
        return a structured verdict: ok True (all checks passed), False (a
        check failed), or None (no runnable check exists — unverified)."""
        mode, checks, unchecked = self._verification_plan(
            root, changed, force_workspace=force_workspace
        )
        results: list[dict[str, Any]] = []
        rendered_blocks: list[str] = []
        failing_blocks: list[str] = []
        for argv in checks:
            started = time.perf_counter()
            outcome = self._execute(tools, "run_command", {"argv": list(argv)})
            latency_ms = int((time.perf_counter() - started) * 1000)
            check_ok = bool(outcome.get("ok", False))
            steps.append(
                {
                    "tool": "run_command",
                    "arguments": {"argv": list(argv)},
                    "ok": check_ok,
                    "latency_ms": latency_ms,
                    "round": rounds,
                    "auto_verify": True,
                }
            )
            results.append(
                {"argv": list(argv), "ok": check_ok, "returncode": outcome.get("returncode")}
            )
            block = _render_verification(argv, outcome)
            rendered_blocks.append(block)
            if not check_ok:
                failing_blocks.append(block)
        ok: bool | None = all(r["ok"] for r in results) if results else None
        if mode == "none":
            rendered = (
                "--- Kernel auto-verification ---\n"
                "No runnable check exists for the changed file(s): "
                + ", ".join(unchecked)
                + ". The change is UNVERIFIED — treat any completion claim accordingly."
            )
        else:
            rendered = "\n\n".join(rendered_blocks)
            if mode == "syntax":
                rendered += (
                    "\n(syntax-level check only — this workspace has no test entry "
                    "point, so behavior is unverified)"
                )
            if unchecked:
                rendered += (
                    "\n(no reliable local checker for: " + ", ".join(unchecked) + " — unverified)"
                )
        anchor = next((r for r in results if not r["ok"]), results[0] if results else {})
        return {
            "mode": mode,
            "ok": ok,
            "checks": results,
            "unchecked_files": unchecked,
            "argv": anchor.get("argv"),
            "returncode": anchor.get("returncode"),
            "output": "\n\n".join(failing_blocks) or rendered,
            "rendered": rendered,
        }

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
                "Your working loop is Goal → Act → Check → Repeat: know what the task needs, act "
                "with tools, check the result through a tool observation (read the file back, run "
                "the focused test), and repeat until the check passes. Read before you edit and "
                "prefer replace_in_file for surgical changes. Before reporting progress, audit "
                "each claim against a tool result from this run: only report work you can point "
                "to evidence for, and if something is not yet verified, say so explicitly. If a "
                "check fails, say so with the output; if a step was skipped, say that. Never "
                "claim an action you did not perform with a tool."
            ),
            (
                "Tool mechanics that matter: replace_in_file old_text must be one CONTIGUOUS "
                "region copied exactly from a read_file result — to remove several separated "
                "lines, make one replace call per line. A failed edit is not done: re-read the "
                "file and retry differently. Use delete_file only for a workspace file that the "
                "task genuinely removes; directory deletion is not available."
            ),
            (
                "When you are done, reply with plain text and no tool calls. Lead with the "
                "outcome: your first sentence answers what happened, then the detail that "
                "changes what the reader does next — no fabricated test results, no padding."
            ),
            (
                "You are pre-authorized to complete this task end to end — never stop to ask "
                "whether to proceed, and never end with a plan instead of the work. Pause for "
                "the founder only when the work genuinely requires them: a destructive or "
                "irreversible choice, a real scope change, or input only they can provide — "
                "then call ask_founder once with one precise question and stop. Everything "
                "else, resolve with the safest reasonable choice and keep going."
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
    def _build_tools(
        self,
        root: Path,
        *,
        question_box: dict[str, Any] | None = None,
        progress_notes: list[str] | None = None,
        write_allowlist: tuple[str, ...] | None = None,
    ) -> dict[str, AgentTool]:
        tools: dict[str, AgentTool] = {}
        allowed_writes = (
            {
                str(self._resolve_in_workspace(root, item).relative_to(root)).replace("\\", "/")
                for item in write_allowlist
            }
            if write_allowlist is not None
            else None
        )

        def add(tool: AgentTool) -> None:
            tools[tool.name] = tool

        def guarded_write(
            handler: Callable[[dict[str, Any]], dict[str, Any]],
            args: dict[str, Any],
        ) -> dict[str, Any]:
            if allowed_writes is not None:
                try:
                    target = self._resolve_in_workspace(root, str(args.get("path") or ""))
                    relative = str(target.relative_to(root)).replace("\\", "/")
                except (OSError, ValueError) as exc:
                    return {"ok": False, "error": str(exc)}
                if relative not in allowed_writes:
                    return {
                        "ok": False,
                        "error": f"{relative} is not allowed by this build phase",
                    }
            return handler(args)

        protected_manifests = {
            "package.json",
            "package-lock.json",
            "pubspec.yaml",
            "pubspec.lock",
            "pyproject.toml",
            "poetry.lock",
            "cargo.toml",
            "cargo.lock",
            "go.mod",
            "go.sum",
        }

        def is_protected_manifest(target: Path) -> bool:
            return target.is_file() and target.name.casefold() in protected_manifests

        def guarded_full_file_write(args: dict[str, Any]) -> dict[str, Any]:
            try:
                target = self._resolve_in_workspace(root, str(args.get("path") or ""))
                relative = str(target.relative_to(root)).replace("\\", "/")
            except (OSError, ValueError) as exc:
                return {"ok": False, "error": str(exc)}
            if is_protected_manifest(target):
                return {
                    "ok": False,
                    "error": (
                        f"{relative} is a protected dependency manifest and cannot be "
                        "overwritten wholesale. Preserve the existing file: use "
                        "replace_in_file for a surgical edit or the ecosystem package "
                        "manager through run_command."
                    ),
                }
            return guarded_write(lambda values: self._tool_write_file(root, values), args)

        def guarded_replace(args: dict[str, Any]) -> dict[str, Any]:
            try:
                target = self._resolve_in_workspace(root, str(args.get("path") or ""))
                relative = str(target.relative_to(root)).replace("\\", "/")
            except (OSError, ValueError) as exc:
                return {"ok": False, "error": str(exc)}
            if is_protected_manifest(target):
                original = target.read_text(encoding="utf-8", errors="replace")
                old_text = str(args.get("old_text") or "")
                new_text = str(args.get("new_text") or "")
                if old_text and original.count(old_text) == 1:
                    candidate = original.replace(old_text, new_text, 1)
                    original_lines = [line for line in original.splitlines() if line.strip()]
                    candidate_lines = [line for line in candidate.splitlines() if line.strip()]
                    cursor = 0
                    for line in original_lines:
                        while cursor < len(candidate_lines) and candidate_lines[cursor] != line:
                            cursor += 1
                        if cursor >= len(candidate_lines):
                            return {
                                "ok": False,
                                "error": (
                                    f"{relative} is a protected dependency manifest. A "
                                    "surgical edit must preserve every existing manifest line "
                                    "in order; use the ecosystem package manager when an "
                                    "existing entry must change."
                                ),
                            }
                        cursor += 1
            return guarded_write(lambda values: self._tool_replace(root, values), args)

        def guarded_delete(args: dict[str, Any]) -> dict[str, Any]:
            try:
                target = self._resolve_in_workspace(root, str(args.get("path") or ""))
                relative = str(target.relative_to(root)).replace("\\", "/")
            except (OSError, ValueError) as exc:
                return {"ok": False, "error": str(exc)}
            if is_protected_manifest(target):
                return {
                    "ok": False,
                    "error": (
                        f"{relative} is a protected dependency manifest and cannot be "
                        "deleted. Preserve it and make only the required additive change."
                    ),
                }
            return guarded_write(lambda values: self._tool_delete_file(root, values), args)

        if progress_notes is not None:
            add(
                AgentTool(
                    name="send_progress",
                    description=(
                        "Send one short progress line to the founder while you keep working "
                        "(it raises a native notification immediately and does NOT end the "
                        "run). Use at real milestones — a file finished, checks starting, a "
                        "blocker routed around. Statements only, never questions; questions "
                        "go through ask_founder."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "message": {
                                "type": "string",
                                "description": "One short progress statement (max ~200 chars).",
                            }
                        },
                        "required": ["message"],
                    },
                    handler=lambda args: self._tool_send_progress(progress_notes, args),
                )
            )

        if question_box is not None:
            add(
                AgentTool(
                    name="ask_founder",
                    description=(
                        "Stop and queue a decision question to the founder. Use ONLY when you "
                        "genuinely cannot proceed safely without their input: a missing requirement, "
                        "an irreversible or destructive choice, or materially different implementations "
                        "with real trade-offs. Never use it to ask permission to do the task you were "
                        "given — that is already granted — and never for this run's own tool limits "
                        "(a blocked or unavailable command): route around those and note them in your "
                        "summary. Calling this ends the run; it resumes after the founder answers."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": "One precise, answerable question for the founder.",
                            },
                            "options": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Optional concrete options, best default first.",
                            },
                        },
                        "required": ["question"],
                    },
                    handler=lambda args: self._tool_ask_founder(question_box, args),
                )
            )

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
                handler=guarded_full_file_write,
                writes=True,
            )
        )
        add(
            AgentTool(
                name="delete_file",
                description="Delete one file inside the workspace. Directories are refused.",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
                handler=guarded_delete,
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
                handler=guarded_replace,
                writes=True,
            )
        )
        if allowed_writes is None:
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
    def _tool_send_progress(
        self, progress_notes: list[str], args: dict[str, Any]
    ) -> dict[str, Any]:
        message = str(args.get("message") or "").strip()
        if not message:
            return {"ok": False, "error": "message is required"}
        if message.rstrip().endswith("?"):
            return {
                "ok": False,
                "error": (
                    "send_progress carries statements, not questions. If you genuinely "
                    "need the founder's input, use ask_founder; otherwise resolve it "
                    "yourself and report what you did."
                ),
            }
        message = message[:300]
        progress_notes.append(message)
        if self.notifier is not None:
            try:
                self.notifier.notify(
                    topic="delegation.progress",
                    title="Zade build progress",
                    body=message,
                    severity="info",
                )
            except Exception:  # noqa: BLE001 - progress delivery must not break the run
                pass
        return {
            "ok": True,
            "note": "Delivered to the founder. Keep working — this does not end the run.",
        }

    def _tool_ask_founder(self, question_box: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        question = str(args.get("question") or "").strip()
        if not question:
            return {"ok": False, "error": "question is required"}
        if _CAPABILITY_BOUNDARY_QUESTION_RE.search(question):
            # A blocked/refused command is a fixed boundary of this run, not a
            # founder decision — bouncing it keeps the run moving instead of
            # interrupting the founder over tooling.
            return {
                "ok": False,
                "error": (
                    "That is a fixed capability boundary of this run, not a founder "
                    "decision — do not ask the founder about blocked or unavailable "
                    "commands. Choose an allowlisted alternative, or skip that step "
                    "and note the skip in your final summary, then continue the task."
                ),
            }
        if _WORKSPACE_MECHANICS_QUESTION_RE.search(question):
            # Path conflicts and stray artifacts are the run's own state to
            # resolve — never the founder's. The founder must not be asked to
            # create directories or fix paths by hand.
            return {
                "ok": False,
                "error": (
                    "That is workspace mechanics, not a founder decision — resolve it "
                    "yourself: inspect with list_files, and either choose a workable "
                    "path or clear a stray artifact this run created (an allowlisted "
                    "python command can remove a file). Note what you did in your "
                    "final summary, then continue the task."
                ),
            }
        options = args.get("options")
        question_box["question"] = question[:600]
        question_box["options"] = [str(o)[:200] for o in options[:6]] if isinstance(options, list) else []
        return {
            "ok": True,
            "note": (
                "Question queued for the founder. Stop working now — reply with a one-line "
                "summary of the state you are leaving things in, and no further tool calls."
            ),
        }

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

    def _tool_delete_file(self, root: Path, args: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_in_workspace(root, str(args.get("path") or ""))
        if not path.is_file() and not path.is_symlink():
            return {"ok": False, "error": f"not a file: {args.get('path')}"}
        rel = str(path.relative_to(root)).replace("\\", "/")
        path.unlink()
        return {"ok": True, "path": rel, "deleted": True, "_changed_file": True}

    def _tool_run_command(self, root: Path, args: dict[str, Any]) -> dict[str, Any]:
        argv = args.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            return {"ok": False, "error": "argv must be a non-empty array of strings"}
        try:
            profile_id, resolved = normalize_coding_agent_command(tuple(argv))
        except CommandPolicyError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "note": (
                    "This boundary is fixed for this run — do NOT ask the founder about it. "
                    "Use an allowlisted alternative, or skip this step, note the skip in "
                    "your final summary, and continue the task."
                ),
            }
        if self.command_runner is not None:
            try:
                outcome = self.command_runner.run(
                    CommandRequest(
                        workspace=root,
                        profile_id=profile_id,
                        argv=resolved,
                        timeout_seconds=COMMAND_TIMEOUT_SECONDS,
                    )
                )
            except (CommandPolicyError, OSError) as exc:
                return {"ok": False, "error": str(exc)}
            if outcome.timed_out:
                return {
                    "ok": False,
                    "error": f"command timed out after {COMMAND_TIMEOUT_SECONDS}s",
                }
            if outcome.cancelled:
                return {"ok": False, "error": "command was cancelled"}
            return {
                "ok": bool(outcome.ok),
                "returncode": outcome.returncode,
                "stdout": outcome.stdout_tail,
                "stderr": outcome.stderr_tail,
            }
        # Pin Python-family programs to the kernel's own interpreter so the run
        # sees the kernel venv (pytest included) instead of whatever bare
        # 'python' resolves to on PATH. git stays git.
        try:
            completed = subprocess.run(
                list(resolved),
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


def _is_generated_review_target(relative_path: str) -> bool:
    parts = tuple(
        part.casefold()
        for part in Path(relative_path.replace("\\", "/")).parts
        if part not in {"", "."}
    )
    if not parts:
        return True
    if parts[0] in {"build", ".dart_tool", ".gradle"}:
        return True
    return parts[:2] == ("android", ".gradle") or parts[:3] == (
        "ios",
        "flutter",
        "ephemeral",
    )


def _is_build_phase_artifact(relative_path: str) -> bool:
    normalized = relative_path.replace("\\", "/").casefold().lstrip("./")
    return normalized.startswith("zade/build/")


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


def _workspace_snapshot(root: Path) -> dict[str, tuple[int, int]] | None:
    """Flat {relpath: (size, mtime_ns)} snapshot of the workspace, skipping
    bulk directories. Returns None when the tree exceeds MAX_SNAPSHOT_FILES —
    the diff is then skipped rather than made expensive."""
    snapshot: dict[str, tuple[int, int]] = {}
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            count += 1
            if count > MAX_SNAPSHOT_FILES:
                return None
            path = Path(dirpath) / name
            try:
                stat = path.stat()
            except OSError:
                continue
            rel = str(path.relative_to(root)).replace("\\", "/")
            snapshot[rel] = (stat.st_size, stat.st_mtime_ns)
    return snapshot


def _diff_snapshots(
    before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]
) -> dict[str, Any]:
    """The run's REAL change set — catches command-driven mutations (npm
    install, python deletions) that write-tool tracking cannot see."""
    added = sorted(set(after) - set(before))
    deleted = sorted(set(before) - set(after))
    modified = sorted(
        path for path in set(before) & set(after) if before[path] != after[path]
    )
    return {"added": added, "modified": modified, "deleted": deleted, "complete": True}


def _render_verification(argv: list[str], result: dict[str, Any]) -> str:
    """Render the kernel-run verification as an artifact block. Only real
    subprocess output goes in here — never model text."""
    lines = [
        "--- Kernel auto-verification (REAL output, appended by the kernel — not the model) ---",
        f"$ {' '.join(argv)}",
    ]
    if "returncode" in result:
        lines.append(f"exit code: {result['returncode']}")
    for stream in ("stdout", "stderr"):
        text = str(result.get(stream) or "").strip()
        if text:
            lines.append(text[:MAX_VERIFY_OUTPUT_CHARS])
    error = str(result.get("error") or "").strip()
    if error:
        lines.append(f"error: {error[:400]}")
    return "\n".join(lines)


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
