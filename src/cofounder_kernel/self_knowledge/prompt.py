from __future__ import annotations

import os
from pathlib import Path
import re

from .parser import parse_auto_blocks
from .snapshot import DEFAULT_DOC_PATH


PROMPT_MODE_ENV = "ZADE_SELF_KNOWLEDGE_PROMPT_MODE"
PROMPT_DOC_ENV = "ZADE_SELF_KNOWLEDGE_DOC"
PROMPT_MODES = frozenset({"slim", "full", "runtime", "off"})


def prompt_self_knowledge_mode() -> str:
    mode = os.getenv(PROMPT_MODE_ENV, "slim").strip().lower()
    return mode if mode in PROMPT_MODES else "slim"


def prompt_self_knowledge_doc_path() -> Path:
    configured = os.getenv(PROMPT_DOC_ENV, "").strip()
    return Path(configured) if configured else DEFAULT_DOC_PATH


def render_prompt_self_knowledge(*, doc_path: Path | None = None, mode: str | None = None) -> str:
    selected_mode = _normalize_mode(mode)
    if selected_mode in {"off", "runtime"}:
        return ""
    path = Path(doc_path) if doc_path is not None else prompt_self_knowledge_doc_path()
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not text:
        return ""
    if selected_mode == "full":
        return text
    return render_slim_self_knowledge(text)


def render_slim_self_knowledge(text: str) -> str:
    sections = _sections(text)
    identity = _flatten(sections.get("Identity", ""))
    principles = _bullets(sections.get("Core Principles", ""), limit=6)
    capabilities = _auto_names(text, "capabilities", limit=40)
    actions = _auto_names(text, "action-handlers", limit=40)

    lines = ["Living self-knowledge summary (from context/self/zade.md)."]
    if identity:
        lines.append(f"Identity: {_clip(identity, 700)}")
    if principles:
        lines.append("Core principles:")
        lines.extend(f"- {principle}" for principle in principles)
    if capabilities:
        lines.append(f"Capabilities: {', '.join(capabilities)}")
    if actions:
        lines.append(f"Approved actions: {', '.join(actions)}")
    return "\n".join(lines)


def _normalize_mode(mode: str | None) -> str:
    if mode is None:
        return prompt_self_knowledge_mode()
    normalized = mode.strip().lower()
    return normalized if normalized in PROMPT_MODES else "slim"


def _sections(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line.removeprefix("## ").strip()
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return {name: "\n".join(lines).strip() for name, lines in sections.items()}


def _bullets(text: str, *, limit: int) -> list[str]:
    bullets: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        bullets.append(_clip(stripped[2:].strip(), 180))
        if len(bullets) >= limit:
            break
    return bullets


def _auto_names(text: str, block_name: str, *, limit: int) -> list[str]:
    try:
        document = parse_auto_blocks(text)
    except Exception:
        return []
    names: list[str] = []
    for block in document.blocks:
        if block.name != block_name:
            continue
        content = "".join(document.lines[block.content_start_line : block.content_end_line])
        for line in content.splitlines():
            if not line.lstrip().startswith("|"):
                continue
            match = re.search(r"`([^`]+)`", line)
            if not match:
                continue
            name = match.group(1).strip()
            if name and name not in names:
                names.append(name)
            if len(names) >= limit:
                return names
    return names


def _flatten(text: str) -> str:
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("<!--")
    ]
    return " ".join(lines)


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: max(0, limit - 3)].rstrip() + "..."
