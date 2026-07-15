from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .common import brief, category, code, table, unavailable, yes_no


def render_action_handlers(handlers: Sequence[Mapping[str, Any]]) -> str:
    try:
        rows = []
        for handler in sorted(handlers, key=lambda item: str(item.get("action", ""))):
            action = str(handler.get("action") or "").strip()
            if not action:
                continue
            rows.append(
                [
                    code(action),
                    category(action),
                    yes_no(handler.get("enabled", False)),
                    brief(handler.get("description")),
                ]
            )
        return table(["Action", "Category", "Enabled", "Description"], rows)
    except Exception as exc:
        return unavailable("action-handlers", str(exc))
