# Project-Autonomy Orchestrator Handoff (for Codex)

Backend truth + notification contract is **shipped at commit `346093e`** and live-verified.
Your job is the **persistent autonomous build orchestrator** that drives projects toward
their MVP. You do **not** build any reporting, persistence, or notification code — you
call the reporter below. Everything founder-facing is already handled.

## Non-negotiable boundaries

1. Record progress ONLY through `ProjectAutonomyReporter`. Never write `projects.metadata`,
   `project_events`, or notifications directly, and never call `NotificationBus` yourself
   for project-autonomy topics.
2. Model prose is never evidence. Every completion claim must carry real command output
   (the reporter rejects anything else — do not try to satisfy the shape with fabricated
   checks; pass through the actual `_run_existing_check`-style results).
3. Preserve inline local execution AND the optional coding-agent/sub-agent backends:
   route build work through the existing `DelegationService` / work queue exactly like
   `ProjectIntakeService.run_until_blocked` does. Local-only provider policy stays enforced
   at the OllamaClient transport — do not add cloud calls.
4. Founder gates: decisions and approvals interrupt the loop; the orchestrator waits.
   Reversible, local, low-risk implementation choices never interrupt — pick the
   documented default and continue.
5. Do not send test Telegram messages. All tests use fake bus/telegram senders.

## The reporter

```python
reporter = app.state.project_autonomy            # wired in create_app
# or: ProjectAutonomyReporter(db=db, bus=bus)    # src/cofounder_kernel/project_autonomy.py
```

Phases: `planning building verifying ready_for_next_increment needs_decision
approval_required blocked mvp_complete`. Every call persists state in the project's
`metadata["autonomy"]` and appends a `project_events` row. All invalid input raises
`ValueError` — treat that as a bug in the orchestrator, never swallow it.

### Loop lifecycle calls

```python
reporter.plan(project_id, criteria=[{"id": "auth", "title": "Sign-in works", "required": True}, ...],
              priority="normal",                      # low|normal|high|urgent
              next_action="...", external_boundaries=["Google Play submission"])
reporter.begin_increment(project_id, criterion_id="auth", run_id=..., next_action="...")
reporter.begin_verification(project_id, run_id=...)
reporter.record_increment(project_id, summary="...", verification=VERIFICATION)  # routine; ledger-only, NO Telegram
reporter.complete_criterion(project_id, "auth", verification=VERIFICATION, commit=head)
reporter.complete_mvp(project_id, final_verification=VERIFICATION)
```

### The evidence shape (both criterion and final verification)

```python
VERIFICATION = {
    "ok": True,
    "checked_at": utc_now(),          # ISO; REJECTED if older than 60 minutes
    "checks": [                        # non-empty; every check must have passed
        {"argv": ["npm", "test"], "ok": True, "returncode": 0, "output": "42 passed"},
    ],
}
```

- `complete_criterion` additionally requires the verified repo `commit` hash.
- `complete_mvp` runs its own live git check (`rev-parse HEAD` + `status --porcelain`) in
  the project root: the repo must be committed and clean or it raises. So the orchestrator
  must commit the project's work before attempting completion.
- Scaffold lifecycle `verified` NEVER satisfies MVP completion. `complete_mvp` on a
  project without a criteria plan is rejected by design.
- `complete_mvp` is idempotent per commit and emits exactly one `project.mvp_complete`
  notification (info severity, forced through Telegram by the reporter).

### Founder boundaries

```python
reporter.report_needs_decision(project_id, decision_id=item_id,
    question="...", recommendation="...",
    options=[{"option": "...", "impact": "..."}, ...])       # exactly 2-3 options
reporter.report_approval_required(project_id, approval_request_id=item_id,
    action="...", reason="...", boundary="app_store_submission",
    approve_hint="")   # default hint: POST /work/items/{id}/approve|deny
reporter.report_blocked(project_id, criterion_id="feed", reason="...",
    verification_output="<real output>", attempts=3, needed="...",
    severity="warning")                                       # or "critical"
```

- `decision_id` should be a real `founder_decision` work item (create it the way
  `ProjectIntakeService._record_build_route` receives one from delegation dispatch), so the
  existing Telegram reply `decision {id}: <answer>` routes through
  `POST /project-intake/decisions/{id}/resolve`.
- Boundary enum (anything else is rejected): `credentials paid_services
  publishing_deployment app_store_submission legal_acceptance external_account_creation
  irreversible_external_commitment mvp_scope_expansion`.
- `report_blocked` is ONLY for hard failures (repeated verification failure, requirement
  conflict, missing tooling). A recoverable product choice must be `report_needs_decision`.

### Resumption

```python
resumed = reporter.resume_after_decision(decision_id, answer=founder_answer)
# -> {"project": {...}, "criterion_id": "feed", "decision_id": N}; phase -> building
resumed = reporter.resume_after_approval(approval_request_id, approved=True, note="")
# approved=False -> phase blocked with the denial reason
```

Wire these into the existing decision-resolution path (after
`ProjectIntakeService.resolve_decision` / approval handling succeeds), then continue the
loop from `resumed["criterion_id"]`.

## Read model (already live — don't duplicate)

- `GET /project-intake/projects` and `/{id}` → each project carries the 16-key `autonomy`
  object (phase, priority, criteria counts, current criterion/increment, blocking info,
  decision/approval ids, active_run_id, last_notification_id, repo_head, mvp_complete).
- `GET /project-intake/status` → portfolio buckets `scaffold_verified actively_building
  waiting_decision waiting_approval blocked mvp_complete intake` (never collapsed).
- `GET /project-intake/projects/{id}/events` → the ledger.

## Tests that must stay green

```
.venv\Scripts\python.exe -m pytest tests/test_project_autonomy.py tests/test_project_autonomy_api.py \
  tests/test_notify.py tests/test_project_intake.py tests/test_project_intake_service.py \
  tests/test_project_intake_api.py tests/test_telegram_adapter.py tests/test_db.py -q
```

78 passed at handoff. Known pre-existing full-suite failures (NOT yours to inherit):
3 stale `SCHEMA_VERSION == 31` assertions in tests/test_build_store.py and
tests/test_build_orchestration_store.py — a separate fix task is already running.

## Live state at handoff

Same Ground (project 1) and The Dark Index (project 3) are `scaffold_verified`,
`mvp_complete: false`, no criteria planned yet. Kernel restart:
`.\scripts\stop.ps1` then `.\scripts\start.ps1 -NoOpen -TimeoutSec 60`.
