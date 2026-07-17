from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 26

# Column additions to EXISTING tables, applied idempotently by migrate() on every
# start (CREATE ... IF NOT EXISTS only creates whole tables, never new columns).
# Every historical schema change so far added a new table, so this is empty — but
# any future `ALTER TABLE ... ADD COLUMN` must be registered here as
# (table, column_name, "column_name TYPE [DEFAULT ...]") so upgraded DBs receive it.
COLUMN_PATCHES: tuple[tuple[str, str, str], ...] = (
    # v23: conversation -> memory distillation cursor. Tracks which turns have been
    # promoted into searchable memory so promotion is incremental and idempotent.
    ("conversations", "distilled_through_turn_id", "distilled_through_turn_id INTEGER"),
    # v25: content hash on document-chunk embeddings, so re-embedding (e.g. after
    # adding retrieval prefixes) can skip unchanged chunks.
    ("chunk_embeddings", "content_hash", "content_hash TEXT NOT NULL DEFAULT ''"),
    # v26: grounding quarantine. External-agent-authored memory is held OUT of the
    # grounding/recall context (the prompt-injection surface) until a founder
    # releases it. 'active' = eligible for recall; 'quarantined' = stored and
    # explicitly searchable, but never auto-injected into Zade's reasoning.
    ("memories", "grounding_status", "grounding_status TEXT NOT NULL DEFAULT 'active'"),
)


# Key-name fragments whose values are redacted before landing in the (plaintext,
# queryable) audit log. The "*_env" naming pattern is exempt — it names an env
# var, not a secret. Defense in depth: nothing should route a secret here, but if
# it does, it is scrubbed.
_AUDIT_SECRET_FRAGMENTS = ("password", "passwd", "pwd", "secret", "token", "credential", "apikey", "api_key", "private_key", "access_key")


def _normalize_workspace_path(raw: str) -> str:
    """Case-folded, separator-normalized form of a workspace path for matching
    (Windows paths are case-insensitive and arrive with mixed separators). No
    filesystem resolution — the directory may no longer exist."""
    cleaned = (raw or "").strip().strip("\"'")
    if not cleaned:
        return ""
    return os.path.normpath(cleaned).casefold()


def _redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, inner in value.items():
            key_l = str(key).lower()
            if not key_l.endswith("_env") and any(frag in key_l for frag in _AUDIT_SECRET_FRAGMENTS):
                redacted[key] = "[redacted]"
            else:
                redacted[key] = _redact_secrets(inner)
        return redacted
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    return value


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class MemoryRecord:
    id: int
    created_at: str
    kind: str
    title: str
    content: str
    source: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class WorkItem:
    id: int
    created_at: str
    updated_at: str
    kind: str
    title: str
    detail: str
    action: str
    target: str
    permission_tier: str
    authority_decision: str
    status: str
    priority: int
    source: str
    due_at: str | None
    last_error: str
    result: dict[str, Any]
    metadata: dict[str, Any]
    unique_key: str | None


@dataclass(frozen=True)
class ApprovalRequest:
    id: int
    created_at: str
    updated_at: str
    source_type: str
    source_id: int | None
    title: str
    detail: str
    action: str
    target: str
    permission_tier: str
    authority_decision: str
    authority: dict[str, Any]
    status: str
    requested_by: str
    resolved_by: str
    resolved_at: str | None
    resolution_note: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ApprovalTrainingEvent:
    id: int
    created_at: str
    approval_request_id: int | None
    work_item_id: int | None
    event_type: str
    outcome: str
    actor: str
    note: str
    action: str
    target: str
    permission_tier: str
    authority_decision: str
    authority: dict[str, Any]
    request_snapshot: dict[str, Any]
    work_item_snapshot: dict[str, Any]
    edits: dict[str, Any]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ModelCall:
    id: int
    created_at: str
    operation: str
    model: str
    role: str
    status: str
    latency_ms: int
    prompt_chars: int
    response_chars: int
    think: bool | None
    error: str
    metadata: dict[str, Any]


