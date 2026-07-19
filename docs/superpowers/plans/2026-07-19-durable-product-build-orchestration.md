# Durable Product Build Orchestration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Zade a durable, local-first SaaS/mobile build lifecycle with governed execution, background control, integrated verification, GitHub/Xcode evidence, optional provider review, and calibration.

**Architecture:** Add focused runner, profile, orchestration, worker, verification, GitHub, review, and calibration modules around the existing build assessment/lease ledger. Keep `BuildService` as the compatibility facade while durable task state lives in `BuildStore` and all commands pass through one governed runner.

**Tech Stack:** Python 3.11+, FastAPI, SQLite, subprocess argv execution, optional Docker Desktop, Playwright, Flutter/Android tools, GitHub CLI, Anthropic SDK, optional OpenAI SDK, pytest.

## Global Constraints

- Local discovery, planning, implementation, commands, and verification must run without a cloud lease.
- No shell strings, arbitrary package installation, inherited cloud keys, automatic paid fallback, automatic lease enlargement, deployment, signing, or store submission.
- Provider requests require provider-specific typed approval, egress authorization, reservation, and settlement.
- GitHub writes require a separate external-action authorization.
- Docker images must exist locally; Zade never pulls an image automatically.
- Managed Agents remain non-executing and readiness-only.
- Tests never perform paid model calls or external GitHub writes.

---

### Task 1: Governed Command Runner And Toolchain Profiles

**Files:**
- Create: `src/cofounder_kernel/command_runner.py`
- Create: `src/cofounder_kernel/toolchain_profiles.py`
- Create: `tests/test_command_runner.py`
- Create: `tests/test_toolchain_profiles.py`
- Modify: `src/cofounder_kernel/coding_agent.py`

**Interfaces:**
- Produces `CommandRequest`, `CommandPreflight`, `CommandResult`, `RunningCommand`, `CommandPolicy`, `GovernedCommandRunner`, `ToolchainProbe`, `VerificationCommand`, `ToolchainProfile`, and `ToolchainRegistry`.
- `CodingAgentService` consumes `GovernedCommandRunner` but preserves existing constructor behavior when no runner is injected.

- [ ] Write failing tests for path confinement, executable resolution, argument policies, credential stripping, bounded logs, timeout, cancellation, Docker eligibility, and Python/Node/Flutter/Android discovery.
- [ ] Run `python -m pytest -q tests/test_command_runner.py tests/test_toolchain_profiles.py` and confirm missing-module failures.
- [ ] Implement immutable request/result types and policy validation.
- [ ] Implement host process lifecycle with `shell=False`, process-group cancellation, bounded tails, and redacted audit details.
- [ ] Implement optional Docker execution using an existing exact image and `--network none`.
- [ ] Implement stack detection and well-known Windows executable candidates.
- [ ] Route coding-agent commands through the runner without widening existing command behavior.
- [ ] Run the focused tests and `tests/test_coding_agent.py`.
- [ ] Commit as `feat: add governed build command runner`.

### Task 2: Durable Tasks, Attempts, Artifacts, And Calibration Storage

**Files:**
- Modify: `src/cofounder_kernel/build_types.py`
- Modify: `src/cofounder_kernel/build_store.py`
- Create: `tests/test_build_orchestration_store.py`

**Interfaces:**
- Produces `BuildTask`, `BuildTaskRun`, `BuildArtifact`, `BuildCalibration`, task status constants, and store CRUD/claim/recovery methods.
- Later tasks consume `ready_tasks`, `claim_task`, `finish_task_run`, `recover_interrupted_runs`, and session control methods.

- [ ] Write failing migration and behavior tests for phase ordering, dependency readiness, atomic claim, idempotent plan keys, attempts, artifacts, pause/resume/cancel, and restart recovery.
- [ ] Run the focused store test and confirm schema/API failures.
- [ ] Add idempotent SQLite tables and indexes.
- [ ] Implement typed row mapping and atomic store methods.
- [ ] Preserve compatibility with existing session/lease/usage records.
- [ ] Run store, budget, and integration persistence tests.
- [ ] Commit as `feat: persist durable build task graphs`.

