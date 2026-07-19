from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cofounder_kernel.build_budget import (
    BuildBudgetExceeded,
    BuildBudgetService,
    ProviderUsage,
    microdollars,
)
from cofounder_kernel.build_store import BuildStore
from cofounder_kernel.build_types import (
    BuildAssessment,
    BuildTier,
    LeaseLimits,
    PricingSnapshot,
)
from cofounder_kernel.db import KernelDatabase


SMALL_LIMITS = LeaseLimits(1_000_000, 120_000, 16_000, 6, 7200)
PRICING = PricingSnapshot(
    provider="anthropic",
    model="claude-opus-4-8",
    base_input_per_mtok="5",
    cache_write_5m_per_mtok="6.25",
    cache_write_1h_per_mtok="10",
    cache_read_per_mtok="0.5",
    output_per_mtok="25",
    review_after="2026-08-31",
)


def assessment() -> BuildAssessment:
    return BuildAssessment(
        id=None,
        task="Build a feature",
        acceptance="Tests pass",
        workspace="C:/workspace",
        repo_fingerprint="abc",
        deterministic_score=20,
        local_adjustment=0,
        final_score=20,
        confidence=0.8,
        recommended_tier=BuildTier.SMALL,
        dimensions={},
        floor_rules=(),
        evidence={},
        unknowns=(),
        local_work=(),
        cloud_reasons=(),
        created_at="2026-07-18T12:00:00+00:00",
    )


def make_budget(
    tmp_path: Path,
    *,
    limits: LeaseLimits = SMALL_LIMITS,
    pricing: PricingSnapshot = PRICING,
) -> tuple[BuildBudgetService, BuildStore, int]:
    database = KernelDatabase(tmp_path / "kernel.sqlite")
    database.migrate()
    store = BuildStore(database)
    session = store.create_session(assessment())
    store.create_lease(
        session.id,
        BuildTier.SMALL,
        limits,
        provider="anthropic",
        model="claude-opus-4-8",
        approval_request_id=1,
    )
    budget = BuildBudgetService(
        store,
        pricing,
        clock=lambda: datetime(2026, 7, 18, 12, 30, tzinfo=UTC),
    )
    return budget, store, session.id


def test_microdollars_rounds_up_fractional_units() -> None:
    assert microdollars(1, PRICING.cache_read_per_mtok) == 1
    assert microdollars(1_000_000, PRICING.output_per_mtok) == 25_000_000


def test_reservation_refuses_before_send_when_any_limit_would_be_exceeded(
    tmp_path: Path,
) -> None:
    budget, store, session_id = make_budget(tmp_path)

    with pytest.raises(BuildBudgetExceeded, match="output_tokens"):
        budget.reserve(
            session_id=session_id,
            request_id="r1",
            input_upper=1000,
            max_output=20_000,
            cache_mode="write_1h",
        )

    lease = store.get_active_lease(session_id)
    assert lease is not None
    assert lease.state == "exhausted"
    assert store.list_usage(lease.id) == []


def test_missing_usage_charges_reserved_maximum(tmp_path: Path) -> None:
    budget, _store, session_id = make_budget(tmp_path)
    reservation = budget.reserve(
        session_id=session_id,
        request_id="r1",
        input_upper=1000,
        max_output=1000,
        cache_mode="write_1h",
    )

    event = budget.settle(reservation.id, usage=None)

    assert event.status == "conservative_settlement"
    assert event.settled_microdollars == reservation.reserved_microdollars
    assert event.input_tokens == reservation.input_upper_tokens
    assert event.output_tokens == reservation.max_output_tokens
    lease = budget.active_lease(session_id)
    assert lease.reserved_microdollars == 0
    assert lease.actual_microdollars == reservation.reserved_microdollars


def test_exact_usage_is_priced_by_reported_category(tmp_path: Path) -> None:
    budget, _store, session_id = make_budget(tmp_path)
    reservation = budget.reserve(
        session_id=session_id,
        request_id="r1",
        input_upper=2000,
        max_output=1000,
        cache_mode="write_1h",
    )

    event = budget.settle(
        reservation.id,
        ProviderUsage(
            input_tokens=500,
            cache_write_5m_tokens=200,
            cache_write_1h_tokens=0,
            cache_read_tokens=300,
            output_tokens=100,
        ),
    )

    assert event.status == "settled"
    assert event.settled_microdollars == 6400
    assert budget.active_lease(session_id).actual_input_tokens == 1000


