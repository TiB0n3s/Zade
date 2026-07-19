"""Project-boundary validation for governed build sessions."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


class BuildWorkspacePolicy:
    """Confine builds to one project below the configured workspace container."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        registered_project_predicate: Callable[[Path], bool] | None = None,
    ):
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self._registered_project_predicate = registered_project_predicate

    def validate(self, workspace: str | Path) -> Path:
        requested = Path(workspace).expanduser()
        requested_is_symlink = requested.is_symlink()
        candidate = requested.resolve()
        if not candidate.exists() or not candidate.is_dir():
            raise ValueError(f"Build workspace is not a directory: {candidate}")
        try:
            candidate.relative_to(self.workspace_root)
        except ValueError as exc:
            raise ValueError(
                f"Build workspace is outside the configured build workspace root: {candidate}"
            ) from exc
        if self._registered_project_predicate is not None:
            if requested_is_symlink:
                raise ValueError(f"Build workspace cannot be a symbolic link: {requested}")
            if candidate == self.workspace_root or candidate.parent != self.workspace_root:
                raise ValueError(
                    "Select a registered direct-child project root below the configured "
                    f"build workspace root: {candidate}"
                )
            if not self._registered_project_predicate(candidate):
                raise ValueError(f"Build workspace is not registered for project intake: {candidate}")
            return candidate
        if candidate == self.workspace_root and not (candidate / ".git").is_dir():
            raise ValueError(
                "Select a project directory below the configured build workspace root; "
                "the root itself is a project container, not a repository."
            )
        return candidate
