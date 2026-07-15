from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from .parser import parse_auto_blocks


DEFAULT_ALLOWLIST_PATH = Path("context/self/.zade-allowlist.txt")

_PATH_RE = re.compile(
    r"(?<![\w:/\\])"
    r"(?:[A-Za-z]:[\\/])?"
    r"(?:[A-Za-z0-9_.-]+[\\/])+[A-Za-z0-9_.-]+"
    r"(?![\w/\\.-])"
)
_QUALIFIED_RE = re.compile(
    r"(?<![\w/\\-])"
    r"(?:[A-Za-z_][A-Za-z0-9_]{1,}\.)+[A-Za-z_][A-Za-z0-9_]{1,}(?:\(\))?"
    r"(?![\w/\\-])"
)
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")


@dataclass(frozen=True)
class DriftFinding:
    kind: str
    reference: str
    location_in_doc: str
    reason: str


@dataclass(frozen=True)
class _StaticIndex:
    modules: frozenset[str]
    module_roots: frozenset[str]
    symbols: frozenset[str]


@dataclass(frozen=True)
class _SnapshotReferences:
    tools: frozenset[str]
    integrations: frozenset[str]
    sub_agents: frozenset[str]


def check_self_knowledge_file(
    doc_path: Path,
    *,
    repo_root: Path | None = None,
    allowlist_path: Path | None = DEFAULT_ALLOWLIST_PATH,
    snapshots: Mapping[str, Any] | None = None,
) -> list[DriftFinding]:
    path = Path(doc_path)
    root = Path(repo_root) if repo_root is not None else Path.cwd()
    return check_self_knowledge_text(
        path.read_text(encoding="utf-8"),
        repo_root=root,
        allowlist_path=allowlist_path,
        snapshots=snapshots,
    )


def check_self_knowledge_text(
    text: str,
    *,
    repo_root: Path,
    allowlist_path: Path | None = DEFAULT_ALLOWLIST_PATH,
    snapshots: Mapping[str, Any] | None = None,
) -> list[DriftFinding]:
    root = Path(repo_root)
    allowlist = _read_allowlist(allowlist_path)
    static_index = _build_static_index(root)
    snapshot_refs = _snapshot_references(snapshots or {})
    findings: list[DriftFinding] = []
    seen: set[tuple[str, str, str]] = set()

    for line_number, line in _hand_written_lines(text):
        for candidate in _references_in_line(line, snapshot_refs, static_index):
            if _is_allowed(candidate.kind, candidate.reference, allowlist):
                continue
            finding = _validate_candidate(
                candidate,
                line_number=line_number,
                repo_root=root,
                static_index=static_index,
                snapshot_refs=snapshot_refs,
            )
            if finding is None:
                continue
            key = (finding.kind, finding.reference, finding.location_in_doc)
            if key in seen:
                continue
            seen.add(key)
            findings.append(finding)
    return findings


@dataclass(frozen=True)
class _Candidate:
    kind: str
    reference: str


def _hand_written_lines(text: str) -> Iterable[tuple[int, str]]:
    document = parse_auto_blocks(text)
    generated_lines: set[int] = set()
    for block in document.blocks:
        generated_lines.update(range(block.start_line, block.end_line + 1))
    for index, line in enumerate(document.lines):
        if index not in generated_lines:
            yield index + 1, line


