"""Local, documentation-confined MVP plan derivation for project intake."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
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
    ".cxx",
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
# Leave most of the 40k-token local context window available for the schema and
# a structured response.  This is an input ceiling, not a document-size limit.
MAX_DOCUMENT_PROMPT_CHARS = 80_000


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
        scope_kind = _scope_kind(project)
        documents = _read_project_documents(root, scope_kind=scope_kind)
        if not documents:
            raise ValueError("No eligible project documents were found for MVP planning.")
        source_hash = _source_hash(documents)
        structured_plan = (
            _load_structured_continuation_plan(root)
            if scope_kind == "continuation"
            else None
        )
        if structured_plan is not None:
            payload, structured_source = structured_plan
            source_hash = hashlib.sha256(
                f"{source_hash}\0{structured_source}".encode("utf-8")
            ).hexdigest()
        else:
            result = self.ollama.chat(
                messages=_planning_messages(project, documents),
                model=(
                    str(self.config.ollama.coding_agent_model or "").strip()
                    or self.config.ollama.coding_model
                ),
                temperature=0,
                think=False,
                format=_planning_schema(project),
                num_predict=4096,
            )
            try:
                payload = _parse_planner_json(result.response)
            except json.JSONDecodeError as exc:
                raise ValueError("The local MVP planner returned invalid structured JSON.") from exc
        if not isinstance(payload, dict):
            raise ValueError("The local MVP planner response must be a JSON object.")

        allowed_sources = {doc.relative_path.casefold(): doc.relative_path for doc in documents}
        criteria = _normalize_criteria(
            payload.get("criteria"),
            allowed_sources,
            fallback_commands=_fallback_verification_commands(root),
        )
        if scope_kind == "continuation":
            completed = _completed_criterion_ids(project)
            criteria = [item for item in criteria if item["id"] not in completed]
            criteria = [
                item for item in criteria if not _is_external_boundary_only_criterion(item)
            ]
        boundaries = _normalize_boundaries(payload.get("external_boundaries"))
        raw_decision = payload.get("needs_decision")
        if scope_kind == "continuation" and (
            _is_external_delivery_decision(raw_decision)
            or _is_blank_decision(raw_decision)
        ):
            needs_decision = None
        else:
            needs_decision = _normalize_decision(raw_decision)
        if not criteria and needs_decision is None and scope_kind != "continuation":
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


def _planning_schema(project: dict[str, Any]) -> dict[str, Any]:
    """Return a schema that makes an already-resolved decision unrepresentable."""
    metadata = project.get("metadata") or {}
    if not str(metadata.get("planner_rejected_duplicate_decision") or "").strip():
        return MVP_PLAN_SCHEMA
    schema = json.loads(json.dumps(MVP_PLAN_SCHEMA))
    schema["properties"]["needs_decision"] = {"type": "null"}
    return schema


def _parse_planner_json(response: Any) -> Any:
    text = str(response or "").strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as primary_error:
        # Some local models honor the schema but still add a short lead-in or
        # trailing note. Recover only a complete embedded JSON object; normal
        # schema and source validation still run afterwards.
        decoder = json.JSONDecoder()
        for index, character in enumerate(text):
            if character != "{":
                continue
            try:
                value, _end = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            return value
        raise primary_error


def _load_structured_continuation_plan(root: Path) -> tuple[dict[str, Any], str] | None:
    path = root / "docs" / "product" / "continuation-plan.json"
    if not path.is_file():
        return None
    try:
        source = path.read_text(encoding="utf-8")
        payload = json.loads(source)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            "The documented structured continuation plan is invalid JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError("The documented structured continuation plan must be a JSON object.")
    return payload, source


def _read_project_documents(root: Path, *, scope_kind: str = "mvp") -> list[_Document]:
    """Read a bounded, deterministic set of founder-authored planning sources.

    Project histories often contain much more prose than fits safely in the
    local model context.  Selection gives current product documentation and
    the project manifest precedence, rather than turning a large historical
    handoff into a permanent autonomy block.
    """
    candidates: list[_Document] = []
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
                # A generated review package or historical handoff cannot be
                # allowed to consume the bounded model context and stall every
                # later delivery scope. Smaller, current planning documents
                # remain eligible through the deterministic selector below.
                continue
            content = _normalize_document(resolved.read_text(encoding="utf-8-sig"))
            if not content:
                continue
            candidates.append(
                _Document(
                    relative_path=resolved.relative_to(root).as_posix(),
                    content=content,
                )
            )
    if scope_kind == "continuation" and any(
        document.relative_path.casefold().startswith("docs/product/")
        for document in candidates
    ):
        # A current product delivery brief explicitly supersedes historical
        # implementation specs. Including those older specs can make a
        # continuation rediscover completed MVP work under a different title.
        candidates = [
            document
            for document in candidates
            if document.relative_path.casefold().startswith("docs/product/")
            or PurePosixPath(document.relative_path).name.casefold()
            in {"project.md", "project.markdown", "readme.md", "readme.markdown"}
        ]
    selected: list[_Document] = []
    total_chars = 0
    for document in sorted(
        candidates,
        key=lambda item: _document_priority(item.relative_path, scope_kind),
    ):
        if total_chars + len(document.content) > MAX_DOCUMENT_PROMPT_CHARS:
            continue
        selected.append(document)
        total_chars += len(document.content)
    if not selected:
        raise ValueError("No project documentation fits within the bounded planning prompt.")
    return sorted(selected, key=lambda item: item.relative_path.casefold())


def _document_priority(relative_path: str, scope_kind: str) -> tuple[int, str]:
    path = PurePosixPath(relative_path)
    lowered = relative_path.casefold()
    name = path.name.casefold()
    if scope_kind == "continuation" and lowered.startswith("docs/product/"):
        return (0, lowered)
    if name in {"project.md", "project.markdown"}:
        return (1, lowered)
    if name in {"readme.md", "readme.markdown"}:
        return (2, lowered)
    if scope_kind == "continuation" and lowered.startswith("docs/"):
        return (3, lowered)
    if "handoff" in name:
        return (4, lowered)
    if name.startswith("mvp"):
        return (5, lowered)
    return (6, lowered)


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
    scope_kind = _scope_kind(project)
    system = (
        "Extract only the MVP requirements explicitly supported by the supplied project "
        "documents. Do not invent features, accounts, services, scope, or external actions. "
        "Every criterion must cite one supplied source-relative document path and have "
        "mechanically testable acceptance checks. If the documents leave a consequential "
        "product or architecture choice unresolved, return needs_decision with the exact "
        "question, one recommendation, and 2-3 concrete options with impacts. External "
        "authority boundaries must use only the schema enum. Accepted founder answers are "
        "binding founder constraints. You must not return needs_decision for a choice already "
        "answered there. If rejected_duplicate_decision is present, the prior response was "
        "invalid: use the accepted answer and return criteria instead. Return JSON matching "
        "the schema."
    )
    if scope_kind == "continuation":
        system += (
            " This is a continuation after an achieved MVP: extract only remaining documented "
            "internal work. Do not repeat completed criteria. Deployment, store submission, "
            "external credentials or accounts, paid services, legal publication, and irreversible "
            "public actions must remain an external boundary rather than a criterion. Never ask for "
            "a production domain, subdomain, hosting provider, deployment account, credential, or "
            "store identifier: those details are not needed for local implementation and are external "
            "boundaries. An empty criteria list is valid when no documented internal work remains."
        )
    header = {
        "name": str(project.get("name") or ""),
        "product_type": str(project.get("product_type") or ""),
        "distribution_targets": list(project.get("distribution_targets") or []),
        "accepted_founder_answers": list(
            (project.get("metadata") or {}).get("planner_founder_answers") or []
        ),
        "rejected_duplicate_decision": str(
            (project.get("metadata") or {}).get("planner_rejected_duplicate_decision") or ""
        ),
        "scope_kind": scope_kind,
        "completed_criteria": sorted(_completed_criterion_ids(project)),
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


def _scope_kind(project: dict[str, Any]) -> str:
    autonomy = (project.get("metadata") or {}).get("autonomy") or {}
    return "continuation" if autonomy.get("scope_kind") == "continuation" else "mvp"


def _completed_criterion_ids(project: dict[str, Any]) -> set[str]:
    autonomy = (project.get("metadata") or {}).get("autonomy") or {}
    completed: set[str] = set()
    for milestone in autonomy.get("milestones") or []:
        if not isinstance(milestone, dict):
            continue
        for criterion_id in milestone.get("criteria") or []:
            value = str(criterion_id or "").strip()
            if value:
                completed.add(value)
    return completed


def _normalize_criteria(
    raw: Any,
    allowed_sources: dict[str, str],
    *,
    fallback_commands: list[str],
) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        raw = [raw]
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
        declared_commands = _string_list(
            entry.get("verification_commands"), "verification_commands", required=True
        )
        commands = [
            command
            for command in declared_commands
            if _is_audited_verification_command(command)
        ] or list(fallback_commands)
        if not commands:
            raise ValueError(
                f"Criterion {criterion_id} has no executable verification command."
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


def _fallback_verification_commands(root: Path) -> list[str]:
    package = root / "package.json"
    if package.is_file():
        try:
            payload = json.loads(package.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        scripts = payload.get("scripts") if isinstance(payload, dict) else {}
        if isinstance(scripts, dict):
            commands: list[str] = []
            if str(scripts.get("test") or "").strip():
                commands.append("npm test")
            if str(scripts.get("typecheck") or "").strip():
                commands.append("npm run typecheck")
            if commands:
                return commands
    if (root / "pubspec.yaml").is_file():
        return ["flutter analyze", "flutter test"]
    if any(
        (root / name).is_file() for name in ("pyproject.toml", "pytest.ini", "setup.cfg")
    ):
        return ["python -m pytest -q"]
    return []


def _is_audited_verification_command(command: str) -> bool:
    try:
        argv = shlex.split(str(command), posix=os.name != "nt")
    except ValueError:
        return False
    if not argv:
        return False
    lowered = [item.casefold() for item in argv]
    executable = Path(lowered[0]).name
    if executable == "npm":
        return len(lowered) >= 2 and (
            lowered[1] == "test"
            or (
                len(lowered) >= 3
                and lowered[1] == "run"
                and lowered[2] in {"test", "typecheck", "lint", "build"}
            )
        )
    if executable in {"pytest", "flutter", "dart"}:
        return len(lowered) >= 2 and lowered[1] in {"test", "analyze"}
    if executable.startswith("python"):
        return len(lowered) >= 3 and lowered[1:3] == ["-m", "pytest"]
    if executable == "node":
        return len(lowered) >= 2 and lowered[1] == "--test"
    return False


def _is_external_boundary_only_criterion(criterion: dict[str, Any]) -> bool:
    text = " ".join(
        str(criterion.get(key) or "")
        for key in ("id", "title", "description")
    ).casefold()
    return "external boundar" in text


def _is_external_delivery_decision(decision: Any) -> bool:
    if not isinstance(decision, dict):
        return False
    text = " ".join(
        [
            str(decision.get("question") or ""),
            str(decision.get("recommendation") or ""),
            *[
                " ".join(
                    (str(option.get("option") or ""), str(option.get("impact") or ""))
                )
                for option in decision.get("options") or []
                if isinstance(option, dict)
            ],
        ]
    ).casefold()
    return any(
        marker in text
        for marker in (
            "domain",
            "subdomain",
            "hosting",
            "host provider",
            "deployment account",
            "credential",
            "app store identifier",
        )
    )


def _is_blank_decision(decision: Any) -> bool:
    if not isinstance(decision, dict):
        return False
    text = [
        str(decision.get("question") or "").strip(),
        str(decision.get("recommendation") or "").strip(),
    ]
    for option in decision.get("options") or []:
        if isinstance(option, dict):
            text.extend(
                [
                    str(option.get("option") or "").strip(),
                    str(option.get("impact") or "").strip(),
                ]
            )
    return not any(text)


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
