# Token-Budgeted Build Delegation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Zade a local-first SaaS/mobile build workflow that assesses complexity locally, obtains one founder-approved cloud lease, and enforces hard token, cost, turn, and time ceilings around selective Anthropic coding assistance.

**Architecture:** A deterministic assessment and local Ollama interpretation produce a tier recommendation. Durable build-session and append-only usage records back an atomic budget module. A router keeps routine work local and creates a session-bound Anthropic model adapter only for eligible work, while the existing coding loop retains workspace confinement, tools, and verification.

**Tech Stack:** Python 3.11+, FastAPI, SQLite, Ollama, Anthropic Python SDK 0.117.x, pytest, vanilla HTML/CSS/JavaScript.

## Global Constraints

- No active approved lease means no paid build request.
- Default tiers are `SMALL` ($1, 120,000 input, 16,000 output, 6 turns, 2 hours), `MEDIUM` ($3, 400,000 input, 40,000 output, 16 turns, 4 hours), and `LARGE` ($7, 1,000,000 input, 80,000 output, 32 turns, 8 hours).
- Deterministic evidence establishes the assessment floor; local-model reasoning may raise but never lower it.
- Local repository discovery, context selection, command execution, and verification occur before or between cloud turns.
- Every cloud request reserves an authorizing input upper bound and maximum output cost atomically before network transmission.
- Missing usage settles conservatively; ambiguous post-send failures retain the reservation and pause cloud use.
- No automatic paid retry, provider fallback, or lease-tier escalation exists.
- Pricing uses local model-specific snapshots with a `review_after` date; missing or stale pricing disables paid calls.
- Raw founder state, credentials, unrelated memory, and unselected source remain forbidden from cloud egress.
- Automated tests use fake provider clients and sentinel keys and make zero paid API calls.
- Existing local builds, strategic reviews, provider-policy checks, work approvals, and offline acceptance must remain compatible.

---

## File Map

- Create `src/cofounder_kernel/build_types.py`: shared tiers, limits, assessments, sessions, leases, reservations, usage, and pricing value objects.
- Create `src/cofounder_kernel/build_assessment.py`: deterministic repository scanner plus optional local Ollama risk adjustment.
- Create `src/cofounder_kernel/build_store.py`: focused SQLite persistence for build assessments, sessions, leases, reservations, and usage events.
- Create `src/cofounder_kernel/build_budget.py`: pricing, atomic reservation, settlement, warning, exhaustion, and upgrade accounting.
- Create `src/cofounder_kernel/build_routing.py`: local/cloud routing decisions and focused cloud context selection.
- Create `src/cofounder_kernel/model_client.py`: provider-neutral chat-client protocol and error type for the existing coding loop.
- Create `src/cofounder_kernel/anthropic_build.py`: session-bound Anthropic streaming, tool conversion, prompt caching, token counting, and usage mapping.
- Create `src/cofounder_kernel/build_service.py`: build-session lifecycle, lease approval, local/cloud execution, egress, checkpoints, and upgrade requests.
- Modify `src/cofounder_kernel/config.py`: build policy, tier, and pricing configuration.
- Modify `src/cofounder_kernel/db.py`: schema version and four build tables.
- Modify `src/cofounder_kernel/coding_agent.py`: use a provider-neutral model client while preserving the local default and existing tools.
- Modify `src/cofounder_kernel/delegation.py`: add the `hybrid` engine and route its work items through `BuildService`.
- Modify `src/cofounder_kernel/egress.py`: issue and audit lease-scoped egress authorization without weakening one-shot grants.
- Modify `src/cofounder_kernel/models.py`: build assessment, approval, denial, and run request models.
- Modify `src/cofounder_kernel/api.py`: construct the new modules and expose build-session/status routes.
- Modify `src/cofounder_kernel/anthropic_client.py`: retain strategic review while sharing SDK client construction and policy checks.
- Modify `config.toml` and `config.example.toml`: select `hybrid` locally and document tier/pricing settings.
- Modify `pyproject.toml` and `uv.lock`: add the optional `cloud` dependency `anthropic>=0.117,<0.118`.
- Modify `ui/swarm.html`: assessment, lease, usage, and upgrade visibility in the existing Delegation panel.
- Create focused tests under `tests/test_build_*.py` and extend existing config, coding-agent, delegation, egress, API, offline, and strategy-review tests.

---

### Task 1: Build Policy Types And Configuration

**Files:**
- Create: `src/cofounder_kernel/build_types.py`
- Modify: `src/cofounder_kernel/config.py`
- Modify: `config.example.toml`
- Test: `tests/test_build_config.py`

**Interfaces:**
- Produces: `BuildTier`, `LeaseLimits`, `PricingSnapshot`, `BuildConfig`, `BuildTierConfig`, and `AnthropicPricingConfig`.
- Consumes: existing `load_config()` TOML/env parsing patterns.

