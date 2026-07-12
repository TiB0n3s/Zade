from __future__ import annotations

import re
import webbrowser
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .config import KernelConfig
from .db import KernelDatabase, WorkItem


Handler = Callable[[WorkItem], dict[str, Any]]


class ActionHandlerRegistry:
    def __init__(self, *, db: KernelDatabase, config: KernelConfig):
        self.db = db
        self.config = config
        self._handlers: dict[str, tuple[str, Handler]] = {
            "local.noop": ("Record a successful no-op dispatch for smoke tests and approval flow checks.", self._noop),
            "local.audit.record": ("Write an audit event using work-item metadata.", self._audit_record),
            "local.memory.write": ("Write a local memory from approved work-item content.", self._memory_write),
            "local.file.write": ("Write or append a file under configured local memory/data roots.", self._file_write),
            "local.report.write": ("Write a markdown report under the local Zade reports folder.", self._report_write),
            "local.vault.organize": ("Write a vault organization plan under the local AI Brain root.", self._vault_organize),
            "local.browser.open": ("Prepare or open a browser target after approval.", self._browser_open),
        }

    def register(self, action: str, description: str, handler: Handler) -> None:
        """Register an additional approved dispatch handler (e.g. connector sync)."""
        self._handlers[action] = (description, handler)

    def list_handlers(self) -> list[dict[str, str]]:
        return [
            {"action": action, "description": description}
            for action, (description, _handler) in sorted(self._handlers.items())
        ]

    def can_dispatch(self, action: str) -> bool:
        return action in self._handlers

    def dispatch(self, item: WorkItem) -> dict[str, Any]:
        entry = self._handlers.get(item.action)
        if not entry:
            raise ValueError(f"No approved local handler registered for action: {item.action}")
        _description, handler = entry
        return handler(item)

    def _noop(self, item: WorkItem) -> dict[str, Any]:
        return {
            "handler": "local.noop",
            "status": "ok",
            "work_item": _work_item_summary(item),
        }

    def _audit_record(self, item: WorkItem) -> dict[str, Any]:
        metadata = item.metadata or {}
        audit_id = self.db.audit(
            actor=str(metadata.get("actor") or "approved-handler"),
            action=str(metadata.get("audit_action") or item.action),
            target=str(metadata.get("audit_target") or item.target or f"work_item:{item.id}"),
            permission_tier=item.permission_tier,
            status=str(metadata.get("status") or "ok"),
            details={
                "work_item": _work_item_summary(item),
                "handler_metadata": metadata,
            },
        )
        return {"handler": "local.audit.record", "status": "ok", "audit_id": audit_id}

    def _memory_write(self, item: WorkItem) -> dict[str, Any]:
        metadata = item.metadata or {}
        content = str(metadata.get("content") or item.detail).strip()
        if not content:
            raise ValueError("local.memory.write requires content in metadata.content or work item detail.")
        memory_id = self.db.add_memory(
            kind=str(metadata.get("kind") or "approved_action"),
            title=str(metadata.get("memory_title") or item.title),
            content=content,
            source=str(metadata.get("source") or "approved-handler"),
            metadata={
                "approved_work_item": _work_item_summary(item),
                "handler_metadata": metadata,
            },
        )
        return {"handler": "local.memory.write", "status": "ok", "memory_id": memory_id}

    def _file_write(self, item: WorkItem) -> dict[str, Any]:
        metadata = item.metadata or {}
        raw_path = str(metadata.get("path") or item.target).strip()
        content = str(metadata.get("content") or item.detail)
        mode = str(metadata.get("mode") or "create").lower()
        if not raw_path:
            raise ValueError("local.file.write requires metadata.path or work item target.")
        if mode not in {"create", "overwrite", "append"}:
            raise ValueError("local.file.write mode must be create, overwrite, or append.")
        path = _resolve_allowed_path(raw_path, self.config)
        path.parent.mkdir(parents=True, exist_ok=True)
        if mode == "create" and path.exists():
            raise ValueError(f"Refusing to overwrite existing file in create mode: {path}")
        if mode == "append":
            with path.open("a", encoding="utf-8") as handle:
                handle.write(content)
        else:
            path.write_text(content, encoding="utf-8")
        audit_id = self.db.audit(
            actor="approved-handler",
            action="local.file.write",
            target=str(path),
            permission_tier=item.permission_tier,
            status="ok",
            details={"work_item": _work_item_summary(item), "mode": mode, "bytes": len(content.encode("utf-8"))},
        )
        return {"handler": "local.file.write", "status": "ok", "path": str(path), "mode": mode, "audit_id": audit_id}

    def _report_write(self, item: WorkItem) -> dict[str, Any]:
        metadata = item.metadata or {}
        title = str(metadata.get("title") or item.title).strip()
        content = str(metadata.get("content") or item.detail).strip()
        if not content:
            raise ValueError("local.report.write requires report content.")
        reports_dir = self.config.paths.hot_root / "Zade" / "reports"
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        path = reports_dir / f"{stamp}-{_slug(title)}.md"
        body = f"# {title}\n\n{content.strip()}\n"
        path = _resolve_allowed_path(str(path), self.config)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        memory_id = self.db.add_memory(
            kind="report",
            title=title,
            content=body,
            source="approved-handler",
            metadata={"path": str(path), "work_item_id": item.id},
        )
        return {"handler": "local.report.write", "status": "ok", "path": str(path), "memory_id": memory_id}

    def _vault_organize(self, item: WorkItem) -> dict[str, Any]:
        metadata = item.metadata or {}
        title = str(metadata.get("title") or item.title).strip()
        plan = str(metadata.get("plan") or metadata.get("content") or item.detail).strip()
        if not plan:
            raise ValueError("local.vault.organize requires a plan or detail.")
        plans_dir = self.config.paths.hot_root / "Zade" / "vault-organization"
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        path = _resolve_allowed_path(str(plans_dir / f"{stamp}-{_slug(title)}.md"), self.config)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = f"# {title}\n\n{plan}\n\n## Boundary\n\nThis handler writes an organization plan only. It does not move or delete vault files.\n"
        path.write_text(body, encoding="utf-8")
        memory_id = self.db.add_memory(
            kind="vault_organization_plan",
            title=title,
            content=body,
            source="approved-handler",
            metadata={"path": str(path), "work_item_id": item.id},
        )
        return {"handler": "local.vault.organize", "status": "ok", "path": str(path), "memory_id": memory_id}

    def _browser_open(self, item: WorkItem) -> dict[str, Any]:
        metadata = item.metadata or {}
        url = str(metadata.get("url") or item.target).strip()
        if not url:
            raise ValueError("local.browser.open requires metadata.url or work item target.")
        lower = url.lower()
        is_local = lower.startswith("http://127.0.0.1") or lower.startswith("http://localhost") or lower.startswith("file://")
        if not is_local and not bool(metadata.get("allow_external_url")):
            raise ValueError("local.browser.open only opens localhost/file URLs unless allow_external_url is true.")
        opened = False
        if bool(metadata.get("open_browser")):
            opened = bool(webbrowser.open(url))
        audit_id = self.db.audit(
            actor="approved-handler",
            action="local.browser.open",
            target=url,
            permission_tier=item.permission_tier,
            status="ok",
            details={"work_item": _work_item_summary(item), "opened": opened, "local": is_local},
        )
        return {"handler": "local.browser.open", "status": "ok", "url": url, "opened": opened, "audit_id": audit_id}


def _work_item_summary(item: WorkItem) -> dict[str, Any]:
    data = asdict(item)
    return {
        "id": data["id"],
        "title": data["title"],
        "action": data["action"],
        "target": data["target"],
        "permission_tier": data["permission_tier"],
        "authority_decision": data["authority_decision"],
        "status": data["status"],
        "source": data["source"],
        "metadata": data["metadata"],
    }


def _resolve_allowed_path(raw_path: str, config: KernelConfig) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = config.paths.hot_root / path
    resolved = path.resolve(strict=False)
    roots = [
        config.paths.hot_root.resolve(strict=False),
        config.paths.cold_root.resolve(strict=False),
        config.paths.data_dir.resolve(strict=False),
    ]
    if not any(_is_relative_to(resolved, root) for root in roots):
        raise ValueError(f"Path is outside configured local roots: {resolved}")
    return resolved


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-")
    return (cleaned or "report")[:80]
