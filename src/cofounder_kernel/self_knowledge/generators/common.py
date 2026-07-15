from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any


def category(name: str) -> str:
    value = str(name or "").strip()
    if "." in value:
        return value.split(".", 1)[0]
    if "-" in value:
        return value.split("-", 1)[0]
    return value or "unknown"


def code(value: Any) -> str:
    return f"`{str(value)}`"


def brief(value: Any, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def yes_no(value: Any) -> str:
    return "yes" if bool(value) else "no"


def table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    rendered_rows = [list(row) for row in rows]
    if not rendered_rows:
        return "_None reported._"
    lines = [
        "| " + " | ".join(_cell(header) for header in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rendered_rows:
        lines.append("| " + " | ".join(_cell(item) for item in row) + " |")
    return "\n".join(lines)


def unavailable(block_name: str, reason: str = "") -> str:
    detail = f": {reason}" if reason else ""
    return f"_unavailable, regenerate manually ({block_name}{detail})_"


def _cell(value: Any) -> str:
    text = str(value)
    return text.replace("\n", " ").replace("|", "\\|").strip()
