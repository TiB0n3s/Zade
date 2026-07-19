"""Deterministic local-first routing and bounded cloud context selection."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Sequence

from .build_types import BuildLease, BuildSession


_CLOUD_REASONS = {
    "architecture": "architecture_with_cross_module_tradeoffs",
    "debug": "difficult_debugging_after_two_local_attempts",
    "cross_cutting": "cross_cutting_high_regression_risk",
    "critical_review": "security_billing_migration_or_release_review",
    "diff_review": "high_risk_diff_review",
    "local_exceeded": "local_model_capability_exceeded",
}
_CRITICAL_DOMAINS = {
    "authentication",
    "authorization",
    "billing",
    "migration",
    "payments",
    "release",
    "security",
}
_ROUTINE_KINDS = {
    "audit_summary",
    "checkpoint",
    "command",
    "context_compression",
    "discovery",
    "edit",
    "format",
    "inventory",
    "read",
    "search",
    "status",
    "test",
}
_EXCLUDED_DIRECTORIES = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "chat_history",
    "chats",
    "conversations",
    "coverage",
    "dist",
    "generated",
    "node_modules",
    "target",
    "vendor",
    "venv",
}
_BINARY_SUFFIXES = {
    ".7z",
    ".bin",
    ".bmp",
    ".class",
    ".dll",
    ".eot",
    ".exe",
    ".gif",
    ".gz",
    ".ico",
    ".jar",
    ".jpeg",
    ".jpg",
    ".mov",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".pyc",
    ".so",
    ".tar",
    ".ttf",
    ".wav",
    ".webm",
    ".woff",
    ".woff2",
    ".xz",
    ".zip",
}
_SECRET_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ed25519",
    "id_rsa",
    "secrets.json",
}
_MANIFEST_NAMES = {
    "package.json",
    "pyproject.toml",
    "cargo.toml",
    "go.mod",
    "pom.xml",
}
_INSTRUCTION_NAMES = {"agents.md", "claude.md", "contributing.md"}
_MAX_SOURCE_BYTES = 2_000_000


@dataclass(frozen=True)
class BuildStep:
    kind: str
    risk: str = "low"
    description: str = ""
    cross_module: bool = False
    regression_risk: bool = False
    critical_domains: tuple[str, ...] = ()
    local_capability_exceeded: bool = False


@dataclass(frozen=True)
class LocalAttempt:
    summary: str
    action: str = ""
    outcome: str = "failed"

    @property
    def action_hash(self) -> str:
        normalized = " ".join((self.action or self.summary).lower().split())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RouteDecision:
    route: str
    reasons: tuple[str, ...]
    blockers: tuple[str, ...] = ()
    lease_id: int | None = None


@dataclass(frozen=True)
class ContextExcerpt:
    path: str
    start_line: int
    end_line: int
    content: str
    content_hash: str
    truncated: bool
    utf8_bytes: int


@dataclass(frozen=True)
class SelectedContext:
    excerpts: tuple[ContextExcerpt, ...]
    total_bytes: int
    truncated: bool

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(excerpt.path for excerpt in self.excerpts)

    @property
    def total_chars(self) -> int:
        return sum(len(excerpt.content) for excerpt in self.excerpts)


class BuildRouter:
    def __init__(
        self,
        *,
        lease_lookup: Callable[[int], BuildLease | None],
        cloud_enabled: bool,
        pricing_current: Callable[[], bool],
        clock: Callable[[], datetime] | None = None,
    ):
        self._lease_lookup = lease_lookup
        self._cloud_enabled = cloud_enabled
        self._pricing_current = pricing_current
        self._clock = clock or (lambda: datetime.now(UTC))

    def route_step(
        self,
        session: BuildSession,
        step: BuildStep,
        attempts: Sequence[LocalAttempt],
    ) -> RouteDecision:
        kind = step.kind.strip().lower()
        risk = step.risk.strip().lower()
        if kind in _ROUTINE_KINDS and not step.local_capability_exceeded:
            return RouteDecision("local", ("routine_work_stays_local",))

        reasons: list[str] = []
        if kind == "architecture" and (step.cross_module or risk == "high"):
            reasons.append(_CLOUD_REASONS["architecture"])
        failed_hashes = {
            attempt.action_hash
            for attempt in attempts
            if attempt.outcome.strip().lower() == "failed"
        }
        if kind == "debug" and risk == "high" and len(failed_hashes) >= 2:
            reasons.append(_CLOUD_REASONS["debug"])
        if step.cross_module and step.regression_risk and risk == "high":
            reasons.append(_CLOUD_REASONS["cross_cutting"])
        critical_domains = {
            value.strip().lower() for value in step.critical_domains if value.strip()
        }
        if critical_domains & _CRITICAL_DOMAINS and risk == "high":
            reasons.append(_CLOUD_REASONS["critical_review"])
        if kind in {"review", "diff_review"} and risk == "high":
            reasons.append(_CLOUD_REASONS["diff_review"])
        if step.local_capability_exceeded:
            reasons.append(_CLOUD_REASONS["local_exceeded"])
        reasons = list(dict.fromkeys(reasons))
        if not reasons:
            return RouteDecision("local", ("local_first_no_cloud_eligibility",))

        lease = self._lease_lookup(session.id)
        blockers: list[str] = []
        if not self._cloud_enabled:
            blockers.append("cloud_disabled")
        if lease is None:
            blockers.append("no_active_lease")
        else:
            if lease.state not in {"active", "warning"}:
                blockers.append(f"lease_{lease.state}")
            if self._now() >= datetime.fromisoformat(lease.expires_at).astimezone(UTC):
                blockers.append("lease_expired")
        if not self._pricing_current():
            blockers.append("pricing_stale")
        if blockers:
            return RouteDecision("founder", tuple(reasons), tuple(blockers))
        return RouteDecision("cloud", tuple(reasons), lease_id=lease.id if lease else None)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


@dataclass(frozen=True)
class _Candidate:
    path: Path
    relative: str
    score: int
    data: bytes
    text: str


class BuildContextSelector:
    def __init__(
        self,
        workspace: str | Path,
        *,
        max_bytes: int = 48_000,
        max_file_bytes: int = 16_000,
        max_files: int = 12,
    ):
        if max_bytes <= 0 or max_file_bytes <= 0 or max_files <= 0:
            raise ValueError("Context limits must be positive")
        self.workspace = Path(workspace).resolve()
        self.max_bytes = max_bytes
        self.max_file_bytes = min(max_file_bytes, max_bytes)
        self.max_files = max_files

    def select(
        self,
        *,
        task: str,
        candidates: Sequence[Path],
        changed_files: Sequence[Path] = (),
        failing_paths: Sequence[Path] = (),
    ) -> SelectedContext:
        changed = self._relative_set(changed_files)
        failing = self._relative_set(failing_paths)
        task_lower = task.lower()
        terms = {
            match.group(0).lower()
            for match in re.finditer(r"[A-Za-z0-9_.-]+", task)
            if len(match.group(0)) >= 3
        }
        ranked: dict[str, _Candidate] = {}
        for candidate_path in candidates:
            safe = self._safe_path(candidate_path)
            if safe is None:
                continue
            path, relative = safe
            try:
                if path.stat().st_size > _MAX_SOURCE_BYTES:
                    continue
                data = path.read_bytes()
                text = data.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if "\x00" in text:
                continue
            score = self._score_candidate(
                relative, text, task_lower, terms, changed, failing
            )
            current = ranked.get(relative)
            value = _Candidate(path, relative, score, data, text)
            if current is None or value.score > current.score:
                ranked[relative] = value

        excerpts: list[ContextExcerpt] = []
        total_bytes = 0
        selection_truncated = False
        for candidate in sorted(ranked.values(), key=lambda item: (-item.score, item.relative)):
            if len(excerpts) >= self.max_files or total_bytes >= self.max_bytes:
                selection_truncated = True
                break
            remaining = self.max_bytes - total_bytes
            excerpt = self._excerpt(candidate, terms, min(self.max_file_bytes, remaining))
            if excerpt is None:
                continue
            excerpts.append(excerpt)
            total_bytes += excerpt.utf8_bytes
            selection_truncated = selection_truncated or excerpt.truncated
        if len(excerpts) < len(ranked):
            selection_truncated = True
        return SelectedContext(tuple(excerpts), total_bytes, selection_truncated)

    def _relative_set(self, paths: Sequence[Path]) -> set[str]:
        result: set[str] = set()
        for path in paths:
            safe = self._safe_path(path)
            if safe is not None:
                result.add(safe[1])
        return result

    def _safe_path(self, candidate: Path) -> tuple[Path, str] | None:
        supplied = Path(candidate)
        if supplied.is_symlink():
            return None
        path = supplied if supplied.is_absolute() else self.workspace / supplied
        try:
            path = path.resolve()
            relative_path = path.relative_to(self.workspace)
        except (OSError, ValueError):
            return None
        if not path.is_file() or path.is_symlink():
            return None
        lowered_parts = {part.lower() for part in relative_path.parts[:-1]}
        name = path.name.lower()
        if lowered_parts & _EXCLUDED_DIRECTORIES:
            return None
        if any(
            token in part
            for part in lowered_parts
            for token in ("secret", "credential", "private_key")
        ):
            return None
        if name in _SECRET_NAMES or name.startswith(".env."):
            return None
        if any(token in name for token in ("secret", "credential", "private_key")):
            return None
        if path.suffix.lower() in _BINARY_SUFFIXES:
            return None
        return path, relative_path.as_posix()

    @staticmethod
    def _score_candidate(
        relative: str,
        text: str,
        task_lower: str,
        terms: set[str],
        changed: set[str],
        failing: set[str],
    ) -> int:
        name = Path(relative).name.lower()
        relative_lower = relative.lower()
        score = 0
        if relative_lower in task_lower:
            score += 120
        if name in task_lower:
            score += 90
        score += min(60, sum(10 for term in terms if term in text.lower()))
        if relative in changed:
            score += 70
        if relative in failing:
            score += 80
        if name in _MANIFEST_NAMES:
            score += 25
        if name in _INSTRUCTION_NAMES:
            score += 20
        return score

    @staticmethod
    def _excerpt(
        candidate: _Candidate, terms: set[str], byte_limit: int
    ) -> ContextExcerpt | None:
        if byte_limit <= 0:
            return None
        lines = candidate.text.splitlines(keepends=True)
        if not lines:
            return None
        matching = [
            index
            for index, line in enumerate(lines)
            if any(term in line.lower() for term in terms)
        ]
        start = max(0, matching[0] - 20) if matching else 0
        end = min(len(lines), start + 160)
        window = "".join(lines[start:end])
        encoded = window.encode("utf-8")
        allowed = min(byte_limit, len(encoded))
        content = encoded[:allowed].decode("utf-8", errors="ignore")
        content_bytes = len(content.encode("utf-8"))
        if not content or content_bytes == 0:
            return None
        line_count = max(1, len(content.splitlines()))
        truncated = start > 0 or end < len(lines) or content_bytes < len(encoded)
        return ContextExcerpt(
            path=candidate.relative,
            start_line=start + 1,
            end_line=start + line_count,
            content=content,
            content_hash=hashlib.sha256(candidate.data).hexdigest(),
            truncated=truncated,
            utf8_bytes=content_bytes,
        )
