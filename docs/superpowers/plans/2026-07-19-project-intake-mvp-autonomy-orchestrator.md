# Project Intake MVP Autonomy Orchestrator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every authorized project-intake project advance autonomously through its documented MVP, pausing only at canonical UI decisions, canonical UI approvals, or mechanically proven blockers.

**Architecture:** Harden the existing `ProjectAutonomyReporter` behind a transactional SQLite store, canonical authority callbacks, commit-bound evidence, a retrying notification outbox, and one shared status projection. Add a local-first planner and a durable per-project orchestrator that uses the existing delegation/work-queue path, holds one lease per project, supports two projects concurrently, and wakes from intake, startup, heartbeat, and boundary resolution.

**Tech Stack:** Python 3.14, FastAPI, SQLite/WAL, local Ollama structured output, existing Zade work queue/delegation/build verification, PowerShell watcher, vanilla HTML/JavaScript UI, pytest.

## Global Constraints

- `Same Ground` and `The Dark Index` are equal `urgent` priorities and may run concurrently.
- A single project has at most one mutation lease and one active implementation run.
- Inline native local coding is the default; existing optional coding-agent/sub-agent paths remain available.
- Cloud inference, paid services, credentials, deployment, publishing, signing, and app-store submission never become automatic fallbacks.
- Approvals and product decisions are resolved only in the existing Zade UI surfaces.
- Telegram is outbound notification-only for project autonomy.
- Model prose is never completion evidence.
- Every accepted criterion and MVP completion is bound to fresh mechanical command output and the exact tested Git commit.
- Existing project-intake routes remain backward compatible.
- Preserve unrelated worktree changes; stage and commit only each task's listed files.

---

## File Map

**Create:**

- `src/cofounder_kernel/project_autonomy_store.py` — transactional state, plan, lease, event, and notification-outbox persistence.
- `src/cofounder_kernel/project_mvp_planner.py` — local structured MVP-plan derivation and plan reconciliation input.
- `src/cofounder_kernel/project_autonomy_orchestrator.py` — wake, claim, execute, verify, repair, continue, and completion loop.
- `tests/test_project_autonomy_store.py` — migrations, CAS, atomicity, leases, and outbox tests.
- `tests/test_project_mvp_planner.py` — structured planning and invalid/ambiguous documentation tests.
- `tests/test_project_autonomy_orchestrator.py` — scheduler, concurrency, repair, boundary, and restart tests.
- `tests/test_project_autonomy_live_contract.py` — app-level wake/resume/status contract using fakes and real temporary Git repositories.

**Modify:**

- `src/cofounder_kernel/db.py` — schema version 33 and autonomy tables.
- `src/cofounder_kernel/project_autonomy.py` — reporter uses the store, guarded transitions, commit-bound evidence, shared projection.
- `src/cofounder_kernel/project_intake.py` — scan wake callback and canonical decision callback; remove autonomy-duplicate notification behavior.
- `src/cofounder_kernel/approval.py` — resolution listeners for canonical UI approval outcomes.
- `src/cofounder_kernel/notify.py` — deliver existing notification records without producer-level channel bypass.
- `src/cofounder_kernel/runtime.py` — use shared autonomy truth for project status answers.
- `src/cofounder_kernel/api.py` — construct/wire services, callbacks, lifecycle, routes, and UI-only decision endpoints.
- `src/cofounder_kernel/heartbeat.py` — bounded autonomy reconciliation callback.
- `src/cofounder_kernel/config.py`, `config.toml`, `config.example.toml` — autonomy worker, lease, repair, and reconcile settings.
- `scripts/run-project-intake-watcher.ps1` — scan remains a wake source without self-triggered build duplication.
- `ui/approvals.html` — display and resolve founder-decision work items; approval links stay here.
- `tests/test_project_autonomy.py`, `tests/test_project_autonomy_api.py`, `tests/test_project_intake_service.py`, `tests/test_project_intake_api.py`, `tests/test_notify.py`, `tests/test_telegram_adapter.py`, `tests/test_runtime_work_status.py`, `tests/test_heartbeat.py`, `tests/test_project_intake_watcher_scripts.py`, `tests/test_db.py` — regression coverage.

---

### Task 1: Transactional Autonomy Store, CAS State, Leases, and Outbox

