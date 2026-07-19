"""Focused SQLite persistence for governed build sessions."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from .build_types import (
    BuildAssessment,
    BuildLease,
    BuildSession,
    BuildTier,
    CloudUsageEvent,
    LeaseLimits,
    PricingSnapshot,
    UsageReservation,
)
from .db import KernelDatabase, utc_now


_PHASES = {
    "assessment",
    "approval",
    "planning",
    "implementation",
    "verification",
    "review",
    "complete",
}


class BuildReservationRejected(ValueError):
    def __init__(self, field: str, detail: str = ""):
        self.field = field
        self.detail = detail
        super().__init__(f"{field}: {detail}" if detail else field)


class BuildStore:
    def __init__(self, database: KernelDatabase):
        self.database = database

    def create_assessment(self, assessment: BuildAssessment) -> BuildAssessment:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO build_assessments (
                  task, acceptance, workspace, repo_fingerprint, deterministic_score,
                  local_adjustment, final_score, confidence, recommended_tier,
                  dimensions_json, floor_rules_json, evidence_json, unknowns_json,
                  local_work_json, cloud_reasons_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assessment.task,
                    assessment.acceptance,
                    assessment.workspace,
                    assessment.repo_fingerprint,
                    assessment.deterministic_score,
                    assessment.local_adjustment,
                    assessment.final_score,
                    assessment.confidence,
                    assessment.recommended_tier.value,
                    _dumps(assessment.dimensions),
                    _dumps(list(assessment.floor_rules)),
                    _dumps(assessment.evidence),
                    _dumps(list(assessment.unknowns)),
                    _dumps(list(assessment.local_work)),
                    _dumps(list(assessment.cloud_reasons)),
                    assessment.created_at,
                ),
            )
            return replace(assessment, id=int(cursor.lastrowid))

    def get_assessment(self, assessment_id: int) -> BuildAssessment | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM build_assessments WHERE id = ?", (assessment_id,)
            ).fetchone()
        return _assessment_from_row(row) if row else None

    def create_session(
        self,
        assessment: BuildAssessment,
        *,
        work_item_id: int | None = None,
    ) -> BuildSession:
        stored = assessment
        if stored.id is None:
            stored = self.create_assessment(stored)
        elif self.get_assessment(stored.id) is None:
            raise ValueError(f"Build assessment not found: {stored.id}")
        now = utc_now()
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO build_sessions (
                  assessment_id, work_item_id, workspace, repo_fingerprint, phase,
                  status, checkpoint_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, 'approval', 'active', '{}', ?, ?)
                """,
                (
                    stored.id,
                    work_item_id,
                    stored.workspace,
                    stored.repo_fingerprint,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM build_sessions WHERE id = ?", (int(cursor.lastrowid),)
            ).fetchone()
        return _session_from_row(row)

    def get_session(self, session_id: int) -> BuildSession | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM build_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return _session_from_row(row) if row else None

    def get_session_for_work_item(self, work_item_id: int) -> BuildSession | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM build_sessions
                WHERE work_item_id = ? ORDER BY id DESC LIMIT 1
                """,
                (work_item_id,),
            ).fetchone()
        return _session_from_row(row) if row else None

    def list_sessions(self, *, limit: int = 50) -> list[BuildSession]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM build_sessions ORDER BY id DESC LIMIT ?", (max(1, limit),)
            ).fetchall()
        return [_session_from_row(row) for row in rows]

    def checkpoint(
        self,
        session_id: int,
        *,
        phase: str,
        checkpoint: dict[str, Any],
    ) -> BuildSession:
        if phase not in _PHASES:
            raise ValueError(f"Invalid build phase: {phase}")
        now = utc_now()
        status = "complete" if phase == "complete" else "active"
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE build_sessions
                SET phase = ?, status = ?, checkpoint_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (phase, status, _dumps(checkpoint), now, session_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Build session not found: {session_id}")
            row = connection.execute(
                "SELECT * FROM build_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return _session_from_row(row)

    def create_lease(
        self,
        session_id: int,
        tier: BuildTier,
        limits: LeaseLimits,
        *,
        provider: str,
        model: str,
        approval_request_id: int,
    ) -> BuildLease:
        started = datetime.now(UTC).replace(microsecond=0)
        expires = started + timedelta(seconds=limits.duration_seconds)
        with self.database.connect() as connection:
            version_row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM build_leases WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            version = int(version_row[0])
            cursor = connection.execute(
                """
                INSERT INTO build_leases (
                  session_id, version, tier, provider, model, limits_json, state,
                  approval_request_id, started_at, expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    session_id,
                    version,
                    tier.value,
                    provider.strip(),
                    model.strip(),
                    _dumps(asdict(limits)),
                    approval_request_id,
                    started.isoformat(),
                    expires.isoformat(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM build_leases WHERE id = ?", (int(cursor.lastrowid),)
            ).fetchone()
        return _lease_from_row(row)

    def get_lease(self, lease_id: int) -> BuildLease | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM build_leases WHERE id = ?", (lease_id,)
            ).fetchone()
        return _lease_from_row(row) if row else None

    def upgrade_lease(
        self,
        session_id: int,
        tier: BuildTier,
        additional_limits: LeaseLimits,
        *,
        approval_request_id: int,
    ) -> BuildLease:
        started = datetime.now(UTC).replace(microsecond=0)
        expires = started + timedelta(seconds=additional_limits.duration_seconds)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """
                SELECT * FROM build_leases
                WHERE session_id = ?
                  AND state IN ('active', 'warning', 'paused', 'exhausted')
                ORDER BY version DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if current is None:
                raise ValueError(f"No current build lease for session {session_id}")
            current_limits = LeaseLimits(**json.loads(current["limits_json"]))
            cumulative = LeaseLimits(
                dollar_micro=current_limits.dollar_micro + additional_limits.dollar_micro,
                input_tokens=current_limits.input_tokens + additional_limits.input_tokens,
                output_tokens=current_limits.output_tokens + additional_limits.output_tokens,
                cloud_turns=current_limits.cloud_turns + additional_limits.cloud_turns,
                duration_seconds=additional_limits.duration_seconds,
            )
            connection.execute(
                "UPDATE build_leases SET state = 'superseded' WHERE id = ?",
                (int(current["id"]),),
            )
            cursor = connection.execute(
                """
                INSERT INTO build_leases (
                  session_id, version, tier, provider, model, limits_json, state,
                  approval_request_id, actual_input_tokens, actual_output_tokens,
                  actual_microdollars, reserved_input_tokens, reserved_output_tokens,
                  reserved_microdollars, cloud_turns, started_at, expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    int(current["version"]) + 1,
                    tier.value,
                    str(current["provider"]),
                    str(current["model"]),
                    _dumps(asdict(cumulative)),
                    approval_request_id,
                    int(current["actual_input_tokens"]),
                    int(current["actual_output_tokens"]),
                    int(current["actual_microdollars"]),
                    int(current["reserved_input_tokens"]),
                    int(current["reserved_output_tokens"]),
                    int(current["reserved_microdollars"]),
                    int(current["cloud_turns"]),
                    started.isoformat(),
                    expires.isoformat(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM build_leases WHERE id = ?", (int(cursor.lastrowid),)
            ).fetchone()
        return _lease_from_row(row)

    def get_active_lease(self, session_id: int) -> BuildLease | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM build_leases
                WHERE session_id = ? AND state IN ('active', 'warning', 'paused', 'exhausted')
                ORDER BY version DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        return _lease_from_row(row) if row else None

    def list_usage(self, lease_id: int) -> list[CloudUsageEvent]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM cloud_usage_events WHERE lease_id = ? ORDER BY id",
                (lease_id,),
            ).fetchall()
        return [_usage_from_row(row) for row in rows]

    def list_session_usage(self, session_id: int) -> list[CloudUsageEvent]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT usage.*
                FROM cloud_usage_events AS usage
                JOIN build_leases AS lease ON lease.id = usage.lease_id
                WHERE lease.session_id = ?
                ORDER BY usage.id
                """,
                (session_id,),
            ).fetchall()
        return [_usage_from_row(row) for row in rows]

    def get_reservation(self, reservation_id: int) -> UsageReservation | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM cloud_usage_events WHERE id = ?", (reservation_id,)
            ).fetchone()
        return _reservation_from_row(row) if row else None

    def create_reservation(
        self,
        session_id: int,
        *,
        request_id: str,
        input_upper_tokens: int,
        max_output_tokens: int,
        reserved_microdollars: int,
        cache_mode: str,
        pricing: PricingSnapshot,
        warning_percent: int,
        now: str,
    ) -> UsageReservation:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM cloud_usage_events WHERE request_id = ?", (request_id,)
            ).fetchone()
            if existing:
                existing_lease = connection.execute(
                    "SELECT session_id FROM build_leases WHERE id = ?",
                    (int(existing["lease_id"]),),
                ).fetchone()
                if (
                    existing_lease
                    and int(existing_lease["session_id"]) == session_id
                    and str(existing["status"]) == "reserved"
                ):
                    return _reservation_from_row(existing)
                raise BuildReservationRejected(
                    "request_id", "already used; paid requests are not retried automatically"
                )

            lease_row = connection.execute(
                """
                SELECT * FROM build_leases
                WHERE session_id = ? AND state IN ('active', 'warning', 'paused', 'exhausted')
                ORDER BY version DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if lease_row is None:
                raise BuildReservationRejected("lease", "no approved lease")
            if str(lease_row["state"]) not in {"active", "warning"}:
                raise BuildReservationRejected("lease_state", str(lease_row["state"]))
            if (
                str(lease_row["provider"]) != pricing.provider
                or str(lease_row["model"]) != pricing.model
            ):
                raise BuildReservationRejected(
                    "pricing_model", "pricing snapshot does not match the approved lease"
                )
            if datetime.fromisoformat(now) >= datetime.fromisoformat(str(lease_row["expires_at"])):
                connection.execute(
                    "UPDATE build_leases SET state = 'expired' WHERE id = ?",
                    (int(lease_row["id"]),),
                )
                raise BuildReservationRejected("expiration", "lease expired")

            limits = LeaseLimits(**json.loads(lease_row["limits_json"]))
            checks = {
                "input_tokens": (
                    int(lease_row["actual_input_tokens"])
                    + int(lease_row["reserved_input_tokens"])
                    + input_upper_tokens,
                    limits.input_tokens,
                ),
                "output_tokens": (
                    int(lease_row["actual_output_tokens"])
                    + int(lease_row["reserved_output_tokens"])
                    + max_output_tokens,
                    limits.output_tokens,
                ),
                "microdollars": (
                    int(lease_row["actual_microdollars"])
                    + int(lease_row["reserved_microdollars"])
                    + reserved_microdollars,
                    limits.dollar_micro,
                ),
                "cloud_turns": (int(lease_row["cloud_turns"]) + 1, limits.cloud_turns),
            }
            for field, (requested, ceiling) in checks.items():
                if requested > ceiling:
                    raise BuildReservationRejected(
                        field, f"requested total {requested} exceeds {ceiling}"
                    )

            turn_number = int(lease_row["cloud_turns"]) + 1
            cursor = connection.execute(
                """
                INSERT INTO cloud_usage_events (
                  lease_id, request_id, turn_number, status, cache_mode,
                  input_upper_tokens, max_output_tokens, reserved_microdollars,
                  pricing_json, created_at
                )
                VALUES (?, ?, ?, 'reserved', ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(lease_row["id"]),
                    request_id,
                    turn_number,
                    cache_mode,
                    input_upper_tokens,
                    max_output_tokens,
                    reserved_microdollars,
                    _dumps(_pricing_dict(pricing)),
                    now,
                ),
            )
            totals = {
                "input": int(lease_row["actual_input_tokens"])
                + int(lease_row["reserved_input_tokens"])
                + input_upper_tokens,
                "output": int(lease_row["actual_output_tokens"])
                + int(lease_row["reserved_output_tokens"])
                + max_output_tokens,
                "dollars": int(lease_row["actual_microdollars"])
                + int(lease_row["reserved_microdollars"])
                + reserved_microdollars,
                "turns": turn_number,
            }
            state = (
                "warning"
                if _at_warning(totals, limits, warning_percent)
                else str(lease_row["state"])
            )
            connection.execute(
                """
                UPDATE build_leases
                SET reserved_input_tokens = reserved_input_tokens + ?,
                    reserved_output_tokens = reserved_output_tokens + ?,
                    reserved_microdollars = reserved_microdollars + ?,
                    cloud_turns = ?, state = ?
                WHERE id = ?
                """,
                (
                    input_upper_tokens,
                    max_output_tokens,
                    reserved_microdollars,
                    turn_number,
                    state,
                    int(lease_row["id"]),
                ),
            )
            row = connection.execute(
                "SELECT * FROM cloud_usage_events WHERE id = ?", (int(cursor.lastrowid),)
            ).fetchone()
        return _reservation_from_row(row)

    def settle_reservation(
        self,
        reservation_id: int,
        *,
        input_tokens: int,
        cache_write_5m_tokens: int,
        cache_write_1h_tokens: int,
        cache_read_tokens: int,
        output_tokens: int,
        settled_microdollars: int,
        status: str,
        warning_percent: int,
        now: str,
        pause_lease: bool = False,
    ) -> CloudUsageEvent:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            event = connection.execute(
                "SELECT * FROM cloud_usage_events WHERE id = ?", (reservation_id,)
            ).fetchone()
            if event is None:
                raise ValueError(f"Usage reservation not found: {reservation_id}")
            if str(event["status"]) not in {"reserved", "uncertain_spend"}:
                return _usage_from_row(event)
            lease = connection.execute(
                "SELECT * FROM build_leases WHERE id = ?", (int(event["lease_id"]),)
            ).fetchone()
            if lease is None:
                raise ValueError(f"Build lease not found: {event['lease_id']}")
            actual_input = (
                input_tokens
                + cache_write_5m_tokens
                + cache_write_1h_tokens
                + cache_read_tokens
            )
            limits = LeaseLimits(**json.loads(lease["limits_json"]))
            totals = {
                "input": int(lease["actual_input_tokens"]) + actual_input,
                "output": int(lease["actual_output_tokens"]) + output_tokens,
                "dollars": int(lease["actual_microdollars"]) + settled_microdollars,
                "turns": int(lease["cloud_turns"]),
            }
            state = str(lease["state"])
            if pause_lease:
                state = "paused"
            elif state != "paused" and _at_warning(totals, limits, warning_percent):
                state = "warning"
            connection.execute(
                """
                UPDATE cloud_usage_events
                SET status = ?, input_tokens = ?, cache_write_5m_tokens = ?,
                    cache_write_1h_tokens = ?, cache_read_tokens = ?, output_tokens = ?,
                    settled_microdollars = ?, settled_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    input_tokens,
                    cache_write_5m_tokens,
                    cache_write_1h_tokens,
                    cache_read_tokens,
                    output_tokens,
                    settled_microdollars,
                    now,
                    reservation_id,
                ),
            )
            connection.execute(
                """
                UPDATE build_leases
                SET reserved_input_tokens = MAX(0, reserved_input_tokens - ?),
                    reserved_output_tokens = MAX(0, reserved_output_tokens - ?),
                    reserved_microdollars = MAX(0, reserved_microdollars - ?),
                    actual_input_tokens = actual_input_tokens + ?,
                    actual_output_tokens = actual_output_tokens + ?,
                    actual_microdollars = actual_microdollars + ?,
                    state = ?
                WHERE id = ?
                """,
                (
                    int(event["input_upper_tokens"]),
                    int(event["max_output_tokens"]),
                    int(event["reserved_microdollars"]),
                    actual_input,
                    output_tokens,
                    settled_microdollars,
                    state,
                    int(event["lease_id"]),
                ),
            )
            row = connection.execute(
                "SELECT * FROM cloud_usage_events WHERE id = ?", (reservation_id,)
            ).fetchone()
        return _usage_from_row(row)

    def release_reservation(self, reservation_id: int) -> None:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            event = connection.execute(
                "SELECT * FROM cloud_usage_events WHERE id = ?", (reservation_id,)
            ).fetchone()
            if event is None:
                return
            if str(event["status"]) != "reserved":
                raise ValueError("Only a proven-unsent reservation can be released")
            connection.execute(
                """
                UPDATE build_leases
                SET reserved_input_tokens = MAX(0, reserved_input_tokens - ?),
                    reserved_output_tokens = MAX(0, reserved_output_tokens - ?),
                    reserved_microdollars = MAX(0, reserved_microdollars - ?),
                    cloud_turns = MAX(0, cloud_turns - 1),
                    state = CASE WHEN state = 'warning' THEN 'active' ELSE state END
                WHERE id = ?
                """,
                (
                    int(event["input_upper_tokens"]),
                    int(event["max_output_tokens"]),
                    int(event["reserved_microdollars"]),
                    int(event["lease_id"]),
                ),
            )
            connection.execute(
                "DELETE FROM cloud_usage_events WHERE id = ?", (reservation_id,)
            )

    def mark_reservation_uncertain(
        self, reservation_id: int, *, reason: str
    ) -> CloudUsageEvent:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            event = connection.execute(
                "SELECT * FROM cloud_usage_events WHERE id = ?", (reservation_id,)
            ).fetchone()
            if event is None:
                raise ValueError(f"Usage reservation not found: {reservation_id}")
            if str(event["status"]) == "reserved":
                connection.execute(
                    "UPDATE cloud_usage_events SET status = 'uncertain_spend', error = ? WHERE id = ?",
                    (reason, reservation_id),
                )
                connection.execute(
                    "UPDATE build_leases SET state = 'paused' WHERE id = ?",
                    (int(event["lease_id"]),),
                )
            row = connection.execute(
                "SELECT * FROM cloud_usage_events WHERE id = ?", (reservation_id,)
            ).fetchone()
        return _usage_from_row(row)

    def pause_lease(self, lease_id: int) -> BuildLease:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE build_leases SET state = 'paused'
                WHERE id = ? AND state IN ('active', 'warning')
                """,
                (lease_id,),
            )
            if cursor.rowcount == 0:
                row = connection.execute(
                    "SELECT * FROM build_leases WHERE id = ?", (lease_id,)
                ).fetchone()
                if row is None:
                    raise ValueError(f"Build lease not found: {lease_id}")
            row = connection.execute(
                "SELECT * FROM build_leases WHERE id = ?", (lease_id,)
            ).fetchone()
        return _lease_from_row(row)

    def expire_lease(self, lease_id: int) -> BuildLease:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE build_leases SET state = 'expired'
                WHERE id = ? AND state IN ('active', 'warning', 'paused', 'exhausted')
                """,
                (lease_id,),
            )
            if cursor.rowcount == 0:
                row = connection.execute(
                    "SELECT * FROM build_leases WHERE id = ?", (lease_id,)
                ).fetchone()
                if row is None:
                    raise ValueError(f"Build lease not found: {lease_id}")
            row = connection.execute(
                "SELECT * FROM build_leases WHERE id = ?", (lease_id,)
            ).fetchone()
        return _lease_from_row(row)

    def exhaust_lease(self, lease_id: int) -> BuildLease:
        return self.set_lease_state(lease_id, "exhausted")

    def set_lease_state(self, lease_id: int, state: str) -> BuildLease:
        if state not in {
            "active",
            "warning",
            "paused",
            "exhausted",
            "expired",
            "denied",
            "superseded",
        }:
            raise ValueError(f"Invalid build lease state: {state}")
        with self.database.connect() as connection:
            cursor = connection.execute(
                "UPDATE build_leases SET state = ? WHERE id = ?", (state, lease_id)
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Build lease not found: {lease_id}")
            row = connection.execute(
                "SELECT * FROM build_leases WHERE id = ?", (lease_id,)
            ).fetchone()
        return _lease_from_row(row)


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _pricing_dict(pricing: PricingSnapshot) -> dict[str, str]:
    return {key: str(value) for key, value in asdict(pricing).items()}


def _at_warning(
    totals: dict[str, int], limits: LeaseLimits, warning_percent: int
) -> bool:
    ceilings = {
        "input": limits.input_tokens,
        "output": limits.output_tokens,
        "dollars": limits.dollar_micro,
        "turns": limits.cloud_turns,
    }
    return any(
        totals[key] * 100 >= ceilings[key] * warning_percent for key in ceilings
    )


def _assessment_from_row(row: sqlite3.Row) -> BuildAssessment:
    return BuildAssessment(
        id=int(row["id"]),
        task=str(row["task"]),
        acceptance=str(row["acceptance"]),
        workspace=str(row["workspace"]),
        repo_fingerprint=str(row["repo_fingerprint"]),
        deterministic_score=int(row["deterministic_score"]),
        local_adjustment=int(row["local_adjustment"]),
        final_score=int(row["final_score"]),
        confidence=float(row["confidence"]),
        recommended_tier=BuildTier(str(row["recommended_tier"])),
        dimensions=json.loads(row["dimensions_json"]),
        floor_rules=tuple(json.loads(row["floor_rules_json"])),
        evidence=json.loads(row["evidence_json"]),
        unknowns=tuple(json.loads(row["unknowns_json"])),
        local_work=tuple(json.loads(row["local_work_json"])),
        cloud_reasons=tuple(json.loads(row["cloud_reasons_json"])),
        created_at=str(row["created_at"]),
    )


def _session_from_row(row: sqlite3.Row) -> BuildSession:
    return BuildSession(
        id=int(row["id"]),
        assessment_id=int(row["assessment_id"]),
        work_item_id=int(row["work_item_id"]) if row["work_item_id"] is not None else None,
        workspace=str(row["workspace"]),
        repo_fingerprint=str(row["repo_fingerprint"]),
        phase=str(row["phase"]),
        status=str(row["status"]),
        checkpoint=json.loads(row["checkpoint_json"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _lease_from_row(row: sqlite3.Row) -> BuildLease:
    limits = LeaseLimits(**json.loads(row["limits_json"]))
    return BuildLease(
        id=int(row["id"]),
        session_id=int(row["session_id"]),
        version=int(row["version"]),
        tier=BuildTier(str(row["tier"])),
        provider=str(row["provider"]),
        model=str(row["model"]),
        limits=limits,
        state=str(row["state"]),
        approval_request_id=int(row["approval_request_id"]),
        actual_input_tokens=int(row["actual_input_tokens"]),
        actual_output_tokens=int(row["actual_output_tokens"]),
        actual_microdollars=int(row["actual_microdollars"]),
        reserved_input_tokens=int(row["reserved_input_tokens"]),
        reserved_output_tokens=int(row["reserved_output_tokens"]),
        reserved_microdollars=int(row["reserved_microdollars"]),
        cloud_turns=int(row["cloud_turns"]),
        started_at=str(row["started_at"]),
        expires_at=str(row["expires_at"]),
    )


def _usage_from_row(row: sqlite3.Row) -> CloudUsageEvent:
    pricing = PricingSnapshot(**json.loads(row["pricing_json"]))
    return CloudUsageEvent(
        id=int(row["id"]),
        lease_id=int(row["lease_id"]),
        request_id=str(row["request_id"]),
        turn_number=int(row["turn_number"]),
        status=str(row["status"]),
        input_tokens=int(row["input_tokens"]),
        cache_write_5m_tokens=int(row["cache_write_5m_tokens"]),
        cache_write_1h_tokens=int(row["cache_write_1h_tokens"]),
        cache_read_tokens=int(row["cache_read_tokens"]),
        output_tokens=int(row["output_tokens"]),
        reserved_microdollars=int(row["reserved_microdollars"]),
        settled_microdollars=int(row["settled_microdollars"]),
        pricing=pricing,
        created_at=str(row["created_at"]),
        settled_at=str(row["settled_at"]) if row["settled_at"] else None,
    )


def _reservation_from_row(row: sqlite3.Row) -> UsageReservation:
    return UsageReservation(
        id=int(row["id"]),
        lease_id=int(row["lease_id"]),
        request_id=str(row["request_id"]),
        turn_number=int(row["turn_number"]),
        input_upper_tokens=int(row["input_upper_tokens"]),
        max_output_tokens=int(row["max_output_tokens"]),
        reserved_microdollars=int(row["reserved_microdollars"]),
        pricing=PricingSnapshot(**json.loads(row["pricing_json"])),
        status=str(row["status"]),
        created_at=str(row["created_at"]),
    )
