"""Local, documentation-confined MVP plan derivation for project intake."""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .config import KernelConfig
from .ollama import OllamaClient
from .project_autonomy import APPROVAL_BOUNDARIES


DOCUMENT_SUFFIXES = {".md", ".markdown", ".txt", ".rst", ".adoc"}
IGNORED_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "out",
    "coverage",
    ".coverage",
    ".expo",
    ".next",
    ".nuxt",
    "generated",
    "legacy",
    "archive",
    "archived",
    "deprecated",
    "old",
}
MAX_DOCUMENT_BYTES = 256_000
MAX_DOCUMENT_PROMPT_CHARS = 600_000


MVP_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["criteria", "external_boundaries", "needs_decision"],
    "properties": {
        "criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "id",
                    "title",
                    "description",
                    "source",
                    "acceptance_checks",
                    "verification_commands",
                    "depends_on",
                ],
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "source": {"type": "string"},
                    "acceptance_checks": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "verification_commands": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "depends_on": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
        "external_boundaries": {
            "type": "array",
            "items": {"type": "string", "enum": list(APPROVAL_BOUNDARIES)},
        },
        "needs_decision": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["question", "recommendation", "options"],
                    "properties": {
                        "question": {"type": "string"},
                        "recommendation": {"type": "string"},
                        "options": {
                            "type": "array",
                            "minItems": 2,
                            "maxItems": 3,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["option", "impact"],
                                "properties": {
                                    "option": {"type": "string"},
                                    "impact": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            ]
        },
    },
}


@dataclass(frozen=True)
class MvpPlanResult:
    criteria: list[dict[str, Any]]
    external_boundaries: list[str]
    source_hash: str
    plan_revision: str
    needs_decision: dict[str, Any] | None


@dataclass(frozen=True)
class _Document:
    relative_path: str
    content: str