**Files:**

- Create: `src/cofounder_kernel/project_autonomy_store.py`
- Create: `tests/test_project_autonomy_store.py`
- Modify: `src/cofounder_kernel/db.py:14, migrate(), SCHEMA_SQL`
- Modify: `tests/test_db.py`

**Interfaces:**

- Produces: `ProjectAutonomyStore.get(project_id) -> dict[str, Any]`
- Produces: `ProjectAutonomyStore.transition(project_id, *, expected_version, state, event, outbox=None) -> dict[str, Any]`
- Produces: `ProjectAutonomyStore.claim(project_id, *, owner, run_id, lease_seconds, expected_version) -> dict[str, Any] | None`
- Produces: `ProjectAutonomyStore.release(project_id, *, owner, run_id) -> bool`
- Produces: `ProjectAutonomyStore.due_outbox(limit=50) -> list[dict[str, Any]]`

- [ ] **Step 1: Write failing migration and atomic-transition tests**

```python
def test_transition_updates_state_event_and_outbox_atomically(tmp_path):
    db = migrated_db(tmp_path)
    project_id = make_project(db, tmp_path)
    store = ProjectAutonomyStore(db)

    updated = store.transition(
        project_id,
        expected_version=0,
        state={"phase": "needs_decision", "priority": "normal"},
        event={"event_type": "decision_requested", "work_item_id": 41},
        outbox={
            "topic": "project.decision_required",
            "dedupe_key": f"project:{project_id}:decision:41",
            "severity": "warning",
            "title": "Decision needed",
            "body": "Open Zade to answer.",
        },
    )

    assert updated["version"] == 1
    assert db.list_project_events(project_id)[0]["work_item_id"] == 41
    assert store.due_outbox()[0]["dedupe_key"].endswith(":decision:41")
```

Add failure-injection tests proving an exception before commit leaves all three records unchanged, stale `expected_version` raises `ProjectAutonomyConflict`, two simultaneous claims yield one winner, expired claims recover, and outbox dedupe is unique without losing an undelivered row.