- [ ] **Step 1: Write failing value-object and configuration tests**

```python
def test_default_build_tiers_are_monotonic():
    cfg = BuildConfig()
    assert cfg.limits(BuildTier.SMALL) == LeaseLimits(1_000_000, 120_000, 16_000, 6, 7200)
    assert cfg.limits(BuildTier.MEDIUM).input_tokens == 400_000
    assert cfg.limits(BuildTier.LARGE).dollar_micro == 7_000_000

def test_stale_pricing_is_not_authorizable():
    pricing = AnthropicPricingConfig(review_after="2026-01-01")
    assert pricing.is_current(at="2026-07-18T00:00:00Z") is False

def test_load_config_rejects_non_monotonic_tiers(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[build.tiers.small]\ninput_tokens=500000\n[build.tiers.medium]\ninput_tokens=100000\n")
    with pytest.raises(ValueError, match="monotonic"):
        load_config(path)
```

- [ ] **Step 2: Run tests and confirm missing-module failure**

Run: `C:\LocalAICofounder\.venv\Scripts\python.exe -m pytest tests/test_build_config.py -q`

Expected: FAIL during collection because `cofounder_kernel.build_types` does not exist.

- [ ] **Step 3: Implement immutable policy value objects**

```python
class BuildTier(StrEnum):
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"

@dataclass(frozen=True)
class LeaseLimits:
    dollar_micro: int
    input_tokens: int
    output_tokens: int
    cloud_turns: int
    duration_seconds: int

@dataclass(frozen=True)
class PricingSnapshot:
    provider: str
    model: str
    base_input_per_mtok: Decimal
    cache_write_5m_per_mtok: Decimal
    cache_write_1h_per_mtok: Decimal
    cache_read_per_mtok: Decimal
    output_per_mtok: Decimal
    review_after: str

@dataclass(frozen=True)
class BuildAssessment:
    id: int | None
    task: str
    acceptance: str
    workspace: str
    repo_fingerprint: str
    deterministic_score: int
    local_adjustment: int
    final_score: int
    confidence: float
    recommended_tier: BuildTier
    dimensions: dict[str, int]
    floor_rules: Sequence[str]
    evidence: dict[str, Any]
    unknowns: Sequence[str]
    local_work: Sequence[str]
    cloud_reasons: Sequence[str]
    created_at: str

@dataclass(frozen=True)
class BuildSession:
    id: int
    assessment_id: int
    work_item_id: int | None
    workspace: str
    repo_fingerprint: str
    phase: str
    status: str
    checkpoint: dict[str, Any]
    created_at: str
    updated_at: str

@dataclass(frozen=True)
class BuildLease:
    id: int
    session_id: int
    version: int
    tier: BuildTier
    provider: str
    model: str
    limits: LeaseLimits
    state: str
    approval_request_id: int
    actual_input_tokens: int
    actual_output_tokens: int
    actual_microdollars: int
    reserved_input_tokens: int
    reserved_output_tokens: int
    reserved_microdollars: int
    cloud_turns: int
    started_at: str
    expires_at: str

@dataclass(frozen=True)
class CloudUsageEvent:
    id: int
    lease_id: int
    request_id: str
    turn_number: int
    status: str
    input_tokens: int
    cache_write_5m_tokens: int
    cache_write_1h_tokens: int
    cache_read_tokens: int
    output_tokens: int
    reserved_microdollars: int
    settled_microdollars: int
    pricing: PricingSnapshot
    created_at: str
    settled_at: str | None
```

Add frozen config dataclasses whose defaults exactly match the approved tier table and Opus 4.8 pricing snapshot. Validate positive values, monotonic tier limits, known cache categories, and ISO dates in `load_config()`.

- [ ] **Step 4: Document configuration without enabling cloud globally**

Add `[build]`, `[build.tiers.small|medium|large]`, and `[build.anthropic_pricing]` examples. Keep the example engine `native`; the machine-specific `config.toml` switches to `hybrid` only in Task 8.

- [ ] **Step 5: Run focused configuration tests**

Run: `C:\LocalAICofounder\.venv\Scripts\python.exe -m pytest tests/test_build_config.py tests/test_config.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add src/cofounder_kernel/build_types.py src/cofounder_kernel/config.py config.example.toml tests/test_build_config.py
git commit -m "feat: define build lease policy"
```

### Task 2: Zero-Cost Local Build Assessment

**Files:**
- Create: `src/cofounder_kernel/build_assessment.py`
- Test: `tests/test_build_assessment.py`

**Interfaces:**
- Consumes: `BuildTier`; optional `OllamaClient.chat(messages: Sequence[dict[str, Any]], format: dict[str, Any])`.
- Produces: `BuildAssessmentService.assess(task, workspace, acceptance="") -> BuildAssessment`.

