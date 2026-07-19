# Project Intake MVP Autonomy Orchestrator Design

Date: 2026-07-19  
Status: Approved design; pending written-spec review  
Supersedes: the one-shot autonomous-flow and Telegram-approval portions of `2026-07-19-project-intake-autopilot-design.md`

## Outcome

When a qualifying project appears under `C:\AI Brain\project-intake`, Zade owns the documented MVP from intake through mechanically verified completion. Zade reads the founder-authored documents, creates a durable MVP contract, implements the smallest next increment, verifies and commits it, and continues until every required MVP criterion passes the final completion gate.

Zade pauses only for a consequential founder decision, an existing authority approval boundary, an unrecoverable verified blocker, or a requirements conflict. Reversible local implementation choices use documented defaults and do not interrupt the founder.

`Same Ground` and `The Dark Index` are equal highest-priority projects. With the configured two-worker ceiling, both may advance concurrently while each project remains single-writer.

## Scope

This design adds a durable project-autonomy orchestrator and hardens the project-autonomy backend contract introduced in commit `346093e`.

It does not authorize:

- app-store submission, publishing, deployment, signing, or release;
- paid services, purchases, or cloud-model fallback;
- credential or secret access;
- legal acceptance or external account creation;
- material scope expansion beyond the documented MVP;
- destructive changes outside the registered project root.

Those actions continue through the existing authority and approval systems.

## Founder-Channel Contract

The existing Zade UI and approval console are the only approval surfaces. Approval messages may be sent to Telegram as notifications, but Telegram never approves, denies, or authorizes an action.

Telegram is outbound notification-only. It is used for:

- notification that a consequential product decision is waiting;
- notification that an approval is waiting in the UI;
- notification of an unrecoverable blocker;
- one delivered MVP-completion notice.

An approval notification contains the project, proposed action, boundary, reason, approval-request ID, and a link or route to the existing UI. It never provides a Telegram approval reply format.

Product questions and approvals are resolved through the existing Zade UI surfaces, not through Telegram replies. A product question is linked to a canonical `founder_decision` work item. The same canonical UI resolution path must update the project-autonomy state and resume the correct criterion.

Routine increments remain quiet: they are recorded in the project ledger and UI without Telegram messages.

## Architecture

### Project Autonomy Orchestrator

A new `ProjectAutonomyOrchestrator` coordinates existing services rather than duplicating them. Its dependencies are:

- `ProjectIntakeService` for registered roots, manifests, documentation, and intake reconciliation;
- `ProjectAutonomyReporter` for project-autonomy transitions and read projections;
- the existing work queue and `DelegationService` for inline local coding and optional coding-agent/sub-agent execution;
- existing build verification and governed command facilities for real checks;
- the existing approval service and approval console for authority boundaries;
- the existing notification bus and Telegram adapter for notifications;
- Git for commit-bound evidence.

The orchestrator never writes project metadata, project events, notifications, or approval records directly.

### Durable State Machine

The project phases are:

```text
intake
  -> planning
  -> building
  -> verifying
  -> ready_for_next_increment
  -> building ...
  -> final_verification
  -> mvp_complete
```

Interrupting phases are:

```text
needs_decision
approval_required
blocked
```

Every transition has an explicit allowed-source set. A build cannot start from `needs_decision` or `approval_required`; only canonical resolution of that exact work item or approval request may resume it. `mvp_complete` is terminal until the founder explicitly approves a new scope and the project is replanned.

### Wake Sources and Leases

The orchestrator is woken by:

- a successful project-intake scan;
- kernel startup recovery;
- the existing heartbeat/reconciliation loop;
- canonical decision resolution;
- canonical approval resolution;
- completion or interruption of an implementation run.

Wake calls are idempotent. A durable per-project lease contains the project ID, run ID, owner, state version, acquisition time, and expiry. The database claims a project with compare-and-swap semantics. The configured global ceiling is two projects, but only one worker may mutate a given project at a time.

Expired leases are recovered on startup. A live lease is never stolen. Recovery reconciles the durable run and Git state before scheduling more work.

## Backend Contract Hardening

The reporter contract is corrected before the runner is enabled.

### Atomic Transitions

Current autonomy state, the append-only project event, and any notification-outbox record are written in one SQLite transaction. Founder notification delivery occurs after the durable waiting state exists. Delivery failure does not roll back the decision or approval.

Project metadata updates use a state revision or dedicated autonomy row. Intake reconciliation merges current metadata inside the database transaction and cannot overwrite newer autonomy state with a stale snapshot.

Project events populate their typed `work_item_id`, `approval_request_id`, and `notification_id` columns when those relationships exist.

### Canonical Decisions and Approvals

