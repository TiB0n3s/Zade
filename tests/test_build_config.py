from __future__ import annotations

from pathlib import Path

import pytest

from cofounder_kernel.build_types import BuildTier, LeaseLimits
from cofounder_kernel.config import AnthropicPricingConfig, BuildConfig, load_config


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
