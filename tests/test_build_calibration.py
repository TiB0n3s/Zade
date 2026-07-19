from __future__ import annotations

from pathlib import Path

from cofounder_kernel.build_calibration import (
    BuildCalibrationService,
    ManagedAgentsReadinessService,
)
from cofounder_kernel.build_store import BuildStore
from cofounder_kernel.build_types import BuildAssessment, BuildTier, LeaseLimits
from cofounder_kernel.db import KernelDatabase


def make_store(tmp_path: Path, *, score: int = 25):
    database = KernelDatabase(tmp_path / "kernel.sqlite")
    database.migrate()
    store = BuildStore(database)
    session = store.create_session(
        BuildAssessment(
            id=None,
            task="Build feature",
            acceptance="Tests pass",
            workspace=str(tmp_path / "workspace"),
            repo_fingerprint="fingerprint",
            deterministic_score=score,
            local_adjustment=0,
            final_score=score,
            confidence=0.8,
            recommended_tier=BuildTier.MEDIUM,
            dimensions={},
            floor_rules=(),
            evidence={},
            unknowns=(),
            local_work=(),
            cloud_reasons=(),
            created_at="2026-07-19T12:00:00+00:00",
        )
    )
    lease = store.create_lease(
        session.id,
        BuildTier.MEDIUM,
        LeaseLimits(3_000_000, 400_000, 40_000, 16, 14_400),
        provider="anthropic",
        model="claude-opus-4-8",
        approval_request_id=1,
    )
    return store, session, lease


def test_calibration_records_prediction_vs_actual_and_survives_reopen(tmp_path: Path) -> None:
    store, session, lease = make_store(tmp_path)
    with store.database.connect() as connection:
        connection.execute(
            """
            UPDATE build_leases
            SET actual_input_tokens = 30000, actual_output_tokens = 3000,
                actual_microdollars = 300000, cloud_turns = 2
            WHERE id = ?
            """,
            (lease.id,),
        )
    service = BuildCalibrationService(store)

    calibration = service.record(session.id, provider="anthropic", outcome="success")
    reopened = BuildCalibrationService(BuildStore(store.database)).list(session_id=session.id)

    assert calibration.predicted_tier is BuildTier.MEDIUM
    assert calibration.actual_input_tokens == 30_000
    assert calibration.input_utilization == 0.075
    assert calibration.recommendation == "consider_lower_tier"
    assert reopened == [calibration]


def test_calibration_is_advisory_and_never_changes_lease(tmp_path: Path) -> None:
    store, session, lease = make_store(tmp_path, score=60)
    before = store.get_lease(lease.id)

    calibration = BuildCalibrationService(store).record(
        session.id, provider="anthropic", outcome="failed"
    )

    assert calibration.recommendation == "review_failure_before_resizing"
    assert store.get_lease(lease.id) == before


def test_managed_agents_remains_readiness_only(tmp_path: Path) -> None:
    store, session, _lease = make_store(tmp_path)
    BuildCalibrationService(store).record(
        session.id, provider="anthropic", outcome="success"
    )
    service = ManagedAgentsReadinessService(store, minimum_calibrations=1)

    ready = service.status(
        orchestration_ready=True,
        verification_ready=True,
        cancellation_ready=True,
    )

    assert ready["ready_for_consideration"] is True
    assert ready["execution_enabled"] is False
    assert ready["mode"] == "readiness_only"

