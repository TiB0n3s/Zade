# Token-Budgeted Build Delegation Design

Date: 2026-07-18
Status: Approved design

## Summary

Zade will lead SaaS and mobile builds through a local-first build session. Before
any paid model call, Zade will assess the requested build locally, recommend a
small, medium, or large cloud budget, and ask the founder to approve one scoped
build-session lease. The lease is a hard authorization envelope, not a spending
target.

The local runtime remains the authority and execution environment. It performs
repository inspection, context selection, routine implementation, command
execution, testing, verification, and state persistence. Anthropic is a
reasoning and coding delegate for selected difficult work. It receives only the
minimum relevant context and requests typed local tools; it does not receive
unrestricted machine access.

Every cloud request must reserve its worst-case cost before transmission. Actual
provider usage replaces the reservation after a successful response. A request
that cannot fit inside the active lease is refused locally. When a lease is near
exhaustion, Zade stops new cloud calls, continues useful local work, and files an
evidence-backed tier-upgrade request. It never upgrades or falls back to another
paid provider automatically.

## Goals

- Let Zade assess, plan, implement, verify, and lead SaaS and mobile builds.
- Keep all repository discovery, context preparation, and routine work local.
- Use Anthropic selectively for architecture, difficult debugging, cross-cutting
  changes, security-sensitive reasoning, and high-risk review.
- Make cloud authorization explicit, project-scoped, time-bounded, and
  budget-bounded.
- Enforce hard input-token, output-token, dollar, turn, and time ceilings.
- Make every estimate, authorization, reservation, charge, and upgrade request
  visible and auditable.
- Preserve build state across process restarts and budget exhaustion.
- Reuse Zade's existing workspace confinement, local coding tools, work queue,
  evidence records, audit log, and egress policy.
- Ensure automated tests never make a paid API call.

## Non-Goals

- Anthropic will not become Zade's authority engine.
- The first implementation will not use Anthropic Managed Agents.
- The first implementation will not add OpenAI as an automatic fallback.
- The cloud model will not receive the complete repository or unfiltered
  conversation history by default.
- A lease will not authorize deployment, purchasing, production database
  mutation, signing-key use, or app-store submission. Those remain separately
  governed actions.
- Budget approval will not imply that Zade should spend the approved amount.
- Pricing will not be fetched from the internet at runtime.

## Current System Constraints

The current delegated-build engine is `native`. It runs Zade's local Ollama
coding-agent loop and exposes confined tools for listing, reading, searching,
writing, exact replacement, allowlisted commands, and Git status/diff. The
existing `daily_budget` is an invocation-count circuit breaker; it does not
measure tokens or dollars.

The current Anthropic client is a one-shot Messages API transport used for
strategic review. It does not expose tool use, streaming, prompt caching,
sessions, or provider usage to callers. The egress matrix already treats source
code and founder briefs sent to Anthropic as per-request governed data.

The new design must deepen these existing modules rather than create an
unrelated second execution stack.

## Architecture

### BuildAssessment Module

Interface:

```python
assess_build(request, workspace) -> BuildAssessment
```

The module performs a zero-paid-token assessment. It combines deterministic
repository evidence with one local-model interpretation pass. Deterministic
evidence establishes the minimum complexity score and tier. The local model may
raise the score or explain uncertainty, but it may not lower that floor.

The result contains:

- normalized goal and acceptance criteria
- repository fingerprint and evidence timestamp
- deterministic score and evidence by dimension
- local-model risk adjustment and explanation
- final score, confidence, and recommended tier
- detected product surfaces and release obligations
- relevant unknowns and assumptions
- work that should remain local
- reasons cloud assistance may become useful

If local-model assessment is unavailable, deterministic assessment still
produces a valid result with lower confidence. Assessment failure never triggers
a cloud request.

### BuildSession Module

Interface:

```python
start_session(assessment) -> BuildSession
checkpoint(session_id, update) -> BuildSession
complete_session(session_id, evidence) -> BuildSession
```