- [ ] **Step 1: Write failing deterministic-floor tests**

```python
def test_greenfield_saas_and_mobile_requires_large(tmp_path):
    result = service().assess(
        task="Build a SaaS backend with auth, billing, iOS and Android clients",
        workspace=tmp_path,
    )
    assert result.recommended_tier is BuildTier.LARGE
    assert "greenfield_saas_plus_mobile" in result.floor_rules

def test_local_adjustment_cannot_lower_floor(tmp_path):
    result = service(local_reply={"score_adjustment": -40, "confidence": 0.9}).assess(
        task="Add Stripe billing and production authentication", workspace=tmp_path
    )
    assert result.recommended_tier in {BuildTier.MEDIUM, BuildTier.LARGE}
    assert result.final_score >= result.deterministic_score

def test_assessment_never_calls_cloud(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SENTINEL")
    result = service(local_reply=None).assess(task="Rename a label", workspace=tmp_path)
    assert result.recommended_tier is BuildTier.SMALL
```

- [ ] **Step 2: Run tests and confirm missing-service failure**

Run: `C:\LocalAICofounder\.venv\Scripts\python.exe -m pytest tests/test_build_assessment.py -q`

Expected: FAIL because `BuildAssessmentService` is missing.

- [ ] **Step 3: Implement bounded deterministic repository evidence**

Implement a scanner that skips `.git`, virtual environments, dependency folders, caches, generated output, binaries, and secrets; caps traversal at 5,000 files; hashes relative paths plus size/mtime; parses `package.json` and `pyproject.toml` with structured parsers; and emits evidence for the seven approved scoring dimensions.

```python
def assess(self, *, task: str, workspace: str | Path, acceptance: str = "") -> BuildAssessment:
    evidence = self._scan(Path(workspace))
    deterministic_score, floors = self._score(task, acceptance, evidence)
    adjustment = self._local_adjustment(task, acceptance, evidence)
    final_score = max(deterministic_score, deterministic_score + max(0, adjustment.points))
    tier = max(_tier_for_score(final_score), _floor_tier(floors), key=_tier_rank)
    if adjustment.confidence < 0.65:
        tier = _raise_one(tier)
    return BuildAssessment(
        id=None,
        task=task.strip(),
        acceptance=acceptance.strip(),
        workspace=str(Path(workspace).resolve()),
        repo_fingerprint=evidence.fingerprint,
        deterministic_score=deterministic_score,
        local_adjustment=max(0, adjustment.points),
        final_score=final_score,
        confidence=adjustment.confidence,
        recommended_tier=tier,
        dimensions=evidence.dimensions,
        floor_rules=tuple(floors),
        evidence=evidence.as_dict(),
        unknowns=tuple(adjustment.unknowns),
        local_work=tuple(evidence.local_work),
        cloud_reasons=tuple(adjustment.reasons),
        created_at=utc_now(),
    )
```

- [ ] **Step 4: Implement one optional structured local-model pass**

Send only the normalized request and compact evidence summary to Ollama with a strict JSON schema containing `score_adjustment`, `confidence`, `reasons`, and `unknowns`. Treat invalid output or local unavailability as a zero adjustment with reduced confidence; never import or call Anthropic from this module.

- [ ] **Step 5: Run assessment tests**

Run: `C:\LocalAICofounder\.venv\Scripts\python.exe -m pytest tests/test_build_assessment.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add src/cofounder_kernel/build_assessment.py tests/test_build_assessment.py
git commit -m "feat: assess build complexity locally"
```

### Task 3: Durable Build Session Store

**Files:**
- Create: `src/cofounder_kernel/build_store.py`
- Modify: `src/cofounder_kernel/db.py`
- Test: `tests/test_build_store.py`

**Interfaces:**
- Consumes: value objects from `build_types.py`; `KernelDatabase.connect()` and `utc_now()`.
- Produces: `BuildStore` CRUD plus atomic `create_reservation()` and `settle_reservation()` transactions.

- [ ] **Step 1: Write failing migration and restart tests**

```python
def test_migration_creates_four_build_tables(db):
    names = {r[0] for r in db.connect().execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"build_assessments", "build_sessions", "build_leases", "cloud_usage_events"} <= names

def test_session_and_lease_survive_reopen(tmp_path):
    first = store(tmp_path)
    session = first.create_session(sample_assessment())
    lease = first.create_lease(
        session.id,
        BuildTier.SMALL,
        SMALL_LIMITS,
        provider="anthropic",
        model="claude-opus-4-8",
        approval_request_id=7,
    )
    second = store(tmp_path)
    assert second.get_session(session.id).id == session.id
    assert second.get_active_lease(session.id).id == lease.id
```

- [ ] **Step 2: Run tests and confirm schema failure**

Run: `C:\LocalAICofounder\.venv\Scripts\python.exe -m pytest tests/test_build_store.py -q`

