from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import KernelConfig
from .db import KernelDatabase, utc_now
from .ollama import OllamaClient


class KernelOpsService:
    def __init__(self, *, config: KernelConfig, db: KernelDatabase, ollama: OllamaClient, ui_dir: Path):
        self.config = config
        self.db = db
        self.ollama = ollama
        self.ui_dir = ui_dir
        self.started_at = utc_now()
        self._started_monotonic = time.monotonic()

    @property
    def backup_dir(self) -> Path:
        return self.config.paths.data_dir / "backups"

    @property
    def supervision_log_path(self) -> Path:
        return self.config.paths.data_dir / "supervision" / "supervisor-log.jsonl"

    def uptime_seconds(self) -> int:
        return max(0, int(time.monotonic() - self._started_monotonic))

    def supervision(self, *, limit: int = 50) -> dict[str, Any]:
        """Report supervisor history from the log the supervisor script owns.

        The supervisor writes to a local JSONL file because the kernel cannot
        receive a report while it is down; the kernel only reads it.
        """
        events: list[dict[str, Any]] = []
        malformed = 0
        log_path = self.supervision_log_path
        if log_path.is_file():
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in lines[-500:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    malformed += 1
                    continue
                if isinstance(parsed, dict):
                    events.append(parsed)
                else:
                    malformed += 1
        events.reverse()
        counts: dict[str, int] = {}
        for event in events:
            key = str(event.get("event", "unknown"))
            counts[key] = counts.get(key, 0) + 1
        return {
            "generated_at": utc_now(),
            "kernel": {
                "started_at": self.started_at,
                "uptime_seconds": self.uptime_seconds(),
            },
            "log_path": str(log_path),
            "log_exists": log_path.is_file(),
            "events": events[:limit],
            "counts": counts,
            "malformed_lines": malformed,
            "last_event": events[0] if events else None,
            "expected_tasks": [
                "Zade Local Supervisor",
                "Zade Local Cadence",
                "Zade Local Health Monitor",
            ],
        }

    def create_backup(self, *, label: str = "manual") -> dict[str, Any]:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        safe_label = _safe_label(label)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        dest = self.backup_dir / f"cofounder-{stamp}-{safe_label}.sqlite"
        source = self.config.paths.database_path
        with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(dest)) as dst:
            src.backup(dst)
        size_bytes = dest.stat().st_size
        audit_id = self.db.audit(
            actor="ops",
            action="ops.backup",
            target=str(dest),
            permission_tier="L1_MEMORY_WRITE",
            status="ok",
            details={"source": str(source), "size_bytes": size_bytes, "label": label},
        )
        return {
            "path": str(dest),
            "size_bytes": size_bytes,
            "created_at": utc_now(),
            "label": label,
            "audit_id": audit_id,
        }

    def list_backups(self, *, limit: int = 25) -> list[dict[str, Any]]:
        if not self.backup_dir.exists():
            return []
        backups = []
        for path in sorted(self.backup_dir.glob("*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
            stat = path.stat()
            backups.append(
                {
                    "path": str(path),
                    "name": path.name,
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(timespec="seconds"),
                }
            )
        return backups

    def prune_backups(self, *, keep_last: int = 10, dry_run: bool = True) -> dict[str, Any]:
        if not self.backup_dir.exists():
            return {"dry_run": dry_run, "keep_last": keep_last, "kept": [], "deleted": [], "deleted_count": 0}
        paths = sorted(self.backup_dir.glob("*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True)
        kept = paths[:keep_last]
        delete_candidates = paths[keep_last:]
        deleted = []
        for path in delete_candidates:
            stat = path.stat()
            item = {
                "path": str(path),
                "name": path.name,
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(timespec="seconds"),
            }
            if not dry_run:
                path.unlink()
            deleted.append(item)
        audit_id = self.db.audit(
            actor="ops",
            action="ops.backup_retention",
            target=str(self.backup_dir),
            permission_tier="L1_MEMORY_WRITE",
            status="dry_run" if dry_run else "ok",
            details={
                "keep_last": keep_last,
                "dry_run": dry_run,
                "kept_count": len(kept),
                "deleted_count": len(deleted),
                "deleted": deleted,
            },
        )
        return {
            "dry_run": dry_run,
            "keep_last": keep_last,
            "kept": [str(path) for path in kept],
            "deleted": deleted,
            "deleted_count": len(deleted),
            "audit_id": audit_id,
        }

    def benchmark_models(
        self,
        *,
        prompt: str,
        roles: list[str],
        num_predict: int = 160,
    ) -> dict[str, Any]:
        results = []
        for role in roles:
            if role == "embedding":
                results.append(
                    {
                        "role": role,
                        "model": self.config.ollama.embedding_model,
                        "status": "skipped",
                        "reason": "Embedding models are measured through semantic search, not generation.",
                    }
                )
                continue
            model = self.config.ollama.model_for_role(role)  # type: ignore[arg-type]
            think = self.config.ollama.think_for_role(role)  # type: ignore[arg-type]
            started = time.perf_counter()
            try:
                generated = self.ollama.generate(
                    prompt=prompt,
                    model=model,
                    think=think,
                    temperature=self.config.ollama.temperature,
                    num_predict=num_predict,
                )
                latency_ms = int((time.perf_counter() - started) * 1000)
                call_id = self.db.record_model_call(
                    operation="ops.model_benchmark",
                    model=generated.model,
                    role=role,
                    status="ok",
                    latency_ms=latency_ms,
                    prompt_chars=len(prompt),
                    response_chars=len(generated.response),
                    think=think,
                    metadata={"num_predict": num_predict},
                )
                results.append(
                    {
                        "role": role,
                        "model": generated.model,
                        "status": "ok",
                        "latency_ms": latency_ms,
                        "think": think,
                        "response_chars": len(generated.response),
                        "response_preview": generated.response[:500],
                        "model_call_id": call_id,
                    }
                )
            except Exception as exc:
                latency_ms = int((time.perf_counter() - started) * 1000)
                call_id = self.db.record_model_call(
                    operation="ops.model_benchmark",
                    model=model,
                    role=role,
                    status="error",
                    latency_ms=latency_ms,
                    prompt_chars=len(prompt),
                    response_chars=0,
                    think=think,
                    error=str(exc),
                    metadata={"num_predict": num_predict},
                )
                results.append(
                    {
                        "role": role,
                        "model": model,
                        "status": "error",
                        "latency_ms": latency_ms,
                        "think": think,
                        "error": str(exc),
                        "model_call_id": call_id,
                    }
                )
        status = "ok" if all(item["status"] in {"ok", "skipped"} for item in results) else "degraded"
        audit_id = self.db.audit(
            actor="ops",
            action="ops.model_benchmark",
            target="ollama",
            permission_tier="L0_READ",
            status=status,
            details={
                "prompt_chars": len(prompt),
                "roles": roles,
                "num_predict": num_predict,
                "results": results,
            },
        )
        return {"status": status, "generated_at": utc_now(), "results": results, "audit_id": audit_id}

    def health_check(
        self,
        *,
        max_cadence_age_hours: int = 30,
        require_recent_cadence: bool = False,
    ) -> dict[str, Any]:
        database_exists = self.config.paths.database_path.exists()
        ui_index = self.ui_dir / "index.html"
        ui_exists = ui_index.exists()
        try:
            ollama_details = self.ollama.health()
            ollama_status = {"ok": True, "details": ollama_details}
        except Exception as exc:
            ollama_status = {"ok": False, "error": str(exc)}

        queue_counts = self.db.work_queue_counts()
        latest_cadence = self._latest_audit("runtime.cadence")
        cadence_status = _cadence_status(
            latest_cadence,
            max_age_hours=max_cadence_age_hours,
            required=require_recent_cadence,
        )
        checks = {
            "database": {"ok": database_exists, "path": str(self.config.paths.database_path)},
            "ui": {"ok": ui_exists, "path": str(ui_index)},
            "ollama": ollama_status,
            "cadence": cadence_status,
            "queue": {"ok": True, "counts": queue_counts},
        }
        required_checks = ["database", "ui", "ollama"]
        if require_recent_cadence:
            required_checks.append("cadence")
        ok = all(bool(checks[name].get("ok")) for name in required_checks)
        return {
            "ok": ok,
            "generated_at": utc_now(),
            "name": self.config.identity.name,
            "checks": checks,
            "backups": self.list_backups(limit=5),
        }

    def _latest_audit(self, action: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM audit_events WHERE action = ? ORDER BY id DESC LIMIT 1",
                (action,),
            ).fetchone()
        if not row:
            return None
        return dict(row)


def _safe_label(label: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", label.strip()).strip("-")
    return (cleaned or "manual")[:80]


def _cadence_status(
    latest: dict[str, Any] | None,
    *,
    max_age_hours: int,
    required: bool,
) -> dict[str, Any]:
    if not latest:
        return {"ok": not required, "latest": None, "required": required}
    created_at = str(latest["created_at"])
    try:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        age_seconds = max(0, int((datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds()))
    except ValueError:
        return {"ok": False, "latest": latest, "error": f"Invalid audit timestamp: {created_at}"}
    max_age_seconds = max_age_hours * 3600
    return {
        "ok": age_seconds <= max_age_seconds,
        "latest": latest,
        "age_seconds": age_seconds,
        "max_age_seconds": max_age_seconds,
        "required": required,
    }
