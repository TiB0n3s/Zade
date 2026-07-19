from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from cofounder_kernel.build_types import (
    BuildAssessment,
    BuildLease,
    BuildSession,
    BuildTier,
    CloudUsageEvent,
    LeaseLimits,
    PricingSnapshot,
    UsageReservation,
)
from cofounder_kernel.config import (
    AnthropicPricingConfig,
    BuildConfig,
    OpenAIReviewConfig,
    load_config,
)


def test_default_build_tiers_match_the_approved_envelopes() -> None:
    config = BuildConfig()

    assert config.limits(BuildTier.SMALL) == LeaseLimits(
        dollar_micro=1_000_000,
        input_tokens=120_000,
        output_tokens=16_000,
        cloud_turns=6,
        duration_seconds=2 * 60 * 60,
    )
    assert config.limits(BuildTier.MEDIUM) == LeaseLimits(
        dollar_micro=3_000_000,
        input_tokens=400_000,
        output_tokens=40_000,
        cloud_turns=16,
        duration_seconds=4 * 60 * 60,
    )
    assert config.limits(BuildTier.LARGE) == LeaseLimits(
        dollar_micro=7_000_000,
        input_tokens=1_000_000,
        output_tokens=80_000,
        cloud_turns=32,
        duration_seconds=8 * 60 * 60,
    )


def test_build_domain_records_are_immutable() -> None:
    assessment = BuildAssessment(
        id=None,
        task="Build an app",
        acceptance="Tests pass",
        workspace="C:/workspace",
        repo_fingerprint="abc",
        deterministic_score=20,
        local_adjustment=5,
        final_score=25,
        confidence=0.9,
        recommended_tier=BuildTier.SMALL,
        dimensions={"product_surfaces": 5},
        floor_rules=("none",),
        evidence={"files": 4},
        unknowns=(),
        local_work=("inventory",),
        cloud_reasons=(),
        created_at="2026-07-18T00:00:00Z",
    )
    session = BuildSession(
        id=1,
        assessment_id=2,
        work_item_id=None,
        workspace=assessment.workspace,
        repo_fingerprint=assessment.repo_fingerprint,
        phase="approval",
        status="pending",
        checkpoint={},
        created_at=assessment.created_at,
        updated_at=assessment.created_at,
    )
    limits = LeaseLimits(1_000_000, 120_000, 16_000, 6, 7200)
    lease = BuildLease(
        id=3,
        session_id=session.id,
        version=1,
        tier=BuildTier.SMALL,
        provider="anthropic",
        model="claude-opus-4-8",
        limits=limits,
        state="active",
        approval_request_id=4,
        actual_input_tokens=0,
        actual_output_tokens=0,
        actual_microdollars=0,
        reserved_input_tokens=0,
        reserved_output_tokens=0,
        reserved_microdollars=0,
        cloud_turns=0,
        started_at=assessment.created_at,
        expires_at="2026-07-18T02:00:00Z",
    )
    pricing = PricingSnapshot(
        "anthropic",
        lease.model,
        "5",
        "6.25",
        "10",
        "0.5",
        "25",
        "2026-08-31",
    )
    reservation = UsageReservation(
        id=5,
        lease_id=lease.id,
        request_id="request-1",
        turn_number=1,
        input_upper_tokens=1000,
        max_output_tokens=500,
        reserved_microdollars=25_000,
        pricing=pricing,
        status="reserved",
        created_at=assessment.created_at,
    )
    usage = CloudUsageEvent(
        id=reservation.id,
        lease_id=lease.id,
        request_id=reservation.request_id,
        turn_number=1,
        status="settled",
        input_tokens=800,
        cache_write_5m_tokens=0,
        cache_write_1h_tokens=0,
        cache_read_tokens=0,
        output_tokens=200,
        reserved_microdollars=reservation.reserved_microdollars,
        settled_microdollars=9_000,
        pricing=pricing,
        created_at=assessment.created_at,
        settled_at="2026-07-18T00:01:00Z",
    )

    assert usage.lease_id == lease.id
    with pytest.raises(FrozenInstanceError):
        session.phase = "implementation"  # type: ignore[misc]


def test_pricing_snapshot_expires_fail_closed() -> None:
    pricing = AnthropicPricingConfig(review_after="2026-01-01")

    assert pricing.is_current(at="2025-12-31T23:59:59Z") is True
    assert pricing.is_current(at="2026-01-01T23:59:59Z") is True
    assert pricing.is_current(at="2026-01-02T00:00:00Z") is False


def test_load_config_reads_custom_build_limits_and_pricing(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[build]
enabled = true
warning_percent = 75
provider_overhead_tokens = 2048

[build.tiers.small]
dollar_micro = 900000
input_tokens = 100000
output_tokens = 12000
cloud_turns = 4
duration_seconds = 3600

[build.tiers.medium]
dollar_micro = 2500000
input_tokens = 300000
output_tokens = 30000
cloud_turns = 12
duration_seconds = 10800

[build.tiers.large]
dollar_micro = 6000000
input_tokens = 800000
output_tokens = 70000
cloud_turns = 28
duration_seconds = 21600

[build.anthropic_pricing]
model = "claude-opus-4-8"
base_input_per_mtok = "5"
cache_write_5m_per_mtok = "6.25"
cache_write_1h_per_mtok = "10"
cache_read_per_mtok = "0.5"
output_per_mtok = "25"
review_after = "2026-09-30"
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.build.warning_percent == 75
    assert config.build.provider_overhead_tokens == 2048
    assert config.build.limits(BuildTier.SMALL).dollar_micro == 900_000
    assert config.build.limits(BuildTier.LARGE).cloud_turns == 28
    assert str(config.build.anthropic_pricing.cache_write_5m_per_mtok) == "6.25"


def test_load_config_rejects_non_monotonic_build_tiers(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[build.tiers.small]
input_tokens = 500000

[build.tiers.medium]
input_tokens = 100000
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="monotonic"):
        load_config(config_path)


def test_openai_reviewer_is_disabled_and_cost_balanced_by_default() -> None:
    config = OpenAIReviewConfig()

    assert config.enabled is False
    assert config.model == "gpt-5.6-terra"
    assert config.pricing.snapshot().provider == "openai"
    assert str(config.pricing.base_input_per_mtok) == "2.5"
    assert str(config.pricing.output_per_mtok) == "15"


def test_load_config_reads_openai_review_without_enabling_fallback(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[openai_review]
enabled = true
model = "gpt-5.6-luna"
max_output_tokens = 1200
reasoning_effort = "low"

[openai_review.pricing]
model = "gpt-5.6-luna"
base_input_per_mtok = "1"
cache_write_5m_per_mtok = "1"
cache_write_1h_per_mtok = "1"
cache_read_per_mtok = "1"
output_per_mtok = "6"
review_after = "2026-09-30"
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.openai_review.enabled is True
    assert config.openai_review.model == "gpt-5.6-luna"
    assert config.openai_review.max_output_tokens == 1200
    assert config.openai_review.reasoning_effort == "low"
    assert config.ollama.cloud_fallback == "never"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dollar_micro", -1),
        ("input_tokens", 0),
        ("output_tokens", 0),
        ("cloud_turns", 0),
        ("duration_seconds", 0),
    ],
)
def test_load_config_rejects_invalid_small_tier_values(
    tmp_path: Path, field: str, value: int
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"[build.tiers.small]\n{field} = {value}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=field):
        load_config(config_path)
