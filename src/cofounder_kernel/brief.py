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

    approval_pressure = inputs.get("approval_pressure", {})
    if approval_pressure.get("has_blockers"):
        lines.append("Approval blockers:")
        lines.append(f"- {approval_pressure['headline']}")
        for item in approval_pressure.get("items", [])[:3]:
            lines.append(f"- #{item['id']} {item['title']} [{item['permission_tier']}]")
        lines.append(f"Approval next action: {approval_pressure['next_action']}")
    else:
        lines.append("No approval blockers.")

    if inputs["recent_memories"]:
        lines.append("Recent memory:")
        lines.extend(f"- [{row['kind']}] {row['title']}" for row in inputs["recent_memories"])
    else:
        lines.append("No memory records yet.")

    if inputs["recent_disagreements"]:
        lines.append("Recent disagreements:")
        lines.extend(f"- {row['topic']}: {row['position']}" for row in inputs["recent_disagreements"])

    return {"generated_at": utc_now(), "brief": "\n".join(lines), "inputs": inputs}
