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

    def get_active_lease(self, session_id: int) -> BuildLease | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM build_leases
                WHERE session_id = ? AND state IN ('active', 'warning', 'paused')
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


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


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
