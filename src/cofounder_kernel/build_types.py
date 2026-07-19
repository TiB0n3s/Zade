from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Sequence


class BuildTier(StrEnum):
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


class BuildTaskKind(StrEnum):
    CHECKPOINT = "checkpoint"
    AGENT = "agent"
    COMMAND = "command"
    VERIFICATION = "verification"
    GITHUB = "github"
    REVIEW = "review"


class BuildTaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


BUILD_PHASES: tuple[str, ...] = (
    "assessment",
    "approval",
    "discovery",
    "requirements",
    "architecture",
    "planning",
    "implementation",
    "verification",
    "review",
    "release",
    "complete",
)


@dataclass(frozen=True)
class BuildTask:
    id: int
    session_id: int
    phase: str
    position: int
    kind: BuildTaskKind
    title: str
    payload: dict[str, Any]
    dependencies: tuple[int, ...]
    acceptance: dict[str, Any]
    idempotency_key: str
    status: BuildTaskStatus
    max_attempts: int
    attempt_count: int
    active_run_id: int | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class BuildTaskRun:
    id: int
    task_id: int
    session_id: int
    attempt_number: int
    worker_id: str
    backend: str
    command: tuple[str, ...]
    pid: int | None
    status: BuildTaskStatus
    result: dict[str, Any]
    error: str
    log_path: str
    artifact_ids: tuple[int, ...]
    started_at: str
    finished_at: str | None


@dataclass(frozen=True)
class BuildArtifact:
    id: int
    session_id: int
    task_id: int | None
    run_id: int | None
    kind: str
    uri: str
    metadata: dict[str, Any]
    created_at: str


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

    def __post_init__(self) -> None:
        for field_name in (
            "base_input_per_mtok",
            "cache_write_5m_per_mtok",
            "cache_write_1h_per_mtok",
            "cache_read_per_mtok",
            "output_per_mtok",
        ):
            object.__setattr__(self, field_name, Decimal(str(getattr(self, field_name))))


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
class UsageReservation:
    id: int
    lease_id: int
    request_id: str
    turn_number: int
    input_upper_tokens: int
    max_output_tokens: int
    reserved_microdollars: int
    pricing: PricingSnapshot
    status: str
    created_at: str


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
