"""Bounded background execution and boot-time build recovery."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from .build_orchestrator import BuildOrchestrator
from .build_store import BuildStore


class BuildExecutionManager:
    def __init__(
        self,
        *,
        store: BuildStore,
        orchestrator: BuildOrchestrator,
        max_workers: int = 2,
        max_tasks_per_run: int = 500,
    ):
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        self.store = store
        self.orchestrator = orchestrator
        self.max_tasks_per_run = max(1, max_tasks_per_run)
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="zade-build"
        )
        self._futures: dict[int, Future[dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._stopping = threading.Event()

    def start(self, session_id: int) -> dict[str, Any]:
        session = self.store.get_session(session_id)
        if session is None:
            raise ValueError(f"Build session not found: {session_id}")
        if self._stopping.is_set():
            return {"started": False, "status": "stopped", "session_id": session_id}
        if session.status in {"cancelled", "quarantined", "complete"}:
            return {"started": False, "status": session.status, "session_id": session_id}
        if session.status == "paused":
            session = self.store.resume_session(session_id)
        with self._lock:
            existing = self._futures.get(session_id)
            if existing is not None and not existing.done():
                return {"started": False, "status": "running", "session_id": session_id}
            future = self._executor.submit(self._run_session, session_id)
            self._futures[session_id] = future
        return {"started": True, "status": session.status, "session_id": session_id}

    def pause(self, session_id: int) -> dict[str, Any]:
        session = self.store.pause_session(session_id)
        return {"status": session.status, "session_id": session_id}

    def resume(self, session_id: int) -> dict[str, Any]:
        session = self.store.get_session(session_id)
        if session is None:
            raise ValueError(f"Build session not found: {session_id}")
        if session.status == "paused":
            self.store.resume_session(session_id)
        return self.start(session_id)

    def cancel(self, session_id: int) -> dict[str, Any]:
        session = self.store.cancel_session(session_id)
        self.orchestrator.cancel(session_id)
        return {"status": session.status, "session_id": session_id}

    def wait(self, session_id: int, *, timeout: float | None = None) -> dict[str, Any]:
        with self._lock:
            future = self._futures.get(session_id)
        if future is None:
            session = self.store.get_session(session_id)
            if session is None:
                raise ValueError(f"Build session not found: {session_id}")
            return {"status": session.status, "session_id": session_id}
        return future.result(timeout=timeout)

    def status(self, session_id: int) -> dict[str, Any]:
        session = self.store.get_session(session_id)
        if session is None:
            raise ValueError(f"Build session not found: {session_id}")
        with self._lock:
            future = self._futures.get(session_id)
        return {
            "session_id": session_id,
            "session_status": session.status,
            "worker_status": (
                "idle"
                if future is None
                else "done"
                if future.done()
                else "running"
            ),
        }

    def recover(self) -> dict[str, Any]:
        interrupted = self.store.recover_interrupted_runs()
        restarted: list[int] = []
        for session in reversed(self.store.list_sessions(limit=10_000)):
            if session.status != "active" or not self.store.list_tasks(session.id):
                continue
            if any(task.status.value == "pending" for task in self.store.list_tasks(session.id)):
                result = self.start(session.id)
                if result["started"]:
                    restarted.append(session.id)
        return {
            "interrupted_runs": len(interrupted),
            "restarted_sessions": restarted,
        }

    def shutdown(self, *, wait: bool = True) -> None:
        self._stopping.set()
        self._executor.shutdown(wait=wait, cancel_futures=True)

    def _run_session(self, session_id: int) -> dict[str, Any]:
        worker_id = f"worker-{uuid.uuid4().hex[:12]}"
        last: dict[str, Any] = {"status": "idle", "session_id": session_id}
        for _ in range(self.max_tasks_per_run):
            if self._stopping.is_set():
                return {"status": "stopped", "session_id": session_id}
            session = self.store.get_session(session_id)
            if session is None:
                return {"status": "missing", "session_id": session_id}
            if session.status != "active":
                return {"status": session.status, "session_id": session_id}
            last = self.orchestrator.run_next(session_id, worker_id=worker_id)
            if last.get("status") in {"blocked", "idle", "complete", "paused", "cancelled"}:
                return last
        return last | {"status": "bounded", "blockers": ["max_tasks_per_run"]}
