"""Headed browser automation on the approved-dispatch substrate.

This is the real replacement for the ``local.browser.open`` URL-opener stub: it
drives a visible browser through a *scripted flow* (navigate, read, extract
links, fill, click, press, screenshot) and returns what it saw. It is not an
open-ended interactive session — one work item carries one fully-specified list
of steps, so the founder approves exactly the sequence that will run, and the
whole flow executes in a single browser context.

Security posture, mirroring the connector layer:
  * Every flow reaches execution only through a work item the founder approved
    with the typed confirmation phrase (tier ``L3_EXTERNAL_ACTION``). The
    existing authority + approval gates are the boundary.
  * Navigation targets are screened before queueing and again before running:
    http/https only (no ``file:``/``javascript:``/``data:``), and private or
    internal hosts are refused unless ``allow_private_navigation`` is set — the
    same SSRF stance netguard applies to the kernel's own egress.
  * Typed values may be supplied literally or, for secrets, by naming an
    environment variable (``value_env``) resolved only at run time. Typed text
    is never written to the audit log or the returned result — only the field
    selector and a redaction marker are recorded.
  * Screenshots may only be written under the configured local roots, through
    the same ``_resolve_allowed_path`` guard the file handlers use.

Playwright is an optional dependency. The module imports lazily so the kernel
boots without it; a flow that needs a browser fails with a clear install hint
instead of crashing at import.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import urllib.parse
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from . import netguard
from .autonomy import WorkQueueService
from .config import KernelConfig
from .db import KernelDatabase, WorkItem, utc_now
from .handlers import _resolve_allowed_path


BROWSER_RUN_ACTION = "external.browser.run"

# Steps that only observe the page vs. steps that change page/remote state. The
# split drives the honest read-only/interactive label on the approval card; both
# are still L3 external actions requiring approval + the typed phrase.
READ_ONLY_STEPS = frozenset({"navigate", "wait", "read", "links", "screenshot"})
INTERACTIVE_STEPS = frozenset({"fill", "click", "press"})
STEP_TYPES = READ_ONLY_STEPS | INTERACTIVE_STEPS

# Output caps so a large page cannot balloon the work-item result / audit row.
MAX_TEXT_CHARS = 8000
MAX_LINKS = 200
REDACTED = "***redacted***"

BrowserRunner = Callable[..., dict[str, Any]]


class BrowserNotAvailable(ValueError):
    """Playwright or its browser binary is not installed.

    Subclasses ValueError so the dispatch path surfaces it as a 400 with the
    install hint, exactly like the connector layer surfaces a missing
    credential env var — a setup gap, not a kernel crash.
    """


class BrowserService:
    """Queue and run approval-gated headed browser flows.

    Wiring matches :class:`ConnectorService`: ``queue_run`` enqueues an L3 work
    item, ``run_from_work_item`` is the registered dispatch handler, and the
    actual Playwright execution is an injectable ``runner`` so the queueing,
    validation, path, and audit logic is fully testable without a real browser.
    """

    def __init__(
        self,
        *,
        config: KernelConfig,
        db: KernelDatabase,
        work_queue: WorkQueueService,
        runner: BrowserRunner | None = None,
    ):
        self.config = config
        self.db = db
        self.work_queue = work_queue
        # An injected runner wins for unit tests; otherwise the module-level
        # run_browser_flow is resolved by name at call time so monkeypatching it
        # (as the connector tests do with fetch_imap_items) takes effect.
        self._runner = runner

    # ---- registration ----
    def register_into(self, registry: Any) -> list[str]:
        """Register the dispatch handler, unless browser automation is disabled."""
        if not self.config.browser.enabled:
            return []
        registry.register(
            BROWSER_RUN_ACTION,
            "Run an approved headed browser flow (navigate/read/links/fill/click/press/screenshot).",
            self.run_from_work_item,
        )
        return [BROWSER_RUN_ACTION]

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.config.browser.enabled,
            "headless": self.config.browser.headless,
            "browser": self.config.browser.browser,
            "playwright_available": _playwright_available(),
            "max_steps": self.config.browser.max_steps,
            "allow_private_navigation": self.config.browser.allow_private_navigation,
            "step_types": sorted(STEP_TYPES),
        }

    # ---- queue ----
    def queue_run(
        self,
        *,
        steps: list[dict[str, Any]],
        title: str = "",
        session_label: str = "",
    ) -> dict[str, Any]:
        """Validate a flow and enqueue it as an approval-gated external action."""
        if not self.config.browser.enabled:
            raise ValueError("Browser automation is disabled (browser.enabled = false).")
        normalized = self._validate_steps(steps)
        interactive = self._is_interactive(normalized)
        label = title.strip() or f"Browser flow ({len(normalized)} steps)"
        digest = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode("utf-8")).hexdigest()[:12]
        result = self.work_queue.enqueue(
            kind="browser_run",
            title=label,
            detail=self._describe(normalized, interactive=interactive),
            action=BROWSER_RUN_ACTION,
            target=_first_navigation(normalized),
            permission_tier="L3_EXTERNAL_ACTION",
            priority=60,
            source="browser",
            metadata={
                "steps": normalized,
                "interactive": interactive,
                "session_label": session_label.strip(),
            },
            unique_key=f"{BROWSER_RUN_ACTION}:{digest}:{utc_now()}",
        )
        return result.as_dict() | {"interactive": interactive, "step_count": len(normalized)}

    # ---- dispatch handler ----
    def run_from_work_item(self, item: WorkItem) -> dict[str, Any]:
        metadata = item.metadata or {}
        raw_steps = metadata.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ValueError("external.browser.run requires metadata.steps (a non-empty list).")
        # Re-validate at dispatch time: the item may have been edited in the
        # approval console after it was queued, so never trust stored steps blindly.
        steps = self._validate_steps(raw_steps)
        interactive = self._is_interactive(steps)
        options = {
            "headless": self.config.browser.headless,
            "browser": self.config.browser.browser,
            "nav_timeout_ms": int(self.config.browser.nav_timeout_seconds * 1000),
            "action_timeout_ms": int(self.config.browser.action_timeout_seconds * 1000),
        }
        runner = self._runner or run_browser_flow
        flow = runner(steps, options=options)
        ok = bool(flow.get("ok", False))
        # Audit a redacted, value-free view: summarized steps only. The raw steps
        # (which may hold a literal fill value for replay) live in the work-item
        # metadata, but typed text must never reach the audit log — so we do NOT
        # embed the full _work_item_summary here.
        self.db.audit(
            actor="approved-handler",
            action=BROWSER_RUN_ACTION,
            target=_first_navigation(steps) or "browser",
            permission_tier=item.permission_tier,
            status="ok" if ok else "flow_error",
            details={
                "work_item_id": item.id,
                "title": item.title,
                "source": item.source,
                "interactive": interactive,
                "step_count": len(steps),
                "urls": [s["url"] for s in steps if s["type"] == "navigate"],
                "steps": [_summarize_step(s) for s in steps],
                "ok": ok,
                "failed_step": flow.get("failed_step"),
                "error": flow.get("error", ""),
            },
        )
        return {
            "handler": BROWSER_RUN_ACTION,
            "status": "ok",
            "ok": ok,
            "interactive": interactive,
            "step_count": len(steps),
            "failed_step": flow.get("failed_step"),
            "error": flow.get("error", ""),
            "steps": flow.get("steps", []),
            "pages": flow.get("pages", []),
        }

    def run_verification_flow(
        self,
        *,
        steps: list[dict[str, Any]],
        trace_path: str = "",
    ) -> dict[str, Any]:
        """Run a read-only evidence flow locally without an external-action approval."""
        if not self.config.browser.enabled:
            raise ValueError("Browser automation is disabled (browser.enabled = false).")
        normalized = self._validate_steps(steps, allow_loopback=True)
        if self._is_interactive(normalized):
            raise ValueError("Build verification browser flows must be read-only.")
        default_trace = (
            self.config.paths.hot_root
            / "Zade"
            / "build-browser-evidence"
            / f"trace-{uuid4().hex}.zip"
        )
        resolved_trace = _resolve_allowed_path(trace_path or str(default_trace), self.config)
        if resolved_trace.suffix.lower() != ".zip":
            raise ValueError("Browser verification trace path must end in .zip.")
        resolved_trace.parent.mkdir(parents=True, exist_ok=True)
        options = {
            "headless": True,
            "browser": self.config.browser.browser,
            "nav_timeout_ms": int(self.config.browser.nav_timeout_seconds * 1000),
            "action_timeout_ms": int(self.config.browser.action_timeout_seconds * 1000),
            "trace_path": str(resolved_trace),
            "allowed_origin": _origin(normalized[0]["url"]),
        }
        runner = self._runner or run_browser_flow
        flow = runner(normalized, options=options)
        screenshots = [
            str(step.get("path"))
            for step in flow.get("steps", [])
            if isinstance(step, dict)
            and step.get("type") == "screenshot"
            and step.get("status") == "ok"
            and step.get("path")
        ]
        return flow | {"screenshots": screenshots, "trace": str(resolved_trace)}

    # ---- validation ----
    def _validate_steps(
        self, steps: Any, *, allow_loopback: bool = False
    ) -> list[dict[str, Any]]:
        if not isinstance(steps, list) or not steps:
            raise ValueError("Browser flow requires a non-empty list of steps.")
        if len(steps) > self.config.browser.max_steps:
            raise ValueError(f"Browser flow has too many steps (max {self.config.browser.max_steps}).")
        normalized: list[dict[str, Any]] = []
        for index, raw in enumerate(steps):
            if not isinstance(raw, dict):
                raise ValueError(f"Step {index} must be an object.")
            step_type = str(raw.get("type", "")).strip().lower()
            if step_type not in STEP_TYPES:
                raise ValueError(
                    f"Step {index} has unknown type {step_type!r}. Allowed: {', '.join(sorted(STEP_TYPES))}."
                )
            normalized.append(
                self._validate_step(
                    index, step_type, raw, allow_loopback=allow_loopback
                )
            )
        if normalized[0]["type"] != "navigate":
            raise ValueError("A browser flow must start with a 'navigate' step.")
        return normalized

    def _validate_step(
        self,
        index: int,
        step_type: str,
        raw: dict[str, Any],
        *,
        allow_loopback: bool = False,
    ) -> dict[str, Any]:
        step: dict[str, Any] = {"type": step_type}
        if step_type == "navigate":
            step["url"] = self._validate_navigation(
                str(raw.get("url", "")).strip(),
                index,
                allow_loopback=allow_loopback,
            )
        elif step_type == "wait":
            selector = str(raw.get("selector", "")).strip()
            ms = raw.get("ms")
            if not selector and ms is None:
                raise ValueError(f"Step {index} 'wait' needs a selector or ms.")
            if selector:
                step["selector"] = selector
            if ms is not None:
                step["ms"] = max(0, min(int(ms), 30000))
        elif step_type in {"read", "links"}:
            selector = str(raw.get("selector", "")).strip()
            if selector:
                step["selector"] = selector
        elif step_type == "fill":
            step["selector"] = _require_selector(raw, index)
            value = raw.get("value")
            value_env = str(raw.get("value_env", "")).strip()
            if value_env:
                step["value_env"] = value_env
            elif value is not None:
                step["value"] = str(value)
            else:
                raise ValueError(f"Step {index} 'fill' needs a value or value_env.")
        elif step_type == "click":
            step["selector"] = _require_selector(raw, index)
        elif step_type == "press":
            selector = str(raw.get("selector", "")).strip()
            key = str(raw.get("key", "")).strip()
            if not key:
                raise ValueError(f"Step {index} 'press' needs a key (e.g. 'Enter').")
            if selector:
                step["selector"] = selector
            step["key"] = key
        elif step_type == "screenshot":
            # Confine captures to the local roots via the same guard file writes use.
            raw_path = str(raw.get("path", "")).strip()
            default = self.config.paths.hot_root / "Zade" / "browser-captures" / f"capture-{index}.png"
            resolved = _resolve_allowed_path(raw_path or str(default), self.config)
            if resolved.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
                raise ValueError(f"Step {index} screenshot path must end in .png/.jpg/.jpeg.")
            step["path"] = str(resolved)
            step["full_page"] = bool(raw.get("full_page", False))
        return step

    def _validate_navigation(
        self, url: str, index: int, *, allow_loopback: bool = False
    ) -> str:
        if not url:
            raise ValueError(f"Step {index} 'navigate' needs a url.")
        parsed = urllib.parse.urlparse(url)
        scheme = (parsed.scheme or "").lower()
        host = (parsed.hostname or "").lower()
        if scheme not in {"http", "https"}:
            raise ValueError(f"Step {index} navigate url must be http/https, got {scheme or 'none'!r}.")
        if not host:
            raise ValueError(f"Step {index} navigate url has no host.")
        loopback = host in {"localhost", "127.0.0.1", "::1"}
        if (
            not self.config.browser.allow_private_navigation
            and not (allow_loopback and loopback)
            and netguard.is_private_host(host)
        ):
            raise ValueError(
                f"Step {index} navigate url {host!r} resolves to a private/internal address; "
                "set browser.allow_private_navigation to permit it."
            )
        return url

    def _is_interactive(self, steps: list[dict[str, Any]]) -> bool:
        return any(step["type"] in INTERACTIVE_STEPS for step in steps)

    def _describe(self, steps: list[dict[str, Any]], *, interactive: bool) -> str:
        header = (
            "Headed browser flow. This is an external action: approve and dispatch with the "
            "typed confirmation phrase to run it.\n"
            f"Mode: {'interactive (fills/clicks/keypresses)' if interactive else 'read-only (navigate/read/screenshot)'}\n\n"
        )
        lines = [f"{i + 1}. {_summarize_step(step)}" for i, step in enumerate(steps)]
        return header + "\n".join(lines)


# ---- Playwright execution (isolated from the caller's event loop) ----
def run_browser_flow(steps: list[dict[str, Any]], *, options: dict[str, Any]) -> dict[str, Any]:
    """Execute a validated flow, returning a structured, value-redacted result.

    Playwright's *sync* API refuses to run inside a running asyncio loop, and the
    dispatch handler may be invoked on the server's event-loop thread. So the
    whole flow runs on a dedicated worker thread with no loop of its own — this
    keeps the handler synchronous (matching every other handler) while staying
    safe regardless of how dispatch was called.
    """
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="zade-browser") as pool:
        return pool.submit(_execute_flow, steps, options).result()


def _execute_flow(steps: list[dict[str, Any]], options: dict[str, Any]) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
        raise BrowserNotAvailable(
            "Browser automation needs Playwright. Install it with: "
            "pip install \"local-ai-cofounder-kernel[browser]\" && python -m playwright install chromium"
        ) from exc

    nav_timeout = int(options.get("nav_timeout_ms", 30000))
    action_timeout = int(options.get("action_timeout_ms", 15000))
    browser_name = str(options.get("browser", "chromium"))
    headless = bool(options.get("headless", False))

    step_logs: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    ok = True
    failed_step: int | None = None
    error = ""

    with sync_playwright() as pw:
        browser_type = getattr(pw, browser_name, None)
        if browser_type is None:
            raise ValueError(f"Unknown browser type: {browser_name!r} (use chromium/firefox/webkit).")
        try:
            browser = browser_type.launch(headless=headless)
        except Exception as exc:  # noqa: BLE001 - surface a readable setup error
            raise BrowserNotAvailable(
                f"Failed to launch {browser_name}: {exc}. Run: python -m playwright install {browser_name}"
            ) from exc
        context = browser.new_context()
        allowed_origin = str(options.get("allowed_origin") or "")
        if allowed_origin:
            context.route(
                "**/*",
                lambda route: (
                    route.continue_()
                    if _request_allowed(route.request.url, allowed_origin)
                    else route.abort("blockedbyclient")
                ),
            )
        trace_path = str(options.get("trace_path") or "").strip()
        if trace_path:
            Path(trace_path).parent.mkdir(parents=True, exist_ok=True)
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
        try:
            page = context.new_page()
            page.set_default_timeout(action_timeout)
            for index, step in enumerate(steps):
                try:
                    step_logs.append(_run_step(page, step, nav_timeout=nav_timeout, action_timeout=action_timeout))
                except Exception as exc:  # noqa: BLE001 - one bad step stops the flow, reported not raised
                    ok = False
                    failed_step = index
                    error = f"Step {index} ({step['type']}) failed: {_clean_error(exc)}"
                    step_logs.append({"type": step["type"], "status": "error", "error": error})
                    break
                pages.append({"url": page.url, "title": _safe_title(page)})
        finally:
            if trace_path:
                context.tracing.stop(path=trace_path)
            context.close()
            browser.close()

    return {
        "ok": ok,
        "failed_step": failed_step,
        "error": error,
        "steps": step_logs,
        "pages": pages,
        "trace": trace_path,
    }


def _origin(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    port = parsed.port
    default = 443 if parsed.scheme == "https" else 80
    authority = parsed.hostname or ""
    if port is not None and port != default:
        authority = f"{authority}:{port}"
    return f"{parsed.scheme}://{authority}"


def _request_allowed(url: str, allowed_origin: str) -> bool:
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme in {"data", "blob"}:
        return True
    return scheme in {"http", "https"} and _origin(url) == allowed_origin


def _run_step(page: Any, step: dict[str, Any], *, nav_timeout: int, action_timeout: int) -> dict[str, Any]:
    step_type = step["type"]
    if step_type == "navigate":
        page.goto(step["url"], wait_until="domcontentloaded", timeout=nav_timeout)
        return {"type": "navigate", "status": "ok", "url": page.url, "title": _safe_title(page)}
    if step_type == "wait":
        if step.get("selector"):
            page.wait_for_selector(step["selector"], timeout=action_timeout)
        if step.get("ms") is not None:
            page.wait_for_timeout(int(step["ms"]))
        return {"type": "wait", "status": "ok", "selector": step.get("selector", ""), "ms": step.get("ms")}
    if step_type == "read":
        if step.get("selector"):
            text = page.locator(step["selector"]).first.inner_text(timeout=action_timeout)
        else:
            text = page.inner_text("body", timeout=action_timeout)
        return {"type": "read", "status": "ok", "selector": step.get("selector", ""), "text": text[:MAX_TEXT_CHARS]}
    if step_type == "links":
        scope = step.get("selector") or "body"
        raw = page.eval_on_selector_all(
            f"{scope} a[href]",
            "els => els.map(e => ({text: (e.innerText || '').trim(), href: e.href}))",
        )
        links = [{"text": str(link.get("text", ""))[:200], "href": str(link.get("href", ""))} for link in raw[:MAX_LINKS]]
        return {"type": "links", "status": "ok", "count": len(links), "links": links}
    if step_type == "fill":
        value, source = _resolve_value(step)
        page.locator(step["selector"]).first.fill(value, timeout=action_timeout)
        return {"type": "fill", "status": "ok", "selector": step["selector"], "value": REDACTED, "source": source}
    if step_type == "click":
        page.locator(step["selector"]).first.click(timeout=action_timeout)
        return {"type": "click", "status": "ok", "selector": step["selector"]}
    if step_type == "press":
        if step.get("selector"):
            page.locator(step["selector"]).first.press(step["key"], timeout=action_timeout)
        else:
            page.keyboard.press(step["key"])
        return {"type": "press", "status": "ok", "selector": step.get("selector", ""), "key": step["key"]}
    if step_type == "screenshot":
        page.screenshot(path=step["path"], full_page=step.get("full_page", False))
        return {"type": "screenshot", "status": "ok", "path": step["path"], "full_page": step.get("full_page", False)}
    raise ValueError(f"Unhandled step type: {step_type}")


def _resolve_value(step: dict[str, Any]) -> tuple[str, str]:
    """Resolve a fill value, reading a secret from the environment when named.

    Returns (value, source) where source is 'literal' or 'env:NAME' so the
    redacted log records where the value came from without recording the value.
    """
    env_name = str(step.get("value_env", "")).strip()
    if env_name:
        value = os.environ.get(env_name, "")
        if not value:
            raise ValueError(f"value_env is not set in the environment: {env_name}")
        return value, f"env:{env_name}"
    return str(step.get("value", "")), "literal"


def _playwright_available() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("playwright") is not None
    except Exception:  # noqa: BLE001
        return False


def _first_navigation(steps: list[dict[str, Any]]) -> str:
    for step in steps:
        if step.get("type") == "navigate":
            return str(step.get("url", ""))
    return ""


def _summarize_step(step: dict[str, Any]) -> str:
    step_type = step["type"]
    if step_type == "navigate":
        return f"navigate to {step['url']}"
    if step_type == "wait":
        parts = []
        if step.get("selector"):
            parts.append(f"for {step['selector']}")
        if step.get("ms") is not None:
            parts.append(f"{step['ms']}ms")
        return "wait " + " / ".join(parts)
    if step_type == "read":
        return f"read text from {step.get('selector') or 'page body'}"
    if step_type == "links":
        return f"extract links from {step.get('selector') or 'page'}"
    if step_type == "fill":
        source = f"env:{step['value_env']}" if step.get("value_env") else "literal value"
        return f"fill {step['selector']} with {REDACTED} ({source})"
    if step_type == "click":
        return f"click {step['selector']}"
    if step_type == "press":
        target = step.get("selector") or "page"
        return f"press {step['key']} on {target}"
    if step_type == "screenshot":
        return f"screenshot to {step['path']}"
    return step_type


def _require_selector(raw: dict[str, Any], index: int) -> str:
    selector = str(raw.get("selector", "")).strip()
    if not selector:
        raise ValueError(f"Step {index} '{raw.get('type')}' needs a selector.")
    return selector


def _safe_title(page: Any) -> str:
    try:
        return page.title()
    except Exception:  # noqa: BLE001 - title is best-effort context, never fatal
        return ""


def _clean_error(exc: Exception) -> str:
    # Playwright errors are multi-line with a call log; keep the first line for a
    # readable audit/result message.
    return str(exc).strip().splitlines()[0][:300] if str(exc).strip() else exc.__class__.__name__
