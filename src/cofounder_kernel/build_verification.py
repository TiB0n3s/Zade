"""Profile-driven local verification with durable build evidence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Callable

from .build_store import BuildStore
from .command_runner import (
    CommandPolicy,
    CommandRequest,
    CommandResult,
    GovernedCommandRunner,
)
from .toolchain_profiles import ToolchainProfile, ToolchainRegistry, VerificationCommand


BrowserCapture = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class VerificationCheck:
    id: str
    kind: str
    required: bool
    available: bool
    argv: tuple[str, ...] = ()
    timeout_seconds: float = 600.0
    artifact_kind: str = "log"
    blocker: str = ""


@dataclass(frozen=True)
class VerificationPlan:
    profile_id: str
    workspace: str
    checks: tuple[VerificationCheck, ...]
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class VerificationCheckResult:
    id: str
    kind: str
    required: bool
    status: str
    ok: bool
    error: str = ""
    stdout_tail: str = ""
    stderr_tail: str = ""
    duration_seconds: float = 0.0
    backend: str = ""


@dataclass(frozen=True)
class VerificationArtifact:
    kind: str
    uri: str
    metadata: dict[str, Any]
    record_id: int | None = None


@dataclass(frozen=True)
class VerificationReport:
    ok: bool
    blocked: bool
    profile_id: str
    workspace: str
    checks: tuple[VerificationCheckResult, ...]
    artifacts: tuple[VerificationArtifact, ...]


class BuildVerificationService:
    def __init__(
        self,
        *,
        toolchains: ToolchainRegistry,
        runner: GovernedCommandRunner,
        store: BuildStore | None = None,
        browser_capture: BrowserCapture | None = None,
    ):
        self.toolchains = toolchains
        self.runner = runner
        self.store = store
        self.browser_capture = browser_capture

    def plan(
        self,
        workspace: Path | str,
        *,
        profile_id: str | None = None,
        browser_url: str = "",
    ) -> VerificationPlan:
        root = Path(workspace).expanduser().resolve()
        profile = (
            self.toolchains.profile(profile_id, root)
            if profile_id
            else self.toolchains.detect(root)
        )
        checks: list[VerificationCheck] = []
        for index, blocker in enumerate(dict.fromkeys(profile.blockers), start=1):
            checks.append(
                VerificationCheck(
                    id=f"toolchain-blocker-{index}",
                    kind="availability",
                    required=True,
                    available=False,
                    blocker=blocker,
                )
            )
        checks.extend(self._command_check(command) for command in profile.verification_commands)
        if profile.id == "flutter-mobile":
            adb = next(
                (probe for probe in profile.probes if probe.id == "adb" and probe.available),
                None,
            )
            if adb is None or adb.path is None:
                if not any("ADB" in blocker for blocker in profile.blockers):
                    checks.append(
                        VerificationCheck(
                            id="adb-online",
                            kind="android-device",
                            required=True,
                            available=False,
                            blocker="ADB is unavailable for Android device verification.",
                        )
                    )
            else:
                checks.append(
                    VerificationCheck(
                        id="adb-online",
                        kind="android-device",
                        required=True,
                        available=True,
                        argv=(str(adb.path), "devices", "-l"),
                        timeout_seconds=60,
                    )
                )
        if browser_url:
            checks.append(
                VerificationCheck(
                    id="playwright-browser-evidence",
                    kind="browser",
                    required=True,
                    available=self.browser_capture is not None,
                    blocker=(
                        "Playwright browser evidence adapter is unavailable."
                        if self.browser_capture is None
                        else ""
                    ),
                    artifact_kind="playwright",
                )
            )
        return VerificationPlan(
            profile_id=profile.id,
            workspace=str(root),
            checks=tuple(checks),
            blockers=tuple(check.blocker for check in checks if check.blocker),
        )

    def verify(
        self,
        workspace: Path | str,
        *,
        session_id: int | None = None,
        task_id: int | None = None,
        run_id: int | None = None,
        profile_id: str | None = None,
        browser_url: str = "",
        android_device: str = "",
    ) -> VerificationReport:
        root = Path(workspace).expanduser().resolve()
        plan = self.plan(root, profile_id=profile_id, browser_url=browser_url)
        profile = self.toolchains.profile(plan.profile_id, root)
        results: list[VerificationCheckResult] = []
        artifacts: list[VerificationArtifact] = []
        for check in plan.checks:
            if not check.available:
                results.append(
                    VerificationCheckResult(
                        id=check.id,
                        kind=check.kind,
                        required=check.required,
                        status="blocked" if check.required else "skipped",
                        ok=False,
                        error=check.blocker,
                    )
                )
                continue
            if check.kind == "browser":
                result, evidence = self._run_browser(
                    check, root, browser_url, session_id, task_id, run_id
                )
                results.append(result)
                artifacts.extend(evidence)
                continue
            command_result = self._run_command(root, profile, check)
            check_result = self._command_result(check, command_result)
            if check.kind == "android-device" and command_result.ok:
                online = _online_android_devices(command_result.stdout_tail)
                target_ok = (
                    android_device in online if android_device else bool(online)
                )
                if not target_ok:
                    expected = android_device or "any online Android device"
                    check_result = VerificationCheckResult(
                        id=check.id,
                        kind=check.kind,
                        required=check.required,
                        status="failed",
                        ok=False,
                        error=f"Expected {expected}; online devices: {', '.join(online) or 'none'}",
                        stdout_tail=command_result.stdout_tail,
                        stderr_tail=command_result.stderr_tail,
                        duration_seconds=command_result.duration_seconds,
                        backend=command_result.backend,
                    )
            results.append(check_result)
            artifacts.extend(
                self._command_artifacts(
                    command_result, session_id=session_id, task_id=task_id, run_id=run_id
                )
            )
            if check.id == "flutter-apk-debug" and command_result.ok:
                apk_result, apk_artifacts = self._apk_evidence(
                    root, session_id=session_id, task_id=task_id, run_id=run_id
                )
                results.append(apk_result)
                artifacts.extend(apk_artifacts)
        blocked = any(item.required and item.status == "blocked" for item in results)
        ok = not any(
            item.required and item.status in {"blocked", "failed"} for item in results
        )
        return VerificationReport(
            ok=ok,
            blocked=blocked,
            profile_id=plan.profile_id,
            workspace=str(root),
            checks=tuple(results),
            artifacts=tuple(artifacts),
        )

    @staticmethod
    def _command_check(command: VerificationCommand) -> VerificationCheck:
        return VerificationCheck(
            id=command.id,
            kind="command",
            required=command.required,
            available=True,
            argv=command.argv,
            timeout_seconds=command.timeout_seconds,
            artifact_kind=command.artifact_kind,
        )

    def _run_command(
        self, root: Path, profile: ToolchainProfile, check: VerificationCheck
    ) -> CommandResult:
        policy_id = f"verify:{profile.id}:{check.id}"
        executable = check.argv[0]
        policy = CommandPolicy(
            id=policy_id,
            executable_candidates=(executable,),
            executable_aliases=(Path(executable).name,),
            allowed_prefixes=(check.argv[1:],),
            denied_tokens=("install", "publish", "deploy", "release", "upload"),
            max_timeout_seconds=max(1.0, check.timeout_seconds),
            docker_image=(
                profile.docker_image if check.kind == "command" else None
            ),
            container_executable=_container_executable(profile.id, executable),
            host_allowed=True,
        )
        self.runner.register_policy(policy)
        return self.runner.run(
            CommandRequest(
                workspace=root,
                profile_id=policy_id,
                argv=check.argv,
                timeout_seconds=check.timeout_seconds,
                backend="auto",
            )
        )

    @staticmethod
    def _command_result(
        check: VerificationCheck, result: CommandResult
    ) -> VerificationCheckResult:
        error = "" if result.ok else result.stderr_tail or "Command exited unsuccessfully"
        return VerificationCheckResult(
            id=check.id,
            kind=check.kind,
            required=check.required,
            status="passed" if result.ok else "failed",
            ok=result.ok,
            error=error,
            stdout_tail=result.stdout_tail,
            stderr_tail=result.stderr_tail,
            duration_seconds=result.duration_seconds,
            backend=result.backend,
        )

    def _command_artifacts(
        self,
        result: CommandResult,
        *,
        session_id: int | None,
        task_id: int | None,
        run_id: int | None,
    ) -> list[VerificationArtifact]:
        return [
            self._artifact(
                "command-stdout",
                result.stdout_log,
                {"command_run_id": result.run_id},
                session_id,
                task_id,
                run_id,
            ),
            self._artifact(
                "command-stderr",
                result.stderr_log,
                {"command_run_id": result.run_id},
                session_id,
                task_id,
                run_id,
            ),
        ]

    def _apk_evidence(
        self,
        root: Path,
        *,
        session_id: int | None,
        task_id: int | None,
        run_id: int | None,
    ) -> tuple[VerificationCheckResult, list[VerificationArtifact]]:
        apk = root / "build" / "app" / "outputs" / "flutter-apk" / "app-debug.apk"
        if not apk.is_file():
            return (
                VerificationCheckResult(
                    id="apk-artifact",
                    kind="artifact",
                    required=True,
                    status="failed",
                    ok=False,
                    error=f"Expected APK was not produced: {apk}",
                ),
                [],
            )
        artifact = self._artifact(
            "apk", apk, {"bytes": apk.stat().st_size}, session_id, task_id, run_id
        )
        return (
            VerificationCheckResult(
                id="apk-artifact",
                kind="artifact",
                required=True,
                status="passed",
                ok=True,
            ),
            [artifact],
        )

    def _run_browser(
        self,
        check: VerificationCheck,
        root: Path,
        url: str,
        session_id: int | None,
        task_id: int | None,
        run_id: int | None,
    ) -> tuple[VerificationCheckResult, list[VerificationArtifact]]:
        assert self.browser_capture is not None
        try:
            payload = self.browser_capture(url=url, workspace=root)
        except Exception as exc:
            payload = {"ok": False, "error": str(exc)}
        artifacts: list[VerificationArtifact] = []
        missing: list[str] = []
        for raw in payload.get("screenshots") or []:
            path = Path(str(raw)).expanduser().resolve()
            if path.is_file():
                artifacts.append(
                    self._artifact(
                        "screenshot", path, {"url": url}, session_id, task_id, run_id
                    )
                )
            else:
                missing.append(str(path))
        trace_raw = str(payload.get("trace") or "")
        if trace_raw:
            trace = Path(trace_raw).expanduser().resolve()
            if trace.is_file():
                artifacts.append(
                    self._artifact(
                        "playwright-trace", trace, {"url": url}, session_id, task_id, run_id
                    )
                )
            else:
                missing.append(str(trace))
        ok = bool(payload.get("ok")) and not missing
        error = str(payload.get("error") or "")
        if missing:
            error = f"Browser evidence files were not produced: {', '.join(missing)}"
        return (
            VerificationCheckResult(
                id=check.id,
                kind=check.kind,
                required=check.required,
                status="passed" if ok else "failed",
                ok=ok,
                error=error,
            ),
            artifacts,
        )

    def _artifact(
        self,
        kind: str,
        path: Path,
        metadata: dict[str, Any],
        session_id: int | None,
        task_id: int | None,
        run_id: int | None,
    ) -> VerificationArtifact:
        record_id = None
        if self.store is not None and session_id is not None:
            record = self.store.create_artifact(
                session_id,
                task_id=task_id,
                run_id=run_id,
                kind=kind,
                uri=str(path),
                metadata=metadata,
            )
            record_id = record.id
        return VerificationArtifact(kind, str(path), metadata, record_id)


def _online_android_devices(output: str) -> tuple[str, ...]:
    devices: list[str] = []
    for line in output.splitlines():
        match = re.match(r"^([^\s]+)\s+device(?:\s|$)", line.strip())
        if match:
            devices.append(match.group(1))
    return tuple(devices)


def _container_executable(profile_id: str, executable: str) -> str | None:
    if profile_id == "python-saas":
        return "python"
    if profile_id == "node-saas":
        return "npm" if Path(executable).name.lower().startswith("npm") else "node"
    return None
