from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cofounder_kernel.build_store import BuildStore
from cofounder_kernel.build_types import BuildAssessment, BuildTier, LeaseLimits
from cofounder_kernel.db import KernelDatabase, SCHEMA_VERSION


SMALL_LIMITS = LeaseLimits(
    dollar_micro=1_000_000,
    input_tokens=120_000,
    output_tokens=16_000,
    cloud_turns=6,
    duration_seconds=7200,
)


def sample_assessment() -> BuildAssessment:
    return BuildAssessment(
        id=None,
        task="Build an export",
        acceptance="Integration tests pass",
        workspace="C:/workspace",
        repo_fingerprint="fingerprint-1",
        deterministic_score=22,
        local_adjustment=3,
        final_score=25,
        confidence=0.85,
        recommended_tier=BuildTier.SMALL,
        dimensions={"product_surfaces": 4, "change_breadth": 3},
        floor_rules=(),
        evidence={"file_count": 12, "frameworks": ["fastapi"]},
        unknowns=("Rollback behavior",),
        local_work=("Edit locally",),
        cloud_reasons=("Review API boundary",),
        created_at="2026-07-18T12:00:00+00:00",
    )


def make_store(path: Path) -> BuildStore:
    database = KernelDatabase(path / "kernel.sqlite")
    database.migrate()
    return BuildStore(database)


def test_migration_creates_build_budget_tables(tmp_path: Path) -> None:
    database = KernelDatabase(tmp_path / "kernel.sqlite")
    database.migrate()

    with database.connect() as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert SCHEMA_VERSION == 32
    assert {
        "build_assessments",
        "build_sessions",
        "build_leases",
        "cloud_usage_events",
    } <= names


def test_session_and_lease_survive_reopen(tmp_path: Path) -> None:
    first = make_store(tmp_path)
    session = first.create_session(sample_assessment())
    lease = first.create_lease(
        session.id,
        BuildTier.SMALL,
        SMALL_LIMITS,
        provider="anthropic",
        model="claude-opus-4-8",
        approval_request_id=7,
    )

    second = make_store(tmp_path)
    restored_session = second.get_session(session.id)
    restored_lease = second.get_active_lease(session.id)

    assert restored_session is not None
    assert restored_session.id == session.id
    assert restored_session.checkpoint == {}
    assert restored_lease is not None
    assert restored_lease.id == lease.id
    assert restored_lease.limits == SMALL_LIMITS
    assert restored_lease.provider == "anthropic"


def test_assessment_and_checkpoint_json_round_trip(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    stored_assessment = store.create_assessment(sample_assessment())
    session = store.create_session(stored_assessment)

    updated = store.checkpoint(
        session.id,
        phase="planning",
        checkpoint={"b": [2, 1], "a": {"done": True}},
    )
    restored = store.get_assessment(stored_assessment.id or 0)

    assert updated.phase == "planning"
    assert updated.checkpoint == {"a": {"done": True}, "b": [2, 1]}
    assert restored is not None
    assert restored.evidence == sample_assessment().evidence
    assert restored.unknowns == sample_assessment().unknowns


def test_session_counts_are_not_limited_by_recent_listing(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = store.create_session(sample_assessment())
    store.create_session(sample_assessment())
    store.checkpoint(first.id, phase="complete", checkpoint={"done": True})

    assert len(store.list_sessions(limit=1)) == 1
    assert store.count_sessions() == 2
    assert store.count_sessions(status="active") == 1


def test_only_one_active_lease_can_exist_for_a_session(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    session = store.create_session(sample_assessment())
    store.create_lease(
        session.id,
        BuildTier.SMALL,
        SMALL_LIMITS,
        provider="anthropic",
        model="claude-opus-4-8",
        approval_request_id=7,
    )

    with pytest.raises(sqlite3.IntegrityError):
        store.create_lease(
            session.id,
            BuildTier.SMALL,
            SMALL_LIMITS,
            provider="anthropic",
            model="claude-opus-4-8",
            approval_request_id=8,
        )


def test_migration_is_idempotent_and_usage_starts_empty(tmp_path: Path) -> None:
    database = KernelDatabase(tmp_path / "kernel.sqlite")
    database.migrate()
    database.migrate()
    store = BuildStore(database)
    session = store.create_session(sample_assessment())
    lease = store.create_lease(
        session.id,
        BuildTier.SMALL,
        SMALL_LIMITS,
        provider="anthropic",
        model="claude-opus-4-8",
        approval_request_id=7,
    )

    assert database.schema_version() == SCHEMA_VERSION
    assert store.list_usage(lease.id) == []
