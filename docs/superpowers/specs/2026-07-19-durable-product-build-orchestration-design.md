# Durable Product Build Orchestration Design

Date: 2026-07-19
Status: Approved for implementation

## Summary

Zade will lead a SaaS or Flutter mobile product from discovery through release
readiness using a durable, local-first task graph. Local execution, repository
inspection, planning, implementation, testing, browser verification, Android
emulator work, and state persistence do not require a cloud-model lease.
Anthropic and OpenAI remain optional, separately approved delegates whose
requests are budgeted, minimized, and audited. GitHub and hosted Xcode are
external execution lanes with their own approval boundary.

The existing token-budgeted build implementation remains the accounting and
egress foundation. This design deepens it with a governed command runner,
stack-aware verification profiles, durable tasks and attempts, background
workers, cancellation, GitHub Actions control, provider-specific review, and
estimate calibration.

## Goals

- Let Zade lead a product through discovery, requirements, architecture,
  planning, implementation, verification, review, and release readiness.
- Perform useful local work before any cloud approval exists.
- Keep every command argv-based, workspace-confined, environment-filtered,
  cancellable, time-bounded, and audited.
- Prefer Docker isolation for supported Python and Node verification when the
  daemon and a pre-approved local image are available.
- Support host-only Flutter, Gradle, ADB, emulator, and local-browser workflows
  through narrow command policies.
- Persist task graphs, attempts, logs, artifacts, blockers, and cancellation so
  work can recover after a Zade restart.
- Integrate Playwright evidence and GitHub-hosted iOS/Xcode verification.
- Run Anthropic and OpenAI only through explicit provider-specific leases.
- Compare build assessment estimates with actual usage without automatically
  changing budget policy.
- Expose all current state and actions through protected API and UI surfaces.

## Non-Goals

- No unrestricted shell, inherited credential environment, or arbitrary Docker
  image execution.
- No automatic paid-provider fallback or automatic lease enlargement.
- No automatic deployment, production mutation, store submission, signing-key
  access, or billing action.
- No automatic package installation through `npx`, `pip`, Gradle, Flutter, or
  another command runner path.
- No Anthropic Managed Agents execution in this release. Readiness is measured
  and reported, but managed cloud environments remain disabled until local
  orchestration has demonstrated repeatable recovery and cancellation.
- OpenClaw is not a build executor or CI authority.

## Architecture

### Governed Command Runner

`command_runner.py` owns executable discovery, policy validation, process
lifecycle, output capture, cancellation, and optional Docker execution.

Primary interfaces:

```python
class GovernedCommandRunner:
    def preflight(self, request: CommandRequest) -> CommandPreflight: ...
    def run(self, request: CommandRequest) -> CommandResult: ...
    def start(self, request: CommandRequest) -> RunningCommand: ...
    def cancel(self, run_id: str) -> bool: ...
```

`CommandRequest` contains a workspace, profile ID, argv, timeout, environment
overrides, artifact directory, and execution backend preference. Callers never
pass a shell string. The runner resolves an executable from the profile's
configured candidates, validates every argument, resolves workspace paths, and
constructs a minimal child environment. Variables whose names contain `KEY`,
`TOKEN`, `SECRET`, `PASSWORD`, or `CREDENTIAL` are removed unless the profile
explicitly declares one for a separately approved external action.

Host processes use `subprocess.Popen(..., shell=False)`. On Windows they start
in a new process group; cancellation terminates the process tree and records the
result. Output is streamed to bounded log files outside the source workspace and
only a bounded tail is returned through API responses.

The Docker backend is optional. It is selected only when Docker is healthy, the
profile declares container support, and its exact image already exists locally.
The workspace is mounted read-write at `/workspace`; no Docker socket, host
home, credential directory, or unrelated path is mounted. Network defaults to
`none` for verification. Image pulls never happen automatically.

### Toolchain Profiles

`toolchain_profiles.py` detects product stacks from repository evidence and
produces command policies plus verification plans.

Initial profiles:

- `python-saas`: Python interpreter, pytest, compile checks, and optional
  Playwright smoke verification.
- `node-saas`: npm scripts, local TypeScript compiler, and locally installed
  Playwright. `npm install`, `npm exec --yes`, and `npx` remain blocked.
- `flutter-mobile`: Flutter pub resolution, analysis, tests, debug APK build,
  workspace Gradle wrapper checks, ADB device inventory, emulator install/run,
  and remote iOS workflow verification.
- `generic`: Git inspection and syntax checks only.

Executable discovery supports configured paths, `PATH`, and bounded Windows
locations. Flutter candidates include `C:\tools\flutter\bin\flutter.bat`;
Android candidates use `ANDROID_HOME` plus the standard per-user SDK location.
Each detected tool reports path, version probe, availability, and blocker.

