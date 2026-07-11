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


class AuthorityEvaluateRequest(BaseModel):
    action: str = Field(min_length=1, max_length=240)
    permission_tier: str = Field(default="L0_READ", min_length=1, max_length=80)
    target: str = Field(default="", max_length=1000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkItemCreate(BaseModel):
    kind: str = Field(default="manual", min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=240)
    detail: str = ""
    action: str = Field(min_length=1, max_length=240)
    target: str = Field(default="", max_length=1000)
    permission_tier: str = Field(default="L0_READ", min_length=1, max_length=80)
    priority: int = Field(default=50, ge=0, le=100)
    source: str = Field(default="api", min_length=1, max_length=240)
    due_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    unique_key: str | None = None


class WorkScanRequest(BaseModel):
    run_autonomous: bool = True
    max_run: int = Field(default=5, ge=0, le=25)


class WorkRunRequest(BaseModel):
    max_items: int = Field(default=5, ge=1, le=25)


class RuntimeRespondRequest(BaseModel):
    message: str = Field(min_length=1)
    model: str | None = None
    task_type: ModelRole = "general"
    proposed_action: str = Field(default="runtime.respond", min_length=1, max_length=240)
    permission_tier: str = Field(default="L0_READ", min_length=1, max_length=80)
    target: str = Field(default="local_runtime", max_length=1000)
    use_memory: bool = True
    use_semantic_memory: bool = True
    semantic_limit: int = Field(default=4, ge=0, le=12)
    think: bool | None = None


class RuntimeContextRequest(BaseModel):
    message: str = ""
    use_memory: bool = True
    use_semantic_memory: bool = True
    semantic_limit: int = Field(default=4, ge=0, le=12)


class RuntimeLoopRequest(BaseModel):
    run_autonomous: bool = True
    max_run: int = Field(default=5, ge=0, le=25)
    review_type: str = Field(default="daily", min_length=1, max_length=80)
    include_integrity: bool = True
    include_cadence: bool = True


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
