"""Resident desktop tray shell for the local kernel.

Two cleanly separated halves so the useful logic is testable and only the thin
OS-integration layer needs a real desktop:

  * ``TrayService.state()`` (kernel-side) aggregates everything the tray polls —
    health, pending approvals, unread notifications, and the newest unread ones
    to toast — into one cheap ``GET /tray/state`` response.
  * ``compute_view()`` (pure) maps that state to what the tray should show:
    icon status, tooltip, menu labels, and which notifications are new enough to
    raise an OS toast (de-duplicated against the ids already seen this session).
  * ``run_tray()`` (client) is a small loop that lazily imports pystray + Pillow,
    polls the kernel over loopback, and drives a real system-tray icon. Pystray's
    own ``notify()`` gives native toasts, so no extra toast dependency.

The tray is a separate resident process (installed at logon via
``scripts/install-tray-task.ps1``) and only ever issues GETs, so it never needs
the mutation token. GUI dependencies are optional (the ``tray`` extra); the
package imports without them.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .config import KernelConfig
from .db import KernelDatabase


# Tray status -> RGB, shared by the tooltip logic and the icon renderer.
STATUS_COLORS = {
    "ok": (46, 160, 67),        # green
    "attention": (219, 154, 4),  # amber
    "error": (207, 34, 46),      # red
}


class TrayService:
    def __init__(self, *, config: KernelConfig, db: KernelDatabase, bus: Any, ollama: Any | None = None):
        self.config = config
        self.db = db
        self.bus = bus
        self.ollama = ollama

    def state(self) -> dict[str, Any]:
        counts = self.db.work_queue_counts()
        pending = int(counts.get("approval_required", 0))
        deferred = int(counts.get("deferred", 0))
        unread = self.bus.list(unread_only=True, limit=500)
        unread_count = len(unread)
        toast_limit = max(1, self.config.tray.max_toast_notifications)

        ollama_ok = True
        if self.ollama is not None:
            try:
                self.ollama.health()
            except Exception:  # noqa: BLE001 - any failure means the brain is unreachable
                ollama_ok = False

        if not ollama_ok:
            status = "error"
        elif pending or unread_count:
            status = "attention"
        else:
            status = "ok"

        return {
            "identity": self.config.identity.name,
            "status": status,
            "ollama_ok": ollama_ok,
            "pending_approvals": pending,
            "deferred": deferred,
            "unread_notifications": unread_count,
            "queue": counts,
            "notifications": [_note_summary(note) for note in unread[:toast_limit]],
            "ui_url": f"http://{self.config.app.host}:{self.config.app.port}/ui/",
            "tooltip": _tooltip(self.config.identity.name, status, pending, unread_count, ollama_ok),
        }


@dataclass
class TrayView:
    status: str
    tooltip: str
    menu: list[str]
    toasts: list[dict[str, str]] = field(default_factory=list)
    color: tuple[int, int, int] = STATUS_COLORS["ok"]


def compute_view(state: dict[str, Any], seen_ids: set[int] | frozenset[int]) -> tuple[TrayView, set[int]]:
    """Map kernel state + already-seen notification ids to a tray view.

    Pure and side-effect-free: returns the view plus the updated seen-set so the
    client persists it across polls and never re-toasts the same notification.
    """
    seen = set(seen_ids)
    toasts: list[dict[str, str]] = []
    for note in state.get("notifications", []):
        note_id = note.get("id")
        if note_id is None or note_id in seen:
            continue
        seen.add(int(note_id))
        toasts.append({"title": note.get("title", "") or "Zade", "body": note.get("body", "") or note.get("title", "")})

    status = str(state.get("status", "ok"))
    pending = int(state.get("pending_approvals", 0))
    unread = int(state.get("unread_notifications", 0))
    menu = [
        "Open Zade",
        f"Approvals: {pending}",
        f"Unread: {unread}",
        "Refresh",
        "Quit",
    ]
    view = TrayView(
        status=status,
        tooltip=str(state.get("tooltip", "")),
        menu=menu,
        toasts=toasts,
        color=STATUS_COLORS.get(status, STATUS_COLORS["ok"]),
    )
    return view, seen


def _note_summary(note: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": note.get("id"),
        "title": note.get("title", ""),
        "body": (note.get("body", "") or "")[:200],
        "severity": note.get("severity", "info"),
        "created_at": note.get("created_at", ""),
    }


def _tooltip(identity: str, status: str, pending: int, unread: int, ollama_ok: bool) -> str:
    if not ollama_ok:
        return f"{identity} — local model offline"
    parts = []
    if pending:
        parts.append(f"{pending} approval{'s' if pending != 1 else ''}")
    if unread:
        parts.append(f"{unread} unread")
    if not parts:
        return f"{identity} — all clear"
    return f"{identity} — " + " · ".join(parts)


# ---- desktop client (lazy GUI imports; not covered by unit tests) ----
def run_tray(*, base_url: str, poll_interval: float = 15.0, identity: str = "Zade", toasts: bool = True) -> None:  # pragma: no cover
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise SystemExit(
            "The tray shell needs pystray + Pillow. Install with: "
            "pip install \"local-ai-cofounder-kernel[tray]\""
        ) from exc

    state_url = base_url.rstrip("/") + "/tray/state"
    seen: set[int] = set()
    stop = threading.Event()

    def render(color: tuple[int, int, int]) -> "Image.Image":
        image = Image.new("RGB", (64, 64), (24, 24, 27))
        draw = ImageDraw.Draw(image)
        draw.ellipse((14, 14, 50, 50), fill=color)
        return image

    def fetch() -> dict[str, Any] | None:
        try:
            with urllib.request.urlopen(state_url, timeout=8) as response:  # noqa: S310 - loopback kernel URL
                return json.loads(response.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 - kernel not up yet / transient; keep the tray alive
            return None

    def open_ui(icon, _item):
        import webbrowser

        webbrowser.open(base_url.rstrip("/") + "/ui/")

    def refresh(icon, _item):
        _poll_once(icon)

    def quit_tray(icon, _item):
        stop.set()
        icon.stop()

    def _poll_once(icon) -> None:
        nonlocal seen
        state = fetch()
        if state is None:
            icon.icon = render(STATUS_COLORS["error"])
            icon.title = f"{identity} — kernel unreachable"
            return
        view, seen = compute_view(state, seen)
        icon.icon = render(view.color)
        icon.title = view.tooltip
        icon.menu = pystray.Menu(
            pystray.MenuItem("Open Zade", open_ui, default=True),
            pystray.MenuItem(f"Approvals: {state.get('pending_approvals', 0)}", None, enabled=False),
            pystray.MenuItem(f"Unread: {state.get('unread_notifications', 0)}", None, enabled=False),
            pystray.MenuItem("Refresh", refresh),
            pystray.MenuItem("Quit tray", quit_tray),
        )
        if toasts:
            for toast in view.toasts:
                try:
                    icon.notify(toast["body"], toast["title"])
                except Exception:  # noqa: BLE001 - a failed toast must never kill the loop
                    pass

    def loop(icon) -> None:
        icon.visible = True
        while not stop.is_set():
            _poll_once(icon)
            stop.wait(poll_interval)

    icon = pystray.Icon("zade", render(STATUS_COLORS["ok"]), f"{identity} tray")
    icon.run(setup=lambda i: threading.Thread(target=loop, args=(i,), daemon=True).start())


def main() -> None:  # pragma: no cover
    from .config import load_config

    config = load_config()
    if not config.tray.enabled:
        raise SystemExit("Tray shell is disabled (tray.enabled = false).")
    run_tray(
        base_url=f"http://{config.app.host}:{config.app.port}",
        poll_interval=config.tray.poll_interval_seconds,
        identity=config.identity.name,
        toasts=config.tray.toasts,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
