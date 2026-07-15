"""Screen awareness — a local, on-demand read of what's on the founder's screen.

Deep Thought could see the screen; Zade could not (zero source hits in the audit).
This closes that gap with a deliberately modest, privacy-respecting primitive:

  * The textual awareness — the focused window and the visible window titles — is
    dependency-free (ctypes/user32 on Windows) and is the useful, low-risk core:
    Zade knows *what you're working in* without capturing pixels.
  * A pixel snapshot is optional (the ``screen`` extra installs ``mss``) and
    on-demand only, written to a confined folder under the data dir and pruned to
    the last N. Raw pixels are never returned over the wire — only a file reference
    and dimensions — so a capture can't leak the screen into a response or log.

This is EXPLICIT, not ambient: nothing captures on a timer. It runs when the
founder (or a governed flow) asks. Local read tier — no network, no approval — but
scoped tightly because the screen is sensitive. The window enumerator and the
snapshotter are injectable so the whole thing is testable headless.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .config import KernelConfig
from .db import KernelDatabase, utc_now

WindowLister = Callable[[], "tuple[str, list[str]]"]
Snapshotter = Callable[..., "tuple[int, int]"]


class ScreenService:
    def __init__(
        self,
        *,
        config: KernelConfig,
        db: KernelDatabase,
        window_lister: WindowLister | None = None,
        snapshotter: Snapshotter | None = None,
    ):
        self.config = config
        self.db = db
        self._window_lister = window_lister
        self._snapshotter = snapshotter

    # ---- paths ----
    def _capture_dir(self) -> Path:
        path = self.config.paths.data_dir / self.config.screen.storage_subdir
        path.mkdir(parents=True, exist_ok=True)
        return path

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.config.screen.enabled,
            "snapshot_available": _mss_available(),
            "keep_last": self.config.screen.keep_last,
            "storage": str(self._capture_dir()) if self.config.screen.enabled else "",
            "operating_rules": [
                "Screen awareness is a LOCAL read: no network, no approval — but explicit, never on a timer.",
                "The textual read (focused + visible window titles) needs no extra dependency and captures no pixels.",
                "A pixel snapshot is optional (the 'screen' extra installs mss), on-demand, confined to the data dir, and pruned to the last N.",
                "Raw pixels never cross the wire or land in a response/log — only a file reference and dimensions.",
            ],
        }

    def capture(self, *, snapshot: bool = False) -> dict[str, Any]:
        """Read the current screen context. Textual by default; a pixel snapshot
        only when explicitly asked and mss is available."""
        if not self.config.screen.enabled:
            raise ValueError("Screen awareness is disabled (screen.enabled = false).")
        lister = self._window_lister or list_windows
        try:
            focused, windows = lister()
        except Exception as exc:  # noqa: BLE001 - awareness degrades gracefully, never 500s
            focused, windows = "", []
            windows_error = str(exc)[:200]
        else:
            windows_error = ""

        result: dict[str, Any] = {
            "captured_at": utc_now(),
            "focused_window": focused,
            "window_count": len(windows),
            "windows": windows[: self.config.screen.max_windows],
            "snapshot": None,
        }
        if windows_error:
            result["windows_error"] = windows_error

        if snapshot:
            result["snapshot"] = self._snapshot()

        self.db.audit(
            actor="local",
            action="local.screen.capture",
            target="screen",
            permission_tier="L0_READ",
            status="ok",
            details={
                "focused_window": focused,
                "window_count": len(windows),
                "snapshot": bool(result["snapshot"]),
            },
        )
        return result

    def _snapshot(self) -> dict[str, Any]:
        if not _mss_available() and self._snapshotter is None:
            return {"status": "unavailable", "note": "Pixel snapshot needs the 'screen' extra (mss). Install it to enable."}
        snapshotter = self._snapshotter or grab_screen
        directory = self._capture_dir()
        filename = f"capture-{utc_now().replace(':', '').replace('-', '').replace('+', '')}.png"
        path = directory / filename
        try:
            width, height = snapshotter(path)
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "error": str(exc)[:200]}
        self._prune(directory)
        return {"status": "ok", "path": str(path), "width": width, "height": height}

    def _prune(self, directory: Path) -> None:
        keep = max(1, self.config.screen.keep_last)
        captures = sorted(directory.glob("capture-*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in captures[keep:]:
            try:
                stale.unlink()
            except OSError:
                pass


# ---- module-level readers (the actual OS access) ----
def list_windows() -> tuple[str, list[str]]:
    """Return (focused_title, [visible_window_titles]) on Windows via user32.

    Dependency-free. On non-Windows or if user32 is unavailable, returns empty —
    the textual awareness simply degrades rather than erroring.
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # noqa: BLE001
        return "", []
    try:
        user32 = ctypes.windll.user32
    except (AttributeError, OSError):
        return "", []

    def title_of(hwnd: int) -> str:
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value.strip()

    focused = title_of(user32.GetForegroundWindow())

    titles: list[str] = []
    seen: set[str] = set()
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _collect(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            title = title_of(hwnd)
            if title and title not in seen:
                seen.add(title)
                titles.append(title)
        return True

    user32.EnumWindows(WNDENUMPROC(_collect), 0)
    return focused, titles


def grab_screen(path: Path) -> tuple[int, int]:
    """Snapshot the primary monitor to *path* (PNG). Returns (width, height).

    Uses mss (the optional 'screen' extra). Lazy-imported so the kernel boots
    without it.
    """
    import mss  # type: ignore
    import mss.tools  # type: ignore

    with mss.mss() as sct:
        monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
        shot = sct.grab(monitor)
        mss.tools.to_png(shot.rgb, shot.size, output=str(path))
        return int(shot.size.width), int(shot.size.height)


def _mss_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("mss") is not None
