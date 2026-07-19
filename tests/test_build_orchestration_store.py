from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cofounder_kernel.build_store import BuildStore
from cofounder_kernel.build_types import (
    BuildAssessment,
    BuildTaskKind,
    BuildTaskStatus,
    BuildTier,
)
from cofounder_kernel.db import KernelDatabase, SCHEMA_VERSION


def sample_assessment() -> BuildAssessment:
    return BuildAssessment(
        id=None,
        task="Ship a durable SaaS build",
        acceptance="The release verification suite passes",
        workspace="C:/workspace",
        repo_fingerprint="fingerprint-1",
        deterministic_score=38,
        local_adjustment=2,
        final_score=40,
        confidence=0.9,
        recommended_tier=BuildTier.MEDIUM,
        dimensions={"product_surfaces": 6, "change_breadth": 5},
        floor_rules=(),
        evidence={"frameworks": ["fastapi", "react"]},
        unknowns=(),
        local_work=("Implement and verify locally",),
        cloud_reasons=("Review the final architecture",),
        created_at="2026-07-19T12:00:00+00:00",
    )


def make_store(path: Path) -> BuildStore:
    database = KernelDatabase(path / "kernel.sqlite")
    database.migrate()
    return BuildStore(database)


def create_session(store: BuildStore):
    return store.create_session(sample_assessment())


def test_migration_creates_durable_orchestration_tables(tmp_path: Path) -> None:
    database = KernelDatabase(tmp_path / "kernel.sqlite")
    database.migrate()

    with database.connect() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert SCHEMA_VERSION == 30
    assert {"build_tasks", "build_task_runs", "build_artifacts"} <= tables


def test_tasks_are_idempotent_and_become_ready_after_dependencies(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    session = create_session(store)
    discovery = store.create_task(
        session.id,
        phase="discovery",
        kind=BuildTaskKind.CHECKPOINT,
        title="Discover constraints",
        payload={"question": "What must ship?"},
        acceptance={"required": ["scope"]},
        idempotency_key="discovery",
    )
    duplicate = store.create_task(
        session.id,
        phase="discovery",
        kind=BuildTaskKind.CHECKPOINT,
        title="This duplicate must not overwrite the original",
        idempotency_key="discovery",
    )
    requirements = store.create_task(
        session.id,
        phase="requirements",
        kind=BuildTaskKind.AGENT,
        title="Write requirements",
        dependencies=(discovery.id,),
        idempotency_key="requirements",
    )

    assert duplicate == discovery
    assert store.ready_tasks(session.id) == [discovery]

    run = store.claim_task(discovery.id, worker_id="worker-1")
    completed = store.finish_task_run(
        run.id,
        status=BuildTaskStatus.SUCCEEDED,
        result={"scope": "MVP"},
    )

    assert completed.status is BuildTaskStatus.SUCCEEDED
    assert store.ready_tasks(session.id) == [requirements]


def test_claim_is_atomic_and_attempt_history_survives_reopen(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    session = create_session(store)
    task = store.create_task(
        session.id,
        phase="implementation",
        kind=BuildTaskKind.COMMAND,
        title="Run tests",
        idempotency_key="tests",
        max_attempts=2,
    )

    run = store.claim_task(
        task.id,
        worker_id="worker-1",
        backend="host",
        command=("python", "-m", "pytest"),
        pid=1234,
    )

    with pytest.raises(ValueError, match="not claimable"):
        store.claim_task(task.id, worker_id="worker-2")

    reopened = make_store(tmp_path)
    restored = reopened.get_task_run(run.id)

    assert restored is not None
    assert restored.command == ("python", "-m", "pytest")
    assert restored.pid == 1234
    assert restored.attempt_number == 1
    assert reopened.get_task(task.id).status is BuildTaskStatus.RUNNING


def test_artifacts_and_terminal_run_results_round_trip(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    session = create_session(store)
    task = store.create_task(
        session.id,
        phase="verification",
        kind=BuildTaskKind.VERIFICATION,
        title="Capture browser evidence",
        idempotency_key="browser-evidence",
    )
    run = store.claim_task(task.id, worker_id="worker-1", backend="container")
    artifact = store.create_artifact(
        session.id,
        task_id=task.id,
        run_id=run.id,
        kind="screenshot",
        uri="artifacts/home.png",
        metadata={"viewport": "1440x900"},
    )
    finished = store.finish_task_run(
        run.id,
        status=BuildTaskStatus.SUCCEEDED,
        result={"passed": True},
        log_path="logs/browser.log",
        artifact_ids=(artifact.id,),
    )

    assert finished.result == {"passed": True}
    assert finished.log_path == "logs/browser.log"
    assert finished.artifact_ids == (artifact.id,)
    assert store.list_artifacts(session.id) == [artifact]


def test_session_pause_resume_and_cancel_are_durable(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    session = create_session(store)
    task = store.create_task(
        session.id,
        phase="planning",
        kind=BuildTaskKind.AGENT,
        title="Plan implementation",
        idempotency_key="plan",
    )

    assert store.pause_session(session.id).status == "paused"
    assert store.ready_tasks(session.id) == []
    assert store.resume_session(session.id).status == "active"
    assert store.ready_tasks(session.id) == [task]

    cancelled = store.cancel_session(session.id)
    assert cancelled.status == "cancelled"
    assert store.get_task(task.id).status is BuildTaskStatus.CANCELLED
    assert make_store(tmp_path).get_session(session.id).status == "cancelled"


def test_restart_recovery_requeues_retryable_runs_and_fails_exhausted_runs(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    session = create_session(store)
    retryable = store.create_task(
        session.id,
        phase="implementation",
        kind=BuildTaskKind.COMMAND,
        title="Retry after restart",
        idempotency_key="retryable",
        max_attempts=2,
    )
    exhausted = store.create_task(
        session.id,
        phase="implementation",
        kind=BuildTaskKind.COMMAND,
        title="Fail after restart",
        idempotency_key="exhausted",
        max_attempts=1,
    )
    retry_run = store.claim_task(retryable.id, worker_id="old-worker")
    exhausted_run = store.claim_task(exhausted.id, worker_id="old-worker")

    recovered = make_store(tmp_path).recover_interrupted_runs()

    assert {item.id for item in recovered} == {retry_run.id, exhausted_run.id}
    assert all(item.status is BuildTaskStatus.INTERRUPTED for item in recovered)
    reopened = make_store(tmp_path)
    assert reopened.get_task(retryable.id).status is BuildTaskStatus.PENDING
    assert reopened.get_task(exhausted.id).status is BuildTaskStatus.FAILED


def test_invalid_dependencies_are_rejected(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first_session = create_session(store)
    second_session = create_session(store)
    foreign = store.create_task(
        first_session.id,
        phase="planning",
        kind=BuildTaskKind.AGENT,
        title="Foreign task",
        idempotency_key="foreign",
    )

    with pytest.raises(ValueError, match="same build session"):
        store.create_task(
            second_session.id,
            phase="implementation",
            kind=BuildTaskKind.AGENT,
            title="Invalid dependency",
            dependencies=(foreign.id,),
            idempotency_key="invalid",
        )


def test_migration_keeps_existing_databases_usable(tmp_path: Path) -> None:
    database_path = tmp_path / "kernel.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('version', '29')"
        )

    database = KernelDatabase(database_path)
    database.migrate()

    assert database.schema_version() == 30
