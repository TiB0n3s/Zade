# Project Intake Autopilot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a live, Vault-native project-intake lane that registers and builds mobile projects, starts Same Ground from its documentation, resets The Dark Index into a clean repository, and uses Telegram for durable founder decisions.

**Architecture:** A deep `ProjectIntakeService` module owns discovery, manifests, project persistence, documentation ingestion, build-session orchestration, and decision resume. Thin HTTP and filesystem-watcher adapters call that interface. Telegram is added as a proactive notification adapter using the existing bound-founder and egress rules.

**Tech Stack:** Python 3.14, FastAPI, SQLite, PowerShell `FileSystemWatcher`, existing Zade build/delegation modules, direct Telegram Bot API adapter, pytest.

## Global Constraints

- Intake root is exactly `C:\AI Brain\project-intake`.
- Only direct-child project directories containing `.git` or `project.md` are eligible.
- Both initial projects are `mobile_application` products targeting `google_play` and `apple_app_store_eventual`.
- Same Ground documentation-only intake authorizes automatic repository initialization and mobile scaffolding.
- No legacy Dark Index implementation, Git history, package metadata, dependencies, generated artifacts, `.zade` state, technical plans, or completion claims may enter the new project or Zade's retrieval context.
- The old `C:\BookCatalogingApp` tree is moved intact to a timestamped recoverable quarantine under `C:\AI Brain\.trash\dark-index-legacy`; it is never hard-deleted by this plan.
- Telegram sends only to already bound founder chats and only through the `reply_text:telegram` egress grant.
- Publishing, deployment, store submission, signing, credentials, paid leases, destructive changes, and material policy choices remain founder decisions.
- Preserve all unrelated dirty-worktree changes.

---

### Task 1: Project-intake configuration and persistence

**Files:**
- Modify: `src/cofounder_kernel/config.py`
- Modify: `src/cofounder_kernel/db.py`
- Modify: `config.toml`
- Test: `tests/test_project_intake.py`

**Interfaces:**
- Produces: `PathConfig.project_intake_dir: Path`.
- Produces: `ProjectIntakeConfig(enabled, scaffold_on_intake, watcher_debounce_seconds)`.
- Produces DB methods `upsert_project`, `get_project`, `list_projects`, `append_project_event`, and `find_project_by_path`.

- [ ] **Step 1: Write failing configuration and schema tests**

```python
def test_project_intake_defaults_below_hot_root(tmp_path):
    cfg = KernelConfig(paths=PathConfig(hot_root=tmp_path / "brain"))
    assert cfg.paths.project_intake_dir == tmp_path / "brain" / "project-intake"

def test_project_record_round_trip(db, tmp_path):
    project_id = db.upsert_project(
        canonical_path=str(tmp_path / "Same Ground"),
        name="Same Ground",
        product_type="mobile_application",
        distribution_targets=["google_play", "apple_app_store_eventual"],
        lifecycle_state="discovered",
        repo_fingerprint="docs-only",
        metadata={"scaffold_on_intake": True},
    )
    assert db.get_project(project_id)["name"] == "Same Ground"
```

- [ ] **Step 2: Run the tests and confirm the missing interfaces fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_project_intake.py -q`

Expected: failures for missing `project_intake_dir` and DB methods.

- [ ] **Step 3: Implement the config and append-only schema**

Add `project_intake_dir`, a `[project_intake]` config record, and `projects`/`project_events` migrations. Normalize JSON through existing DB helpers and enforce a unique canonical path.

- [ ] **Step 4: Make local-path initialization create the intake root**

Extend `ensure_local_paths()` with `config.paths.project_intake_dir.mkdir(parents=True, exist_ok=True)` and set:

```toml
[project_intake]
enabled = true
scaffold_on_intake = true
watcher_debounce_seconds = 3
```

- [ ] **Step 5: Run focused tests and commit**

Run: `.venv\Scripts\python.exe -m pytest tests/test_project_intake.py -q`

Commit: `feat: add project intake persistence`

### Task 2: Deep project discovery and manifest module

**Files:**
- Create: `src/cofounder_kernel/project_intake.py`
- Create: `src/cofounder_kernel/project_manifest.py`
- Test: `tests/test_project_intake.py`

**Interfaces:**
- Consumes: DB methods and `PathConfig.project_intake_dir` from Task 1.
- Produces: `ProjectIntakeService.scan()`, `get(project_id)`, `run_until_blocked(project_id)`, and `resolve_decision(decision_id, answer)`.
- Produces: `load_project_manifest(path) -> ProjectManifest` and `write_project_manifest(path, manifest)`.

- [ ] **Step 1: Write failing path-confinement and idempotency tests**

```python
def test_scan_accepts_only_qualifying_direct_children(service, intake_root):
    valid = intake_root / "Same Ground"
    valid.mkdir()
    (valid / "project.md").write_text("---\nname: Same Ground\nproduct_type: mobile_application\n---\n")
    nested = valid / "nested"
    nested.mkdir()
    (nested / ".git").mkdir()
    result = service.scan()
    assert [item["name"] for item in result.projects] == ["Same Ground"]

