from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import KernelConfig
from .db import KernelDatabase


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
                vector = self.embedder.embed(text=query, model=self.config.ollama.embedding_model)
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
            vector = self.embedder.embed(text=chunk.text, model=self.config.ollama.embedding_model)
            self.db.add_chunk_embedding(chunk_id=chunk_id, model=self.config.ollama.embedding_model, vector=vector)
        return IngestResult(job_id=0, status="ok", document_id=document_id, documents_count=1, chunks_count=len(chunks))

    def _resolve_allowed_path(self, path: str | Path) -> Path:
        resolved = Path(path).expanduser().resolve()
        roots = [
            self.config.paths.hot_root.resolve(),
            self.config.paths.cold_root.resolve(),
            self.config.paths.data_dir.resolve(),
        ]
        if not any(is_relative_to(resolved, root) for root in roots):
            raise ValueError(f"Path is outside allowed memory roots: {resolved}")
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
