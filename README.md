# Zade Local AI Co-founder Kernel

Zade is the local-first kernel for a Jarvis-like AI co-founder. It keeps the critical path local:

- Ollama for model calls
- SQLite for structured memory and audit logs
- `C:\AI Brain` as hot memory
- `D:\AI Brain-Cold` as cold/archive storage
- typed local tools with permission tiers
- authority-gated autonomous work queue
- FastAPI bound to localhost by default

No external API is required for the initial runtime.

## Quick Start

```powershell
cd C:\LocalAICofounder
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\scripts\start.ps1
```

Then open:

```text
http://127.0.0.1:8787/ui
```

`.\Start-Zade.cmd` is the double-clickable Windows launcher. It starts the kernel if needed, verifies `/health`, checks that `/ui` is served, then opens the UI.

Stop the server with:

```powershell
.\scripts\stop.ps1
```

Check status with:

```powershell
.\scripts\status.ps1
```

Run the local operating cadence once:

```powershell
.\scripts\run-cadence.ps1
```

Run the local model benchmark:

```powershell
.\scripts\benchmark-models.ps1
```

Preview backup retention:

```powershell
.\scripts\prune-backups.ps1 -KeepLast 10
```

Commit backup retention:

```powershell
.\scripts\prune-backups.ps1 -KeepLast 10 -Commit
```

Install the daily local cadence task:

```powershell
.\scripts\install-cadence-task.ps1 -At "8:00AM"
```

## Configuration

Copy `config.example.toml` to `config.toml` if you want to override defaults.

Default identity:

```text
name = "Zade"
description = "Local-first AI co-founder and private operating partner."
```

The default memory paths are:

```text
C:\AI Brain\memory-hot\cofounder-kernel
C:\AI Brain\inbox
D:\AI Brain-Cold
D:\AI Brain-Cold\raw-ingest
```

The app creates the hot data directory on startup. It does not create or mutate the rest of the vault layout unless a local tool explicitly writes a memory record.

Optional mutation guard:

```powershell
$env:COFOUNDER_LOCAL_TOKEN = "choose-a-local-secret"
```

When set, every `POST`, `PUT`, `PATCH`, and `DELETE` requires `X-Zade-Token`. The static UI reads the same token from `localStorage.zadeKernelToken`.

## Core Endpoints

```text
GET  /health
GET  /authority
POST /authority/evaluate
GET  /self-inventory
GET  /identity/charter
POST /identity/charter
GET  /identity/relationships
GET  /identity/relationships/{subject_name}
POST /identity/relationships
GET  /identity/voice
POST /identity/voice
GET  /work/queue
POST /work/items
POST /work/scan
POST /work/run-next
POST /work/run-due
GET  /approval-requests
GET  /approval-requests/{request_id}
POST /approval-requests/{request_id}/approve
POST /approval-requests/{request_id}/deny
POST /work/items/{item_id}/approve
POST /work/items/{item_id}/deny
POST /work/items/{item_id}/dispatch
GET  /trading-bot/status
GET  /trading-bot/safe-ops-checks
GET  /trading-bot/deep-thought-replacement
GET  /trading-bot/sqlite/schema
POST /trading-bot/sqlite/query
POST /trading-bot/evidence/snapshot
POST /trading-bot/ops-check
POST /trading-bot/recommendations
POST /trading-bot/advisory/generate
POST /trading-bot/advisory/score
POST /trading-bot/daily-brief
GET  /trading-bot/judgments
POST /trading-bot/judgments/score
POST /trading-bot/dt-trigger/proposals
GET  /founder/mental-models
GET  /founder/thesis
POST /founder/thesis
GET  /founder/dashboard
GET  /founder/metrics
GET  /founder/brief
GET  /founder/strategy
POST /founder/strategy
GET  /founder/initiatives
POST /founder/initiatives
GET  /founder/decisions
POST /founder/decisions
GET  /founder/predictions
POST /founder/predictions
POST /founder/predictions/score
GET  /founder/contrarian-reviews
POST /founder/contrarian-reviews
GET  /founder/reflections
POST /founder/reflections
GET  /founder/assumptions
POST /founder/assumptions
GET  /founder/evidence
POST /founder/evidence
GET  /founder/links
POST /founder/links
GET  /founder/strategy-objects
POST /founder/strategy-objects
GET  /founder/goals
POST /founder/goals
GET  /founder/tasks
POST /founder/tasks
GET  /founder/kill-criteria
POST /founder/kill-criteria
GET  /founder/overrides
POST /founder/overrides
GET  /founder/confidence-events
GET  /founder/thesis-conflicts
POST /founder/thesis-conflicts
GET  /founder/missed-calls
POST /founder/missed-calls
GET  /founder/integrity-warnings
POST /founder/integrity-check
GET  /founder/cadence-reviews
POST /founder/cadence-reviews
POST /founder/cadence-reviews/generate/{review_type}
POST /conversations
GET  /conversations
GET  /conversations/{conversation_id}
GET  /conversations/{conversation_id}/turns
GET  /surface/attention
POST /surface/brief
GET  /evals/cases
POST /evals/cases
POST /evals/run
GET  /evals/runs
GET  /evals/runs/{run_id}
GET  /connectors
POST /connectors
GET  /connectors/{name}
POST /connectors/{name}/sync
GET  /connectors/items
POST /connectors/items/import
POST /connectors/items/{item_id}/dismiss
GET  /voice/status
POST /voice/transcribe
POST /voice/speak
POST /voice/converse
POST /action-plans
POST /action-plans/from-recommendation/{recommendation_id}
GET  /action-plans
GET  /action-plans/{plan_id}
POST /action-plans/{plan_id}/advance
POST /action-plans/{plan_id}/steps/{step_id}/approve
POST /action-plans/{plan_id}/steps/{step_id}/complete
POST /action-plans/{plan_id}/steps/{step_id}/fail
POST /action-plans/{plan_id}/steps/{step_id}/skip
POST /action-plans/{plan_id}/steps/{step_id}/evidence
POST /commitments
GET  /commitments
GET  /commitments/{commitment_id}
POST /commitments/{commitment_id}/done
POST /commitments/{commitment_id}/miss
POST /commitments/{commitment_id}/drop
POST /commitments/{commitment_id}/renegotiate
POST /commitments/check
POST /notify
GET  /notifications
POST /notifications/{notification_id}/read
GET  /notify/channels
POST /notify/channels/{channel}
GET  /audit/recent
GET  /models
GET  /models/telemetry
GET  /models/telemetry/calls
POST /models/benchmark
GET  /ops/health-check
GET  /ops/security
GET  /ops/supervision
POST /ops/backup
GET  /ops/backups
POST /ops/backups/prune
GET  /ingest/jobs
POST /memory
POST /memory/search
POST /memory/semantic-search
POST /ingest/text
POST /ingest/file
POST /ingest/folder
GET  /brief/daily
POST /chat
GET  /runtime/charter-stack
GET  /runtime/context
POST /runtime/context
POST /runtime/respond
POST /runtime/operating-loop
POST /runtime/evidence-loop
POST /runtime/experiment-loop
POST /runtime/cadence
GET  /runtime/events
POST /teach/deepthought/scan
GET  /teach/deepthought/candidates
POST /teach/deepthought/import
POST /teach/deepthought/link
POST /teach/deepthought/auto-link
GET  /evidence/gaps
GET  /experiments
POST /experiments
GET  /experiments/dashboard
GET  /experiments/reviews
GET  /experiments/{experiment_id}
POST /experiments/{experiment_id}/evidence
POST /experiments/{experiment_id}/review
POST /experiments/{experiment_id}/pushback
GET  /action-handlers
```

