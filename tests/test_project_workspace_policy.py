from pathlib import Path

import pytest

from cofounder_kernel.build_workspace import BuildWorkspacePolicy


def test_workspace_policy_preserves_container_behavior_without_registry(tmp_path: Path) -> None:
    intake_root = tmp_path / "project-intake"
    nested = intake_root / "Project" / "src"
    nested.mkdir(parents=True)

    policy = BuildWorkspacePolicy(intake_root)

    assert policy.validate(nested) == nested.resolve()


def test_registered_policy_accepts_registered_direct_child(tmp_path: Path) -> None:
    intake_root = tmp_path / "project-intake"
    project = intake_root / "Same Ground"
    project.mkdir(parents=True)
    registered = {project.resolve()}
    policy = BuildWorkspacePolicy(
        intake_root,
        registered_project_predicate=lambda candidate: candidate in registered,
    )

    assert policy.validate(project) == project.resolve()


def test_registered_policy_rejects_intake_root(tmp_path: Path) -> None:
    intake_root = tmp_path / "project-intake"
    intake_root.mkdir()
    policy = BuildWorkspacePolicy(
        intake_root,
        registered_project_predicate=lambda _candidate: True,
    )

    with pytest.raises(ValueError, match="registered direct-child project root"):
        policy.validate(intake_root)


def test_registered_policy_rejects_nested_project_path(tmp_path: Path) -> None:
    intake_root = tmp_path / "project-intake"
    nested = intake_root / "Same Ground" / "node_modules" / "dependency"
    nested.mkdir(parents=True)
    policy = BuildWorkspacePolicy(
        intake_root,
        registered_project_predicate=lambda _candidate: True,
    )

    with pytest.raises(ValueError, match="registered direct-child project root"):
        policy.validate(nested)


def test_registered_policy_rejects_unregistered_sibling(tmp_path: Path) -> None:
    intake_root = tmp_path / "project-intake"
    registered = intake_root / "Same Ground"
    unregistered = intake_root / "Unregistered"
    registered.mkdir(parents=True)
    unregistered.mkdir()
    policy = BuildWorkspacePolicy(
        intake_root,
        registered_project_predicate=lambda candidate: candidate == registered.resolve(),
    )

    with pytest.raises(ValueError, match="not registered"):
        policy.validate(unregistered)


def test_registered_policy_rejects_symlink_alias(tmp_path: Path, monkeypatch) -> None:
    intake_root = tmp_path / "project-intake"
    registered = intake_root / "Same Ground"
    alias = intake_root / "Same Ground Alias"
    registered.mkdir(parents=True)
    try:
        alias.symlink_to(registered, target_is_directory=True)
    except OSError:
        alias.mkdir()
        original_is_symlink = Path.is_symlink
        monkeypatch.setattr(
            Path,
            "is_symlink",
            lambda candidate: candidate == alias or original_is_symlink(candidate),
        )
    policy = BuildWorkspacePolicy(
        intake_root,
        registered_project_predicate=lambda candidate: candidate == registered.resolve(),
    )

    with pytest.raises(ValueError, match="symbolic link"):
        policy.validate(alias)