### Task 3: Local-First Orchestrator And Background Control

**Files:**
- Create: `src/cofounder_kernel/build_orchestrator.py`
- Create: `src/cofounder_kernel/build_workers.py`
- Modify: `src/cofounder_kernel/build_service.py`
- Create: `tests/test_build_orchestrator.py`
- Create: `tests/test_build_workers.py`
- Modify: `tests/test_build_service.py`

**Interfaces:**
- Produces `BuildPlanner`, `BuildOrchestrator`, and `BuildExecutionManager`.
- `BuildService.run` aliases one synchronous ready-task execution; `start`, `pause`, `resume`, and `cancel` delegate to the manager.

- [ ] Write failing tests proving no lease is required for local work, cloud tasks block without the matching lease, dependency order is enforced, planning is idempotent, workers survive task failure, and cancellation is durable.
- [ ] Run focused tests and confirm missing orchestration APIs.
- [ ] Implement deterministic default graphs for generic, Python SaaS, Node SaaS, and Flutter mobile sessions.
- [ ] Implement one-ready-task execution with local agent, command, checkpoint, verification, GitHub, and review dispatch seams.
- [ ] Change lease checks so only a cloud route requires a lease.
- [ ] Implement bounded background workers, cooperative cancellation, and serving-boot recovery.
- [ ] Run service, routing, worker, and delegation tests.
- [ ] Commit as `feat: orchestrate durable local-first builds`.

### Task 4: SaaS, Mobile, Playwright, And Android Verification

**Files:**
- Create: `src/cofounder_kernel/build_verification.py`
- Create: `tests/test_build_verification.py`
- Modify: `src/cofounder_kernel/browser.py`
- Modify: `tests/test_browser.py`

**Interfaces:**
- Produces `VerificationPlan`, `VerificationCheckResult`, `VerificationReport`, and `BuildVerificationService`.
- Consumes `ToolchainRegistry`, `GovernedCommandRunner`, and an injectable browser-evidence adapter.

- [ ] Write failing tests for required/unavailable checks, Python, Node, Flutter, APK artifacts, online/offline ADB, Playwright screenshots/traces, and result aggregation.
- [ ] Run focused tests and confirm missing verification service.
- [ ] Implement profile-driven check plans and artifact registration.
- [ ] Reuse the existing Playwright execution boundary through an injectable adapter; do not create a second browser engine.
- [ ] Ensure unavailable required tools block completion and optional checks are reported as skipped.
- [ ] Run verification, browser, and coding-agent tests.
- [ ] Commit as `feat: add product build verification profiles`.

### Task 5: Governed GitHub And Xcode CI Integration

**Files:**
- Create: `src/cofounder_kernel/github_ci.py`
- Create: `tests/test_github_ci.py`
- Modify: `src/cofounder_kernel/build_orchestrator.py`
- Modify: `tests/test_build_orchestrator.py`

**Interfaces:**
- Produces `GitHubRun`, `GitHubCIClient`, read methods, and authorization-gated dispatch/cancel methods.
- Flutter release tasks consume `dispatch_workflow`, `find_run`, and `wait_for_run`.

- [ ] Write failing tests for repository detection, auth blockers, read calls, approval refusal, workflow dispatch, terminal polling, cancellation, output bounds, and run metadata.
- [ ] Run focused tests and confirm missing client.
- [ ] Implement argv-only GitHub CLI calls through the governed runner.
- [ ] Require an authorization callback for every write operation.
- [ ] Integrate configured iOS workflow evidence into Flutter release tasks.
- [ ] Run GitHub and orchestrator tests.
- [ ] Commit as `feat: add governed GitHub build verification`.

### Task 6: Optional OpenAI Review And Build Calibration