A build session owns the product goal, workspace, assessment, acceptance
criteria, task graph, current phase, checkpoints, cloud lease, and verification
evidence. It is durable and can resume after restart.

Valid phases are:

```text
assessment -> approval -> planning -> implementation -> verification -> review -> complete
```

Budget exhaustion changes the lease state, not the build phase. Local work may
continue while cloud use is paused.

### BuildLease Module

Interface:

```python
approve_lease(session_id, tier, approval) -> BuildLease
reserve(lease_id, request_estimate) -> UsageReservation
settle(reservation_id, provider_usage) -> UsageEvent
pause(lease_id, reason) -> BuildLease
```

The module is the only seam through which a paid build request may be
authorized. An approved lease binds:

- one build session
- one workspace fingerprint
- one provider and model
- permitted egress data classes
- input-token ceiling
- output-token ceiling
- dollar ceiling
- cloud-turn ceiling
- expiration time
- founder approval record

Only one lease may be active for a build session. Counters are monotonic and
cannot be reset by process restart, retry, or model change.

### BuildRouter Module

Interface:

```python
route_step(session, step, local_history) -> local | cloud | founder
```

The router is deterministic-first and local-preferred. It may select cloud only
when an active lease exists and one or more cloud-eligibility reasons are
present. It records the reasons with the routing decision.

Cloud-eligible reasons are:

- architecture with material cross-module trade-offs
- difficult debugging after two distinct local attempts fail
- cross-cutting implementation with high regression risk
- security, authorization, billing, migration, or release-critical review
- high-risk diff review before Zade accepts the work
- a local model explicitly reports that the task exceeds its reliable capability

Cloud-ineligible work includes:

- repository inventory and dependency discovery
- file listing, reading, searching, and filtering
- task-status and audit summaries
- routine formatting or mechanical edits
- ordinary command execution
- test discovery and basic test runs
- context compression and checkpoint creation
- work that can be completed reliably by the local coding agent

### CloudBuildAdapter Module

Interface:

```python
run_turn(session, selected_context, tools, lease) -> CloudTurnResult
```

The first adapter uses the official Anthropic Python SDK and the Messages API.
It supports:

- structured client-executed tool use
- streaming response consumption
- prompt caching
- provider usage extraction
- bounded retries
- dependency injection for a no-network test adapter

The adapter does not decide whether cloud use is authorized, select arbitrary
files, execute tools, or mutate the ledger. Those responsibilities stay behind
the BuildRouter, local context selector, local tool executor, and BuildLease
interfaces.

### LocalToolExecutor Module

The existing native coding-agent tools become a provider-neutral local executor.
Both Ollama and Anthropic adapters receive the same schemas and invoke the same
confined handlers. Tool execution remains:

- restricted to the approved workspace
- argv-based with no shell parsing
- allowlisted for commands
- audited per call
- observable by the build session
- independently verified before completion claims are accepted

The cloud model never receives direct credentials or an unrestricted shell.

## Build Complexity Assessment

### Scoring Dimensions

The deterministic score ranges from 0 to 100:

| Dimension | Points | Evidence |
|---|---:|---|
| Product surfaces | 0-20 | UI, API, workers, admin, analytics, notifications |
| External integrations | 0-15 | payments, identity, vendors, native APIs |
| Change breadth | 0-15 | packages, modules, files, greenfield versus existing |
| Data and security | 0-15 | auth, tenancy, permissions, migrations, sensitive data |
| Platform and release | 0-15 | web, iOS, Android, signing, stores, infrastructure |
| Verification burden | 0-10 | unit, integration, E2E, emulator, migration, security |
| Novelty and ambiguity | 0-10 | unfamiliar stack, uncertain requirements, research risk |

Initial thresholds are:

- `SMALL`: 0-29
- `MEDIUM`: 30-64
- `LARGE`: 65-100

### Minimum-Tier Rules

The following rules prevent optimistic undersizing:

- Production authentication, authorization, payments, multitenancy, or database
  migrations require at least `MEDIUM`.