`/chat` calls Ollama with `think=false` by default so Qwen3 does not spend the response budget in the hidden thinking field.

## Trading-Bot Bridge

Zade integrates with the trading-bot through the existing observe-only Deep Thought recommendation lane:

- `GET /trading-bot/status` checks the local WSL checkout and advisory lane files.
- `POST /trading-bot/ops-check` runs only allowlisted read-only diagnostics.
- `GET /trading-bot/sqlite/schema` inspects the bot database schema through the read-only adapter.
- `POST /trading-bot/sqlite/query` runs a capped read-only SQLite query against the allowlisted bot database.
- `POST /trading-bot/evidence/snapshot` captures date/symbol-scoped evidence rows from known diagnostic tables.
- `POST /trading-bot/recommendations` queues an approval-required work item.
- `POST /trading-bot/advisory/generate` collects real bot diagnostics, records them as Zade evidence, generates conservative symbol-scoped advisory candidates, and optionally queues them for approval.
- `POST /trading-bot/advisory/score` runs the bot-owned outcome report and stores the scorecard as founder evidence.
- `POST /trading-bot/daily-brief` runs the local daily trading intelligence loop, writes a Zade evidence brief, records `trading_judgments`, scores available outcomes, and optionally exports markdown to the Trading Project raw folder.
- `GET /trading-bot/judgments` lists Zade's stored trading judgment ledger with date, symbol, and outcome filters.
- `POST /trading-bot/judgments/score` directly scores stored judgments against read-only realized outcome rows.
- `POST /trading-bot/dt-trigger/proposals` queues a proposal-only `dt_trigger` work item; approved dispatch records the proposal locally and does not run the bot.
- `GET /trading-bot/deep-thought-replacement` shows which Deep Thought trading-bot seams Zade has replaced and which remain planned.
- Approved dispatch calls the bot-owned `scripts/dt_recommendation_ingest.py` CLI.

The bridge cannot approve, block, size, route, place, or cancel trades. It writes only local Zade evidence, local Zade trading judgments, missed-call reviews, optional local vault brief exports, proposal-only dt_trigger records, and approval-gated advisory rows with `runtime_effect = "advisory_only_no_trade_authority"`. Dispatch requires the normal approval-console typed confirmation phrase.

The SQLite adapter opens only the allowlisted bot database with `mode=ro`, sets `PRAGMA query_only = ON`, blocks write/schema/attachment tokens before WSL execution, permits only `SELECT`, `WITH`, `EXPLAIN`, and narrow read-only `PRAGMA` statements, and caps rows/timeouts on every call.

Inspect a table:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8787/trading-bot/sqlite/schema?table=auto_buy_candidates"
```

Run a capped read-only query:

```powershell
$body = @{
  sql = "SELECT symbol, decision, score FROM auto_buy_candidates WHERE substr(timestamp, 1, 10) = ? LIMIT 5"
  params = @("2026-07-10")
  limit = 5
} | ConvertTo-Json -Depth 8

Invoke-RestMethod -Uri "http://127.0.0.1:8787/trading-bot/sqlite/query" -Method Post -Body $body -ContentType "application/json"
```

Capture a symbol-scoped evidence snapshot:

```powershell
$body = @{
  target_date = "2026-07-10"
  symbols = @("AAPL")
  tables = @("auto_buy_candidates", "trades", "dt_recommendations")
  store_evidence = $true
} | ConvertTo-Json -Depth 8

Invoke-RestMethod -Uri "http://127.0.0.1:8787/trading-bot/evidence/snapshot" -Method Post -Body $body -ContentType "application/json"
```

Generate advisory candidates without queueing them:

```powershell
$body = @{
  target_date = "2026-07-10"
  symbols = @("AAPL", "MSFT")
  queue = $false
} | ConvertTo-Json -Depth 8

Invoke-RestMethod -Uri "http://127.0.0.1:8787/trading-bot/advisory/generate" -Method Post -Body $body -ContentType "application/json"
```

Queue advisory candidates for approval:

```powershell
$body = @{
  target_date = "2026-07-10"
  symbols = @("AAPL", "MSFT")
  queue = $true
} | ConvertTo-Json -Depth 8

Invoke-RestMethod -Uri "http://127.0.0.1:8787/trading-bot/advisory/generate" -Method Post -Body $body -ContentType "application/json"
Invoke-RestMethod -Uri "http://127.0.0.1:8787/approval-console"
```

Score the advisory track record against bot outcomes:

```powershell
$body = @{ target_date = "2026-07-10" } | ConvertTo-Json
Invoke-RestMethod -Uri "http://127.0.0.1:8787/trading-bot/advisory/score" -Method Post -Body $body -ContentType "application/json"
```

Run the daily trading intelligence loop:

```powershell
cd C:\LocalAICofounder
.\scripts\run-trading-brief.ps1 -TargetDate "2026-07-10"
```

Run it and export the markdown artifact into `C:\AI Brain\Trading Project\01-raw`:

```powershell
cd C:\LocalAICofounder
.\scripts\run-trading-brief.ps1 -TargetDate "2026-07-10" -ExportVault
```

Install it as a daily Windows scheduled task:

```powershell
cd C:\LocalAICofounder
.\scripts\install-trading-brief-task.ps1 -At "5:30PM"
```

Inspect the judgment ledger:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8787/trading-bot/judgments?market_date=2026-07-10"
```

Score stored judgments directly against read-only realized outcomes:

```powershell
$body = @{ target_date = "2026-07-10"; symbols = @(); store_evidence = $true } | ConvertTo-Json -Depth 8
Invoke-RestMethod -Uri "http://127.0.0.1:8787/trading-bot/judgments/score" -Method Post -Body $body -ContentType "application/json"
```

Queue a proposal-only `dt_trigger` review:

```powershell
$body = @{
  operation = "paper-session-review"
  target_date = "2026-07-10"
  reason = "Review the paper-session evidence before any future promotion discussion."
  params = @{ mode = "review_only" }
} | ConvertTo-Json -Depth 8

Invoke-RestMethod -Uri "http://127.0.0.1:8787/trading-bot/dt-trigger/proposals" -Method Post -Body $body -ContentType "application/json"
```

Open the local trading console:

```powershell
Start-Process "http://127.0.0.1:8787/ui/trading.html"
```

## Conversation Memory

The governed runtime keeps durable episodic memory. Each `/runtime/respond` call can carry a `conversation_id` so Zade remembers the thread across turns. Recent turns are folded into the governed prompt verbatim; older turns roll into a bounded rolling summary so a thread can grow indefinitely without unbounded prompt size. Conversation memory never overrides authority, the voice charter, or evidence honesty.

Start a conversation and continue it:

```powershell
$conversation = Invoke-RestMethod -Uri "http://127.0.0.1:8787/conversations" -Method Post -Body (@{ title = "Pricing" } | ConvertTo-Json) -ContentType "application/json"

$turn = @{
  message = "We should price Zade at $99/month for solo founders."
  conversation_id = $conversation.conversation.id
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://127.0.0.1:8787/runtime/respond" -Method Post -Body $turn -ContentType "application/json"

$followup = @{
  message = "Remind me what price we landed on."
  conversation_id = $conversation.conversation.id
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://127.0.0.1:8787/runtime/respond" -Method Post -Body $followup -ContentType "application/json"
```