def _references_in_line(
    line: str,
    snapshot_refs: _SnapshotReferences,
    static_index: _StaticIndex,
) -> list[_Candidate]:
    candidates: list[_Candidate] = []
    seen: set[tuple[str, str]] = set()
    path_spans: list[tuple[int, int]] = []

    def add(kind: str, reference: str) -> None:
        normalized = _normalize_reference(reference)
        if not normalized:
            return
        key = (kind, normalized)
        if key in seen:
            return
        seen.add(key)
        candidates.append(_Candidate(kind=kind, reference=normalized))

    for match in _PATH_RE.finditer(line):
        reference = _normalize_path_reference(match.group(0))
        if reference and not reference.startswith("/"):
            path_spans.append(match.span())
            add("file_path", reference)

    for match in _QUALIFIED_RE.finditer(line):
        if _overlaps(match.span(), path_spans):
            continue
        reference = _normalize_reference(match.group(0))
        if _is_tool_like(reference, snapshot_refs, static_index):
            add("tool", reference)
        else:
            add("qualified_symbol", _strip_call_suffix(reference))

    for match in _BACKTICK_RE.finditer(line):
        reference = _normalize_reference(match.group(1))
        if not reference or _is_route_reference(reference):
            continue
        path_reference = _normalize_path_reference(reference)
        if path_reference and not path_reference.startswith("/"):
            add("file_path", path_reference)
            continue
        if reference in snapshot_refs.tools:
            add("tool", reference)
            continue
        if reference in snapshot_refs.integrations:
            add("integration", reference)
            continue
        if reference in snapshot_refs.sub_agents:
            add("sub_agent", reference)
            continue
        if _looks_like_sub_agent_reference(reference):
            add("sub_agent", reference)
            continue
        if _looks_like_integration_reference(reference):
            add("integration", reference)
            continue
        if _looks_like_qualified_reference(reference):
            if _is_tool_like(reference, snapshot_refs, static_index):
                add("tool", reference)
            else:
                add("qualified_symbol", _strip_call_suffix(reference))

    return candidates


def _validate_candidate(
    candidate: _Candidate,
    *,
    line_number: int,
    repo_root: Path,
    static_index: _StaticIndex,
    snapshot_refs: _SnapshotReferences,
) -> DriftFinding | None:
    location = f"line {line_number}"
    if candidate.kind == "file_path":
        path = _resolve_path(repo_root, candidate.reference)
        if path.exists():
            return None
        return DriftFinding(
            kind="file_path",
            reference=candidate.reference,
            location_in_doc=location,
            reason="Path does not exist relative to repo root.",
        )
    if candidate.kind == "qualified_symbol":
        if candidate.reference in static_index.modules or candidate.reference in static_index.symbols:
            return None
        return DriftFinding(
            kind="qualified_symbol",
            reference=candidate.reference,
            location_in_doc=location,
            reason="No matching Python module, class, function, or method was found in src/.",
        )
    if candidate.kind == "tool":
        if candidate.reference in snapshot_refs.tools:
            return None
        return DriftFinding(
            kind="tool",
            reference=candidate.reference,
            location_in_doc=location,
            reason="No matching tool capability or approved action handler was found in generated snapshots.",
        )
    if candidate.kind == "integration":
        if candidate.reference in snapshot_refs.integrations:
            return None
        return DriftFinding(
            kind="integration",
            reference=candidate.reference,
            location_in_doc=location,
            reason="No matching integration name was found in generated snapshots.",
        )
    if candidate.kind == "sub_agent":
        if candidate.reference in snapshot_refs.sub_agents:
            return None
        return DriftFinding(
            kind="sub_agent",
            reference=candidate.reference,
            location_in_doc=location,
            reason="No matching sub-agent name was found in generated snapshots.",
        )
    return None


def _build_static_index(repo_root: Path) -> _StaticIndex:
    modules: set[str] = set()
    module_roots: set[str] = set()
    symbols: set[str] = set()
    src_root = repo_root / "src"
    if not src_root.exists():
        return _StaticIndex(frozenset(), frozenset(), frozenset())

    for path in src_root.rglob("*.py"):
        if not path.is_file():
            continue
        module_name = _module_name(src_root, path)
        modules.add(module_name)
        if module_name:
            module_roots.add(module_name.split(".", 1)[0])
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        _add_symbols(module_name, tree, symbols)

    return _StaticIndex(frozenset(modules), frozenset(module_roots), frozenset(symbols))


