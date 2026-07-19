"""Zero-paid-token build complexity assessment."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from .build_types import BuildAssessment, BuildTier
from .build_workspace import BuildWorkspacePolicy
from .db import utc_now
from .ollama import OllamaError


_MAX_FILES = 5_000
_MAX_MANIFEST_BYTES = 1_000_000
_SKIP_DIRECTORIES = {
    ".git",
    ".hg",
    ".idea",
    ".mypy_cache",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    ".vscode",
    "__pycache__",
    "build",
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
    ".a",
    ".avi",
    ".bin",
    ".bmp",
    ".class",
    ".dll",
    ".dylib",
    ".eot",
    ".exe",
    ".gif",
    ".gz",
    ".ico",
    ".jar",
    ".jpeg",
    ".jpg",
    ".lockb",
    ".mov",
    ".mp3",
    ".mp4",
    ".o",
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
_LOCAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["score_adjustment", "confidence", "reasons", "unknowns"],
    "properties": {
        "score_adjustment": {"type": "integer", "minimum": -100, "maximum": 30},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reasons": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "unknowns": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
    },
}
_TIER_RANK = {BuildTier.SMALL: 0, BuildTier.MEDIUM: 1, BuildTier.LARGE: 2}


class LocalAssessmentClient(Protocol):
    def chat(self, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class _RepositoryEvidence:
    fingerprint: str
    file_count: int
    total_bytes: int
    truncated: bool
    scanned_paths: tuple[str, ...]
    extensions: Mapping[str, int]
    manifests: Mapping[str, Any]
    frameworks: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "truncated": self.truncated,
            "scanned_paths": list(self.scanned_paths),
            "extensions": dict(self.extensions),
            "manifests": dict(self.manifests),
            "frameworks": list(self.frameworks),
        }


@dataclass(frozen=True)
class _LocalAdjustment:
    points: int
    confidence: float
    reasons: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()


class BuildAssessmentService:
    """Assess build scope from local evidence without authorizing cloud use."""

    def __init__(
        self,
        *,
        local_client: LocalAssessmentClient | None = None,
        workspace_policy: BuildWorkspacePolicy | None = None,
    ):
        self._local_client = local_client
        self._workspace_policy = workspace_policy

    def assess(
        self,
        *,
        task: str,
        workspace: str | Path,
        acceptance: str = "",
    ) -> BuildAssessment:
        normalized_task = " ".join(task.split())
        normalized_acceptance = " ".join(acceptance.split())
        root = (
            self._workspace_policy.validate(workspace)
            if self._workspace_policy is not None
            else Path(workspace).resolve()
        )
        evidence = self._scan(root)
        dimensions, floors = self._score(
            normalized_task, normalized_acceptance, evidence
        )
        deterministic_score = min(100, sum(dimensions.values()))
        adjustment = self._local_adjustment(
            normalized_task, normalized_acceptance, evidence, dimensions
        )
        local_points = max(0, adjustment.points)
        final_score = min(100, deterministic_score + local_points)
        tier = max(
            _tier_for_score(final_score),
            _floor_tier(floors),
            key=_TIER_RANK.__getitem__,
        )
        if adjustment.confidence < 0.65:
            tier = _raise_one(tier)

        cloud_reasons = adjustment.reasons
        if not cloud_reasons and floors:
            cloud_reasons = tuple(
                f"Review risk covered by floor rule: {rule}" for rule in floors
            )

        return BuildAssessment(
            id=None,
            task=normalized_task,
            acceptance=normalized_acceptance,
            workspace=str(root),
            repo_fingerprint=evidence.fingerprint,
            deterministic_score=deterministic_score,
            local_adjustment=local_points,
            final_score=final_score,
            confidence=adjustment.confidence,
            recommended_tier=tier,
            dimensions=dimensions,
            floor_rules=tuple(floors),
            evidence=evidence.as_dict(),
            unknowns=adjustment.unknowns,
            local_work=(
                "repository inventory and task decomposition",
                "routine edits and local verification",
                "checkpoint and acceptance evidence collection",
            ),
            cloud_reasons=cloud_reasons,
            created_at=utc_now(),
        )

    def _scan(self, root: Path) -> _RepositoryEvidence:
        if not root.exists() or not root.is_dir():
            raise ValueError(f"Build workspace is not a directory: {root}")

        digest = hashlib.sha256()
        paths: list[str] = []
        extensions: dict[str, int] = {}
        manifests: dict[str, Any] = {}
        total_bytes = 0
        truncated = False

        for current, directories, files in os.walk(root):
            directories[:] = sorted(
                name
                for name in directories
                if name.lower() not in _SKIP_DIRECTORIES
                and not (Path(current) / name).is_symlink()
            )
            for name in sorted(files):
                path = Path(current) / name
                if len(paths) >= _MAX_FILES:
                    truncated = True
                    directories[:] = []
                    break
                if self._skip_file(path):
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                relative = path.relative_to(root).as_posix()
                paths.append(relative)
                total_bytes += stat.st_size
                suffix = path.suffix.lower() or "[none]"
                extensions[suffix] = extensions.get(suffix, 0) + 1
                digest.update(
                    f"{relative}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode("utf-8")
                )
                if relative == "package.json":
                    parsed = self._parse_package_json(path, stat.st_size)
                    if parsed is not None:
                        manifests[relative] = parsed
                elif relative == "pyproject.toml":
                    parsed = self._parse_pyproject(path, stat.st_size)
                    if parsed is not None:
                        manifests[relative] = parsed
            if truncated:
                break

        return _RepositoryEvidence(
            fingerprint=digest.hexdigest(),
            file_count=len(paths),
            total_bytes=total_bytes,
            truncated=truncated,
            scanned_paths=tuple(paths),
            extensions=dict(sorted(extensions.items())),
            manifests=manifests,
            frameworks=_infer_frameworks(manifests),
        )

    @staticmethod
    def _skip_file(path: Path) -> bool:
        name = path.name.lower()
        if path.is_symlink() or path.suffix.lower() in _BINARY_SUFFIXES:
            return True
        if name in _SECRET_NAMES or name.startswith(".env."):
            return True
        return any(token in name for token in ("secret", "credential", "private_key"))

    @staticmethod
    def _parse_package_json(path: Path, size: int) -> dict[str, Any] | None:
        if size > _MAX_MANIFEST_BYTES:
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        dependencies: set[str] = set()
        for key in ("dependencies", "devDependencies", "peerDependencies"):
            group = payload.get(key)
            if isinstance(group, dict):
                dependencies.update(str(name) for name in group)
        scripts = payload.get("scripts")
        return {
            "dependencies": sorted(dependencies)[:250],
            "scripts": sorted(str(name) for name in scripts)[:100]
            if isinstance(scripts, dict)
            else [],
        }

    @staticmethod
    def _parse_pyproject(path: Path, size: int) -> dict[str, Any] | None:
        if size > _MAX_MANIFEST_BYTES:
            return None
        try:
            with path.open("rb") as handle:
                payload = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError):
            return None
        dependencies: set[str] = set()
        project = payload.get("project")
        if isinstance(project, dict):
            for dependency in project.get("dependencies", []):
                if isinstance(dependency, str):
                    dependencies.add(_dependency_name(dependency))
        tool = payload.get("tool")
        if isinstance(tool, dict):
            poetry = tool.get("poetry")
            if isinstance(poetry, dict) and isinstance(poetry.get("dependencies"), dict):
                dependencies.update(str(name) for name in poetry["dependencies"])
        return {"dependencies": sorted(dependencies)[:250]}

    def _score(
        self,
        task: str,
        acceptance: str,
        evidence: _RepositoryEvidence,
    ) -> tuple[dict[str, int], list[str]]:
        text = f"{task} {acceptance}".lower()
        dependencies = {
            dependency.lower()
            for manifest in evidence.manifests.values()
            for dependency in manifest.get("dependencies", [])
        }
        has = lambda *terms: _contains(text, *terms)

        ui = has("ui", "frontend", "web app", "dashboard") or bool(
            dependencies & {"react", "vue", "svelte", "next", "next.js"}
        )
        backend = has("api", "backend", "server") or bool(
            dependencies & {"fastapi", "django", "flask", "express", "nestjs"}
        )
        workers = has("worker", "queue", "scheduled job", "background job")
        admin = has("admin", "back office")
        analytics = has("analytics", "reporting", "metrics")
        notifications = has("notification", "email", "sms", "push")
        product_surfaces = min(
            20,
            4 * int(ui)
            + 4 * int(backend)
            + 3 * int(workers)
            + 3 * int(admin)
            + 3 * int(analytics)
            + 3 * int(notifications),
        )

        auth = has(
            "auth",
            "authentication",
            "authorization",
            "oauth",
            "identity",
            "permission",
        )
        payments = has("payment", "billing", "stripe", "subscription", "in-app purchase")
        vendors = has("integration", "vendor", "webhook", "third-party", "third party")
        native_api = has("native module", "camera", "bluetooth", "location services")
        external_integrations = min(
            15,
            5 * int(payments)
            + 4 * int(auth)
            + 3 * int(vendors)
            + 3 * int(native_api),
        )

        saas = has("saas", "software as a service")
        greenfield = has("greenfield", "from scratch") or (
            evidence.file_count == 0 and has("build", "create") and (saas or backend)
        )
        surface_count = sum((ui, backend, workers, admin, analytics, notifications))
        change_breadth = min(
            15,
            5 * int(greenfield)
            + min(5, max(0, surface_count - 1) * 2)
            + (1 if evidence.file_count >= 25 else 0)
            + (2 if evidence.file_count >= 100 else 0)
            + (2 if evidence.file_count >= 500 else 0),
        )

        tenancy = has("multi-tenant", "multitenant", "multitenancy", "tenant isolation")
        migration = has("migration", "schema change", "backfill")
        sensitive = has("sensitive data", "pii", "security", "encryption", "compliance")
        data_and_security = min(
            15,
            5 * int(auth) + 4 * int(tenancy) + 3 * int(migration) + 3 * int(sensitive),
        )

        ios = has("ios", "iphone", "ipad")
        android = has("android")
        mobile = ios or android or has("mobile app", "mobile client", "cross-platform mobile")
        release = has("release", "app store", "play store", "signing", "production")
        infrastructure = has("infrastructure", "deploy", "kubernetes", "terraform", "ci/cd")
        platform_and_release = min(
            15,
            3 * int(ui)
            + 5 * int(ios)
            + 5 * int(android)
            + 2 * int(release or infrastructure),
        )

        verification_burden = min(
            10,
            2 * int(has("test", "acceptance", "e2e", "end-to-end"))
            + 2 * int(auth or payments)
            + 2 * int(mobile)
            + 2 * int(migration)
            + 2 * int(release or infrastructure),
        )
        novelty_and_ambiguity = min(
            10,
            4 * int(has("research", "unknown", "unfamiliar", "prototype"))
            + 2 * int(greenfield)
            + 2 * int(has("new architecture", "novel"))
            + int(not acceptance),
        )

        dimensions = {
            "product_surfaces": product_surfaces,
            "external_integrations": external_integrations,
            "change_breadth": change_breadth,
            "data_and_security": data_and_security,
            "platform_and_release": platform_and_release,
            "verification_burden": verification_burden,
            "novelty_and_ambiguity": novelty_and_ambiguity,
        }

        medium_risks: list[str] = []
        if auth:
            medium_risks.append("production_authentication_or_authorization")
        if payments:
            medium_risks.append("payments_or_billing")
        if tenancy:
            medium_risks.append("multitenancy")
        if migration:
            medium_risks.append("database_migration")
        if mobile and backend:
            medium_risks.append("mobile_client_plus_backend")
        if ios and android:
            medium_risks.append("simultaneous_ios_android")
        if has("store billing", "custom native module", "offline sync", "offline synchronization"):
            medium_risks.append("high_risk_mobile_release")

        large_risks: list[str] = []
        cross_platform_mobile = (ios and android) or has(
            "cross-platform mobile", "mobile clients"
        )
        if greenfield and saas and backend and cross_platform_mobile:
            large_risks.append("greenfield_saas_plus_mobile")
        production_migration = migration and has("production", "live data")
        cross_system = vendors or has("cross-system", "cross system")
        if production_migration and (sensitive or auth or cross_system):
            large_risks.append("production_migration_plus_security_or_cross_system")
        if len(set(medium_risks)) >= 3:
            large_risks.append("three_or_more_medium_release_risks")
        return dimensions, medium_risks + large_risks

    def _local_adjustment(
        self,
        task: str,
        acceptance: str,
        evidence: _RepositoryEvidence,
        dimensions: Mapping[str, int],
    ) -> _LocalAdjustment:
        if self._local_client is None:
            return _LocalAdjustment(points=0, confidence=0.75)

        compact_evidence = {
            "task": task,
            "acceptance": acceptance,
            "dimensions": dict(dimensions),
            "file_count": evidence.file_count,
            "truncated": evidence.truncated,
            "extensions": dict(evidence.extensions),
            "frameworks": list(evidence.frameworks),
            "manifest_names": sorted(evidence.manifests),
        }
        try:
            result = self._local_client.chat(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Assess only build complexity risks omitted by deterministic scoring. "
                            "Return JSON matching the supplied schema. A negative adjustment will "
                            "be ignored. Do not recommend providers or budgets."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(compact_evidence, sort_keys=True),
                    },
                ],
                think=False,
                temperature=0,
                num_predict=400,
                format=_LOCAL_SCHEMA,
            )
            payload = json.loads(result.response)
            return _parse_local_adjustment(payload)
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError, OllamaError):
            return _LocalAdjustment(
                points=0,
                confidence=0.5,
                unknowns=("Local complexity interpretation was unavailable or invalid.",),
            )


def _parse_local_adjustment(payload: Any) -> _LocalAdjustment:
    if not isinstance(payload, dict) or set(payload) != {
        "score_adjustment",
        "confidence",
        "reasons",
        "unknowns",
    }:
        raise ValueError("Invalid local assessment shape")
    points = payload["score_adjustment"]
    confidence = payload["confidence"]
    reasons = payload["reasons"]
    unknowns = payload["unknowns"]
    if isinstance(points, bool) or not isinstance(points, int):
        raise ValueError("score_adjustment must be an integer")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("confidence must be numeric")
    if not 0 <= float(confidence) <= 1:
        raise ValueError("confidence is outside 0..1")
    if not _string_list(reasons) or not _string_list(unknowns):
        raise ValueError("reasons and unknowns must be string lists")
    return _LocalAdjustment(
        points=max(-100, min(30, points)),
        confidence=float(confidence),
        reasons=tuple(reasons[:8]),
        unknowns=tuple(unknowns[:8]),
    )


def _string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _dependency_name(specification: str) -> str:
    match = re.match(r"[A-Za-z0-9_.-]+", specification.strip())
    return match.group(0) if match else specification.strip()


def _infer_frameworks(manifests: Mapping[str, Any]) -> tuple[str, ...]:
    known = {
        "django",
        "express",
        "fastapi",
        "flask",
        "flutter",
        "next",
        "react",
        "react-native",
        "sqlalchemy",
        "svelte",
        "vue",
    }
    dependencies = {
        str(dependency).lower()
        for manifest in manifests.values()
        for dependency in manifest.get("dependencies", [])
    }
    return tuple(sorted(known & dependencies))


def _contains(text: str, *terms: str) -> bool:
    return any(re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) for term in terms)


def _tier_for_score(score: int) -> BuildTier:
    if score >= 65:
        return BuildTier.LARGE
    if score >= 30:
        return BuildTier.MEDIUM
    return BuildTier.SMALL


def _floor_tier(floors: list[str]) -> BuildTier:
    if any(
        floor
        in {
            "greenfield_saas_plus_mobile",
            "production_migration_plus_security_or_cross_system",
            "three_or_more_medium_release_risks",
        }
        for floor in floors
    ):
        return BuildTier.LARGE
    if floors:
        return BuildTier.MEDIUM
    return BuildTier.SMALL


def _raise_one(tier: BuildTier) -> BuildTier:
    if tier is BuildTier.SMALL:
        return BuildTier.MEDIUM
    return BuildTier.LARGE