class ProjectMvpPlanner:
    """Extract the documented MVP with a local structured-output model call."""

    def __init__(self, *, config: KernelConfig, ollama: Any | None = None):
        self.config = config
        self.ollama = ollama or OllamaClient(config.ollama)

    def plan(self, project: dict[str, Any]) -> MvpPlanResult:
        root = self._registered_root(project)
        documents = _read_project_documents(root)
        if not documents:
            raise ValueError("No eligible project documents were found for MVP planning.")
        source_hash = _source_hash(documents)
        result = self.ollama.chat(
            messages=_planning_messages(project, documents),
            model=(
                str(self.config.ollama.coding_agent_model or "").strip()
                or self.config.ollama.coding_model
            ),
            temperature=0,
            think=False,
            format=MVP_PLAN_SCHEMA,
            num_predict=4096,
        )
        try:
            payload = json.loads(str(result.response or ""))
        except json.JSONDecodeError as exc:
            raise ValueError("The local MVP planner returned invalid structured JSON.") from exc
        if not isinstance(payload, dict):
            raise ValueError("The local MVP planner response must be a JSON object.")

        allowed_sources = {doc.relative_path.casefold(): doc.relative_path for doc in documents}
        criteria = _normalize_criteria(payload.get("criteria"), allowed_sources)
        boundaries = _normalize_boundaries(payload.get("external_boundaries"))
        needs_decision = _normalize_decision(payload.get("needs_decision"))
        if not criteria and needs_decision is None:
            raise ValueError(
                "The documented MVP planner returned neither criteria nor a decision request."
            )
        canonical_plan = {
            "criteria": criteria,
            "external_boundaries": boundaries,
            "needs_decision": needs_decision,
            "source_hash": source_hash,
        }
        plan_revision = hashlib.sha256(
            json.dumps(canonical_plan, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return MvpPlanResult(
            criteria=criteria,
            external_boundaries=boundaries,
            source_hash=source_hash,
            plan_revision=plan_revision,
            needs_decision=needs_decision,
        )

    def _registered_root(self, project: dict[str, Any]) -> Path:
        raw = str(project.get("canonical_path") or "").strip()
        if not raw:
            raise ValueError("A registered project requires canonical_path.")
        root = Path(raw).resolve()
        intake_root = self.config.paths.project_intake_dir.resolve()
        if root == intake_root or not root.is_relative_to(intake_root):
            raise ValueError(
                f"Project path is outside the registered project-intake root: {root}"
            )
        if not root.is_dir():
            raise ValueError(f"Registered project root does not exist: {root}")
        return root


def _read_project_documents(root: Path) -> list[_Document]:
    documents: list[_Document] = []
    total_chars = 0
    for current, directories, filenames in os.walk(root, topdown=True, followlinks=False):
        directories[:] = sorted(
            name
            for name in directories
            if name.casefold() not in IGNORED_DIRECTORIES
            and not Path(current, name).is_symlink()
        )
        for filename in sorted(filenames):
            path = Path(current, filename)
            if path.is_symlink() or path.suffix.casefold() not in DOCUMENT_SUFFIXES:
                continue
            if "legacy" in path.stem.casefold():
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(root):
                continue
            if resolved.stat().st_size > MAX_DOCUMENT_BYTES:
                raise ValueError(f"Project document is too large to plan safely: {path.name}")
            content = _normalize_document(resolved.read_text(encoding="utf-8-sig"))
            if not content:
                continue
            total_chars += len(content)
            if total_chars > MAX_DOCUMENT_PROMPT_CHARS:
                raise ValueError("Project documentation exceeds the bounded planning prompt.")
            documents.append(
                _Document(
                    relative_path=resolved.relative_to(root).as_posix(),
                    content=content,
                )
            )
    return sorted(documents, key=lambda item: item.relative_path.casefold())


def _normalize_document(content: str) -> str:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in normalized.split("\n")]
    return "\n".join(lines).strip() + "\n" if any(line.strip() for line in lines) else ""


def _source_hash(documents: list[_Document]) -> str:
    digest = hashlib.sha256()
    for document in documents:
        digest.update(document.relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(document.content.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _planning_messages(
    project: dict[str, Any], documents: list[_Document]
) -> list[dict[str, str]]:
    system = (
        "Extract only the MVP requirements explicitly supported by the supplied project "
        "documents. Do not invent features, accounts, services, scope, or external actions. "
        "Every criterion must cite one supplied source-relative document path and have "
        "mechanically testable acceptance checks. If the documents leave a consequential "
        "product or architecture choice unresolved, return needs_decision with the exact "
        "question, one recommendation, and 2-3 concrete options with impacts. External "
        "authority boundaries must use only the schema enum. Return JSON matching the schema."
    )
    header = {
        "name": str(project.get("name") or ""),
        "product_type": str(project.get("product_type") or ""),
        "distribution_targets": list(project.get("distribution_targets") or []),
    }
    sections = [f"PROJECT\n{json.dumps(header, sort_keys=True)}"]
    sections.extend(
        f"DOCUMENT {document.relative_path}\n{document.content}"
        for document in documents
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(sections)},
    ]


def _normalize_criteria(
    raw: Any, allowed_sources: dict[str, str]
) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("MVP criteria must be a list.")
    criteria: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError("Each MVP criterion must be an object.")
        criterion_id = _criterion_id(entry.get("id"))
        if criterion_id in seen:
            raise ValueError(f"MVP plan contains duplicate criterion id: {criterion_id}")
        seen.add(criterion_id)
        title = _required_text(entry.get("title"), "criterion title")
        description = _required_text(entry.get("description"), "criterion description")
        source = _validated_source(entry.get("source"), allowed_sources)
        acceptance = _string_list(
            entry.get("acceptance_checks"), "acceptance_checks", required=True
        )
        commands = _string_list(
            entry.get("verification_commands"), "verification_commands", required=True
        )
        dependencies = [_criterion_id(item) for item in _string_list(
            entry.get("depends_on"), "depends_on", required=False
        )]
        criteria.append(
            {
                "id": criterion_id,
                "title": title,
                "description": description,
                "source": source,
                "acceptance_checks": acceptance,
                "verification_commands": commands,
                "depends_on": dependencies,
            }
        )
    known = {item["id"] for item in criteria}
    for item in criteria:
        for dependency in item["depends_on"]:
            if dependency not in known:
                raise ValueError(
                    f"Criterion {item['id']} depends on unknown criterion {dependency}."
                )
            if dependency == item["id"]:
                raise ValueError(f"Criterion {item['id']} cannot depend on itself.")
    _reject_dependency_cycles(criteria)
    return criteria


def _criterion_id(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode(
        "ascii", "ignore"
    ).decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")
    slug = re.sub(r"^mvp-+", "", slug)
    if not slug:
        raise ValueError("Each MVP criterion requires a stable id.")
    return f"mvp-{slug}"


def _validated_source(value: Any, allowed_sources: dict[str, str]) -> str:
    citation = str(value or "").strip().replace("\\", "/")
    base, separator, anchor = citation.partition("#")
    pure = PurePosixPath(base)
    if (
        not base
        or pure.is_absolute()
        or ".." in pure.parts
        or re.match(r"^[A-Za-z]:", base)
    ):
        raise ValueError(f"Criterion document source is outside the project: {citation}")
    canonical = allowed_sources.get(pure.as_posix().casefold())
    if canonical is None:
        raise ValueError(f"Criterion document source was not supplied: {citation}")
    return f"{canonical}#{anchor}" if separator and anchor.strip() else canonical


def _normalize_boundaries(raw: Any) -> list[str]:
    boundaries = _string_list(raw, "external_boundaries", required=False)
    invalid = sorted(set(boundaries) - set(APPROVAL_BOUNDARIES))
    if invalid:
        raise ValueError(f"Unknown external approval boundaries: {invalid}")
    return list(dict.fromkeys(boundaries))


def _normalize_decision(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("needs_decision must be null or an object.")
    options = raw.get("options")
    if not isinstance(options, list) or not 2 <= len(options) <= 3:
        raise ValueError("A planner decision requires 2-3 options.")
    normalized_options: list[dict[str, str]] = []
    for option in options:
        if not isinstance(option, dict):
            raise ValueError("Each planner decision option must be an object.")
        normalized_options.append(
            {
                "option": _required_text(option.get("option"), "decision option"),
                "impact": _required_text(option.get("impact"), "decision impact"),
            }
        )
    return {
        "question": _required_text(raw.get("question"), "decision question"),
        "recommendation": _required_text(
            raw.get("recommendation"), "decision recommendation"
        ),
        "options": normalized_options,
    }


def _required_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"MVP plan requires {label}.")
    return text


def _string_list(value: Any, label: str, *, required: bool) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"MVP plan field {label} must be a list.")
    cleaned = [str(item).strip() for item in value if str(item).strip()]
    if required and not cleaned:
        raise ValueError(f"MVP plan field {label} cannot be empty.")
    if len(cleaned) != len(value):
        raise ValueError(f"MVP plan field {label} contains an empty value.")
    return cleaned


def _reject_dependency_cycles(criteria: list[dict[str, Any]]) -> None:
    graph = {item["id"]: item["depends_on"] for item in criteria}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise ValueError(f"MVP criteria contain a dependency cycle at {node}.")
        if node in visited:
            return
        visiting.add(node)
        for dependency in graph[node]:
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for criterion_id in graph:
        visit(criterion_id)