Expected: FAIL because the tables and `BuildStore` are absent.

- [ ] **Step 3: Add idempotent schema and indexes**

Increase `SCHEMA_VERSION` by one and add the four approved tables. Store JSON with stable sorted serialization. Add unique indexes for one open reservation request id, one lease version per session, and one active lease per session using a partial index.

- [ ] **Step 4: Implement focused persistence methods**

Implement these exact public signatures on `BuildStore`: `create_assessment(assessment: BuildAssessment) -> BuildAssessment`, `create_session(assessment: BuildAssessment, *, work_item_id: int | None = None) -> BuildSession`, `checkpoint(session_id: int, *, phase: str, checkpoint: dict[str, Any]) -> BuildSession`, `create_lease(session_id: int, tier: BuildTier, limits: LeaseLimits, *, provider: str, model: str, approval_request_id: int) -> BuildLease`, `get_active_lease(session_id: int) -> BuildLease | None`, and `list_usage(lease_id: int) -> list[CloudUsageEvent]`.

Keep SQL in this module except for table declarations in `db.py`.

- [ ] **Step 5: Run store tests and migration regression tests**

Run: `C:\LocalAICofounder\.venv\Scripts\python.exe -m pytest tests/test_build_store.py tests/test_db_migrations.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add src/cofounder_kernel/build_store.py src/cofounder_kernel/db.py tests/test_build_store.py
git commit -m "feat: persist build sessions and leases"
```

### Task 4: Atomic Budget Reservation And Settlement

**Files:**
- Create: `src/cofounder_kernel/build_budget.py`
- Modify: `src/cofounder_kernel/build_store.py`
- Test: `tests/test_build_budget.py`

**Interfaces:**
- Consumes: `BuildStore`, `LeaseLimits`, `PricingSnapshot`.
- Produces: `BuildBudgetService.reserve()`, `settle()`, `release_pre_send()`, `mark_uncertain()`, and `request_upgrade_summary()`.

- [ ] **Step 1: Write failing hard-cap tests**

```python
def test_reservation_refuses_before_send_when_any_limit_would_be_exceeded(budget):
    with pytest.raises(BuildBudgetExceeded, match="output_tokens"):
        budget.reserve(session_id=1, request_id="r1", input_upper=1000, max_output=20_000, cache_mode="write_1h")

def test_missing_usage_charges_reserved_maximum(budget):
    reservation = budget.reserve(session_id=1, request_id="r1", input_upper=1000, max_output=1000, cache_mode="write_1h")
    event = budget.settle(reservation.id, usage=None)
    assert event.status == "conservative_settlement"
    assert event.settled_microdollars == reservation.reserved_microdollars

def test_ambiguous_timeout_pauses_without_releasing_reservation(budget):
    reservation = budget.reserve(session_id=1, request_id="r1", input_upper=1000, max_output=1000, cache_mode="none")
    event = budget.mark_uncertain(reservation.id, "timeout after headers")
    assert event.status == "uncertain_spend"
    assert budget.active_lease(1).state == "paused"
```

- [ ] **Step 2: Run tests and confirm missing-budget failure**

Run: `C:\LocalAICofounder\.venv\Scripts\python.exe -m pytest tests/test_build_budget.py -q`

Expected: FAIL because `BuildBudgetService` is absent.

- [ ] **Step 3: Implement Decimal pricing with ceiling to integer microdollars**

```python
def microdollars(tokens: int, usd_per_million: Decimal) -> int:
    return int((Decimal(tokens) * usd_per_million).quantize(Decimal("1"), rounding=ROUND_CEILING))
```

For reservation, price input at the most expensive request-possible category, add maximum output cost, and reject missing/expired pricing. Count uncached input, cache creation, and cache reads toward the input ceiling.

- [ ] **Step 4: Implement atomic compare-and-insert**

Within `BEGIN IMMEDIATE`, reload the active lease, sum settled plus open reservations, verify time/turn/input/output/dollar limits, insert an open usage event, and increment reserved counters. A concurrent loser raises `BuildBudgetExceeded` without creating a row.

- [ ] **Step 5: Implement settlement and lease state transitions**

Settle actual usage by category, retain the pricing snapshot, decrement reservations, increment actuals, and mark `warning` at 80 percent of any ceiling. `mark_uncertain()` retains reserved counters and sets `paused`; `release_pre_send()` deletes/releases only rows proven unsent.

- [ ] **Step 6: Run budget and concurrency tests**

Run: `C:\LocalAICofounder\.venv\Scripts\python.exe -m pytest tests/test_build_budget.py -q`

Expected: PASS, including a two-thread reservation race with exactly one winner.

- [ ] **Step 7: Commit**

```powershell
git add src/cofounder_kernel/build_budget.py src/cofounder_kernel/build_store.py tests/test_build_budget.py
git commit -m "feat: enforce atomic cloud budgets"
```