Profiles declare exact command shapes. A recognized executable does not imply
that all of its subcommands are allowed.

### Durable Build Graph

The build session remains the product-level aggregate. It owns ordered tasks
across these phases:

```text
discovery -> requirements -> architecture -> planning -> implementation
-> verification -> review -> release -> complete
```

`build_tasks` stores task identity, phase, kind, dependencies, command or agent
payload, acceptance evidence, status, attempt limit, and timestamps.

Task kinds are:

- `checkpoint`: deterministic local state/evidence update.
- `agent`: bounded local coding-agent work; cloud is eligible only after local
  attempts and router criteria justify it.
- `command`: one governed command.
- `verification`: a profile-generated verification plan.
- `github`: a governed GitHub or Actions operation.
- `review`: local review or explicitly selected provider review.

Task states are `pending`, `running`, `succeeded`, `failed`, `blocked`, and
`cancelled`. A task is ready only when every dependency succeeded. Failed,
blocked, or cancelled dependencies prevent descendants from starting and are
reported as explicit blockers.

`build_task_runs` is append-only per attempt. It stores backend, redacted argv,
PID when available, start/end times, result summary, log path, artifact IDs,
and interruption/cancellation reason. A restart marks abandoned `running`
attempts `interrupted`; their tasks return to `pending` only when the configured
attempt limit permits a retry.

`build_artifacts` records logs, screenshots, Playwright traces, APK paths,
verification summaries, CI run URLs, and review reports. Artifacts are metadata
references; large contents stay on disk or in the external CI system.

### Local-Before-Cloud Routing

Absence of an Anthropic lease no longer blocks a build session. Discovery,
requirements normalization, planning, local coding-agent attempts, commands,
and verification continue locally. If deterministic routing selects cloud and
no matching lease exists, the task is blocked on a deduplicated provider lease
request while any independent local task remains eligible to run.

Cloud provider selection is explicit on the task. Anthropic cannot fall back to
OpenAI, and OpenAI cannot fall back to Anthropic. The existing reservation,
settlement, egress, and workspace-fingerprint checks remain mandatory.

### Background Execution And Cancellation

`build_workers.py` owns in-process background scheduling. It permits one active
task per build session and a configured global worker ceiling. It claims a task
transactionally, creates an attempt, and invokes the orchestrator. Long-running
commands receive a cancellation event linked to the process runner.

Session controls are:

- `start`: enqueue and run ready work in the background.
- `run-next`: synchronously execute one ready task for tests and operator use.
- `pause`: stop claiming new tasks while the current task may finish.
- `resume`: make a paused session runnable again.
- `cancel`: cancel the active command or mark the active non-command task for
  cooperative cancellation, then cancel all remaining pending tasks.

Process restart recovery runs before workers accept new work.

### Verification

`build_verification.py` runs a profile's checks through the governed runner and
stores a structured result for every check. Completion requires all required
checks to pass; unavailable required tools produce blockers, never a synthetic
pass.

Python and Node SaaS profiles can execute Playwright when the repository already
contains a Playwright configuration or the session supplies a local URL smoke
target. Browser evidence includes final URL, title, screenshot, console errors,
and trace path when available. Zade never starts an untrusted public URL through
this local-browser verification path.

Flutter verification runs analysis, tests, and a debug APK build locally. If an
emulator is online, it can install and launch the app through a profile-approved
command. Xcode remains remote: a configured GitHub workflow is dispatched and
its terminal result is attached as release evidence.

### GitHub And CI

`github_ci.py` wraps the installed `gh` CLI with argv-only calls. Read operations
include repository identity, authentication status, workflow inventory, and run
status. Write operations include branch push, workflow dispatch, run
cancellation, and pull-request creation; each requires an external-action
authorization callback before the command starts.

The initial iOS profile targets a repository-configured workflow name and
branch. It records GitHub run ID, URL, status, conclusion, commit SHA, and job
summary. A successful workflow is evidence, not release authority.

### OpenAI Review

`openai_review.py` is an optional lazy adapter built on the official OpenAI
Python SDK and the Responses API. It uses `store=False`, a stable review
instruction, minimal selected context, and no hosted tools. The default remains
disabled until the SDK, `OPENAI_API_KEY`, current pricing, and a typed provider
lease are present.

OpenAI review has a provider-specific lease and usage events in the existing
build ledger. A review task names `openai` explicitly. It cannot be selected by
provider failure and cannot modify files or execute tools. Its output is treated
as advisory evidence that the local orchestrator must verify.

### Calibration

