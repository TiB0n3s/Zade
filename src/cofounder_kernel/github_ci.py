"""Governed GitHub CLI integration for build and Xcode CI evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import time
from typing import Any, Callable, Mapping

from .command_runner import (
    CommandPolicy,
    CommandRequest,
    CommandResult,
    GovernedCommandRunner,
)


_RUN_FIELDS = (
    "databaseId,name,workflowName,status,conclusion,url,headBranch,headSha,"
    "event,createdAt,updatedAt"
)
_TERMINAL_STATUSES = {"completed", "cancelled", "failure", "neutral", "skipped", "stale", "success", "timed_out"}
_INPUT_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


class GitHubCIError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitHubAuthorizationRequest:
    action: str
    workspace: str
    details: dict[str, Any]


@dataclass(frozen=True)
class GitHubRun:
    id: int
    name: str
    workflow_name: str
    status: str
    conclusion: str
    url: str
    head_branch: str
    head_sha: str
    event: str
    created_at: str
    updated_at: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "GitHubRun":
        return cls(
            id=int(payload.get("databaseId") or 0),
            name=str(payload.get("name") or ""),
            workflow_name=str(payload.get("workflowName") or ""),
            status=str(payload.get("status") or ""),
            conclusion=str(payload.get("conclusion") or ""),
            url=str(payload.get("url") or ""),
            head_branch=str(payload.get("headBranch") or ""),
            head_sha=str(payload.get("headSha") or ""),
            event=str(payload.get("event") or ""),
            created_at=str(payload.get("createdAt") or ""),
            updated_at=str(payload.get("updatedAt") or ""),
        )


class GitHubCIClient:
    def __init__(
        self,
        *,
        runner: GovernedCommandRunner,
        workspace: Path | str,
        gh_executable: Path | str | None = None,
        authorize_write: Callable[[GitHubAuthorizationRequest], bool] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.runner = runner
        self.workspace = Path(workspace).expanduser().resolve()
        discovered = str(gh_executable or shutil.which("gh") or "")
        self.gh_executable = Path(discovered).expanduser().resolve() if discovered else None
        self.authorize_write = authorize_write
        self.sleep = sleep
        self.clock = clock

    def status(self) -> dict[str, Any]:
        if self.gh_executable is None or not self.gh_executable.is_file():
            return {
                "ready": False,
                "authenticated": False,
                "repository": None,
                "blockers": ["GitHub CLI is unavailable."],
            }
        auth = self._run((str(self.gh_executable), "auth", "status"), check=False)
        if not auth.ok:
            return {
                "ready": False,
                "authenticated": False,
                "repository": None,
                "blockers": [(auth.stderr_tail or "GitHub CLI is not authenticated.")[:1000]],
            }
        try:
            repository = self.repository()
        except GitHubCIError as exc:
            return {
                "ready": False,
                "authenticated": True,
                "repository": None,
                "blockers": [str(exc)],
            }
        return {
            "ready": True,
            "authenticated": True,
            "repository": repository,
            "blockers": [],
        }

    def repository(self) -> dict[str, Any]:
        payload = self._json(
            (
                self._gh(),
                "repo",
                "view",
                "--json",
                "nameWithOwner,url,defaultBranchRef",
            )
        )
        if not isinstance(payload, dict):
            raise GitHubCIError("GitHub repository response was not an object")
        branch = payload.get("defaultBranchRef")
        return {
            "name_with_owner": str(payload.get("nameWithOwner") or ""),
            "url": str(payload.get("url") or ""),
            "default_branch": (
                str(branch.get("name") or "") if isinstance(branch, dict) else ""
            ),
        }

    def list_runs(self, *, workflow: str = "", limit: int = 20) -> list[GitHubRun]:
        bounded_limit = max(1, min(int(limit), 20))
        argv = [
            self._gh(),
            "run",
            "list",
            "--json",
            _RUN_FIELDS,
            "--limit",
            str(bounded_limit),
        ]
        if workflow.strip():
            argv.extend(("--workflow", workflow.strip()))
        payload = self._json(tuple(argv))
        if not isinstance(payload, list):
            raise GitHubCIError("GitHub workflow run response was not a list")
        return [GitHubRun.from_payload(item) for item in payload if isinstance(item, dict)]

    def find_run(self, run_id: int) -> GitHubRun:
        payload = self._json(
            (self._gh(), "run", "view", str(int(run_id)), "--json", _RUN_FIELDS)
        )
        if not isinstance(payload, dict):
            raise GitHubCIError(f"GitHub workflow run {run_id} was not found")
        return GitHubRun.from_payload(payload)

    def wait_for_run(
        self,
        run_id: int,
        *,
        timeout_seconds: float = 1800,
        poll_seconds: float = 10,
    ) -> GitHubRun:
        if timeout_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("GitHub wait timeouts must be positive")
        deadline = self.clock() + timeout_seconds
        while True:
            if self.clock() > deadline:
                raise TimeoutError(f"Timed out waiting for GitHub workflow run {run_id}")
            run = self.find_run(run_id)
            if run.status.strip().lower() in _TERMINAL_STATUSES:
                return run
            self.sleep(min(poll_seconds, max(0.01, deadline - self.clock())))

    def dispatch_workflow(
        self,
        workflow: str,
        *,
        ref: str = "",
        inputs: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        clean_workflow = workflow.strip()
        if not clean_workflow:
            raise ValueError("workflow is required")
        clean_inputs = _validated_inputs(inputs or {})
        details = {
            "workflow": clean_workflow,
            "ref": ref.strip(),
            "input_names": sorted(clean_inputs),
        }
        self._authorize("workflow_dispatch", details)
        argv = [self._gh(), "workflow", "run", clean_workflow]
        if ref.strip():
            argv.extend(("--ref", ref.strip()))
        for key in sorted(clean_inputs):
            argv.extend(("-f", f"{key}={clean_inputs[key]}"))
        result = self._run(tuple(argv))
        url = next(
            (line.strip() for line in result.stdout_tail.splitlines() if line.strip().startswith("https://")),
            "",
        )
        return {"status": "dispatched", "workflow": clean_workflow, "url": url}

    def cancel_run(self, run_id: int) -> dict[str, Any]:
        normalized_id = int(run_id)
        self._authorize("run_cancel", {"run_id": normalized_id})
        self._run((self._gh(), "run", "cancel", str(normalized_id)))
        return {"status": "cancelled", "run_id": normalized_id}

    def verify_workflow(self, workflow: str) -> dict[str, Any]:
        runs = self.list_runs(workflow=workflow, limit=1)
        if not runs:
            return {
                "ok": False,
                "status": "missing",
                "error": f"No GitHub workflow runs were found for {workflow}",
            }
        run = runs[0]
        ok = run.status == "completed" and run.conclusion == "success"
        return {
            "ok": ok,
            "status": run.conclusion or run.status,
            "error": "" if ok else f"Workflow {workflow} is {run.status}/{run.conclusion}",
            "run": run.__dict__,
        }

    def execute_build_task(self, task: Any, _assessment: Any) -> dict[str, Any]:
        workflow = str(task.payload.get("workflow") or "ios.yml")
        operation = str(task.payload.get("operation") or "verify_workflow")
        if operation == "verify_workflow":
            return self.verify_workflow(workflow)
        return {
            "ok": False,
            "status": "unsupported",
            "error": f"Unsupported GitHub build operation: {operation}",
        }

    def _authorize(self, action: str, details: dict[str, Any]) -> None:
        request = GitHubAuthorizationRequest(
            action=action,
            workspace=str(self.workspace),
            details=details,
        )
        if self.authorize_write is None or not self.authorize_write(request):
            raise PermissionError(f"GitHub {action} requires fresh external-action authorization")

    def _json(self, argv: tuple[str, ...]) -> Any:
        result = self._run(argv)
        try:
            return json.loads(result.stdout_tail)
        except json.JSONDecodeError as exc:
            raise GitHubCIError("GitHub CLI returned invalid JSON") from exc

    def _run(self, argv: tuple[str, ...], *, check: bool = True) -> CommandResult:
        policy_id = "github:" + hashlib.sha256("\0".join(argv).encode("utf-8")).hexdigest()[:16]
        policy = CommandPolicy(
            id=policy_id,
            executable_candidates=(self._gh(),),
            executable_aliases=(Path(self._gh()).name,),
            allowed_prefixes=(argv[1:],),
            denied_tokens=("--web", "--force", "--log", "--log-failed"),
            max_timeout_seconds=120,
            host_allowed=True,
        )
        self.runner.register_policy(policy)
        result = self.runner.run(
            CommandRequest(
                workspace=self.workspace,
                profile_id=policy_id,
                argv=argv,
                timeout_seconds=120,
                backend="host",
            )
        )
        if check and not result.ok:
            raise GitHubCIError(
                (result.stderr_tail or result.stdout_tail or "GitHub CLI command failed")[:2000]
            )
        return result

    def _gh(self) -> str:
        if self.gh_executable is None or not self.gh_executable.is_file():
            raise GitHubCIError("GitHub CLI is unavailable")
        return str(self.gh_executable)


def _validated_inputs(inputs: Mapping[str, str]) -> dict[str, str]:
    clean: dict[str, str] = {}
    for raw_key, raw_value in inputs.items():
        key = str(raw_key).strip()
        value = str(raw_value)
        if not _INPUT_KEY.fullmatch(key):
            raise ValueError(f"Invalid GitHub workflow input name: {key!r}")
        if len(value) > 1000 or "\x00" in value:
            raise ValueError(f"GitHub workflow input {key!r} is invalid or too long")
        clean[key] = value
    return clean
