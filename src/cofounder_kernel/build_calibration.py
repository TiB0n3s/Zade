"""Advisory assessment calibration and managed-agent maturity gates."""

from __future__ import annotations

from typing import Any

from .build_store import BuildStore
from .build_types import BuildCalibration


class BuildCalibrationService:
    def __init__(self, store: BuildStore):
        self.store = store

    def record(
        self, session_id: int, *, provider: str, outcome: str
    ) -> BuildCalibration:
        session = self.store.get_session(session_id)
        if session is None:
            raise ValueError(f"Build session not found: {session_id}")
        assessment = self.store.get_assessment(session.assessment_id)
        if assessment is None:
            raise ValueError(f"Build assessment not found: {session.assessment_id}")
        lease = self.store.get_active_lease(session_id, provider=provider)
        if lease is None:
            leases = self._all_session_leases(session_id, provider)
            lease = leases[-1] if leases else None
        if lease is None:
            raise ValueError(
                f"No {provider} lease exists for build session {session_id}"
            )
        utilization = {
            "input": _ratio(lease.actual_input_tokens, lease.limits.input_tokens),
            "output": _ratio(lease.actual_output_tokens, lease.limits.output_tokens),
            "cost": _ratio(lease.actual_microdollars, lease.limits.dollar_micro),
            "turns": _ratio(lease.cloud_turns, lease.limits.cloud_turns),
        }
        recommendation = _recommend(outcome, tuple(utilization.values()))
        return self.store.create_calibration(
            session_id=session_id,
            assessment_id=assessment.id or session.assessment_id,
            lease_id=lease.id,
            provider=lease.provider,
            model=lease.model,
            predicted_tier=assessment.recommended_tier,
            assessment_score=assessment.final_score,
            outcome=outcome.strip() or "unknown",
            actual_input_tokens=lease.actual_input_tokens,
            actual_output_tokens=lease.actual_output_tokens,
            actual_microdollars=lease.actual_microdollars,
            actual_cloud_turns=lease.cloud_turns,
            input_utilization=utilization["input"],
            output_utilization=utilization["output"],
            cost_utilization=utilization["cost"],
            turn_utilization=utilization["turns"],
            recommendation=recommendation,
        )

    def list(
        self,
        *,
        session_id: int | None = None,
        provider: str | None = None,
        limit: int = 100,
    ) -> list[BuildCalibration]:
        return self.store.list_calibrations(
            session_id=session_id, provider=provider, limit=limit
        )

    def _all_session_leases(self, session_id: int, provider: str):
        with self.store.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM build_leases
                WHERE session_id = ? AND provider = ? ORDER BY version
                """,
                (session_id, provider),
            ).fetchall()
        return [self.store.get_lease(int(row["id"])) for row in rows if row]


class ManagedAgentsReadinessService:
    """Report maturity only; this service has no managed-agent execution path."""

    def __init__(self, store: BuildStore, *, minimum_calibrations: int = 3):
        self.store = store
        self.minimum_calibrations = max(1, int(minimum_calibrations))

    def status(
        self,
        *,
        orchestration_ready: bool,
        verification_ready: bool,
        cancellation_ready: bool,
        evidence_required: bool = False,
    ) -> dict[str, Any]:
        calibration_count = len(self.store.list_calibrations(limit=1000))
        gates = {
            "durable_orchestration": bool(orchestration_ready),
            "integrated_verification": bool(verification_ready),
            "durable_cancellation": bool(cancellation_ready),
            "minimum_calibrations": calibration_count >= self.minimum_calibrations,
        }
        evidence: dict[str, int] = {}
        if evidence_required:
            with self.store.database.connect() as connection:
                evidence = {
                    "completed_local_sessions": int(connection.execute(
                        "SELECT COUNT(*) FROM build_sessions WHERE status = 'complete'"
                    ).fetchone()[0]),
                    "cancelled_tasks": int(connection.execute(
                        "SELECT COUNT(*) FROM build_tasks WHERE status = 'cancelled'"
                    ).fetchone()[0]),
                    "interrupted_runs": int(connection.execute(
                        "SELECT COUNT(*) FROM build_task_runs WHERE status = 'interrupted'"
                    ).fetchone()[0]),
                    "cloud_calibrations": int(connection.execute(
                        "SELECT COUNT(*) FROM build_calibrations WHERE actual_cloud_turns > 0"
                    ).fetchone()[0]),
                }
            gates.update(
                {
                    "completed_local_sessions": evidence["completed_local_sessions"] >= 3,
                    "verified_cancellation": evidence["cancelled_tasks"] >= 1,
                    "restart_recovery_exercised": evidence["interrupted_runs"] >= 1,
                    "cloud_calibrations": evidence["cloud_calibrations"] >= 2,
                }
            )
        return {
            "mode": "readiness_only",
            "execution_enabled": False,
            "ready_for_consideration": all(gates.values()),
            "gates": gates,
            "calibration_count": calibration_count,
            "minimum_calibrations": self.minimum_calibrations,
            "evidence": evidence,
        }


def _ratio(actual: int, limit: int) -> float:
    return round(actual / limit, 6) if limit > 0 else 0.0


def _recommend(outcome: str, values: tuple[float, ...]) -> str:
    if outcome.strip().lower() not in {"success", "succeeded", "complete"}:
        return "review_failure_before_resizing"
    peak = max(values, default=0.0)
    if peak <= 0.25:
        return "consider_lower_tier"
    if peak >= 0.9:
        return "consider_higher_tier"
    return "tier_fit"
