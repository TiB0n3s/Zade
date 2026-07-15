from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping


_START_RE = re.compile(r"^\s*<!--\s*AUTO-START:\s*([A-Za-z0-9_.-]+)\s*-->\s*$")
_END_RE = re.compile(r"^\s*<!--\s*AUTO-END:\s*([A-Za-z0-9_.-]+)\s*-->\s*$")


class AutoBlockParseError(ValueError):
    """Raised when AUTO block markers are missing, nested, or mismatched."""


@dataclass(frozen=True)
class AutoBlock:
    name: str
    start_line: int
    content_start_line: int
    content_end_line: int
    end_line: int
    newline: str


@dataclass(frozen=True)
class AutoBlockDocument:
    text: str
    lines: tuple[str, ...]
    blocks: tuple[AutoBlock, ...]

    def serialize(self) -> str:
        return "".join(self.lines)


def parse_auto_blocks(text: str) -> AutoBlockDocument:
    lines = tuple(text.splitlines(keepends=True))
    blocks: list[AutoBlock] = []
    active_name: str | None = None
    active_start = -1
    active_newline = "\n"

    for index, line in enumerate(lines):
        marker_text = line.rstrip("\r\n")
        start = _START_RE.match(marker_text)
        if start:
            if active_name is not None:
                raise AutoBlockParseError(
                    f"Nested AUTO block '{start.group(1)}' inside '{active_name}'."
                )
            active_name = start.group(1)
            active_start = index
            active_newline = _line_ending(line)
            continue

        end = _END_RE.match(marker_text)
        if not end:
            continue
        if active_name is None:
            raise AutoBlockParseError(f"AUTO-END for '{end.group(1)}' has no matching AUTO-START.")
        end_name = end.group(1)
        if end_name != active_name:
            raise AutoBlockParseError(f"AUTO block '{active_name}' ended by AUTO-END for '{end_name}'.")
        blocks.append(
            AutoBlock(
                name=active_name,
                start_line=active_start,
                content_start_line=active_start + 1,
                content_end_line=index,
                end_line=index,
                newline=active_newline,
            )
        )
        active_name = None
        active_start = -1

    if active_name is not None:
        raise AutoBlockParseError(f"AUTO-START for '{active_name}' has no matching AUTO-END.")
    return AutoBlockDocument(text=text, lines=lines, blocks=tuple(blocks))


def render_auto_blocks(text: str, replacements: Mapping[str, str]) -> str:
    document = parse_auto_blocks(text)
    lines = list(document.lines)
    for block in reversed(document.blocks):
        if block.name not in replacements:
            continue
        lines[block.content_start_line : block.content_end_line] = _render_body_lines(
            replacements[block.name],
            newline=block.newline,
        )
    return "".join(lines)


def _line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    if line.endswith("\r"):
        return "\r"
    return "\n"


def _render_body_lines(markdown: str, *, newline: str) -> list[str]:
    body = markdown.strip("\r\n")
    if not body:
        return []
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    return [f"{line}{newline}" for line in normalized.split("\n")]
