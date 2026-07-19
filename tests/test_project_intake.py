import sqlite3
from pathlib import Path

import pytest

from cofounder_kernel.config import KernelConfig, PathConfig, ensure_local_paths, load_config
from cofounder_kernel.db import KernelDatabase


@pytest.fixture
def db(tmp_path: Path) -> KernelDatabase:
    database = KernelDatabase(tmp_path / "kernel.sqlite")
    database.migrate()
    return database


def test_project_intake_defaults_below_hot_root(tmp_path: Path) -> None:
    cfg = KernelConfig(paths=PathConfig(hot_root=tmp_path / "brain"))

    assert cfg.paths.project_intake_dir == tmp_path / "brain" / "project-intake"


def test_project_intake_config_loads_from_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[project_intake]
enabled = true
scaffold_on_intake = true
watcher_debounce_seconds = 7
""",
        encoding="utf-8",
    )

    cfg = load_config(config_path)

    assert cfg.project_intake.enabled is True
    assert cfg.project_intake.scaffold_on_intake is True
    assert cfg.project_intake.watcher_debounce_seconds == 7


def test_ensure_local_paths_creates_project_intake_root(tmp_path: Path) -> None:
    cfg = KernelConfig(
        paths=PathConfig(
            hot_root=tmp_path / "brain",
            cold_root=tmp_path / "cold",
            data_dir=tmp_path / "data",
        )
    )

    ensure_local_paths(cfg)

    assert cfg.paths.project_intake_dir.is_dir()


def test_project_record_round_trip(db: KernelDatabase, tmp_path: Path) -> None:
    project_path = tmp_path / "Same Ground"
    project_id = db.upsert_project(
        canonical_path=str(project_path),
        name="Same Ground",
        product_type="mobile_application",
        distribution_targets=["google_play", "apple_app_store_eventual"],
        lifecycle_state="discovered",
        repo_fingerprint="docs-only",
        metadata={"scaffold_on_intake": True},
    )

    project = db.get_project(project_id)

    assert project is not None
    assert project["name"] == "Same Ground"
    assert project["canonical_path"] == str(project_path.resolve())
    assert project["product_type"] == "mobile_application"
    assert project["distribution_targets"] == ["google_play", "apple_app_store_eventual"]
    assert project["lifecycle_state"] == "discovered"
    assert project["repo_fingerprint"] == "docs-only"
    assert project["metadata"] == {"scaffold_on_intake": True}


def test_project_upsert_reuses_canonical_path_and_supports_lookup(
    db: KernelDatabase, tmp_path: Path
) -> None:
    project_path = tmp_path / "The Dark Index"
    first_id = db.upsert_project(
        canonical_path=str(project_path),
        name="The Dark Index",
        product_type="mobile_application",
        distribution_targets=["google_play", "apple_app_store_eventual"],
        lifecycle_state="discovered",
        repo_fingerprint="initial",
        metadata={"source": "manifest"},
    )
    second_id = db.upsert_project(
        canonical_path=str(project_path),
        name="The Dark Index",
        product_type="mobile_application",
        distribution_targets=["google_play", "apple_app_store_eventual"],
        lifecycle_state="intake",
        repo_fingerprint="refreshed",
        metadata={"source": "git"},
    )

    found = db.find_project_by_path(str(project_path))
    projects = db.list_projects()

    assert second_id == first_id
    assert found is not None
    assert found["id"] == first_id
    assert found["lifecycle_state"] == "intake"
    assert found["repo_fingerprint"] == "refreshed"
    assert found["metadata"] == {"source": "git"}
    assert [project["id"] for project in projects] == [first_id]


def test_project_event_is_appended_with_json_payload(db: KernelDatabase, tmp_path: Path) -> None:
    project_id = db.upsert_project(
        canonical_path=str(tmp_path / "Same Ground"),
        name="Same Ground",
        product_type="mobile_application",
        distribution_targets=["google_play", "apple_app_store_eventual"],
        lifecycle_state="discovered",
        repo_fingerprint="docs-only",
    )

    first_event_id = db.append_project_event(
        project_id,
        event_type="discovered",
        detail="Project manifest detected.",
        metadata={"intake_source": True},
    )
    second_event_id = db.append_project_event(
        project_id,
        event_type="documentation_ingested",
        metadata={"documents": 3},
    )

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM project_events WHERE project_id = ? ORDER BY id", (project_id,)
        ).fetchall()

    assert second_event_id > first_event_id
    assert [row["event_type"] for row in rows] == ["discovered", "documentation_ingested"]
    assert rows[0]["detail"] == "Project manifest detected."
    assert rows[0]["metadata_json"] == '{"intake_source": true}'
    assert rows[1]["metadata_json"] == '{"documents": 3}'


def test_project_events_remain_append_only_when_project_delete_is_attempted(
    db: KernelDatabase, tmp_path: Path
) -> None:
    project_id = db.upsert_project(
        canonical_path=str(tmp_path / "Same Ground"),
        name="Same Ground",
        product_type="mobile_application",
        distribution_targets=["google_play", "apple_app_store_eventual"],
        lifecycle_state="discovered",
        repo_fingerprint="docs-only",
    )
    event_id = db.append_project_event(project_id, event_type="discovered")

    with pytest.raises(sqlite3.IntegrityError):
        with db.connect() as conn:
            conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))

    with db.connect() as conn:
        event = conn.execute("SELECT id FROM project_events WHERE id = ?", (event_id,)).fetchone()

    assert event is not None
