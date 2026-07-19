from __future__ import annotations

from pathlib import Path

from cofounder_kernel.build_store import BuildStore
from cofounder_kernel.build_types import BuildAssessment, BuildTier
from cofounder_kernel.build_verification import BuildVerificationService
from cofounder_kernel.command_runner import CommandResult
from cofounder_kernel.db import KernelDatabase
from cofounder_kernel.toolchain_profiles import (
    ToolchainProbe,
    ToolchainProfile,
    VerificationCommand,
)


class FakeRegistry:
    def __init__(self, profile: ToolchainProfile):
        self._profile = profile

    def detect(self, _workspace: Path | str) -> ToolchainProfile:
        return self._profile

    def profile(self, _profile_id: str, _workspace: Path | str) -> ToolchainProfile:
        return self._profile


class FakeRunner:
    def __init__(self, root: Path, outputs: dict[str, tuple[bool, str, str]]):
        self.root = root
        self.outputs = outputs
        self.policies = {}
        self.requests = []

    def register_policy(self, policy):
        self.policies[policy.id] = policy

    def run(self, request):
        self.requests.append(request)
        command_id = request.profile_id.rsplit(":", 1)[-1]
        ok, stdout, stderr = self.outputs.get(command_id, (True, "", ""))
        run_dir = self.root / command_id
        run_dir.mkdir(parents=True, exist_ok=True)
        stdout_log = run_dir / "stdout.log"
        stderr_log = run_dir / "stderr.log"
        stdout_log.write_text(stdout, encoding="utf-8")
        stderr_log.write_text(stderr, encoding="utf-8")
        return CommandResult(
            run_id=command_id,
            ok=ok,
            returncode=0 if ok else 1,
            backend="host",
            redacted_argv=request.argv,
            stdout_tail=stdout,
            stderr_tail=stderr,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            duration_seconds=0.1,
        )


def profile(
    profile_id: str,
    *,
    commands: tuple[VerificationCommand, ...] = (),
    probes: tuple[ToolchainProbe, ...] = (),
    blockers: tuple[str, ...] = (),
) -> ToolchainProfile:
    return ToolchainProfile(
        id=profile_id,
        label=profile_id,
        probes=probes,
        verification_commands=commands,
        blockers=blockers,
    )


def make_store(tmp_path: Path, workspace: Path):
    database = KernelDatabase(tmp_path / "kernel.sqlite")
    database.migrate()
    store = BuildStore(database)
    session = store.create_session(
        BuildAssessment(
            id=None,
            task="Build product",
            acceptance="Checks pass",
            workspace=str(workspace),
            repo_fingerprint="fingerprint",
            deterministic_score=20,
            local_adjustment=0,
            final_score=20,
            confidence=0.9,
            recommended_tier=BuildTier.SMALL,
            dimensions={},
            floor_rules=(),
            evidence={},
            unknowns=(),
            local_work=(),
            cloud_reasons=(),
            created_at="2026-07-19T12:00:00+00:00",
        )
    )
    return store, session


def test_required_unavailable_toolchain_blocks_without_running_commands(tmp_path: Path) -> None:
    workspace = tmp_path / "app"
    workspace.mkdir()
    missing = ToolchainProbe(
        id="flutter",
        available=False,
        path=None,
        version_argv=(),
        blocker="Flutter is unavailable.",
    )
    runner = FakeRunner(tmp_path / "logs", {})
    service = BuildVerificationService(
        toolchains=FakeRegistry(
            profile("flutter-mobile", probes=(missing,), blockers=(missing.blocker,))
        ),
        runner=runner,
    )

    report = service.verify(workspace)

    assert report.ok is False
    assert report.blocked is True
    assert report.checks[0].status == "blocked"
    assert report.checks[0].required is True
    assert runner.requests == []


def test_python_and_node_commands_are_run_through_governed_runner(tmp_path: Path) -> None:
    workspace = tmp_path / "app"
    workspace.mkdir()
    python = Path(__import__("sys").executable)
    runner = FakeRunner(tmp_path / "logs", {"pytest": (True, "2 passed", "")})
    service = BuildVerificationService(
        toolchains=FakeRegistry(
            profile(
                "python-saas",
                commands=(VerificationCommand("pytest", (str(python), "-m", "pytest", "-q")),),
                probes=(ToolchainProbe("python", True, python, (str(python), "--version")),),
            )
        ),
        runner=runner,
    )

    report = service.verify(workspace)

    assert report.ok is True
    assert report.checks[0].status == "passed"
    assert report.checks[0].stdout_tail == "2 passed"
    assert runner.requests[0].argv[1:] == ("-m", "pytest", "-q")
    assert runner.requests[0].profile_id in runner.policies


