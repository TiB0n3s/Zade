from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .common import brief, code, table, unavailable, yes_no


def render_skills(snapshot: Mapping[str, Any]) -> str:
    try:
        summary = snapshot.get("summary") or {}
        total = int(summary.get("total", 0) or 0)
        enabled = int(summary.get("enabled", 0) or 0)
        lines = [f"- Registered skills: {total} total, {enabled} enabled."]
        by_risk = summary.get("by_risk_tier") or {}
        if isinstance(by_risk, Mapping) and by_risk:
            risk_parts = [f"{key}={by_risk[key]}" for key in sorted(by_risk)]
            lines.append("- Risk tiers: " + ", ".join(risk_parts) + ".")
        items = snapshot.get("items") or []
        rows = []
        for item in sorted(items, key=lambda entry: str(entry.get("name", ""))):
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            rows.append(
                [
                    code(name),
                    yes_no(item.get("enabled", False)),
                    brief(item.get("description")),
                ]
            )
        if rows:
            lines.append(table(["Name", "Enabled", "Description"], rows))
        return "\n".join(lines)
    except Exception as exc:
        return unavailable("skills", str(exc))
