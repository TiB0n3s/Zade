from __future__ import annotations

import json
from pathlib import Path

import pytest

from cofounder_kernel.command_runner import CommandResult
from cofounder_kernel.github_ci import GitHubCIClient, GitHubRun


class FakeRunner:
    def __init__(self, root: Path, responses: list[tuple[bool, str, str]]):
        self.root = root
        self.responses = list(responses)
        self.requests = []
        self.policies = {}

    def register_policy(self, policy):
        self.policies[policy.id] = policy

    def run(self, request):
        self.requests.append(request)
        ok, stdout, stderr = self.responses.pop(0)
        index = len(self.requests)
        run_dir = self.root / str(index)
        run_dir.mkdir(parents=True, exist_ok=True)
        stdout_log = run_dir / "stdout.log"
        stderr_log = run_dir / "stderr.log"
        stdout_log.write_text(stdout, encoding="utf-8")
        stderr_log.write_text(stderr, encoding="utf-8")
        return CommandResult(
            run_id=str(index),
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


def run_json(*, status: str = "completed", conclusion: str = "success", run_id: int = 42):
    return {
        "databaseId": run_id,
        "name": "iOS Build",
        "workflowName": "iOS Build",
        "status": status,
        "conclusion": conclusion,
        "url": f"https://github.com/acme/app/actions/runs/{run_id}",
        "headBranch": "main",
        "headSha": "abc123",
        "event": "workflow_dispatch",
        "createdAt": "2026-07-19T12:00:00Z",
        "updatedAt": "2026-07-19T12:01:00Z",
    }


def make_client(
    tmp_path: Path,
    responses: list[tuple[bool, str, str]],
    *,
    authorize=lambda _request: False,
    sleep=lambda _seconds: None,
):
    workspace = tmp_path / "repo"
    workspace.mkdir(exist_ok=True)
    gh = tmp_path / "gh.exe"
    gh.write_text("", encoding="utf-8")
    runner = FakeRunner(tmp_path / "logs", responses)
    client = GitHubCIClient(
        runner=runner,
        workspace=workspace,
        gh_executable=gh,
        authorize_write=authorize,
        sleep=sleep,
    )
    return client, runner


def test_status_reports_auth_and_repository_blockers(tmp_path: Path) -> None:
    client, runner = make_client(
        tmp_path,
        [(False, "", "not logged in")],
    )

    status = client.status()

    assert status["ready"] is False
    assert status["authenticated"] is False
    assert status["blockers"] == ["not logged in"]
    assert runner.requests[0].argv == (str(client.gh_executable), "auth", "status")


def test_repository_and_run_reads_are_structured(tmp_path: Path) -> None:
    repo = {
        "nameWithOwner": "acme/app",
        "url": "https://github.com/acme/app",
        "defaultBranchRef": {"name": "main"},
    }
    client, runner = make_client(
        tmp_path,
        [
            (True, json.dumps(repo), ""),
            (True, json.dumps([run_json()]), ""),
            (True, json.dumps(run_json()), ""),
        ],
    )

    repository = client.repository()
    runs = client.list_runs(workflow="ios.yml", limit=5)
    run = client.find_run(42)

    assert repository["name_with_owner"] == "acme/app"
    assert repository["default_branch"] == "main"
    assert runs == [run]
    assert isinstance(run, GitHubRun)
    assert run.conclusion == "success"
    assert "--limit" in runner.requests[1].argv
    assert "ios.yml" in runner.requests[1].argv


def test_writes_require_fresh_authorization_and_do_not_execute_when_denied(
    tmp_path: Path,
) -> None:
    requests = []
    client, runner = make_client(
        tmp_path,
        [],
        authorize=lambda request: requests.append(request) or False,
    )

    with pytest.raises(PermissionError, match="authorization"):
        client.dispatch_workflow("ios.yml", ref="main", inputs={"flavor": "canary"})
    with pytest.raises(PermissionError, match="authorization"):
        client.cancel_run(42)

    assert [request.action for request in requests] == [
        "workflow_dispatch",
        "run_cancel",
    ]
    assert runner.requests == []


def test_authorized_dispatch_and_cancel_use_argv_only_commands(tmp_path: Path) -> None:
    approvals = []
    client, runner = make_client(
        tmp_path,
        [
            (True, "https://github.com/acme/app/actions/runs/99\n", ""),
            (True, "", ""),
        ],
        authorize=lambda request: approvals.append(request) or True,
    )

    dispatched = client.dispatch_workflow(
        "ios.yml", ref="main", inputs={"flavor": "canary"}
    )
    cancelled = client.cancel_run(99)

    assert dispatched["status"] == "dispatched"
    assert dispatched["url"].endswith("/99")
    assert cancelled == {"status": "cancelled", "run_id": 99}
    assert runner.requests[0].argv == (
        str(client.gh_executable),
        "workflow",
        "run",
        "ios.yml",
        "--ref",
        "main",
        "-f",
        "flavor=canary",
    )
    assert runner.requests[1].argv == (
        str(client.gh_executable),
        "run",
        "cancel",
        "99",
    )
    assert len(approvals) == 2


def test_wait_for_run_polls_to_terminal_state(tmp_path: Path) -> None:
    client, _runner = make_client(
        tmp_path,
        [
            (True, json.dumps(run_json(status="queued", conclusion="")), ""),
            (True, json.dumps(run_json(status="in_progress", conclusion="")), ""),
            (True, json.dumps(run_json()), ""),
        ],
    )

    run = client.wait_for_run(42, timeout_seconds=5, poll_seconds=0.01)

    assert run.status == "completed"
    assert run.conclusion == "success"


def test_wait_for_run_times_out_without_cancelling_remote_run(tmp_path: Path) -> None:
    ticks = iter((0.0, 0.0, 2.0, 2.0))
    client, runner = make_client(
        tmp_path,
        [(True, json.dumps(run_json(status="queued", conclusion="")), "")],
    )
    client.clock = lambda: next(ticks)

    with pytest.raises(TimeoutError, match="42"):
        client.wait_for_run(42, timeout_seconds=1, poll_seconds=0.01)

    assert all("cancel" not in request.argv for request in runner.requests)