def test_flutter_verification_requires_online_adb_and_registers_apk(tmp_path: Path) -> None:
    workspace = tmp_path / "flutter_app"
    workspace.mkdir()
    flutter = tmp_path / "flutter.bat"
    adb = tmp_path / "adb.exe"
    flutter.write_text("", encoding="utf-8")
    adb.write_text("", encoding="utf-8")
    apk = workspace / "build" / "app" / "outputs" / "flutter-apk" / "app-debug.apk"
    apk.parent.mkdir(parents=True)
    apk.write_bytes(b"apk")
    mobile = profile(
        "flutter-mobile",
        probes=(
            ToolchainProbe("flutter", True, flutter, (str(flutter), "--version")),
            ToolchainProbe("adb", True, adb, (str(adb), "version")),
        ),
        commands=(
            VerificationCommand("flutter-test", (str(flutter), "test")),
            VerificationCommand(
                "flutter-apk-debug",
                (str(flutter), "build", "apk", "--debug"),
                artifact_kind="apk",
            ),
        ),
    )
    runner = FakeRunner(
        tmp_path / "logs",
        {
            "flutter-test": (True, "All tests passed", ""),
            "flutter-apk-debug": (True, "Built app-debug.apk", ""),
            "adb-online": (
                True,
                "List of devices attached\nemulator-5554 device product:sdk model:Pixel\n",
                "",
            ),
        },
    )
    store, session = make_store(tmp_path, workspace)
    service = BuildVerificationService(
        toolchains=FakeRegistry(mobile), runner=runner, store=store
    )

    report = service.verify(workspace, session_id=session.id, android_device="emulator-5554")

    assert report.ok is True
    assert any(check.id == "adb-online" and check.status == "passed" for check in report.checks)
    assert any(artifact.kind == "apk" and artifact.uri.endswith("app-debug.apk") for artifact in report.artifacts)
    assert store.list_artifacts(session.id)


def test_offline_android_device_fails_required_check(tmp_path: Path) -> None:
    workspace = tmp_path / "flutter_app"
    workspace.mkdir()
    adb = tmp_path / "adb.exe"
    adb.write_text("", encoding="utf-8")
    mobile = profile(
        "flutter-mobile",
        probes=(ToolchainProbe("adb", True, adb, (str(adb), "version")),),
    )
    runner = FakeRunner(
        tmp_path / "logs",
        {"adb-online": (True, "List of devices attached\nemulator-5554 offline\n", "")},
    )
    service = BuildVerificationService(
        toolchains=FakeRegistry(mobile), runner=runner
    )

    report = service.verify(workspace, android_device="emulator-5554")

    assert report.ok is False
    assert next(check for check in report.checks if check.id == "adb-online").status == "failed"


def test_playwright_evidence_adds_screenshot_and_trace_artifacts(tmp_path: Path) -> None:
    workspace = tmp_path / "web_app"
    workspace.mkdir()
    screenshot = tmp_path / "evidence" / "home.png"
    trace = tmp_path / "evidence" / "trace.zip"
    screenshot.parent.mkdir()
    screenshot.write_bytes(b"png")
    trace.write_bytes(b"zip")
    calls = []

    def browser_capture(**kwargs):
        calls.append(kwargs)
        return {
            "ok": True,
            "error": "",
            "screenshots": [str(screenshot)],
            "trace": str(trace),
        }

    store, session = make_store(tmp_path, workspace)
    service = BuildVerificationService(
        toolchains=FakeRegistry(profile("node-saas")),
        runner=FakeRunner(tmp_path / "logs", {}),
        store=store,
        browser_capture=browser_capture,
    )

    report = service.verify(
        workspace,
        session_id=session.id,
        browser_url="http://127.0.0.1:3000",
    )

    assert report.ok is True
    assert calls[0]["url"] == "http://127.0.0.1:3000"
    assert {artifact.kind for artifact in report.artifacts} >= {"screenshot", "playwright-trace"}


def test_optional_failed_check_does_not_fail_report(tmp_path: Path) -> None:
    workspace = tmp_path / "app"
    workspace.mkdir()
    executable = Path(__import__("sys").executable)
    optional = VerificationCommand(
        "optional-lint", (str(executable), "-c", "print('lint')"), required=False
    )
    runner = FakeRunner(tmp_path / "logs", {"optional-lint": (False, "", "lint failed")})
    service = BuildVerificationService(
        toolchains=FakeRegistry(profile("python-saas", commands=(optional,))),
        runner=runner,
    )

    report = service.verify(workspace)

    assert report.ok is True
    assert report.checks[0].status == "failed"
    assert report.checks[0].required is False
