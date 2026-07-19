from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


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