- A mobile client plus a new backend requires at least `MEDIUM`.
- Simultaneous iOS and Android release, store billing, custom native modules, or
  offline synchronization require at least `MEDIUM`.
- A greenfield SaaS backend plus cross-platform mobile clients requires `LARGE`.
- A production data migration combined with security-sensitive or cross-system
  changes requires `LARGE`.
- Three or more medium-floor release risks in one build require `LARGE`.
- Confidence below 0.65 raises the recommendation by one tier. `LARGE` remains
  `LARGE` and clearly reports the uncertainty.

The founder may approve the recommended tier, choose a lower tier with an audit
note, or explicitly authorize a custom envelope. Zade may not enlarge the
approved envelope itself.

## Default Lease Tiers

| Tier | Dollar ceiling | Input tokens | Output tokens | Cloud turns | Expiration |
|---|---:|---:|---:|---:|---:|
| `SMALL` | $1.00 | 120,000 | 16,000 | 6 | 2 hours |
| `MEDIUM` | $3.00 | 400,000 | 40,000 | 16 | 4 hours |
| `LARGE` | $7.00 | 1,000,000 | 80,000 | 32 | 8 hours |

These values are configuration defaults and hard ceilings. Tier configuration
must reject negative values, zero expirations, and a larger tier whose limits
are lower than a smaller tier.

Input-token accounting includes uncached input, cache creation, and cache reads.
Each category is retained separately for cost calculation and visibility.
Output tokens are accounted separately because their unit price differs.

## Reservation And Settlement

Before transmission, a cloud request must provide a local estimate containing:

- an authorizing input-token upper bound
- requested maximum output tokens
- expected cache action and its worst applicable price category
- resulting worst-case cost in integer microdollars

The authorizing input bound is not a normal language-model heuristic. When the
provider offers a confirmed non-billable token-count operation, the adapter may
use it after lease and egress authorization. Otherwise Zade uses a conservative
upper bound derived from the complete serialized UTF-8 request size plus a
configured provider-overhead allowance. A `characters / 4` style estimate may
be shown for planning but may not authorize transmission. If neither an exact
count nor a safe upper bound can be produced, the request fails closed.

The lease atomically creates a reservation only when all post-reservation totals
fit under every active ceiling. The request is not created until reservation
succeeds.

After a successful response:

1. Read provider-reported uncached input, cache-write, cache-read, and output
   usage.
2. Calculate cost using the pricing snapshot attached to the request.
3. Replace the reservation with an immutable settled usage event.
4. Update monotonic lease totals in the same transaction.

Dollar values are stored as integer microdollars. Every usage event stores the
model and pricing snapshot that produced its charge, so later pricing changes do
not rewrite history.

The pricing catalog is local configuration keyed by provider and model. Each
entry contains all input, cache, and output rates plus a `review_after` date.
Missing or expired pricing disables new paid requests for that model until the
catalog is explicitly refreshed. Zade never assumes an unknown model has the
same price as a known model and never fetches pricing automatically at runtime.

If provider usage is missing or malformed, Zade settles the reservation at its
reserved maximum and marks the event `conservative_settlement`.

If a network error or timeout occurs after transmission and usage is uncertain,
Zade keeps the full reservation, marks it `uncertain_spend`, and pauses new cloud
calls for that lease. It does not automatically resend the request.

## Prompt And Context Efficiency

### Local Context Selection

Zade never begins a cloud turn by sending the repository. A deterministic local
context selector constructs a working set from:

- build goal and acceptance criteria
- current task slice
- concise architecture/project manifest
- relevant file excerpts selected by local search
- current diff or patch, when applicable
- failing command output trimmed to high-signal sections
- decisions and constraints that affect the current slice
- previous cloud conclusion summarized locally

Unrelated files, old conversation turns, duplicate tool results, generated
artifacts, dependencies, secrets, and build output are excluded.

### Prompt Caching

The stable prefix is ordered as:

1. tool definitions
2. Zade build charter and execution rules
3. project conventions and stable architecture manifest
4. volatile task messages and tool results