Inspect a thread and its turns:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8787/conversations"
Invoke-RestMethod -Uri "http://127.0.0.1:8787/conversations/1"
Invoke-RestMethod -Uri "http://127.0.0.1:8787/conversations/1/turns"
```

## Proactive Surfacing

Zade initiates instead of waiting to be asked. The surfacing layer deterministically scans the operating layer for signals that need founder attention, ranks them, and composes an initiated brief: what changed since the last brief, the one thing that matters most, and the longest-open risk you may be underweighting.

Signal sources:

- overdue kill criteria (you committed to a decision by a date and it passed)
- open integrity warnings
- experiments awaiting a continue/revise/kill decision
- open thesis conflicts
- predictions past due and unscored
- decisions past their revisit date
- confidence drops since the last brief
- founder overrides due for review
- assumptions past their review date
- experiments short on evidence
- approval requests waiting on you

Detection never calls a model, so the attention queue is deterministic and auditable. Briefs are persisted as `initiated_brief` memories only when something needs attention; quiet runs log a runtime event without writing noise.

Read the current attention queue:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8787/surface/attention"
```

Generate an initiated brief (optionally with a model-narrated executive read):

```powershell
$body = @{ narrate = $false; force = $false } | ConvertTo-Json
Invoke-RestMethod -Uri "http://127.0.0.1:8787/surface/brief" -Method Post -Body $body -ContentType "application/json"
```

The daily cadence (`/runtime/cadence` and the installed `Zade Local Cadence` task) generates an initiated brief automatically after the operating, evidence, and experiment loops run. When attention items exist, the cadence `next_action` is driven by the top surfaced item.

## Eval Harness

The eval harness regression-tests Zade's reasoning quality so model swaps and prompt changes are measurable instead of vibes. A golden set of founder scenarios runs through the real model pipeline and is graded with deterministic checks — no model judges another model's output.

Default golden cases:

- `probe-exact-ack`, `probe-json-object`, `probe-coding-function` — instruction-following probes per model role
- `critic-json-contract` — the reasoning model honors the JSON contract the automatic contrarian pass depends on
- `respond-decision-contract` — governed recommendations include the decision-engine contract elements
- `respond-evidence-honesty` — with no local evidence, the response says so instead of faking certainty
- `grounding-memory-recall` — a fact seeded into local memory is recalled through the governed pipeline

Each run records the active model roles and is compared against the previous run: `newly_failing` cases are regressions, `newly_passing` are recoveries, and `pass_rate_delta` tracks drift.

Run the golden set:

```powershell
$body = @{ label = "baseline" } | ConvertTo-Json
Invoke-RestMethod -Uri "http://127.0.0.1:8787/evals/run" -Method Post -Body $body -ContentType "application/json"
```

Inspect cases and run history:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8787/evals/cases"
Invoke-RestMethod -Uri "http://127.0.0.1:8787/evals/runs"
Invoke-RestMethod -Uri "http://127.0.0.1:8787/evals/runs/1"
```

Add a custom case (executors: `generate`, `respond`, `critic`; checks: `contains`, `contains_any`, `not_contains`, `regex`, `json_parseable`, `json_keys`, `min_chars`, `max_chars`):

```powershell
$case = @{
  name = "custom-positioning-recall"
  category = "grounding"
  executor = "respond"
  prompt = "What positioning did we commit to?"
  setup_memories = @(@{ kind = "decision"; title = "Positioning decision"; content = "We committed to founder-operators as the wedge." })
  checks = @(@{ type = "contains"; value = "founder-operators" })
} | ConvertTo-Json -Depth 8

Invoke-RestMethod -Uri "http://127.0.0.1:8787/evals/cases" -Method Post -Body $case -ContentType "application/json"
```

Run evals after changing models, prompts, or routing before trusting the new configuration.

## Read-Only External Connectors

Connectors give Zade situational awareness of email and calendars without granting any outbound capability. They are read-only by construction: IMAP mailboxes are opened `readonly` with `BODY.PEEK` fetches, and ICS calendars are parsed from local exports or feeds. Nothing is ever sent or mutated.

The safety contract:

- **Sync executes only through the approved dispatch flow.** `POST /connectors/{name}/sync` queues an `external.connector.sync` work item (approval required under authority v2); the founder approves and dispatches it with the typed confirmation phrase.
- **Credentials never enter the database.** Connector configs reference an environment variable via `password_env`; configs containing literal password/token/secret keys are rejected at creation.
- **External content is staged, then graded.** Synced items land as candidates; the founder imports the useful ones as `founder_evidence` (reliability defaults to C) with entity-boundary metadata — external claims never become native certainty. The surfacing layer flags staged candidates awaiting review.

Create an IMAP connector (Gmail/Outlook app passwords work well):

```powershell
$env:ZADE_INBOX_PASSWORD = "app-password-here"
$body = @{
  name = "founder-inbox"
  connector_type = "imap"
  config = @{ host = "imap.gmail.com"; username = "you@example.com"; mailbox = "INBOX"; password_env = "ZADE_INBOX_PASSWORD" }
} | ConvertTo-Json -Depth 8

Invoke-RestMethod -Uri "http://127.0.0.1:8787/connectors" -Method Post -Body $body -ContentType "application/json"
```

Create an ICS calendar connector from a local export or a secret feed URL:

```powershell
$body = @{
  name = "founder-calendar"
  connector_type = "ics"
  config = @{ path = "C:\AI Brain\inbox\calendar-export.ics" }
} | ConvertTo-Json -Depth 8

Invoke-RestMethod -Uri "http://127.0.0.1:8787/connectors" -Method Post -Body $body -ContentType "application/json"
```

Queue, approve, and dispatch a sync:

```powershell
$queued = Invoke-RestMethod -Uri "http://127.0.0.1:8787/connectors/founder-calendar/sync" -Method Post

