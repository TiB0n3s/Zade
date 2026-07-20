"""Transactional persistence primitives for autonomous project execution.

The store is deliberately smaller than the orchestration policy layered on top
of it.  Its job is to make a state transition, its append-only event, and an
optional founder notification indivisible while providing a compare-and-swap
version and a single-writer lease per project.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from .db import KernelDatabase, utc_now


class ProjectAutonomyConflict(RuntimeError):
    """The caller attempted to mutate a stale autonomy-state version."""


class ProjectAutonomyStore:
    def __init__(self, db: KernelDatabase):
        self.db = db

    def get(self, project_id: int) -> dict[str, Any]:
        project_id = _project_id(project_id)
        with self.db.connect() as conn:
            self._require_project(conn, project_id)
            row = conn.execute(
                "SELECT * FROM project_autonomy_states WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        return _state_from_row(row, project_id=project_id)

    def transition(
        self,
        project_id: int,
        *,
        expected_version: int,
        state: dict[str, Any],
        event: dict[str, Any],
        outbox: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        project_id = _project_id(project_id)
        expected_version = _version(expected_version)
        if not isinstance(state, dict):
            raise ValueError("Autonomy state must be a mapping.")
        if not isinstance(event, dict):
            raise ValueError("Autonomy event must be a mapping.")
        event_type = str(event.get("event_type") or "").strip()
        if not event_type:
            raise ValueError("Autonomy event requires event_type.")

        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_project(conn, project_id)
            current = conn.execute(
                "SELECT * FROM project_autonomy_states WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            current_state = _state_from_row(current, project_id=project_id)
            if current_state["version"] != expected_version:
                raise ProjectAutonomyConflict(
                    f"Project {project_id} autonomy version is "
                    f"{current_state['version']}, expected {expected_version}."
                )

            now = utc_now()
            next_version = expected_version + 1
            serialized = dict(state)
            phase = str(serialized.get("phase") or current_state.get("phase") or "planning")
            priority = str(
                serialized.get("priority") or current_state.get("priority") or "normal"
            )
            plan_revision = str(
                serialized.get("plan_revision")
                if serialized.get("plan_revision") is not None
                else current_state.get("plan_revision") or ""
            )
            created_at = str(current_state.get("created_at") or now)
            conn.execute(
                """
                INSERT INTO project_autonomy_states (
                  project_id, created_at, updated_at, version, phase, priority,
                  plan_revision, state_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                  updated_at = excluded.updated_at,
                  version = excluded.version,
                  phase = excluded.phase,
                  priority = excluded.priority,
                  plan_revision = excluded.plan_revision,
                  state_json = excluded.state_json
                """,
                (
                    project_id,
                    created_at,
                    now,
                    next_version,
                    phase,
                    priority,
                    plan_revision,
                    json.dumps(serialized, sort_keys=True),
                ),
            )
            self._insert_event(conn, project_id=project_id, event=event, now=now)
            if outbox is not None:
                self._insert_outbox(conn, project_id=project_id, outbox=outbox, now=now)

            updated = conn.execute(
                "SELECT * FROM project_autonomy_states WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        return _state_from_row(updated, project_id=project_id)

    def claim(
        self,
        project_id: int,
        *,
        owner: str,
        run_id: str,
        lease_seconds: int,
        expected_version: int,
    ) -> dict[str, Any] | None:
        project_id = _project_id(project_id)
        expected_version = _version(expected_version)
        owner = _required_text(owner, "Lease owner")
        run_id = _required_text(run_id, "Lease run_id")
        if isinstance(lease_seconds, bool) or int(lease_seconds) <= 0:
            raise ValueError("lease_seconds must be a positive integer.")
        lease_seconds = int(lease_seconds)

        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_project(conn, project_id)
            state_row = conn.execute(
                "SELECT version FROM project_autonomy_states WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            current_version = int(state_row["version"]) if state_row else 0
            if current_version != expected_version:
                raise ProjectAutonomyConflict(
                    f"Project {project_id} autonomy version is "
                    f"{current_version}, expected {expected_version}."
                )

            now_dt = datetime.now(UTC)
            existing = conn.execute(
                "SELECT * FROM project_autonomy_leases WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if existing is not None:
                expires_at = _parse_utc(existing["expires_at"])
                if expires_at > now_dt:
                    return None
                conn.execute(
                    "DELETE FROM project_autonomy_leases WHERE project_id = ?",
                    (project_id,),
                )

            acquired_at = now_dt.isoformat()
            expires_at = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
            conn.execute(
                """
                INSERT INTO project_autonomy_leases (
                  project_id, owner, run_id, acquired_at, expires_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (project_id, owner, run_id, acquired_at, expires_at),
            )
            row = conn.execute(
                "SELECT * FROM project_autonomy_leases WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def release(self, project_id: int, *, owner: str, run_id: str) -> bool:
        project_id = _project_id(project_id)
        owner = _required_text(owner, "Lease owner")
        run_id = _required_text(run_id, "Lease run_id")
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                DELETE FROM project_autonomy_leases
                WHERE project_id = ? AND owner = ? AND run_id = ?
                """,
                (project_id, owner, run_id),
            )
        return int(cur.rowcount or 0) == 1

    def renew(
        self,
        project_id: int,
        *,
        owner: str,
        run_id: str,
        lease_seconds: int,
    ) -> dict[str, Any] | None:
        project_id = _project_id(project_id)
        owner = _required_text(owner, "Lease owner")
        run_id = _required_text(run_id, "Lease run_id")
        if isinstance(lease_seconds, bool) or int(lease_seconds) <= 0:
            raise ValueError("lease_seconds must be a positive integer.")
        now_dt = datetime.now(UTC)
        expires_at = (now_dt + timedelta(seconds=int(lease_seconds))).isoformat()
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM project_autonomy_leases WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if (
                row is None
                or str(row["owner"]) != owner
                or str(row["run_id"]) != run_id
                or _parse_utc(row["expires_at"]) <= now_dt
            ):
                return None
            conn.execute(
                "UPDATE project_autonomy_leases SET expires_at = ? WHERE project_id = ?",
                (expires_at, project_id),
            )
            renewed = conn.execute(
                "SELECT * FROM project_autonomy_leases WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        return dict(renewed) if renewed is not None else None

    def clear_expired_leases(self) -> int:
        now_dt = datetime.now(UTC)
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT project_id, expires_at FROM project_autonomy_leases"
            ).fetchall()
            expired = [
                int(row["project_id"])
                for row in rows
                if _parse_utc(row["expires_at"]) <= now_dt
            ]
            if expired:
                conn.executemany(
                    "DELETE FROM project_autonomy_leases WHERE project_id = ?",
                    [(project_id,) for project_id in expired],
                )
        return len(expired)

    def clear_orphaned_process_leases(
        self, *, is_process_alive: Callable[[int], bool] | None = None
    ) -> int:
        """Release active leases left behind by a forcibly stopped kernel process."""
        alive = is_process_alive or _process_is_alive
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT project_id, owner FROM project_autonomy_leases"
            ).fetchall()
            orphaned = [
                (int(row["project_id"]), str(row["owner"]))
                for row in rows
                if (pid := _autonomy_owner_pid(str(row["owner"]))) is not None
                and not alive(pid)
            ]
            if orphaned:
                conn.executemany(
                    "DELETE FROM project_autonomy_leases WHERE project_id = ? AND owner = ?",
                    orphaned,
                )
        return len(orphaned)

    def due_outbox(self, limit: int = 50) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or int(limit) <= 0:
            raise ValueError("limit must be a positive integer.")
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM project_autonomy_outbox
                WHERE status IN ('pending', 'retry') AND next_attempt_at <= ?
                ORDER BY next_attempt_at, id
                LIMIT ?
                """,
                (utc_now(), int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_outbox_delivered(
        self, outbox_id: int, *, notification_id: int | None
    ) -> dict[str, Any]:
        outbox_id = _positive_id(outbox_id, "outbox_id")
        notification_id = (
            _positive_id(notification_id, "notification_id")
            if notification_id is not None
            else None
        )
        now = utc_now()
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                UPDATE project_autonomy_outbox
                SET updated_at = ?, status = 'delivered', attempts = attempts + 1,
                    notification_id = ?, delivered_at = ?, last_error = ''
                WHERE id = ? AND status IN ('pending', 'retry')
                """,
                (now, notification_id, now, outbox_id),
            )
            if int(cur.rowcount or 0) != 1:
                raise ValueError(f"Pending autonomy outbox row not found: {outbox_id}")
            row = conn.execute(
                "SELECT * FROM project_autonomy_outbox WHERE id = ?", (outbox_id,)
            ).fetchone()
        return dict(row)

    def reschedule_outbox(
        self,
        outbox_id: int,
        *,
        error: str,
        notification_id: int | None = None,
        delay_seconds: int = 300,
    ) -> dict[str, Any]:
        outbox_id = _positive_id(outbox_id, "outbox_id")
        notification_id = (
            _positive_id(notification_id, "notification_id")
            if notification_id is not None
            else None
        )
        if isinstance(delay_seconds, bool) or int(delay_seconds) < 0:
            raise ValueError("delay_seconds must be a non-negative integer.")
        now_dt = datetime.now(UTC)
        next_attempt_at = (now_dt + timedelta(seconds=int(delay_seconds))).isoformat()
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                UPDATE project_autonomy_outbox
                SET updated_at = ?, status = 'retry', attempts = attempts + 1,
                    next_attempt_at = ?, last_error = ?, notification_id = ?
                WHERE id = ? AND status IN ('pending', 'retry')
                """,
                (
                    now_dt.isoformat(),
                    next_attempt_at,
                    str(error or "delivery did not complete")[:400],
                    notification_id,
                    outbox_id,
                ),
            )
            if int(cur.rowcount or 0) != 1:
                raise ValueError(f"Pending autonomy outbox row not found: {outbox_id}")
            row = conn.execute(
                "SELECT * FROM project_autonomy_outbox WHERE id = ?", (outbox_id,)
            ).fetchone()
        return dict(row)

    @staticmethod
    def _require_project(conn: sqlite3.Connection, project_id: int) -> None:
        row = conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise ValueError(f"Project not found: {project_id}")

    @staticmethod
    def _insert_event(
        conn: sqlite3.Connection,
        *,
        project_id: int,
        event: dict[str, Any],
        now: str,
    ) -> int:
        typed_ids = {
            key: _optional_positive_id(event.get(key), key)
            for key in (
                "build_session_id",
                "work_item_id",
                "approval_request_id",
                "notification_id",
            )
        }
        metadata = event.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ValueError("Autonomy event metadata must be a mapping.")
        cur = conn.execute(
            """
            INSERT INTO project_events (
              created_at, project_id, event_type, detail, build_session_id,
              work_item_id, approval_request_id, notification_id, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                project_id,
                _required_text(event.get("event_type"), "event_type"),
                str(event.get("detail") or ""),
                typed_ids["build_session_id"],
                typed_ids["work_item_id"],
                typed_ids["approval_request_id"],
                typed_ids["notification_id"],
                json.dumps(metadata, sort_keys=True),
            ),
        )
        return int(cur.lastrowid or 0)

    @staticmethod
    def _insert_outbox(
        conn: sqlite3.Connection,
        *,
        project_id: int,
        outbox: dict[str, Any],
        now: str,
    ) -> int | None:
        if not isinstance(outbox, dict):
            raise ValueError("Autonomy outbox record must be a mapping.")
        topic = _required_text(outbox.get("topic"), "Outbox topic")
        severity = _required_text(outbox.get("severity") or "info", "Outbox severity")
        title = _required_text(outbox.get("title"), "Outbox title")
        dedupe_key = _required_text(outbox.get("dedupe_key"), "Outbox dedupe_key")
        next_attempt_at = str(outbox.get("next_attempt_at") or now)
        cur = conn.execute(
            """
            INSERT INTO project_autonomy_outbox (
              created_at, updated_at, project_id, topic, severity, title, body,
              dedupe_key, status, attempts, next_attempt_at, last_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, '')
            ON CONFLICT(dedupe_key) DO NOTHING
            """,
            (
                now,
                now,
                project_id,
                topic,
                severity,
                title,
                str(outbox.get("body") or ""),
                dedupe_key,
                next_attempt_at,
            ),
        )
        return int(cur.lastrowid) if cur.lastrowid else None


def _state_from_row(row: sqlite3.Row | None, *, project_id: int) -> dict[str, Any]:
    if row is None:
        return {
            "project_id": project_id,
            "created_at": "",
            "updated_at": "",
            "version": 0,
            "phase": "planning",
            "priority": "normal",
            "plan_revision": "",
        }
    state = json.loads(row["state_json"] or "{}")
    state.update(
        {
            "project_id": int(row["project_id"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "version": int(row["version"]),
            "phase": row["phase"],
            "priority": row["priority"],
            "plan_revision": row["plan_revision"],
        }
    )
    return state


def _project_id(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("project_id must be a positive integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("project_id must be a positive integer.") from exc
    if parsed <= 0:
        raise ValueError("project_id must be a positive integer.")
    return parsed


def _version(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("expected_version must be a non-negative integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("expected_version must be a non-negative integer.") from exc
    if parsed < 0:
        raise ValueError("expected_version must be a non-negative integer.")
    return parsed


def _optional_positive_id(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer or null.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a positive integer or null.") from exc
    if parsed <= 0:
        raise ValueError(f"{field} must be a positive integer or null.")
    return parsed


def _positive_id(value: Any, field: str) -> int:
    parsed = _optional_positive_id(value, field)
    if parsed is None:
        raise ValueError(f"{field} must be a positive integer.")
    return parsed


def _required_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} is required.")
    return text


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _autonomy_owner_pid(owner: str) -> int | None:
    prefix = "zade-project-autonomy:"
    if not owner.startswith(prefix):
        return None
    try:
        pid = int(owner.removeprefix(prefix))
    except ValueError:
        return None
    return pid if pid > 0 else None


def _process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
