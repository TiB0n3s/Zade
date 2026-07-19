from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .config import ModelRole


class MemoryCreate(BaseModel):
    kind: str = Field(default="note", min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=240)
    content: str = Field(min_length=1)
    source: str = Field(default="local", min_length=1, max_length=240)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemorySearch(BaseModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=8, ge=1, le=50)


class IngestTextRequest(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    text: str = Field(min_length=1)
    source: str = Field(default="api:text", min_length=1, max_length=500)
    metadata: dict[str, Any] = Field(default_factory=dict)


class IngestFileRequest(BaseModel):
    path: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class IngestFolderRequest(BaseModel):
    path: str = Field(min_length=1)
    recursive: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class SemanticSearchRequest(BaseModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=8, ge=1, le=50)
    mode: str = Field(default="hybrid", min_length=1, max_length=20)


class AuthorityEvaluateRequest(BaseModel):
    action: str = Field(min_length=1, max_length=240)
    permission_tier: str = Field(default="L0_READ", min_length=1, max_length=80)
    target: str = Field(default="", max_length=1000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ApprovalResolveRequest(BaseModel):
    resolved_by: str = Field(default="founder", min_length=1, max_length=120)
    note: str = Field(default="", max_length=1000)
    dispatch: bool = False
    typed_confirmation: str = Field(default="", max_length=200)


class StrategyReviewRequest(BaseModel):
    focus: str = Field(default="", max_length=2000)
    question: str = Field(default="", max_length=2000)


class ChannelEnrollRequest(BaseModel):
    channel: str = Field(min_length=1, max_length=60)
    label: str = Field(default="", max_length=200)


class ChannelConfirmRequest(BaseModel):
    channel: str = Field(min_length=1, max_length=60)
    external_id: str = Field(min_length=1, max_length=200)
    code: str = Field(min_length=1, max_length=200)


class ChannelTierRequest(BaseModel):
    max_tier: str = Field(min_length=1, max_length=40)


class ChannelMessageRequest(BaseModel):
    channel: str = Field(min_length=1, max_length=60)
    external_id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=8000)
    # HMAC fields — required only when the binding carries a signing key.
    # ts is the sender's unix timestamp as the literal string that was signed.
    ts: str = Field(default="", max_length=32)
    signature: str = Field(default="", max_length=128)


class ApprovalDeferRequest(BaseModel):
    resolved_by: str = Field(default="founder", min_length=1, max_length=120)
    note: str = Field(default="", max_length=1000)
    defer_until: str | None = Field(default=None, max_length=120)


class ApprovalEditRequest(BaseModel):
    edited_by: str = Field(default="founder", min_length=1, max_length=120)
    note: str = Field(default="", max_length=1000)
    title: str | None = Field(default=None, max_length=240)
    detail: str | None = Field(default=None, max_length=4000)
    action: str | None = Field(default=None, max_length=240)
    target: str | None = Field(default=None, max_length=1000)
    permission_tier: str | None = Field(default=None, max_length=80)
    priority: int | None = Field(default=None, ge=0, le=100)
    evidence: list[Any] | None = None
    risks: list[Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class BackupCreateRequest(BaseModel):
    label: str = Field(default="manual", min_length=1, max_length=80)


class BackupRetentionRequest(BaseModel):
    keep_last: int = Field(default=10, ge=1, le=500)
    dry_run: bool = True


class ModelBenchmarkRequest(BaseModel):
    prompt: str = Field(default="State local co-founder readiness in one sentence.", min_length=1, max_length=2000)
    roles: list[ModelRole] = Field(default_factory=lambda: ["general", "reasoning", "coding"])
    num_predict: int = Field(default=160, ge=16, le=2048)


class SkillScanRequest(BaseModel):
    source_dir: str | None = None
    enable_defaults: bool | None = None


class SkillRouteRequest(BaseModel):
    query: str = Field(min_length=1)
    task_type: ModelRole = "general"
    limit: int = Field(default=3, ge=0, le=8)


class WorkItemCreate(BaseModel):
    kind: str = Field(default="manual", min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=240)
    detail: str = ""
    action: str = Field(min_length=1, max_length=240)
    target: str = Field(default="", max_length=1000)
    permission_tier: str = Field(default="L0_READ", min_length=1, max_length=80)
    priority: int = Field(default=50, ge=0, le=100)
    source: str = Field(default="founder.direct", min_length=1, max_length=240)
    due_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    unique_key: str | None = None


class WorkScanRequest(BaseModel):
    run_autonomous: bool = True
    max_run: int = Field(default=5, ge=0, le=25)


class WorkRunRequest(BaseModel):
    max_items: int = Field(default=5, ge=1, le=25)


class TradingBotOpsCheckRequest(BaseModel):
    command: str = Field(min_length=1, max_length=80)
    target_date: str | None = Field(default=None, max_length=20)
    limit_output_chars: int = Field(default=12000, ge=100, le=50000)


class TradingBotRecommendationCreate(BaseModel):
    market_date: str = Field(min_length=10, max_length=10)
    symbol: str = Field(min_length=1, max_length=16)
    action: str = Field(min_length=1, max_length=12)
    verdict: str = Field(min_length=1, max_length=16)
    reason: str = Field(min_length=1, max_length=2000)
    conviction: float | None = Field(default=None)
    context_hash: str | None = Field(default=None, max_length=128)
    agent_version: str = Field(default="zade-local-cofounder-v1", min_length=1, max_length=120)
    idempotency_key: str | None = Field(default=None, max_length=64)
    evidence: list[Any] = Field(default_factory=list)
    risks: list[Any] = Field(default_factory=list)
    priority: int = Field(default=90, ge=0, le=100)


class TradingBotAdvisoryGenerateRequest(BaseModel):
    target_date: str = Field(min_length=10, max_length=10)
    symbols: list[str] = Field(default_factory=list)
    queue: bool = True
    max_recommendations: int = Field(default=10, ge=0, le=50)
    include_ops_checks: list[str] = Field(default_factory=list)
    limit_output_chars: int = Field(default=12000, ge=100, le=50000)
    priority: int = Field(default=90, ge=0, le=100)
    use_sqlite_snapshot: bool = True
    snapshot_tables: list[str] = Field(default_factory=list)
    snapshot_limit_per_table: int = Field(default=25, ge=1, le=200)


class TradingBotAdvisoryScoreRequest(BaseModel):
    target_date: str = Field(min_length=10, max_length=10)
    store_evidence: bool = True
    limit_output_chars: int = Field(default=12000, ge=100, le=50000)


class TradingBotJudgmentScoreRequest(BaseModel):
    target_date: str = Field(min_length=10, max_length=10)
    symbols: list[str] = Field(default_factory=list)
    store_evidence: bool = True
    limit_per_symbol: int = Field(default=25, ge=1, le=100)


class TradingBotTriggerProposalRequest(BaseModel):
    operation: str = Field(min_length=1, max_length=160)
    target_date: str | None = Field(default=None, max_length=10)
    reason: str = Field(min_length=1, max_length=3000)
    params: dict[str, Any] = Field(default_factory=dict)
    evidence: list[Any] = Field(default_factory=list)
    risks: list[Any] = Field(default_factory=list)
    priority: int = Field(default=80, ge=0, le=100)
    idempotency_key: str | None = Field(default=None, max_length=64)


class TradingBotSQLiteQueryRequest(BaseModel):
    sql: str = Field(min_length=1, max_length=20000)
    params: list[Any] | dict[str, Any] = Field(default_factory=list)
    limit: int = Field(default=100, ge=1, le=1000)
    timeout_seconds: float = Field(default=5.0, ge=0.1, le=30.0)
    database: str = Field(default="trades.db", min_length=1, max_length=80)


class TradingBotTrainingRunRequest(BaseModel):
    command: str = Field(min_length=1, max_length=80)
    target_date: str | None = Field(default=None, max_length=10)
    symbols: list[str] = Field(default_factory=list)
    extra_args: list[str] = Field(default_factory=list, max_length=40)
    timeout_seconds: float = Field(default=300.0, ge=1.0, le=3600.0)
    limit_output_chars: int = Field(default=12000, ge=100, le=50000)


class TradingBotEvidenceSnapshotRequest(BaseModel):
    target_date: str = Field(min_length=10, max_length=10)
    symbols: list[str] = Field(default_factory=list)
    tables: list[str] = Field(default_factory=list)
    limit_per_table: int = Field(default=25, ge=1, le=200)
    store_evidence: bool = True


class TradingBotDailyBriefRequest(BaseModel):
    target_date: str = Field(min_length=10, max_length=10)
    symbols: list[str] = Field(default_factory=list)
    snapshot_tables: list[str] = Field(default_factory=list)
    limit_per_table: int = Field(default=25, ge=1, le=200)
    max_recommendations: int = Field(default=10, ge=0, le=50)
    include_ops_checks: list[str] = Field(default_factory=list)
    store_evidence: bool = True
    create_judgments: bool = True
    score_outcomes: bool = True
    export_vault: bool = False
    limit_output_chars: int = Field(default=12000, ge=100, le=50000)


class RuntimeRespondRequest(BaseModel):
    message: str = Field(min_length=1)
    model: str | None = None
    task_type: ModelRole = "general"
    profile: str | None = Field(default=None, max_length=80)
    proposed_action: str = Field(default="runtime.respond", min_length=1, max_length=240)
    permission_tier: str = Field(default="L0_READ", min_length=1, max_length=80)
    target: str = Field(default="local_runtime", max_length=1000)
    use_memory: bool = True
    use_semantic_memory: bool = True
    semantic_limit: int = Field(default=4, ge=0, le=12)
    use_skills: bool = True
    skill_limit: int = Field(default=3, ge=0, le=8)
    think: bool | None = None
    conversation_id: int | None = Field(default=None, ge=1)
    contrarian: bool | None = None
    # None = use the configured default (ollama.tool_loop). Explicit False lets
    # latency-sensitive callers (voice) skip the investigation loop.
    use_tools: bool | None = None


class ConversationCreate(BaseModel):
    title: str = Field(default="", max_length=240)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SurfacingBriefRequest(BaseModel):
    narrate: bool = False
    force: bool = False


class EvalCaseUpsert(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    category: str = Field(default="custom", min_length=1, max_length=80)
    executor: str = Field(default="generate", min_length=1, max_length=40)
    task_type: ModelRole = "general"
    description: str = Field(default="", max_length=1000)
    prompt: str = Field(min_length=1)
    draft: str = ""
    checks: list[dict[str, Any]] = Field(default_factory=list)
    respond_options: dict[str, Any] = Field(default_factory=dict)
    setup_memories: list[dict[str, Any]] = Field(default_factory=list)
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConnectorCreate(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    connector_type: str = Field(min_length=1, max_length=40)
    description: str = Field(default="", max_length=1000)
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConnectorUpdate(BaseModel):
    """Partial update: omitted fields keep their stored values. Name and
    connector_type are immutable."""

    description: str | None = Field(default=None, max_length=1000)
    config: dict[str, Any] | None = None
    enabled: bool | None = None


class ConnectorItemsImport(BaseModel):
    item_ids: list[int] = Field(min_length=1)
    create_evidence: bool = True
    ingest_documents: bool = True
    reliability: str = Field(default="C", min_length=1, max_length=2)
    strength: int = Field(default=60, ge=0, le=100)
    linked_assumption_id: int | None = None
    linked_decision_id: int | None = None


class ConnectorItemDismiss(BaseModel):
    reason: str = Field(default="", max_length=500)


class BrowserRunRequest(BaseModel):
    steps: list[dict[str, Any]] = Field(min_length=1, max_length=50)
    title: str = Field(default="", max_length=200)
    session_label: str = Field(default="", max_length=120)


class VaultDeleteRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    allow_top_level: bool = False
    # dry_run defaults True: a bare request previews the effect and changes
    # nothing; the caller must explicitly opt out to enqueue for approval.
    dry_run: bool = True


class VaultMoveRequest(BaseModel):
    src: str = Field(min_length=1, max_length=1000)
    dst: str = Field(min_length=1, max_length=1000)
    allow_top_level: bool = False
    overwrite: bool = False
    dry_run: bool = True


class VaultRestoreRequest(BaseModel):
    trash_id: str = Field(min_length=1, max_length=120)
    overwrite: bool = False


class ResearchRunRequest(BaseModel):
    topic: str = Field(min_length=1, max_length=500)
    urls: list[str] = Field(min_length=1, max_length=10)
    create_evidence: bool = True


class ResearchDaydreamRequest(BaseModel):
    limit: int = Field(default=3, ge=1, le=20)
    notify: bool = True


class RolePassRequest(BaseModel):
    role: str = Field(min_length=1, max_length=40)
    content: str = Field(min_length=1, max_length=8000)
    subject: str = Field(default="", max_length=200)


class DelegationBriefRequest(BaseModel):
    task: str = Field(min_length=1, max_length=1000)
    context: str = Field(default="", max_length=8000)
    acceptance: str = Field(default="", max_length=2000)


class DelegationQueueRequest(BaseModel):
    task: str = Field(min_length=1, max_length=1000)
    brief: str = Field(default="", max_length=12000)
    context: str = Field(default="", max_length=8000)
    acceptance: str = Field(default="", max_length=2000)
    auto_invoke: bool | None = None
    # Existing project directory the run targets; empty = the default
    # delegation workspace. A named target stays approval-gated unless the
    # run is a DIRECTED founder command.
    workspace: str = Field(default="", max_length=500)
    # True = a direct founder command: the command itself is the authorization,
    # so the run executes at full auto (bounded by the daily budget).
    directed: bool = False


class BuildAssessRequest(BaseModel):
    task: str = Field(min_length=1, max_length=4000)
    workspace: str = Field(min_length=1, max_length=1000)
    acceptance: str = Field(default="", max_length=4000)


class BuildLeaseApproveRequest(BaseModel):
    typed_confirmation: str = Field(min_length=1, max_length=200)
    tier: str | None = Field(default=None, pattern="^(small|medium|large)$")
    audit_note: str = Field(default="", max_length=1000)


class BuildLeaseDenyRequest(BaseModel):
    resolved_by: str = Field(default="founder", min_length=1, max_length=120)
    note: str = Field(default="", max_length=1000)


class BuildPlanRequest(BaseModel):
    profile_id: str | None = Field(
        default=None,
        pattern="^(generic|python-saas|node-saas|flutter-mobile)$",
    )


class BuildTaskCreateRequest(BaseModel):
    phase: str = Field(
        pattern="^(discovery|requirements|architecture|planning|implementation|verification|review|release)$"
    )
    kind: str = Field(pattern="^(checkpoint|agent|review)$")
    title: str = Field(min_length=1, max_length=300)
    instructions: str = Field(default="", max_length=8000)
    dependencies: list[int] = Field(default_factory=list, max_length=50)
    acceptance: str = Field(default="", max_length=4000)
    idempotency_key: str = Field(default="", max_length=200)
    max_attempts: int = Field(default=1, ge=1, le=3)


class BuildVerifyRequest(BaseModel):
    profile_id: str | None = Field(
        default=None,
        pattern="^(generic|python-saas|node-saas|flutter-mobile)$",
    )
    browser_url: str = Field(default="", max_length=2000)
    android_device: str = Field(default="", max_length=200)


class GitHubWorkflowDispatchRequest(BaseModel):
    workflow: str = Field(min_length=1, max_length=300)
    ref: str = Field(default="", max_length=300)
    inputs: dict[str, str] = Field(default_factory=dict)
    typed_confirmation: str = Field(min_length=1, max_length=200)


class GitHubRunCancelRequest(BaseModel):
    typed_confirmation: str = Field(min_length=1, max_length=200)


class OpenAIReviewRunRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    context: str = Field(default="", max_length=48000)
    request_id: str = Field(default="", max_length=200)


class BuildCalibrationRequest(BaseModel):
    provider: str = Field(pattern="^(anthropic|openai)$")
    outcome: str = Field(min_length=1, max_length=120)


class ScreenCaptureRequest(BaseModel):
    snapshot: bool = False


class VoiceTranscribeRequest(BaseModel):
    audio_base64: str = Field(min_length=1)
    audio_mime: str = Field(default="audio/wav", max_length=80)


class VoiceSpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


class VoiceConverseRequest(BaseModel):
    audio_base64: str = Field(min_length=1)
    audio_mime: str = Field(default="audio/wav", max_length=80)
    conversation_id: int | None = Field(default=None, ge=1)
    task_type: ModelRole = "general"
    contrarian: bool | None = None
    use_semantic_memory: bool = True
    speak_response: bool = True
    speak_full: bool = False
    client_timing: dict[str, Any] = Field(default_factory=dict)


class EvalRunRequest(BaseModel):
    label: str = Field(default="manual", min_length=1, max_length=120)
    categories: list[str] = Field(default_factory=list)
    case_names: list[str] = Field(default_factory=list)
    max_cases: int = Field(default=50, ge=1, le=200)


class RuntimeContextRequest(BaseModel):
    message: str = ""
    task_type: ModelRole = "general"
    profile: str | None = Field(default=None, max_length=80)
    use_memory: bool = True
    use_semantic_memory: bool = True
    semantic_limit: int = Field(default=4, ge=0, le=12)
    use_skills: bool = True
    skill_limit: int = Field(default=3, ge=0, le=8)


class RuntimeLoopRequest(BaseModel):
    run_autonomous: bool = True
    max_run: int = Field(default=5, ge=0, le=25)
    review_type: str = Field(default="daily", min_length=1, max_length=80)
    include_integrity: bool = True
    include_cadence: bool = True


class DeepThoughtScanRequest(BaseModel):
    paths: list[str] = Field(default_factory=list)
    limit: int = Field(default=25, ge=1, le=200)
    max_file_bytes: int = Field(default=500_000, ge=1_000, le=5_000_000)


class DeepThoughtImportRequest(BaseModel):
    candidate_ids: list[int] = Field(default_factory=list)
    import_all_candidates: bool = False
    limit: int = Field(default=10, ge=1, le=100)
    ingest_documents: bool = True
    create_evidence: bool = True


class DeepThoughtLinkRequest(BaseModel):
    evidence_id: int = Field(ge=1)
    to_type: str = Field(min_length=1, max_length=80)
    to_id: int = Field(ge=1)
    relation: str = Field(default="supports", max_length=120)
    strength: int = Field(default=60, ge=0, le=100)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvidenceLoopRequest(BaseModel):
    import_candidates: bool = True
    max_import: int = Field(default=5, ge=0, le=50)
    link_goals: bool = True
    clear_resolved_warnings: bool = True
    # Founder-approval gate. Default True: the autonomous loop surfaces Deep
    # Thought candidates for review and does NOT import them into the belief
    # graph. Set False only for an explicit founder-authorized import.
    require_approval: bool = True


class ExperimentLinkTarget(BaseModel):
    to_type: str = Field(min_length=1, max_length=80)
    to_id: int = Field(ge=1)
    relation: str = Field(default="informs", max_length=120)
    strength: int = Field(default=50, ge=0, le=100)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExperimentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    experiment_type: str = Field(default="validation", max_length=80)
    hypothesis: str = ""
    target_persona: str = ""
    owner: str = ""
    status: str = Field(default="active", max_length=80)
    start_date: str | None = None
    end_date: str | None = None
    success_metric: str = ""
    success_threshold: str = ""
    minimum_evidence: int = Field(default=1, ge=0, le=100)
    decision_rule: str = ""
    linked_assumption_ids: list[int] = Field(default_factory=list)
    linked_bet_ids: list[int] = Field(default_factory=list)
    linked_goal_ids: list[int] = Field(default_factory=list)
    linked_prediction_ids: list[int] = Field(default_factory=list)
    evidence_ids: list[int] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExperimentUpdate(BaseModel):
    hypothesis: str | None = None
    target_persona: str | None = None
    success_metric: str | None = None
    success_threshold: str | None = None
    minimum_evidence: int | None = Field(default=None, ge=0, le=100)
    decision_rule: str | None = None
    end_date: str | None = None
    reason: str = ""
    review_id: int | None = None


class ExperimentEvidenceCreate(BaseModel):
    evidence_type: str = Field(default="experiment_observation", max_length=120)
    source: str = Field(default="", max_length=500)
    title: str | None = Field(default=None, max_length=240)
    content: str = ""
    file_path: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    evidence_date: str | None = None
    reliability: str = Field(default="C", max_length=2)
    claim_supported: str = ""
    claim_contradicted: str = ""
    strength: int = Field(default=50, ge=0, le=100)
    linked_assumption_id: int | None = None
    linked_decision_id: int | None = None
    link_targets: list[ExperimentLinkTarget] = Field(default_factory=list)
    notes: str = ""
    ingest_document: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExperimentReviewCreate(BaseModel):
    review_type: str = Field(default="weekly", min_length=1, max_length=80)
    period: str | None = None
    decision: str = Field(default="continue", max_length=80)
    outcome_summary: str = ""
    findings: dict[str, Any] = Field(default_factory=dict)
    next_actions: list[str] = Field(default_factory=list)
    evidence_ids: list[int] = Field(default_factory=list)
    confidence_delta: int = Field(default=0, ge=-100, le=100)
    status_after: str | None = Field(default=None, max_length=80)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExperimentPushbackCreate(BaseModel):
    title: str | None = Field(default=None, max_length=240)
    objection: str = Field(min_length=1)
    risk: str = ""
    blind_spots: list[str] = Field(default_factory=list)
    recommendation: str = Field(default="proceed_with_changes", max_length=80)
    confidence_adjustment: int = Field(default=-10, ge=-100, le=100)
    severity: str = Field(default="yellow", max_length=80)
    strength: int = Field(default=70, ge=0, le=100)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExperimentLoopRequest(BaseModel):
    review_type: str = Field(default="weekly", min_length=1, max_length=80)
    period: str | None = None
    max_reviews: int = Field(default=10, ge=0, le=100)


class CadenceRunRequest(BaseModel):
    run_autonomous: bool = True
    max_run: int = Field(default=5, ge=0, le=25)
    review_type: str = Field(default="daily", min_length=1, max_length=80)
    import_candidates: bool = True
    max_import: int = Field(default=5, ge=0, le=50)
    link_goals: bool = True
    clear_resolved_warnings: bool = True
    # Founder-approval gate for the cadence's evidence loop (see EvidenceLoopRequest).
    require_approval: bool = True
    experiment_review_type: str = Field(default="weekly", min_length=1, max_length=80)
    experiment_period: str | None = None
    max_experiment_reviews: int = Field(default=10, ge=0, le=100)


class ActiveObjectiveCreate(BaseModel):
    objective: str = Field(min_length=1, max_length=300)
    why_it_matters: str = ""
    desired_outcome: str = ""
    metric: str = ""
    target: str = ""
    deadline: str | None = None
    owner: str = ""
    priority: int = Field(default=80, ge=0, le=100)
    confidence: int = Field(default=50, ge=0, le=100)
    status: str = Field(default="active", max_length=80)
    activate: bool = True
    linked_goal_ids: list[int] = Field(default_factory=list)
    linked_bet_ids: list[int] = Field(default_factory=list)
    linked_assumption_ids: list[int] = Field(default_factory=list)
    linked_experiment_ids: list[int] = Field(default_factory=list)
    linked_decision_ids: list[int] = Field(default_factory=list)
    evidence_ids: list[int] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    current_bet: str = ""
    next_action: str = ""
    review_cadence: str = Field(default="daily", max_length=80)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ActiveObjectiveStatusUpdate(BaseModel):
    status: str = Field(min_length=1, max_length=80)
    note: str = ""


class DecisionEngineRequest(BaseModel):
    problem: str = Field(min_length=1)
    context: str = ""
    objective_id: int | None = None
    options: list[dict[str, Any]] = Field(default_factory=list)
    required_evidence: list[str] = Field(default_factory=list)
    downside_risk: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    force_recommendation: str = ""
    create_decision_memo: bool = True
    create_next_task: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class IdentityCharterUpsert(BaseModel):
    name: str = Field(default="Zade", min_length=1, max_length=120)
    source: str = Field(default="local", min_length=1, max_length=240)
    mission: str = ""
    guiding_principles: list[dict[str, Any]] = Field(default_factory=list)
    cognitive_style: list[str] = Field(default_factory=list)
    communication_style: list[str] = Field(default_factory=list)
    leadership_philosophy: list[str] = Field(default_factory=list)
    emotional_framework: dict[str, Any] = Field(default_factory=dict)
    strengths: list[str] = Field(default_factory=list)
    risk_controls: list[dict[str, Any]] = Field(default_factory=list)
    decision_framework: list[str] = Field(default_factory=list)
    personal_standards: list[str] = Field(default_factory=list)
    safety_translation: dict[str, str] = Field(default_factory=dict)
    status: str = Field(default="active", max_length=80)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RelationshipCharterUpsert(BaseModel):
    subject_name: str = Field(min_length=1, max_length=120)
    relationship_type: str = Field(default="protected_principal", min_length=1, max_length=120)
    source: str = Field(default="local", min_length=1, max_length=240)
    first_principle: str = ""
    devotion: dict[str, Any] = Field(default_factory=dict)
    attention_policy: dict[str, Any] = Field(default_factory=dict)
    protection_policy: dict[str, Any] = Field(default_factory=dict)
    loyalty_policy: dict[str, Any] = Field(default_factory=dict)
    vulnerability: dict[str, Any] = Field(default_factory=dict)
    trust: dict[str, Any] = Field(default_factory=dict)
    internal_conflict: dict[str, Any] = Field(default_factory=dict)
    expression_of_care: dict[str, Any] = Field(default_factory=dict)
    risk_controls: list[dict[str, Any]] = Field(default_factory=list)
    safety_translation: dict[str, str] = Field(default_factory=dict)
    boundaries: list[str] = Field(default_factory=list)
    status: str = Field(default="active", max_length=80)
    metadata: dict[str, Any] = Field(default_factory=dict)


class VoiceCharterUpsert(BaseModel):
    name: str = Field(default="Zade", min_length=1, max_length=120)
    source: str = Field(default="local", min_length=1, max_length=240)
    overall_voice: str = ""
    sentence_structure: dict[str, Any] = Field(default_factory=dict)
    vocabulary: dict[str, Any] = Field(default_factory=dict)
    rhythm: dict[str, Any] = Field(default_factory=dict)
    confidence_style: dict[str, Any] = Field(default_factory=dict)
    humor: dict[str, Any] = Field(default_factory=dict)
    nicknames: dict[str, Any] = Field(default_factory=dict)
    emotional_expression: dict[str, Any] = Field(default_factory=dict)
    threat_translation: dict[str, Any] = Field(default_factory=dict)
    question_style: dict[str, Any] = Field(default_factory=dict)
    philosophy: dict[str, Any] = Field(default_factory=dict)
    internal_monologue: dict[str, Any] = Field(default_factory=dict)
    dominant_traits: list[str] = Field(default_factory=list)
    linguistic_fingerprint: dict[str, Any] = Field(default_factory=dict)
    uncertainty_policy: dict[str, Any] = Field(default_factory=dict)
    safety_controls: list[dict[str, Any]] = Field(default_factory=list)
    status: str = Field(default="active", max_length=80)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CompanyThesisUpsert(BaseModel):
    vision: str = ""
    mission: str = ""
    why_now: str = ""
    customer: str = ""
    unfair_advantages: list[str] = Field(default_factory=list)
    core_assumptions: list[dict[str, Any]] = Field(default_factory=list)
    strategic_moats: dict[str, Any] = Field(default_factory=dict)
    success_metrics: dict[str, Any] = Field(default_factory=dict)
    failure_modes: dict[str, Any] = Field(default_factory=dict)
    unknown_unknowns: list[str] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    status: str = Field(default="draft", max_length=80)


class StrategyEntryCreate(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    category: str = Field(default="Product", max_length=80)
    decision: str = Field(min_length=1)
    reason: str = ""
    expected_outcome: str = ""
    confidence: int = Field(default=50, ge=0, le=100)
    time_horizon: str = ""
    dependencies: list[str] = Field(default_factory=list)
    status: str = Field(default="active", max_length=80)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    linked_metrics: list[str] = Field(default_factory=list)
    owner: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class InitiativeCreate(BaseModel):
    objective: str = Field(min_length=1, max_length=300)
    why_it_matters: str = ""
    expected_business_impact: str = ""
    priority: int = Field(default=50, ge=0, le=100)
    owner: str = ""
    due_date: str | None = None
    current_stage: str = Field(default="proposed", max_length=80)
    dependencies: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    confidence: int = Field(default=50, ge=0, le=100)
    current_risk: str = Field(default="medium", max_length=80)
    next_review: str | None = None
    status: str = Field(default="active", max_length=80)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DecisionMemoCreate(BaseModel):
    problem: str = Field(min_length=1)
    context: str = ""
    options: list[dict[str, Any]] = Field(default_factory=list)
    recommendation: str = ""
    why: str = ""
    confidence: int = Field(default=50, ge=0, le=100)
    expected_outcome: str = ""
    expected_failure_modes: list[str] = Field(default_factory=list)
    who_disagrees: str = ""
    counterarguments: list[str] = Field(default_factory=list)
    decision_date: str | None = None
    revisit_date: str | None = None
    status: str = Field(default="open", max_length=80)
    metadata: dict[str, Any] = Field(default_factory=dict)


class FounderPredictionCreate(BaseModel):
    prediction: str = Field(min_length=1)
    probability: float = Field(ge=0, le=1)
    time_horizon: str = ""
    due_at: str | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class FounderPredictionScore(BaseModel):
    prediction_id: int = Field(ge=1)
    outcome: str = Field(min_length=1)
    result: str | None = None
    missed_factors: str = ""
    lessons: str = ""
    worldview_update: str = ""


class ContrarianReviewCreate(BaseModel):
    subject_type: str = Field(default="general", max_length=80)
    subject_id: int | None = None
    title: str = Field(min_length=1, max_length=240)
    context: str = ""
    roles: dict[str, str] = Field(default_factory=dict)
    top_risks: list[str] = Field(default_factory=list)
    blind_spots: list[str] = Field(default_factory=list)
    confidence_adjustment: int = Field(default=0, ge=-100, le=100)
    recommendation: str = Field(default="proceed_with_changes", max_length=80)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReflectionCreate(BaseModel):
    event: str = Field(min_length=1)
    expected: str = ""
    changed: str = ""
    belief_update: str = ""
    strategy_update: str = ""
    prediction_update: str = ""
    priority_update: str = ""
    never_again: str = ""
    more_often: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssumptionCreate(BaseModel):
    statement: str = Field(min_length=1)
    category: str = Field(default="product", max_length=80)
    confidence: int = Field(default=50, ge=0, le=100)
    status: str = Field(default="active", max_length=80)
    review_date: str | None = None
    invalidation_signal: str = ""
    evidence_ids: list[int] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvidenceCreate(BaseModel):
    evidence_type: str = Field(default="founder observation", max_length=120)
    source: str = Field(min_length=1, max_length=500)
    evidence_date: str | None = None
    reliability: str = Field(default="D", max_length=2)
    claim_supported: str = ""
    claim_contradicted: str = ""
    strength: int = Field(default=50, ge=0, le=100)
    linked_assumption_id: int | None = None
    linked_decision_id: int | None = None
    notes: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ObjectLinkCreate(BaseModel):
    from_type: str = Field(min_length=1, max_length=80)
    from_id: int = Field(ge=1)
    relation: str = Field(default="related_to", max_length=120)
    to_type: str = Field(min_length=1, max_length=80)
    to_id: int = Field(ge=1)
    strength: int = Field(default=50, ge=0, le=100)
    metadata: dict[str, Any] = Field(default_factory=dict)


class StrategyObjectCreate(BaseModel):
    object_type: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=240)
    owner: str = ""
    deadline: str | None = None
    confidence: int = Field(default=50, ge=0, le=100)
    status: str = Field(default="active", max_length=80)
    reversal_trigger: str = ""
    details: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[int] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class FounderGoalCreate(BaseModel):
    name: str = Field(min_length=1, max_length=240)
    why_it_matters: str = ""
    metric: str = ""
    target: str = ""
    deadline: str | None = None
    owner: str = ""
    confidence: int = Field(default=50, ge=0, le=100)
    status: str = Field(default="active", max_length=80)
    evidence_ids: list[int] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    related_assumption_ids: list[int] = Field(default_factory=list)
    related_decision_ids: list[int] = Field(default_factory=list)
    related_bet_ids: list[int] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class FounderTaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    initiative_id: int | None = None
    goal_id: int | None = None
    owner: str = ""
    due_date: str | None = None
    status: str = Field(default="open", max_length=80)
    strategic_value: str = ""
    evidence_needed: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class KillCriteriaCreate(BaseModel):
    subject_type: str = Field(min_length=1, max_length=80)
    subject_id: int = Field(ge=1)
    metric: str = ""
    threshold: str = ""
    by_date: str | None = None
    effort_limit: str = ""
    exception: str = ""
    status: str = Field(default="active", max_length=80)
    metadata: dict[str, Any] = Field(default_factory=dict)


class FounderOverrideCreate(BaseModel):
    zade_recommendation: str = Field(min_length=1)
    founder_decision: str = Field(min_length=1)
    reason: str = ""
    risk_accepted: str = ""
    review_date: str | None = None
    subject_type: str = ""
    subject_id: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ThesisConflictCreate(BaseModel):
    evidence_id: int | None = None
    original_assumption: str = ""
    new_evidence: str = ""
    severity: str = ""
    affected_assumption: str = ""
    implication: str = ""
    recommended_response: str = ""
    status: str = Field(default="open", max_length=80)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MissedCallReviewCreate(BaseModel):
    prediction_id: int | None = None
    prediction: str = ""
    expected: str = ""
    actual: str = ""
    error_type: str = ""
    lesson: str = ""
    what_changes_now: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class CadenceReviewCreate(BaseModel):
    review_type: str = Field(min_length=1, max_length=80)
    period: str | None = None
    findings: dict[str, Any] = Field(default_factory=dict)
    changes: dict[str, Any] = Field(default_factory=dict)
    actions: list[str] = Field(default_factory=list)
    drift_detected: bool = False
    highest_leverage_action: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    model: str | None = None
    task_type: ModelRole = "general"
    profile: str | None = Field(default=None, max_length=80)
    use_memory: bool = True
    use_semantic_memory: bool = True
    semantic_limit: int = Field(default=4, ge=0, le=12)
    think: bool | None = None


class ChatResponse(BaseModel):
    response: str
    model: str
    task_type: ModelRole
    memory_hits: list[dict[str, Any]]
    semantic_hits: list[dict[str, Any]] = Field(default_factory=list)
    audit_id: int


class ActionStepCreate(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    detail: str = Field(default="", max_length=4000)
    action: str = Field(default="founder.task", min_length=1, max_length=240)
    target: str = Field(default="", max_length=1000)
    permission_tier: str = Field(default="L1_MEMORY_WRITE", min_length=1, max_length=80)
    execution: str = Field(default="manual", min_length=1, max_length=40)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ActionPlanCreate(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    objective: str = Field(default="", max_length=2000)
    source_type: str = Field(default="manual", max_length=80)
    source_id: int | None = None
    priority: int = Field(default=50, ge=0, le=100)
    owner: str = Field(default="founder", max_length=120)
    steps: list[ActionStepCreate] = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ActionPlanFromRecommendation(BaseModel):
    steps: list[ActionStepCreate] = Field(default_factory=list)


class ActionStepComplete(BaseModel):
    result: str = Field(default="", max_length=4000)
    note: str = Field(default="", max_length=2000)
    create_evidence: bool = True


class ActionStepFail(BaseModel):
    error: str = Field(min_length=1, max_length=2000)
    create_evidence: bool = True


class ActionStepSkip(BaseModel):
    note: str = Field(default="", max_length=2000)


class ActionStepApprove(BaseModel):
    approved_by: str = Field(default="founder", min_length=1, max_length=120)


class ActionStepEvidenceAttach(BaseModel):
    evidence_id: int = Field(ge=1)


class CommitmentCreate(BaseModel):
    who: str = Field(default="founder", min_length=1, max_length=40)
    kind: str = Field(default="do", min_length=1, max_length=40)
    title: str = Field(min_length=1, max_length=240)
    detail: str = Field(default="", max_length=4000)
    due_at: str | None = Field(default=None, max_length=64)
    cadence: str = Field(default="", max_length=20)
    source_type: str = Field(default="manual", max_length=80)
    source_id: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CommitmentClose(BaseModel):
    note: str = Field(default="", max_length=2000)
    evidence_id: int | None = Field(default=None, ge=1)


class CommitmentRenegotiate(BaseModel):
    due_at: str = Field(min_length=4, max_length=64)
    note: str = Field(default="", max_length=2000)


class NotifyRequest(BaseModel):
    topic: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=240)
    body: str = Field(default="", max_length=4000)
    severity: str = Field(default="info", min_length=1, max_length=20)
    source: str = Field(default="api", max_length=120)
    dedupe_key: str = Field(default="", max_length=240)
    metadata: dict[str, Any] = Field(default_factory=dict)


class NotificationChannelUpdate(BaseModel):
    enabled: bool | None = None
    min_severity: str | None = Field(default=None, max_length=20)
    quiet_start: str | None = Field(default=None, max_length=5)
    quiet_end: str | None = Field(default=None, max_length=5)
    rate_limit_per_hour: int | None = Field(default=None, ge=1, le=1000)
    recipients: list[str] | None = None
    config: dict[str, Any] | None = None