def test_scan_is_idempotent(service, intake_root):
    project = intake_root / "App"
    project.mkdir()
    (project / ".git").mkdir()
    assert service.scan().created_count == 1
    assert service.scan().created_count == 0
```

- [ ] **Step 2: Verify the tests fail because the module is absent**

Run: `.venv\Scripts\python.exe -m pytest tests/test_project_intake.py -q`

- [ ] **Step 3: Implement manifest parsing and safe direct-child discovery**

Resolve every candidate path and require `candidate.parent == intake_root.resolve()`. Reject symlinks, the intake root itself, malformed manifests, and directories without `.git` or `project.md`. Fingerprint Git projects from the Git root plus HEAD/status; fingerprint documentation-only projects from allowed source files and manifest content.

- [ ] **Step 4: Implement canonical manifest writing**

Write Markdown with YAML front matter containing exact keys:

```yaml
name: Same Ground
product_type: mobile_application
lifecycle_state: intake
distribution_targets: [google_play, apple_app_store_eventual]
scaffold_on_intake: true
```

- [ ] **Step 5: Run focused tests and commit**

Run: `.venv\Scripts\python.exe -m pytest tests/test_project_intake.py -q`

Commit: `feat: discover project intake folders`

### Task 3: Documentation ingestion and governed scaffolding

**Files:**
- Modify: `src/cofounder_kernel/project_intake.py`
- Modify: `src/cofounder_kernel/api.py`
- Modify: `src/cofounder_kernel/build_workspace.py`
- Test: `tests/test_project_intake.py`
- Test: `tests/test_build_integration.py`

**Interfaces:**
- Consumes: `IngestionService`, `DelegationService`, `BuildService`, and project records.
- Produces routes `POST /project-intake/scan`, `GET /project-intake/projects`, `GET /project-intake/projects/{id}`, and `POST /project-intake/projects/{id}/run`.

- [ ] **Step 1: Write failing documentation-only scaffold tests**

```python
def test_same_ground_docs_trigger_repository_and_scaffold(client, intake_root):
    root = intake_root / "Same Ground"
    root.mkdir()
    (root / "project.md").write_text(SAME_GROUND_MANIFEST)
    (root / "Same_Ground_Zade_Handoff.md").write_text("Same Ground is a mobile app")
    result = client.post("/project-intake/scan", headers=token_headers()).json()
    project = result["projects"][0]
    assert (root / ".git").is_dir()
    assert project["product_type"] == "mobile_application"
    assert project["lifecycle_state"] in {"building", "blocked", "verified"}
```

- [ ] **Step 2: Verify red against the missing routes and orchestration**

Run: `.venv\Scripts\python.exe -m pytest tests/test_project_intake.py tests/test_build_integration.py -q`

- [ ] **Step 3: Implement authenticated scan and project read routes**

Use the existing mutation-token dependency. Return created/existing/error counts plus project summaries. Register the module and dependencies in `create_app()` and expose it on `app.state.project_intake`.

- [ ] **Step 4: Implement documentation ingestion and scaffold work orders**

Ingest only supported source documents with metadata `{project_id, project_name, intake_source: true}`. For a documentation-only project with `scaffold_on_intake: true`, initialize Git through an argv-only runner, inspect installed mobile tooling, and route a build order whose acceptance requires a runnable starter app, tests/type checks, Android readiness, and preserved Apple-target metadata. Do not hard-code Expo, React Native CLI, Flutter, or native Android before the local tooling inspection.

- [ ] **Step 5: Restrict build workspaces to registered direct children**

Extend `BuildWorkspacePolicy` with an optional registered-project predicate and test that the intake container itself, nested dependencies, quarantine, and unregistered siblings are refused.

- [ ] **Step 6: Run focused tests and commit**

Run: `.venv\Scripts\python.exe -m pytest tests/test_project_intake.py tests/test_build_integration.py tests/test_delegation.py -q`

Commit: `feat: scaffold documented mobile projects`

### Task 4: Proactive Telegram notification adapter and decision resume

**Files:**
- Modify: `src/cofounder_kernel/notify.py`
- Modify: `src/cofounder_kernel/telegram_adapter.py`
- Modify: `src/cofounder_kernel/api.py`
- Modify: `src/cofounder_kernel/project_intake.py`
- Test: `tests/test_notify.py`
- Test: `tests/test_telegram_adapter.py`
- Test: `tests/test_project_intake.py`

**Interfaces:**
- Produces: `TelegramAdapter.send_bound_founders(text: str) -> TelegramDeliveryResult`.
- Consumes: project decision events and existing `NotificationBus.notify()`.
- Produces: notification channel `telegram` with severity, quiet-hours, rate-limit, and dedupe behavior.

- [ ] **Step 1: Write failing proactive-delivery tests**

```python
def test_telegram_notification_only_targets_bound_founders(adapter, db, fake_client):
    bind_founder(db, external_id="42", channel="telegram")
    adapter.send_bound_founders("Same Ground needs a decision")
    assert fake_client.sent == [(42, "Same Ground needs a decision")]