The adapter places a cache breakpoint after the stable prefix. A stable-prefix
version changes only when its content changes. Cache creation and cache-read
usage are visible in the lease ledger.

### Turn Discipline

- One cloud turn addresses one concrete task slice.
- Maximum output is selected per turn and must fit the remaining reservation.
- Local execution and verification occur between cloud turns.
- Repeated file discovery through cloud is a routing defect and must fall back
  to local selection.
- The local checkpoint summarizes completed work before another cloud turn is
  considered.

## Egress And Approval

The build-session approval creates a session-scoped egress authorization, not a
standing grant. Every Anthropic request still passes the egress gate and cites
the active lease. The authorization is valid only for:

- the approved build session and workspace fingerprint
- the approved provider and model
- the approved data classes
- the lease lifetime and remaining budget

Raw founder state, credentials, unrelated memory, and unselected source code
remain forbidden. Existing one-shot strategic-review grants continue unchanged.

The approval request shows:

- assessment score, confidence, evidence, and tier
- all token, dollar, turn, and time ceilings
- provider and model
- permitted data classes
- workspace and repository fingerprint
- local work that will occur before cloud use

## Exhaustion And Upgrade

At 80 percent of any ceiling, the lease enters `warning`. Warning alone does not
authorize extra spend. New cloud turns are permitted only when their full
reservation fits safely inside every remaining ceiling.

When no useful request fits, the lease enters `exhausted` and cloud routing is
disabled. Zade then:

1. preserves the current checkpoint and cloud transcript summary
2. continues local implementation, testing, or inspection where useful
3. creates one deduplicated founder upgrade request
4. reports authorized versus actual spend, work completed, remaining work, the
   reason more cloud assistance is needed, and the proposed next tier

An upgrade creates a new version of the same lease ledger. It does not reset
prior usage. `SMALL` may upgrade to `MEDIUM`; `MEDIUM` may upgrade to `LARGE`;
`LARGE` requires an explicit custom envelope. No upgrade occurs automatically.

## Retry And Failure Policy

- Failures never trigger automatic fallback to another paid provider.
- A pre-transmission failure releases its reservation because no request left
  the process.
- A post-transmission ambiguous failure keeps its reservation and pauses cloud
  use.
- A provider-declared zero-usage rejection may release the reservation when the
  response proves no model inference occurred.
- Tool failures are returned to the active model turn only when budget remains;
  otherwise they are checkpointed for local handling.
- Retry count is bounded and each retry is a separately reserved cloud turn.
- Rate limits pause cloud activity until the provider's retry time, lease
  expiration, or founder intervention, whichever occurs first.
- Process restart restores reservations, settlements, checkpoints, and lease
  state from SQLite.

## Persistence Model

The database adds four durable records:

### build_assessments

- assessment id and timestamps
- normalized request and acceptance criteria
- workspace and repository fingerprint
- deterministic and local-model scores
- final score, confidence, tier, and evidence JSON

### build_sessions

- session id and assessment id
- work item, conversation, and workspace references
- phase and status
- task graph and checkpoint JSON
- verification and completion evidence references

### build_leases

- lease id, session id, version, tier, provider, and model
- each approved ceiling
- each actual and reserved counter
- state, approval reference, start, and expiration

### cloud_usage_events

- unique request/reservation id
- lease id and cloud turn number
- estimated and actual token categories
- reserved and settled microdollars
- pricing snapshot
- status and timestamps

Usage events are append-only. Lease totals can be rebuilt from them and are
checked against the stored counters during startup maintenance.

## Status, UI, And Audit

`/delegation/status` gains a build-session summary while preserving existing
delegation fields. A dedicated build-session read endpoint supplies full detail
for the UI.

The founder can see:

- assessment score, evidence, confidence, and recommended tier
- build phase and current task slice
- lease state and expiration
- actual, reserved, and remaining tokens by category
- actual and authorized dollar amounts
- cloud turns used and remaining
- cache creation and cache-read performance
- local versus cloud work performed
- upgrade reason and projected remaining work

