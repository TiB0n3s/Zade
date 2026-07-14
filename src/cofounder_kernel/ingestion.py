from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import KernelConfig
from .db import KernelDatabase, utc_now


SUPPORTED_TEXT_EXTENSIONS = {
    ".csv",
    ".json",
    ".jsonl",
    ".log",
    ".md",
    ".ps1",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}


class Embedder(Protocol):
    def embed(self, *, text: str, model: str | None = None) -> list[float]:
        ...


RETRIEVAL_MODES = {"hybrid", "vector", "keyword"}
RRF_K = 60


def _rrf(rank: int) -> float:
    return round(1.0 / (RRF_K + rank), 6)


def _nomic_task_prefix(model: str, kind: str) -> str:
    """nomic-embed-text is trained for asymmetric retrieval with task prefixes
    ("search_query: " / "search_document: "). Without them, embeddings cluster near
    0.5 and the true query→document match does not stand out. Applied only to
    nomic-family models so a different embedder is unaffected."""
    if "nomic" in (model or "").lower():
        return f"search_{kind}: "
    return ""


# Defense-in-depth: memory is a trust surface, so the write path refuses to persist
# obvious credentials/keys/tokens even if an extractor or command tries to.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key block"),
    (re.compile(r"(?i)\b(api[_-]?key|secret|password|passwd|client[_-]?secret|token|bearer)\b\s*[:=]\s*\S{6,}"), "credential assignment"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key id"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "GitHub token"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "Slack token"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "JWT"),
)


def _looks_like_secret(text: str) -> str | None:
    """Return a label when the text contains an obvious credential/key/token, else
    None. Cheap heuristic — not a guarantee, but it stops the common cases from
    ever landing in the (git-tracked) memory store."""
    for pattern, label in _SECRET_PATTERNS:
        if pattern.search(text or ""):
            return label
    return None


def _fuse_memory_rankings(
    vector_hits: list[dict],
    keyword_hits: list[dict],
    *,
    limit: int,
    degraded: bool,
) -> list[dict]:
    """Reciprocal-rank-fuse memory vector and keyword hits by memory id, mirroring
    _fuse_rankings for document chunks."""
    fused: dict[int, dict] = {}
    for rank, hit in enumerate(vector_hits, start=1):
        mid = int(hit["id"])
        fused[mid] = hit | {
            "retrieval": {
                "mode": "hybrid",
                "vector_rank": rank,
                "keyword_rank": None,
                "rrf_score": _rrf(rank),
                "degraded_to_keyword": degraded,
            }
        }
    for rank, hit in enumerate(keyword_hits, start=1):
        mid = int(hit["id"])
        if mid in fused:
            retrieval = fused[mid]["retrieval"]
            retrieval["keyword_rank"] = rank
            retrieval["rrf_score"] = round(retrieval["rrf_score"] + _rrf(rank), 6)
        else:
            fused[mid] = hit | {
                "score": hit.get("score", 0.0),
                "retrieval": {
                    "mode": "hybrid",
                    "vector_rank": None,
                    "keyword_rank": rank,
                    "rrf_score": _rrf(rank),
                    "degraded_to_keyword": degraded,
                },
            }
    ranked = sorted(fused.values(), key=lambda hit: hit["retrieval"]["rrf_score"], reverse=True)
    return ranked[:limit]


def _fuse_rankings(
    vector_hits: list[dict],
    keyword_hits: list[dict],
    *,
    limit: int,
    degraded: bool,
) -> list[dict]:
    fused: dict[int, dict] = {}
    for rank, hit in enumerate(vector_hits, start=1):
        chunk_id = int(hit["chunk_id"])
        fused[chunk_id] = hit | {
            "retrieval": {
                "mode": "hybrid",
                "vector_rank": rank,
                "keyword_rank": None,
                "rrf_score": _rrf(rank),
                "degraded_to_keyword": degraded,
            }
        }
    for hit in keyword_hits:
        chunk_id = int(hit["chunk_id"])
        keyword_rank = int(hit["keyword_rank"])
        if chunk_id in fused:
            retrieval = fused[chunk_id]["retrieval"]
            retrieval["keyword_rank"] = keyword_rank
            retrieval["rrf_score"] = round(retrieval["rrf_score"] + _rrf(keyword_rank), 6)
        else:
            fused[chunk_id] = hit | {
                "score": 0.0,
                "retrieval": {
                    "mode": "hybrid",
                    "vector_rank": None,
                    "keyword_rank": keyword_rank,
                    "rrf_score": _rrf(keyword_rank),
                    "degraded_to_keyword": degraded,
                },
            }
    ranked = sorted(
        fused.values(),
        key=lambda item: (-item["retrieval"]["rrf_score"], -float(item.get("score", 0.0)), int(item["chunk_id"])),
    )
    return ranked[:limit]


