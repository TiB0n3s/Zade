from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .common import code, unavailable


def render_recent_activity(commits: Sequence[Mapping[str, Any]]) -> str:
    try:
        lines = []
        for commit in commits:
            commit_hash = str(commit.get("hash") or "").strip()
            date = str(commit.get("date") or "").strip()
            subject = str(commit.get("subject") or "").strip()
            if not commit_hash or not subject:
                continue
            lines.append(f"- {code(commit_hash)} {date} - {subject}".strip())
        return "\n".join(lines) if lines else "_No recent commits reported._"
    except Exception as exc:
        return unavailable("recent-activity", str(exc))