### Task 5: Local-First Routing And Focused Context

**Files:**
- Create: `src/cofounder_kernel/build_routing.py`
- Test: `tests/test_build_routing.py`

**Interfaces:**
- Consumes: `BuildAssessment`, `BuildSession`, local-attempt history, workspace path.
- Produces: `BuildRouter.route_step(session: BuildSession, step: BuildStep, attempts: Sequence[LocalAttempt]) -> RouteDecision` and `BuildContextSelector.select(task: str, candidates: Sequence[Path]) -> SelectedContext`.

- [ ] **Step 1: Write failing local-default and context-exclusion tests**

```python
def test_small_routine_edit_stays_local_even_with_lease(router, small_session):
    decision = router.route_step(small_session, BuildStep(kind="edit", risk="low"), [])
    assert decision.route == "local"

def test_two_distinct_local_failures_make_debugging_cloud_eligible(router, medium_session):
    attempts = [LocalAttempt("pytest failure A"), LocalAttempt("different fix; failure B")]
    assert router.route_step(medium_session, BuildStep(kind="debug", risk="high"), attempts).route == "cloud"

def test_context_excludes_secrets_dependencies_and_unrelated_history(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "auth.py").write_text("def callback(): pass\n")
    (tmp_path / ".env").write_text("SECRET=sentinel\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "vendor.js").write_text("vendor\n")
    selected = selector(tmp_path).select(
        task="fix auth callback",
        candidates=[tmp_path / "src" / "auth.py", tmp_path / ".env", tmp_path / "node_modules" / "vendor.js"],
    )
    assert ".env" not in selected.paths
    assert not any("node_modules" in p for p in selected.paths)
    assert selected.total_chars <= 48_000
```

- [ ] **Step 2: Run tests and confirm missing-router failure**

Run: `C:\LocalAICofounder\.venv\Scripts\python.exe -m pytest tests/test_build_routing.py -q`

Expected: FAIL because routing/context modules are missing.

- [ ] **Step 3: Implement explicit routing reasons**

Use only the approved cloud reasons. Local failure escalation requires two attempts with different normalized action hashes. No lease, expired lease, stale pricing, exhausted budget, or cloud-disabled policy returns `local` or `founder`, never an implicit provider call.

- [ ] **Step 4: Implement deterministic context selection**

Rank exact path mentions, search matches, changed files, failing stack paths, dependency manifests, and instruction files. Exclude secret names, binary files, dependencies, generated output, and old chat turns. Return excerpts with path, line range, content hash, truncation flag, and total UTF-8 bytes.

- [ ] **Step 5: Run routing tests**

Run: `C:\LocalAICofounder\.venv\Scripts\python.exe -m pytest tests/test_build_routing.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add src/cofounder_kernel/build_routing.py tests/test_build_routing.py
git commit -m "feat: route build work local first"
```

### Task 6: Provider-Neutral Coding Model Seam

**Files:**
- Create: `src/cofounder_kernel/model_client.py`
- Modify: `src/cofounder_kernel/coding_agent.py`
- Modify: `tests/test_coding_agent.py`

**Interfaces:**
- Produces: `CodingModelClient.chat(messages: Sequence[Any], model: str | None, think: bool | None, temperature: float | None, num_predict: int, tools: Sequence[Mapping[str, Any]] | None, format: str | Mapping[str, Any] | None) -> GenerateResult`, `provider_info()`, and `CodingModelError`.
- Preserves: `CodingAgentService(config=kernel_config, db=kernel_db, ollama=ollama_client)` and all existing local behavior.

- [ ] **Step 1: Write a failing alternate-client tool-loop test**

```python
def test_coding_loop_uses_injected_model_client_with_same_local_tools(tmp_path, fixture_repo):
    cloud = ScriptedModelClient([{"tool_calls": [_call("read_file", path="calc.py")]}, {"content": "done"}])
    svc = CodingAgentService(config=cfg, db=db, ollama=local, model_client=cloud, inventory=inventory)
    result = svc.run(task="review calc", workspace=fixture_repo, model="cloud-test")
    assert result["provider"]["provider"] == "fake-cloud"
    assert any(step["tool"] == "read_file" for step in result["steps"])
```

- [ ] **Step 2: Run the focused test and confirm constructor failure**

Run: `C:\LocalAICofounder\.venv\Scripts\python.exe -m pytest tests/test_coding_agent.py::test_coding_loop_uses_injected_model_client_with_same_local_tools -q`

Expected: FAIL because `model_client` is not accepted.

- [ ] **Step 3: Add the protocol without changing the local interface**

```python
class CodingModelClient(Protocol):
    def chat(self, *, messages, model=None, think=None, temperature=None, num_predict=512, tools=None, format=None) -> GenerateResult:
        raise NotImplementedError
    def provider_info(self) -> dict[str, Any]:
        raise NotImplementedError

class CodingModelError(RuntimeError):
    pass
```

