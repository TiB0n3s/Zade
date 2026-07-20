from __future__ import annotations

import threading
from pathlib import Path

import pytest

from cofounder_kernel.db import KernelDatabase
from cofounder_kernel.project_autonomy_store import (
    ProjectAutonomyConflict,
    ProjectAutonomyStore,
)


def migrated_db(tmp_path: Path) -> KernelDatabase:
    db = KernelDatabase(tmp_path / "kernel.sqlite")
    db.migrate()
    return db


def make_project(db: KernelDatabase, tmp_path: Path) -> int:
    root = tmp_path / "project-intake" / "Same Ground"
    root.mkdir(parents=True, exist_ok=True)
    return db.upsert_project(
        canonical_path=str(root),
        name="Same Ground",
        product_type="mobile_application",
        distribution_targets=["google_play", "apple_app_store_eventual"],
        lifecycle_state="verified",
        repo_fingerprint="test-fingerprint",
        metadata={},
    )


def make_work_item(db: KernelDatabase) -> int:
    work_item_id, _created = db.enqueue_work_item(
        kind="founder_decision",
        title="Choose local storage",
        detail="Choose the documented local storage default.",
        action="project.decision.resolve",
        target="Same Ground",
        permission_tier="L1_MEMORY_WRITE",
        unique_key="test:project-decision",
    )
    return work_item_id


def decision_transition(project_id: int, work_item_id: int) -> tuple[dict, dict, dict]:
    return (
        {"phase": "needs_decision", "priority": "normal"},
        {"event_type": "decision_requested", "work_item_id": work_item_id},
        {
            "topic": "project.decision_required",
            "dedupe_key": f"project:{project_id}:decision:{work_item_id}",
            "severity": "warning",
            "title": "Decision needed",
            "body": "Open Zade to answer.",
        },
    )


def test_schema_version_33_has_autonomy_tables(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)

    assert db.schema_version() == 33
    with db.connect() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    assert {
        "project_autonomy_states",
        "project_autonomy_leases",
        "project_autonomy_outbox",
    } <= tables


def test_transition_updates_state_event_and_outbox_atomically(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)
    project_id = make_project(db, tmp_path)
    work_item_id = make_work_item(db)
    store = ProjectAutonomyStore(db)
    state, event, outbox = decision_transition(project_id, work_item_id)

    updated = store.transition(
        project_id,
        expected_version=0,
        state=state,
        event=event,
        outbox=outbox,
    )

    assert updated["version"] == 1
    assert updated["phase"] == "needs_decision"
    assert db.list_project_events(project_id)[0]["work_item_id"] == work_item_id
    assert store.due_outbox()[0]["dedupe_key"].endswith(f":decision:{work_item_id}")


def test_transition_rolls_back_every_record_on_failure(tmp_path: Path, monkeypatch) -> None:
    db = migrated_db(tmp_path)
    project_id = make_project(db, tmp_path)
    work_item_id = make_work_item(db)
    store = ProjectAutonomyStore(db)
    state, event, outbox = decision_transition(project_id, work_item_id)

    def fail_outbox(*_args, **_kwargs):
        raise RuntimeError("injected before commit")

    monkeypatch.setattr(store, "_insert_outbox", fail_outbox)

    with pytest.raises(RuntimeError, match="injected"):
        store.transition(
            project_id,
            expected_version=0,
            state=state,
            event=event,
            outbox=outbox,
        )

    assert store.get(project_id)["version"] == 0
    assert db.list_project_events(project_id) == []
    assert store.due_outbox() == []


def test_transition_rejects_stale_version(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)
    project_id = make_project(db, tmp_path)
    store = ProjectAutonomyStore(db)
    store.transition(
        project_id,
        expected_version=0,
        state={"phase": "planning"},
        event={"event_type": "autonomy_planned"},
    )

    with pytest.raises(ProjectAutonomyConflict):
        store.transition(
            project_id,
            expected_version=0,
            state={"phase": "building"},
            event={"event_type": "increment_started"},
        )


def test_two_simultaneous_claims_have_one_winner(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)
    project_id = make_project(db, tmp_path)
    store = ProjectAutonomyStore(db)
    barrier = threading.Barrier(2)
    results: list[dict | None] = []

    def claim(owner: str) -> None:
        barrier.wait()
        results.append(
            store.claim(
                project_id,
                owner=owner,
                run_id=f"run-{owner}",
                lease_seconds=60,
                expected_version=0,
            )
        )

    threads = [threading.Thread(target=claim, args=(owner,)) for owner in ("one", "two")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert sum(result is not None for result in results) == 1


def test_expired_claim_can_be_recovered(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)
    project_id = make_project(db, tmp_path)
    store = ProjectAutonomyStore(db)
    first = store.claim(
        project_id,
        owner="old-worker",
        run_id="old-run",
        lease_seconds=60,
        expected_version=0,
    )
    with db.connect() as conn:
        conn.execute(
            "UPDATE project_autonomy_leases SET expires_at = ? WHERE project_id = ?",
            ("2000-01-01T00:00:00+00:00", project_id),
        )

    recovered = store.claim(
        project_id,
        owner="new-worker",
        run_id="new-run",
        lease_seconds=60,
        expected_version=0,
    )

    assert first is not None
    assert recovered is not None
    assert recovered["owner"] == "new-worker"


def test_release_requires_matching_owner_and_run(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)
    project_id = make_project(db, tmp_path)
    store = ProjectAutonomyStore(db)
    store.claim(
        project_id,
        owner="worker",
        run_id="run-1",
        lease_seconds=60,
        expected_version=0,
    )

    assert store.release(project_id, owner="other", run_id="run-1") is False
    assert store.release(project_id, owner="worker", run_id="run-1") is True


def test_recovery_clears_lease_owned_by_a_dead_kernel_process(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)
    project_id = make_project(db, tmp_path)
    store = ProjectAutonomyStore(db)
    store.claim(
        project_id,
        owner="zade-project-autonomy:999999",
        run_id="abandoned-run",
        lease_seconds=900,
        expected_version=0,
    )

    cleared = store.clear_orphaned_process_leases(is_process_alive=lambda _pid: False)

    assert cleared == 1
    recovered = store.claim(
        project_id,
        owner="zade-project-autonomy:current",
        run_id="current-run",
        lease_seconds=900,
        expected_version=0,
    )
    assert recovered is not None


def test_outbox_dedupe_preserves_undelivered_row(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)
    project_id = make_project(db, tmp_path)
    work_item_id = make_work_item(db)
    store = ProjectAutonomyStore(db)
    state, event, outbox = decision_transition(project_id, work_item_id)
    store.transition(
        project_id,
        expected_version=0,
        state=state,
        event=event,
        outbox=outbox,
    )

    store.transition(
        project_id,
        expected_version=1,
        state={**state, "next_action": "still waiting"},
        event={"event_type": "decision_reminder", "work_item_id": work_item_id},
        outbox=outbox,
    )

    rows = store.due_outbox()
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    assert rows[0]["attempts"] == 0


@pytest.mark.parametrize("bad_id", [True, False, 0, -1])
def test_store_rejects_invalid_project_ids(tmp_path: Path, bad_id) -> None:
    db = migrated_db(tmp_path)
    store = ProjectAutonomyStore(db)

    with pytest.raises(ValueError):
        store.get(bad_id)
