from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .common import brief, category, code, table, unavailable


def render_capabilities(tools: Sequence[Mapping[str, Any]]) -> str:
    try:
        rows = []
        for tool in sorted(tools, key=lambda item: str(item.get("name", ""))):
            name = str(tool.get("name") or "").strip()
            if not name:
                continue
            rows.append(
                [
                    code(name),
                    category(name),
                    code(tool.get("permission_tier", "")),
                    brief(tool.get("description")),
                ]
            )
        return table(["Name", "Category", "Permission", "Description"], rows)
    except Exception as exc:
        return unavailable("capabilities", str(exc))