def _module_name(src_root: Path, path: Path) -> str:
    relative = path.relative_to(src_root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _add_symbols(module_name: str, tree: ast.Module, symbols: set[str]) -> None:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.add(node.name)
            symbols.add(f"{module_name}.{node.name}")
        elif isinstance(node, ast.ClassDef):
            symbols.add(node.name)
            symbols.add(f"{module_name}.{node.name}")
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.add(f"{node.name}.{child.name}")
                    symbols.add(f"{module_name}.{node.name}.{child.name}")


def _snapshot_references(snapshots: Mapping[str, Any]) -> _SnapshotReferences:
    tools: set[str] = set()
    integrations: set[str] = set()
    sub_agents: set[str] = set()

    for item in _items(snapshots.get("capabilities")):
        _add_if_string(tools, item.get("name"))
    for item in _items(snapshots.get("action-handlers")):
        _add_if_string(tools, item.get("action"))
    for item in _items(snapshots.get("integrations")):
        _add_if_string(integrations, item.get("name"))
    for key in ("sub-agents", "subagents", "agents"):
        for item in _items(snapshots.get(key)):
            _add_if_string(sub_agents, item.get("name") or item.get("id"))

    return _SnapshotReferences(
        tools=frozenset(tools),
        integrations=frozenset(integrations),
        sub_agents=frozenset(sub_agents),
    )


def _items(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        value = value.get("items", [])
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _add_if_string(target: set[str], value: Any) -> None:
    if isinstance(value, str) and value.strip():
        target.add(value.strip())


def _read_allowlist(path: Path | None) -> frozenset[str]:
    if path is None:
        return frozenset()
    allowlist_path = Path(path)
    if not allowlist_path.exists():
        return frozenset()
    entries: set[str] = set()
    for raw in allowlist_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        entries.add(line)
    return frozenset(entries)


def _is_allowed(kind: str, reference: str, allowlist: frozenset[str]) -> bool:
    return reference in allowlist or f"{kind}:{reference}" in allowlist


def _normalize_reference(reference: str) -> str:
    return reference.strip().strip(".,;:")


def _normalize_path_reference(reference: str) -> str:
    normalized = _normalize_reference(reference).replace("\\", "/")
    if not normalized or "://" in normalized:
        return ""
    if "/" not in normalized:
        return ""
    return normalized


def _resolve_path(repo_root: Path, reference: str) -> Path:
    path = Path(reference)
    if path.is_absolute():
        return path
    return repo_root / reference


def _strip_call_suffix(reference: str) -> str:
    if reference.endswith("()"):
        return reference[:-2]
    return reference


def _looks_like_qualified_reference(reference: str) -> bool:
    return bool(_QUALIFIED_RE.fullmatch(reference))


def _looks_like_integration_reference(reference: str) -> bool:
    if "/" in reference or "\\" in reference or "." in reference:
        return False
    return any(char.isupper() for char in reference) and bool(re.search(r"[A-Za-z]", reference))


def _looks_like_sub_agent_reference(reference: str) -> bool:
    lowered = reference.lower()
    return lowered.startswith("subagent:") or lowered.startswith("agent:")


def _is_tool_like(
    reference: str,
    snapshot_refs: _SnapshotReferences,
    static_index: _StaticIndex,
) -> bool:
    stripped = _strip_call_suffix(reference)
    if stripped in snapshot_refs.tools:
        return True
    if stripped.split(".", 1)[0] in static_index.module_roots:
        return False
    if any(part[:1].isupper() for part in stripped.split(".")):
        return False
    if reference.endswith("()"):
        return False
    return True


def _is_route_reference(reference: str) -> bool:
    return bool(re.match(r"^(GET|POST|PUT|PATCH|DELETE)\s+/", reference))


def _overlaps(span: tuple[int, int], others: list[tuple[int, int]]) -> bool:
    start, end = span
    return any(start < other_end and other_start < end for other_start, other_end in others)