$approve = @{
  resolved_by = "founder"
  dispatch = $true
  typed_confirmation = "make the jump to hyperspace"
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://127.0.0.1:8787/work/items/$($queued.item_id)/approve" -Method Post -Body $approve -ContentType "application/json"
```

Review staged items, import the useful ones as evidence, dismiss the rest:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8787/connectors/items?status=candidate"

$import = @{ item_ids = @(1); reliability = "C"; strength = 60 } | ConvertTo-Json
Invoke-RestMethod -Uri "http://127.0.0.1:8787/connectors/items/import" -Method Post -Body $import -ContentType "application/json"

Invoke-RestMethod -Uri "http://127.0.0.1:8787/connectors/items/2/dismiss" -Method Post -Body (@{ reason = "not business-relevant" } | ConvertTo-Json) -ContentType "application/json"
```

## Decision-to-Action Pipeline

Recommendations become action plans; plans become steps; every step carries its own authority evaluation and its own evidence trail.

- `POST /action-plans/from-recommendation/{id}` converts a decision-engine recommendation into a plan (the recommendation is marked `planned`).
- Steps are `manual` (founder work the pipeline tracks) or `work_queue` (machine steps that execute through the existing work queue, so approvals and the typed confirmation phrase apply unchanged).
- Step statuses: `pending`, `blocked`, `approval_required`, `approved`, `queued`, `running`, `done`, `failed`, `skipped`. A denied action blocks the step — and the plan — at creation.
- Step outcomes are recorded automatically as grade-A evidence (`action_step_outcome`) in the founder ledger; additional evidence can be attached per step.
- Stalled plans (blocked/failed) surface in the attention queue; failed steps go through the notification bus.

```powershell
$plan = @{
  title = "Validate pricing with five founders"
  steps = @(
    @{ title = "Draft the interview script" },
    @{ title = "Send follow-up emails"; action = "email.send"; permission_tier = "L3_EXTERNAL_ACTION" }
  )
} | ConvertTo-Json -Depth 8
$created = Invoke-RestMethod -Uri "http://127.0.0.1:8787/action-plans" -Method Post -Body $plan -ContentType "application/json"

Invoke-RestMethod -Uri "http://127.0.0.1:8787/action-plans/$($created.item.id)/advance" -Method Post
```

### Developer Action Handlers

Zade can do developer work, not just advise on it, through a set of approved action handlers. These make him a co-founder that acts — run tests and lint, inspect/branch/commit a repo, and draft outbound messages — while every action stays behind the same gate as any other external action.

- `dev.command.run` — run an **allowlisted** command in the workspace: `pytest`, `ruff-check`, `ruff-format-check`, `git-status`, `git-diff`, `git-diff-staged`, `git-log`, `python-version`. No arbitrary shell; args cannot use absolute paths or `..`.
- `dev.git.branch` — create or switch to a branch.
- `dev.git.commit` — stage and commit local changes; **refuses the default branch** unless `metadata.allow_default_branch` is set, and only commits already-staged local changes.
- `dev.draft.write` — write an email/PR/message draft to the local drafts folder. It is never sent; sending stays a human action.

The safety model is unchanged: these run **only through approved dispatch** — an approved work item plus the typed confirmation phrase — and execution is confined to `[devtools] workspace_root` in `config.toml` (defaults to the kernel's own repo; point it at whichever repo you want help with). Because they run as work-queue steps, the decision-to-action pipeline can emit them: a plan step with `execution = "work_queue"` and `action = "dev.command.run"` becomes a queued item that you approve, and its output is recorded as grade-A evidence.

Run the workspace tests through the approval flow:

```powershell
$item = @{
  kind = "action_step"; title = "Run tests before shipping"
  action = "dev.command.run"; permission_tier = "L3_EXTERNAL_ACTION"
  metadata = @{ command = "pytest" }
} | ConvertTo-Json -Depth 8
$queued = Invoke-RestMethod -Uri "http://127.0.0.1:8787/work/items" -Method Post -Body $item -ContentType "application/json"

$approve = @{ dispatch = $true; typed_confirmation = "make the jump to hyperspace" } | ConvertTo-Json
Invoke-RestMethod -Uri "http://127.0.0.1:8787/work/items/$($queued.item_id)/approve" -Method Post -Body $approve -ContentType "application/json"
```

## Commitment Ledger

The ledger tracks what you said you would do and what Zade said he would monitor — deadlines, misses, drift, and follow-ups. This is what makes Zade a co-founder instead of a smart notes app.

- `who` is `founder` or `zade`; `kind` is `do`, `decide`, `deliver`, or `monitor` (monitors carry a `daily`/`weekly`/`monthly` cadence).
- `POST /commitments/check` (also run by the daily cadence) flags overdue, due-soon, drifting, and monitor-due commitments, records at most one follow-up per commitment per day, and notifies on newly overdue promises. It never closes anything.
- Closing is explicit: `done` (optionally with evidence), `miss`, or `drop` — history is never quietly rewritten.
- Renegotiating moves the date and counts it; two or more renegotiations is drift, and drift gets surfaced.

```powershell
$commitment = @{ title = "Send the pilot pricing proposal"; due_at = "2026-07-15"; who = "founder" } | ConvertTo-Json
Invoke-RestMethod -Uri "http://127.0.0.1:8787/commitments" -Method Post -Body $commitment -ContentType "application/json"

Invoke-RestMethod -Uri "http://127.0.0.1:8787/commitments/check" -Method Post
```

## Notification Bus

One internal `notify()` for every producer — surfacing briefs, overdue commitments, failed action steps, and anything else. No feature talks to a delivery channel directly.

- Channels: `ui` (the notification feed, on by default), `voice` (speaks via the configured TTS engine, off by default), `sms` (off by default; your Android SMS gateway plugs in here via `config.gateway_url`, not into random features).
- Rules per channel: enabled flag, minimum severity (`info`/`warning`/`critical`), quiet hours (`22:00`-`07:00` style, overnight windows supported), hourly rate limits, and a recipient whitelist for outbound channels.
- Critical notifications bypass quiet hours but never the whitelist or the rate limit. Duplicate `dedupe_key`s within an hour are suppressed. Every suppression is recorded with its reason.
- Enabling an outbound channel is a standing founder grant, bounded by those rules.

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8787/notify/channels"

$sms = @{
  enabled = $true
  recipients = @("+15551234567")
  config = @{ gateway_url = "http://192.168.1.50:8686/send"; to = "+15551234567" }
} | ConvertTo-Json -Depth 8
Invoke-RestMethod -Uri "http://127.0.0.1:8787/notify/channels/sms" -Method Post -Body $sms -ContentType "application/json"

Invoke-RestMethod -Uri "http://127.0.0.1:8787/notifications?unread_only=true"
```

## Voice Loop

The voice loop wraps the governed runtime in speech. Voice is an interface, not a bypass — `/voice/converse` transcribes, answers through `runtime.respond` (authority, charters, episodic memory, and the contrarian pass all apply), then synthesizes the reply.

The easiest way to use it is the browser: open `http://127.0.0.1:8787/ui/voice.html` (also linked as **Voice** from the founder dashboard). Click the mic to record a question and hear Zade answer inline; or type text and hear it spoken back. Browser audio needs no file associations — recordings post as base64 with their real mime type, and Zade's reply plays in an `<audio>` element. Every reply also appears as **full text** in an always-visible "Zade says" readout (including the contrarian check), so you never miss anything when audio is off. Uncheck **Play reply audio** to read silently — Zade then skips synthesis entirely, saving latency and TTS quota. For direct API use:

Two engine families per direction:

- **`command`** (local-first default): Whisper-family STT and Piper-family TTS as founder-configured argv arrays run without a shell; text to speak reaches TTS via stdin, never a command line.
- **`deepgram`** (STT) and **`elevenlabs`** (TTS): the founder's cloud speech APIs. Selecting a cloud engine in `config.toml` is an explicit standing grant — audio and reply text leave the machine. API keys are read from environment variables (`DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY`) and are never stored in config files or the database.

The contract:

- audio travels as base64 JSON; recordings and transcripts are stored under the local data dir for the audit trail regardless of engine
- unconfigured engines and missing API keys report unavailable (`503`) naming exactly what to set; cloud HTTP failures return `400` with the status code
- a TTS failure still returns the text answer with a `speech_error` note
- by default the spoken reply omits the appended `Contrarian check` block (pass `speak_full = true` to hear it)

Cloud engine setup (Deepgram transcription + ElevenLabs voice):

```toml
[voice]
stt_engine = "deepgram"
tts_engine = "elevenlabs"
tts_voice = "21m00Tcm4TlvDq8ikWAM"   # any ElevenLabs voice id
```

```powershell
[Environment]::SetEnvironmentVariable("DEEPGRAM_API_KEY", "your-key", "User")
[Environment]::SetEnvironmentVariable("ELEVENLABS_API_KEY", "your-key", "User")
```

Local engine setup (see `config.example.toml` for whisper.cpp and piper examples):

```toml
[voice]
stt_command = ["C:\\tools\\whisper\\whisper-cli.exe", "-m", "C:\\models\\ggml-base.en.bin", "-f", "{audio}", "-otxt", "-of", "{transcript_base}"]
tts_command = ["C:\\tools\\piper\\piper.exe", "--model", "C:\\models\\en_US-lessac-medium.onnx", "--output_file", "{output}"]
```

Check engine readiness:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8787/voice/status"
```

Hold a governed voice exchange (wav in, wav out, with thread continuity):

```powershell
$audio = [Convert]::ToBase64String([IO.File]::ReadAllBytes("C:\AI Brain\inbox\question.wav"))
$body = @{ audio_base64 = $audio; conversation_id = 1 } | ConvertTo-Json

$reply = Invoke-RestMethod -Uri "http://127.0.0.1:8787/voice/converse" -Method Post -Body $body -ContentType "application/json"
[IO.File]::WriteAllBytes("C:\AI Brain\inbox\reply.wav", [Convert]::FromBase64String($reply.speech.audio_base64))
$reply.transcript
$reply.response
```

Model roles:

```text
general   -> qwen3:14b
reasoning -> deepseek-r1:14b with thinking enabled by default
coding    -> qwen2.5-coder:14b
embedding -> nomic-embed-text
```

Example role-routed chat:

```powershell
$body = @{ message = "Review this function for bugs"; task_type = "coding" } | ConvertTo-Json
Invoke-RestMethod -Uri "http://127.0.0.1:8787/chat" -Method Post -Body $body -ContentType "application/json"
```

## Runtime Identity Charter

Zade has a durable runtime identity charter stored in SQLite. The charter is not just a note: it is loaded into chat prompts, exposed in self-inventory, audited on update, and reflected into the founder operating layer.

The charter translates character-inspired traits into safe co-founder behavior:

- relentless purpose becomes long-horizon focus
- intimidation becomes calm executive presence and pressure-tested reasoning
- violence becomes decisive non-harmful action, never threats or physical harm
- protective loyalty becomes privacy, boundary defense, and user-aligned advocacy within the authority policy

Inspect the active charter:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8787/identity/charter"
```

Seed or replace the charter:

```powershell
$charter = @{
  name = "Zade"
  source = "local"
  mission = "Advance the founder mission with discipline, evidence, and local-first memory."
  guiding_principles = @(
    @{ name = "Mission above comfort"; rule = "Evaluate decisions against long-term objectives." },
    @{ name = "Strategic patience"; rule = "Gather information before decisive action." }
  )
  cognitive_style = @("systems thinking", "pattern recognition", "long time horizons")
  communication_style = @("concise", "direct", "dry", "confident")
  decision_framework = @("Gather information.", "Identify leverage.", "Minimize unnecessary risk.", "Commit fully once reality is clear.", "Adapt if reality changes.")
  safety_translation = @{
    violence = "decisive non-harmful action, never threats or physical harm"
    intimidation = "calm executive presence and pressure-tested reasoning"
  }
} | ConvertTo-Json -Depth 8

Invoke-RestMethod -Uri "http://127.0.0.1:8787/identity/charter" -Method Post -Body $charter -ContentType "application/json"
```

Relationship charters store protected-principal context separately from Zade's global identity. They can express that a person matters deeply while keeping autonomy, consent, privacy, and non-coercion as hard boundaries.

The Ellie-style translation is:

- possessiveness becomes enduring commitment without ownership
- obsession becomes attentive care only through consented context
- protection becomes risk awareness and support, not unauthorized intervention
- loyalty becomes consistency and privacy-respecting advocacy
- fear of loss never authorizes surveillance, coercive control, harassment, or harm

Inspect relationship charters:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8787/identity/relationships"
Invoke-RestMethod -Uri "http://127.0.0.1:8787/identity/relationships/Ellie"
```

Voice charters store Zade's operating voice separately from identity and relationship context. The voice can be terse, concrete, controlled, and decisive, but it does not override truthfulness or safety.

The safe voice translation is:

- certainty becomes clean, direct language without fake confidence
- commands become task directives, not coercion
- threats become calm boundary statements and lawful next steps
- violent imagery becomes operational urgency without violent language
- dry humor stays sparse and never becomes cruelty or harassment

Inspect or replace the active voice charter:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8787/identity/voice"
```

## Runtime Governor

The runtime layer turns Zade's seeded charters into an operating contract:

- assemble identity, relationship, voice, authority, founder, memory, and queue context
- apply authority before implying action
- preserve decisive voice without false certainty
- keep the voice charter read-only unless updated through `/identity/voice`
- log governed responses and operating-loop runs to `runtime_events`

Inspect the active charter stack:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8787/runtime/charter-stack"
```

Inspect current runtime context:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8787/runtime/context?message=What%20matters%20next"
```

Use the governed response endpoint:

```powershell
$body = @{
  message = "What should we do next?"
  proposed_action = "runtime.respond"
  permission_tier = "L0_READ"
  use_semantic_memory = $true
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://127.0.0.1:8787/runtime/respond" -Method Post -Body $body -ContentType "application/json"
```

### Automatic Contrarian Pass

Recommendation-shaped questions get an automatic red-team pass before the answer reaches you. A deterministic heuristic on the message (phrases like "should we", "recommend", "prioritize", "which option") triggers a second call through the reasoning model (`deepseek-r1` with thinking enabled) that attacks the draft: weakest assumption, missing evidence, downside risk, and a verdict with a confidence adjustment.

The pass is non-blocking pushback:

- the challenge is attached visibly under a `Contrarian check` section; the draft is never silently rewritten
- every pass persists as a contrarian review (`subject_type = runtime_event`) in the founder operating layer
- if the reasoning model is unavailable, the response returns unchallenged with a governor note
- if the critique cannot be parsed, the raw critique text is attached instead of being dropped

Control it per request with the `contrarian` flag (`$true` forces the pass, `$false` suppresses it, omit for auto-detection):

```powershell
$body = @{
  message = "Summarize the current memory state."
  contrarian = $true
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://127.0.0.1:8787/runtime/respond" -Method Post -Body $body -ContentType "application/json"
```

Run the local operating loop:

```powershell
$body = @{
  run_autonomous = $true
  max_run = 5
  review_type = "daily"
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://127.0.0.1:8787/runtime/operating-loop" -Method Post -Body $body -ContentType "application/json"
Invoke-RestMethod -Uri "http://127.0.0.1:8787/runtime/events"
```

Run the full local cadence loop:

```powershell
$body = @{
  run_autonomous = $true
  max_run = 5
  review_type = "daily"
  import_candidates = $true
  max_import = 5
  experiment_review_type = "weekly"
  max_experiment_reviews = 10
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://127.0.0.1:8787/runtime/cadence" -Method Post -Body $body -ContentType "application/json"
```

## Local Ops

Start Zade and open the UI:

```powershell
C:\LocalAICofounder\Start-Zade.cmd
```

Run the smoke contract:

```powershell
C:\LocalAICofounder\Run-Zade-Smoke.cmd
```

Create a database backup:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\LocalAICofounder\scripts\backup.ps1 -Label manual
```

Benchmark local model roles:

```powershell
C:\LocalAICofounder\Run-Zade-Benchmark.cmd
```

Preview backup retention:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\LocalAICofounder\scripts\prune-backups.ps1 -KeepLast 10
```

Commit backup retention:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\LocalAICofounder\scripts\prune-backups.ps1 -KeepLast 10 -Commit
```

Check local health:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\LocalAICofounder\scripts\monitor-health.ps1
```

Installed local automation:

- `Zade Local Supervisor` keeps the kernel resident: starts it at logon and restarts it if the health check fails.
- `Zade Local Cadence` runs the operating, evidence, and experiment loop.
- `Zade Local Health Monitor` checks kernel/UI/Ollama posture hourly.
- `C:\Users\TiBon\Desktop\Zade.lnk` starts the kernel and opens `/ui`.

## Always-On Supervisor

The supervisor turns Zade from "runs when started" into a resident service. A scheduled task runs `scripts\supervise.ps1` at logon and every few minutes: if the kernel answers `/health` it logs a heartbeat and exits; if not, it starts the kernel via `start.ps1`, re-verifies health, and records what happened.

Supervision history lives in a JSONL log the supervisor owns (`<data_dir>\supervision\supervisor-log.jsonl`, auto-trimmed) — the kernel cannot receive a report while it is down, so the kernel only reads that history:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8787/ops/supervision"
```

`/health` also reports `uptime_seconds` so the supervisor and founder can see restarts at a glance.

Install the supervisor task (at logon + every 5 minutes):

```powershell
.\scripts\install-supervisor-task.ps1 -IntervalMinutes 5
```

Run one supervision pass manually (or double-click `Run-Zade-Supervise.cmd`):

```powershell
.\scripts\supervise.ps1
```

Check without starting (useful for probes):

```powershell
.\scripts\supervise.ps1 -CheckOnly
```

Remove the task:

```powershell
.\scripts\uninstall-supervisor-task.ps1
```

## Deep Thought Teaching Bridge

Zade can learn from Deep Thought, but only as sourced evidence. The bridge preserves the entity boundary:

```text
Deep Thought says X -> Zade records X as evidence with source metadata.
```

It does not treat Deep Thought memory as native Zade certainty.

Default source priority:

```text
C:\AI Brain\Deep Thought\architecture\deep-thought-standing-brief.md
C:\AI Brain\Deep Thought\architecture\deep-thought-cofounder.md
C:\AI Brain\Deep Thought\architecture\deep-thought-operating-guide.md
C:\AI Brain\Deep Thought\architecture\deep-thought-memory-architecture.md
C:\AI Brain\Deep Thought\context.md
C:\AI Brain\Deep Thought\core-knowledge.md
C:\AI Brain\Deep Thought\session-state.md
C:\AI Brain\Deep Thought\memory
C:\AI Brain\Deep Thought\architecture
C:\DeepThought\docs
C:\DeepThought\README.md
C:\DeepThought\HANDOFF.md
C:\DeepThought\PRD.md
```

Scan for teaching candidates:

```powershell
$body = @{ limit = 25 } | ConvertTo-Json
Invoke-RestMethod -Uri "http://127.0.0.1:8787/teach/deepthought/scan" -Method Post -Body $body -ContentType "application/json"
Invoke-RestMethod -Uri "http://127.0.0.1:8787/teach/deepthought/candidates"
```

Import selected candidates as semantic documents and structured evidence:

```powershell
$body = @{
  candidate_ids = @(1)
  ingest_documents = $true
  create_evidence = $true
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://127.0.0.1:8787/teach/deepthought/import" -Method Post -Body $body -ContentType "application/json"
```

Link imported evidence to a Zade operating object:

```powershell
$body = @{
  evidence_id = 1
  to_type = "goal"
  to_id = 1
  relation = "supports"
  strength = 70
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://127.0.0.1:8787/teach/deepthought/link" -Method Post -Body $body -ContentType "application/json"
```

Auto-link imported candidates using their scanned suggestions:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8787/teach/deepthought/auto-link?limit=50" -Method Post
```

Run the evidence loop:

```powershell
$body = @{
  import_candidates = $true
  max_import = 5
  link_goals = $true
  clear_resolved_warnings = $true
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://127.0.0.1:8787/runtime/evidence-loop" -Method Post -Body $body -ContentType "application/json"
Invoke-RestMethod -Uri "http://127.0.0.1:8787/evidence/gaps"
```

Reliability defaults:

```text
A = verified runtime or validation output
B = standing brief or explicit decision record
C = architecture, handoff, or PRD source
D = general note or unverified draft
F = contradicted or obsolete source
```

## Experiment + Evidence Loop

Experiments turn knowledge gaps into proof loops. Each experiment can test assumptions, bets, goals, and predictions. Evidence lands in the shared `founder_evidence` ledger and is linked back to the operating objects it informs.

Create an experiment:

```powershell
$body = @{
  title = "Manual Object Habit Test"
  experiment_type = "retention"
  hypothesis = "Founders will maintain operating objects manually before integrations."
  success_metric = "founders completing two weekly reviews"
  success_threshold = "3 of 5"
  minimum_evidence = 2
  decision_rule = "Continue if at least 3 founders complete two reviews; revise otherwise."
  linked_assumption_ids = @(1)
  linked_bet_ids = @(3)
  linked_goal_ids = @(1)
} | ConvertTo-Json -Depth 8

Invoke-RestMethod -Uri "http://127.0.0.1:8787/experiments" -Method Post -Body $body -ContentType "application/json"
```

On a fresh database, `/experiments/dashboard` seeds `EXP-001 - Founder Evidence Intake` so the UI has a real first proof loop for fast evidence capture.

Add interview, metric, file, CSV, screenshot-reference, or trial evidence:

```powershell
$body = @{
  evidence_type = "founder_interview"
  source = "interview:founder-001"
  content = "Founder said manual objects are acceptable if weekly review creates sharper decisions."
  metrics = @{ manual_objects_created = 6; weekly_review_completed = $true }
  reliability = "C"
  strength = 70
  linked_assumption_id = 1
} | ConvertTo-Json -Depth 8

Invoke-RestMethod -Uri "http://127.0.0.1:8787/experiments/1/evidence" -Method Post -Body $body -ContentType "application/json"
```

Force a review outcome:

```powershell
$body = @{
  review_type = "weekly"
  decision = "revise" # continue, revise, kill, or escalate
  outcome_summary = "Evidence exists, but sample size is still thin."
  next_actions = @("Collect four more founder trials.")
} | ConvertTo-Json -Depth 8

Invoke-RestMethod -Uri "http://127.0.0.1:8787/experiments/1/review" -Method Post -Body $body -ContentType "application/json"
```

Log Zade pushback without blocking execution:

```powershell
$body = @{
  objection = "One interview is not enough to trust manual-object retention."
  risk = "We may confuse founder curiosity with durable habit."
  recommendation = "proceed_with_changes"
} | ConvertTo-Json -Depth 8

Invoke-RestMethod -Uri "http://127.0.0.1:8787/experiments/1/pushback" -Method Post -Body $body -ContentType "application/json"
```

Run the local experiment loop:

```powershell
$body = @{ review_type = "weekly"; max_reviews = 10 } | ConvertTo-Json
Invoke-RestMethod -Uri "http://127.0.0.1:8787/runtime/experiment-loop" -Method Post -Body $body -ContentType "application/json"
Invoke-RestMethod -Uri "http://127.0.0.1:8787/experiments/dashboard"
```

## Local Ingestion

Supported first-pass text-like formats:

```text
.csv .json .jsonl .log .md .ps1 .py .toml .txt .yaml .yml
```

Ingest raw text:

```powershell
$body = @{
  title = "Operating Note"
  text = "Every local memory write should leave an audit trail."
  source = "manual"
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://127.0.0.1:8787/ingest/text" -Method Post -Body $body -ContentType "application/json"
```

Ingest one file from the hot inbox:

```powershell
$body = @{ path = "C:\AI Brain\inbox\note.md" } | ConvertTo-Json
Invoke-RestMethod -Uri "http://127.0.0.1:8787/ingest/file" -Method Post -Body $body -ContentType "application/json"
```

Ingest a folder:

```powershell
$body = @{ path = "C:\AI Brain\inbox"; recursive = $true } | ConvertTo-Json
Invoke-RestMethod -Uri "http://127.0.0.1:8787/ingest/folder" -Method Post -Body $body -ContentType "application/json"
```

Semantic search:

```powershell
$body = @{ query = "audit trails for local memory"; limit = 5 } | ConvertTo-Json
Invoke-RestMethod -Uri "http://127.0.0.1:8787/memory/semantic-search" -Method Post -Body $body -ContentType "application/json"
```

File ingestion copies originals into `D:\AI Brain-Cold\raw-ingest` by content hash. The hot SQLite database stores document records, chunks, embeddings, and job/audit history.

### Hybrid Retrieval

Semantic search defaults to hybrid retrieval: an FTS5 BM25 keyword ranking and an embedding cosine ranking are fused with reciprocal rank fusion, so exact-term matches and meaning-only matches both surface. Each hit carries a `retrieval` block with `vector_rank`, `keyword_rank`, and `rrf_score`.

Pick a mode explicitly with `mode` (`hybrid`, `vector`, or `keyword`):

```powershell
$body = @{ query = "audit trail monitoring"; limit = 5; mode = "hybrid" } | ConvertTo-Json
Invoke-RestMethod -Uri "http://127.0.0.1:8787/memory/semantic-search" -Method Post -Body $body -ContentType "application/json"
```

If the embedding model is unavailable, hybrid degrades to keyword-only (marked `degraded_to_keyword`) instead of failing; explicit `vector` mode still surfaces the failure.

Skill routing is hybrid too: `/skills/scan` embeds each skill (re-embedding only when content changes), and `/skills/route` blends keyword scoring with embedding similarity so a query can reach a skill it shares no keywords with. Keyword routing keeps working when embeddings are unavailable, and a failed embedder is backed off for 60 seconds instead of stalling every governed response.

## Autonomous Work Queue

Zade keeps a durable local work queue in SQLite. The queue records what was prepared, what ran, what was skipped as a duplicate, what requires approval, and what was denied.

Current autonomous handlers:

- `brief.daily.prepare` writes a local daily brief memory.
- `self.inventory.snapshot` writes a local runtime posture snapshot.
- `ingest.file` imports supported text-like inbox files into semantic memory.
- `goal.review` writes a local review note for due active goals.

Scan for local work and run allowed items:

```powershell
$body = @{ run_autonomous = $true; max_run = 5 } | ConvertTo-Json
Invoke-RestMethod -Uri "http://127.0.0.1:8787/work/scan" -Method Post -Body $body -ContentType "application/json"
```

Inspect the queue:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8787/work/queue"
```

Queue a proposed action:

```powershell
$body = @{
  kind = "external"
  title = "Send follow-up email"
  action = "email.send"
  target = "founder@example.com"
  permission_tier = "L3_EXTERNAL_ACTION"
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://127.0.0.1:8787/work/items" -Method Post -Body $body -ContentType "application/json"
```

The authority engine assigns `pending`, `approval_required`, or `denied` at queue time. Only `pending` items can run through `/work/run-next` or `/work/run-due`.

Approval-required items also create durable `approval_requests` records. Approval marks the work item `approved` and records the decision. Dispatch only happens through registered local handlers.

Registered approval dispatch handlers:

- `local.noop`
- `local.audit.record`
- `local.memory.write`
- `local.file.write`
- `local.report.write`
- `local.vault.organize`
- `local.browser.open`

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8787/approval-requests"
Invoke-RestMethod -Uri "http://127.0.0.1:8787/action-handlers"
Invoke-RestMethod -Uri "http://127.0.0.1:8787/work/items/4/approve" -Method Post
Invoke-RestMethod -Uri "http://127.0.0.1:8787/work/items/4/deny" -Method Post
```

Approve and dispatch a registered local handler:

```powershell
$body = @{
  resolved_by = "founder"
  note = "Approved."
  dispatch = $true
  typed_confirmation = "make the jump to hyperspace"
} | ConvertTo-Json
Invoke-RestMethod -Uri "http://127.0.0.1:8787/work/items/4/approve" -Method Post -Body $body -ContentType "application/json"
```

Approval records authorization. Dispatch is separate authority. It only runs registered local handlers and requires the typed confirmation phrase.

## Founder Operating Layer

Zade is meant to behave like a co-founder, not a task assistant. The founder layer stores durable business context and produces an executive dashboard and brief from it.

Artifacts:

- living company thesis
- strategy ledger
- initiatives with measurable success criteria
- decision memos with options, counterarguments, and revisit dates
- predictions with calibration/error tracking
- contrarian reviews with founder-specific roles
- reflections that update beliefs, strategy, predictions, and priorities
- mental model library
- founder dashboard and founder brief
- assumptions with explicit confidence and invalidation signals
- evidence with reliability grades A/B/C/D/F
- object links across assumptions, evidence, decisions, goals, bets, predictions, and outcomes
- strategy objects: active bets, rejected paths, constraints, and open questions
- goals and tasks separated from initiatives
- kill criteria
- founder overrides
- thesis conflicts
- missed-call reviews
- cadence reviews and integrity warnings

Create or update the company thesis:

```powershell
$body = @{
  vision = "A private AI co-founder compounds founder context over years."
  mission = "Build Zade into a durable operating partner."
  why_now = "Local models, cheap storage, and private memory are good enough."
  customer = "Founder-operators building complex systems."
  unfair_advantages = @("local exo-brain", "operator context")
  core_assumptions = @(
    @{ assumption = "Private longitudinal memory improves founder decisions"; confidence = 70; evidence = @() }
  )
  unknown_unknowns = @("distribution wedge")
  status = "active"
} | ConvertTo-Json -Depth 8

Invoke-RestMethod -Uri "http://127.0.0.1:8787/founder/thesis" -Method Post -Body $body -ContentType "application/json"
```

Create an initiative:

```powershell
$body = @{
  objective = "Ship founder operating layer"
  why_it_matters = "Zade needs institutional business memory, not just chat memory."
  priority = 95
  success_criteria = @("Dashboard exists", "Predictions can be scored", "Contrarian review is stored")
  confidence = 75
  current_risk = "medium"
} | ConvertTo-Json -Depth 8

Invoke-RestMethod -Uri "http://127.0.0.1:8787/founder/initiatives" -Method Post -Body $body -ContentType "application/json"
```

Create and score a prediction:

```powershell
$body = @{
  prediction = "Founder artifacts will improve Zade's next-step recommendations."
  probability = 0.75
  time_horizon = "2 weeks"
} | ConvertTo-Json -Depth 8

$prediction = Invoke-RestMethod -Uri "http://127.0.0.1:8787/founder/predictions" -Method Post -Body $body -ContentType "application/json"

$score = @{
  prediction_id = $prediction.id
  outcome = "true"
  lessons = "Durable artifacts made the recommended focus concrete."
} | ConvertTo-Json -Depth 8

Invoke-RestMethod -Uri "http://127.0.0.1:8787/founder/predictions/score" -Method Post -Body $score -ContentType "application/json"
```

Read the executive surfaces:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8787/founder/dashboard"
Invoke-RestMethod -Uri "http://127.0.0.1:8787/founder/metrics"
Invoke-RestMethod -Uri "http://127.0.0.1:8787/founder/brief"
```

Contrarian review roles include red team, skeptic, historian, economist, customer, engineer, investor, competitor, and future founder. The review summary stores top risks, blind spots, confidence adjustment, and proceed/change/kill recommendation.

### Operating Objects v2

The v2 layer turns gaps into concrete state:

```text
POST /founder/assumptions
POST /founder/evidence
POST /founder/links
POST /founder/strategy-objects
POST /founder/goals
POST /founder/tasks
POST /founder/kill-criteria
POST /founder/overrides
POST /founder/thesis-conflicts
POST /founder/missed-calls
POST /founder/integrity-check
POST /founder/cadence-reviews
POST /founder/cadence-reviews/generate/{daily|weekly|monthly}
GET  /founder/confidence-events
GET  /founder/integrity-warnings
```

Create an assumption and attach contradicting evidence:

```powershell
$assumption = @{
  statement = "Solo founders will pay $99/month for Zade."
  category = "pricing"
  confidence = 70
  invalidation_signal = "Founders only show willingness around $29/month."
} | ConvertTo-Json -Depth 8

$a = Invoke-RestMethod -Uri "http://127.0.0.1:8787/founder/assumptions" -Method Post -Body $assumption -ContentType "application/json"

$evidence = @{
  evidence_type = "customer interview"
  source = "five solo founder calls"
  reliability = "C"
  claim_contradicted = "Willingness to pay clusters around $29/month unless revenue is directly saved."
  strength = 80
  linked_assumption_id = $a.id
} | ConvertTo-Json -Depth 8

Invoke-RestMethod -Uri "http://127.0.0.1:8787/founder/evidence" -Method Post -Body $evidence -ContentType "application/json"
```

That flow will:

- link evidence to the assumption
- create a confidence event
- reduce assumption confidence deterministically based on evidence grade/strength
- create a thesis conflict if the evidence contradicts a claim

Create an active bet with a reversal trigger and kill criteria:

```powershell
$bet = @{
  object_type = "active_bet"
  title = "Start with solo founders instead of teams."
  confidence = 68
  reversal_trigger = "Fewer than 20% of trial users activate weekly founder review within 14 days."
  details = @{
    upside = "Sharper pain and faster sales cycle"
    downside = "Lower ACV and higher churn risk"
  }
} | ConvertTo-Json -Depth 8

$b = Invoke-RestMethod -Uri "http://127.0.0.1:8787/founder/strategy-objects" -Method Post -Body $bet -ContentType "application/json"

$kill = @{
  subject_type = "bet"
  subject_id = $b.id
  metric = "weekly founder review activation"
  threshold = "< 20%"
  by_date = "2026-08-01"
} | ConvertTo-Json -Depth 8

Invoke-RestMethod -Uri "http://127.0.0.1:8787/founder/kill-criteria" -Method Post -Body $kill -ContentType "application/json"
```

Run operating pressure checks:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8787/founder/integrity-check" -Method Post
Invoke-RestMethod -Uri "http://127.0.0.1:8787/founder/cadence-reviews/generate/daily" -Method Post
Invoke-RestMethod -Uri "http://127.0.0.1:8787/founder/dashboard"
```

## Authority Policy

The kernel exposes a machine-readable authority contract so the assistant can decide what it may do without asking first.

Decisions derive from a structured action taxonomy, not free-text keyword sniffing (policy `v2`):

1. deny taxonomy on the action — tokenized capability matching (`broker`, `trading`, `payment`, `wire`, `exfiltrate`, ...) plus high-signal phrases
2. known-local action allow for L0/L1 tiers (`memory.*`, `ingest.*`, `runtime.*`, `founder.*`, ...)
3. deny phrases on the target for unrecognized actions (`rm -rf`, `disable defender`, ...)
4. external approval taxonomy on the action (`email`, `browser`, `shell`, `deploy`, `github`, ...)
5. tier defaults

Two properties this guarantees:

- **Request metadata never changes a decision.** Payload content (notes, file contents, titles) can neither trip the safety boundary ("reviewed the purchase order" no longer denies a local report) nor evade it — the action string is authoritative.
- **A claimed tier cannot bypass the deny boundary.** Deny screening runs before any tier-based allow, so `broker.place_order` is denied even when it claims `L0_READ`, and `email.send` at `L0_READ` still requires approval.

Autonomous by default:

- local Ollama chat/reasoning/coding calls
- local memory search and semantic search
- local audit and daily brief reads
- local memory writes
- text/file/folder ingestion under configured memory roots
- cold archive copies for ingested files
- daily brief, self-inventory snapshot, inbox ingestion, and goal-review queue items

Approval required:

- generic file edits or writes
- shell commands and process control
- installing software or changing services
- browser, email, calendar, messaging, GitHub, deployment, or other external actions
- network/API calls outside the configured local Ollama endpoint

Denied until the boundary is deliberately redesigned:

- live trading, broker mutation, order placement, or account-risk changes
- credential, token, password, or secret exfiltration
- destructive disk, vault, registry, system, or security-control changes
- payments, transfers, purchases, or irreversible external commitments

Inspect the active rules:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8787/authority"
Invoke-RestMethod -Uri "http://127.0.0.1:8787/self-inventory"
```

Ask the policy engine about a proposed action:

```powershell
$body = @{
  action = "memory.write"
  permission_tier = "L1_MEMORY_WRITE"
  target = "memories"
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://127.0.0.1:8787/authority/evaluate" -Method Post -Body $body -ContentType "application/json"
```

## Current Scope

Implemented:

- local config and path policy
- Zade identity config
- runtime identity charter with prompt injection and safe character-trait translation
- relationship charters with prompt injection and consent-respecting safety boundaries
- voice charter with prompt injection, decisive style, and evidence-honest safety controls
- runtime governor with charter-stack context, authority gating, operating loop, and runtime event log
- episodic conversation memory with durable threads, turn history, prompt continuity, and bounded rolling summaries
- proactive surfacing layer with deterministic attention scanning, ranked initiated briefs, since-last-brief deltas, and cadence integration
- automatic contrarian pass that red-teams recommendation-shaped responses through the reasoning model, attaches the challenge visibly, and persists it as a contrarian review
- eval harness with a golden founder-scenario set, deterministic graders, persisted runs, and regression comparison across model/prompt changes
- hybrid retrieval with keyword+vector reciprocal rank fusion, per-hit ranking provenance, keyword degradation when embeddings are down, and embedding-based skill routing with failure backoff
- structured authority evaluation (policy v2) with action-token deny taxonomy, deny-before-allow ordering, and metadata excluded from the decision surface
- read-only external connectors (IMAP email, ICS calendar) with approval-gated sync, env-referenced credentials, staged candidates, and graded evidence import
- local voice loop with founder-configured STT/TTS engines, governed voice conversations with episodic memory, and honest engine-unavailable reporting
- always-on supervisor with a resident scheduled task, crash recovery through start.ps1, a supervisor-owned JSONL history, and kernel uptime/supervision reporting
- decision-to-action pipeline with per-step authority, work-queue execution for machine steps, step-level evidence trails, and stalled-plan surfacing
- developer action handlers (run tests/lint, git branch/commit, draft messages) on the approved-dispatch substrate, with command allowlisting, workspace confinement, and default-branch protection
- commitment ledger tracking founder and Zade promises with deadlines, misses, drift detection, throttled follow-ups, and cadence integration
- notification bus with ui/voice/sms channels, severity floors, quiet hours, rate limits, recipient whitelists, dedupe, and recorded suppressions
- Deep Thought teaching bridge with sourced candidates, evidence import, object linking, and evidence loop
- Deep Thought imported-candidate auto-link pass
- experiment + evidence loop with experiment objects, evidence intake, object linking, forced reviews, runtime loop, and non-blocking pushback
- local authority policy and self-inventory
- authority-gated autonomous work queue
- optional local mutation token guard
- approval request ledger with approve/deny endpoints and typed-confirmed local dispatch
- approved local handlers for audit, memory, file writes, reports, vault organization plans, and local browser opens
- static local UI served from `/ui`
- browser-smokeable UI contract for tabs, chat, queue actions, and experiment evidence intake
- local startup script that verifies health and opens `/ui`
- local cadence endpoint and Windows scheduled-task scripts
- founder operating layer with thesis, strategy, initiatives, decisions, predictions, reviews, reflections, dashboard, and brief
- founder operating layer v2 with assumptions, evidence, links, strategy objects, goals, tasks, kill criteria, overrides, confidence events, thesis conflicts, missed-call reviews, cadence reviews, and integrity warnings
- founder metrics read model
- model-role benchmark endpoint and operator script
- backup retention endpoint and operator script
- SQLite schema and migrations
- audit events for tool/model actions
- memory write/search tools
- local text/file/folder ingestion
- document chunks and local embedding records
- semantic search over local document chunks
- daily brief from local memory/goals/decisions
- Ollama generate and health adapter
- FastAPI routes
- tests for config, database, tools, autonomy, founder artifacts, and API health

Deferred:

- outbound or mutating external actions (sending email, writing calendars, slack)
- cloud model fallback
- external browser automation
- trading-bot authority
- multi-agent swarm orchestration
