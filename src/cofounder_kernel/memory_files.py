"""Memory as human-editable markdown files (Tier 6).

Each durable memory is one file with a small frontmatter block and a prose body.
Frontmatter values are stored as JSON so titles, sources, and unicode never break
the parser, and there is no YAML dependency. These files are the *source of truth*;
the SQLite `memories`/`memory_fts`/`memory_embeddings` tables are a derived index
that can be wiped and rebuilt from the files at any time.

File shape:

    ---
    id: 8
    kind: "company_manifesto"
    title: "Dead Star Labs Manifesto"
    source: "founder:chat:2026-07-12"
    created_at: "2026-07-12T15:27:45+00:00"
    metadata: {"category": "..."}
    ---

    <the memory body — the fact, and for feedback/project memories why it matters
    and how to apply it>
"""
from __future__ import annotations

import json
import re
from typing import Any

_FRONTMATTER_KEYS = ("id", "kind", "title", "source", "created_at")


def slugify(text: str, max_len: int = 50) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    slug = slug[:max_len].strip("-")
    return slug or "note"


def memory_filename(memory: dict[str, Any]) -> str:
    """A stable, human-browsable filename: zero-padded id + a title slug."""
    slug = slugify(str(memory.get("title") or memory.get("kind") or "note"))
    mid = memory.get("id")
    prefix = f"{int(mid):05d}-" if mid is not None else "new-"
    return f"{prefix}{slug}.md"


def serialize_memory(memory: dict[str, Any]) -> str:
    lines = ["---"]
    for key in _FRONTMATTER_KEYS:
        lines.append(f"{key}: {json.dumps(memory.get(key), ensure_ascii=False)}")
    metadata = memory.get("metadata") or {}
    if metadata:
        lines.append(f"metadata: {json.dumps(metadata, ensure_ascii=False, sort_keys=True)}")
    lines.append("---")
    lines.append("")
    lines.append((memory.get("content") or "").strip())
    lines.append("")
    return "\n".join(lines)


def parse_memory_file(text: str) -> dict[str, Any] | None:
    """Parse a memory file back into a dict, or None if it isn't one."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    frontmatter: dict[str, Any] = {}
    i = 1
    while i < len(lines) and lines[i].strip() != "---":
        key, sep, raw = lines[i].partition(":")
        if sep:
            key = key.strip()
            raw = raw.strip()
            try:
                frontmatter[key] = json.loads(raw)
            except Exception:
                frontmatter[key] = raw
        i += 1
    body = "\n".join(lines[i + 1:]).strip() if i < len(lines) else ""
    metadata = frontmatter.get("metadata")
    return {
        "id": frontmatter.get("id"),
        "kind": str(frontmatter.get("kind") or "note"),
        "title": str(frontmatter.get("title") or ""),
        "source": str(frontmatter.get("source") or "local"),
        "created_at": str(frontmatter.get("created_at") or ""),
        "metadata": metadata if isinstance(metadata, dict) else {},
        "content": body,
    }


def render_index(entries: list[dict[str, Any]]) -> str:
    """A browsable index of memory hooks. Derived from the files; not authoritative."""
    ordered = sorted(entries, key=lambda m: (m.get("id") is None, m.get("id") or 0))
    lines = [
        "# Zade Memory — Index",
        "",
        f"{len(ordered)} memories. **These files are the source of truth.** The SQLite "
        "index (search + embeddings) is derived and can be rebuilt from them at any time "
        "(`POST /memory/rebuild-from-files`). Edit or delete a file by hand, then rebuild.",
        "",
    ]
    for entry in ordered:
        mid = entry.get("id")
        tag = f"#{mid}" if mid is not None else "#(new)"
        lines.append(f"- **{tag}** [{entry.get('kind')}] {entry.get('title')}  ·  `{entry.get('filename')}`")
    lines.append("")
    return "\n".join(lines)