Keep `ollama` for local inventory/model resolution. Store `self.model_client = model_client or ollama`; route chat and provider telemetry through it; catch `CodingModelError` alongside `OllamaError`. Explicit `model=` bypasses local inventory resolution but not workspace/tool constraints.

- [ ] **Step 4: Run the complete coding-agent suite**

Run: `C:\LocalAICofounder\.venv\Scripts\python.exe -m pytest tests/test_coding_agent.py -q`

Expected: PASS with all previous local tests unchanged.

- [ ] **Step 5: Commit**

```powershell
git add src/cofounder_kernel/model_client.py src/cofounder_kernel/coding_agent.py tests/test_coding_agent.py
git commit -m "refactor: make coding model provider neutral"
```

### Task 7: Budgeted Anthropic Streaming Adapter

**Files:**
- Create: `src/cofounder_kernel/anthropic_build.py`
- Modify: `src/cofounder_kernel/anthropic_client.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Test: `tests/test_anthropic_build.py`
- Test: `tests/test_strategy_review.py`

**Interfaces:**
- Consumes: `BuildBudgetService`, active session/lease, selected context, Anthropic SDK client.
- Produces: a session-bound `AnthropicBuildModelClient` satisfying `CodingModelClient`.

- [ ] **Step 1: Write failing no-lease, cache, usage, and timeout tests**

```python
def test_chat_refuses_before_constructing_sdk_request_without_lease(adapter):
    with pytest.raises(BuildLeaseRequired):
        adapter.chat(messages=[{"role": "user", "content": "x"}], tools=[])
    assert adapter.fake.messages.calls == []

