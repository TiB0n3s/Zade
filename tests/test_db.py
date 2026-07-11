from pathlib import Path

from cofounder_kernel.db import KernelDatabase


def test_memory_write_search_and_audit(tmp_path: Path) -> None:
    db = KernelDatabase(tmp_path / "kernel.sqlite")
    db.migrate()

    memory_id = db.add_memory(
        kind="decision",
        title="Use local-first model routing",
        content="The co-founder should use Ollama before any cloud model.",
        source="test",
    )
    audit_id = db.audit(
        actor="test",
        action="memory.write",
        target=str(memory_id),
        permission_tier="L1_MEMORY_WRITE",
        status="ok",
    )

    matches = db.search_memories("Ollama", limit=5)
    events = db.recent_audit_events()

    assert memory_id > 0
    assert audit_id > 0
    assert len(matches) == 1
    assert matches[0].title == "Use local-first model routing"
    assert events[0]["action"] == "memory.write"


def test_memory_search_falls_back_from_sentence_to_keywords(tmp_path: Path) -> None:
    db = KernelDatabase(tmp_path / "kernel.sqlite")
    db.migrate()
    db.add_memory(
        kind="decision",
        title="Build local first",
        content="The co-founder should rely on Ollama and SQLite before external APIs.",
        source="test",
    )

    matches = db.search_memories("Based on local memory, what is the first build principle?", limit=5)

    assert len(matches) == 1
    assert matches[0].title == "Build local first"


def test_work_queue_dedupes_and_returns_next_item(tmp_path: Path) -> None:
    db = KernelDatabase(tmp_path / "kernel.sqlite")
    db.migrate()

    first_id, first_created = db.enqueue_work_item(
        kind="brief",
        title="Prepare daily brief",
        detail="Summarize local state.",
        action="brief.daily.prepare",
        target="local_memory",
        permission_tier="L1_MEMORY_WRITE",
        priority=80,
        unique_key="brief:today",
    )
    second_id, second_created = db.enqueue_work_item(
        kind="brief",
        title="Prepare daily brief",
        detail="Summarize local state.",
        action="brief.daily.prepare",
        target="local_memory",
        permission_tier="L1_MEMORY_WRITE",
        priority=80,
        unique_key="brief:today",
    )

    item = db.next_work_item()

    assert first_created is True
    assert second_created is False
    assert second_id == first_id
    assert item is not None
    assert item.action == "brief.daily.prepare"


def test_work_item_status_update_and_counts(tmp_path: Path) -> None:
    db = KernelDatabase(tmp_path / "kernel.sqlite")
    db.migrate()
    item_id, _created = db.enqueue_work_item(
        kind="snapshot",
        title="Snapshot self inventory",
        detail="Capture local posture.",
        action="self.inventory.snapshot",
        target="self-inventory",
        permission_tier="L1_MEMORY_WRITE",
    )

    db.update_work_item(item_id, status="done", authority_decision="allow", result={"memory_id": 7})

    items = db.list_work_items(status="done")
    counts = db.work_queue_counts()

    assert len(items) == 1
    assert items[0].result["memory_id"] == 7
    assert counts["done"] == 1
