"""Project-boundary validation for governed build sessions."""

from __future__ import annotations

from pathlib import Path


class BuildWorkspacePolicy:
    """Confine builds to one project below the configured workspace container."""

    def __init__(self, workspace_root: str | Path):
        self.workspace_root = Path(workspace_root).expanduser().resolve()

    def validate(self, workspace: str | Path) -> Path:
        candidate = Path(workspace).expanduser().resolve()
        if not candidate.exists() or not candidate.is_dir():
            raise ValueError(f"Build workspace is not a directory: {candidate}")
        try:
            candidate.relative_to(self.workspace_root)
        except ValueError as exc:
            raise ValueError(
                f"Build workspace is outside the configured build workspace root: {candidate}"
            ) from exc
        if candidate == self.workspace_root and not (candidate / ".git").is_dir():
            raise ValueError(
                "Select a project directory below the configured build workspace root; "
                "the root itself is a project container, not a repository."
            )
        return candidate
