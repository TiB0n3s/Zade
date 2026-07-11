from pathlib import Path

from cofounder_kernel.authority import AuthorityPolicy
from cofounder_kernel.autonomy import WorkQueueService
from cofounder_kernel.config import AppConfig, IdentityConfig, KernelConfig, OllamaConfig, PathConfig, ensure_local_paths
from cofounder_kernel.db import KernelDatabase
from cofounder_kernel.ingestion import IngestionService


class FakeEmbedder:
    def embed(self, *, text: str, model: str | None = None) -> list[float]:
        return [1.0, 0.0] if "audit" in text.lower() else [0.0, 1.0]


def make_queue(tmp_path: Path) -> tuple[WorkQueueService, KernelDatabase, KernelConfig]:
    config = KernelConfig(
        app=AppConfig(),
        identity=IdentityConfig(name="Zade"),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    ensure_local_paths(config)
    db = KernelDatabase(config.paths.database_path)
    db.migrate()
    authority = AuthorityPolicy.from_config(config)
    ingestion = IngestionService(config=config, db=db, embedder=FakeEmbedder())
    queue = WorkQueueService(
        config=config,
        db=db,
        authority=authority,
        ingestion=ingestion,
        inventory_provider=lambda: {"identity": {"name": "Zade"}, "locality": {"local_only": True}},
    )
    return queue, db, config


def test_scan_queues_local_autonomous_work(tmp_path: Path) -> None:
    queue, db, _config = make_queue(tmp_path)

    result = queue.scan(run_autonomous=False)
    items = db.list_work_items()

    assert result["created_count"] == 2
    assert {item.action for item in items} == {"brief.daily.prepare", "self.inventory.snapshot"}
    assert all(item.status == "pending" for item in items)
    assert all(item.authority_decision == "allow" for item in items)


def test_run_due_prepares_brief_and_inventory_snapshot(tmp_path: Path) -> None:
    queue, db, _config = make_queue(tmp_path)
    queue.scan(run_autonomous=False)

    results = queue.run_due(max_items=2)
    memories = db.search_memories("Zade", limit=10)

    assert [result.status for result in results] == ["done", "done"]
    assert db.work_queue_counts()["done"] == 2
    assert any(memory.kind == "brief" for memory in memories)
    assert any(memory.kind == "system_snapshot" for memory in memories)


def test_scan_queues_and_runs_inbox_ingestion(tmp_path: Path) -> None:
    queue, db, config = make_queue(tmp_path)
    inbox_file = config.paths.inbox_dir / "note.md"
    inbox_file.write_text("# Audit note\n\nAudit trails should be searchable.", encoding="utf-8")

    result = queue.scan(run_autonomous=True, max_run=5)
    semantic_matches = db.semantic_search_chunks([1.0, 0.0], limit=3)

    assert result["created_count"] == 3
    assert any(item["action"] == "ingest.file" for item in result["queued"])
    assert any(run["action"] == "ingest.file" and run["status"] == "done" for run in result["run"])
    assert semantic_matches[0]["document_title"] == "note.md"


def test_enqueue_external_action_stops_at_approval_required(tmp_path: Path) -> None:
    queue, db, _config = make_queue(tmp_path)

    queued = queue.enqueue(
        kind="external",
        title="Send email",
        detail="Outbound email requires approval.",
        action="email.send",
        target="founder@example.com",
        permission_tier="L3_EXTERNAL_ACTION",
    )
    run = queue.run_next()

    assert queued.status == "approval_required"
    assert run.status == "empty"
    assert db.work_queue_counts()["approval_required"] == 1
