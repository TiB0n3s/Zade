from __future__ import annotations

from .db import KernelDatabase, utc_now


def build_daily_brief(db: KernelDatabase) -> dict:
    inputs = db.daily_brief_inputs()
    lines: list[str] = [f"Local co-founder brief generated at {utc_now()}."]

    if inputs["active_goals"]:
        lines.append("Active goals:")
        lines.extend(f"- {row['title']}" for row in inputs["active_goals"])
    else:
        lines.append("No active goals recorded yet.")

    if inputs["open_decisions"]:
        lines.append("Open decisions:")
        lines.extend(f"- {row['title']}" for row in inputs["open_decisions"])
    else:
        lines.append("No open decisions recorded yet.")

    if inputs["recent_memories"]:
        lines.append("Recent memory:")
        lines.extend(f"- [{row['kind']}] {row['title']}" for row in inputs["recent_memories"])
    else:
        lines.append("No memory records yet.")

    if inputs["recent_disagreements"]:
        lines.append("Recent disagreements:")
        lines.extend(f"- {row['topic']}: {row['position']}" for row in inputs["recent_disagreements"])

    return {"generated_at": utc_now(), "brief": "\n".join(lines), "inputs": inputs}