**Files:**
- Create: `src/cofounder_kernel/openai_review.py`
- Create: `src/cofounder_kernel/build_calibration.py`
- Create: `tests/test_openai_review.py`
- Create: `tests/test_build_calibration.py`
- Modify: `src/cofounder_kernel/config.py`
- Modify: `src/cofounder_kernel/build_store.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

**Interfaces:**
- Produces `OpenAIReviewClient`, `ReviewResult`, `BuildCalibrationService`, and `ManagedAgentsReadinessService`.
- Adds disabled-by-default OpenAI review configuration and provider-specific lease lookup.

- [ ] Write failing tests for disabled/missing-key/missing-SDK states, host enforcement, `store=False`, Responses API parsing, usage extraction, no fallback, provider lease enforcement, calibration comparisons, and managed-agent readiness gates.
- [ ] Run focused tests and confirm missing modules/config.
- [ ] Add a lazy optional OpenAI SDK dependency and disabled configuration.
- [ ] Implement the review adapter with injected SDK factory and egress authorization.
- [ ] Generalize active lease lookup by provider without changing Anthropic behavior.
- [ ] Implement immutable calibration records and advisory recommendations.
- [ ] Implement readiness-only Managed Agents reporting.
- [ ] Run provider, config, budget, egress, and calibration tests.
- [ ] Commit as `feat: add optional review and build calibration`.

### Task 7: API, UI, Self-Inventory, And Documentation

**Files:**
- Modify: `src/cofounder_kernel/api.py`
- Modify: `tests/test_build_integration.py`
- Modify: `tests/test_api.py`
- Modify: `ui/swarm.html`
- Modify: `tests/test_ui_shell.py`
- Modify: `src/cofounder_kernel/self_knowledge.py`
- Modify: `tests/test_self_knowledge.py`
- Modify: `README.md`
- Modify: `config.toml`

**Interfaces:**
- Exposes the routes enumerated in the design and preserves current build endpoints.
- Adds one Delegation build-workspace surface without arbitrary command input.

- [ ] Write failing protected-route and UI contract tests for plan/tasks/start/run-next/pause/resume/cancel/runs/toolchains/verify/GitHub/review/calibration/readiness.
- [ ] Run focused API/UI tests and confirm route failures.
- [ ] Wire services during app creation and serving-boot recovery.
- [ ] Implement protected routes with typed request models and bounded responses.
- [ ] Add phase/task/run/toolchain/verification controls and status rendering to the existing Delegation UI.
- [ ] Update self-inventory and README/config documentation with exact boundaries and prerequisites.
- [ ] Run API, integration, UI, self-knowledge, and offline acceptance tests.
- [ ] Commit as `feat: expose durable product build orchestration`.

### Task 8: End-To-End Verification, Canary, And Review

**Files:**
- Modify only files required by verified review findings.

**Interfaces:**
- Produces final test evidence, live toolchain evidence, canary calibration when approved, and a reviewed feature branch.

- [ ] Run `python -m pytest -q` against the complete repository.
- [ ] Run compile verification and inspect `git diff --check`.
- [ ] Start the worktree runtime on a non-production port with temporary data roots and run protected API smokes.
- [ ] Verify Docker daemon/backend, Flutter analysis/tests/APK, ADB emulator visibility, Playwright browser evidence, GitHub auth, and the existing iOS workflow read path.
- [ ] Prepare a disposable Small Anthropic assessment against `zade-mobile-toolchain-canary`; do not transmit until the founder supplies the typed approval phrase immediately before the call.
- [ ] If approved, run one bounded cloud turn, capture provider usage, and generate calibration without upgrading the lease.
- [ ] Run a security review focused on process escape, credential leakage, approval bypass, cancellation races, and provider fallback.
- [ ] Fix all high-severity findings and rerun affected plus complete tests.
- [ ] Commit final review fixes and report every unavailable external prerequisite.