- [ ] **Step 2: Run the tests and confirm RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_project_autonomy_store.py tests\test_db.py -q
```

Expected: failure because schema version 33, tables, and `ProjectAutonomyStore` do not exist.

- [ ] **Step 3: Add schema version 33 and the store**

Add these tables to `SCHEMA_SQL`:

```sql
CREATE TABLE IF NOT EXISTS project_autonomy_states (
  project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 0,
  phase TEXT NOT NULL DEFAULT 'planning',
  priority TEXT NOT NULL DEFAULT 'normal',
  plan_revision TEXT NOT NULL DEFAULT '',
  state_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS project_autonomy_leases (
  project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  owner TEXT NOT NULL,
  run_id TEXT NOT NULL,
  acquired_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_autonomy_outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  topic TEXT NOT NULL,
  severity TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  dedupe_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT NOT NULL,
  last_error TEXT NOT NULL DEFAULT '',
  notification_id INTEGER REFERENCES notifications(id),
  delivered_at TEXT
);
```

Implement `transition()` with one `db.connect()` transaction that checks the current version, upserts state, inserts the typed `project_events` columns, and inserts the outbox row. Use ISO UTC timestamps and reject boolean IDs.

- [ ] **Step 4: Run Task 1 tests and confirm GREEN**

Expected: all Task 1 tests pass; schema assertions expect `33`.

- [ ] **Step 5: Commit Task 1**

```powershell
git add src/cofounder_kernel/db.py src/cofounder_kernel/project_autonomy_store.py tests/test_project_autonomy_store.py tests/test_db.py
git commit -m "feat: add transactional project autonomy store"
```

---

### Task 2: Guarded Reporter and Commit-Bound Mechanical Evidence

**Files:**

- Modify: `src/cofounder_kernel/project_autonomy.py`
- Modify: `tests/test_project_autonomy.py`
- Test: `tests/test_project_autonomy_store.py`

**Interfaces:**

- Consumes: `ProjectAutonomyStore.transition()` and `ProjectAutonomyStore.get()`
- Produces: the existing reporter public methods with state-machine guards and idempotence
- Produces: `VerificationEnvelope` fields `project_path`, `repo_head`, `repo_status`, `checked_at`, and `checks`

- [ ] **Step 1: Add failing evidence and transition tests**

```python
@pytest.mark.parametrize("bad", [
    {"ok": True, "checked_at": utc_now(), "checks": [{"name": "passed", "ok": True}]},
    {"ok": True, "checked_at": utc_now(), "checks": [{"argv": ["npm", "test"], "ok": True, "returncode": 1, "output": "failed"}]},
])
def test_completion_rejects_non_mechanical_checks(reporter, project_id, bad):
    reporter.plan(project_id, criteria=[{"id": "mvp-1", "title": "Core flow"}])
    with pytest.raises(ValueError):
        reporter.complete_criterion(project_id, "mvp-1", verification=bad, commit="deadbeef")

def test_begin_increment_cannot_bypass_waiting_decision(reporter, project_id):
    reporter.plan(project_id, criteria=[{"id": "mvp-1", "title": "Core flow"}])
    reporter.begin_increment(project_id, criterion_id="mvp-1")
    reporter.report_needs_decision(project_id, decision_id=41, question="Choose", recommendation="A", options=OPTIONS)
    with pytest.raises(ValueError, match="needs_decision"):
        reporter.begin_increment(project_id, criterion_id="mvp-1")
```

Also test: future timestamps are rejected, linked worktrees are accepted, Git status failure is rejected, criterion commit must equal evidence HEAD and current project commit, final evidence must equal clean HEAD, unchanged `plan()` preserves completion, a changed plan reconciles by criterion ID, `record_increment()` requires `verifying` plus passing evidence, `report_blocked()` clears the active run, and repeated completion at the same commit is idempotent.

- [ ] **Step 2: Run focused reporter tests and confirm RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_project_autonomy.py -q
```

Expected: new assertions fail on the unchecked commit/evidence and permissive phase transitions.

- [ ] **Step 3: Refactor the reporter around the store**

Define allowed transitions explicitly:

```python
ALLOWED_TRANSITIONS = {
    "planning": {"building", "needs_decision", "blocked"},
    "building": {"verifying", "needs_decision", "approval_required", "blocked"},
    "verifying": {"ready_for_next_increment", "needs_decision", "approval_required", "blocked"},
    "ready_for_next_increment": {"building", "mvp_complete", "needs_decision", "blocked"},
    "needs_decision": {"building", "blocked"},
    "approval_required": {"building", "blocked"},
    "blocked": {"planning", "building"},
    "mvp_complete": set(),
}
```

Replace metadata writes with `store.transition()`. Store bounded evidence references in state and full evidence in event metadata/artifacts. Validate `returncode == 0`, non-empty `output`, exact project path, exact `repo_head`, clean `repo_status`, timestamp no more than five minutes in the future and no more than sixty minutes old. Use Git commands rather than `.git.is_dir()` or best-effort `_git_snapshot`.

- [ ] **Step 4: Run reporter/store tests and confirm GREEN**

Expected: all project-autonomy reporter and store tests pass.

- [ ] **Step 5: Commit Task 2**

```powershell
git add src/cofounder_kernel/project_autonomy.py tests/test_project_autonomy.py tests/test_project_autonomy_store.py
git commit -m "fix: enforce project autonomy evidence and transitions"
```

---

### Task 3: Canonical UI Decisions and Approvals, Notification-Only Telegram

**Files:**

- Modify: `src/cofounder_kernel/project_intake.py`
- Modify: `src/cofounder_kernel/approval.py`
- Modify: `src/cofounder_kernel/project_autonomy.py`
- Modify: `src/cofounder_kernel/notify.py`
- Modify: `src/cofounder_kernel/api.py`
- Modify: `ui/approvals.html`
- Modify: `tests/test_project_intake_service.py`
- Modify: `tests/test_project_intake_api.py`
- Modify: `tests/test_notify.py`
- Modify: `tests/test_telegram_adapter.py`

**Interfaces:**

- Produces: `ApprovalService.add_resolution_listener(listener: Callable[[dict[str, Any]], None])`
- Produces: `ProjectIntakeService.set_decision_listener(listener: Callable[[int, str, dict[str, Any]], None])`
- Produces: `ProjectAutonomyReporter.deliver_due_notifications(limit=50) -> dict[str, int]`
- Preserves: `POST /project-intake/decisions/{decision_id}/resolve` as the UI decision route
- Preserves: `POST /approval-requests/{request_id}/approve|deny` as the only approval routes

- [ ] **Step 1: Add failing production-seam tests**

```python
def test_ui_decision_resolution_resumes_same_autonomy_criterion(app, client):
    project_id, decision_id = waiting_decision(app)
    response = client.post(
        f"/project-intake/decisions/{decision_id}/resolve",
        json={"note": "Use SQLite", "resolved_by": "founder.ui"},
        headers=token_header(app),
    )
    assert response.status_code == 200
    assert app.state.project_autonomy.state(project_id)["phase"] == "building"

def test_telegram_cannot_resolve_project_decision(app):
    result = app.state.telegram_adapter._route(bound_message("decision 41: Use SQLite"))
    assert "Open Zade" in result.text
    assert app.state.project_autonomy.state(PROJECT_ID)["phase"] == "needs_decision"
```

Add approval tests proving the UI approval endpoint resumes the correct project, denial blocks/replans it, notifications link to `approvals.html`, no Telegram reply format is present, intake and autonomy do not emit duplicate messages, typed project-event columns are populated, quiet-hours/rate-limit suppression stays pending in the outbox, and later delivery marks the row delivered exactly once.

- [ ] **Step 2: Run the authority/notification tests and confirm RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_project_intake_service.py tests\test_project_intake_api.py tests\test_notify.py tests\test_telegram_adapter.py -q
```

- [ ] **Step 3: Wire canonical listeners and the outbox**

Have intake create the canonical founder-decision work item and call only `report_needs_decision()`. After UI resolution succeeds, call the registered decision listener, then wake the orchestrator. Add approval-service listeners after canonical resolution commits. Remove `force_channels`; the outbox retries through the existing bus after quiet hours/rate limits without creating a new approval path.

Add a Decisions panel to `ui/approvals.html` that loads waiting `founder_decision` items, accepts an answer, and posts to `/project-intake/decisions/{id}/resolve`. Approval cards continue using `/approval-requests/{id}/approve|deny`.

- [ ] **Step 4: Run Task 3 tests and confirm GREEN**

Expected: all authority and notification tests pass with fake senders; no network calls occur.

- [ ] **Step 5: Commit Task 3**

```powershell
git add src/cofounder_kernel/project_intake.py src/cofounder_kernel/approval.py src/cofounder_kernel/project_autonomy.py src/cofounder_kernel/notify.py src/cofounder_kernel/api.py ui/approvals.html tests/test_project_intake_service.py tests/test_project_intake_api.py tests/test_notify.py tests/test_telegram_adapter.py
git commit -m "feat: resolve project boundaries through Zade UI"
```

---

### Task 4: One Shared Project-Autonomy Truth Surface

**Files:**

- Modify: `src/cofounder_kernel/project_autonomy.py`
- Modify: `src/cofounder_kernel/runtime.py`
- Modify: `src/cofounder_kernel/api.py`
- Modify: `tests/test_project_autonomy_api.py`
- Modify: `tests/test_api.py`

**Interfaces:**

- Produces: `ProjectAutonomyReporter.project_view(project_id) -> dict[str, Any]`
- Produces: `ProjectAutonomyReporter.list_views(*, lifecycle_state=None, limit=500) -> list[dict[str, Any]]`
- Produces: `ProjectAutonomyReporter.portfolio() -> dict[str, Any]`

- [ ] **Step 1: Add a failing cross-surface status test**

```python
def test_runtime_and_portfolio_report_same_active_phase(app, client, monkeypatch):
    project_id = planned_project(app)
    app.state.project_autonomy.begin_increment(project_id, criterion_id="mvp-1", run_id="run-1")
    portfolio = client.get("/project-intake/status").json()
    reply = post_runtime(client, "What is the status of my projects?")
    assert portfolio["projects"][0]["status"] == "actively_building"
    assert "building" in reply["response"]
    assert "state: verified" not in reply["response"]
```

Also assert planning and ready states are not `actively_building`, all existing raw project fields remain present, list responses omit full historical command output, and lifecycle scaffold verification never becomes MVP completion.

- [ ] **Step 2: Run API/runtime status tests and confirm RED**

- [ ] **Step 3: Route every consumer through reporter views**

Replace raw `db.list_projects()` status rendering with `project_autonomy.list_views()` and `project_autonomy.portfolio()`. Keep the deterministic runtime answer, but render priority, autonomy phase, completed/total criteria, current criterion, active run, next action, blocker, last verified commit, and distribution targets from the same view.

- [ ] **Step 4: Run Task 4 tests and confirm GREEN**

- [ ] **Step 5: Commit Task 4**

```powershell
git add src/cofounder_kernel/project_autonomy.py src/cofounder_kernel/runtime.py src/cofounder_kernel/api.py tests/test_project_autonomy_api.py tests/test_api.py
git commit -m "fix: unify project autonomy status truth"
```

---

### Task 5: Documented MVP Planner and Priority Reconciliation

**Files:**

- Create: `src/cofounder_kernel/project_mvp_planner.py`
- Create: `tests/test_project_mvp_planner.py`
- Modify: `src/cofounder_kernel/config.py`
- Modify: `config.toml`
- Modify: `config.example.toml`

**Interfaces:**

- Produces: `ProjectMvpPlanner.plan(project: dict[str, Any]) -> MvpPlanResult`
- Produces: `MvpPlanResult.criteria`, `.external_boundaries`, `.source_hash`, `.needs_decision`

- [ ] **Step 1: Add failing structured-planner tests**

```python
def test_planner_returns_stable_source_cited_criteria(tmp_path, fake_ollama):
    root = make_documented_project(tmp_path, "Same Ground", MVP_DOC)
    planner = ProjectMvpPlanner(config=config_for(root), ollama=fake_ollama)
    result = planner.plan(project_record(root))
    assert result.needs_decision is None
    assert [item["id"] for item in result.criteria] == ["mvp-resource-search", "mvp-crisis-access"]
    assert all(item["source"] for item in result.criteria)
    assert result.source_hash
```

Test exact JSON-schema use, stable ID normalization, duplicate rejection, confinement to project documents, an ambiguous MVP returning a two-or-three-option decision request, no source files in the prompt, and unchanged documents producing the same source hash and plan revision.

- [ ] **Step 2: Run planner tests and confirm RED**

- [ ] **Step 3: Implement local structured planning**

Use `OllamaClient.chat(..., model=config.ollama.coding_agent_model, temperature=0, think=False, format=MVP_PLAN_SCHEMA)` with a system instruction that extracts only documented MVP requirements and returns source-relative citations. Reject criteria without a source. Hash normalized documentation content for reconciliation. Do not include ignored directories, generated artifacts, legacy Dark Index material, or files outside the registered root.

Extend `[project_intake]` configuration:

```toml
autonomy_enabled = true
autonomy_max_workers = 2
autonomy_lease_seconds = 900
autonomy_repair_attempts = 3
autonomy_reconcile_seconds = 60
```

- [ ] **Step 4: Run planner/config tests and confirm GREEN**

- [ ] **Step 5: Commit Task 5**

```powershell
git add src/cofounder_kernel/project_mvp_planner.py src/cofounder_kernel/config.py config.toml config.example.toml tests/test_project_mvp_planner.py
git commit -m "feat: derive durable MVP plans from project docs"
```

---

### Task 6: Durable Autonomous Execution, Verification, Repair, and Completion

**Files:**

- Create: `src/cofounder_kernel/project_autonomy_orchestrator.py`
- Create: `tests/test_project_autonomy_orchestrator.py`
- Modify: `src/cofounder_kernel/delegation.py`
- Modify: `src/cofounder_kernel/project_autonomy.py`

**Interfaces:**

- Produces: `ProjectAutonomyOrchestrator.wake(project_id: int | None = None, *, reason: str) -> dict[str, Any]`
- Produces: `ProjectAutonomyOrchestrator.run_once() -> dict[str, Any]`
- Produces: `ProjectAutonomyOrchestrator.recover() -> dict[str, int]`
- Produces: `ProjectAutonomyOrchestrator.shutdown(wait=False) -> None`

- [ ] **Step 1: Add failing orchestrator tests**

```python
def test_verified_increment_immediately_queues_next_criterion(orchestrator, reporter, project):
    orchestrator.wake(project["id"], reason="intake")
    first = orchestrator.run_once()
    second = orchestrator.run_once()
    assert first["criterion_id"] == "mvp-1"
    assert second["criterion_id"] == "mvp-2"
    assert reporter.state(project["id"])["mvp_criteria_completed"] == 1

def test_two_urgent_projects_run_concurrently_but_never_twice_each(orchestrator, same_ground, dark_index):
    orchestrator.wake(reason="startup")
    claims = orchestrator.claim_ready(limit=2)
    assert {item["project_id"] for item in claims} == {same_ground["id"], dark_index["id"]}
    assert orchestrator.claim_ready(limit=2) == []
```

Test smallest dependency-ready criterion selection, unique work-item keys, inline native execution with `directed=True` under the standing founder authorization, optional existing agent routes, no cloud fallback, three repair attempts, decision classification, UI approval classification, hard block classification, commit-after-pass, post-commit verification, final completion, lease expiry recovery, and shutdown behavior.

- [ ] **Step 2: Run orchestrator tests and confirm RED**

- [ ] **Step 3: Implement the orchestrator**

Use unique keys of this form:

```python
unique_key = (
    f"project-autonomy:{project_id}:{plan_revision}:"
    f"{criterion_id}:{increment}:{attempt}"
)
```

Build a brief from the exact criterion, cited documents, current clean Git head, accepted founder answers, verification commands, and project boundaries. Dispatch through `DelegationService.queue_delegation(..., directed=True, workspace=project_root)`. Convert the returned real auto-verification into a commit-bound envelope, repair failures up to the configured budget, and use only reporter/store methods for transitions.

Do not hold a database transaction while the model, commands, or Git run. Renew the lease between bounded stages and re-check state version before recording results.

- [ ] **Step 4: Run Task 6 tests and confirm GREEN**

- [ ] **Step 5: Commit Task 6**

```powershell
git add src/cofounder_kernel/project_autonomy_orchestrator.py src/cofounder_kernel/delegation.py src/cofounder_kernel/project_autonomy.py tests/test_project_autonomy_orchestrator.py
git commit -m "feat: run project intake through verified MVP completion"
```

---

### Task 7: Wake Sources, Restart Recovery, API Controls, and UI Status

**Files:**

- Modify: `src/cofounder_kernel/api.py`
- Modify: `src/cofounder_kernel/project_intake.py`
- Modify: `src/cofounder_kernel/heartbeat.py`
- Modify: `scripts/run-project-intake-watcher.ps1`
- Modify: `ui/approvals.html`
- Create: `tests/test_project_autonomy_live_contract.py`
- Modify: `tests/test_heartbeat.py`
- Modify: `tests/test_project_intake_watcher_scripts.py`

**Interfaces:**

- Adds: `POST /project-intake/autonomy/wake`
- Adds: `POST /project-intake/projects/{id}/autonomy/pause`
- Adds: `POST /project-intake/projects/{id}/autonomy/resume`
- Adds: `POST /project-intake/projects/{id}/autonomy/priority`
- Adds: `GET /project-intake/autonomy/status`

- [ ] **Step 1: Add failing app lifecycle and wake tests**

```python
def test_scan_wakes_new_project_once(app, client, monkeypatch):
    calls = []
    monkeypatch.setattr(app.state.project_autonomy_orchestrator, "wake", lambda project_id=None, **kw: calls.append((project_id, kw)))
    response = client.post("/project-intake/scan", headers=token_header(app))
    assert response.status_code == 200
    assert len({project_id for project_id, _ in calls if project_id}) == 1

def test_startup_recovery_wakes_unfinished_projects(app):
    result = app.state.project_autonomy_orchestrator.recover()
    assert result["unfinished_seen"] >= 1
    assert result["duplicate_runs_created"] == 0
```

Test heartbeat reconciliation, decision/approval callbacks, pause/resume/priority auth, lifespan shutdown, notification outbox retry, and watcher scan storms producing no duplicate run.

- [ ] **Step 2: Run lifecycle tests and confirm RED**

- [ ] **Step 3: Wire app construction and lifecycle**

Construct store, reporter, planner, and orchestrator in `create_app`; expose them on `app.state`. Run `recover()` during boot maintenance, start workers in lifespan startup, and shut them down in lifespan teardown. Pass a bounded callback into `KernelHeartbeat.tick()` and call `wake()` after scan registration and canonical boundary resolution.

Keep the watcher thin: it calls scan, logs created/existing/errors, and never directly dispatches a build. Debounced scan storms are safe because the orchestrator claims by version and lease.

- [ ] **Step 4: Run Task 7 tests and confirm GREEN**

- [ ] **Step 5: Commit Task 7**

```powershell
git add src/cofounder_kernel/api.py src/cofounder_kernel/project_intake.py src/cofounder_kernel/heartbeat.py scripts/run-project-intake-watcher.ps1 ui/approvals.html tests/test_project_autonomy_live_contract.py tests/test_heartbeat.py tests/test_project_intake_watcher_scripts.py
git commit -m "feat: wire durable project autonomy wake and recovery"
```

---

### Task 8: Regression, Live Activation, and First Real Increments

**Files:**

- Modify only if verification finds a defect in a Task 1-7 file.
- Runtime state: `C:\AI Brain\project-intake\Same Ground`
- Runtime state: `C:\AI Brain\project-intake\The Dark Index`

**Interfaces:**

- Consumes: all Task 1-7 services and routes.
- Produces: both real projects planned at `urgent`, running or waiting on a mechanically honest boundary, with continued autonomous progression enabled.

- [ ] **Step 1: Run the focused backend/orchestrator suite**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_project_autonomy_store.py tests\test_project_autonomy.py tests\test_project_autonomy_api.py tests\test_project_mvp_planner.py tests\test_project_autonomy_orchestrator.py tests\test_project_autonomy_live_contract.py tests\test_project_intake.py tests\test_project_intake_service.py tests\test_project_intake_api.py tests\test_notify.py tests\test_telegram_adapter.py tests\test_heartbeat.py tests\test_db.py -q
```

Expected: zero failures.

- [ ] **Step 2: Run the full repository suite**

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: zero failures. Investigate every failure; do not classify a failure as unrelated without reproducing its pre-existing state from the commit before Task 1.

- [ ] **Step 3: Restart the live kernel**

```powershell
.\scripts\stop.ps1
.\scripts\start.ps1 -NoOpen -TimeoutSec 60
```

Expected: `Health: ok`, the watcher process remains running, Telegram adapter is healthy, and schema version is 33.

- [ ] **Step 4: Activate both real projects through the protected API**

Use `/session/token`, set both priorities to `urgent`, and call the autonomy wake route. Do not directly edit the SQLite database or project metadata.

Expected live state:

```text
Same Ground: urgent, criteria > 0, phase planning/building/verifying or an honest UI boundary
The Dark Index: urgent, criteria > 0, phase planning/building/verifying or an honest UI boundary
mvp_complete: false until every documented criterion and final verification pass
```

- [ ] **Step 5: Observe the first real verified increment for each project**

Capture `/project-intake/status`, each project event ledger, work-item/run evidence, repository HEAD/status, and Zade's conversational status answer. Confirm the first completed criterion automatically advances or queues the next criterion without another founder command.

- [ ] **Step 6: Probe boundary behavior without sending Telegram**

Use a temporary registered test project and fake notification sender to exercise UI decision and approval resolution. Confirm the real founder's Telegram receives no synthetic message. Delete no project data; leave the test project in a recoverable test root or clean it through the test harness.

- [ ] **Step 7: Commit any verification repair and report live status**

If Task 8 required a code repair, commit only those files with:

```powershell
git commit -m "fix: complete project autonomy live acceptance"
```

Report exact project phases, current criteria, commits, checks, blockers, worker/lease status, notification status, test counts, and commit hashes. Never describe scaffold verification as MVP completion.

---

## Plan Self-Review Results

- Spec coverage: every approved architecture, UI-only boundary, notification-only Telegram rule, backend-review finding, concurrency rule, evidence rule, wake source, recovery behavior, and live-activation requirement maps to Tasks 1-8.
- Placeholder scan: no deferred implementation markers remain.
- Type consistency: reporter, store, planner, and orchestrator signatures are defined once in their producing tasks and consumed under the same names later.
- Scope: backend hardening and orchestration are sequential parts of one truth-preserving feature; neither produces the requested behavior independently, so they remain one implementation plan.
