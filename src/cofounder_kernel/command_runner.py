"""Governed argv-only command execution for local product builds."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable, Literal, Mapping
from uuid import uuid4


Backend = Literal["auto", "host", "docker"]
_SHELL_TOKENS = frozenset({"&", "&&", "|", "||", ";", ">", ">>", "<", "`"})
_SENSITIVE_ENV = re.compile(r"(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.IGNORECASE)
_BASE_ENV = frozenset(
    {
        "ALLUSERSPROFILE",
        "APPDATA",
        "COMSPEC",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "LANG",
        "LOCALAPPDATA",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "PATH",
        "PATHEXT",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    }
)


class CommandPolicyError(ValueError):
    """A command was refused before process creation."""


@dataclass(frozen=True)
class CommandPolicy:
    id: str
    executable_candidates: tuple[str, ...]
    executable_aliases: tuple[str, ...] = ()
    allowed_prefixes: tuple[tuple[str, ...], ...] = ()
    denied_tokens: tuple[str, ...] = ()
    allowed_env: tuple[str, ...] = ()
    max_timeout_seconds: float = 600.0
    docker_image: str | None = None
    container_executable: str | None = None
    host_allowed: bool = True

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("Command policy id is required")
        if not self.executable_candidates:
            raise ValueError("At least one executable candidate is required")
        if self.max_timeout_seconds <= 0:
            raise ValueError("max_timeout_seconds must be positive")


@dataclass(frozen=True)
class CommandRequest:
    workspace: Path | str
    profile_id: str
    argv: tuple[str, ...]
    timeout_seconds: float | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    backend: Backend = "auto"
    run_id: str | None = None


@dataclass(frozen=True)
class CommandPreflight:
    run_id: str
    workspace: Path
    profile_id: str
    argv: tuple[str, ...]
    resolved_argv: tuple[str, ...]
    redacted_argv: tuple[str, ...]
    backend: Literal["host", "docker"]
    docker_image: str | None
    timeout_seconds: float
    environment: Mapping[str, str]


@dataclass(frozen=True)
class CommandResult:
    run_id: str
    ok: bool
    returncode: int | None
    backend: str
    redacted_argv: tuple[str, ...]
    stdout_tail: str
    stderr_tail: str
    stdout_log: Path
    stderr_log: Path
    duration_seconds: float
    timed_out: bool = False
    cancelled: bool = False


@dataclass
class _ActiveProcess:
    preflight: CommandPreflight
    process: subprocess.Popen[str]
    stdout_log: Path
    stderr_log: Path
    stdout_handle: object
    stderr_handle: object
    started_at: float
    cancelled: bool = False
    timed_out: bool = False
    result: CommandResult | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    wait_lock: threading.Lock = field(default_factory=threading.Lock)


class RunningCommand:
    def __init__(self, runner: "GovernedCommandRunner", run_id: str, pid: int):
        self._runner = runner
        self.run_id = run_id
        self.pid = pid

    def wait(self, timeout_seconds: float | None = None) -> CommandResult:
        return self._runner._wait(self.run_id, timeout_seconds=timeout_seconds)

    def cancel(self) -> bool:
        return self._runner.cancel(self.run_id)


_CODING_COMMAND_PREFIXES: dict[str, tuple[tuple[str, ...], ...]] = {
    "python": (
        ("--version",),
        ("-m", "pytest"),
        ("-m", "unittest"),
        ("-m", "py_compile"),
        ("-m", "compileall"),
        ("-m", "json.tool"),
    ),
    "uv": (("--version",), ("run", "pytest")),
    "git": (("status",), ("diff",), ("rev-parse",), ("log",)),
    "npm": (
        ("--version",),
        ("test",),
        ("run", "test"),
        ("run", "typecheck"),
        ("run", "lint"),
        ("run", "build"),
        ("exec", "--no", "--", "tsc", "--noEmit"),
    ),
    "node": (("--version",), ("--test",), ("--check",)),
}


def normalize_coding_agent_command(argv: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    """Map a coding-agent command to one narrow runner profile and executable."""
    if not argv or not all(isinstance(item, str) and item for item in argv):
        raise CommandPolicyError("argv must contain non-empty strings")
    name = Path(argv[0]).name.casefold()
    for suffix in (".exe", ".cmd", ".bat"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    tail = tuple(argv[1:])
    if name in {"python", "python3", "py", "pytest"}:
        profile = "python"
        normalized = (
            (sys.executable, "-m", "pytest", *tail)
            if name == "pytest"
            else (sys.executable, *tail)
        )
    elif name in {"uv", "git", "npm", "node"}:
        profile = name
        located = shutil.which(argv[0]) or shutil.which(name)
        normalized = (located or argv[0], *tail)
    else:
        allowed = ", ".join(sorted(_CODING_COMMAND_PREFIXES))
        raise CommandPolicyError(
            f"program {argv[0]!r} is not allowlisted. Allowed: {allowed}"
        )
    normalized_tail = tuple(normalized[1:])
    if not any(
        normalized_tail[: len(prefix)] == prefix
        for prefix in _CODING_COMMAND_PREFIXES[profile]
    ):
        raise CommandPolicyError(
            "command is outside the approved test and verification shapes"
        )
    return f"coding-agent:{profile}", tuple(str(item) for item in normalized)


def coding_agent_command_policies() -> dict[str, CommandPolicy]:
    """Policies for non-installing coding checks and repository inspection."""
    programs: dict[str, tuple[str, ...]] = {
        "python": (sys.executable,),
        "uv": tuple(filter(None, (shutil.which("uv"), "uv"))),
        "git": tuple(filter(None, (shutil.which("git"), "git"))),
        "npm": tuple(filter(None, (shutil.which("npm"), "npm", "npm.cmd"))),
        "node": tuple(filter(None, (shutil.which("node"), "node"))),
    }
    aliases = {
        "python": ("python", "python3", "py", "python.exe", Path(sys.executable).name),
        "uv": ("uv", "uv.exe"),
        "git": ("git", "git.exe"),
        "npm": ("npm", "npm.cmd"),
        "node": ("node", "node.exe"),
    }
    images = {"python": "python:3.12-local", "npm": "node:22-local", "node": "node:22-local"}
    return {
        f"coding-agent:{program}": CommandPolicy(
            id=f"coding-agent:{program}",
            executable_candidates=candidates,
            executable_aliases=aliases[program],
            allowed_prefixes=_CODING_COMMAND_PREFIXES[program],
            denied_tokens=("install", "publish", "deploy", "release", "upload"),
            max_timeout_seconds=420,
            docker_image=images.get(program),
            container_executable=("python" if program == "python" else program),
            host_allowed=True,
        )
        for program, candidates in programs.items()
    }


class GovernedCommandRunner:
    """Execute only commands admitted by explicit immutable policies."""

    def __init__(
        self,
        *,
        policies: Mapping[str, CommandPolicy],
        artifact_root: Path | str,
        max_output_chars: int = 20_000,
        max_log_bytes: int = 5_000_000,
        docker_probe: Callable[[str], bool] | None = None,
        audit: Callable[[dict[str, object]], None] | None = None,
    ):
        self.policies = dict(policies)
        self.artifact_root = Path(artifact_root).expanduser().resolve()
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.max_output_chars = max(1, int(max_output_chars))
        self.max_log_bytes = max(1, int(max_log_bytes))
        self.docker_probe = docker_probe or _docker_image_available
        self.audit = audit
        self._active: dict[str, _ActiveProcess] = {}
        self._completed: dict[str, CommandResult] = {}
        self._lock = threading.Lock()

    def preflight(self, request: CommandRequest) -> CommandPreflight:
        policy = self.policies.get(request.profile_id)
        if policy is None:
            raise CommandPolicyError(f"Unknown command profile: {request.profile_id}")
        if not request.argv or not all(isinstance(item, str) and item for item in request.argv):
            raise CommandPolicyError("argv must contain non-empty strings")
        workspace = Path(request.workspace).expanduser().resolve()
        if not workspace.is_dir():
            raise CommandPolicyError(f"workspace does not exist: {workspace}")
        timeout = (
            min(300.0, policy.max_timeout_seconds)
            if request.timeout_seconds is None
            else float(request.timeout_seconds)
        )
        if timeout <= 0 or timeout > policy.max_timeout_seconds:
            raise CommandPolicyError(
                f"timeout must be between 0 and {policy.max_timeout_seconds:g} seconds"
            )

        executable = _resolve_executable(policy, request.argv[0])
        tail = tuple(request.argv[1:])
        if policy.allowed_prefixes and not any(
            tail[: len(prefix)] == prefix for prefix in policy.allowed_prefixes
        ):
            raise CommandPolicyError("command argument shape is not permitted by this profile")
        denied = {item.casefold() for item in policy.denied_tokens}
        for index, token in enumerate(tail, start=1):
            if token in _SHELL_TOKENS:
                raise CommandPolicyError(f"shell metacharacter is not permitted: {token}")
            if token.casefold() in denied:
                raise CommandPolicyError(f"argument is denied by this profile: {token}")
            if _is_payload_argument(request.argv, index):
                continue
            _validate_workspace_path_token(workspace, token)

        backend = _select_backend(request.backend, policy, self.docker_probe)
        if backend == "host" and not policy.host_allowed:
            raise CommandPolicyError("host execution is disabled for this profile")
        environment = _child_environment(request.env, policy.allowed_env)
        resolved = (str(executable), *tail)
        run_id = request.run_id or uuid4().hex
        return CommandPreflight(
            run_id=run_id,
            workspace=workspace,
            profile_id=policy.id,
            argv=tuple(request.argv),
            resolved_argv=resolved,
            redacted_argv=_redact_argv(tuple(request.argv)),
            backend=backend,
            docker_image=policy.docker_image if backend == "docker" else None,
            timeout_seconds=timeout,
            environment=environment,
        )

    def register_policy(self, policy: CommandPolicy) -> None:
        """Register a derived immutable policy without widening an existing id."""
        with self._lock:
            current = self.policies.get(policy.id)
            if current is not None and current != policy:
                raise CommandPolicyError(
                    f"command profile {policy.id} is already registered differently"
                )
            self.policies[policy.id] = policy

    def run(self, request: CommandRequest) -> CommandResult:
        running = self.start(request)
        return running.wait()

    def start(self, request: CommandRequest) -> RunningCommand:
        preflight = self.preflight(request)
        run_dir = self.artifact_root / preflight.run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        stdout_log = run_dir / "stdout.log"
        stderr_log = run_dir / "stderr.log"
        stdout_handle = stdout_log.open("w", encoding="utf-8", errors="replace")
        stderr_handle = stderr_log.open("w", encoding="utf-8", errors="replace")
        argv = list(preflight.resolved_argv)
        if preflight.backend == "docker":
            policy = self.policies[preflight.profile_id]
            container_executable = policy.container_executable or Path(argv[0]).name
            argv = [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "256",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=512m",
                "--mount",
                f"type=bind,source={preflight.workspace},target=/workspace",
                "--workdir",
                "/workspace",
                str(preflight.docker_image),
                container_executable,
                *argv[1:],
            ]
        creationflags = 0
        if os.name == "nt":
            creationflags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        try:
            process = subprocess.Popen(
                argv,
                cwd=str(preflight.workspace),
                env=dict(preflight.environment),
                stdout=stdout_handle,
                stderr=stderr_handle,
                text=True,
                shell=False,
                creationflags=creationflags,
                start_new_session=os.name != "nt",
            )
        except Exception:
            stdout_handle.close()
            stderr_handle.close()
            raise
        active = _ActiveProcess(
            preflight=preflight,
            process=process,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            stdout_handle=stdout_handle,
            stderr_handle=stderr_handle,
            started_at=time.monotonic(),
        )
        with self._lock:
            if preflight.run_id in self._active or preflight.run_id in self._completed:
                _terminate_process(process)
                raise CommandPolicyError(f"duplicate command run id: {preflight.run_id}")
            self._active[preflight.run_id] = active
        self._audit("started", active)
        return RunningCommand(self, preflight.run_id, process.pid)

    def cancel(self, run_id: str) -> bool:
        with self._lock:
            active = self._active.get(run_id)
        if active is None:
            return False
        with active.lock:
            if (
                active.cancelled
                or active.result is not None
                or active.process.poll() is not None
            ):
                return False
            active.cancelled = True
        _terminate_process(active.process)
        return True

    def cancel_workspace(self, workspace: Path | str) -> int:
        """Cancel active commands rooted in one workspace."""
        root = Path(workspace).expanduser().resolve()
        with self._lock:
            run_ids = [
                run_id
                for run_id, active in self._active.items()
                if active.preflight.workspace == root
            ]
        return sum(1 for run_id in run_ids if self.cancel(run_id))

    def cancel_all(self) -> int:
        with self._lock:
            run_ids = list(self._active)
        return sum(1 for run_id in run_ids if self.cancel(run_id))

    def _wait(self, run_id: str, *, timeout_seconds: float | None = None) -> CommandResult:
        with self._lock:
            completed = self._completed.get(run_id)
            active = self._active.get(run_id)
        if completed is not None:
            return completed
        if active is None:
            raise KeyError(f"Unknown command run: {run_id}")
        with active.wait_lock:
            with active.lock:
                if active.result is not None:
                    return active.result
            wait_timeout = (
                active.preflight.timeout_seconds if timeout_seconds is None else timeout_seconds
            )
            try:
                active.process.wait(timeout=wait_timeout)
            except subprocess.TimeoutExpired:
                with active.lock:
                    active.timed_out = True
                _terminate_process(active.process)
                active.process.wait(timeout=2)
            active.stdout_handle.close()
            active.stderr_handle.close()
            _bound_log(active.stdout_log, self.max_log_bytes)
            _bound_log(active.stderr_log, self.max_log_bytes)
            duration = max(0.0, time.monotonic() - active.started_at)
            returncode = active.process.returncode
            with active.lock:
                result = CommandResult(
                    run_id=run_id,
                    ok=(returncode == 0 and not active.cancelled and not active.timed_out),
                    returncode=returncode,
                    backend=active.preflight.backend,
                    redacted_argv=active.preflight.redacted_argv,
                    stdout_tail=_tail(active.stdout_log, self.max_output_chars),
                    stderr_tail=_tail(active.stderr_log, self.max_output_chars),
                    stdout_log=active.stdout_log,
                    stderr_log=active.stderr_log,
                    duration_seconds=duration,
                    timed_out=active.timed_out,
                    cancelled=active.cancelled,
                )
                active.result = result
        with self._lock:
            self._active.pop(run_id, None)
            self._completed[run_id] = result
        self._audit("finished", active, result=result)
        return result

    def _audit(
        self,
        event: str,
        active: _ActiveProcess,
        *,
        result: CommandResult | None = None,
    ) -> None:
        if self.audit is None:
            return
        payload: dict[str, object] = {
            "event": event,
            "run_id": active.preflight.run_id,
            "profile_id": active.preflight.profile_id,
            "backend": active.preflight.backend,
            "argv": list(active.preflight.redacted_argv),
            "workspace": str(active.preflight.workspace),
        }
        if result is not None:
            payload.update(
                {
                    "ok": result.ok,
                    "returncode": result.returncode,
                    "timed_out": result.timed_out,
                    "cancelled": result.cancelled,
                    "duration_seconds": result.duration_seconds,
                }
            )
        try:
            self.audit(payload)
        except Exception:
            return


def _resolve_executable(policy: CommandPolicy, requested: str) -> Path:
    requested_path = Path(requested).expanduser()
    requested_name = requested_path.name.casefold()
    aliases = {alias.casefold() for alias in policy.executable_aliases}
    candidates: list[Path] = []
    for raw in policy.executable_candidates:
        expanded = os.path.expandvars(os.path.expanduser(raw))
        candidate = Path(expanded)
        if not candidate.is_absolute():
            located = shutil.which(expanded)
            if located:
                candidate = Path(located)
        if candidate.is_file():
            candidates.append(candidate.resolve())
    if not candidates:
        raise CommandPolicyError(
            f"no executable is available for command profile {policy.id}"
        )
    if requested_path.is_absolute():
        resolved_requested = requested_path.resolve()
        if resolved_requested not in candidates:
            raise CommandPolicyError(f"program {requested!r} is not permitted by this profile")
        return resolved_requested
    candidate_names = {candidate.name.casefold() for candidate in candidates}
    if requested_name not in aliases and requested_name not in candidate_names:
        raise CommandPolicyError(f"program {requested!r} is not permitted by this profile")
    for candidate in candidates:
        if candidate.name.casefold() == requested_name:
            return candidate
    return candidates[0]


def _select_backend(
    requested: Backend,
    policy: CommandPolicy,
    docker_probe: Callable[[str], bool],
) -> Literal["host", "docker"]:
    if requested not in {"auto", "host", "docker"}:
        raise CommandPolicyError(f"unknown command backend: {requested}")
    if requested == "host":
        return "host"
    if policy.docker_image and docker_probe(policy.docker_image):
        return "docker"
    if requested == "docker":
        raise CommandPolicyError(
            f"approved Docker image is unavailable locally: {policy.docker_image or 'none configured'}"
        )
    return "host"


def _child_environment(overrides: Mapping[str, str], allowed_env: tuple[str, ...]) -> dict[str, str]:
    allowed = {name.upper() for name in allowed_env}
    child: dict[str, str] = {}
    for name, value in os.environ.items():
        upper = name.upper()
        if (upper in _BASE_ENV or upper in allowed) and not _SENSITIVE_ENV.search(upper):
            child[name] = value
    for name, value in overrides.items():
        upper = str(name).upper()
        if upper not in allowed or _SENSITIVE_ENV.search(upper):
            continue
        child[str(name)] = str(value)
    return child


def _is_payload_argument(argv: tuple[str, ...], index: int) -> bool:
    return index == 2 and len(argv) > 2 and argv[1] in {"-c", "-Command"}


def _validate_workspace_path_token(workspace: Path, token: str) -> None:
    value = token.split("=", 1)[1] if token.startswith("-") and "=" in token else token
    if "://" in value or not value:
        return
    candidate = Path(value).expanduser()
    resolved = candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise CommandPolicyError(f"argument path escapes the workspace: {token}") from exc


def _tail(path: Path, max_chars: int) -> str:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - (max_chars * 4)))
        text = handle.read().decode("utf-8", errors="replace")
    return text[-max_chars:]


def _bound_log(path: Path, max_bytes: int) -> None:
    size = path.stat().st_size
    if size <= max_bytes:
        return
    with path.open("rb") as handle:
        handle.seek(max(0, size - max_bytes))
        tail = handle.read(max_bytes)
    with path.open("wb") as handle:
        handle.write(tail)


def _redact_argv(argv: tuple[str, ...]) -> tuple[str, ...]:
    redacted: list[str] = []
    mask_next = False
    for token in argv:
        if mask_next:
            redacted.append("[REDACTED]")
            mask_next = False
            continue
        upper = token.upper()
        if any(marker in upper for marker in ("API_KEY=", "TOKEN=", "PASSWORD=", "SECRET=")):
            key = token.split("=", 1)[0]
            redacted.append(f"{key}=[REDACTED]")
            continue
        if token.casefold() in {"--token", "--password", "--api-key", "--secret"}:
            redacted.append(token)
            mask_next = True
            continue
        redacted.append(token)
    return tuple(redacted)


def _docker_image_available(image: str) -> bool:
    docker = shutil.which("docker")
    if not docker:
        return False
    try:
        info = subprocess.run(
            [docker, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if info.returncode != 0:
            return False
        inspected = subprocess.run(
            [docker, "image", "inspect", image],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return inspected.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            process.wait(timeout=1)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            process.wait(timeout=1)
            return
        except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
            pass
    try:
        process.kill()
    except OSError:
        return
