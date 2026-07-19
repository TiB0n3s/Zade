from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any

from .config import KernelConfig
from .db import KernelDatabase, utc_now
from .project_manifest import ProjectManifest, load_project_manifest


class ProjectIntakeService:
    """Discover and advance durable project folders behind one small interface."""

    def __init__(
        self,
        *,
        config: KernelConfig,
        db: KernelDatabase,
        ingestion: Any | None = None,
        delegation: Any | None = None,
        bus: Any | None = None,
    ):
        self.config = config
        self.db = db
        self.ingestion = ingestion
        self.delegation = delegation
        self.bus = bus
        self.root = config.paths.project_intake_dir.resolve()

    def scan(self, *, auto_run: bool = True) -> dict[str, Any]:
        if not self.config.project_intake.enabled:
            return {"created_count": 0, "existing_count": 0, "projects": [], "errors": []}
        self.root.mkdir(parents=True, exist_ok=True)
        created = 0
        existing = 0
        projects: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for candidate in sorted(self.root.iterdir(), key=lambda item: item.name.casefold()):
            if not candidate.is_dir() or candidate.is_symlink():
                continue
            resolved = candidate.resolve()
            if resolved.parent != self.root:
                continue
            if not ((resolved / ".git").is_dir() or (resolved / "project.md").is_file()):
                continue
            try:
                prior = self.db.find_project_by_path(str(resolved))
                project = self._register(resolved)
                if prior is None:
                    created += 1
                else:
                    existing += 1
                if auto_run and project["metadata"].get("scaffold_on_intake"):
                    project = self.run_until_blocked(project["id"])
                projects.append(project)
            except Exception as exc:  # noqa: BLE001 - one bad intake must not stop siblings
                errors.append({"path": str(resolved), "error": str(exc)[:400]})
        return {
            "created_count": created,
            "existing_count": existing,
            "projects": projects,
            "errors": errors,
        }

    def get(self, project_id: int) -> dict[str, Any]:
        project = self.db.get_project(project_id)
        if project is None:
            raise ValueError(f"Project not found: {project_id}")
        return project

    def run_until_blocked(self, project_id: int) -> dict[str, Any]:
        project = self.get(project_id)
        root = self._validated_project_root(project["canonical_path"])
        manifest = load_project_manifest(root / "project.md")
        if not (root / ".git").is_dir():
            self._initialize_git(root)
            self.db.append_project_event(project_id, event_type="repository_initialized")
        if self.delegation is None:
            return self._set_state(project, manifest, "blocked", {"reason": "delegation unavailable"})
        context = self._documentation_context(root)
        result = self.delegation.queue_delegation(
            task=(
                f"Create and build the initial {manifest.product_type.replace('_', ' ')} "
                f"for {manifest.name} from its founder-authored project documentation"
            ),
            context=context,
            acceptance=(
                "Create a runnable mobile application scaffold from scratch; preserve the product definition; "
                "make Android/Google Play the first distribution target; retain eventual Apple App Store intent; "
                "run the available project-local checks and report real output."
            ),
            auto_invoke=None,
            workspace=str(root),
            directed=True,
        )
        dispatch = result.get("dispatch") if isinstance(result.get("dispatch"), dict) else {}
        if result.get("auto_invoked") and dispatch.get("ok"):
            state = "verified"
        elif dispatch.get("status") in {"approval_required", "needs_decision"}:
            state = "blocked"
        else:
            state = "building" if result.get("auto_invoked") else "blocked"
        self.db.append_project_event(
            project_id,
            event_type="build_routed",
            work_item_id=result.get("item_id"),
            approval_request_id=dispatch.get("approval_request_id"),
            metadata={"state": state, "result_status": result.get("status")},
        )
        return self._set_state(project, manifest, state, {"last_build_route": result})

    def resolve_decision(self, decision_id: int, answer: str) -> dict[str, Any]:
        if not answer.strip():
            raise ValueError("Decision answer must not be empty.")
        for project in self.db.list_projects(limit=500):
            metadata = project.get("metadata") or {}
            if int(metadata.get("decision_id") or 0) == int(decision_id):
                self.db.append_project_event(
                    project["id"], event_type="decision_resolved", detail=answer.strip(), metadata={"decision_id": decision_id}
                )
                return self.run_until_blocked(project["id"])
        raise ValueError(f"Project decision not found: {decision_id}")

    def _register(self, root: Path) -> dict[str, Any]:
        manifest_path = root / "project.md"
        if not manifest_path.is_file():
            raise ValueError(f"Registered projects require project.md: {root}")
        manifest = load_project_manifest(manifest_path)
        fingerprint = self._fingerprint(root)
        project_id = self.db.upsert_project(
            canonical_path=str(root),
            name=manifest.name,
            product_type=manifest.product_type,
            distribution_targets=list(manifest.distribution_targets),
            lifecycle_state=manifest.lifecycle_state,
            repo_fingerprint=fingerprint,
            metadata={"scaffold_on_intake": manifest.scaffold_on_intake},
            last_scanned_at=utc_now(),
        )
        self.db.append_project_event(project_id, event_type="discovered", metadata={"fingerprint": fingerprint})
        self._ingest_documentation(project_id, root)
        return self.get(project_id)

    def _ingest_documentation(self, project_id: int, root: Path) -> None:
        if self.ingestion is None:
            return
        for path in sorted(root.iterdir()):
            if path.is_file() and path.suffix.lower() in {".md", ".txt", ".csv", ".json", ".yaml", ".yml"}:
                self.ingestion.ingest_file(
                    path=path,
                    metadata={"project_id": project_id, "project_name": root.name, "intake_source": True},
                )

    def _validated_project_root(self, raw: str) -> Path:
        root = Path(raw).resolve()
        if root.parent != self.root or root.is_symlink():
            raise ValueError(f"Project must be a registered direct child of {self.root}: {root}")
        return root

    @staticmethod
    def _initialize_git(root: Path) -> None:
        result = subprocess.run(
            ["git", "init", "--initial-branch=main"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git init failed: {(result.stderr or result.stdout)[:300]}")

    @staticmethod
    def _documentation_context(root: Path) -> str:
        sections = []
        for path in sorted(root.glob("*.md")):
            sections.append(f"## {path.name}\n{path.read_text(encoding='utf-8-sig', errors='replace')[:12000]}")
        return "\n\n".join(sections)[:30000]

    @staticmethod
    def _fingerprint(root: Path) -> str:
        digest = hashlib.sha256()
        for path in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
            if path.name in {".git", "node_modules", ".zade"}:
                continue
            digest.update(path.name.encode("utf-8", errors="replace"))
            if path.is_file():
                digest.update(str(path.stat().st_size).encode("ascii"))
                if path.suffix.lower() in {".md", ".txt", ".json", ".yaml", ".yml"}:
                    digest.update(path.read_bytes())
        return digest.hexdigest()

    def _set_state(
        self, project: dict[str, Any], manifest: ProjectManifest, state: str, metadata_update: dict[str, Any]
    ) -> dict[str, Any]:
        metadata = dict(project.get("metadata") or {})
        metadata.update(metadata_update)
        self.db.upsert_project(
            canonical_path=project["canonical_path"],
            name=manifest.name,
            product_type=manifest.product_type,
            distribution_targets=list(manifest.distribution_targets),
            lifecycle_state=state,
            repo_fingerprint=self._fingerprint(Path(project["canonical_path"])),
            metadata=metadata,
            active_build_session_id=project.get("active_build_session_id"),
            last_scanned_at=utc_now(),
        )
        return self.get(project["id"])
