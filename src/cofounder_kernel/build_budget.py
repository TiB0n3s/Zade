"""Hard budget authorization and accounting for paid build requests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_CEILING, Decimal
from typing import Callable

from .build_store import BuildReservationRejected, BuildStore
from .build_types import (
    BuildLease,
    CloudUsageEvent,
    PricingSnapshot,
    UsageReservation,
)


_CACHE_RATE_FIELDS = {
    "none": "base_input_per_mtok",
    "write_5m": "cache_write_5m_per_mtok",
    "write_1h": "cache_write_1h_per_mtok",
    "read": "cache_read_per_mtok",
}


class BuildBudgetExceeded(RuntimeError):
    def __init__(self, field: str, detail: str = ""):
        self.field = field
        self.detail = detail
        super().__init__(f"{field}: {detail}" if detail else field)


@dataclass(frozen=True)
class ProviderUsage:
    input_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_input_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cache_write_5m_tokens
            + self.cache_write_1h_tokens
            + self.cache_read_tokens
        )


def microdollars(tokens: int, usd_per_million: Decimal) -> int:
    if tokens < 0:
        raise ValueError("Token count cannot be negative")
    return int(
        (Decimal(tokens) * usd_per_million).quantize(
            Decimal("1"), rounding=ROUND_CEILING
        )
    )


class BuildBudgetService:
    def __init__(
        self,
        store: BuildStore,
        pricing: PricingSnapshot,
        *,
        warning_percent: int = 80,
        clock: Callable[[], datetime] | None = None,
    ):
        if not 1 <= warning_percent <= 100:
            raise ValueError("warning_percent must be between 1 and 100")
        self.store = store
        self.pricing = pricing
        self.warning_percent = warning_percent
        self._clock = clock or (lambda: datetime.now(UTC))

    def reserve(
        self,
        *,
        session_id: int,
        request_id: str,
        input_upper: int,
        max_output: int,
        cache_mode: str,
    ) -> UsageReservation:
        now = self._now()
        self._validate_pricing(now.date())
        if cache_mode not in _CACHE_RATE_FIELDS:
            raise BuildBudgetExceeded("cache_mode", f"unsupported mode {cache_mode!r}")
        if input_upper <= 0:
            raise BuildBudgetExceeded("input_tokens", "upper bound must be positive")
        if max_output <= 0:
            raise BuildBudgetExceeded("output_tokens", "maximum must be positive")
        request_id = request_id.strip()
        if not request_id:
            raise BuildBudgetExceeded("request_id", "must not be blank")

        input_rate = getattr(self.pricing, _CACHE_RATE_FIELDS[cache_mode])
        reserved_cost = microdollars(input_upper, input_rate) + microdollars(
            max_output, self.pricing.output_per_mtok
        )
        try:
            return self.store.create_reservation(
                session_id,
                request_id=request_id,
                input_upper_tokens=input_upper,
                max_output_tokens=max_output,
                reserved_microdollars=reserved_cost,
                cache_mode=cache_mode,
                pricing=self.pricing,
                warning_percent=self.warning_percent,
                now=now.isoformat(),
            )
        except BuildReservationRejected as exc:
            if exc.field in {
                "input_tokens",
                "output_tokens",
                "microdollars",
                "cloud_turns",
                "expiration",
            }:
                lease = self.store.get_active_lease(session_id)
                if lease is not None and lease.state in {"active", "warning"}:
                    if exc.field == "expiration":
                        self.store.expire_lease(lease.id)
                    else:
                        self.store.pause_lease(lease.id)
            raise BuildBudgetExceeded(exc.field, exc.detail) from exc

    def settle(
        self,
        reservation_id: int,
        usage: ProviderUsage | None,
    ) -> CloudUsageEvent:
        reservation = self.store.get_reservation(reservation_id)
        if reservation is None:
            raise ValueError(f"Usage reservation not found: {reservation_id}")
        now = self._now().isoformat()
        if usage is None:
            return self.store.settle_reservation(
                reservation_id,
                input_tokens=reservation.input_upper_tokens,
                cache_write_5m_tokens=0,
                cache_write_1h_tokens=0,
                cache_read_tokens=0,
                output_tokens=reservation.max_output_tokens,
                settled_microdollars=reservation.reserved_microdollars,
                status="conservative_settlement",
                warning_percent=self.warning_percent,
                now=now,
            )

        self._validate_usage(usage)
        settled_cost = self._usage_cost(usage, reservation.pricing)
        provider_overage = (
            usage.total_input_tokens > reservation.input_upper_tokens
            or usage.output_tokens > reservation.max_output_tokens
            or settled_cost > reservation.reserved_microdollars
        )
        return self.store.settle_reservation(
            reservation_id,
            input_tokens=usage.input_tokens,
            cache_write_5m_tokens=usage.cache_write_5m_tokens,
            cache_write_1h_tokens=usage.cache_write_1h_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            output_tokens=usage.output_tokens,
            settled_microdollars=settled_cost,
            status="provider_overage" if provider_overage else "settled",
            warning_percent=self.warning_percent,
            now=now,
            pause_lease=provider_overage,
        )

    def release_pre_send(self, reservation_id: int) -> None:
        self.store.release_reservation(reservation_id)

    def mark_uncertain(self, reservation_id: int, reason: str) -> CloudUsageEvent:
        reason = " ".join(reason.split())
        if not reason:
            raise ValueError("An uncertainty reason is required")
        return self.store.mark_reservation_uncertain(reservation_id, reason=reason)

    def active_lease(self, session_id: int) -> BuildLease:
        lease = self.store.get_active_lease(session_id)
        if lease is None:
            raise BuildBudgetExceeded("lease", "no approved lease")
        return lease

    def request_upgrade_summary(self, session_id: int, reason: str) -> dict[str, object]:
        lease = self.active_lease(session_id)
        return {
            "session_id": session_id,
            "lease_id": lease.id,
            "tier": lease.tier.value,
            "state": lease.state,
            "reason": " ".join(reason.split()),
            "actual": {
                "input_tokens": lease.actual_input_tokens,
                "output_tokens": lease.actual_output_tokens,
                "microdollars": lease.actual_microdollars,
                "cloud_turns": lease.cloud_turns,
            },
            "reserved": {
                "input_tokens": lease.reserved_input_tokens,
                "output_tokens": lease.reserved_output_tokens,
                "microdollars": lease.reserved_microdollars,
            },
            "limits": {
                "input_tokens": lease.limits.input_tokens,
                "output_tokens": lease.limits.output_tokens,
                "microdollars": lease.limits.dollar_micro,
                "cloud_turns": lease.limits.cloud_turns,
            },
        }

    def _validate_pricing(self, today: date) -> None:
        try:
            review_after = date.fromisoformat(self.pricing.review_after)
        except ValueError as exc:
            raise BuildBudgetExceeded("pricing_invalid", "review_after is not ISO") from exc
        if today > review_after:
            raise BuildBudgetExceeded(
                "pricing_stale", f"review was due {self.pricing.review_after}"
            )

    @staticmethod
    def _validate_usage(usage: ProviderUsage) -> None:
        for name in (
            "input_tokens",
            "cache_write_5m_tokens",
            "cache_write_1h_tokens",
            "cache_read_tokens",
            "output_tokens",
        ):
            if getattr(usage, name) < 0:
                raise ValueError(f"Provider usage {name} cannot be negative")

    @staticmethod
    def _usage_cost(usage: ProviderUsage, pricing: PricingSnapshot) -> int:
        return sum(
            (
                microdollars(usage.input_tokens, pricing.base_input_per_mtok),
                microdollars(
                    usage.cache_write_5m_tokens, pricing.cache_write_5m_per_mtok
                ),
                microdollars(
                    usage.cache_write_1h_tokens, pricing.cache_write_1h_per_mtok
                ),
                microdollars(usage.cache_read_tokens, pricing.cache_read_per_mtok),
                microdollars(usage.output_tokens, pricing.output_per_mtok),
            )
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