def test_chat_caches_stable_system_and_tool_prefix(adapter_with_lease):
    adapter_with_lease.chat(messages=MESSAGES, tools=TOOLS, num_predict=512)
    sent = adapter_with_lease.fake.messages.last_request
    assert sent["system"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert sent["tools"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}

def test_timeout_after_stream_start_marks_uncertain(adapter_with_lease):
    adapter_with_lease.fake.raise_after_enter = TimeoutError("lost")
    with pytest.raises(CodingModelError):
        adapter_with_lease.chat(messages=MESSAGES, tools=TOOLS)
    assert adapter_with_lease.budget.active_lease().state == "paused"
```

- [ ] **Step 2: Run tests and confirm missing-adapter failure**

Run: `C:\LocalAICofounder\.venv\Scripts\python.exe -m pytest tests/test_anthropic_build.py -q`

Expected: FAIL because `AnthropicBuildModelClient` is missing.

- [ ] **Step 3: Add and lock the optional SDK dependency**

Add `cloud = ["anthropic>=0.117,<0.118"]` and run `uv lock`. Import Anthropic lazily so local-only startup and offline tests do not require the extra.

- [ ] **Step 4: Implement token authorization and reservation before streaming**

Convert Ollama-style system/user/assistant/tool messages and function schemas to Anthropic Messages blocks. Use `messages.count_tokens` only after lease and egress authorization; if unavailable, use serialized UTF-8 bytes plus configured overhead. Reserve input upper bound, output max, cache mode, and pricing before entering the SDK `messages.stream` context manager.

- [ ] **Step 5: Implement streaming and response mapping**

Use the SDK stream context manager and `get_final_message()`. Convert text and `tool_use` blocks into `GenerateResult(response=text, raw={"message": {"content": text, "tool_calls": tool_calls}})` so the existing coding loop executes the shared local tools. Map usage into uncached input, cache write 5m/1h, cache read, and output categories and settle exactly once.

- [ ] **Step 6: Preserve strategic review behavior through shared SDK construction**

Keep `AnthropicClient.review(prompt, system, max_tokens)` stable. Reuse a lazy SDK factory and existing provider-policy/host/key checks. Strategic review remains one-shot and continues using its existing per-request egress grant.

- [ ] **Step 7: Run adapter and strategic-review tests**

Run: `C:\LocalAICofounder\.venv\Scripts\python.exe -m pytest tests/test_anthropic_build.py tests/test_strategy_review.py -q`

Expected: PASS with no real network calls.

- [ ] **Step 8: Commit**

```powershell
git add src/cofounder_kernel/anthropic_build.py src/cofounder_kernel/anthropic_client.py pyproject.toml uv.lock tests/test_anthropic_build.py tests/test_strategy_review.py
git commit -m "feat: add budgeted Anthropic build adapter"
```

### Task 8: Build Session Lifecycle, Approval, Egress, And Delegation

**Files:**
- Create: `src/cofounder_kernel/build_service.py`
- Modify: `src/cofounder_kernel/delegation.py`
- Modify: `src/cofounder_kernel/egress.py`
- Modify: `src/cofounder_kernel/config.py`
- Modify: `config.toml`
- Test: `tests/test_build_service.py`
- Modify: `tests/test_delegation.py`
- Modify: `tests/test_egress.py`

**Interfaces:**
- Consumes: assessor, store, budget, router, context selector, local coding agent, Anthropic model-client factory, work queue, approval requests, and egress policy.
- Produces: `BuildService.prepare()`, `approve()`, `deny()`, `run()`, `status()`, and deduplicated upgrade requests.

- [ ] **Step 1: Write failing prepare/approve/run/exhaustion tests**

```python
def test_prepare_creates_assessment_session_and_one_approval(service):
    prepared = service.prepare(task="build the app", workspace=workspace, acceptance="tests pass")
    assert prepared["session"]["phase"] == "approval"
    assert prepared["assessment"]["recommended_tier"] in {"small", "medium", "large"}
    assert len(db.list_approval_requests(status="pending")) == 1
    assert cloud.calls == []

def test_approve_mints_lease_then_local_route_spends_zero(service):
    prepared = service.prepare(task="rename one label", workspace=workspace)
    result = service.approve(prepared["session"]["id"], typed_phrase=CONFIRMATION)
    assert result["run"]["route"] == "local"
    assert result["usage"]["actual_microdollars"] == 0

def test_exhaustion_continues_local_and_creates_one_upgrade_request(service):
    exhaust_active_lease(service)
    first = service.run(session_id)
    second = service.run(session_id)
    assert first["lease"]["state"] == "exhausted"
    assert first["local_continued"] is True
    assert first["upgrade_request_id"] == second["upgrade_request_id"]
```

- [ ] **Step 2: Run tests and confirm missing-service failure**

Run: `C:\LocalAICofounder\.venv\Scripts\python.exe -m pytest tests/test_build_service.py -q`

Expected: FAIL because `BuildService` is absent.

- [ ] **Step 3: Implement the session lifecycle and typed approval**

`prepare()` assesses locally, persists assessment/session, and creates one `build_lease` approval request containing the workspace fingerprint, provider/model, permitted data classes, exact limits, score, confidence, evidence, and local-first rules. `approve()` verifies the configured typed phrase, resolves the request, creates the version-1 lease, and dispatches `run()`.

- [ ] **Step 4: Implement lease-scoped egress decisions**

Add `authorize_build_egress(db, policy, request, *, lease)` that constructs `EgressAuthorization` only from an active, unexpired, matching lease and audits the lease/session/usage request ids. It never calls or consumes the existing one-shot grant helpers.

- [ ] **Step 5: Implement local/cloud execution and checkpoints**

Route each task slice. Local uses the existing injected `CodingAgentService`. Cloud constructs `AnthropicBuildModelClient` bound to the lease and passes it through `CodingAgentService(model_client=anthropic_model_client)` with `run(model=lease.model)`, preserving the same tools and verification. Persist phase, selected context hashes, route reasons, result summary, and verification evidence after each run.

- [ ] **Step 6: Add hybrid delegation without changing native semantics**

Accept `engine = "hybrid"`. In `DelegationService.run_from_work_item`, call `BuildService.prepare()` for an unprepared hybrid item and return `approval_required`; after approval, the build service updates the linked work item with the run result. Existing `native`, `bridge`, and `brief` tests remain unchanged.

- [ ] **Step 7: Implement warning, exhaustion, and upgrade requests**

At 80 percent expose warning. When no reservation fits, set exhausted, checkpoint, run any eligible local continuation once, and create one `build_lease_upgrade` approval request keyed by session and lease version with actual spend, completed work, remaining work, and the next tier. Approval adds limits cumulatively; it never resets usage.

- [ ] **Step 8: Select hybrid in machine config**

Change only `[delegation] engine = "hybrid"` in `config.toml`. Provider policy remains `local_preferred`; no standing Anthropic source-code grant is added.

- [ ] **Step 9: Run lifecycle, delegation, and egress tests**

Run: `C:\LocalAICofounder\.venv\Scripts\python.exe -m pytest tests/test_build_service.py tests/test_delegation.py tests/test_egress.py -q`

Expected: PASS.

- [ ] **Step 10: Commit**

```powershell
git add src/cofounder_kernel/build_service.py src/cofounder_kernel/delegation.py src/cofounder_kernel/egress.py src/cofounder_kernel/config.py config.toml tests/test_build_service.py tests/test_delegation.py tests/test_egress.py
git commit -m "feat: orchestrate approved hybrid builds"
```

### Task 9: API, UI, Status, Offline Acceptance, And Final Verification

**Files:**
- Modify: `src/cofounder_kernel/models.py`
- Modify: `src/cofounder_kernel/api.py`
- Modify: `ui/swarm.html`
- Modify: `tests/test_api.py`
- Modify: `tests/test_offline_acceptance.py`
- Create: `tests/test_build_integration.py`

**Interfaces:**
- Produces: `POST /build/assess`, `GET /build/sessions`, `GET /build/sessions/{id}`, `POST /build/sessions/{id}/approve`, `POST /build/sessions/{id}/deny`, `POST /build/sessions/{id}/run`, and build-session fields in `GET /delegation/status`.
- Preserves: existing delegation routes and runtime build-command routing.

- [ ] **Step 1: Write failing protected-route integration tests**

```python
def test_build_assess_is_local_and_returns_approval(client, monkeypatch, workspace):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SENTINEL")
    body = client.post("/build/assess", json={"task": "build auth API", "workspace": str(workspace)}).json()
    assert body["assessment"]["recommended_tier"] in {"medium", "large"}
    assert body["session"]["phase"] == "approval"

def test_approve_then_status_reconciles_usage(client, prepared_session, fake_anthropic):
    approved = client.post(f"/build/sessions/{prepared_session}/approve", json={"typed_confirmation": CONFIRMATION})
    assert approved.status_code == 200
    detail = client.get(f"/build/sessions/{prepared_session}").json()
    assert detail["lease"]["authorized_microdollars"] >= detail["usage"]["actual_microdollars"]

def test_automated_app_never_sends_sentinel_key(offline_client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SENTINEL-must-never-send")
    offline_client.post("/build/assess", json={"task": "build app", "workspace": str(workspace)})
    assert recorded_network_hosts == []
```

- [ ] **Step 2: Run integration tests and confirm route failure**

Run: `C:\LocalAICofounder\.venv\Scripts\python.exe -m pytest tests/test_build_integration.py -q`

Expected: FAIL with 404 for the new routes.

- [ ] **Step 3: Wire services and routes**

Construct `BuildStore`, `BuildBudgetService`, `BuildAssessmentService`, `BuildRouter`, `BuildContextSelector`, and `BuildService` in `create_app()`. Store them on `app.state`. Use bounded Pydantic request models. Convert domain errors to 400, missing sessions to 404, unavailable cloud configuration to 503, and provider flow errors to structured 502 responses without losing checkpoints.

- [ ] **Step 4: Extend status and egress-ledger visibility**

Return active/recent sessions, tier, phase, lease state, expiration, actual/reserved/remaining token categories, actual/authorized microdollars, turns, cache usage, route counts, and upgrade request id. Add lease/session/usage ids to cloud egress audit records and expose them in the existing ledger aggregation.

- [ ] **Step 5: Update the Delegation UI**

In `ui/swarm.html`, add compact assessment evidence, tier badge, lease progress meters, dollar/token/turn counters, expiration, route, and upgrade reason. Use existing tabs, button styles, and DOM helpers; no nested cards. Approve and deny actions call the new routes and refresh status.

- [ ] **Step 6: Run focused API and offline tests**

Run: `C:\LocalAICofounder\.venv\Scripts\python.exe -m pytest tests/test_build_integration.py tests/test_api.py tests/test_offline_acceptance.py -q`

Expected: PASS and no outbound Anthropic/OpenAI/Ollama-cloud hosts in offline acceptance.

- [ ] **Step 7: Run complete regression and static verification**

Run: `C:\LocalAICofounder\.venv\Scripts\python.exe -m pytest -q`

Expected: all tests PASS.

Run: `C:\LocalAICofounder\.venv\Scripts\python.exe -m compileall -q src tests`

Expected: exit code 0.

Run: `git diff --check $(git merge-base main HEAD)..HEAD`

Expected: no output and exit code 0.

- [ ] **Step 8: Perform a no-spend local API smoke**

Start the kernel with no Anthropic SDK call, fetch `/session/token`, assess a fixture build, confirm an approval exists, and verify the egress ledger reports zero Anthropic sends. Do not approve the lease during this smoke.

- [ ] **Step 9: Commit**

```powershell
git add src/cofounder_kernel/models.py src/cofounder_kernel/api.py ui/swarm.html tests/test_api.py tests/test_offline_acceptance.py tests/test_build_integration.py
git commit -m "feat: expose budgeted build sessions"
```

## Final Review Checklist

- [ ] Compare every implementation commit to `docs/superpowers/specs/2026-07-18-token-budgeted-build-delegation-design.md`.
- [ ] Confirm no test imports or invokes a real Anthropic client.
- [ ] Confirm one-shot strategic-review grants are still single-use.
- [ ] Confirm `native`, `bridge`, and `brief` delegation behavior remains covered.
- [ ] Confirm SQLite restart tests prove budgets cannot reset.
- [ ] Confirm every provider request has reservation, lease, egress, and settlement audit links.
- [ ] Confirm source files, prompts, and API keys are absent from usage/status audit payloads.
- [ ] Run the complete test suite and compile verification again after final review fixes.
