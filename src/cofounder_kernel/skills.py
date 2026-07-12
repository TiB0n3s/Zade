from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import KernelConfig
from .db import KernelDatabase, cosine_similarity
from .ingestion import Embedder, normalize_text, read_text_file


SEMANTIC_WEIGHT = 6.0
MIN_SEMANTIC_SIMILARITY = 0.30
EMBED_BODY_CHARS = 2000
EMBED_FAILURE_COOLDOWN_SECONDS = 60.0


DEFAULT_ENABLED_SKILLS = {
    "analytics",
    "ai-seo",
    "brainstorming",
    "brand-guidelines",
    "churn-prevention",
    "code-review",
    "competitor-profiling",
    "content-strategy",
    "copywriting",
    "cro",
    "customer-research",
    "diagnosing-bugs",
    "domain-modeling",
    "executing-plans",
    "frontend-design",
    "handoff",
    "launch",
    "marketing-psychology",
    "pricing",
    "sales-enablement",
    "seo-audit",
    "systematic-debugging",
    "test-driven-development",
    "verification-before-completion",
    "webapp-testing",
    "writing-plans",
}

CODING_HINT_SKILLS = {
    "code-review",
    "diagnosing-bugs",
    "systematic-debugging",
    "test-driven-development",
    "verification-before-completion",
    "webapp-testing",
}

PLANNING_HINT_SKILLS = {
    "brainstorming",
    "domain-modeling",
    "executing-plans",
    "writing-plans",
}

APPROVAL_GATED_TERMS = {
    "ad spend",
    "ads",
    "api key",
    "cold email",
    "cold outreach",
    "credential",
    "deploy to production",
    "email sequence",
    "external api",
    "github token",
    "linkedin",
    "oauth",
    "outbound",
    "payment",
    "paid",
    "prospecting",
    "production deploy",
    "public relations",
    "send email",
    "slack",
    "sms",
    "social",
}

LOCAL_WRITE_TERMS = {
    "build",
    "code",
    "create file",
    "edit",
    "generate",
    "implement",
    "refactor",
    "write",
}


@dataclass(frozen=True)
class SkillRouteResult:
    items: list[dict[str, Any]]
    query: str
    task_type: str

    def summary(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "task_type": self.task_type,
            "selected_count": len(self.items),
            "selected": [
                {
                    "id": item["id"],
                    "name": item["name"],
                    "description": item["description"],
                    "source": item["source"],
                    "risk_tier": item["risk_tier"],
                    "score": item["score"],
                    "keyword_score": item.get("keyword_score", item["score"]),
                    "semantic_score": item.get("semantic_score", 0.0),
                    "semantic_similarity": item.get("semantic_similarity", 0.0),
                }
                for item in self.items
            ],
        }