def test_ambiguous_timeout_pauses_without_releasing_reservation(tmp_path: Path) -> None:
    budget, _store, session_id = make_budget(tmp_path)
    reservation = budget.reserve(
        session_id=session_id,
        request_id="r1",
        input_upper=1000,
        max_output=1000,
        cache_mode="none",
    )

    event = budget.mark_uncertain(reservation.id, "timeout after headers")

    assert event.status == "uncertain_spend"
    lease = budget.active_lease(session_id)
    assert lease.state == "paused"
    assert lease.reserved_microdollars == reservation.reserved_microdollars


def test_proven_unsent_request_releases_all_reserved_counters(tmp_path: Path) -> None:
    budget, store, session_id = make_budget(tmp_path)
    reservation = budget.reserve(
        session_id=session_id,
        request_id="r1",
        input_upper=1000,
        max_output=1000,
        cache_mode="none",
    )

    budget.release_pre_send(reservation.id)

    lease = budget.active_lease(session_id)
    assert lease.reserved_input_tokens == 0
    assert lease.reserved_output_tokens == 0
    assert lease.reserved_microdollars == 0
    assert lease.cloud_turns == 0
    assert store.list_usage(lease.id) == []


def test_settled_request_id_cannot_trigger_an_automatic_retry(tmp_path: Path) -> None:
    budget, _store, session_id = make_budget(tmp_path)
    reservation = budget.reserve(
        session_id=session_id,
        request_id="r1",
        input_upper=1000,
        max_output=1000,
        cache_mode="none",
    )
    budget.settle(reservation.id, ProviderUsage(input_tokens=100, output_tokens=100))

    with pytest.raises(BuildBudgetExceeded, match="not retried automatically"):
        budget.reserve(
            session_id=session_id,
            request_id="r1",
            input_upper=1000,
            max_output=1000,
            cache_mode="none",
        )


def test_warning_state_includes_open_reservations(tmp_path: Path) -> None:
    limits = LeaseLimits(10_000_000, 10_000, 10_000, 6, 7200)
    budget, _store, session_id = make_budget(tmp_path, limits=limits)

    budget.reserve(
        session_id=session_id,
        request_id="r1",
        input_upper=8_000,
        max_output=100,
        cache_mode="none",
    )

    assert budget.active_lease(session_id).state == "warning"


def test_stale_pricing_fails_closed_without_reservation(tmp_path: Path) -> None:
    stale = PricingSnapshot(
        provider="anthropic",
        model="claude-opus-4-8",
        base_input_per_mtok="5",
        cache_write_5m_per_mtok="6.25",
        cache_write_1h_per_mtok="10",
        cache_read_per_mtok="0.5",
        output_per_mtok="25",
        review_after="2026-07-01",
    )
    budget, store, session_id = make_budget(tmp_path, pricing=stale)

    with pytest.raises(BuildBudgetExceeded, match="pricing_stale"):
        budget.reserve(
            session_id=session_id,
            request_id="r1",
            input_upper=100,
            max_output=100,
            cache_mode="none",
        )

    lease = store.get_active_lease(session_id)
    assert lease is not None
    assert store.list_usage(lease.id) == []


def test_expired_lease_is_closed_durably(tmp_path: Path) -> None:
    budget, store, session_id = make_budget(tmp_path)
    lease = store.get_active_lease(session_id)
    assert lease is not None
    with store.database.connect() as connection:
        connection.execute(
            "UPDATE build_leases SET expires_at = ? WHERE id = ?",
            ("2026-07-18T12:00:00+00:00", lease.id),
        )

    with pytest.raises(BuildBudgetExceeded, match="expiration"):
        budget.reserve(
            session_id=session_id,
            request_id="r1",
            input_upper=100,
            max_output=100,
            cache_mode="none",
        )

    assert store.get_active_lease(session_id) is None
    assert store.get_lease(lease.id).state == "expired"  # type: ignore[union-attr]


def test_two_thread_reservation_race_has_exactly_one_winner(tmp_path: Path) -> None:
    limits = LeaseLimits(10_000_000, 100_000, 15_000, 6, 7200)
    budget, store, session_id = make_budget(tmp_path, limits=limits)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def reserve(request_id: str) -> None:
        barrier.wait()
        try:
            budget.reserve(
                session_id=session_id,
                request_id=request_id,
                input_upper=1000,
                max_output=10_000,
                cache_mode="none",
            )
        except BuildBudgetExceeded:
            outcome = "rejected"
        else:
            outcome = "reserved"
        with lock:
            outcomes.append(outcome)

    threads = [
        threading.Thread(target=reserve, args=("r1",)),
        threading.Thread(target=reserve, args=("r2",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert sorted(outcomes) == ["rejected", "reserved"]
    lease = store.get_active_lease(session_id)
    assert lease is not None
    assert len(store.list_usage(lease.id)) == 1
