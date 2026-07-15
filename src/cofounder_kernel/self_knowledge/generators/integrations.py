from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .common import brief, code, table, unavailable


def render_integrations(integrations: Sequence[Mapping[str, Any]]) -> str:
    try:
        rows = []
        for integration in sorted(integrations, key=lambda item: str(item.get("name", ""))):
            name = str(integration.get("name") or "").strip()
            if not name:
                continue
            rows.append(
                [
                    name,
                    str(integration.get("mode") or "").strip(),
                    code(integration.get("source", "")),
                    brief(integration.get("summary")),
                ]
            )
        return table(["Name", "Mode", "Source", "Summary"], rows)
    except Exception as exc:
        return unavailable("integrations", str(exc))