`needs_decision` references a real `founder_decision` work item. The existing UI decision-resolution service remains canonical and invokes an autonomy callback after successful resolution. The callback clears the exact project decision and wakes the orchestrator at the stored criterion. Telegram may announce the waiting decision but cannot resolve it.

`approval_required` references a real approval request created through the existing authority and approval services. Approval and denial remain UI operations. The approval service invokes an autonomy callback after resolution; approval resumes the stored criterion, while denial replans around the denied action or becomes a durable blocker when no in-scope path remains.

The reporter does not implement a second decision or approval engine, does not scan an arbitrary 500-project window to find ownership, and does not advertise work-item routes for approval-request IDs.

### Guarded and Idempotent Planning

An unchanged documented MVP plan is idempotent. Re-running planning preserves completed criteria, verification evidence, current work, and decisions. Changed criteria are reconciled by stable criterion ID:

- unchanged criteria retain state;
- new criteria enter `pending`;
- removed required criteria require a recorded plan-reconciliation event and cannot silently erase completion history;
- changing a completed criterion requires explicit re-verification.

Repeated criterion completion at the same verified commit is idempotent. A different commit requires fresh verification rather than overwriting the original attestation.

### Mechanical Evidence

Every accepted verification envelope contains:

- resolved project path;
- repository HEAD;
- repository status command result;
- checked-at timestamp with bounded future-clock tolerance;
- one or more executed checks;
- each check's argv, return code, bounded real output, start/end time, and success result.

The reporter validates that every required check has return code zero and non-empty captured output. Name-only or caller-authored prose records are rejected.

Criterion completion verifies that the supplied commit exists in the registered repository and is the exact commit tested by the evidence envelope. Final completion reruns the project-level verification against the current HEAD, then confirms the evidence HEAD equals the clean repository HEAD.

Git checks use Git itself (`rev-parse --is-inside-work-tree`, `rev-parse HEAD`, and `status --porcelain`) and treat command failure as unknown/failure, never as clean. Linked worktrees, where `.git` is a file, are valid.

Large command output and full evidence live in the evidence/artifact store. Project status retains bounded summaries and references rather than rewriting an ever-growing evidence blob on every transition.

### Notification Outbox

Boundary notifications use the existing notification bus through a durable outbox. The notification policy remains configured centrally; project producers cannot bypass channel policy with a per-call channel escape hatch.

Suppressed or failed Telegram deliveries remain pending for retry after quiet hours or rate-limit recovery. Dedupe prevents duplicate delivered messages but never converts a never-delivered MVP-completion message into permanent silence.

Approval notifications link to the existing UI. Decision and approval notification construction is centralized so intake and autonomy cannot emit competing messages with the same dedupe key and different content.

### Shared Truth Projection

The API, Zade runtime status renderer, project portfolio, UI, and Telegram notification summaries consume the same project-autonomy projection. `lifecycle_state=verified` means only that the scaffold passed its checks. It never masks `planning`, `building`, `verifying`, `needs_decision`, `approval_required`, `blocked`, or `mvp_complete`.

Portfolio buckets distinguish:

- `scaffold_verified`;
- `planning`;
- `ready`;
- `actively_building`;
- `waiting_decision`;
- `waiting_approval`;
- `blocked`;
- `mvp_complete`.

Only a live claimed run in `building` or `verifying` is `actively_building`.

## Autonomous MVP Flow

### 1. Intake and Planning

For a new project, Zade reads all supported founder-authored documents inside the registered root and produces a durable MVP contract. Each criterion has a stable ID, title, source citation, required flag, verification plan, dependencies, and completion state.

If no implementable MVP can be derived without a consequential product decision, Zade creates one canonical founder-decision work item and pauses. Low-risk omissions receive a reversible documented default.

### 2. Selecting Work

The scheduler orders eligible projects by:

1. explicit autonomy priority;
2. oldest eligible wake time;
3. stable project ID.

`Same Ground` and `The Dark Index` are seeded as `urgent`, equal highest priority. With two workers they are both eligible concurrently.

Within a project, Zade selects the smallest dependency-ready incomplete criterion or increment. Work is bounded so each run can be verified, committed, retried, and recovered independently.

### 3. Execution

The orchestrator creates one unique work item for the project, criterion, increment, plan revision, and attempt. It dispatches through the existing governed delegation path:

- inline native local coding is the default;
- optional coding-agent/sub-agent execution remains available for suitable programming tasks;
- cloud inference is never an automatic fallback;
- no work may escape the registered project root.

The build brief contains the relevant project documents, exact criterion, current Git state, acceptance checks, founder answers, constraints, and required evidence. It does not include unrelated historical memories or obsolete Dark Index implementation material.

### 4. Verification and Repair

After execution, Zade runs the criterion's mechanical checks. Failure enters a bounded repair loop with fresh diagnostic context. Each attempt records the changed files, real output, Git state, and failure classification.