Audit rows retain provider, model, lease, usage, calculated cost, cache
categories, tool names, routing reasons, and status. They do not retain API keys,
complete prompts, or unnecessary source-code contents. The egress ledger should
reference the lease and usage event for every permitted cloud send.

## Security Invariants

- No active approved lease means no paid build request.
- A reservation that exceeds any ceiling means no network call.
- The provider key is read from the configured environment variable only.
- All Anthropic traffic passes the existing HTTPS host allowlist.
- The cloud model cannot change its own lease, routing rules, tool allowlist, or
  workspace.
- Tool paths cannot escape the workspace.
- Credentials and raw founder state remain forbidden by the egress matrix.
- Approval, reservation, send, settlement, and upgrade are separately audited.
- A build lease never grants deployment or production mutation authority.

## Testing Strategy

Implementation follows red-green-refactor. Tests use the real assessment,
ledger, routing, and persistence modules with a fake provider transport only at
the network seam.

### Assessment Tests

- simple contained changes classify as `SMALL`
- auth, billing, migration, mobile, and release rules enforce their floors
- greenfield SaaS plus mobile classifies as `LARGE`
- local-model adjustment can raise but not lower the deterministic floor
- unavailable local inference yields deterministic output without cloud access
- repository fingerprints change when relevant project inputs change

### Lease And Accounting Tests

- approval creates exactly one active lease for a session
- no reservation succeeds without an active lease
- each token, dollar, turn, and time ceiling is enforced independently
- reservations are atomic under concurrent requests
- reported input, output, cache-write, and cache-read usage prices correctly
- pricing snapshots preserve historical cost
- missing usage settles at the reserved maximum
- ambiguous timeout retains the reservation and pauses the lease
- restart restores counters without resetting budget
- upgrade preserves all previous usage

### Routing And Context Tests

- simple work remains local even with an approved cloud lease
- eligible difficult work routes to cloud only with a valid reservation
- full repository contents and unrelated conversation turns are excluded
- stable prompt prefix carries cache control and stable ordering
- tool calls execute through the existing confined local handlers
- local verification occurs before completion evidence is filed

### Egress And Integration Tests

- no network call occurs before founder approval
- every build request cites a valid lease authorization
- source code crosses only the permitted egress cell
- provider failure never falls back to OpenAI or another paid provider
- exhaustion creates one deduplicated upgrade request
- status and egress-ledger views reconcile with immutable usage events
- the complete automated suite runs with sentinel API keys and proves they are
  never transmitted

An optional manually invoked live smoke may use a deliberately tiny approved
lease and a harmless fixture repository. It is excluded from automated tests and
must report actual provider usage and cost.

## Acceptance Criteria

The feature is complete when all of the following are true:

1. A build request produces a local evidence-based assessment and tier before
   any paid request.
2. The founder can approve one project-scoped, expiring build lease.
3. Local work remains the default even when a lease exists.
4. Anthropic can reason and request Zade's confined coding tools for eligible
   task slices.
5. Every paid request is preceded by an atomic reservation that fits every
   ceiling.
6. Provider usage is settled into an immutable, restart-safe ledger.
7. Prompt caching and focused context selection are active and observable.
8. Exhaustion pauses cloud use, preserves progress, continues local work where
   useful, and creates one evidence-backed upgrade request.
9. No automatic paid retry, provider fallback, or tier escalation exists.
10. Zade's completion claims remain backed by locally executed verification.
11. Automated tests make zero paid API calls and cover the failure cases above.

## Recommended Implementation Sequence

The implementation plan should split work into small reviewable commits in this
order:

1. assessment types, deterministic scanner, scoring, and tests
2. durable build-session and lease schema with accounting tests
3. approval, reservation, settlement, and status interfaces
4. provider-neutral extraction of the local coding tools
5. Anthropic SDK adapter with fake-transport tests, usage, and caching
6. local-first build router and context selector
7. exhaustion, checkpoint, and upgrade flow
8. API/UI visibility and egress-ledger reconciliation
9. full regression, offline acceptance, and optional bounded live smoke
