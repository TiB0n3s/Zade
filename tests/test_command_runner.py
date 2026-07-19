from __future__ import annotations

import os
from pathlib import Path
import sys
import time

import pytest

from cofounder_kernel.command_runner import (
    CommandPolicy,
    CommandPolicyError,
    CommandRequest,
    GovernedCommandRunner,
    coding_agent_command_policies,
)


def _python_policy(*, docker_image: str | None = None) -> CommandPolicy:
    return CommandPolicy(
        id="test-python",
        executable_candidates=(sys.executable,),
        executable_aliases=("python", "python.exe"),
        allowed_prefixes=(("-c",),),
        allowed_env=("SAFE_FLAG",),
        max_timeout_seconds=5,
        docker_image=docker_image,
    )


def _runner(tmp_path: Path, policy: CommandPolicy, **kwargs) -> GovernedCommandRunner:
    return GovernedCommandRunner(
        policies={policy.id: policy},
        artifact_root=tmp_path / "artifacts",
        **kwargs,
    )


def test_preflight_rejects_unknown_program_and_workspace_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = _python_policy()
    runner = _runner(tmp_path, policy)

    with pytest.raises(CommandPolicyError, match="not permitted"):
        runner.preflight(
            CommandRequest(
                workspace=workspace,
                profile_id=policy.id,
                argv=("powershell", "-c", "Write-Host nope"),
            )
        )

    with pytest.raises(CommandPolicyError, match="workspace"):
        runner.preflight(
            CommandRequest(
                workspace=workspace,
                profile_id=policy.id,
                argv=("python", "-c", "print('x')", "../outside.txt"),
            )
        )


def test_preflight_rejects_shell_metacharacters_and_unapproved_shape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = _python_policy()
    runner = _runner(tmp_path, policy)

    with pytest.raises(CommandPolicyError, match="shape"):
        runner.preflight(
            CommandRequest(
                workspace=workspace,
                profile_id=policy.id,
                argv=("python", "-m", "pip", "install", "requests"),
            )
        )

    with pytest.raises(CommandPolicyError, match="shell metacharacter"):
        runner.preflight(
            CommandRequest(
                workspace=workspace,
                profile_id=policy.id,
                argv=("python", "-c", "print('x')", "&&"),
            )
        )


def test_run_strips_credentials_and_keeps_only_allowed_overrides(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = _python_policy()
    runner = _runner(tmp_path, policy)
    request = CommandRequest(
        workspace=workspace,
        profile_id=policy.id,
        argv=(
            "python",
            "-c",
            (
                "import os; print(os.getenv('SAFE_FLAG')); "
                "print(os.getenv('OPENAI_API_KEY')); print(os.getenv('ANTHROPIC_API_KEY'))"
            ),
        ),
        env={
            "SAFE_FLAG": "visible",
            "OPENAI_API_KEY": "must-not-leak",
            "ANTHROPIC_API_KEY": "must-not-leak",
        },
    )

    result = runner.run(request)

    assert result.ok is True
    assert result.stdout_tail.splitlines() == ["visible", "None", "None"]
    assert "must-not-leak" not in result.stdout_tail
    assert result.backend == "host"
    assert result.stdout_log.is_file()
    assert result.stderr_log.is_file()


def test_output_tail_is_bounded_while_full_log_is_retained(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = _python_policy()
    runner = _runner(tmp_path, policy, max_output_chars=64)

    result = runner.run(
        CommandRequest(
            workspace=workspace,
            profile_id=policy.id,
            argv=("python", "-c", "print('x' * 500)"),
        )
    )

    assert result.ok is True
    assert len(result.stdout_tail) <= 64
    assert len(result.stdout_log.read_text(encoding="utf-8")) > 400


def test_retained_command_logs_have_a_disk_bound(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = _python_policy()
    runner = _runner(tmp_path, policy, max_log_bytes=128)

    result = runner.run(
        CommandRequest(
            workspace=workspace,
            profile_id=policy.id,
            argv=("python", "-c", "print('x' * 500)"),
        )
    )

    assert result.ok is True
    assert result.stdout_log.stat().st_size <= 128


def test_timeout_terminates_process_and_records_timeout(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = _python_policy()
    runner = _runner(tmp_path, policy)

    result = runner.run(
        CommandRequest(
            workspace=workspace,
            profile_id=policy.id,
            argv=("python", "-c", "import time; time.sleep(30)"),
            timeout_seconds=0.1,
        )
    )

    assert result.ok is False
    assert result.timed_out is True
    assert result.cancelled is False
    assert result.duration_seconds < 5


def test_background_command_can_be_cancelled(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = _python_policy()
    runner = _runner(tmp_path, policy)

    running = runner.start(
        CommandRequest(
            workspace=workspace,
            profile_id=policy.id,
            argv=("python", "-c", "import time; time.sleep(30)"),
            timeout_seconds=5,
        )
    )
    time.sleep(0.05)

    assert runner.cancel(running.run_id) is True
    result = running.wait()
    assert result.ok is False
    assert result.cancelled is True
    assert runner.cancel(running.run_id) is False


def test_auto_backend_selects_docker_only_for_existing_approved_image(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = _python_policy(docker_image="python:3.12-local")
    runner = _runner(
        tmp_path,
        policy,
        docker_probe=lambda image: image == "python:3.12-local",
    )

    preflight = runner.preflight(
        CommandRequest(
            workspace=workspace,
            profile_id=policy.id,
            argv=("python", "-c", "print('ok')"),
            backend="auto",
        )
    )

    assert preflight.backend == "docker"
    assert preflight.docker_image == "python:3.12-local"


def test_auto_backend_falls_back_to_host_without_pulling_image(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = _python_policy(docker_image="python:missing")
    probes: list[str] = []
    runner = _runner(
        tmp_path,
        policy,
        docker_probe=lambda image: probes.append(image) or False,
    )

    preflight = runner.preflight(
        CommandRequest(
            workspace=workspace,
            profile_id=policy.id,
            argv=("python", "-c", "print('ok')"),
        )
    )

    assert probes == ["python:missing"]
    assert preflight.backend == "host"


def test_request_timeout_cannot_exceed_policy_ceiling(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = _python_policy()
    runner = _runner(tmp_path, policy)

    with pytest.raises(CommandPolicyError, match="timeout"):
        runner.preflight(
            CommandRequest(
                workspace=workspace,
                profile_id=policy.id,
                argv=("python", "-c", "print('ok')"),
                timeout_seconds=6,
            )
        )


def test_coding_agent_policies_allow_checks_but_not_install_or_payloads(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policies = coding_agent_command_policies()
    runner = GovernedCommandRunner(
        policies=policies,
        artifact_root=tmp_path / "artifacts",
        docker_probe=lambda _image: False,
    )

    allowed = runner.preflight(
        CommandRequest(
            workspace=workspace,
            profile_id="coding-agent:python",
            argv=(sys.executable, "-m", "pytest", "-q"),
        )
    )
    assert allowed.backend == "host"

    with pytest.raises(CommandPolicyError, match="shape"):
        runner.preflight(
            CommandRequest(
                workspace=workspace,
                profile_id="coding-agent:python",
                argv=(sys.executable, "-m", "pip", "install", "requests"),
            )
        )
    with pytest.raises(CommandPolicyError, match="shape"):
        runner.preflight(
            CommandRequest(
                workspace=workspace,
                profile_id="coding-agent:python",
                argv=(sys.executable, "-c", "print('no')"),
            )
        )