class SkillService:
    def __init__(self, *, config: KernelConfig, db: KernelDatabase, embedder: Embedder | None = None):
        self.config = config
        self.db = db
        self.embedder = embedder
        self._embed_unavailable_until = 0.0

    def scan(self, *, source_dir: str | Path | None = None, enable_defaults: bool | None = None) -> dict[str, Any]:
        root = Path(source_dir or self.config.skills.source_dir).expanduser().resolve()
        if not root.exists():
            raise ValueError(f"Skill source directory does not exist: {root}")
        lock = self._load_lock()
        defaulting = self.config.skills.enable_defaults if enable_defaults is None else enable_defaults
        scanned = 0
        created = 0
        updated = 0
        embedded = 0
        embedding_errors = 0
        errors: list[dict[str, str]] = []

        for skill_md in sorted(root.rglob("SKILL.md")):
            try:
                text = read_text_file(skill_md)
                frontmatter, body = parse_frontmatter(text)
                inferred_name = skill_md.parent.name
                name = str(frontmatter.get("name") or inferred_name).strip() or inferred_name
                description = str(frontmatter.get("description") or first_paragraph(body)).strip()
                lock_item = lock.get(name) or lock.get(inferred_name) or {}
                risk_tier, risk_reasons = classify_skill_risk(name=name, description=description, body=body)
                default_enabled = bool(defaulting and name in DEFAULT_ENABLED_SKILLS)
                content_hash = sha256_text(normalize_text(text))
                skill_id, was_created = self.db.upsert_skill(
                    name=name,
                    description=description,
                    body=normalize_text(body),
                    source=str(lock_item.get("source", "")),
                    source_type=str(lock_item.get("sourceType", "")),
                    skill_path=str(lock_item.get("skillPath", str(skill_md.relative_to(root)))),
                    local_path=str(skill_md),
                    content_hash=content_hash,
                    risk_tier=risk_tier,
                    risk_reasons=risk_reasons,
                    default_enabled=default_enabled,
                    metadata={
                        "frontmatter": frontmatter,
                        "lock_hash": lock_item.get("computedHash", ""),
                        "source_root": str(root),
                    },
                )
                self.db.replace_skill_references(skill_id, collect_references(skill_md.parent))
                embed_status = self._embed_skill(
                    skill_id=skill_id,
                    name=name,
                    description=description,
                    body=body,
                    content_hash=content_hash,
                )
                if embed_status == "embedded":
                    embedded += 1
                elif embed_status == "error":
                    embedding_errors += 1
                scanned += 1
                if was_created:
                    created += 1
                else:
                    updated += 1
            except Exception as exc:
                errors.append({"path": str(skill_md), "error": str(exc)})

        audit_id = self.db.audit(
            actor="skills",
            action="skills.scan",
            target=str(root),
            permission_tier="L1_MEMORY_WRITE",
            status="ok" if not errors else "partial",
            details={
                "scanned": scanned,
                "created": created,
                "updated": updated,
                "embedded": embedded,
                "embedding_errors": embedding_errors,
                "errors": errors,
            },
        )
        return {
            "status": "ok" if not errors else "partial",
            "source_dir": str(root),
            "scanned": scanned,
            "created": created,
            "updated": updated,
            "embedded": embedded,
            "embedding_errors": embedding_errors,
            "errors": errors,
            "audit_id": audit_id,
            "summary": self.db.skill_summary(),
        }

    def _embed_skill(self, *, skill_id: int, name: str, description: str, body: str, content_hash: str) -> str:
        """Embed a skill for semantic routing; failures are silent so keyword routing keeps working."""
        if not self._embedder_ready():
            return "skipped"
        if self.db.get_skill_embedding_hash(skill_id) == content_hash:
            return "cached"
        embed_text = "\n".join(part for part in [name, description, body[:EMBED_BODY_CHARS]] if part)
        try:
            vector = self.embedder.embed(text=embed_text, model=self.config.ollama.embedding_model)  # type: ignore[union-attr]
        except Exception:
            self._mark_embedder_down()
            return "error"
        if not vector:
            return "error"
        self.db.upsert_skill_embedding(
            skill_id=skill_id,
            model=self.config.ollama.embedding_model,
            vector=vector,
            content_hash=content_hash,
        )
        return "embedded"

    def list_skills(
        self,
        *,
        enabled: bool | None = None,
        risk_tier: str | None = None,
        source: str | None = None,
        limit: int = 250,
    ) -> dict[str, Any]:
        return {
            "items": self.db.list_skills(enabled=enabled, risk_tier=risk_tier, source=source, limit=limit),
            "summary": self.db.skill_summary(),
        }

    def get_skill(self, name: str) -> dict[str, Any]:
        skill = self.db.get_skill(name)
        if not skill:
            raise ValueError(f"Unknown skill: {name}")
        return skill

    def enable(self, name: str) -> dict[str, Any]:
        skill = self.db.set_skill_enabled(name, True)
        self.db.audit(
            actor="skills",
            action="skills.enable",
            target=name,
            permission_tier="L1_MEMORY_WRITE",
            status="ok",
            details={"risk_tier": skill["risk_tier"]},
        )
        return skill

    def disable(self, name: str) -> dict[str, Any]:
        skill = self.db.set_skill_enabled(name, False)
        self.db.audit(
            actor="skills",
            action="skills.disable",
            target=name,
            permission_tier="L1_MEMORY_WRITE",
            status="ok",
            details={"risk_tier": skill["risk_tier"]},
        )
        return skill

    def route(self, *, query: str, task_type: str = "general", limit: int = 3) -> SkillRouteResult:
        limit = max(0, min(limit, 8))
        if limit <= 0 or not query.strip():
            return SkillRouteResult(items=[], query=query, task_type=task_type)
        candidates = self.db.search_skills(query, enabled=True, limit=max(limit * 6, 12))
        semantic_scores = self._semantic_scores(query)
        candidates = candidates + self._semantic_extras(semantic_scores, exclude_ids={int(item["id"]) for item in candidates})
        scored = []
        for index, item in enumerate(candidates):
            keyword_score = score_skill_match(item, query=query, task_type=task_type, rank_index=index)
            similarity = max(0.0, semantic_scores.get(int(item["id"]), 0.0))
            semantic_score = similarity * SEMANTIC_WEIGHT
            total = keyword_score + semantic_score
            if total >= 2.0:
                scored.append(
                    item
                    | {
                        "score": round(total, 4),
                        "keyword_score": round(keyword_score, 4),
                        "semantic_score": round(semantic_score, 4),
                        "semantic_similarity": round(similarity, 4),
                        "prompt_excerpt": prompt_excerpt(item, self.config.skills.max_prompt_chars),
                    }
                )
        scored.sort(key=lambda item: item["score"], reverse=True)
        return SkillRouteResult(items=scored[:limit], query=query, task_type=task_type)

    def _semantic_scores(self, query: str) -> dict[int, float]:
        """Cosine similarity per enabled skill; empty when no embedder or embeddings exist."""
        if not self._embedder_ready():
            return {}
        embeddings = self.db.list_skill_embeddings(enabled_only=True)
        if not embeddings:
            return {}
        try:
            query_vector = self.embedder.embed(text=query, model=self.config.ollama.embedding_model)  # type: ignore[union-attr]
        except Exception:
            self._mark_embedder_down()
            return {}
        if not query_vector:
            return {}
        return {item["skill_id"]: cosine_similarity(query_vector, item["vector"]) for item in embeddings}

    def _embedder_ready(self) -> bool:
        return bool(self.embedder) and time.monotonic() >= self._embed_unavailable_until

    def _mark_embedder_down(self) -> None:
        """Back off after an embed failure so a down Ollama does not stall every route call."""
        self._embed_unavailable_until = time.monotonic() + EMBED_FAILURE_COOLDOWN_SECONDS

    def _semantic_extras(self, semantic_scores: dict[int, float], *, exclude_ids: set[int]) -> list[dict[str, Any]]:
        """Skills the keyword search missed but the embedding space says are close."""
        ranked = sorted(semantic_scores.items(), key=lambda pair: -pair[1])
        wanted = [
            skill_id
            for skill_id, similarity in ranked[:12]
            if similarity >= MIN_SEMANTIC_SIMILARITY and skill_id not in exclude_ids
        ]
        if not wanted:
            return []
        enabled = {int(item["id"]): item for item in self.db.list_skills(enabled=True, limit=500)}
        return [enabled[skill_id] for skill_id in wanted if skill_id in enabled]

    def record_invocations(self, route: SkillRouteResult, *, runtime_event_id: int | None = None) -> list[int]:
        invocation_ids = []
        for item in route.items:
            invocation_ids.append(
                self.db.record_skill_invocation(
                    skill_id=int(item["id"]),
                    name=item["name"],
                    query=route.query,
                    score=float(item["score"]),
                    task_type=route.task_type,
                    runtime_event_id=runtime_event_id,
                    metadata={"risk_tier": item["risk_tier"], "source": item["source"]},
                )
            )
        return invocation_ids

    def prompt_block(self, route: SkillRouteResult) -> str:
        if not route.items:
            return "No operating skills matched this request."
        lines = []
        for item in route.items:
            lines.append(
                "\n".join(
                    [
                        f"- Skill: {item['name']} (score {item['score']}, risk {item['risk_tier']}, source {item['source'] or 'local'})",
                        f"  Description: {item['description']}",
                        f"  Operating excerpt: {item['prompt_excerpt']}",
                    ]
                )
            )
        return "\n\n".join(lines)

    def _load_lock(self) -> dict[str, dict[str, Any]]:
        lock_path = self.config.skills.lock_file.expanduser()
        if not lock_path.exists():
            return {}
        try:
            raw = json.loads(lock_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        skills = raw.get("skills", {})
        return skills if isinstance(skills, dict) else {}


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---\n"):
        return {}, normalized.strip()
    end = normalized.find("\n---", 4)
    if end == -1:
        return {}, normalized.strip()
    front = normalized[4:end].strip()
    body = normalized[end + 4 :].strip()
    parsed: dict[str, Any] = {}
    current_key = ""
    for line in front.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith((" ", "\t")):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        current_key = key.strip()
        parsed[current_key] = clean_scalar(value.strip())
    return parsed, body


def clean_scalar(value: str) -> Any:
    if value in {"", "|", ">"}:
        return ""
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    return value.strip().strip('"').strip("'")


def first_paragraph(markdown: str) -> str:
    for block in re.split(r"\n\s*\n", markdown):
        cleaned = re.sub(r"^#+\s*", "", block.strip())
        if cleaned and not cleaned.startswith("```"):
            return re.sub(r"\s+", " ", cleaned)[:500]
    return ""


def collect_references(skill_dir: Path) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file() or path.name == "SKILL.md":
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        references.append(
            {
                "relative_path": str(path.relative_to(skill_dir)),
                "local_path": str(path),
                "content_hash": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
                "metadata": {"extension": path.suffix.lower()},
            }
        )
    return references


def classify_skill_risk(*, name: str, description: str, body: str) -> tuple[str, list[str]]:
    haystack = f"{name}\n{description}\n{body}".lower()
    reasons = sorted(term for term in APPROVAL_GATED_TERMS if term in haystack)
    if reasons:
        return "approval_gated", reasons[:8]
    local_reasons = sorted(term for term in LOCAL_WRITE_TERMS if term in haystack)
    if local_reasons:
        return "local_write", local_reasons[:8]
    return "read_only", []


def score_skill_match(skill: dict[str, Any], *, query: str, task_type: str, rank_index: int) -> float:
    tokens = query_tokens(query)
    if not tokens:
        return 0.0
    name = skill["name"].lower()
    description = skill["description"].lower()
    body = skill["body"].lower()
    score = max(0.0, 0.5 - (rank_index * 0.03))
    for token in tokens[:12]:
        if token in name:
            score += 4.0
        if token in description:
            score += 2.0
        if token in body:
            score += 0.08
    if skill.get("default_enabled"):
        score += 0.1
    if task_type == "coding" and skill["name"] in CODING_HINT_SKILLS:
        score += 3.0
    verification_terms = {"claim", "claiming", "complete", "done", "fixed", "pass", "passing", "test", "tests", "verify"}
    if skill["name"] == "verification-before-completion" and verification_terms.intersection(tokens):
        score += 4.0
    planning_terms = {"plan", "planning", "roadmap", "strategy", "execute", "steps", "breakdown"}
    if task_type in {"general", "reasoning"} and skill["name"] in PLANNING_HINT_SKILLS and planning_terms.intersection(tokens):
        score += 0.6
    return score


def query_tokens(query: str) -> list[str]:
    stopwords = {
        "about",
        "after",
        "and",
        "before",
        "action",
        "check",
        "could",
        "for",
        "from",
        "have",
        "into",
        "local",
        "need",
        "next",
        "one",
        "please",
        "sentence",
        "should",
        "smoke",
        "state",
        "that",
        "the",
        "this",
        "will",
        "what",
        "when",
        "with",
        "would",
        "zade",
    }
    return [token for token in re.findall(r"[a-z0-9_]+", query.lower()) if len(token) >= 3 and token not in stopwords]


def prompt_excerpt(skill: dict[str, Any], max_chars: int) -> str:
    body = normalize_text(skill.get("body", ""))
    if not body:
        return ""
    body = re.sub(r"\n{3,}", "\n\n", body)
    body = re.sub(r"[ \t]+", " ", body)
    return body[: max(200, max_chars)].strip()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
