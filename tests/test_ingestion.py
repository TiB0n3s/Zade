from pathlib import Path

from cofounder_kernel.config import AppConfig, KernelConfig, OllamaConfig, PathConfig, ensure_local_paths
from cofounder_kernel.db import KernelDatabase
from cofounder_kernel.ingestion import IngestionService, chunk_text
from cofounder_kernel.ollama import OllamaClient


class FakeEmbedder:
    def embed(self, *, text: str, model: str | None = None) -> list[float]:
        lowered = text.lower()
        if "audit" in lowered:
            return [1.0, 0.0, 0.0]
        if "backup" in lowered:
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]


def make_service(tmp_path: Path) -> tuple[IngestionService, KernelDatabase, KernelConfig]:
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    ensure_local_paths(config)
    db = KernelDatabase(config.paths.database_path)
    db.migrate()
    return IngestionService(config=config, db=db, embedder=FakeEmbedder()), db, config


def test_chunk_text_overlaps_long_content() -> None:
    text = "alpha " * 700

    chunks = chunk_text(text, max_chars=500, overlap=50)

    assert len(chunks) > 1
    assert chunks[0].char_start == 0
    assert chunks[1].char_start < chunks[0].char_end


def test_ingest_text_and_semantic_search(tmp_path: Path) -> None:
    service, _db, _config = make_service(tmp_path)

    result = service.ingest_text(
        title="Audit policy",
        text="Every memory write needs audit logs for accountability.",
        source="test",
    )
    matches = service.semantic_search(query="audit logs", limit=3)

    assert result.status == "ok"
    assert result.document_id is not None
    assert result.chunks_count == 1
    assert matches[0]["document_title"] == "Audit policy"
    assert matches[0]["score"] > 0.99


def test_ingest_text_dedupes_by_content_hash(tmp_path: Path) -> None:
    service, _db, _config = make_service(tmp_path)

    first = service.ingest_text(title="One", text="Backups belong on cold storage.", source="a")
    second = service.ingest_text(title="Two", text="Backups belong on cold storage.", source="b")

    assert first.status == "ok"
    assert second.status == "skipped"
    assert second.document_id == first.document_id


def test_ingest_file_archives_to_cold_raw_ingest(tmp_path: Path) -> None:
    service, _db, config = make_service(tmp_path)
    source = config.paths.inbox_dir / "note.md"
    source.write_text("# Note\n\nAudit trails matter.", encoding="utf-8")

    result = service.ingest_file(path=source)

    assert result.status == "ok"
    archived = list(config.paths.cold_raw_ingest_dir.glob("*.md"))
    assert len(archived) == 1
    assert archived[0].read_text(encoding="utf-8") == "# Note\n\nAudit trails matter."


def fake_embed(self: OllamaClient, *, text: str, model: str | None = None) -> list[float]:
    return FakeEmbedder().embed(text=text, model=model)

