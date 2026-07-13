"""Whole-vault file operator: list, search, move, and delete-to-trash.

This turns Zade's additive-only file surface into a real vault operator, while
keeping the safety model strict. It complements — does not replace — the
propose-only ``local.vault.organize`` handler: this one actually moves and
deletes, so every guarantee is enforced in code.

Safety model (layered, most-general first):
  * Reads (``list``/``search``) are L0 and direct. Mutations (``move``/``delete``)
    run only through an approved work item + the typed confirmation phrase, at
    tier L2_FILE_WRITE — the same gate as file writes.
  * Every path resolves inside the configured hot/cold roots and never the
    kernel state dir (reusing ``_resolve_allowed_path``).
  * Deletes and clobbered move-targets are moved to a restorable trash snapshot
    under the kernel state dir — never a hard unlink.
  * ``guard_segments`` (e.g. ``01-raw``, ``raw-ingest``) are source-of-truth
    folders: any path with such a segment is refused.
  * A ``protected_marker`` file protects its whole subtree — per-project
    instruction precedence over the global allow.
  * Operating on a top-level folder (a direct child of a root) requires an
    explicit ``allow_top_level`` confirmation; a vault root can never be moved
    or deleted at all.
  * ``dry_run`` previews the exact effect (counts + resolved paths + guard
    result) and changes nothing.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .autonomy import WorkQueueService
from .config import KernelConfig
from .db import KernelDatabase, WorkItem, utc_now
from .handlers import _is_relative_to, _resolve_allowed_path, _work_item_summary


MOVE_ACTION = "local.vault.move"
DELETE_ACTION = "local.vault.delete"


class VaultService:
    def __init__(self, *, config: KernelConfig, db: KernelDatabase, work_queue: WorkQueueService):
        self.config = config
        self.db = db
        self.work_queue = work_queue
        self.roots = [
            config.paths.hot_root.resolve(strict=False),
            config.paths.cold_root.resolve(strict=False),
        ]
        self.trash_root = (config.paths.data_dir / "vault-trash").resolve(strict=False)

    # ---- registration ----
    def register_into(self, registry: Any) -> list[str]:
        if not self.config.vault.enabled:
            return []
        registry.register(
            MOVE_ACTION,
            "Move a vault file/folder within the local roots (approved; clobbered targets are trashed).",
            self.move_from_work_item,
        )
        registry.register(
            DELETE_ACTION,
            "Delete a vault file/folder to a restorable trash snapshot (approved).",
            self.delete_from_work_item,
        )
        return [MOVE_ACTION, DELETE_ACTION]

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.config.vault.enabled,
            "roots": [str(root) for root in self.roots],
            "trash_root": str(self.trash_root),
            "guard_segments": list(self.config.vault.guard_segments),
            "protected_marker": self.config.vault.protected_marker,
            "list_limit": self.config.vault.list_limit,
        }

    # ---- reads (no approval) ----
    def list_entries(self, path: str = "", *, limit: int | None = None) -> dict[str, Any]:
        base = self._resolve_dir(path)
        limit = min(int(limit or self.config.vault.list_limit), self.config.vault.list_limit)
        entries: list[dict[str, Any]] = []
        for child in sorted(base.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            if _is_relative_to(child.resolve(strict=False), self.trash_root):
                continue  # never surface the trash as vault content
            entries.append(self._entry_info(child))
            if len(entries) >= limit:
                break
        rel, _root = self._relative(base)
        return {
            "path": str(base),
            "relative": str(rel),
            "count": len(entries),
            "entries": entries,
            "governing_instructions": self._governing_instructions(base),
        }

    def search(self, query: str, *, path: str = "", limit: int | None = None) -> dict[str, Any]:
        needle = query.strip().lower()
        if not needle:
            raise ValueError("Vault search requires a non-empty query.")
        base = self._resolve_dir(path)
        limit = min(int(limit or self.config.vault.search_limit), self.config.vault.search_limit)
        matches: list[dict[str, Any]] = []
        for child in base.rglob("*"):
            resolved = child.resolve(strict=False)
            if _is_relative_to(resolved, self.trash_root) or _is_relative_to(resolved, self.config.paths.data_dir.resolve(strict=False)):
                continue
            if needle in child.name.lower():
                matches.append(self._entry_info(child))
                if len(matches) >= limit:
                    break
        return {"query": query, "base": str(base), "count": len(matches), "matches": matches}

    # ---- previews / queue ----
    def plan_delete(self, path: str, *, allow_top_level: bool = False) -> dict[str, Any]:
        target = self._resolve(path)
        self._guard(target, allow_top_level=allow_top_level, op="delete")
        return {
            "dry_run": True,
            "action": DELETE_ACTION,
            "path": str(target),
            "kind": "dir" if target.is_dir() else "file",
            "file_count": _count_files(target),
            "trash_root": str(self.trash_root),
            "guards_passed": True,
        }

    def plan_move(self, src: str, dst: str, *, allow_top_level: bool = False, overwrite: bool = False) -> dict[str, Any]:
        source, destination = self._resolve_move(src, dst, allow_top_level=allow_top_level)
        clobber = destination.exists()
        if clobber and not overwrite:
            raise ValueError(f"Destination exists: {destination}. Set overwrite to replace (it is trashed first).")
        return {
            "dry_run": True,
            "action": MOVE_ACTION,
            "src": str(source),
            "dst": str(destination),
            "kind": "dir" if source.is_dir() else "file",
            "file_count": _count_files(source),
            "destination_exists": clobber,
            "will_trash_destination": clobber and overwrite,
            "guards_passed": True,
        }

    def queue_delete(self, path: str, *, allow_top_level: bool = False) -> dict[str, Any]:
        self._require_enabled()
        target = self._resolve(path)
        self._guard(target, allow_top_level=allow_top_level, op="delete")
        rel, _root = self._relative(target)
        result = self.work_queue.enqueue(
            kind="vault_delete",
            title=f"Delete vault path: {rel}",
            detail=(
                f"Delete '{target}' ({_count_files(target)} file(s)) to a restorable trash snapshot. "
                "Nothing is hard-deleted; restore from /vault/trash if needed."
            ),
            action=DELETE_ACTION,
            target=str(target),
            permission_tier="L2_FILE_WRITE",
            priority=55,
            source="vault",
            metadata={"path": str(target), "allow_top_level": allow_top_level},
            unique_key=f"{DELETE_ACTION}:{_digest(str(target))}:{utc_now()}",
        )
        return result.as_dict()

    def queue_move(self, src: str, dst: str, *, allow_top_level: bool = False, overwrite: bool = False) -> dict[str, Any]:
        self._require_enabled()
        source, destination = self._resolve_move(src, dst, allow_top_level=allow_top_level)
        if destination.exists() and not overwrite:
            raise ValueError(f"Destination exists: {destination}. Set overwrite to replace (it is trashed first).")
        rel_src, _r = self._relative(source)
        rel_dst, _r2 = self._relative(destination)
        result = self.work_queue.enqueue(
            kind="vault_move",
            title=f"Move vault path: {rel_src} -> {rel_dst}",
            detail=f"Move '{source}' to '{destination}'." + (" Existing destination is trashed first." if destination.exists() else ""),
            action=MOVE_ACTION,
            target=str(source),
            permission_tier="L2_FILE_WRITE",
            priority=55,
            source="vault",
            metadata={"src": str(source), "dst": str(destination), "allow_top_level": allow_top_level, "overwrite": overwrite},
            unique_key=f"{MOVE_ACTION}:{_digest(str(source) + str(destination))}:{utc_now()}",
        )
        return result.as_dict()

    # ---- dispatch handlers ----
    def delete_from_work_item(self, item: WorkItem) -> dict[str, Any]:
        metadata = item.metadata or {}
        target = self._resolve(str(metadata.get("path", item.target)))
        allow_top_level = bool(metadata.get("allow_top_level", False))
        # Re-guard at dispatch: the item may have been edited after queueing.
        self._guard(target, allow_top_level=allow_top_level, op="delete")
        if not target.exists():
            raise ValueError(f"Vault path no longer exists: {target}")
        trash_id, manifest = self._to_trash(target, item=item, reason="deleted")
        audit_id = self.db.audit(
            actor="approved-handler",
            action=DELETE_ACTION,
            target=str(target),
            permission_tier=item.permission_tier,
            status="ok",
            details={"work_item": _work_item_summary(item), "trash_id": trash_id, "file_count": manifest["file_count"]},
        )
        return {
            "handler": DELETE_ACTION,
            "status": "ok",
            "deleted": str(target),
            "trash_id": trash_id,
            "file_count": manifest["file_count"],
            "restorable": True,
            "audit_id": audit_id,
        }

    def move_from_work_item(self, item: WorkItem) -> dict[str, Any]:
        metadata = item.metadata or {}
        source = self._resolve(str(metadata.get("src", item.target)))
        destination = self._resolve(str(metadata["dst"]), must_exist=False)
        allow_top_level = bool(metadata.get("allow_top_level", False))
        overwrite = bool(metadata.get("overwrite", False))
        self._guard(source, allow_top_level=allow_top_level, op="move")
        self._guard(destination, allow_top_level=allow_top_level, op="move into", check_top_level=False)
        if not source.exists():
            raise ValueError(f"Vault source no longer exists: {source}")
        trash_id = None
        if destination.exists():
            if not overwrite:
                raise ValueError(f"Destination exists: {destination}. Re-queue with overwrite to replace it.")
            trash_id, _manifest = self._to_trash(destination, item=item, reason="overwritten by move")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        audit_id = self.db.audit(
            actor="approved-handler",
            action=MOVE_ACTION,
            target=str(source),
            permission_tier=item.permission_tier,
            status="ok",
            details={"work_item": _work_item_summary(item), "dst": str(destination), "trashed_destination": trash_id},
        )
        return {
            "handler": MOVE_ACTION,
            "status": "ok",
            "src": str(source),
            "dst": str(destination),
            "trashed_destination": trash_id,
            "audit_id": audit_id,
        }

    # ---- trash / restore ----
    def list_trash(self, *, limit: int = 50) -> dict[str, Any]:
        if not self.trash_root.exists():
            return {"count": 0, "items": []}
        items: list[dict[str, Any]] = []
        for entry in sorted(self.trash_root.iterdir(), reverse=True):
            manifest_path = entry / "manifest.json"
            if manifest_path.is_file():
                try:
                    items.append(json.loads(manifest_path.read_text(encoding="utf-8")))
                except (ValueError, OSError):
                    continue
            if len(items) >= limit:
                break
        return {"count": len(items), "items": items}

    def restore(self, trash_id: str, *, overwrite: bool = False) -> dict[str, Any]:
        if "/" in trash_id or "\\" in trash_id or ".." in trash_id:
            raise ValueError("Invalid trash id.")
        entry = self.trash_root / trash_id
        manifest_path = entry / "manifest.json"
        if not manifest_path.is_file():
            raise ValueError(f"Trash entry not found: {trash_id}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        original = _resolve_allowed_path(str(manifest["original_path"]), self.config)
        payload = entry / "payload" / manifest["name"]
        if not payload.exists():
            raise ValueError(f"Trash payload missing for: {trash_id}")
        restored_over = None
        if original.exists():
            if not overwrite:
                raise ValueError(f"Original path exists: {original}. Set overwrite to replace it.")
            restored_over, _m = self._to_trash(original, item=None, reason="replaced by restore")
        original.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(payload), str(original))
        manifest["restored_at"] = utc_now()
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        self.db.audit(
            actor="vault",
            action="local.vault.restore",
            target=str(original),
            permission_tier="L2_FILE_WRITE",
            status="ok",
            details={"trash_id": trash_id, "restored_over_trash_id": restored_over},
        )
        return {"status": "ok", "restored": str(original), "trash_id": trash_id, "restored_over": restored_over}

    # ---- internals ----
    def _require_enabled(self) -> None:
        if not self.config.vault.enabled:
            raise ValueError("Vault file operator is disabled (vault.enabled = false).")

    def _resolve(self, path: str, *, must_exist: bool = True) -> Path:
        if not str(path).strip():
            raise ValueError("A vault path is required.")
        resolved = _resolve_allowed_path(str(path), self.config)
        if must_exist and not resolved.exists():
            raise ValueError(f"Vault path not found: {resolved}")
        return resolved

    def _resolve_dir(self, path: str) -> Path:
        if not str(path).strip():
            return self.roots[0]  # empty -> hot root
        resolved = self._resolve(path)
        if not resolved.is_dir():
            raise ValueError(f"Not a directory: {resolved}")
        return resolved

    def _resolve_move(self, src: str, dst: str, *, allow_top_level: bool) -> tuple[Path, Path]:
        source = self._resolve(src)
        destination = self._resolve(dst, must_exist=False)
        self._guard(source, allow_top_level=allow_top_level, op="move")
        self._guard(destination, allow_top_level=allow_top_level, op="move into", check_top_level=False)
        return source, destination

    def _relative(self, path: Path) -> tuple[Path, Path]:
        resolved = path.resolve(strict=False)
        for root in self.roots:
            if _is_relative_to(resolved, root):
                return resolved.relative_to(root), root
        raise ValueError(f"Path is outside configured local roots: {resolved}")

    def _guard(self, path: Path, *, allow_top_level: bool, op: str, check_top_level: bool = True) -> None:
        rel, root = self._relative(path)
        parts = rel.parts
        if not parts:
            raise ValueError(f"Refusing to {op} a vault root directory: {root}")
        guard_set = {segment.lower() for segment in self.config.vault.guard_segments}
        for segment in parts:
            if segment.lower() in guard_set:
                raise ValueError(f"Refusing to {op}: '{segment}' is a protected raw/source folder.")
        if check_top_level and len(parts) == 1 and not allow_top_level:
            raise ValueError(
                f"Refusing to {op} a top-level vault entry ({rel}) without confirmation; set allow_top_level to proceed."
            )
        protected = self._protected_ancestor(path, root)
        if protected is not None:
            raise ValueError(
                f"Refusing to {op}: a {self.config.vault.protected_marker} marker in '{protected}' protects this location."
            )

    def _protected_ancestor(self, path: Path, root: Path) -> Path | None:
        """Return the folder whose protected marker covers *path*, or None.

        Walks from the path's directory up to (and including) the root. A marker
        file anywhere in that chain protects the whole subtree beneath it.
        """
        marker = self.config.vault.protected_marker
        if not marker:
            return None
        current = path if path.is_dir() else path.parent
        current = current.resolve(strict=False)
        root = root.resolve(strict=False)
        while True:
            if (current / marker).is_file():
                try:
                    return current.relative_to(root)
                except ValueError:
                    return current
            if current == root or not _is_relative_to(current, root):
                return None
            current = current.parent

    def _to_trash(self, path: Path, *, item: WorkItem | None, reason: str) -> tuple[str, dict[str, Any]]:
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        trash_id = f"{stamp}-{_digest(str(path))}"
        entry = self.trash_root / trash_id
        entry.mkdir(parents=True, exist_ok=True)
        file_count = _count_files(path)
        payload_dir = entry / "payload"
        payload_dir.mkdir(parents=True, exist_ok=True)
        destination = payload_dir / path.name
        shutil.move(str(path), str(destination))
        manifest = {
            "trash_id": trash_id,
            "original_path": str(path),
            "name": path.name,
            "kind": "dir" if destination.is_dir() else "file",
            "file_count": file_count,
            "reason": reason,
            "deleted_at": utc_now(),
            "work_item_id": item.id if item else None,
        }
        (entry / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return trash_id, manifest

    def _entry_info(self, child: Path) -> dict[str, Any]:
        rel, _root = self._relative(child)
        is_dir = child.is_dir()
        segments = {segment.lower() for segment in self.config.vault.guard_segments}
        guarded = any(part.lower() in segments for part in rel.parts)
        protected = is_dir and (child / self.config.vault.protected_marker).is_file()
        return {
            "name": child.name,
            "relative": str(rel),
            "type": "dir" if is_dir else "file",
            "size": child.stat().st_size if child.is_file() else None,
            "top_level": len(rel.parts) == 1,
            "guarded": guarded,
            "protected_marker": protected,
        }

    def _governing_instructions(self, base: Path) -> str | None:
        """Nearest per-project instruction file governing *base*, if any."""
        marker = self.config.vault.instructions_marker
        if not marker:
            return None
        try:
            _rel, root = self._relative(base)
        except ValueError:
            return None
        current = base.resolve(strict=False)
        root = root.resolve(strict=False)
        while True:
            candidate = current / marker
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8", errors="replace")[:4000]
            if current == root or not _is_relative_to(current, root):
                return None
            current = current.parent


def _count_files(path: Path) -> int:
    if path.is_file():
        return 1
    if not path.exists():
        return 0
    return sum(1 for child in path.rglob("*") if child.is_file())


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