class KernelDatabase:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def migrate(self) -> None:
        with self.connect() as conn:
            # The base schema is fully idempotent (CREATE TABLE/INDEX IF NOT
            # EXISTS), so new tables always land on upgrade.
            conn.executescript(SCHEMA_SQL)
            # Column-level reconciliation: unlike new tables, a new column on an
            # EXISTING table is NOT applied by CREATE ... IF NOT EXISTS, so we add
            # any missing column explicitly. Register future column additions in
            # COLUMN_PATCHES; this pass is idempotent and safe to run every start.
            for table, column, ddl in COLUMN_PATCHES:
                self._add_column_if_missing(conn, table, column, ddl)
            conn.execute("INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)", ("version", str(SCHEMA_VERSION)))
            conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")

    def _add_column_if_missing(self, conn: sqlite3.Connection, table: str, column: str, ddl: str) -> bool:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if not existing:
            return False  # table itself does not exist yet (fresh DB handled by CREATE)
        if column in existing:
            return False
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        return True

    def schema_version(self) -> int:
        with self.connect() as conn:
            row = conn.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row else 0

    # ---- work plans (the work ledger) ---------------------------------------
    # Steps live as ROWS, not prose: chat threads accumulate synthetic,
    # fabricated, and meta turns, and re-deriving "step 5" from them by text
    # search proved endlessly poisonable. The ledger is materialized once and
    # updated only from verified run outcomes.

    def create_work_plan(
        self,
        *,
        conversation_id: int | None,
        title: str,
        workspace: str = "",
        steps: list[tuple[int, str]],
    ) -> int:
        now = utc_now()
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO work_plans (created_at, updated_at, conversation_id, title, workspace, status) "
                "VALUES (?, ?, ?, ?, ?, 'active')",
                (now, now, conversation_id, title[:200], workspace[:400]),
            )
            plan_id = int(cur.lastrowid or 0)
            for step_number, instructions in steps:
                conn.execute(
                    "INSERT OR IGNORE INTO work_plan_steps "
                    "(plan_id, step_number, instructions, status, updated_at) "
                    "VALUES (?, ?, ?, 'pending', ?)",
                    (plan_id, int(step_number), instructions[:4000], now),
                )
        return plan_id

    def get_active_work_plan(self, conversation_id: int | None) -> dict[str, Any] | None:
        if not conversation_id:
            return None
        with self.connect() as conn:
            plan = conn.execute(
                "SELECT * FROM work_plans WHERE conversation_id = ? AND status = 'active' "
                "ORDER BY id DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            if plan is None:
                return None
            steps = conn.execute(
                "SELECT step_number, instructions, status, last_item_id, last_outcome "
                "FROM work_plan_steps WHERE plan_id = ? ORDER BY step_number",
                (plan["id"],),
            ).fetchall()
        return {
            "id": int(plan["id"]),
            "conversation_id": plan["conversation_id"],
            "title": str(plan["title"]),
            "workspace": str(plan["workspace"] or ""),
            "status": str(plan["status"]),
            "steps": [dict(step) for step in steps],
        }

    def update_work_plan_step(
        self,
        plan_id: int,
        step_number: int,
        *,
        status: str,
        last_item_id: int | None = None,
        last_outcome: str = "",
    ) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                "UPDATE work_plan_steps SET status = ?, last_item_id = COALESCE(?, last_item_id), "
                "last_outcome = ?, updated_at = ? WHERE plan_id = ? AND step_number = ?",
                (status[:40], last_item_id, last_outcome[:400], now, plan_id, step_number),
            )
            conn.execute(
                "UPDATE work_plans SET updated_at = ? WHERE id = ?", (now, plan_id)
            )

    def set_work_plan_workspace(self, plan_id: int, workspace: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE work_plans SET workspace = ?, updated_at = ? WHERE id = ?",
                (workspace[:400], utc_now(), plan_id),
            )

    def list_work_items_for_workspace(
        self, workspace: str, *, limit: int = 12
    ) -> list[dict[str, Any]]:
        """The delegated-run record for a project workspace: recent work items
        whose metadata targets that directory, newest first. This is what lets
        "what has been done on <project>?" be answered from verified rows even
        when the asking thread has no materialized work plan of its own."""
        target = _normalize_workspace_path(workspace)
        if not target:
            return []
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, created_at, kind, title, status, metadata_json "
                "FROM work_items ORDER BY id DESC LIMIT 400"
            ).fetchall()
        matched: list[dict[str, Any]] = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, ValueError):
                metadata = {}
            if _normalize_workspace_path(str(metadata.get("workspace") or "")) != target:
                continue
            matched.append(
                {
                    "id": int(row["id"]),
                    "created_at": str(row["created_at"]),
                    "kind": str(row["kind"]),
                    "title": str(row["title"]),
                    "status": str(row["status"]),
                    "task": str(metadata.get("task") or ""),
                }
            )
            if len(matched) >= max(1, limit):
                break
        return matched

    def claim_next_work_item(self) -> WorkItem | None:
        """Atomically transition the highest-priority pending item to 'running'
        and return it, so two concurrent runners (scheduler + API) can never
        both dispatch the same item. SQLite serializes the single UPDATE."""
        with self.connect() as conn:
            row = conn.execute(
                """
                UPDATE work_items
                SET status = 'running', updated_at = ?
                WHERE id = (
                    SELECT id FROM work_items
                    WHERE status = 'pending' AND (due_at IS NULL OR due_at <= ?)
                    ORDER BY priority DESC, id ASC
                    LIMIT 1
                )
                RETURNING *
                """,
                (utc_now(), utc_now()),
            ).fetchone()
            return _work_item_from_row(row) if row else None

    def audit(
        self,
        *,
        actor: str,
        action: str,
        target: str,
        permission_tier: str,
        status: str,
        details: dict[str, Any] | None = None,
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO audit_events (created_at, actor, action, target, permission_tier, status, details_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (utc_now(), actor, action, target, permission_tier, status, json.dumps(_redact_secrets(details or {}), sort_keys=True)),
            )
            return int(cur.lastrowid)

    def add_memory(
        self,
        *,
        kind: str,
        title: str,
        content: str,
        source: str = "local",
        metadata: dict[str, Any] | None = None,
        grounding_status: str = "active",
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO memories (created_at, kind, title, content, source, metadata_json, grounding_status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (utc_now(), kind, title, content, source, json.dumps(metadata or {}, sort_keys=True), grounding_status),
            )
            memory_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO memory_fts (rowid, title, content, kind, source) VALUES (?, ?, ?, ?, ?)",
                (memory_id, title, content, kind, source),
            )
            return memory_id

    def delete_memory(self, memory_id: int) -> dict[str, Any] | None:
        """Remove a memory row plus its FTS entry; returns the deleted record's
        summary, or None if no such memory exists. Relationships that cited the
        memory as evidence keep the edge but lose the citation (FK is enforced,
        so the reference must be cleared before the row can go)."""
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
            if row is None:
                return None
            # External-content FTS5 requires the old column values to delete.
            conn.execute(
                "INSERT INTO memory_fts (memory_fts, rowid, title, content, kind, source) VALUES ('delete', ?, ?, ?, ?, ?)",
                (memory_id, row["title"], row["content"], row["kind"], row["source"]),
            )
            conn.execute(
                "UPDATE relationships SET evidence_memory_id = NULL WHERE evidence_memory_id = ?",
                (memory_id,),
            )
            conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            return {"id": memory_id, "kind": row["kind"], "title": row["title"], "source": row["source"]}

    def list_memories_by_grounding_status(self, status: str, *, limit: int = 200) -> list[dict[str, Any]]:
        """Memories in a grounding state — used to surface the quarantine queue."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, created_at, kind, title, content, source, metadata_json, grounding_status
                FROM memories WHERE grounding_status = ? ORDER BY id DESC LIMIT ?
                """,
                (status, limit),
            ).fetchall()
        out = []
        for row in rows:
            data = _memory_row_to_dict(row)
            data["grounding_status"] = row["grounding_status"]
            out.append(data)
        return out

    def set_memory_grounding_status(self, memory_id: int, status: str) -> dict[str, Any] | None:
        """Release a memory into grounding ('active') or hold it ('quarantined').
        Returns the updated summary, or None if the memory does not exist."""
        with self.connect() as conn:
            row = conn.execute("SELECT id, title, source FROM memories WHERE id = ?", (memory_id,)).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE memories SET grounding_status = ? WHERE id = ?", (status, memory_id))
        return {"id": memory_id, "title": row["title"], "source": row["source"], "grounding_status": status}

    def memory_stats(self) -> dict[str, int]:
        with self.connect() as conn:
            hot = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            documents = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            chunks = conn.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0]
        return {"hot_memories": int(hot), "cold_documents": int(documents), "cold_chunks": int(chunks)}

    def create_ingestion_job(self, *, job_type: str, source: str, metadata: dict[str, Any] | None = None) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO ingestion_jobs (created_at, updated_at, job_type, source, status, metadata_json)
                VALUES (?, ?, ?, ?, 'running', ?)
                """,
                (utc_now(), utc_now(), job_type, source, json.dumps(metadata or {}, sort_keys=True)),
            )
            return int(cur.lastrowid)

    def update_ingestion_job(
        self,
        job_id: int,
        *,
        status: str,
        documents_count: int = 0,
        chunks_count: int = 0,
        error: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE ingestion_jobs
                SET updated_at = ?, status = ?, documents_count = ?, chunks_count = ?, error = ?, metadata_json = ?
                WHERE id = ?
                """,
                (utc_now(), status, documents_count, chunks_count, error, json.dumps(metadata or {}, sort_keys=True), job_id),
            )

    def recent_ingestion_jobs(self, limit: int = 25) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM ingestion_jobs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) | {"metadata": json.loads(row["metadata_json"] or "{}")} for row in rows]

    def enqueue_work_item(
        self,
        *,
        kind: str,
        title: str,
        detail: str,
        action: str,
        target: str,
        permission_tier: str,
        priority: int = 50,
        source: str = "local",
        due_at: str | None = None,
        metadata: dict[str, Any] | None = None,
        unique_key: str | None = None,
    ) -> tuple[int, bool]:
        with self.connect() as conn:
            if unique_key:
                existing = conn.execute("SELECT id FROM work_items WHERE unique_key = ?", (unique_key,)).fetchone()
                if existing:
                    return int(existing["id"]), False
            cur = conn.execute(
                """
                INSERT INTO work_items (
                  created_at, updated_at, kind, title, detail, action, target, permission_tier,
                  priority, source, due_at, metadata_json, unique_key
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    utc_now(),
                    kind,
                    title,
                    detail,
                    action,
                    target,
                    permission_tier,
                    priority,
                    source,
                    due_at,
                    json.dumps(metadata or {}, sort_keys=True),
                    unique_key,
                ),
            )
            return int(cur.lastrowid), True

    def list_work_items(self, *, status: str | None = None, limit: int = 50) -> list[WorkItem]:
        with self.connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM work_items WHERE status = ? ORDER BY priority DESC, id ASC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM work_items ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [_work_item_from_row(row) for row in rows]

    def get_work_item(self, item_id: int) -> WorkItem | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM work_items WHERE id = ?", (item_id,)).fetchone()
            return _work_item_from_row(row) if row else None

    def next_work_item(self) -> WorkItem | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM work_items
                WHERE status = 'pending' AND (due_at IS NULL OR due_at <= ?)
                ORDER BY priority DESC, id ASC
                LIMIT 1
                """,
                (utc_now(),),
            ).fetchone()
            return _work_item_from_row(row) if row else None

    def update_work_item(
        self,
        item_id: int,
        *,
        status: str,
        authority_decision: str | None = None,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE work_items
                SET updated_at = ?,
                    status = ?,
                    authority_decision = COALESCE(?, authority_decision),
                    result_json = ?,
                    last_error = ?
                WHERE id = ?
                """,
                (
                    utc_now(),
                    status,
                    authority_decision,
                    json.dumps(result or {}, sort_keys=True),
                    error,
                    item_id,
                ),
            )

    def update_work_item_proposal(
        self,
        item_id: int,
        *,
        title: str | None = None,
        detail: str | None = None,
        action: str | None = None,
        target: str | None = None,
        permission_tier: str | None = None,
        priority: int | None = None,
        source: str | None = None,
        due_at: str | None = None,
        metadata: dict[str, Any] | None = None,
        status: str | None = None,
        authority_decision: str | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> WorkItem:
        with self.connect() as conn:
            current = conn.execute("SELECT * FROM work_items WHERE id = ?", (item_id,)).fetchone()
            if current is None:
                raise ValueError(f"Work item not found: {item_id}")
            merged_metadata = (
                {**json.loads(current["metadata_json"] or "{}"), **metadata}
                if metadata is not None
                else json.loads(current["metadata_json"] or "{}")
            )
            merged_result = result if result is not None else json.loads(current["result_json"] or "{}")
            conn.execute(
                """
                UPDATE work_items
                SET updated_at = ?,
                    title = COALESCE(?, title),
                    detail = COALESCE(?, detail),
                    action = COALESCE(?, action),
                    target = COALESCE(?, target),
                    permission_tier = COALESCE(?, permission_tier),
                    priority = COALESCE(?, priority),
                    source = COALESCE(?, source),
                    due_at = COALESCE(?, due_at),
                    metadata_json = ?,
                    status = COALESCE(?, status),
                    authority_decision = COALESCE(?, authority_decision),
                    result_json = ?,
                    last_error = COALESCE(?, last_error)
                WHERE id = ?
                """,
                (
                    utc_now(),
                    title,
                    detail,
                    action,
                    target,
                    permission_tier,
                    priority,
                    source,
                    due_at,
                    json.dumps(merged_metadata, sort_keys=True),
                    status,
                    authority_decision,
                    json.dumps(merged_result, sort_keys=True),
                    error,
                    item_id,
                ),
            )
            row = conn.execute("SELECT * FROM work_items WHERE id = ?", (item_id,)).fetchone()
            return _work_item_from_row(row)

    def work_queue_counts(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS count FROM work_items GROUP BY status").fetchall()
            return {str(row["status"]): int(row["count"]) for row in rows}

    def ensure_approval_request(
        self,
        *,
        source_type: str,
        source_id: int | None,
        title: str,
        detail: str,
        action: str,
        target: str,
        permission_tier: str,
        authority_decision: str,
        authority: dict[str, Any] | None = None,
        requested_by: str = "system",
        metadata: dict[str, Any] | None = None,
    ) -> tuple[ApprovalRequest, bool]:
        with self.connect() as conn:
            if source_id is not None:
                # Match the unresolved set used by get_pending_approval_for_source:
                # a deferred request is still open, so never create a duplicate
                # beside it.
                existing = conn.execute(
                    """
                    SELECT *
                    FROM approval_requests
                    WHERE source_type = ? AND source_id = ? AND status IN ('pending', 'deferred')
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (source_type, source_id),
                ).fetchone()
                if existing:
                    return _approval_request_from_row(existing), False
            now = utc_now()
            cur = conn.execute(
                """
                INSERT INTO approval_requests (
                  created_at, updated_at, source_type, source_id, title, detail, action, target,
                  permission_tier, authority_decision, authority_json, status, requested_by,
                  metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    now,
                    now,
                    source_type,
                    source_id,
                    title,
                    detail,
                    action,
                    target,
                    permission_tier,
                    authority_decision,
                    json.dumps(authority or {}, sort_keys=True),
                    requested_by,
                    json.dumps(metadata or {}, sort_keys=True),
                ),
            )
            row = conn.execute("SELECT * FROM approval_requests WHERE id = ?", (int(cur.lastrowid),)).fetchone()
            return _approval_request_from_row(row), True

    def list_approval_requests(self, *, status: str | None = None, limit: int = 50) -> list[ApprovalRequest]:
        with self.connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM approval_requests WHERE status = ? ORDER BY id DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM approval_requests ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [_approval_request_from_row(row) for row in rows]

    def get_approval_request(self, request_id: int) -> ApprovalRequest | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM approval_requests WHERE id = ?", (request_id,)).fetchone()
            return _approval_request_from_row(row) if row else None

    def get_pending_approval_for_source(self, *, source_type: str, source_id: int) -> ApprovalRequest | None:
        """Latest still-unresolved approval request for a source.

        A deferred request is unresolved too — it is parked, not decided —
        and approve/deny explicitly accept deferred requests. Excluding it
        here made deferred work items undecidable through the work-item
        routes (only the approvals console could reach them by request id).
        """
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM approval_requests
                WHERE source_type = ? AND source_id = ? AND status IN ('pending', 'deferred')
                ORDER BY id DESC
                LIMIT 1
                """,
                (source_type, source_id),
            ).fetchone()
            return _approval_request_from_row(row) if row else None

    def resolve_approval_request(
        self,
        request_id: int,
        *,
        status: str,
        resolved_by: str,
        resolution_note: str = "",
    ) -> ApprovalRequest:
        with self.connect() as conn:
            resolved_at = utc_now()
            conn.execute(
                """
                UPDATE approval_requests
                SET updated_at = ?, status = ?, resolved_by = ?, resolved_at = ?, resolution_note = ?
                WHERE id = ?
                """,
                (resolved_at, status, resolved_by, resolved_at, resolution_note, request_id),
            )
            row = conn.execute("SELECT * FROM approval_requests WHERE id = ?", (request_id,)).fetchone()
            return _approval_request_from_row(row)

    def update_approval_request(
        self,
        request_id: int,
        *,
        title: str | None = None,
        detail: str | None = None,
        action: str | None = None,
        target: str | None = None,
        permission_tier: str | None = None,
        authority_decision: str | None = None,
        authority: dict[str, Any] | None = None,
        status: str | None = None,
        requested_by: str | None = None,
        resolved_by: str | None = None,
        resolved_at: str | None = None,
        resolution_note: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ApprovalRequest:
        with self.connect() as conn:
            current = conn.execute("SELECT * FROM approval_requests WHERE id = ?", (request_id,)).fetchone()
            if current is None:
                raise ValueError(f"Approval request not found: {request_id}")
            merged_metadata = (
                {**json.loads(current["metadata_json"] or "{}"), **metadata}
                if metadata is not None
                else json.loads(current["metadata_json"] or "{}")
            )
            authority_json = authority if authority is not None else json.loads(current["authority_json"] or "{}")
            conn.execute(
                """
                UPDATE approval_requests
                SET updated_at = ?,
                    title = COALESCE(?, title),
                    detail = COALESCE(?, detail),
                    action = COALESCE(?, action),
                    target = COALESCE(?, target),
                    permission_tier = COALESCE(?, permission_tier),
                    authority_decision = COALESCE(?, authority_decision),
                    authority_json = ?,
                    status = COALESCE(?, status),
                    requested_by = COALESCE(?, requested_by),
                    resolved_by = COALESCE(?, resolved_by),
                    resolved_at = COALESCE(?, resolved_at),
                    resolution_note = COALESCE(?, resolution_note),
                    metadata_json = ?
                WHERE id = ?
                """,
                (
                    utc_now(),
                    title,
                    detail,
                    action,
                    target,
                    permission_tier,
                    authority_decision,
                    json.dumps(authority_json, sort_keys=True),
                    status,
                    requested_by,
                    resolved_by,
                    resolved_at,
                    resolution_note,
                    json.dumps(merged_metadata, sort_keys=True),
                    request_id,
                ),
            )
            row = conn.execute("SELECT * FROM approval_requests WHERE id = ?", (request_id,)).fetchone()
            return _approval_request_from_row(row)

    def record_approval_training_event(
        self,
        *,
        approval_request_id: int | None,
        work_item_id: int | None,
        event_type: str,
        outcome: str,
        actor: str,
        note: str = "",
        action: str = "",
        target: str = "",
        permission_tier: str = "",
        authority_decision: str = "",
        authority: dict[str, Any] | None = None,
        request_snapshot: dict[str, Any] | None = None,
        work_item_snapshot: dict[str, Any] | None = None,
        edits: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO approval_training_events (
                  created_at, approval_request_id, work_item_id, event_type, outcome, actor, note,
                  action, target, permission_tier, authority_decision, authority_json,
                  request_snapshot_json, work_item_snapshot_json, edits_json, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    approval_request_id,
                    work_item_id,
                    event_type,
                    outcome,
                    actor,
                    note,
                    action,
                    target,
                    permission_tier,
                    authority_decision,
                    json.dumps(authority or {}, sort_keys=True),
                    json.dumps(request_snapshot or {}, sort_keys=True),
                    json.dumps(work_item_snapshot or {}, sort_keys=True),
                    json.dumps(edits or {}, sort_keys=True),
                    json.dumps(metadata or {}, sort_keys=True),
                ),
            )
            return int(cur.lastrowid)

    def list_approval_training_events(
        self,
        *,
        approval_request_id: int | None = None,
        outcome: str | None = None,
        limit: int = 50,
    ) -> list[ApprovalTrainingEvent]:
        clauses: list[str] = []
        params: list[Any] = []
        if approval_request_id is not None:
            clauses.append("approval_request_id = ?")
            params.append(approval_request_id)
        if outcome:
            clauses.append("outcome = ?")
            params.append(outcome)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM approval_training_events {where} ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()
            return [_approval_training_event_from_row(row) for row in rows]

    def upsert_document(
        self,
        *,
        title: str,
        source_uri: str,
        content_hash: str,
        media_type: str,
        size_bytes: int,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[int, bool]:
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM documents WHERE content_hash = ?",
                (content_hash,),
            ).fetchone()
            if existing:
                return int(existing["id"]), False
            cur = conn.execute(
                """
                INSERT INTO documents (created_at, title, source_uri, content_hash, media_type, size_bytes, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    title,
                    source_uri,
                    content_hash,
                    media_type,
                    size_bytes,
                    json.dumps(metadata or {}, sort_keys=True),
                ),
            )
            return int(cur.lastrowid), True

    def add_document_chunk(
        self,
        *,
        document_id: int,
        chunk_index: int,
        text: str,
        char_start: int,
        char_end: int,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO document_chunks (created_at, document_id, chunk_index, text, char_start, char_end, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (utc_now(), document_id, chunk_index, text, char_start, char_end, json.dumps(metadata or {}, sort_keys=True)),
            )
            chunk_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO document_chunk_fts (rowid, text) VALUES (?, ?)",
                (chunk_id, text),
            )
            return chunk_id

    def add_chunk_embedding(self, *, chunk_id: int, model: str, vector: list[float], content_hash: str = "") -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT OR REPLACE INTO chunk_embeddings (chunk_id, model, dimensions, content_hash, vector_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (chunk_id, model, len(vector), content_hash, json.dumps(vector), utc_now()),
            )
            return int(cur.lastrowid)

    def list_all_chunks(self, *, limit: int = 1_000_000) -> list[dict[str, Any]]:
        """id/text for every document chunk — the source the chunk embedding index
        is (re)built from."""
        with self.connect() as conn:
            rows = conn.execute("SELECT id, text FROM document_chunks ORDER BY id LIMIT ?", (limit,)).fetchall()
        return [{"id": int(row["id"]), "text": str(row["text"])} for row in rows]

    def list_chunk_embedding_hashes(self, model: str) -> dict[int, str]:
        """{chunk_id: content_hash} for one embedding model, so a rebuild can skip
        chunks whose embedded text is unchanged."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT chunk_id, content_hash FROM chunk_embeddings WHERE model = ?", (model,)
            ).fetchall()
        return {int(row["chunk_id"]): str(row["content_hash"]) for row in rows}

    def document_chunk_count(self, document_id: int) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM document_chunks WHERE document_id = ?",
                (document_id,),
            ).fetchone()
            return int(row["count"])

    def semantic_search_chunks(self, query_vector: list[float], limit: int = 8) -> list[dict[str, Any]]:
        if not query_vector:
            return []
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                  c.id AS chunk_id,
                  c.chunk_index,
                  c.text,
                  c.char_start,
                  c.char_end,
                  c.metadata_json AS chunk_metadata_json,
                  d.id AS document_id,
                  d.title AS document_title,
                  d.source_uri,
                  d.media_type,
                  e.model AS embedding_model,
                  e.vector_json
                FROM chunk_embeddings e
                JOIN document_chunks c ON c.id = e.chunk_id
                JOIN documents d ON d.id = c.document_id
                """
            ).fetchall()
        scored = []
        for row in rows:
            vector = json.loads(row["vector_json"] or "[]")
            score = cosine_similarity(query_vector, vector)
            scored.append(
                {
                    "score": score,
                    "chunk_id": row["chunk_id"],
                    "chunk_index": row["chunk_index"],
                    "text": row["text"],
                    "char_start": row["char_start"],
                    "char_end": row["char_end"],
                    "chunk_metadata": json.loads(row["chunk_metadata_json"] or "{}"),
                    "document_id": row["document_id"],
                    "document_title": row["document_title"],
                    "source_uri": row["source_uri"],
                    "media_type": row["media_type"],
                    "embedding_model": row["embedding_model"],
                }
            )
        scored.sort(key=lambda item: item["score"], reverse=True)
        return scored[:limit]

    def keyword_search_chunks(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        query = query.strip()
        if not query:
            return []
        with self.connect() as conn:
            rows = self._keyword_chunk_fts(conn, query, limit)
            if not rows:
                token_query = _token_fts_query(query)
                if token_query and token_query != query:
                    rows = self._keyword_chunk_fts(conn, token_query, limit)
        results = []
        for rank, row in enumerate(rows, start=1):
            results.append(
                {
                    "keyword_rank": rank,
                    "search_rank": float(row["search_rank"] or 0.0),
                    "chunk_id": row["chunk_id"],
                    "chunk_index": row["chunk_index"],
                    "text": row["text"],
                    "char_start": row["char_start"],
                    "char_end": row["char_end"],
                    "chunk_metadata": json.loads(row["chunk_metadata_json"] or "{}"),
                    "document_id": row["document_id"],
                    "document_title": row["document_title"],
                    "source_uri": row["source_uri"],
                    "media_type": row["media_type"],
                }
            )
        return results

    def _keyword_chunk_fts(self, conn: sqlite3.Connection, query: str, limit: int) -> list[sqlite3.Row]:
        try:
            return conn.execute(
                """
                SELECT
                  bm25(document_chunk_fts) AS search_rank,
                  c.id AS chunk_id,
                  c.chunk_index,
                  c.text,
                  c.char_start,
                  c.char_end,
                  c.metadata_json AS chunk_metadata_json,
                  d.id AS document_id,
                  d.title AS document_title,
                  d.source_uri,
                  d.media_type
                FROM document_chunk_fts f
                JOIN document_chunks c ON c.id = f.rowid
                JOIN documents d ON d.id = c.document_id
                WHERE document_chunk_fts MATCH ?
                ORDER BY bm25(document_chunk_fts)
                LIMIT ?
                """,
                (query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []

    def upsert_skill_embedding(self, *, skill_id: int, model: str, vector: list[float], content_hash: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO skill_embeddings (skill_id, model, dimensions, content_hash, vector_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (skill_id, model, len(vector), content_hash, json.dumps(vector), utc_now()),
            )

    def get_skill_embedding_hash(self, skill_id: int) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT content_hash FROM skill_embeddings WHERE skill_id = ?",
                (skill_id,),
            ).fetchone()
        return str(row["content_hash"]) if row else None

    def list_skill_embeddings(self, *, enabled_only: bool = True) -> list[dict[str, Any]]:
        where = "WHERE s.enabled = 1" if enabled_only else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT e.skill_id, e.model, e.vector_json, s.name
                FROM skill_embeddings e
                JOIN skill_registry s ON s.id = e.skill_id
                {where}
                """
            ).fetchall()
        return [
            {
                "skill_id": int(row["skill_id"]),
                "name": str(row["name"]),
                "model": str(row["model"]),
                "vector": json.loads(row["vector_json"] or "[]"),
            }
            for row in rows
        ]

    def search_memories(self, query: str, limit: int = 8, *, include_quarantined: bool = True) -> list[MemoryRecord]:
        """Keyword/FTS recall over memory. ``include_quarantined`` defaults True so
        an explicit search (e.g. the agent's memory.search tool) still finds every
        record; the grounding recall path passes False so external-agent memory
        held in quarantine never auto-enters Zade's reasoning context."""
        query = query.strip()
        if not query:
            return []
        with self.connect() as conn:
            rows = self._search_fts(conn, query, limit, include_quarantined=include_quarantined)
            if not rows:
                token_query = _token_fts_query(query)
                if token_query and token_query != query:
                    rows = self._search_fts(conn, token_query, limit, include_quarantined=include_quarantined)
            if not rows:
                rows = self._search_like(conn, query, limit, include_quarantined=include_quarantined)
            return [_memory_from_row(row) for row in rows]

    def _search_fts(
        self, conn: sqlite3.Connection, query: str, limit: int, *, include_quarantined: bool = True
    ) -> list[sqlite3.Row]:
        quarantine_clause = "" if include_quarantined else " AND m.grounding_status != 'quarantined'"
        try:
            return conn.execute(
                f"""
                SELECT m.*
                FROM memory_fts f
                JOIN memories m ON m.id = f.rowid
                WHERE memory_fts MATCH ?{quarantine_clause}
                ORDER BY bm25(memory_fts)
                LIMIT ?
                """,
                (query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []

    def _search_like(
        self, conn: sqlite3.Connection, query: str, limit: int, *, include_quarantined: bool = True
    ) -> list[sqlite3.Row]:
        tokens = _query_tokens(query)
        if not tokens:
            tokens = [query]
        clauses = []
        params: list[Any] = []
        for token in tokens[:8]:
            clauses.append("(title LIKE ? OR content LIKE ? OR kind LIKE ? OR source LIKE ?)")
            params.extend([f"%{token}%", f"%{token}%", f"%{token}%", f"%{token}%"])
        params.append(limit)
        quarantine_clause = "" if include_quarantined else " AND grounding_status != 'quarantined'"
        return conn.execute(
            f"""
            SELECT *
            FROM memories
            WHERE ({' OR '.join(clauses)}){quarantine_clause}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

    def upsert_memory_embedding(self, *, memory_id: int, model: str, vector: list[float], content_hash: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO memory_embeddings (memory_id, model, dimensions, content_hash, vector_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (memory_id, model, len(vector), content_hash, json.dumps(vector), utc_now()),
            )

    def list_memory_embedding_hashes(self, model: str) -> dict[int, str]:
        """{memory_id: content_hash} for one embedding model, so a rebuild can skip
        memories whose title+content is unchanged."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT memory_id, content_hash FROM memory_embeddings WHERE model = ?",
                (model,),
            ).fetchall()
        return {int(row["memory_id"]): str(row["content_hash"]) for row in rows}

    def list_all_memories(self, *, limit: int = 100_000) -> list[dict[str, Any]]:
        """id/title/content for every memory, oldest first — the source the memory
        embedding index is (re)built from."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, title, content FROM memories ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
        return [{"id": int(row["id"]), "title": str(row["title"]), "content": str(row["content"])} for row in rows]

    def semantic_search_memories(
        self, query_vector: list[float], limit: int = 8, *, include_quarantined: bool = True
    ) -> list[dict[str, Any]]:
        """Cosine similarity over embedded memories (brute force, mirroring
        semantic_search_chunks). Empty when nothing is embedded yet.
        ``include_quarantined`` False drops external-agent memory held out of the
        grounding context (the grounding recall path passes False)."""
        if not query_vector:
            return []
        quarantine_clause = "" if include_quarantined else " WHERE m.grounding_status != 'quarantined'"
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT m.id, m.kind, m.title, m.content, m.source, m.metadata_json,
                       e.model AS embedding_model, e.vector_json
                FROM memory_embeddings e
                JOIN memories m ON m.id = e.memory_id{quarantine_clause}
                """
            ).fetchall()
        scored = []
        for row in rows:
            vector = json.loads(row["vector_json"] or "[]")
            score = cosine_similarity(query_vector, vector)
            scored.append(
                {
                    "score": score,
                    "id": int(row["id"]),
                    "kind": str(row["kind"]),
                    "title": str(row["title"]),
                    "content": str(row["content"]),
                    "source": str(row["source"]),
                    "metadata": json.loads(row["metadata_json"] or "{}"),
                    "embedding_model": row["embedding_model"],
                }
            )
        scored.sort(key=lambda item: item["score"], reverse=True)
        return scored[:limit]

    def get_memory(self, memory_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, created_at, kind, title, content, source, metadata_json FROM memories WHERE id = ?",
                (memory_id,),
            ).fetchone()
        return _memory_row_to_dict(row) if row else None

    def list_memory_rows(self, *, limit: int = 100_000) -> list[dict[str, Any]]:
        """Full memory rows (oldest first) — the source the memory files mirror."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, created_at, kind, title, content, source, metadata_json FROM memories ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
        return [_memory_row_to_dict(row) for row in rows]

    def recent_audit_events(self, limit: int = 25) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) | {"details": json.loads(row["details_json"] or "{}")} for row in rows]

    def record_model_call(
        self,
        *,
        operation: str,
        model: str,
        role: str = "",
        status: str,
        latency_ms: int = 0,
        prompt_chars: int = 0,
        response_chars: int = 0,
        think: bool | None = None,
        error: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO model_calls (
                  created_at, operation, model, role, status, latency_ms, prompt_chars,
                  response_chars, think, error, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    operation,
                    model,
                    role,
                    status,
                    int(latency_ms),
                    int(prompt_chars),
                    int(response_chars),
                    None if think is None else int(bool(think)),
                    error,
                    json.dumps(metadata or {}, sort_keys=True),
                ),
            )
            return int(cur.lastrowid)

    def list_model_calls(self, *, status: str | None = None, limit: int = 50) -> list[ModelCall]:
        with self.connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM model_calls WHERE status = ? ORDER BY id DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM model_calls ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [_model_call_from_row(row) for row in rows]

    def model_call_summary(self, *, limit: int = 250) -> dict[str, Any]:
        calls = self.list_model_calls(limit=limit)
        by_status: dict[str, int] = {}
        by_operation: dict[str, int] = {}
        by_role: dict[str, int] = {}
        latencies = []
        for call in calls:
            by_status[call.status] = by_status.get(call.status, 0) + 1
            by_operation[call.operation] = by_operation.get(call.operation, 0) + 1
            if call.role:
                by_role[call.role] = by_role.get(call.role, 0) + 1
            if call.latency_ms > 0:
                latencies.append(call.latency_ms)
        avg_latency = int(sum(latencies) / len(latencies)) if latencies else 0
        return {
            "window": len(calls),
            "by_status": by_status,
            "by_operation": by_operation,
            "by_role": by_role,
            "avg_latency_ms": avg_latency,
            "latest": _model_call_to_dict(calls[0]) if calls else None,
        }

    def upsert_skill(
        self,
        *,
        name: str,
        description: str,
        body: str,
        source: str,
        source_type: str,
        skill_path: str,
        local_path: str,
        content_hash: str,
        risk_tier: str,
        risk_reasons: list[str],
        default_enabled: bool,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[int, bool]:
        with self.connect() as conn:
            existing = conn.execute("SELECT * FROM skill_registry WHERE name = ?", (name,)).fetchone()
            now = utc_now()
            if existing:
                skill_id = int(existing["id"])
                enabled = int(existing["enabled"])
                conn.execute(
                    """
                    UPDATE skill_registry
                    SET updated_at = ?, description = ?, body = ?, source = ?, source_type = ?,
                        skill_path = ?, local_path = ?, content_hash = ?, risk_tier = ?,
                        risk_reasons_json = ?, default_enabled = ?, last_scanned_at = ?,
                        metadata_json = ?
                    WHERE id = ?
                    """,
                    (
                        now,
                        description,
                        body,
                        source,
                        source_type,
                        skill_path,
                        local_path,
                        content_hash,
                        risk_tier,
                        json.dumps(risk_reasons, sort_keys=True),
                        int(default_enabled),
                        now,
                        json.dumps(metadata or {}, sort_keys=True),
                        skill_id,
                    ),
                )
                created = False
            else:
                enabled = int(default_enabled)
                cur = conn.execute(
                    """
                    INSERT INTO skill_registry (
                      created_at, updated_at, name, description, body, source, source_type,
                      skill_path, local_path, content_hash, risk_tier, risk_reasons_json,
                      enabled, default_enabled, last_scanned_at, metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        now,
                        now,
                        name,
                        description,
                        body,
                        source,
                        source_type,
                        skill_path,
                        local_path,
                        content_hash,
                        risk_tier,
                        json.dumps(risk_reasons, sort_keys=True),
                        enabled,
                        int(default_enabled),
                        now,
                        json.dumps(metadata or {}, sort_keys=True),
                    ),
                )
                skill_id = int(cur.lastrowid)
                created = True
            conn.execute("DELETE FROM skill_fts WHERE rowid = ?", (skill_id,))
            conn.execute(
                "INSERT INTO skill_fts (rowid, name, description, body, source, risk_tier) VALUES (?, ?, ?, ?, ?, ?)",
                (skill_id, name, description, body, source, risk_tier),
            )
            return skill_id, created

    def replace_skill_references(self, skill_id: int, references: list[dict[str, Any]]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM skill_references WHERE skill_id = ?", (skill_id,))
            for ref in references:
                conn.execute(
                    """
                    INSERT INTO skill_references (
                      created_at, skill_id, relative_path, local_path, content_hash,
                      size_bytes, metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        utc_now(),
                        skill_id,
                        str(ref.get("relative_path", "")),
                        str(ref.get("local_path", "")),
                        str(ref.get("content_hash", "")),
                        int(ref.get("size_bytes", 0)),
                        json.dumps(ref.get("metadata", {}) or {}, sort_keys=True),
                    ),
                )

    def get_skill(self, name: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM skill_registry WHERE name = ?", (name,)).fetchone()
            if not row:
                return None
            refs = conn.execute(
                "SELECT * FROM skill_references WHERE skill_id = ? ORDER BY relative_path ASC",
                (int(row["id"]),),
            ).fetchall()
        return _skill_from_row(row) | {"references": [_skill_reference_from_row(ref) for ref in refs]}

    def list_skills(
        self,
        *,
        enabled: bool | None = None,
        risk_tier: str | None = None,
        source: str | None = None,
        limit: int = 250,
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if enabled is not None:
            clauses.append("enabled = ?")
            params.append(int(enabled))
        if risk_tier:
            clauses.append("risk_tier = ?")
            params.append(risk_tier)
        if source:
            clauses.append("source = ?")
            params.append(source)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM skill_registry
                {where}
                ORDER BY enabled DESC, default_enabled DESC, name ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [_skill_from_row(row) for row in rows]

    def search_skills(self, query: str, *, enabled: bool | None = True, limit: int = 5) -> list[dict[str, Any]]:
        query = query.strip()
        if not query:
            return []
        rows: list[sqlite3.Row] = []
        token_query = _token_fts_query(query)
        if token_query:
            clauses = ["skill_fts MATCH ?"]
            params: list[Any] = [token_query]
            if enabled is not None:
                clauses.append("s.enabled = ?")
                params.append(int(enabled))
            params.append(limit)
            try:
                with self.connect() as conn:
                    rows = conn.execute(
                        f"""
                        SELECT s.*, bm25(skill_fts) AS search_rank
                        FROM skill_fts
                        JOIN skill_registry s ON s.id = skill_fts.rowid
                        WHERE {' AND '.join(clauses)}
                        ORDER BY search_rank ASC
                        LIMIT ?
                        """,
                        params,
                    ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        if not rows:
            tokens = _query_tokens(query)
            if not tokens:
                return []
            clauses = []
            params = []
            for token in tokens[:8]:
                clauses.append("(name LIKE ? OR description LIKE ? OR body LIKE ? OR source LIKE ?)")
                params.extend([f"%{token}%", f"%{token}%", f"%{token}%", f"%{token}%"])
            enabled_clause = ""
            if enabled is not None:
                enabled_clause = "AND enabled = ?"
                params.append(int(enabled))
            params.append(limit)
            with self.connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT *, 0.0 AS search_rank
                    FROM skill_registry
                    WHERE ({' OR '.join(clauses)}) {enabled_clause}
                    ORDER BY default_enabled DESC, name ASC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
        return [_skill_from_row(row) | {"search_rank": float(row["search_rank"] or 0.0)} for row in rows]

    def set_skill_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute(
                "UPDATE skill_registry SET updated_at = ?, enabled = ? WHERE name = ?",
                (utc_now(), int(enabled), name),
            )
            row = conn.execute("SELECT * FROM skill_registry WHERE name = ?", (name,)).fetchone()
        if not row:
            raise ValueError(f"Unknown skill: {name}")
        return _skill_from_row(row)

    # --- Action-handler access overlay ---------------------------------------
    # Handlers are registered in code at startup; this table only records
    # explicit founder grant/revoke overrides. An absent row means "enabled"
    # (the historical default), so existing databases keep every handler live.
    def get_handler_access(self, action: str) -> bool | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT enabled FROM action_handler_access WHERE action = ?", (action,)
            ).fetchone()
        return None if row is None else bool(row["enabled"])

    def list_handler_access(self) -> dict[str, bool]:
        with self.connect() as conn:
            rows = conn.execute("SELECT action, enabled FROM action_handler_access").fetchall()
        return {row["action"]: bool(row["enabled"]) for row in rows}

    def set_handler_access(self, action: str, enabled: bool) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO action_handler_access (created_at, updated_at, action, enabled)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(action) DO UPDATE SET
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (now, now, action, int(enabled)),
            )

    def record_skill_invocation(
        self,
        *,
        skill_id: int,
        name: str,
        query: str,
        score: float,
        task_type: str = "",
        runtime_event_id: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO skill_invocations (
                  created_at, skill_id, name, query, score, task_type, runtime_event_id, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    skill_id,
                    name,
                    query,
                    float(score),
                    task_type,
                    runtime_event_id,
                    json.dumps(metadata or {}, sort_keys=True),
                ),
            )
            return int(cur.lastrowid)

    def recent_skill_invocations(self, *, limit: int = 25) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM skill_invocations ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(str(item.pop("metadata_json", "{}")) or "{}")
            items.append(item)
        return items

    def skill_summary(self) -> dict[str, Any]:
        with self.connect() as conn:
            total = conn.execute("SELECT COUNT(*) AS count FROM skill_registry").fetchone()
            enabled = conn.execute("SELECT COUNT(*) AS count FROM skill_registry WHERE enabled = 1").fetchone()
            by_risk = conn.execute(
                "SELECT risk_tier AS key, COUNT(*) AS count FROM skill_registry GROUP BY risk_tier ORDER BY count DESC"
            ).fetchall()
            by_source = conn.execute(
                "SELECT source AS key, COUNT(*) AS count FROM skill_registry GROUP BY source ORDER BY count DESC, source ASC"
            ).fetchall()
            recently_used = conn.execute(
                """
                SELECT name, COUNT(*) AS count, MAX(created_at) AS last_used_at
                FROM skill_invocations
                GROUP BY name
                ORDER BY last_used_at DESC
                LIMIT 10
                """
            ).fetchall()
        return {
            "total": int(total["count"] or 0) if total else 0,
            "enabled": int(enabled["count"] or 0) if enabled else 0,
            "by_risk_tier": {str(row["key"] or "unknown"): int(row["count"]) for row in by_risk},
            "by_source": {str(row["key"] or "unknown"): int(row["count"]) for row in by_source},
            "recently_used": [dict(row) for row in recently_used],
        }

    def daily_brief_inputs(self) -> dict[str, Any]:
        with self.connect() as conn:
            return {
                "recent_memories": [dict(row) for row in conn.execute("SELECT * FROM memories ORDER BY id DESC LIMIT 8")],
                "active_goals": [dict(row) for row in conn.execute("SELECT * FROM goals WHERE status = 'active' ORDER BY id DESC LIMIT 8")],
                "open_decisions": [dict(row) for row in conn.execute("SELECT * FROM decisions WHERE status = 'open' ORDER BY id DESC LIMIT 8")],
                "recent_disagreements": [dict(row) for row in conn.execute("SELECT * FROM disagreements ORDER BY id DESC LIMIT 5")],
                "approval_pressure": self.approval_pressure(limit=3),
            }

    def approval_pressure(self, *, limit: int = 3) -> dict[str, Any]:
        with self.connect() as conn:
            counts = conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM approval_requests
                WHERE status IN ('pending', 'deferred')
                GROUP BY status
                """
            ).fetchall()
            rows = conn.execute(
                """
                SELECT
                  ar.*,
                  wi.priority AS work_priority,
                  wi.kind AS work_kind,
                  wi.source AS work_source,
                  wi.due_at AS work_due_at,
                  wi.metadata_json AS work_metadata_json
                FROM approval_requests ar
                LEFT JOIN work_items wi
                  ON ar.source_type = 'work_item' AND ar.source_id = wi.id
                WHERE ar.status IN ('pending', 'deferred')
                ORDER BY
                  CASE ar.status WHEN 'pending' THEN 0 ELSE 1 END,
                  COALESCE(wi.priority, 50) DESC,
                  ar.id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        by_status = {str(row["status"]): int(row["count"]) for row in counts}
        items = [_approval_pressure_from_row(row) for row in rows]
        top = items[0] if items else {}
        pending = by_status.get("pending", 0)
        deferred = by_status.get("deferred", 0)
        if pending:
            headline = f"{pending} approval request(s) waiting on you."
        elif deferred:
            headline = f"{deferred} deferred approval request(s) still on the board."
        else:
            headline = "No approval blockers."
        return {
            "pending": pending,
            "deferred": deferred,
            "total": pending + deferred,
            "items": items,
            "top": top,
            "headline": headline,
            "next_action": f"Review approval #{top['id']} in /ui/approvals.html: {top['title']}" if top else "No approval blockers.",
            "console_url": "/ui/approvals.html",
            "has_blockers": bool(items),
        }

    def create_conversation(self, *, title: str = "", metadata: dict[str, Any] | None = None) -> int:
        now = utc_now()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO conversations (created_at, updated_at, title, status, metadata_json)
                VALUES (?, ?, ?, 'active', ?)
                """,
                (now, now, title, json.dumps(metadata or {}, sort_keys=True)),
            )
            return int(cur.lastrowid)

    def get_conversation(self, conversation_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        return _conversation_from_row(row) if row else None

    def list_conversations(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM conversations WHERE status = ? ORDER BY COALESCE(last_message_at, updated_at) DESC, id DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM conversations ORDER BY COALESCE(last_message_at, updated_at) DESC, id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [_conversation_from_row(row) for row in rows]

    def add_conversation_turn(
        self,
        *,
        conversation_id: int,
        role: str,
        content: str,
        task_type: str = "",
        model: str = "",
        authority_decision: str = "",
        runtime_event_id: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        now = utc_now()
        with self.connect() as conn:
            exists = conn.execute("SELECT title FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
            if not exists:
                raise ValueError(f"Conversation not found: {conversation_id}")
            cur = conn.execute(
                """
                INSERT INTO conversation_turns (
                  created_at, conversation_id, role, content, task_type, model,
                  authority_decision, runtime_event_id, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    conversation_id,
                    role,
                    content,
                    task_type,
                    model,
                    authority_decision,
                    runtime_event_id,
                    json.dumps(metadata or {}, sort_keys=True),
                ),
            )
            turn_id = int(cur.lastrowid)
            derived_title = ""
            if not str(exists["title"]).strip() and role == "user":
                derived_title = _conversation_title_from_message(content)
            conn.execute(
                """
                UPDATE conversations
                SET updated_at = ?,
                    last_message_at = ?,
                    turn_count = turn_count + 1,
                    title = CASE WHEN title = '' AND ? != '' THEN ? ELSE title END
                WHERE id = ?
                """,
                (now, now, derived_title, derived_title, conversation_id),
            )
            return turn_id

    def list_conversation_turns(
        self,
        conversation_id: int,
        *,
        limit: int = 50,
        newest_first: bool = False,
    ) -> list[dict[str, Any]]:
        order = "DESC" if newest_first else "ASC"
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM conversation_turns WHERE conversation_id = ? ORDER BY id {order} LIMIT ?",
                (conversation_id, limit),
            ).fetchall()
        return [_conversation_turn_from_row(row) for row in rows]

    def recent_conversation_turns(self, conversation_id: int, *, window: int = 12) -> list[dict[str, Any]]:
        turns = self.list_conversation_turns(conversation_id, limit=window, newest_first=True)
        return list(reversed(turns))

    def count_conversation_turns_after(self, conversation_id: int, *, after_turn_id: int | None) -> int:
        with self.connect() as conn:
            if after_turn_id is None:
                row = conn.execute(
                    "SELECT COUNT(*) AS count FROM conversation_turns WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS count FROM conversation_turns WHERE conversation_id = ? AND id > ?",
                    (conversation_id, after_turn_id),
                ).fetchone()
        return int(row["count"]) if row else 0

    def update_conversation_summary(
        self,
        conversation_id: int,
        *,
        summary: str,
        summary_through_turn_id: int,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE conversations SET updated_at = ?, summary = ?, summary_through_turn_id = ? WHERE id = ?",
                (utc_now(), summary, summary_through_turn_id, conversation_id),
            )

    def update_conversation_status(self, conversation_id: int, *, status: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE conversations SET updated_at = ?, status = ? WHERE id = ?",
                (utc_now(), status, conversation_id),
            )

    def update_conversation_distilled(
        self,
        conversation_id: int,
        *,
        distilled_through_turn_id: int,
    ) -> None:
        """Advance the distillation cursor after promoting turns into memory."""
        with self.connect() as conn:
            conn.execute(
                "UPDATE conversations SET updated_at = ?, distilled_through_turn_id = ? WHERE id = ?",
                (utc_now(), distilled_through_turn_id, conversation_id),
            )

    def list_memories_by_source(self, source: str, *, limit: int = 200) -> list[MemoryRecord]:
        """Memories written under a given source (e.g. 'conversation:42'), newest
        first. Used to dedup distilled items against what's already been promoted."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM memories WHERE source = ? ORDER BY id DESC LIMIT ?",
                (source, limit),
            ).fetchall()
        return [_memory_from_row(row) for row in rows]


def _memory_from_row(row: sqlite3.Row) -> MemoryRecord:
    return MemoryRecord(
        id=int(row["id"]),
        created_at=str(row["created_at"]),
        kind=str(row["kind"]),
        title=str(row["title"]),
        content=str(row["content"]),
        source=str(row["source"]),
        metadata=json.loads(row["metadata_json"] or "{}"),
    )


def _memory_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "created_at": str(row["created_at"]),
        "kind": str(row["kind"]),
        "title": str(row["title"]),
        "content": str(row["content"]),
        "source": str(row["source"]),
        "metadata": json.loads(row["metadata_json"] or "{}"),
    }


def _work_item_from_row(row: sqlite3.Row) -> WorkItem:
    return WorkItem(
        id=int(row["id"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        kind=str(row["kind"]),
        title=str(row["title"]),
        detail=str(row["detail"]),
        action=str(row["action"]),
        target=str(row["target"]),
        permission_tier=str(row["permission_tier"]),
        authority_decision=str(row["authority_decision"]),
        status=str(row["status"]),
        priority=int(row["priority"]),
        source=str(row["source"]),
        due_at=str(row["due_at"]) if row["due_at"] else None,
        last_error=str(row["last_error"]),
        result=json.loads(row["result_json"] or "{}"),
        metadata=json.loads(row["metadata_json"] or "{}"),
        unique_key=str(row["unique_key"]) if row["unique_key"] else None,
    )


def _approval_request_from_row(row: sqlite3.Row) -> ApprovalRequest:
    return ApprovalRequest(
        id=int(row["id"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        source_type=str(row["source_type"]),
        source_id=int(row["source_id"]) if row["source_id"] is not None else None,
        title=str(row["title"]),
        detail=str(row["detail"]),
        action=str(row["action"]),
        target=str(row["target"]),
        permission_tier=str(row["permission_tier"]),
        authority_decision=str(row["authority_decision"]),
        authority=json.loads(row["authority_json"] or "{}"),
        status=str(row["status"]),
        requested_by=str(row["requested_by"]),
        resolved_by=str(row["resolved_by"]),
        resolved_at=str(row["resolved_at"]) if row["resolved_at"] else None,
        resolution_note=str(row["resolution_note"]),
        metadata=json.loads(row["metadata_json"] or "{}"),
    )


def _approval_training_event_from_row(row: sqlite3.Row) -> ApprovalTrainingEvent:
    return ApprovalTrainingEvent(
        id=int(row["id"]),
        created_at=str(row["created_at"]),
        approval_request_id=int(row["approval_request_id"]) if row["approval_request_id"] is not None else None,
        work_item_id=int(row["work_item_id"]) if row["work_item_id"] is not None else None,
        event_type=str(row["event_type"]),
        outcome=str(row["outcome"]),
        actor=str(row["actor"]),
        note=str(row["note"]),
        action=str(row["action"]),
        target=str(row["target"]),
        permission_tier=str(row["permission_tier"]),
        authority_decision=str(row["authority_decision"]),
        authority=json.loads(row["authority_json"] or "{}"),
        request_snapshot=json.loads(row["request_snapshot_json"] or "{}"),
        work_item_snapshot=json.loads(row["work_item_snapshot_json"] or "{}"),
        edits=json.loads(row["edits_json"] or "{}"),
        metadata=json.loads(row["metadata_json"] or "{}"),
    )


def _approval_pressure_from_row(row: sqlite3.Row) -> dict[str, Any]:
    request_metadata = json.loads(row["metadata_json"] or "{}")
    work_metadata = json.loads(row["work_metadata_json"] or "{}") if "work_metadata_json" in row.keys() and row["work_metadata_json"] else {}
    authority = json.loads(row["authority_json"] or "{}")
    merged_metadata = {**work_metadata, **request_metadata}
    evidence = _metadata_list(merged_metadata, "evidence", "evidence_items", "required_evidence")
    risks = _metadata_list(merged_metadata, "risk", "risks", "downside_risk", "downside_risks")
    if authority.get("reason"):
        risks.append({"authority_reason": authority["reason"]})
    action = str(row["action"])
    target = str(row["target"] or "")
    return {
        "id": int(row["id"]),
        "status": str(row["status"]),
        "title": str(row["title"]),
        "detail": str(row["detail"]),
        "action": action,
        "target": target,
        "zade_wants": f"Zade wants to {action} -> {target}" if target else f"Zade wants to {action}",
        "permission_tier": str(row["permission_tier"]),
        "authority_decision": str(row["authority_decision"]),
        "authority_reason": str(authority.get("reason", "")),
        "matched_rule": str(authority.get("matched_rule", "")),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "source_type": str(row["source_type"]),
        "source_id": int(row["source_id"]) if row["source_id"] is not None else None,
        "priority": int(row["work_priority"] if row["work_priority"] is not None else 50),
        "due_at": str(row["work_due_at"]) if "work_due_at" in row.keys() and row["work_due_at"] else None,
        "evidence": evidence,
        "risks": risks,
    }


def _metadata_list(metadata: dict[str, Any], *keys: str) -> list[Any]:
    values: list[Any] = []
    for key in keys:
        raw = metadata.get(key)
        if isinstance(raw, list):
            values.extend(raw)
        elif raw:
            values.append(raw)
    return values


def _model_call_from_row(row: sqlite3.Row) -> ModelCall:
    think = row["think"]
    return ModelCall(
        id=int(row["id"]),
        created_at=str(row["created_at"]),
        operation=str(row["operation"]),
        model=str(row["model"]),
        role=str(row["role"]),
        status=str(row["status"]),
        latency_ms=int(row["latency_ms"]),
        prompt_chars=int(row["prompt_chars"]),
        response_chars=int(row["response_chars"]),
        think=None if think is None else bool(think),
        error=str(row["error"]),
        metadata=json.loads(row["metadata_json"] or "{}"),
    )


def _model_call_to_dict(call: ModelCall) -> dict[str, Any]:
    return {
        "id": call.id,
        "created_at": call.created_at,
        "operation": call.operation,
        "model": call.model,
        "role": call.role,
        "status": call.status,
        "latency_ms": call.latency_ms,
        "prompt_chars": call.prompt_chars,
        "response_chars": call.response_chars,
        "think": call.think,
        "error": call.error,
        "metadata": call.metadata,
    }


def _skill_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "name": str(row["name"]),
        "description": str(row["description"]),
        "body": str(row["body"]),
        "source": str(row["source"]),
        "source_type": str(row["source_type"]),
        "skill_path": str(row["skill_path"]),
        "local_path": str(row["local_path"]),
        "content_hash": str(row["content_hash"]),
        "risk_tier": str(row["risk_tier"]),
        "risk_reasons": json.loads(row["risk_reasons_json"] or "[]"),
        "enabled": bool(row["enabled"]),
        "default_enabled": bool(row["default_enabled"]),
        "last_scanned_at": str(row["last_scanned_at"]) if row["last_scanned_at"] else None,
        "last_used_at": str(row["last_used_at"]) if "last_used_at" in row.keys() and row["last_used_at"] else None,
        "metadata": json.loads(row["metadata_json"] or "{}"),
    }


def _skill_reference_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "created_at": str(row["created_at"]),
        "skill_id": int(row["skill_id"]),
        "relative_path": str(row["relative_path"]),
        "local_path": str(row["local_path"]),
        "content_hash": str(row["content_hash"]),
        "size_bytes": int(row["size_bytes"]),
        "metadata": json.loads(row["metadata_json"] or "{}"),
    }


def _conversation_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "title": str(row["title"]),
        "status": str(row["status"]),
        "summary": str(row["summary"]),
        "summary_through_turn_id": int(row["summary_through_turn_id"]) if row["summary_through_turn_id"] is not None else None,
        "distilled_through_turn_id": (
            int(row["distilled_through_turn_id"])
            if "distilled_through_turn_id" in row.keys() and row["distilled_through_turn_id"] is not None
            else None
        ),
        "turn_count": int(row["turn_count"]),
        "last_message_at": str(row["last_message_at"]) if row["last_message_at"] else None,
        "metadata": json.loads(row["metadata_json"] or "{}"),
    }


def _conversation_turn_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "created_at": str(row["created_at"]),
        "conversation_id": int(row["conversation_id"]),
        "role": str(row["role"]),
        "content": str(row["content"]),
        "task_type": str(row["task_type"]),
        "model": str(row["model"]),
        "authority_decision": str(row["authority_decision"]),
        "runtime_event_id": int(row["runtime_event_id"]) if row["runtime_event_id"] is not None else None,
        "metadata": json.loads(row["metadata_json"] or "{}"),
    }


def _conversation_title_from_message(message: str) -> str:
    cleaned = re.sub(r"\s+", " ", message).strip()
    if len(cleaned) <= 60:
        return cleaned
    return cleaned[:57].rstrip() + "..."


def _query_tokens(query: str) -> list[str]:
    stopwords = {
        "about",
        "answer",
        "based",
        "first",
        "from",
        "local",
        "memory",
        "sentence",
        "should",
        "what",
        "with",
        "your",
    }
    tokens = [token.lower() for token in re.findall(r"[A-Za-z0-9_]+", query)]
    return [token for token in tokens if len(token) >= 3 and token not in stopwords]


def _token_fts_query(query: str) -> str:
    tokens = _query_tokens(query)
    return " OR ".join(f'"{token}"' for token in tokens[:8])


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    pairs = list(zip(a, b, strict=False))
    dot = sum(left * right for left, right in pairs)
    norm_a = sum(left * left for left, _ in pairs) ** 0.5
    norm_b = sum(right * right for _, right in pairs) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT NOT NULL,
  permission_tier TEXT NOT NULL,
  status TEXT NOT NULL,
  details_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  content TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'local',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  grounding_status TEXT NOT NULL DEFAULT 'active'
);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
  title,
  content,
  kind,
  source,
  content='memories',
  content_rowid='id'
);

CREATE TABLE IF NOT EXISTS entities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  name TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  aliases_json TEXT NOT NULL DEFAULT '[]',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(name, entity_type)
);

CREATE TABLE IF NOT EXISTS relationships (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  subject_entity_id INTEGER NOT NULL REFERENCES entities(id),
  predicate TEXT NOT NULL,
  object_entity_id INTEGER NOT NULL REFERENCES entities(id),
  evidence_memory_id INTEGER REFERENCES memories(id),
  confidence REAL NOT NULL DEFAULT 0.5,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS goals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  review_after TEXT,
  notes TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  decision TEXT NOT NULL DEFAULT '',
  revisit_after TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS predictions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  statement TEXT NOT NULL,
  due_at TEXT,
  probability REAL,
  outcome TEXT,
  scored_at TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS disagreements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  topic TEXT NOT NULL,
  position TEXT NOT NULL,
  rationale TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS tool_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  permission_tier TEXT NOT NULL,
  args_json TEXT NOT NULL DEFAULT '{}',
  result_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  title TEXT NOT NULL,
  source_uri TEXT NOT NULL,
  content_hash TEXT NOT NULL UNIQUE,
  media_type TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS document_chunks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  chunk_index INTEGER NOT NULL,
  text TEXT NOT NULL,
  char_start INTEGER NOT NULL,
  char_end INTEGER NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(document_id, chunk_index)
);

CREATE VIRTUAL TABLE IF NOT EXISTS document_chunk_fts USING fts5(
  text,
  content='document_chunks',
  content_rowid='id'
);

CREATE TABLE IF NOT EXISTS chunk_embeddings (
  chunk_id INTEGER NOT NULL REFERENCES document_chunks(id) ON DELETE CASCADE,
  model TEXT NOT NULL,
  dimensions INTEGER NOT NULL,
  content_hash TEXT NOT NULL DEFAULT '',
  vector_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(chunk_id, model)
);

-- Derived, rebuildable semantic index over the memory store (Tier 4). One row per
-- (memory, embedding model); content_hash lets a rebuild skip unchanged memories.
-- Cascades on memory delete. Never the source of truth — rebuildable from memories.
CREATE TABLE IF NOT EXISTS memory_embeddings (
  memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  model TEXT NOT NULL,
  dimensions INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  vector_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(memory_id, model)
);

CREATE TABLE IF NOT EXISTS skill_registry (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  name TEXT NOT NULL UNIQUE,
  description TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  source_type TEXT NOT NULL DEFAULT '',
  skill_path TEXT NOT NULL DEFAULT '',
  local_path TEXT NOT NULL DEFAULT '',
  content_hash TEXT NOT NULL DEFAULT '',
  risk_tier TEXT NOT NULL DEFAULT 'read_only',
  risk_reasons_json TEXT NOT NULL DEFAULT '[]',
  enabled INTEGER NOT NULL DEFAULT 0,
  default_enabled INTEGER NOT NULL DEFAULT 0,
  last_scanned_at TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS skill_references (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  skill_id INTEGER NOT NULL REFERENCES skill_registry(id) ON DELETE CASCADE,
  relative_path TEXT NOT NULL,
  local_path TEXT NOT NULL DEFAULT '',
  content_hash TEXT NOT NULL DEFAULT '',
  size_bytes INTEGER NOT NULL DEFAULT 0,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(skill_id, relative_path)
);

CREATE TABLE IF NOT EXISTS skill_invocations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  skill_id INTEGER NOT NULL REFERENCES skill_registry(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  query TEXT NOT NULL DEFAULT '',
  score REAL NOT NULL DEFAULT 0,
  task_type TEXT NOT NULL DEFAULT '',
  runtime_event_id INTEGER,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE VIRTUAL TABLE IF NOT EXISTS skill_fts USING fts5(
  name,
  description,
  body,
  source,
  risk_tier
);

CREATE TABLE IF NOT EXISTS ingestion_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  job_type TEXT NOT NULL,
  source TEXT NOT NULL,
  status TEXT NOT NULL,
  documents_count INTEGER NOT NULL DEFAULT 0,
  chunks_count INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS work_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL,
  target TEXT NOT NULL DEFAULT '',
  permission_tier TEXT NOT NULL,
  authority_decision TEXT NOT NULL DEFAULT 'unknown',
  status TEXT NOT NULL DEFAULT 'pending',
  priority INTEGER NOT NULL DEFAULT 50,
  source TEXT NOT NULL DEFAULT 'local',
  due_at TEXT,
  last_error TEXT NOT NULL DEFAULT '',
  result_json TEXT NOT NULL DEFAULT '{}',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  unique_key TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS approval_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  source_type TEXT NOT NULL,
  source_id INTEGER,
  title TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL,
  target TEXT NOT NULL DEFAULT '',
  permission_tier TEXT NOT NULL,
  authority_decision TEXT NOT NULL,
  authority_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending',
  requested_by TEXT NOT NULL DEFAULT 'system',
  resolved_by TEXT NOT NULL DEFAULT '',
  resolved_at TEXT,
  resolution_note TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS approval_training_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  approval_request_id INTEGER,
  work_item_id INTEGER,
  event_type TEXT NOT NULL,
  outcome TEXT NOT NULL,
  actor TEXT NOT NULL DEFAULT 'founder',
  note TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL DEFAULT '',
  target TEXT NOT NULL DEFAULT '',
  permission_tier TEXT NOT NULL DEFAULT '',
  authority_decision TEXT NOT NULL DEFAULT '',
  authority_json TEXT NOT NULL DEFAULT '{}',
  request_snapshot_json TEXT NOT NULL DEFAULT '{}',
  work_item_snapshot_json TEXT NOT NULL DEFAULT '{}',
  edits_json TEXT NOT NULL DEFAULT '{}',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS runtime_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  event_type TEXT NOT NULL,
  status TEXT NOT NULL,
  message TEXT NOT NULL DEFAULT '',
  response TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  authority_decision TEXT NOT NULL DEFAULT '',
  details_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS model_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  operation TEXT NOT NULL,
  model TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  latency_ms INTEGER NOT NULL DEFAULT 0,
  prompt_chars INTEGER NOT NULL DEFAULT 0,
  response_chars INTEGER NOT NULL DEFAULT 0,
  think INTEGER,
  error TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS teaching_candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  source_system TEXT NOT NULL,
  source_uri TEXT NOT NULL,
  title TEXT NOT NULL,
  content_hash TEXT NOT NULL UNIQUE,
  excerpt TEXT NOT NULL DEFAULT '',
  candidate_type TEXT NOT NULL DEFAULT 'document',
  reliability TEXT NOT NULL DEFAULT 'C',
  status TEXT NOT NULL DEFAULT 'candidate',
  suggested_links_json TEXT NOT NULL DEFAULT '[]',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS identity_charter (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  name TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'local',
  mission TEXT NOT NULL DEFAULT '',
  guiding_principles_json TEXT NOT NULL DEFAULT '[]',
  cognitive_style_json TEXT NOT NULL DEFAULT '[]',
  communication_style_json TEXT NOT NULL DEFAULT '[]',
  leadership_philosophy_json TEXT NOT NULL DEFAULT '[]',
  emotional_framework_json TEXT NOT NULL DEFAULT '{}',
  strengths_json TEXT NOT NULL DEFAULT '[]',
  risk_controls_json TEXT NOT NULL DEFAULT '[]',
  decision_framework_json TEXT NOT NULL DEFAULT '[]',
  personal_standards_json TEXT NOT NULL DEFAULT '[]',
  safety_translation_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'active',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS relationship_charters (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  subject_name TEXT NOT NULL,
  relationship_type TEXT NOT NULL DEFAULT 'protected_principal',
  source TEXT NOT NULL DEFAULT 'local',
  first_principle TEXT NOT NULL DEFAULT '',
  devotion_json TEXT NOT NULL DEFAULT '{}',
  attention_policy_json TEXT NOT NULL DEFAULT '{}',
  protection_policy_json TEXT NOT NULL DEFAULT '{}',
  loyalty_policy_json TEXT NOT NULL DEFAULT '{}',
  vulnerability_json TEXT NOT NULL DEFAULT '{}',
  trust_json TEXT NOT NULL DEFAULT '{}',
  internal_conflict_json TEXT NOT NULL DEFAULT '{}',
  expression_of_care_json TEXT NOT NULL DEFAULT '{}',
  risk_controls_json TEXT NOT NULL DEFAULT '[]',
  safety_translation_json TEXT NOT NULL DEFAULT '{}',
  boundaries_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'active',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(subject_name, relationship_type)
);

CREATE TABLE IF NOT EXISTS voice_charter (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  name TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'local',
  overall_voice TEXT NOT NULL DEFAULT '',
  sentence_structure_json TEXT NOT NULL DEFAULT '{}',
  vocabulary_json TEXT NOT NULL DEFAULT '{}',
  rhythm_json TEXT NOT NULL DEFAULT '{}',
  confidence_style_json TEXT NOT NULL DEFAULT '{}',
  humor_json TEXT NOT NULL DEFAULT '{}',
  nicknames_json TEXT NOT NULL DEFAULT '{}',
  emotional_expression_json TEXT NOT NULL DEFAULT '{}',
  threat_translation_json TEXT NOT NULL DEFAULT '{}',
  question_style_json TEXT NOT NULL DEFAULT '{}',
  philosophy_json TEXT NOT NULL DEFAULT '{}',
  internal_monologue_json TEXT NOT NULL DEFAULT '{}',
  dominant_traits_json TEXT NOT NULL DEFAULT '[]',
  linguistic_fingerprint_json TEXT NOT NULL DEFAULT '{}',
  uncertainty_policy_json TEXT NOT NULL DEFAULT '{}',
  safety_controls_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'active',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS company_thesis (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  vision TEXT NOT NULL DEFAULT '',
  mission TEXT NOT NULL DEFAULT '',
  why_now TEXT NOT NULL DEFAULT '',
  customer TEXT NOT NULL DEFAULT '',
  unfair_advantages_json TEXT NOT NULL DEFAULT '[]',
  core_assumptions_json TEXT NOT NULL DEFAULT '[]',
  strategic_moats_json TEXT NOT NULL DEFAULT '{}',
  success_metrics_json TEXT NOT NULL DEFAULT '{}',
  failure_modes_json TEXT NOT NULL DEFAULT '{}',
  unknown_unknowns_json TEXT NOT NULL DEFAULT '[]',
  evidence_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'draft'
);

CREATE TABLE IF NOT EXISTS strategy_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  title TEXT NOT NULL,
  category TEXT NOT NULL,
  decision TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  expected_outcome TEXT NOT NULL DEFAULT '',
  confidence INTEGER NOT NULL DEFAULT 50,
  time_horizon TEXT NOT NULL DEFAULT '',
  dependencies_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'active',
  evidence_json TEXT NOT NULL DEFAULT '[]',
  linked_metrics_json TEXT NOT NULL DEFAULT '[]',
  owner TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS founder_initiatives (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  objective TEXT NOT NULL,
  why_it_matters TEXT NOT NULL DEFAULT '',
  expected_business_impact TEXT NOT NULL DEFAULT '',
  priority INTEGER NOT NULL DEFAULT 50,
  owner TEXT NOT NULL DEFAULT '',
  due_date TEXT,
  current_stage TEXT NOT NULL DEFAULT 'proposed',
  dependencies_json TEXT NOT NULL DEFAULT '[]',
  blockers_json TEXT NOT NULL DEFAULT '[]',
  success_criteria_json TEXT NOT NULL DEFAULT '[]',
  evidence_json TEXT NOT NULL DEFAULT '[]',
  confidence INTEGER NOT NULL DEFAULT 50,
  current_risk TEXT NOT NULL DEFAULT 'medium',
  next_review TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS decision_memos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  problem TEXT NOT NULL,
  context TEXT NOT NULL DEFAULT '',
  options_json TEXT NOT NULL DEFAULT '[]',
  recommendation TEXT NOT NULL DEFAULT '',
  why TEXT NOT NULL DEFAULT '',
  confidence INTEGER NOT NULL DEFAULT 50,
  expected_outcome TEXT NOT NULL DEFAULT '',
  expected_failure_modes_json TEXT NOT NULL DEFAULT '[]',
  who_disagrees TEXT NOT NULL DEFAULT '',
  counterarguments_json TEXT NOT NULL DEFAULT '[]',
  decision_date TEXT,
  revisit_date TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS founder_predictions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  prediction TEXT NOT NULL,
  probability REAL NOT NULL,
  time_horizon TEXT NOT NULL DEFAULT '',
  due_at TEXT,
  evidence_json TEXT NOT NULL DEFAULT '[]',
  outcome TEXT NOT NULL DEFAULT '',
  result TEXT NOT NULL DEFAULT 'open',
  calibration_error REAL,
  missed_factors TEXT NOT NULL DEFAULT '',
  lessons TEXT NOT NULL DEFAULT '',
  worldview_update TEXT NOT NULL DEFAULT '',
  scored_at TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS contrarian_reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  subject_type TEXT NOT NULL,
  subject_id INTEGER,
  title TEXT NOT NULL,
  context TEXT NOT NULL DEFAULT '',
  roles_json TEXT NOT NULL DEFAULT '{}',
  top_risks_json TEXT NOT NULL DEFAULT '[]',
  blind_spots_json TEXT NOT NULL DEFAULT '[]',
  confidence_adjustment INTEGER NOT NULL DEFAULT 0,
  recommendation TEXT NOT NULL DEFAULT 'proceed_with_changes',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS founder_reflections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  event TEXT NOT NULL,
  expected TEXT NOT NULL DEFAULT '',
  changed TEXT NOT NULL DEFAULT '',
  belief_update TEXT NOT NULL DEFAULT '',
  strategy_update TEXT NOT NULL DEFAULT '',
  prediction_update TEXT NOT NULL DEFAULT '',
  priority_update TEXT NOT NULL DEFAULT '',
  never_again TEXT NOT NULL DEFAULT '',
  more_often TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS founder_assumptions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  statement TEXT NOT NULL,
  category TEXT NOT NULL,
  confidence INTEGER NOT NULL DEFAULT 50,
  status TEXT NOT NULL DEFAULT 'active',
  review_date TEXT,
  invalidation_signal TEXT NOT NULL DEFAULT '',
  evidence_ids_json TEXT NOT NULL DEFAULT '[]',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS founder_evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  evidence_type TEXT NOT NULL,
  source TEXT NOT NULL,
  evidence_date TEXT,
  reliability TEXT NOT NULL DEFAULT 'D',
  claim_supported TEXT NOT NULL DEFAULT '',
  claim_contradicted TEXT NOT NULL DEFAULT '',
  strength INTEGER NOT NULL DEFAULT 50,
  linked_assumption_id INTEGER,
  linked_decision_id INTEGER,
  notes TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS founder_links (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  from_type TEXT NOT NULL,
  from_id INTEGER NOT NULL,
  relation TEXT NOT NULL,
  to_type TEXT NOT NULL,
  to_id INTEGER NOT NULL,
  strength INTEGER NOT NULL DEFAULT 50,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS strategy_objects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  object_type TEXT NOT NULL,
  title TEXT NOT NULL,
  owner TEXT NOT NULL DEFAULT '',
  deadline TEXT,
  confidence INTEGER NOT NULL DEFAULT 50,
  status TEXT NOT NULL DEFAULT 'active',
  reversal_trigger TEXT NOT NULL DEFAULT '',
  details_json TEXT NOT NULL DEFAULT '{}',
  evidence_ids_json TEXT NOT NULL DEFAULT '[]',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS founder_goals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  name TEXT NOT NULL,
  why_it_matters TEXT NOT NULL DEFAULT '',
  metric TEXT NOT NULL DEFAULT '',
  target TEXT NOT NULL DEFAULT '',
  deadline TEXT,
  owner TEXT NOT NULL DEFAULT '',
  confidence INTEGER NOT NULL DEFAULT 50,
  status TEXT NOT NULL DEFAULT 'active',
  evidence_ids_json TEXT NOT NULL DEFAULT '[]',
  blockers_json TEXT NOT NULL DEFAULT '[]',
  related_assumption_ids_json TEXT NOT NULL DEFAULT '[]',
  related_decision_ids_json TEXT NOT NULL DEFAULT '[]',
  related_bet_ids_json TEXT NOT NULL DEFAULT '[]',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS active_objectives (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  objective TEXT NOT NULL,
  why_it_matters TEXT NOT NULL DEFAULT '',
  desired_outcome TEXT NOT NULL DEFAULT '',
  metric TEXT NOT NULL DEFAULT '',
  target TEXT NOT NULL DEFAULT '',
  deadline TEXT,
  owner TEXT NOT NULL DEFAULT '',
  priority INTEGER NOT NULL DEFAULT 80,
  confidence INTEGER NOT NULL DEFAULT 50,
  status TEXT NOT NULL DEFAULT 'active',
  is_current INTEGER NOT NULL DEFAULT 0,
  linked_goal_ids_json TEXT NOT NULL DEFAULT '[]',
  linked_bet_ids_json TEXT NOT NULL DEFAULT '[]',
  linked_assumption_ids_json TEXT NOT NULL DEFAULT '[]',
  linked_experiment_ids_json TEXT NOT NULL DEFAULT '[]',
  linked_decision_ids_json TEXT NOT NULL DEFAULT '[]',
  evidence_ids_json TEXT NOT NULL DEFAULT '[]',
  constraints_json TEXT NOT NULL DEFAULT '[]',
  risks_json TEXT NOT NULL DEFAULT '[]',
  current_bet TEXT NOT NULL DEFAULT '',
  next_action TEXT NOT NULL DEFAULT '',
  review_cadence TEXT NOT NULL DEFAULT 'daily',
  last_reviewed_at TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS decision_recommendations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  objective_id INTEGER,
  problem TEXT NOT NULL,
  context TEXT NOT NULL DEFAULT '',
  options_json TEXT NOT NULL DEFAULT '[]',
  recommendation TEXT NOT NULL,
  rationale TEXT NOT NULL DEFAULT '',
  confidence INTEGER NOT NULL DEFAULT 50,
  required_evidence_json TEXT NOT NULL DEFAULT '[]',
  downside_risk_json TEXT NOT NULL DEFAULT '[]',
  kill_or_reversal_condition TEXT NOT NULL DEFAULT '',
  next_action TEXT NOT NULL DEFAULT '',
  decision_memo_id INTEGER,
  next_task_id INTEGER,
  status TEXT NOT NULL DEFAULT 'proposed',
  authority_note TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS founder_tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  title TEXT NOT NULL,
  initiative_id INTEGER,
  goal_id INTEGER,
  owner TEXT NOT NULL DEFAULT '',
  due_date TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  strategic_value TEXT NOT NULL DEFAULT '',
  evidence_needed TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS kill_criteria (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  subject_type TEXT NOT NULL,
  subject_id INTEGER NOT NULL,
  metric TEXT NOT NULL DEFAULT '',
  threshold TEXT NOT NULL DEFAULT '',
  by_date TEXT,
  effort_limit TEXT NOT NULL DEFAULT '',
  exception TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS founder_overrides (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  zade_recommendation TEXT NOT NULL,
  founder_decision TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  risk_accepted TEXT NOT NULL DEFAULT '',
  review_date TEXT,
  subject_type TEXT NOT NULL DEFAULT '',
  subject_id INTEGER,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS confidence_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  subject_type TEXT NOT NULL,
  subject_id INTEGER NOT NULL,
  previous_confidence INTEGER,
  new_confidence INTEGER NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  evidence_id INTEGER,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS thesis_conflicts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  original_assumption TEXT NOT NULL,
  new_evidence TEXT NOT NULL,
  severity TEXT NOT NULL,
  affected_assumption TEXT NOT NULL DEFAULT '',
  implication TEXT NOT NULL DEFAULT '',
  recommended_response TEXT NOT NULL DEFAULT '',
  evidence_id INTEGER,
  status TEXT NOT NULL DEFAULT 'open',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS missed_call_reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  prediction_id INTEGER,
  prediction TEXT NOT NULL,
  expected TEXT NOT NULL DEFAULT '',
  actual TEXT NOT NULL DEFAULT '',
  error_type TEXT NOT NULL DEFAULT '',
  lesson TEXT NOT NULL DEFAULT '',
  what_changes_now TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS trading_judgments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  market_date TEXT NOT NULL,
  symbol TEXT NOT NULL,
  action TEXT NOT NULL,
  verdict TEXT NOT NULL,
  conviction REAL,
  rationale TEXT NOT NULL DEFAULT '',
  evidence_hash TEXT NOT NULL DEFAULT '',
  evidence_json TEXT NOT NULL DEFAULT '{}',
  outcome_status TEXT NOT NULL DEFAULT 'pending',
  outcome_summary TEXT NOT NULL DEFAULT '',
  score REAL,
  lesson TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'zade_daily_loop',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(market_date, symbol, action, verdict, evidence_hash)
);

CREATE INDEX IF NOT EXISTS idx_trading_judgments_date_symbol
  ON trading_judgments (market_date, symbol);

CREATE TABLE IF NOT EXISTS cadence_reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  review_type TEXT NOT NULL,
  period TEXT NOT NULL,
  findings_json TEXT NOT NULL DEFAULT '{}',
  changes_json TEXT NOT NULL DEFAULT '{}',
  actions_json TEXT NOT NULL DEFAULT '[]',
  drift_detected INTEGER NOT NULL DEFAULT 0,
  highest_leverage_action TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS founder_experiments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  title TEXT NOT NULL,
  experiment_type TEXT NOT NULL DEFAULT 'validation',
  hypothesis TEXT NOT NULL DEFAULT '',
  target_persona TEXT NOT NULL DEFAULT '',
  owner TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  start_date TEXT,
  end_date TEXT,
  success_metric TEXT NOT NULL DEFAULT '',
  success_threshold TEXT NOT NULL DEFAULT '',
  minimum_evidence INTEGER NOT NULL DEFAULT 1,
  decision_rule TEXT NOT NULL DEFAULT '',
  linked_assumption_ids_json TEXT NOT NULL DEFAULT '[]',
  linked_bet_ids_json TEXT NOT NULL DEFAULT '[]',
  linked_goal_ids_json TEXT NOT NULL DEFAULT '[]',
  linked_prediction_ids_json TEXT NOT NULL DEFAULT '[]',
  evidence_ids_json TEXT NOT NULL DEFAULT '[]',
  result TEXT NOT NULL DEFAULT '',
  recommendation TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS experiment_reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  experiment_id INTEGER NOT NULL REFERENCES founder_experiments(id) ON DELETE CASCADE,
  review_type TEXT NOT NULL DEFAULT 'weekly',
  period TEXT NOT NULL,
  decision TEXT NOT NULL,
  outcome_summary TEXT NOT NULL DEFAULT '',
  findings_json TEXT NOT NULL DEFAULT '{}',
  next_actions_json TEXT NOT NULL DEFAULT '[]',
  evidence_ids_json TEXT NOT NULL DEFAULT '[]',
  confidence_delta INTEGER NOT NULL DEFAULT 0,
  status_after TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS integrity_warnings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  warning_type TEXT NOT NULL,
  subject_type TEXT NOT NULL,
  subject_id INTEGER,
  message TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'yellow',
  recommendation TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS conversations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  summary TEXT NOT NULL DEFAULT '',
  summary_through_turn_id INTEGER,
  distilled_through_turn_id INTEGER,
  turn_count INTEGER NOT NULL DEFAULT 0,
  last_message_at TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS conversation_turns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  task_type TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  authority_decision TEXT NOT NULL DEFAULT '',
  runtime_event_id INTEGER,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_conversation_turns_conversation
  ON conversation_turns(conversation_id, id);

CREATE TABLE IF NOT EXISTS eval_cases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  name TEXT NOT NULL UNIQUE,
  category TEXT NOT NULL DEFAULT 'custom',
  executor TEXT NOT NULL DEFAULT 'generate',
  task_type TEXT NOT NULL DEFAULT 'general',
  description TEXT NOT NULL DEFAULT '',
  prompt TEXT NOT NULL,
  draft TEXT NOT NULL DEFAULT '',
  checks_json TEXT NOT NULL DEFAULT '[]',
  respond_options_json TEXT NOT NULL DEFAULT '{}',
  setup_memories_json TEXT NOT NULL DEFAULT '[]',
  enabled INTEGER NOT NULL DEFAULT 1,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS eval_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  label TEXT NOT NULL DEFAULT 'manual',
  total INTEGER NOT NULL DEFAULT 0,
  passed INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0,
  errors INTEGER NOT NULL DEFAULT 0,
  pass_rate REAL NOT NULL DEFAULT 0,
  duration_ms INTEGER NOT NULL DEFAULT 0,
  model_roles_json TEXT NOT NULL DEFAULT '{}',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS eval_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  run_id INTEGER NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
  case_id INTEGER NOT NULL,
  case_name TEXT NOT NULL,
  category TEXT NOT NULL DEFAULT '',
  executor TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  score REAL NOT NULL DEFAULT 0,
  checks_json TEXT NOT NULL DEFAULT '[]',
  response_excerpt TEXT NOT NULL DEFAULT '',
  latency_ms INTEGER NOT NULL DEFAULT 0,
  model TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_eval_results_run ON eval_results(run_id, id);

CREATE TABLE IF NOT EXISTS skill_embeddings (
  skill_id INTEGER PRIMARY KEY REFERENCES skill_registry(id) ON DELETE CASCADE,
  model TEXT NOT NULL,
  dimensions INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  vector_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connectors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  name TEXT NOT NULL UNIQUE,
  connector_type TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  config_json TEXT NOT NULL DEFAULT '{}',
  enabled INTEGER NOT NULL DEFAULT 1,
  last_sync_at TEXT,
  last_sync_status TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS connector_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  connector_id INTEGER NOT NULL REFERENCES connectors(id) ON DELETE CASCADE,
  external_id TEXT NOT NULL,
  item_type TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  sender TEXT NOT NULL DEFAULT '',
  occurred_at TEXT,
  excerpt TEXT NOT NULL DEFAULT '',
  content_hash TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'candidate',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(connector_id, external_id)
);

CREATE TABLE IF NOT EXISTS action_plans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  title TEXT NOT NULL,
  objective TEXT NOT NULL DEFAULT '',
  source_type TEXT NOT NULL DEFAULT 'manual',
  source_id INTEGER,
  status TEXT NOT NULL DEFAULT 'active',
  priority INTEGER NOT NULL DEFAULT 50,
  owner TEXT NOT NULL DEFAULT 'founder',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS action_steps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  plan_id INTEGER NOT NULL REFERENCES action_plans(id) ON DELETE CASCADE,
  step_index INTEGER NOT NULL,
  title TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL DEFAULT 'founder.task',
  target TEXT NOT NULL DEFAULT '',
  permission_tier TEXT NOT NULL DEFAULT 'L1_MEMORY_WRITE',
  execution TEXT NOT NULL DEFAULT 'manual',
  authority_decision TEXT NOT NULL DEFAULT '',
  authority_reason TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  work_item_id INTEGER,
  approved_by TEXT NOT NULL DEFAULT '',
  result TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  evidence_ids_json TEXT NOT NULL DEFAULT '[]',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(plan_id, step_index)
);

CREATE TABLE IF NOT EXISTS commitments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  who TEXT NOT NULL DEFAULT 'founder',
  kind TEXT NOT NULL DEFAULT 'do',
  title TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  due_at TEXT,
  cadence TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open',
  source_type TEXT NOT NULL DEFAULT 'manual',
  source_id INTEGER,
  closed_at TEXT,
  closed_note TEXT NOT NULL DEFAULT '',
  renegotiation_count INTEGER NOT NULL DEFAULT 0,
  follow_up_count INTEGER NOT NULL DEFAULT 0,
  last_follow_up_at TEXT,
  evidence_ids_json TEXT NOT NULL DEFAULT '[]',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS commitment_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  commitment_id INTEGER NOT NULL REFERENCES commitments(id) ON DELETE CASCADE,
  event TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  topic TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'info',
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'kernel',
  dedupe_key TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'queued',
  suppressed_reason TEXT NOT NULL DEFAULT '',
  read_at TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS notification_deliveries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  notification_id INTEGER NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
  channel TEXT NOT NULL,
  status TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS notification_channels (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  channel TEXT NOT NULL UNIQUE,
  enabled INTEGER NOT NULL DEFAULT 0,
  min_severity TEXT NOT NULL DEFAULT 'info',
  quiet_start TEXT NOT NULL DEFAULT '',
  quiet_end TEXT NOT NULL DEFAULT '',
  rate_limit_per_hour INTEGER NOT NULL DEFAULT 30,
  recipients_json TEXT NOT NULL DEFAULT '[]',
  config_json TEXT NOT NULL DEFAULT '{}',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS action_handler_access (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  action TEXT NOT NULL UNIQUE,
  enabled INTEGER NOT NULL DEFAULT 1,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS work_plans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  conversation_id INTEGER,
  title TEXT NOT NULL,
  workspace TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active'
);

CREATE INDEX IF NOT EXISTS idx_work_plans_conversation
  ON work_plans (conversation_id, status);

CREATE TABLE IF NOT EXISTS work_plan_steps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id INTEGER NOT NULL,
  step_number INTEGER NOT NULL,
  instructions TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  last_item_id INTEGER,
  last_outcome TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL,
  UNIQUE (plan_id, step_number)
);
"""
