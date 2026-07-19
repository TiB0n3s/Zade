"""Focused SQLite persistence for governed build sessions."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from .build_types import (
    BUILD_PHASES,
    BuildArtifact,
    BuildAssessment,
    BuildCalibration,
    BuildLease,
    BuildSession,
    BuildTask,
    BuildTaskKind,
    BuildTaskRun,
    BuildTaskStatus,
    BuildTier,
    CloudUsageEvent,
    LeaseLimits,
    PricingSnapshot,
    UsageReservation,
)
from .db import KernelDatabase, utc_now


_PHASES = set(BUILD_PHASES)
_SESSION_STATUSES = {
    "active",
    "paused",
    "cancelling",
    "cancelled",
    "quarantined",
    "complete",
}
_RUN_TERMINAL_STATUSES = {
    BuildTaskStatus.SUCCEEDED,
    BuildTaskStatus.FAILED,
    BuildTaskStatus.CANCELLED,
    BuildTaskStatus.INTERRUPTED,
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

    def count_sessions(self, *, status: str | None = None) -> int:
        with self.database.connect() as connection:
            if status is None:
                row = connection.execute(
                    "SELECT COUNT(*) AS count FROM build_sessions"
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) AS count FROM build_sessions WHERE status = ?",
                    (status,),
                ).fetchone()
        return int(row["count"] if row else 0)

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
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE build_sessions
                SET phase = ?,
                    status = CASE WHEN ? = 'complete' THEN 'complete' ELSE status END,
                    checkpoint_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (phase, phase, _dumps(checkpoint), now, session_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Build session not found: {session_id}")
            row = connection.execute(
                "SELECT * FROM build_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return _session_from_row(row)

    def create_task(
        self,
        session_id: int,
        *,
        phase: str,
        kind: BuildTaskKind | str,
        title: str,
        payload: dict[str, Any] | None = None,
        dependencies: tuple[int, ...] = (),
        acceptance: dict[str, Any] | None = None,
        idempotency_key: str = "",
        max_attempts: int = 1,
    ) -> BuildTask:
        if phase not in _PHASES:
            raise ValueError(f"Invalid build phase: {phase}")
        task_kind = BuildTaskKind(kind)
        clean_title = title.strip()
        if not clean_title:
            raise ValueError("Build task title is required")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        dependency_ids = tuple(dict.fromkeys(int(item) for item in dependencies))
        key = idempotency_key.strip()
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute(
                "SELECT id FROM build_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if session is None:
                raise ValueError(f"Build session not found: {session_id}")
            if key:
                existing = connection.execute(
                    """
                    SELECT * FROM build_tasks
                    WHERE session_id = ? AND idempotency_key = ?
                    """,
                    (session_id, key),
                ).fetchone()
                if existing is not None:
                    return _task_from_row(existing)
            if dependency_ids:
                placeholders = ",".join("?" for _ in dependency_ids)
                rows = connection.execute(
                    f"SELECT id, session_id FROM build_tasks WHERE id IN ({placeholders})",
                    dependency_ids,
                ).fetchall()
                if len(rows) != len(dependency_ids) or any(
                    int(row["session_id"]) != session_id for row in rows
                ):
                    raise ValueError("Build task dependencies must belong to the same build session")
            position_row = connection.execute(
                """
                SELECT COALESCE(MAX(position), 0) + 1 AS position
                FROM build_tasks WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            position = int(position_row["position"])
            cursor = connection.execute(
                """
                INSERT INTO build_tasks (
                  session_id, phase, position, kind, title, payload_json,
                  dependencies_json, acceptance_json, idempotency_key, status,
                  max_attempts, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    session_id,
                    phase,
                    position,
                    task_kind.value,
                    clean_title,
                    _dumps(payload or {}),
                    _dumps(dependency_ids),
                    _dumps(acceptance or {}),
                    key,
                    max_attempts,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM build_tasks WHERE id = ?", (int(cursor.lastrowid),)
            ).fetchone()
        return _task_from_row(row)

    def get_task(self, task_id: int) -> BuildTask | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM build_tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return _task_from_row(row) if row else None

    def list_tasks(self, session_id: int) -> list[BuildTask]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM build_tasks
                WHERE session_id = ? ORDER BY position, id
                """,
                (session_id,),
            ).fetchall()
        return [_task_from_row(row) for row in rows]

    def ready_tasks(self, session_id: int) -> list[BuildTask]:
        session = self.get_session(session_id)
        if session is None:
            raise ValueError(f"Build session not found: {session_id}")
        if session.status != "active":
            return []
        tasks = self.list_tasks(session_id)
        states = {task.id: task.status for task in tasks}
        phase_order = {phase: index for index, phase in enumerate(BUILD_PHASES)}
        ready = [
            task
            for task in tasks
            if task.status is BuildTaskStatus.PENDING
            and all(states.get(item) is BuildTaskStatus.SUCCEEDED for item in task.dependencies)
        ]
        return sorted(
            ready,
            key=lambda task: (phase_order.get(task.phase, len(phase_order)), task.position),
        )

    def claim_task(
        self,
        task_id: int,
        *,
        worker_id: str,
        backend: str = "host",
        command: tuple[str, ...] = (),
        pid: int | None = None,
    ) -> BuildTaskRun:
        clean_worker_id = worker_id.strip()
        if not clean_worker_id:
            raise ValueError("worker_id is required")
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM build_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Build task not found: {task_id}")
            session = connection.execute(
                "SELECT status FROM build_sessions WHERE id = ?",
                (int(row["session_id"]),),
            ).fetchone()
            if (
                str(row["status"]) != BuildTaskStatus.PENDING.value
                or session is None
                or str(session["status"]) != "active"
            ):
                raise ValueError(f"Build task {task_id} is not claimable")
            dependencies = tuple(json.loads(row["dependencies_json"]))
            if dependencies:
                placeholders = ",".join("?" for _ in dependencies)
                dependency_rows = connection.execute(
                    f"SELECT id, status FROM build_tasks WHERE id IN ({placeholders})",
                    dependencies,
                ).fetchall()
                if len(dependency_rows) != len(dependencies) or any(
                    str(item["status"]) != BuildTaskStatus.SUCCEEDED.value
                    for item in dependency_rows
                ):
                    raise ValueError(f"Build task {task_id} is not claimable")
            attempt_number = int(row["attempt_count"]) + 1
            if attempt_number > int(row["max_attempts"]):
                raise ValueError(f"Build task {task_id} has exhausted its attempts")
            cursor = connection.execute(
                """
                INSERT INTO build_task_runs (
                  task_id, session_id, attempt_number, worker_id, backend,
                  command_json, pid, status, started_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?)
                """,
                (
                    task_id,
                    int(row["session_id"]),
                    attempt_number,
                    clean_worker_id,
                    backend.strip() or "host",
                    _dumps(command),
                    pid,
                    now,
                ),
            )
            run_id = int(cursor.lastrowid)
            updated = connection.execute(
                """
                UPDATE build_tasks
                SET status = 'running', attempt_count = ?, active_run_id = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (attempt_number, run_id, now, task_id),
            )
            if updated.rowcount != 1:
                raise ValueError(f"Build task {task_id} is not claimable")
            run_row = connection.execute(
                "SELECT * FROM build_task_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return _task_run_from_row(run_row)

    def get_task_run(self, run_id: int) -> BuildTaskRun | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM build_task_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return _task_run_from_row(row) if row else None

    def list_task_runs(
        self, *, task_id: int | None = None, session_id: int | None = None
    ) -> list[BuildTaskRun]:
        if task_id is None and session_id is None:
            raise ValueError("task_id or session_id is required")
        column = "task_id" if task_id is not None else "session_id"
        value = task_id if task_id is not None else session_id
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM build_task_runs WHERE {column} = ? ORDER BY id",
                (value,),
            ).fetchall()
        return [_task_run_from_row(row) for row in rows]

    def finish_task_run(
        self,
        run_id: int,
        *,
        status: BuildTaskStatus | str,
        result: dict[str, Any] | None = None,
        error: str = "",
        log_path: str = "",
        artifact_ids: tuple[int, ...] = (),
    ) -> BuildTaskRun:
        run_status = BuildTaskStatus(status)
        if run_status not in _RUN_TERMINAL_STATUSES:
            raise ValueError(f"Invalid terminal task run status: {run_status.value}")
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT * FROM build_task_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise ValueError(f"Build task run not found: {run_id}")
            if str(run["status"]) != BuildTaskStatus.RUNNING.value:
                raise ValueError(f"Build task run {run_id} is already terminal")
            ids = tuple(dict.fromkeys(int(item) for item in artifact_ids))
            if ids:
                placeholders = ",".join("?" for _ in ids)
                artifacts = connection.execute(
                    f"SELECT id, run_id FROM build_artifacts WHERE id IN ({placeholders})",
                    ids,
                ).fetchall()
                if len(artifacts) != len(ids) or any(
                    int(item["run_id"] or 0) != run_id for item in artifacts
                ):
                    raise ValueError("Artifacts must belong to the completed task run")
            connection.execute(
                """
                UPDATE build_task_runs
                SET status = ?, result_json = ?, error = ?, log_path = ?,
                    artifact_ids_json = ?, finished_at = ?
                WHERE id = ?
                """,
                (
                    run_status.value,
                    _dumps(result or {}),
                    error,
                    log_path,
                    _dumps(ids),
                    now,
                    run_id,
                ),
            )
            retryable_local_failure = (
                run_status in {BuildTaskStatus.FAILED, BuildTaskStatus.INTERRUPTED}
                and str(run["backend"]) != "cloud"
                and int(run["attempt_number"])
                < self._task_max_attempts(connection, int(run["task_id"]))
            )
            task_status = (
                BuildTaskStatus.PENDING if retryable_local_failure else run_status
            )
            connection.execute(
                """
                UPDATE build_tasks
                SET status = ?, active_run_id = NULL, updated_at = ?
                WHERE id = ? AND active_run_id = ?
                """,
                (task_status.value, now, int(run["task_id"]), run_id),
            )
            self._finalize_cancellation(connection, int(run["session_id"]), now)
            row = connection.execute(
                "SELECT * FROM build_task_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return _task_run_from_row(row)

    def create_artifact(
        self,
        session_id: int,
        *,
        task_id: int | None,
        run_id: int | None,
        kind: str,
        uri: str,
        metadata: dict[str, Any] | None = None,
    ) -> BuildArtifact:
        clean_kind = kind.strip()
        clean_uri = uri.strip()
        if not clean_kind or not clean_uri:
            raise ValueError("Artifact kind and URI are required")
        now = utc_now()
        with self.database.connect() as connection:
            session = connection.execute(
                "SELECT id FROM build_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if session is None:
                raise ValueError(f"Build session not found: {session_id}")
            if task_id is not None:
                task = connection.execute(
                    "SELECT session_id FROM build_tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if task is None or int(task["session_id"]) != session_id:
                    raise ValueError("Artifact task must belong to the build session")
            if run_id is not None:
                run = connection.execute(
                    "SELECT session_id, task_id FROM build_task_runs WHERE id = ?", (run_id,)
                ).fetchone()
                if (
                    run is None
                    or int(run["session_id"]) != session_id
                    or (task_id is not None and int(run["task_id"]) != task_id)
                ):
                    raise ValueError("Artifact run must belong to the build session and task")
            cursor = connection.execute(
                """
                INSERT INTO build_artifacts (
                  session_id, task_id, run_id, kind, uri, metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    task_id,
                    run_id,
                    clean_kind,
                    clean_uri,
                    _dumps(metadata or {}),
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM build_artifacts WHERE id = ?", (int(cursor.lastrowid),)
            ).fetchone()
        return _artifact_from_row(row)

    def list_artifacts(self, session_id: int) -> list[BuildArtifact]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM build_artifacts WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
        return [_artifact_from_row(row) for row in rows]

    def pause_session(self, session_id: int) -> BuildSession:
        return self._set_session_status(session_id, "paused", allowed={"active"})

    def resume_session(self, session_id: int) -> BuildSession:
        return self._set_session_status(session_id, "active", allowed={"paused"})

    def quarantine_session(
        self, session_id: int, *, reason: str, actor: str = "founder"
    ) -> BuildSession:
        clean_reason = " ".join(reason.split())
        clean_actor = " ".join(actor.split())
        if not clean_reason:
            raise ValueError("Quarantine reason is required")
        if not clean_actor:
            raise ValueError("Quarantine actor is required")
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM build_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Build session not found: {session_id}")
            status = str(row["status"])
            if status in {"complete", "cancelled"}:
                raise ValueError(f"Cannot quarantine a {status} build session")
            checkpoint = json.loads(str(row["checkpoint_json"] or "{}"))
            checkpoint["quarantine"] = {
                "reason": clean_reason[:2000],
                "actor": clean_actor[:200],
                "at": now,
            }
            connection.execute(
                """
                UPDATE build_sessions
                SET status = 'quarantined', checkpoint_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (_dumps(checkpoint), now, session_id),
            )
            connection.execute(
                """
                UPDATE build_leases SET state = 'paused'
                WHERE session_id = ? AND state IN ('active', 'warning')
                """,
                (session_id,),
            )
            updated = connection.execute(
                "SELECT * FROM build_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return _session_from_row(updated)

    def cancel_session(self, session_id: int) -> BuildSession:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute(
                "SELECT * FROM build_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if session is None:
                raise ValueError(f"Build session not found: {session_id}")
            running = connection.execute(
                """
                SELECT COUNT(*) AS count FROM build_tasks
                WHERE session_id = ? AND status = 'running'
                """,
                (session_id,),
            ).fetchone()
            status = "cancelling" if int(running["count"]) else "cancelled"
            connection.execute(
                """
                UPDATE build_tasks SET status = 'cancelled', updated_at = ?
                WHERE session_id = ? AND status IN ('pending', 'blocked')
                """,
                (now, session_id),
            )
            connection.execute(
                "UPDATE build_sessions SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, session_id),
            )
            row = connection.execute(
                "SELECT * FROM build_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return _session_from_row(row)

    def recover_interrupted_runs(self) -> list[BuildTaskRun]:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT r.*, t.max_attempts
                FROM build_task_runs r
                JOIN build_tasks t ON t.id = r.task_id
                WHERE r.status = 'running'
                ORDER BY r.id
                """
            ).fetchall()
            recovered_ids: list[int] = []
            for row in rows:
                connection.execute(
                    """
                    UPDATE build_task_runs
                    SET status = 'interrupted', error = ?, finished_at = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    ("Worker stopped before reporting a terminal result", now, int(row["id"])),
                )
                task_status = (
                    BuildTaskStatus.PENDING.value
                    if str(row["backend"]) != "cloud"
                    and int(row["attempt_number"]) < int(row["max_attempts"])
                    else BuildTaskStatus.FAILED.value
                )
                connection.execute(
                    """
                    UPDATE build_tasks
                    SET status = ?, active_run_id = NULL, updated_at = ?
                    WHERE id = ? AND active_run_id = ?
                    """,
                    (task_status, now, int(row["task_id"]), int(row["id"])),
                )
                self._finalize_cancellation(connection, int(row["session_id"]), now)
                recovered_ids.append(int(row["id"]))
            if not recovered_ids:
                return []
            placeholders = ",".join("?" for _ in recovered_ids)
            recovered = connection.execute(
                f"SELECT * FROM build_task_runs WHERE id IN ({placeholders}) ORDER BY id",
                recovered_ids,
            ).fetchall()
        return [_task_run_from_row(row) for row in recovered]

    def _set_session_status(
        self, session_id: int, status: str, *, allowed: set[str]
    ) -> BuildSession:
        if status not in _SESSION_STATUSES:
            raise ValueError(f"Invalid build session status: {status}")
        now = utc_now()
        with self.database.connect() as connection:
            placeholders = ",".join("?" for _ in allowed)
            cursor = connection.execute(
                f"""
                UPDATE build_sessions SET status = ?, updated_at = ?
                WHERE id = ? AND status IN ({placeholders})
                """,
                (status, now, session_id, *sorted(allowed)),
            )
            row = connection.execute(
                "SELECT * FROM build_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Build session not found: {session_id}")
            if cursor.rowcount != 1:
                raise ValueError(
                    f"Build session {session_id} cannot transition from {row['status']} to {status}"
                )
        return _session_from_row(row)

    @staticmethod
    def _task_max_attempts(connection: sqlite3.Connection, task_id: int) -> int:
        row = connection.execute(
            "SELECT max_attempts FROM build_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Build task not found: {task_id}")
        return int(row["max_attempts"])

    @staticmethod
    def _finalize_cancellation(
        connection: sqlite3.Connection, session_id: int, now: str
    ) -> None:
        session = connection.execute(
            "SELECT status FROM build_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if session is None or str(session["status"]) != "cancelling":
            return
        running = connection.execute(
            """
            SELECT COUNT(*) AS count FROM build_tasks
            WHERE session_id = ? AND status = 'running'
            """,
            (session_id,),
        ).fetchone()
        if int(running["count"]) == 0:
            connection.execute(
                "UPDATE build_sessions SET status = 'cancelled', updated_at = ? WHERE id = ?",
                (now, session_id),
            )

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
        provider: str = "anthropic",
    ) -> BuildLease:
        started = datetime.now(UTC).replace(microsecond=0)
        expires = started + timedelta(seconds=additional_limits.duration_seconds)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """
                SELECT * FROM build_leases
                WHERE session_id = ?
                  AND provider = ?
                  AND state IN ('active', 'warning', 'paused', 'exhausted')
                ORDER BY version DESC LIMIT 1
                """,
                (session_id, provider.strip()),
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

    def get_active_lease(
        self, session_id: int, *, provider: str = "anthropic"
    ) -> BuildLease | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM build_leases
                WHERE session_id = ? AND provider = ?
                  AND state IN ('active', 'warning', 'paused', 'exhausted')
                ORDER BY version DESC LIMIT 1
                """,
                (session_id, provider.strip()),
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
                WHERE session_id = ? AND provider = ? AND model = ?
                  AND state IN ('active', 'warning', 'paused', 'exhausted')
                ORDER BY version DESC LIMIT 1
                """,
                (session_id, pricing.provider, pricing.model),
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

    def create_calibration(
        self,
        *,
        session_id: int,
        assessment_id: int,
        lease_id: int,
        provider: str,
        model: str,
        predicted_tier: BuildTier,
        assessment_score: int,
        outcome: str,
        actual_input_tokens: int,
        actual_output_tokens: int,
        actual_microdollars: int,
        actual_cloud_turns: int,
        input_utilization: float,
        output_utilization: float,
        cost_utilization: float,
        turn_utilization: float,
        recommendation: str,
    ) -> BuildCalibration:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM build_calibrations
                WHERE session_id = ? AND provider = ? AND lease_id = ?
                """,
                (session_id, provider, lease_id),
            ).fetchone()
            if existing is not None:
                return _calibration_from_row(existing)
            cursor = connection.execute(
                """
                INSERT INTO build_calibrations (
                  session_id, assessment_id, lease_id, provider, model,
                  predicted_tier, assessment_score, outcome, actual_input_tokens,
                  actual_output_tokens, actual_microdollars, actual_cloud_turns,
                  input_utilization, output_utilization, cost_utilization,
                  turn_utilization, recommendation, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    assessment_id,
                    lease_id,
                    provider,
                    model,
                    predicted_tier.value,
                    assessment_score,
                    outcome,
                    actual_input_tokens,
                    actual_output_tokens,
                    actual_microdollars,
                    actual_cloud_turns,
                    input_utilization,
                    output_utilization,
                    cost_utilization,
                    turn_utilization,
                    recommendation,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM build_calibrations WHERE id = ?", (int(cursor.lastrowid),)
            ).fetchone()
        return _calibration_from_row(row)

    def list_calibrations(
        self,
        *,
        session_id: int | None = None,
        provider: str | None = None,
        limit: int = 100,
    ) -> list[BuildCalibration]:
        clauses: list[str] = []
        values: list[Any] = []
        if session_id is not None:
            clauses.append("session_id = ?")
            values.append(session_id)
        if provider is not None:
            clauses.append("provider = ?")
            values.append(provider.strip())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(max(1, min(int(limit), 1000)))
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM build_calibrations {where} ORDER BY id LIMIT ?",
                values,
            ).fetchall()
        return [_calibration_from_row(row) for row in rows]


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


def _task_from_row(row: sqlite3.Row) -> BuildTask:
    return BuildTask(
        id=int(row["id"]),
        session_id=int(row["session_id"]),
        phase=str(row["phase"]),
        position=int(row["position"]),
        kind=BuildTaskKind(str(row["kind"])),
        title=str(row["title"]),
        payload=json.loads(row["payload_json"]),
        dependencies=tuple(int(item) for item in json.loads(row["dependencies_json"])),
        acceptance=json.loads(row["acceptance_json"]),
        idempotency_key=str(row["idempotency_key"]),
        status=BuildTaskStatus(str(row["status"])),
        max_attempts=int(row["max_attempts"]),
        attempt_count=int(row["attempt_count"]),
        active_run_id=(
            int(row["active_run_id"]) if row["active_run_id"] is not None else None
        ),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _task_run_from_row(row: sqlite3.Row) -> BuildTaskRun:
    return BuildTaskRun(
        id=int(row["id"]),
        task_id=int(row["task_id"]),
        session_id=int(row["session_id"]),
        attempt_number=int(row["attempt_number"]),
        worker_id=str(row["worker_id"]),
        backend=str(row["backend"]),
        command=tuple(str(item) for item in json.loads(row["command_json"])),
        pid=int(row["pid"]) if row["pid"] is not None else None,
        status=BuildTaskStatus(str(row["status"])),
        result=json.loads(row["result_json"]),
        error=str(row["error"]),
        log_path=str(row["log_path"]),
        artifact_ids=tuple(
            int(item) for item in json.loads(row["artifact_ids_json"])
        ),
        started_at=str(row["started_at"]),
        finished_at=str(row["finished_at"]) if row["finished_at"] else None,
    )


def _artifact_from_row(row: sqlite3.Row) -> BuildArtifact:
    return BuildArtifact(
        id=int(row["id"]),
        session_id=int(row["session_id"]),
        task_id=int(row["task_id"]) if row["task_id"] is not None else None,
        run_id=int(row["run_id"]) if row["run_id"] is not None else None,
        kind=str(row["kind"]),
        uri=str(row["uri"]),
        metadata=json.loads(row["metadata_json"]),
        created_at=str(row["created_at"]),
    )


def _calibration_from_row(row: sqlite3.Row) -> BuildCalibration:
    return BuildCalibration(
        id=int(row["id"]),
        session_id=int(row["session_id"]),
        assessment_id=int(row["assessment_id"]),
        lease_id=int(row["lease_id"]),
        provider=str(row["provider"]),
        model=str(row["model"]),
        predicted_tier=BuildTier(str(row["predicted_tier"])),
        assessment_score=int(row["assessment_score"]),
        outcome=str(row["outcome"]),
        actual_input_tokens=int(row["actual_input_tokens"]),
        actual_output_tokens=int(row["actual_output_tokens"]),
        actual_microdollars=int(row["actual_microdollars"]),
        actual_cloud_turns=int(row["actual_cloud_turns"]),
        input_utilization=float(row["input_utilization"]),
        output_utilization=float(row["output_utilization"]),
        cost_utilization=float(row["cost_utilization"]),
        turn_utilization=float(row["turn_utilization"]),
        recommendation=str(row["recommendation"]),
        created_at=str(row["created_at"]),
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