@dataclass(frozen=True)
class TextChunk:
    text: str
    char_start: int
    char_end: int


@dataclass(frozen=True)
class IngestResult:
    job_id: int
    status: str
    document_id: int | None
    documents_count: int
    chunks_count: int
    skipped: bool = False
    error: str = ""


class IngestionService:
    def __init__(self, *, config: KernelConfig, db: KernelDatabase, embedder: Embedder):
        self.config = config
        self.db = db
        self.embedder = embedder

    def ingest_text(
        self,
        *,
        title: str,
        text: str,
        source: str = "text",
        metadata: dict | None = None,
    ) -> IngestResult:
        job_id = self.db.create_ingestion_job(job_type="text", source=source, metadata=metadata)
        try:
            result = self._store_document(
                title=title,
                text=text,
                source_uri=source,
                media_type="text/plain",
                metadata=metadata or {},
            )
            self.db.update_ingestion_job(
                job_id,
                status=result.status,
                documents_count=result.documents_count,
                chunks_count=result.chunks_count,
                metadata={"document_id": result.document_id, "skipped": result.skipped},
            )
            self.db.audit(
                actor="ingestion",
                action="ingest.text",
                target=source,
                permission_tier="L1_MEMORY_WRITE",
                status=result.status,
                details={"document_id": result.document_id, "chunks_count": result.chunks_count},
            )
            return with_job_id(result, job_id)
        except Exception as exc:
            self.db.update_ingestion_job(job_id, status="error", error=str(exc))
            self.db.audit(
                actor="ingestion",
                action="ingest.text",
                target=source,
                permission_tier="L1_MEMORY_WRITE",
                status="error",
                details={"error": str(exc)},
            )
            return IngestResult(job_id=job_id, status="error", document_id=None, documents_count=0, chunks_count=0, error=str(exc))

    def ingest_file(self, *, path: str | Path, metadata: dict | None = None) -> IngestResult:
        file_path = self._resolve_allowed_path(path)
        job_id = self.db.create_ingestion_job(job_type="file", source=str(file_path), metadata=metadata)
        try:
            if not file_path.is_file():
                raise ValueError(f"Not a file: {file_path}")
            if file_path.suffix.lower() not in SUPPORTED_TEXT_EXTENSIONS:
                raise ValueError(f"Unsupported text extension: {file_path.suffix}")
            text = read_text_file(file_path)
            archived_path = self._archive_file(file_path)
            result = self._store_document(
                title=file_path.name,
                text=text,
                source_uri=str(file_path),
                media_type=media_type_for_path(file_path),
                metadata={**(metadata or {}), "archived_path": str(archived_path)},
            )
            self.db.update_ingestion_job(
                job_id,
                status=result.status,
                documents_count=result.documents_count,
                chunks_count=result.chunks_count,
                metadata={"document_id": result.document_id, "skipped": result.skipped, "archived_path": str(archived_path)},
            )
            self.db.audit(
                actor="ingestion",
                action="ingest.file",
                target=str(file_path),
                permission_tier="L1_MEMORY_WRITE",
                status=result.status,
                details={"document_id": result.document_id, "chunks_count": result.chunks_count, "archived_path": str(archived_path)},
            )
            return with_job_id(result, job_id)
        except Exception as exc:
            self.db.update_ingestion_job(job_id, status="error", error=str(exc))
            self.db.audit(
                actor="ingestion",
                action="ingest.file",
                target=str(file_path),
                permission_tier="L1_MEMORY_WRITE",
                status="error",
                details={"error": str(exc)},
            )
            return IngestResult(job_id=job_id, status="error", document_id=None, documents_count=0, chunks_count=0, error=str(exc))

    def ingest_folder(self, *, path: str | Path, recursive: bool = True, metadata: dict | None = None) -> dict:
        folder_path = self._resolve_allowed_path(path)
        job_id = self.db.create_ingestion_job(job_type="folder", source=str(folder_path), metadata=metadata)
        documents_count = 0
        chunks_count = 0
        files_count = 0
        errors: list[dict] = []
        try:
            if not folder_path.is_dir():
                raise ValueError(f"Not a folder: {folder_path}")
            pattern = "**/*" if recursive else "*"
            for file_path in sorted(folder_path.glob(pattern)):
                if not file_path.is_file() or file_path.suffix.lower() not in SUPPORTED_TEXT_EXTENSIONS:
                    continue
                files_count += 1
                result = self.ingest_file(path=file_path, metadata=metadata)
                if result.status == "error":
                    errors.append({"path": str(file_path), "error": result.error})
                    continue
                if not result.skipped:
                    documents_count += result.documents_count
                    chunks_count += result.chunks_count
            status = "ok" if not errors else "partial"
            self.db.update_ingestion_job(
                job_id,
                status=status,
                documents_count=documents_count,
                chunks_count=chunks_count,
                metadata={"files_count": files_count, "errors": errors},
            )
            self.db.audit(
                actor="ingestion",
                action="ingest.folder",
                target=str(folder_path),
                permission_tier="L1_MEMORY_WRITE",
                status=status,
                details={"files_count": files_count, "documents_count": documents_count, "chunks_count": chunks_count, "errors": errors},
            )
            return {
                "job_id": job_id,
                "status": status,
                "files_count": files_count,
                "documents_count": documents_count,
                "chunks_count": chunks_count,
                "errors": errors,
            }
        except Exception as exc:
            self.db.update_ingestion_job(job_id, status="error", error=str(exc))
            self.db.audit(
                actor="ingestion",
                action="ingest.folder",
                target=str(folder_path),
                permission_tier="L1_MEMORY_WRITE",
                status="error",
                details={"error": str(exc)},
            )
            return {"job_id": job_id, "status": "error", "files_count": files_count, "documents_count": 0, "chunks_count": 0, "errors": [{"error": str(exc)}]}

    def semantic_search(self, *, query: str, limit: int = 8, mode: str = "hybrid") -> list[dict]:
        """Retrieve document chunks by hybrid keyword+vector fusion.

        Modes: "hybrid" (default) fuses FTS BM25 and embedding cosine rankings
        with reciprocal rank fusion; "vector" and "keyword" run a single
        ranking. Hybrid degrades to keyword-only when the embedder is
        unavailable instead of failing the request.
        """
        mode = (mode or "hybrid").strip().lower()
        if mode not in RETRIEVAL_MODES:
            raise ValueError(f"Retrieval mode must be one of: {', '.join(sorted(RETRIEVAL_MODES))}")
        pool = max(limit, min(limit * 3, 24))
        vector_hits: list[dict] = []
        vector_error = ""
        if mode in {"hybrid", "vector"}:
            try:
                embed_model = self.config.ollama.embedding_model
                vector = self.embedder.embed(text=_nomic_task_prefix(embed_model, "query") + query, model=embed_model)
                vector_hits = self.db.semantic_search_chunks(vector, limit=pool)
            except Exception as exc:
                if mode == "vector":
                    raise
                vector_error = str(exc)
        keyword_hits = self.db.keyword_search_chunks(query, limit=pool) if mode in {"hybrid", "keyword"} else []

        if mode == "vector":
            matches = [
                hit | {"retrieval": {"mode": "vector", "vector_rank": rank, "rrf_score": _rrf(rank)}}
                for rank, hit in enumerate(vector_hits[:limit], start=1)
            ]
        elif mode == "keyword":
            matches = [
                hit
                | {
                    "score": 0.0,
                    "retrieval": {"mode": "keyword", "keyword_rank": hit["keyword_rank"], "rrf_score": _rrf(hit["keyword_rank"])},
                }
                for hit in keyword_hits[:limit]
            ]
        else:
            matches = _fuse_rankings(vector_hits, keyword_hits, limit=limit, degraded=bool(vector_error))
        self.db.audit(
            actor="ingestion",
            action="memory.semantic_search",
            target="document_chunks",
            permission_tier="L0_READ",
            status="ok" if not vector_error else "degraded",
            details={
                "query": query,
                "mode": mode,
                "matches": len(matches),
                "vector_candidates": len(vector_hits),
                "keyword_candidates": len(keyword_hits),
                "vector_error": vector_error,
            },
        )
        return matches

    def rebuild_memory_embeddings(self) -> dict:
        """(Re)compute the semantic index over the memory store from the memories
        table — the derived, rebuildable index behind semantic memory recall.
        Incremental: skips memories whose title+content is unchanged (content_hash
        match). Never raises on an embedder outage; those memories stay keyword-only
        until the next rebuild.
        """
        model = self.config.ollama.embedding_model
        doc_prefix = _nomic_task_prefix(model, "document")
        existing = self.db.list_memory_embedding_hashes(model)
        memories = self.db.list_all_memories()
        embedded = skipped = failed = 0
        for memory in memories:
            # Hash the exact text embedded (prefix included), so any change to the
            # embedding recipe — like adding these prefixes — invalidates stale
            # embeddings and forces a re-embed.
            embed_text = doc_prefix + f"{memory['title']}\n{memory['content']}".strip()
            digest = sha256_text(embed_text)
            if existing.get(memory["id"]) == digest:
                skipped += 1
                continue
            try:
                vector = self.embedder.embed(text=embed_text, model=model)
            except Exception:
                failed += 1
                continue
            if vector:
                self.db.upsert_memory_embedding(
                    memory_id=memory["id"], model=model, vector=vector, content_hash=digest
                )
                embedded += 1
            else:
                failed += 1
        return {"embedded": embedded, "skipped": skipped, "failed": failed, "total": len(memories), "model": model}

    def rebuild_chunk_embeddings(self) -> dict:
        """Re-embed document chunks with the nomic search_document prefix so
        semantic document recall matches queries properly (same fix as the memory
        store). Incremental: skips chunks whose prefixed text is unchanged
        (content_hash). Best-effort — an embedder outage leaves chunks as-is."""
        model = self.config.ollama.embedding_model
        doc_prefix = _nomic_task_prefix(model, "document")
        existing = self.db.list_chunk_embedding_hashes(model)
        chunks = self.db.list_all_chunks()
        embedded = skipped = failed = 0
        for chunk in chunks:
            embed_text = doc_prefix + (chunk["text"] or "")
            digest = sha256_text(embed_text)
            if existing.get(chunk["id"]) == digest:
                skipped += 1
                continue
            try:
                vector = self.embedder.embed(text=embed_text, model=model)
            except Exception:
                failed += 1
                continue
            if vector:
                self.db.add_chunk_embedding(chunk_id=chunk["id"], model=model, vector=vector, content_hash=digest)
                embedded += 1
            else:
                failed += 1
        return {"embedded": embedded, "skipped": skipped, "failed": failed, "total": len(chunks), "model": model}

    def search_memories_hybrid(self, *, query: str, limit: int = 5, mode: str = "hybrid") -> list[dict]:
        """Recall durable memories by hybrid keyword+vector fusion, so a paraphrase
        that shares no words with a memory still finds it. Degrades to keyword-only
        when the embedder is unavailable (or nothing is embedded yet) instead of
        failing — recall never goes blind.
        """
        query = (query or "").strip()
        if not query:
            return []
        mode = (mode or "hybrid").strip().lower()
        if mode not in RETRIEVAL_MODES:
            mode = "hybrid"
        pool = max(limit, min(limit * 3, 24))
        vector_hits: list[dict] = []
        vector_error = ""
        if mode in {"hybrid", "vector"}:
            try:
                embed_model = self.config.ollama.embedding_model
                vector = self.embedder.embed(
                    text=_nomic_task_prefix(embed_model, "query") + query, model=embed_model
                )
                vector_hits = self.db.semantic_search_memories(vector, limit=pool)
            except Exception as exc:
                if mode == "vector":
                    raise
                vector_error = str(exc)
        keyword_hits: list[dict] = []
        if mode in {"hybrid", "keyword"}:
            keyword_hits = [
                {
                    "id": record.id,
                    "kind": record.kind,
                    "title": record.title,
                    "content": record.content,
                    "source": record.source,
                    "metadata": record.metadata,
                    "score": 0.0,
                }
                for record in self.db.search_memories(query, limit=pool)
            ]
        if mode == "vector":
            matches = [
                hit | {"retrieval": {"mode": "vector", "vector_rank": rank, "rrf_score": _rrf(rank)}}
                for rank, hit in enumerate(vector_hits[:limit], start=1)
            ]
        elif mode == "keyword":
            matches = [
                hit | {"retrieval": {"mode": "keyword", "keyword_rank": rank, "rrf_score": _rrf(rank)}}
                for rank, hit in enumerate(keyword_hits[:limit], start=1)
            ]
        else:
            matches = _fuse_memory_rankings(vector_hits, keyword_hits, limit=limit, degraded=bool(vector_error))
        try:
            self.db.audit(
                actor="ingestion",
                action="memory.recall",
                target="memories",
                permission_tier="L0_READ",
                status="ok" if not vector_error else "degraded",
                details={
                    "query": query[:240],
                    "mode": mode,
                    "matches": len(matches),
                    "vector_candidates": len(vector_hits),
                    "keyword_candidates": len(keyword_hits),
                    "vector_error": vector_error,
                },
            )
        except Exception:
            pass
        return matches

    def save_memory(
        self,
        *,
        kind: str,
        title: str,
        content: str,
        source: str = "local",
        metadata: dict | None = None,
        dedupe: bool = True,
        dedupe_threshold: float = 0.93,
    ) -> dict:
        """The governed write path for durable memory: refuses obvious secrets,
        rejects near-IDENTICAL re-saves, writes the row, and embeds it so it is
        immediately recallable. Never raises on an embedder outage — it writes
        without dedupe/embedding and reports ``degraded`` so the caller knows.
        Returns a status dict: written | duplicate | blocked_secret | empty.

        Dedupe is deliberately conservative. Embedding cosine measures "same
        subject", not "same fact": a genuinely NEW fact about a known subject
        ("standups moved to 2pm") can score higher against the old fact than a
        reworded true duplicate does. So the threshold sits high enough to catch
        near-identical re-extractions while never dropping a differing update —
        keeping a stray near-dup is cheap; silently losing a real update is not.
        """
        title = (title or "").strip()
        content = (content or "").strip()
        if not title or not content:
            return {"status": "empty"}
        secret = _looks_like_secret(f"{title}\n{content}")
        if secret:
            return {"status": "blocked_secret", "reason": secret}
        model = self.config.ollama.embedding_model
        embed_text = _nomic_task_prefix(model, "document") + f"{title}\n{content}".strip()
        vector: list[float] = []
        degraded = False
        try:
            vector = self.embedder.embed(text=embed_text, model=model)
        except Exception:
            degraded = True
        if dedupe and vector:
            top = self.db.semantic_search_memories(vector, limit=1)
            if top and top[0]["score"] >= dedupe_threshold:
                return {
                    "status": "duplicate",
                    "duplicate_of": top[0]["id"],
                    "duplicate_title": top[0]["title"],
                    "score": round(top[0]["score"], 4),
                }
        memory_id = self.db.add_memory(kind=kind, title=title, content=content, source=source, metadata=metadata or {})
        if vector:
            self.db.upsert_memory_embedding(
                memory_id=memory_id, model=model, vector=vector, content_hash=sha256_text(embed_text)
            )
        # Files are the source of truth: mirror the new memory to disk (best-effort;
        # the DB stays the working index even if the vault write fails).
        try:
            row = self.db.get_memory(memory_id)
            if row:
                self.write_memory_file(row)
                self.write_memory_index()
        except Exception:
            pass
        return {"status": "written", "memory_id": memory_id, "degraded": degraded}

    def _memory_dir(self) -> Path:
        directory = Path(self.config.paths.hot_root) / "40-profile" / "zade" / "memory"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def write_memory_file(self, memory: dict) -> Path:
        """Write/replace the markdown file for one memory (its source of truth).
        Removes any stale file with the same id whose title slug changed."""
        from . import memory_files as mf

        directory = self._memory_dir()
        filename = mf.memory_filename(memory)
        mid = memory.get("id")
        if mid is not None:
            for stale in directory.glob(f"{int(mid):05d}-*.md"):
                if stale.name != filename:
                    stale.unlink(missing_ok=True)
        path = directory / filename
        path.write_text(mf.serialize_memory(memory), encoding="utf-8")
        return path

    def write_memory_index(self) -> Path:
        from . import memory_files as mf

        directory = self._memory_dir()
        entries = []
        for path in sorted(directory.glob("*.md")):
            if path.name == "INDEX.md":
                continue
            memory = mf.parse_memory_file(path.read_text(encoding="utf-8"))
            if memory:
                memory["filename"] = path.name
                entries.append(memory)
        index_path = directory / "INDEX.md"
        index_path.write_text(mf.render_index(entries), encoding="utf-8")
        return index_path

    def read_memory_files(self) -> list[dict]:
        from . import memory_files as mf

        directory = self._memory_dir()
        memories = []
        for path in sorted(directory.glob("*.md")):
            if path.name == "INDEX.md":
                continue
            memory = mf.parse_memory_file(path.read_text(encoding="utf-8"))
            if memory:
                memory["_path"] = str(path)
                memories.append(memory)
        return memories

    def export_memories_to_files(self, *, overwrite: bool = False) -> dict:
        """Backfill: write a file for every DB memory that lacks one (idempotent).
        Establishes the file store from the working DB without touching existing
        (possibly hand-edited) files unless overwrite is set."""
        directory = self._memory_dir()
        existing_ids = set()
        for path in directory.glob("*.md"):
            match = re.match(r"^(\d+)-", path.name)
            if match:
                existing_ids.add(int(match.group(1)))
        rows = self.db.list_memory_rows()
        written = 0
        for row in rows:
            if not overwrite and row["id"] in existing_ids:
                continue
            self.write_memory_file(row)
            written += 1
        self.write_memory_index()
        return {"written": written, "total": len(rows), "dir": str(directory)}

    def rebuild_index_from_files(self) -> dict:
        """Files are the source of truth: reconcile the DB (memories + FTS +
        embeddings) to match the files on disk exactly. Hand-edited files update
        their rows, hand-deleted files drop their rows, and a wiped DB is fully
        reconstructed. A file without an id is assigned one and rewritten so it
        stays stable across future rebuilds."""
        memories = self.read_memory_files()
        with_id = [m for m in memories if m.get("id") is not None]
        without_id = [m for m in memories if m.get("id") is None]
        assigned = []
        with self.db.connect() as conn:
            conn.execute("DELETE FROM memory_embeddings")
            conn.execute("DELETE FROM memories")
            for memory in with_id:
                conn.execute(
                    "INSERT INTO memories (id, created_at, kind, title, content, source, metadata_json) VALUES (?,?,?,?,?,?,?)",
                    (
                        int(memory["id"]),
                        memory.get("created_at") or utc_now(),
                        memory["kind"],
                        memory["title"],
                        memory["content"],
                        memory["source"],
                        json.dumps(memory.get("metadata") or {}, sort_keys=True),
                    ),
                )
            for memory in without_id:
                cur = conn.execute(
                    "INSERT INTO memories (created_at, kind, title, content, source, metadata_json) VALUES (?,?,?,?,?,?)",
                    (
                        memory.get("created_at") or utc_now(),
                        memory["kind"],
                        memory["title"],
                        memory["content"],
                        memory["source"],
                        json.dumps(memory.get("metadata") or {}, sort_keys=True),
                    ),
                )
                memory["id"] = int(cur.lastrowid)
                assigned.append(memory)
            conn.execute("INSERT INTO memory_fts(memory_fts) VALUES('rebuild')")
        for memory in assigned:
            old_path = memory.get("_path")
            new_path = self.write_memory_file(memory)
            if old_path and Path(old_path) != new_path:
                Path(old_path).unlink(missing_ok=True)
        embed = self.rebuild_memory_embeddings()
        self.write_memory_index()
        return {"files": len(memories), "assigned_new_ids": len(assigned), "embedded": embed}

    def _store_document(
        self,
        *,
        title: str,
        text: str,
        source_uri: str,
        media_type: str,
        metadata: dict,
    ) -> IngestResult:
        normalized_text = normalize_text(text)
        if not normalized_text:
            raise ValueError("No ingestible text content")
        content_hash = sha256_text(normalized_text)
        document_id, created = self.db.upsert_document(
            title=title,
            source_uri=source_uri,
            content_hash=content_hash,
            media_type=media_type,
            size_bytes=len(normalized_text.encode("utf-8")),
            metadata=metadata,
        )
        if not created:
            return IngestResult(
                job_id=0,
                status="skipped",
                document_id=document_id,
                documents_count=0,
                chunks_count=self.db.document_chunk_count(document_id),
                skipped=True,
            )
        chunks = chunk_text(normalized_text)
        for index, chunk in enumerate(chunks):
            chunk_id = self.db.add_document_chunk(
                document_id=document_id,
                chunk_index=index,
                text=chunk.text,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
            )
            embed_model = self.config.ollama.embedding_model
            embed_text = _nomic_task_prefix(embed_model, "document") + chunk.text
            vector = self.embedder.embed(text=embed_text, model=embed_model)
            self.db.add_chunk_embedding(
                chunk_id=chunk_id, model=embed_model, vector=vector, content_hash=sha256_text(embed_text)
            )
        return IngestResult(job_id=0, status="ok", document_id=document_id, documents_count=1, chunks_count=len(chunks))

    def _resolve_allowed_path(self, path: str | Path) -> Path:
        resolved = Path(path).expanduser().resolve()
        roots = [
            self.config.paths.hot_root.resolve(),
            self.config.paths.cold_root.resolve(),
        ]
        if not any(is_relative_to(resolved, root) for root in roots):
            raise ValueError(f"Path is outside allowed memory roots: {resolved}")
        # Never ingest the kernel's own state (SQLite DB, blobs, backups); it
        # lives inside hot_root by default and is not founder content.
        if is_relative_to(resolved, self.config.paths.data_dir.resolve()):
            raise ValueError(f"Refusing to ingest from the kernel state directory: {resolved}")
        return resolved

    def _archive_file(self, file_path: Path) -> Path:
        digest = sha256_bytes(file_path.read_bytes())
        target = self.config.paths.cold_raw_ingest_dir / f"{digest}{file_path.suffix.lower()}"
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file_path, target)
        return target


def read_text_file(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def normalize_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def chunk_text(text: str, *, max_chars: int = 1600, overlap: int = 200) -> list[TextChunk]:
    text = normalize_text(text)
    if not text:
        return []
    chunks: list[TextChunk] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        if end < len(text):
            boundary = max(text.rfind("\n\n", start, end), text.rfind("\n", start, end), text.rfind(". ", start, end))
            if boundary > start + max_chars // 2:
                end = boundary + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(TextChunk(text=chunk, char_start=start, char_end=end))
        if end >= len(text):
            break
        start = max(0, end - overlap)
    return chunks


def media_type_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".md"}:
        return "text/markdown"
    if suffix in {".json", ".jsonl"}:
        return "application/json"
    if suffix == ".csv":
        return "text/csv"
    return "text/plain"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def with_job_id(result: IngestResult, job_id: int) -> IngestResult:
    return IngestResult(
        job_id=job_id,
        status=result.status,
        document_id=result.document_id,
        documents_count=result.documents_count,
        chunks_count=result.chunks_count,
        skipped=result.skipped,
        error=result.error,
    )
