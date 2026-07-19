# Project Intake Autopilot Design

## Outcome

Zade gains a live project-intake lane rooted at `C:\AI Brain\project-intake`. A direct-child project folder containing either a Git repository or a `project.md` manifest is registered, documented in the Obsidian Vault, assessed, and advanced automatically until work completes or a genuine founder decision is required.

The initial projects are:

- `C:\AI Brain\project-intake\The Dark Index`
- `C:\AI Brain\project-intake\Same Ground`

Both are mobile applications intended for Google Play distribution and eventual Apple App Store distribution.

## Intake Contract

The intake root follows the operational shape of `C:\AI Brain\inbox`: it is local, watched with filesystem events, debounced, authenticated to the loopback kernel, audited, and installed as a limited user logon task. Project intake remains a separate lane because repositories are durable workspaces rather than disposable ingestion inputs.

Only direct-child folders are eligible. A folder becomes a project when it contains at least one of:

- a `.git` directory;
- a `project.md` manifest.

Generated files, dependency caches, editor metadata, and nested folders do not independently create projects. Repeated events are idempotent through a stable project path and repository fingerprint.

## Project Manifest

Each registered project has a human-readable `project.md` in its project root. This file is the canonical founder-facing project definition and remains visible in Obsidian.

Required fields:

- project name;
- product type;
- product summary;
- lifecycle state;
- source-material paths;
- distribution targets;
- autonomy policy;
- current build objective.

Both initial manifests declare:

```yaml
product_type: mobile_application
distribution_targets:
  - google_play
  - apple_app_store_eventual
```

The Dark Index manifest also records that it catalogs and helps users understand physical book collections; it is not a reading platform. Same Ground records that it is a resource, support, and community mobile app for veterans, EMTs, and law-enforcement personnel, with optional service verification as a trust layer.

## Deep Module and Interface

Project-intake behavior sits behind one deep `ProjectIntakeService` module. Callers and tests use this interface:

```python
scan() -> ProjectIntakeScanResult
get(project_id: int) -> ProjectRecord
run_until_blocked(project_id: int) -> ProjectRunResult
resolve_decision(decision_id: int, answer: str) -> ProjectRunResult
```

The implementation owns discovery, manifest parsing, repository fingerprints, documentation ingestion, build-session creation, decision deduplication, and notification production. Watchers and HTTP routes remain thin adapters at this seam.

## Persistence

A `projects` table records canonical path, name, product type, distribution targets, repository fingerprint, lifecycle state, active build session, last scan, and timestamps.

A `project_events` table records discovery, documentation ingestion, assessment, scaffold creation, build progress, verification, decisions, failures, and completion. Events are append-only.

Existing build sessions, work items, approval requests, evidence, and notification records remain authoritative for their respective concerns. Project intake links to them rather than duplicating their state.

## Autonomous Flow

1. The watcher observes a qualifying folder event and calls the authenticated project-intake scan route.
2. The service validates that the resolved project path is a direct child of the configured intake root.
3. The service creates or refreshes the project record and ingests supported project documentation with project metadata.
4. Zade evaluates the manifest and repository state.
5. A documentation-only project is scaffolded as a mobile application when its manifest authorizes `scaffold_on_intake`.
6. A Git repository is initialized inside a documentation-only project before source work starts.
7. Zade creates or resumes one governed build session and runs until completion, verification failure, or a decision gate.
8. Every terminal or blocked state is recorded and surfaced through the notification bus.

Same Ground explicitly enables `scaffold_on_intake`. Its handoff, workbook, and CSV bundle seed the initial product context. The first scaffold is selected from the locally supported mobile toolchain after environment inspection; the service does not invent a framework before checking installed tooling and the source documents.

## Authority and Decision Gates

Zade may automatically:

- read project files and Git state;
- ingest project documentation;
- create the project manifest when founder intent is already explicit;
- initialize a local Git repository for a documentation-only project;
- create and edit source files inside the registered project;
- install project-local dependencies;
- run project-local formatting, lint, test, type-check, and build commands;
- make reversible local commits on a non-protected branch;
- retry bounded repair loops after failed checks.

Zade must stop and request a founder decision for:

- conflicting or materially incomplete product requirements;
- credentials, signing keys, store accounts, or identity verification configuration;
- paid/cloud build leases;
- destructive migrations or deletion of founder-authored material;
- publishing, store submission, deployment, or external communication;
- privacy, crisis-response, moderation, or legal-policy choices that materially affect Same Ground;
- repeated verification failure after the bounded repair budget.

No project-intake rule grants access outside the registered project root.

## Telegram Decisions

Telegram becomes a proactive notification-bus adapter, separate from its existing inbound-reply loop. Delivery is limited to already bound founder chats and still requires the standing `reply_text:telegram` egress grant.

Decision messages include the project, blocking question, options when available, risk, recommended choice, work-item or approval identifier, and the fact that the build is paused. Dedupe keys prevent repeated alerts for the same unresolved decision.

A founder reply is routed through the existing governed Telegram conversation flow. Zade resolves the referenced durable decision, records the answer, and calls `run_until_blocked` for the same project. Ambiguous replies do not guess; they prompt once for the missing reference.

Notifications are also sent for verified completion and terminal failure. Routine progress does not generate Telegram noise.

## Initial Migration

The Dark Index migration moves the validated Git repository at `C:\BookCatalogingApp` to `C:\AI Brain\project-intake\The Dark Index` as one repository-preserving move. The destination is checked before the move, and the resulting Git root, status, and working tree are verified afterward. Existing untracked project files remain intact.

Same Ground migration creates `C:\AI Brain\project-intake\Same Ground` and moves the validated source pack:

- `Same_Ground_Zade_Handoff.md`;
- `Same_Ground_Project_Workbook.xlsx`;
- `Same_Ground_CSV_Bundle.zip`.

Duplicate inbox copies are reconciled only after content hashes are compared. No conflicting file is overwritten.

Zade's delegated-build configuration is retargeted from the single Dark Index repository to the project-intake container, while `BuildWorkspacePolicy` continues to accept only registered direct-child project roots.

## Failure Handling

- Partially copied or moved repositories are not registered as ready.
- Invalid or escaping paths are rejected and audited.
- A malformed manifest produces one decision item and no scaffold.
- Missing local mobile tooling pauses the project with the exact unmet prerequisite.
- A watcher failure is logged and retried on the next filesystem event or scheduled reconciliation scan.
- Notification delivery failures never discard the underlying decision; the UI Inbox remains authoritative.
- Restart recovery resumes from persisted project and build-session state, not conversation prose.

## Verification

Automated tests cover direct-child confinement, idempotent discovery, manifest parsing, documentation-only scaffolding, repository fingerprint changes, decision deduplication, Telegram recipient confinement, Telegram reply-to-resume behavior, migration-safe configuration, and restart recovery.

Live verification requires:

1. both destination folders exist and source locations no longer contain the moved project assets;
2. The Dark Index remains a valid Git repository with its pre-move status preserved;
3. Same Ground becomes a Git repository with a generated mobile scaffold;
4. both project records declare mobile application, Google Play, and eventual Apple App Store intent;
5. Zade can answer those product facts from live project intake state;
6. a synthetic decision produces exactly one Telegram alert to the bound founder chat;
7. a Telegram answer resolves that decision and resumes its project;
8. the kernel, watcher, Telegram adapter, and UI report healthy after restart.