def test_project_decision_is_deduplicated(bus, project_service):
    project_service.block_for_decision(project_id=1, decision_id=9, question="Choose storage")
    project_service.block_for_decision(project_id=1, decision_id=9, question="Choose storage")
    assert len(bus.list(topic="project.decision_required")) == 1
```

- [ ] **Step 2: Verify red**

Run: `.venv\Scripts\python.exe -m pytest tests/test_notify.py tests/test_telegram_adapter.py tests/test_project_intake.py -q`

- [ ] **Step 3: Add the proactive Telegram adapter**

Reuse `TelegramClient.send_message`, bound channel identities, `reply_text:telegram`, maximum reply length, and audit conventions. Add `telegram` to notification defaults as enabled with `warning` minimum severity and a bounded hourly rate. Record every delivery or suppression.

- [ ] **Step 4: Add durable decision messages and reply resolution**

Decision text names the project, question, recommendation, risk, decision/approval identifier, and paused state. Extend the governed inbound route to resolve an unambiguous referenced decision, append the founder answer, and resume `run_until_blocked`. An ambiguous answer returns a clarification and makes no state change.

- [ ] **Step 5: Run focused tests and commit**

Run: `.venv\Scripts\python.exe -m pytest tests/test_notify.py tests/test_telegram_adapter.py tests/test_project_intake.py -q`

Commit: `feat: notify project decisions through Telegram`

### Task 5: Project-intake watcher and logon installation

**Files:**
- Create: `scripts/run-project-intake-watcher.ps1`
- Create: `scripts/install-project-intake-watcher-task.ps1`
- Test: `tests/test_project_intake_watcher_scripts.py`

**Interfaces:**
- Consumes: `POST /project-intake/scan` and `scripts/zade-token.ps1`.
- Produces: scheduled task `Zade Project Intake Watcher` and `run-logs/project-intake-watcher.jsonl`.

- [ ] **Step 1: Write failing script-contract tests**

Assert the watcher uses `System.IO.FileSystemWatcher`, watches `C:\AI Brain\project-intake`, sets `IncludeSubdirectories = $true`, debounces events, calls `POST /project-intake/scan` with `X-Zade-Token`, and logs counts. Assert the installer uses `New-ScheduledTaskTrigger -AtLogon`, a limited interactive principal, hidden/noninteractive execution, and `-ErrorAction Stop`.

- [ ] **Step 2: Run the tests and verify missing-file failures**

Run: `.venv\Scripts\python.exe -m pytest tests/test_project_intake_watcher_scripts.py -q`

- [ ] **Step 3: Implement the watcher and installer**

Follow the existing inbox watcher conventions, but trigger on qualifying folder/manifest/Git changes and call only the project-intake scan route. Include a startup reconciliation scan so projects created while the watcher was offline are discovered.

- [ ] **Step 4: Run tests and commit**

Run: `.venv\Scripts\python.exe -m pytest tests/test_project_intake_watcher_scripts.py tests/test_inbox_watcher_scripts.py -q`

Commit: `feat: watch the project intake vault`

### Task 6: Safe initial-project migration

**Files:**
- Create: `scripts/migrate-initial-project-intake.ps1`
- Test: `tests/test_project_intake_migration_script.py`
- Create at runtime: `C:\AI Brain\project-intake\The Dark Index\project.md`
- Create at runtime: `C:\AI Brain\project-intake\Same Ground\project.md`

**Interfaces:**
- Consumes exact validated source paths and produces a JSON migration receipt.
- Produces no overwrite; reruns either prove completion or stop with a conflict.

- [ ] **Step 1: Write failing migration guard tests**

Test that the script resolves every absolute source/destination, refuses a non-empty conflicting destination, verifies source hashes, never uses wildcard deletion, moves `C:\BookCatalogingApp` as one literal recoverable quarantine operation, and creates the fresh Dark Index directory only after the legacy source no longer exists.

- [ ] **Step 2: Verify red**

Run: `.venv\Scripts\python.exe -m pytest tests/test_project_intake_migration_script.py -q`

- [ ] **Step 3: Implement the migration script**

Use native PowerShell `Move-Item -LiteralPath` from source to a fully resolved timestamped quarantine. Check that both paths stay below their intended parent directories. Hash and select the founder-authored Dark Index and Same Ground source packs, move them into clean destinations, write canonical manifests, initialize no source framework directly, and write a receipt under `run-logs`.

- [ ] **Step 4: Run the migration dry-run and inspect its receipt**

Run: `.\scripts\migrate-initial-project-intake.ps1 -WhatIf`

Expected: exact source, quarantine, destination, selected source-material hashes, and no mutations.

- [ ] **Step 5: Run tests and commit the script**

Run: `.venv\Scripts\python.exe -m pytest tests/test_project_intake_migration_script.py -q`

Commit: `feat: add safe initial project migration`

### Task 7: Live cutover and end-to-end verification

**Files:**
- Modify: `config.toml`
- Modify: `src/cofounder_kernel/api.py` inventory output
- Test: `tests/test_api.py`
- Runtime artifacts: scheduled task, migration receipt, project records, project events, build sessions, notification deliveries.

**Interfaces:**
- Consumes all prior tasks.
- Produces live health and inventory truth for project intake.

- [ ] **Step 1: Add failing inventory tests**

Assert `/health` and `/self-inventory` expose the intake root, watcher contract, project counts, both initial projects, mobile product type, store targets, active build state, and Telegram proactive readiness without returning tokens or private source contents.

- [ ] **Step 2: Run the full focused suite and verify red**

Run: `.venv\Scripts\python.exe -m pytest tests/test_project_intake.py tests/test_project_intake_watcher_scripts.py tests/test_project_intake_migration_script.py tests/test_notify.py tests/test_telegram_adapter.py tests/test_api.py -q`

- [ ] **Step 3: Complete inventory output and retarget delegation**

Set `delegation.workspace_root = "C:\\AI Brain\\project-intake"`, expose registered-project confinement, and add the project-intake layer to self-inventory.

- [ ] **Step 4: Perform the verified migration and intake scan**

Run the migration without `-WhatIf`, inspect the receipt, restart Zade through `scripts/stop.ps1` then `scripts/start.ps1`, install/start the intake watcher, and call the authenticated scan route.

- [ ] **Step 5: Verify live project truth**

Confirm the old Dark Index active path is absent, quarantine is outside discovery, the new Dark Index Git history contains no legacy commits, Same Ground is scaffolded in a new Git repository, and both manifests/store targets match the spec. Query live project routes and Zade's rendered provider payload to ensure legacy implementation memories are not used as project truth.

- [ ] **Step 6: Verify Telegram decision and resume end to end**

Create one synthetic non-destructive project decision, verify exactly one Telegram delivery to the bound founder, answer it through Telegram, and confirm the same project resumes. Remove or close the synthetic decision through the normal audited lifecycle.

- [ ] **Step 7: Run regression suites and commit**

Run: `.venv\Scripts\python.exe -m pytest tests/test_project_intake.py tests/test_project_intake_watcher_scripts.py tests/test_project_intake_migration_script.py tests/test_notify.py tests/test_telegram_adapter.py tests/test_build_integration.py tests/test_delegation.py tests/test_api.py -q`

Commit: `feat: activate project intake autopilot`

## Self-Review Result

- Spec coverage: discovery, manifests, persistence, documentation ingestion, Same Ground scaffolding, Dark Index clean reset, build routing, decision gates, proactive Telegram, watcher installation, restart recovery, and live verification are each mapped to a task.
- Placeholder scan: no deferred requirements or unspecified implementation steps remain.
- Type consistency: all callers use the four-method `ProjectIntakeService` interface; project and decision identifiers remain integers; distribution targets remain string lists end to end.
