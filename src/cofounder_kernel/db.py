from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 9


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


class KernelDatabase:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
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
            conn.executescript(SCHEMA_SQL)
            conn.execute("INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)", ("version", str(SCHEMA_VERSION)))

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
                (utc_now(), actor, action, target, permission_tier, status, json.dumps(details or {}, sort_keys=True)),
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
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO memories (created_at, kind, title, content, source, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (utc_now(), kind, title, content, source, json.dumps(metadata or {}, sort_keys=True)),
            )
            memory_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO memory_fts (rowid, title, content, kind, source) VALUES (?, ?, ?, ?, ?)",
                (memory_id, title, content, kind, source),
            )
            return memory_id

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

    def work_queue_counts(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS count FROM work_items GROUP BY status").fetchall()
            return {str(row["status"]): int(row["count"]) for row in rows}

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

    def add_chunk_embedding(self, *, chunk_id: int, model: str, vector: list[float]) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT OR REPLACE INTO chunk_embeddings (chunk_id, model, dimensions, vector_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (chunk_id, model, len(vector), json.dumps(vector), utc_now()),
            )
            return int(cur.lastrowid)

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

    def search_memories(self, query: str, limit: int = 8) -> list[MemoryRecord]:
        query = query.strip()
        if not query:
            return []
        with self.connect() as conn:
            rows = self._search_fts(conn, query, limit)
            if not rows:
                token_query = _token_fts_query(query)
                if token_query and token_query != query:
                    rows = self._search_fts(conn, token_query, limit)
            if not rows:
                rows = self._search_like(conn, query, limit)
            return [_memory_from_row(row) for row in rows]

    def _search_fts(self, conn: sqlite3.Connection, query: str, limit: int) -> list[sqlite3.Row]:
        try:
            return conn.execute(
                """
                SELECT m.*
                FROM memory_fts f
                JOIN memories m ON m.id = f.rowid
                WHERE memory_fts MATCH ?
                ORDER BY bm25(memory_fts)
                LIMIT ?
                """,
                (query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []

    def _search_like(self, conn: sqlite3.Connection, query: str, limit: int) -> list[sqlite3.Row]:
        tokens = _query_tokens(query)
        if not tokens:
            tokens = [query]
        clauses = []
        params: list[Any] = []
        for token in tokens[:8]:
            clauses.append("(title LIKE ? OR content LIKE ? OR kind LIKE ? OR source LIKE ?)")
            params.extend([f"%{token}%", f"%{token}%", f"%{token}%", f"%{token}%"])
        params.append(limit)
        return conn.execute(
            f"""
            SELECT *
            FROM memories
            WHERE {' OR '.join(clauses)}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

    def recent_audit_events(self, limit: int = 25) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) | {"details": json.loads(row["details_json"] or "{}")} for row in rows]

    def daily_brief_inputs(self) -> dict[str, list[dict[str, Any]]]:
        with self.connect() as conn:
            return {
                "recent_memories": [dict(row) for row in conn.execute("SELECT * FROM memories ORDER BY id DESC LIMIT 8")],
                "active_goals": [dict(row) for row in conn.execute("SELECT * FROM goals WHERE status = 'active' ORDER BY id DESC LIMIT 8")],
                "open_decisions": [dict(row) for row in conn.execute("SELECT * FROM decisions WHERE status = 'open' ORDER BY id DESC LIMIT 8")],
                "recent_disagreements": [dict(row) for row in conn.execute("SELECT * FROM disagreements ORDER BY id DESC LIMIT 5")],
            }


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
  metadata_json TEXT NOT NULL DEFAULT '{}'
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
  vector_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(chunk_id, model)
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
"""