The default repair budget is three attempts per increment. After the budget:

- a consequential missing choice becomes `needs_decision`;
- an authority boundary becomes `approval_required` in the UI;
- a true tooling, requirements, or verification impasse becomes `blocked`;
- otherwise Zade replans a smaller in-scope increment.

Passing work is committed locally with a focused commit. Criterion completion is recorded only after the post-commit verification envelope is bound to that commit.

### 5. Continue or Complete

After one criterion completes, Zade immediately selects the next eligible criterion. It does not stop merely because the scaffold or one increment is verified.

When all required criteria are complete, Zade runs the fresh final project verification. `mvp_complete` requires:

- all required documented criteria complete;
- no unresolved decision, approval, blocker, or active run;
- fresh required checks passing on the recorded HEAD;
- a clean repository at that exact HEAD;
- a durable completion event and notification-outbox entry.

Store preparation may be included when explicitly documented as part of the MVP, but signing, store-account actions, submission, publishing, and release remain outside autonomous completion.

## Restart and Failure Recovery

- Startup reconciles expired leases, interrupted work items, reporter state, and Git state before new dispatch.
- An interrupted uncommitted increment is inspected and either resumed, repaired, or reverted only through a safe project-scoped plan; it is never called complete.
- Notification failure leaves the durable decision, approval, blocker, or completion state intact and schedules delivery retry.
- A watcher scan cannot create a duplicate plan or run.
- A late decision/approval response is accepted only when its canonical record and project state still match.
- A blocked run clears its active run and lease.
- Requirements changes create a plan revision and reconcile criteria instead of resetting history.

## API and UI

Existing project-intake endpoints remain compatible and use the shared projection:

```text
GET  /project-intake/projects
GET  /project-intake/projects/{id}
GET  /project-intake/projects/{id}/events
GET  /project-intake/status
```

The existing UI shows priority, plan revision, criteria progress, current criterion, current increment, active run, last verified commit, next action, decision, approval, blocker, and completion evidence.

Approvals are opened, approved, denied, and audited only in the existing approval UI. Telegram notifications provide a link back to that surface.

Operator controls may pause/resume a project or reprioritize it, but the default for a new authorized intake project is autonomous execution through documented MVP completion.

## Testing

### Backend Contract

Tests cover:

- atomic state/event/outbox transitions with injected failures;
- versioned updates and concurrent watcher/orchestrator writes;
- allowed-source transition enforcement;
- idempotent planning and plan reconciliation;
- canonical decision creation, Telegram notification, UI resolution, and correct-project resume;
- canonical UI approval, approval callback, denial, and correct-project resume/replan;
- rejection of arbitrary commits, stale/future evidence, failing return codes, empty output, dirty repositories, and Git command failures;
- exact evidence-to-HEAD binding;
- linked-worktree completion;
- notification quiet-hours/rate-limit retry and exactly-once delivered MVP completion;
- consistent runtime/API/UI status projection.

### Orchestrator

Tests cover:

- intake wake, startup wake, heartbeat wake, and idempotent duplicate wakes;
- global two-project concurrency and one-writer-per-project leases;
- equal urgent priority for Same Ground and The Dark Index;
- smallest-ready-criterion selection;
- inline local execution and optional coding-agent route preservation;
- bounded repair loops and correct boundary classification;
- restart recovery during planning, building, verification, decision, approval, and notification delivery;
- continuation after a verified increment;
- final MVP completion only after all criteria and final checks pass.

### Live Acceptance

Live acceptance uses the running kernel and the two real project repositories:

1. Backend hardening tests pass and the kernel restarts healthy.
2. Same Ground and The Dark Index are both `urgent`, planned, and independently leased.
3. Both receive a real next MVP increment through the existing local delegation path.
4. Their status endpoints and Zade's conversational answer report the same phase and evidence.
5. A safe synthetic decision uses a canonical work item, is resolved through the existing UI API, and resumes the correct test project; no fake founder Telegram message is sent.
6. A safe synthetic approval is resolved through the existing UI API and resumes the correct test project; Telegram remains notification-only.
7. Restart recovery does not duplicate work or lose criteria.
8. Mechanical verification evidence is tied to actual Git commits.
9. The two real projects continue after their first verified increment rather than returning to scaffold-only idle state.

## Implementation Order

1. Harden reporter persistence, transitions, Git evidence, canonical authority integration, outbox delivery, and shared projection.
2. Add durable project leases and project-owned plan revisions.
3. Implement the orchestrator wake/claim/execute/verify/repair loop.
4. Wire intake, startup, heartbeat, decision, and approval wake sources.
5. Seed Same Ground and The Dark Index as equal urgent priorities and derive their documented MVP contracts.
6. Verify safe canary behavior, then enable both real projects.
7. Observe the first real increments and confirm continued autonomous advancement.
