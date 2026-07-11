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
http://127.0.0.1:8787/health
```

Stop the server with:

```powershell
.\scripts\stop.ps1
```

Check status with:

```powershell
.\scripts\status.ps1
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
GET  /founder/mental-models
GET  /founder/thesis
POST /founder/thesis
GET  /founder/dashboard
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
GET  /audit/recent
GET  /models
GET  /ingest/jobs
POST /memory
POST /memory/search
POST /memory/semantic-search
POST /ingest/text
POST /ingest/file
POST /ingest/folder
GET  /brief/daily
POST /chat
```

`/chat` calls Ollama with `think=false` by default so Qwen3 does not spend the response budget in the hidden thinking field.

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
- local authority policy and self-inventory
- authority-gated autonomous work queue
- founder operating layer with thesis, strategy, initiatives, decisions, predictions, reviews, reflections, dashboard, and brief
- founder operating layer v2 with assumptions, evidence, links, strategy objects, goals, tasks, kill criteria, overrides, confidence events, thesis conflicts, missed-call reviews, cadence reviews, and integrity warnings
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

- external APIs
- cloud model fallback
- browser automation
- email/calendar/slack
- trading-bot authority
- multi-agent swarm orchestration
- perpetual background scheduler