`build_calibration.py` writes one calibration record after a completed or
stopped build. It compares predicted tier, score, file breadth, verification
burden, planned cloud reasons, actual local attempts, cloud turns, provider
usage, cost, duration, failures, and final outcome.

Calibration reports over/under-estimation and suggests threshold or envelope
review. It never changes configuration automatically. The first Anthropic
Small canary uses the disposable Flutter canary repository and requires the
normal typed lease approval immediately before transmission.

### Managed Agents Readiness

Status exposes a non-executing readiness assessment. Managed Agents stay
disabled until all of these are true:

- at least three durable local build sessions completed;
- at least one active-command cancellation was verified;
- at least two cloud build/review calibrations completed;
- restart recovery was exercised without losing task state;
- no unresolved high-severity security review finding remains.

Meeting the criteria only permits a future design decision. It does not create
an Anthropic environment, agent, session, or spend authorization.

## API

Protected routes extend the current build API:

```text
POST /build/sessions/{id}/plan
GET  /build/sessions/{id}/tasks
POST /build/sessions/{id}/tasks
POST /build/sessions/{id}/run-next
POST /build/sessions/{id}/start
POST /build/sessions/{id}/pause
POST /build/sessions/{id}/resume
POST /build/sessions/{id}/cancel
GET  /build/runs/{run_id}
POST /build/runs/{run_id}/cancel
GET  /build/toolchains
POST /build/sessions/{id}/verify
POST /build/sessions/{id}/github/dispatch
POST /build/sessions/{id}/review/prepare
POST /build/sessions/{id}/review/approve
POST /build/sessions/{id}/review/run
GET  /build/calibration
GET  /build/managed-agents/readiness
```

Existing endpoints remain compatible. `/build/sessions/{id}/run` becomes an
alias for synchronous `run-next` behavior.

## UI

The Delegation surface gains an unframed build workspace view with phase tabs,
task rows, task/run status, current blocker, local/cloud route, lease meters,
toolchain readiness, verification evidence, and controls for start, pause,
resume, cancel, and approved cloud review. It does not expose arbitrary command
entry.

## Security Invariants

- Local work never requires a cloud lease.
- A paid provider request always requires its own active lease and successful
  worst-case reservation.
- A GitHub write always requires external-action authorization.
- Commands never use a shell string.
- Executable and argument policy is checked before process creation.
- Workspace path resolution rejects traversal and symlink escape.
- Child environments exclude model keys and unrelated credentials.
- Docker never mounts the Docker socket or host credential directories.
- Cancellation, timeouts, interrupted attempts, and uncertain provider spend
  are durable audit events.
- Verification evidence, not model prose, determines task completion.

## Failure Handling

- Missing local tools block only tasks that require them and name the exact
  executable or setup command.
- A Docker outage falls back to the strict host backend only when the profile
  permits host execution.
- A command timeout cancels the process tree and fails the attempt.
- A Zade restart marks abandoned attempts interrupted and applies retry policy.
- A GitHub outage leaves the task blocked with its run metadata intact.
- A cloud pre-send failure releases its reservation; ambiguous post-send usage
  remains conservatively reserved and pauses that provider lease.
- A cancelled session does not automatically delete workspace changes.

## Testing

- Command policy tests cover executable spoofing, disallowed arguments,
  environment stripping, workspace escape, output bounds, timeout, Docker
  selection, and cancellation.
- Toolchain tests use fake executable discovery and real temporary manifests.
- Store tests cover migration, dependency readiness, atomic claims, restart
  recovery, idempotent planning, and cancellation.
- Orchestrator tests prove local work runs with no lease and cloud remains
  blocked without one.
- Worker tests cover background completion, one-task-per-session, pause/resume,
  and cooperative cancellation.
- Verification tests cover Python, Node, Flutter, Playwright, Android, and
  unavailable-tool blockers through injected runners.
- GitHub and OpenAI tests use injected transports and never perform network
  writes or paid inference.
- API tests cover every protected route and mutation token.
- Full offline acceptance tests continue to reject Anthropic/OpenAI network
  calls.
- A final live toolchain canary proves Docker readiness, local Flutter checks,
  Android emulator visibility, and GitHub iOS workflow status.

## External Prerequisites

- Docker Desktop CLI is installed. The daemon must be running to exercise the
  preferred container backend.
- Flutter, Android SDK, Gradle wrapper, ADB, emulator, GitHub CLI, and the remote
  iOS workflow have already been validated.
- The OpenAI Python SDK and `OPENAI_API_KEY` are currently absent. The provider
  remains disabled until both are supplied with current pricing configuration.
- `ANTHROPIC_API_KEY` is present, but the Small canary still requires the normal
  typed approval phrase and hard lease.
