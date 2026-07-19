from pathlib import Path

from cofounder_kernel.db import SCHEMA_VERSION, KernelDatabase


def test_current_schema_version(tmp_path: Path) -> None:
    db = KernelDatabase(tmp_path / "kernel.sqlite")
    db.migrate()

    assert SCHEMA_VERSION == 33
    assert db.schema_version() == 33


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


def test_approval_request_lifecycle(tmp_path: Path) -> None:
    db = KernelDatabase(tmp_path / "kernel.sqlite")
    db.migrate()
    item_id, _created = db.enqueue_work_item(
        kind="external",
        title="Send founder email",
        detail="Outbound email needs approval.",
        action="email.send",
        target="founder@example.com",
        permission_tier="L3_EXTERNAL_ACTION",
    )

    request, created = db.ensure_approval_request(
        source_type="work_item",
        source_id=item_id,
        title="Send founder email",
        detail="Outbound email needs approval.",
        action="email.send",
        target="founder@example.com",
        permission_tier="L3_EXTERNAL_ACTION",
        authority_decision="approval_required",
        authority={"decision": "approval_required"},
        requested_by="test",
    )
    duplicate, duplicate_created = db.ensure_approval_request(
        source_type="work_item",
        source_id=item_id,
        title="Send founder email",
        detail="Outbound email needs approval.",
        action="email.send",
        target="founder@example.com",
        permission_tier="L3_EXTERNAL_ACTION",
        authority_decision="approval_required",
        authority={"decision": "approval_required"},
        requested_by="test",
    )
    resolved = db.resolve_approval_request(request.id, status="approved", resolved_by="founder", resolution_note="ok")

    assert created is True
    assert duplicate_created is False
    assert duplicate.id == request.id
    assert request.source_id == item_id
    assert db.get_pending_approval_for_source(source_type="work_item", source_id=item_id) is None
    assert resolved.status == "approved"
    assert resolved.resolved_by == "founder"


def test_approval_training_events_capture_decision_snapshots(tmp_path: Path) -> None:
    db = KernelDatabase(tmp_path / "kernel.sqlite")
    db.migrate()
    item_id, _created = db.enqueue_work_item(
        kind="external",
        title="Send founder update",
        detail="Outbound update requires approval.",
        action="email.send",
        target="founder@example.com",
        permission_tier="L3_EXTERNAL_ACTION",
        metadata={"evidence": ["Founder requested SMS/email updates."], "risks": ["External send."]},
    )
    request, _request_created = db.ensure_approval_request(
        source_type="work_item",
        source_id=item_id,
        title="Send founder update",
        detail="Outbound update requires approval.",
        action="email.send",
        target="founder@example.com",
        permission_tier="L3_EXTERNAL_ACTION",
        authority_decision="approval_required",
        authority={"decision": "approval_required", "reason": "external action"},
        requested_by="test",
    )

    event_id = db.record_approval_training_event(
        approval_request_id=request.id,
        work_item_id=item_id,
        event_type="approval_resolution",
        outcome="approved",
        actor="founder",
        note="Approved after checking evidence.",
        action=request.action,
        target=request.target,
        permission_tier=request.permission_tier,
        authority_decision=request.authority_decision,
        authority=request.authority,
        request_snapshot=request.__dict__,
        work_item_snapshot={"id": item_id, "action": "email.send"},
    )
    events = db.list_approval_training_events(approval_request_id=request.id)

    assert event_id > 0
    assert events[0].outcome == "approved"
    assert events[0].request_snapshot["title"] == "Send founder update"
    assert events[0].work_item_snapshot["action"] == "email.send"


def test_model_call_telemetry_records_and_summarizes(tmp_path: Path) -> None:
    db = KernelDatabase(tmp_path / "kernel.sqlite")
    db.migrate()

    call_id = db.record_model_call(
        operation="runtime.respond",
        model="qwen3:14b",
        role="general",
        status="ok",
        latency_ms=1234,
        prompt_chars=500,
        response_chars=50,
        think=False,
        metadata={"event_id": 7},
    )
    db.record_model_call(
        operation="runtime.respond",
        model="deepseek-r1:14b",
        role="reasoning",
        status="error",
        latency_ms=25,
        prompt_chars=100,
        error="timeout",
    )

    calls = db.list_model_calls(limit=10)
    errors = db.list_model_calls(status="error", limit=10)
    summary = db.model_call_summary(limit=10)

    assert call_id > 0
    assert len(calls) == 2
    assert calls[-1].model == "qwen3:14b"
    assert errors[0].error == "timeout"
    assert summary["by_status"] == {"error": 1, "ok": 1}
    assert summary["by_role"]["general"] == 1
    assert summary["avg_latency_ms"] == 629
