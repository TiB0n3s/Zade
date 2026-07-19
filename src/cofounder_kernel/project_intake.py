from __future__ import annotations

import hashlib
import re
import shutil
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
        approvals: Any | None = None,
    ):
        self.config = config
        self.db = db
        self.ingestion = ingestion
        self.delegation = delegation
        self.bus = bus
        self.approvals = approvals
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
                project = self._register(resolved, prior=prior)
                if prior is None:
                    created += 1
                else:
                    existing += 1
                should_auto_run = (
                    auto_run
                    and project["metadata"].get("scaffold_on_intake")
                    and project["lifecycle_state"] in {"intake", "discovered"}
                    and not project["metadata"].get("last_build_route")
                )
                if should_auto_run:
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
        context = (
            f"{self._documentation_context(root)}\n\n"
            f"{self._mobile_tooling_context(manifest.name)}"
        ).strip()
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
        return self._record_build_route(project, manifest, result)

    def resolve_decision(
        self,
        decision_id: int,
        answer: str,
        *,
        resolved_by: str = "founder.telegram",
    ) -> dict[str, Any]:
        if not answer.strip():
            raise ValueError("Decision answer must not be empty.")
        if self.approvals is None:
            raise ValueError("Project decision approval service is unavailable.")
        for project in self.db.list_projects(limit=500):
            metadata = project.get("metadata") or {}
            if int(metadata.get("decision_id") or 0) == int(decision_id):
                item = self.db.get_work_item(decision_id)
                if item is None or item.kind != "founder_decision":
                    raise ValueError(f"Project decision work item not found: {decision_id}")
                root = self._validated_project_root(project["canonical_path"])
                item_workspace = Path(str(item.metadata.get("workspace") or "")).resolve()
                if item_workspace != root:
                    raise ValueError(
                        f"Project decision {decision_id} does not belong to {project['name']}."
                    )
                clean_answer = answer.strip()
                prior_brief = str(item.metadata.get("brief") or "").rstrip()
                self.db.update_work_item_proposal(
                    decision_id,
                    metadata={
                        "brief": (
                            f"{prior_brief}\n\n## Founder answer\n{clean_answer}\n"
                            "Continue the paused project build using this answer."
                        ),
                        "founder_answer": clean_answer,
                        "founder_answered_by": resolved_by,
                    },
                )
                self.db.append_project_event(
                    project["id"],
                    event_type="decision_resolved",
                    detail=clean_answer,
                    work_item_id=decision_id,
                    metadata={"decision_id": decision_id, "resolved_by": resolved_by},
                )
                resumed = self.approvals.approve_work_item(
                    decision_id,
                    resolved_by=resolved_by,
                    note=clean_answer,
                    dispatch=True,
                    typed_confirmation="",
                )
                dispatch = (
                    resumed.get("dispatch_result")
                    if isinstance(resumed.get("dispatch_result"), dict)
                    else {}
                )
                manifest = load_project_manifest(root / "project.md")
                route = {
                    "item_id": decision_id,
                    "status": "resumed",
                    "auto_invoked": True,
                    "dispatch": dispatch,
                    "approval_dispatch": resumed.get("dispatch"),
                }
                return self._record_build_route(project, manifest, route)
        raise ValueError(f"Project decision not found: {decision_id}")

    def _record_build_route(
        self,
        project: dict[str, Any],
        manifest: ProjectManifest,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        dispatch = result.get("dispatch") if isinstance(result.get("dispatch"), dict) else {}
        dispatch_status = str(dispatch.get("status") or "").strip().lower()
        decision_id = _positive_int(dispatch.get("decision_item_id"))
        approval_request_id = _positive_int(dispatch.get("approval_request_id"))
        metadata_update: dict[str, Any] = {"last_build_route": result}
        notification_id: int | None = None
        verification_blocked = False

        if dispatch_status == "needs_decision":
            state = "blocked"
            question = dispatch.get("founder_question")
            question = question if isinstance(question, dict) else {}
            metadata_update.update(
                {
                    "decision_id": decision_id,
                    "founder_question": question,
                }
            )
        elif dispatch_status == "approval_required":
            state = "blocked"
            metadata_update["approval_request_id"] = approval_request_id
        elif (
            bool(result.get("auto_invoked"))
            and bool(dispatch.get("ok"))
            and _dispatch_verified(dispatch)
        ):
            state = "verified"
            metadata_update.update(
                {
                    "decision_id": None,
                    "founder_question": None,
                    "approval_request_id": None,
                }
            )
        elif bool(result.get("auto_invoked")) and bool(dispatch.get("ok")):
            state = "blocked"
            verification_blocked = True
            metadata_update["blocked_reason"] = _verification_block_reason(dispatch)
        else:
            state = "blocked"
            metadata_update["blocked_reason"] = str(
                dispatch.get("error") or result.get("reason") or dispatch_status or "build did not run"
            )[:400]

        updated = self._set_state(project, manifest, state, metadata_update)
        if dispatch_status == "needs_decision" and decision_id is not None:
            notification_id = self._notify_founder_decision(updated, decision_id, dispatch)
        elif dispatch_status == "approval_required" and approval_request_id is not None:
            notification_id = self._notify_approval_required(updated, approval_request_id)
        elif verification_blocked:
            notification_id = self._notify_build_blocked(updated)

        self.db.append_project_event(
            project["id"],
            event_type="build_routed",
            work_item_id=_positive_int(result.get("item_id")),
            approval_request_id=approval_request_id,
            notification_id=notification_id,
            metadata={"state": state, "dispatch_status": dispatch_status},
        )
        return self.get(project["id"])

    def _notify_founder_decision(
        self,
        project: dict[str, Any],
        decision_id: int,
        dispatch: dict[str, Any],
    ) -> int | None:
        if self.bus is None:
            return None
        question_info = dispatch.get("founder_question")
        question_info = question_info if isinstance(question_info, dict) else {}
        question = str(question_info.get("question") or "Zade needs your direction to continue.").strip()
        options = [str(item).strip() for item in question_info.get("options", []) if str(item).strip()]
        options_block = "\n\nOptions:\n" + "\n".join(f"- {item}" for item in options) if options else ""
        notification = self.bus.notify(
            topic="project.decision_required",
            title=f"{project['name']} needs a decision",
            body=(
                f"{question}{options_block}\n\n"
                f"Reply exactly: decision {decision_id}: <your answer>"
            ),
            severity="warning",
            source="project_intake",
            dedupe_key=f"project:{project['id']}:decision:{decision_id}",
            metadata={"project_id": project["id"], "decision_id": decision_id},
        )
        return _positive_int(notification.get("id"))

    def _notify_approval_required(
        self, project: dict[str, Any], approval_request_id: int
    ) -> int | None:
        if self.bus is None:
            return None
        notification = self.bus.notify(
            topic="project.approval_required",
            title=f"{project['name']} is waiting for approval",
            body=(
                f"Build approval {approval_request_id} is required before Zade can continue. "
                "Open Zade's approval console to review and decide."
            ),
            severity="warning",
            source="project_intake",
            dedupe_key=f"project:{project['id']}:approval:{approval_request_id}",
            metadata={"project_id": project["id"], "approval_request_id": approval_request_id},
        )
        return _positive_int(notification.get("id"))

    def _notify_build_blocked(self, project: dict[str, Any]) -> int | None:
        if self.bus is None:
            return None
        reason = str(
            (project.get("metadata") or {}).get("blocked_reason")
            or "verification did not pass"
        )
        notification = self.bus.notify(
            topic="project.build_blocked",
            title=f"{project['name']} scaffold needs attention",
            body=(
                f"Zade created project files, but did not mark them verified: {reason}. "
                "The project remains paused until a real local check passes."
            ),
            severity="warning",
            source="project_intake",
            dedupe_key=f"project:{project['id']}:build-blocked:{project['repo_fingerprint']}",
            metadata={"project_id": project["id"], "reason": reason},
        )
        return _positive_int(notification.get("id"))

    def _register(
        self, root: Path, *, prior: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        manifest_path = root / "project.md"
        if not manifest_path.is_file():
            raise ValueError(f"Registered projects require project.md: {root}")
        manifest = load_project_manifest(manifest_path)
        fingerprint = self._fingerprint(root)
        prior_metadata = dict(prior.get("metadata") or {}) if prior else {}
        project_id = self.db.upsert_project(
            canonical_path=str(root),
            name=manifest.name,
            product_type=manifest.product_type,
            distribution_targets=list(manifest.distribution_targets),
            lifecycle_state=(prior["lifecycle_state"] if prior else manifest.lifecycle_state),
            repo_fingerprint=fingerprint,
            metadata={**prior_metadata, "scaffold_on_intake": manifest.scaffold_on_intake},
            active_build_session_id=(prior.get("active_build_session_id") if prior else None),
            last_scanned_at=utc_now(),
        )
        if prior is None:
            self.db.append_project_event(
                project_id, event_type="discovered", metadata={"fingerprint": fingerprint}
            )
        elif prior.get("repo_fingerprint") != fingerprint:
            self.db.append_project_event(
                project_id,
                event_type="source_updated",
                metadata={"prior_fingerprint": prior.get("repo_fingerprint"), "fingerprint": fingerprint},
            )
        self._ingest_documentation(project_id, root)
        registered = self.get(project_id)
        route = (registered.get("metadata") or {}).get("last_build_route")
        route = route if isinstance(route, dict) else {}
        dispatch = route.get("dispatch") if isinstance(route.get("dispatch"), dict) else {}
        if (
            prior is not None
            and registered["lifecycle_state"] == "verified"
            and dispatch.get("ok")
            and not _dispatch_verified(dispatch)
        ):
            registered = self._set_state(
                registered,
                manifest,
                "blocked",
                {"blocked_reason": _verification_block_reason(dispatch)},
            )
            notification_id = self._notify_build_blocked(registered)
            self.db.append_project_event(
                project_id,
                event_type="verification_state_corrected",
                notification_id=notification_id,
                metadata={"state": "blocked"},
            )
        return registered

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
    def _mobile_tooling_context(project_name: str) -> str:
        tool_names = ("node", "npm", "npx", "java", "gradle", "adb", "flutter", "dart")
        lines = [
            "## Verified local mobile tooling",
            "Do not choose a framework whose required local toolchain is unavailable.",
        ]
        for name in tool_names:
            resolved = _mobile_tool_path(name)
            state = f"available ({resolved})" if resolved else "unavailable"
            lines.append(f"- {name}: {state}")
        if _mobile_tool_path("flutter"):
            flutter_name = _flutter_project_name(project_name)
            lines.extend(
                [
                    "- Approved offline Flutter bootstrap: "
                    f"flutter create --no-pub --project-name {flutter_name} .",
                    "- Approved offline Flutter dependency resolution: flutter pub get --offline",
                ]
            )
        return "\n".join(lines)

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
        metadata = {key: value for key, value in metadata.items() if value is not None}
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


_DECISION_REPLY_RE = re.compile(
    r"^\s*/?decision\s+#?(?P<decision_id>\d+)\s*(?::|-)\s*(?P<answer>.+?)\s*$",
    flags=re.IGNORECASE | re.DOTALL,
)


def parse_project_decision_reply(text: str) -> tuple[int, str] | None:
    """Parse the explicit founder reply syntax used in proactive notifications."""
    match = _DECISION_REPLY_RE.match(str(text or ""))
    if match is None:
        return None
    answer = match.group("answer").strip()
    if not answer:
        return None
    return int(match.group("decision_id")), answer


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _dispatch_verified(dispatch: dict[str, Any]) -> bool:
    verification = dispatch.get("auto_verification")
    if not isinstance(verification, dict) or verification.get("ok") is not True:
        return False
    verifier = dispatch.get("verifier_review")
    if isinstance(verifier, dict) and str(verifier.get("verdict") or "").lower() == "fail":
        return False
    return True


def _mobile_tool_path(name: str) -> str | None:
    resolved = shutil.which(name)
    if resolved:
        return resolved
    known = {
        "flutter": Path(r"C:\tools\flutter\bin\flutter.bat"),
        "dart": Path(r"C:\tools\flutter\bin\dart.bat"),
    }.get(name)
    return str(known) if known is not None and known.is_file() else None


def _flutter_project_name(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")
    if not normalized or not normalized[0].isalpha():
        normalized = f"app_{normalized}" if normalized else "mobile_app"
    return normalized[:64]


def _verification_block_reason(dispatch: dict[str, Any]) -> str:
    verifier = dispatch.get("verifier_review")
    if isinstance(verifier, dict) and str(verifier.get("verdict") or "").lower() == "fail":
        return "fresh-context verifier failed"
    verification = dispatch.get("auto_verification")
    if isinstance(verification, dict) and verification.get("ok") is False:
        return "kernel auto-verification did not pass"
    return "no runnable verification passed"
