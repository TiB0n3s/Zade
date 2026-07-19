from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from cofounder_kernel.config import KernelConfig, load_config
from cofounder_kernel.self_knowledge.renderer import render_self_knowledge


DEFAULT_DOC_PATH = Path("context/self/zade.md")


def collect_snapshots(
    *,
    config: KernelConfig | None = None,
    repo_root: Path | None = None,
    doc_path: Path = DEFAULT_DOC_PATH,
) -> dict[str, Any]:
    cfg = config or load_config()
    root = repo_root or Path.cwd()
    app = _create_runtime_app(cfg)
    state = app.state

    return {
        "capabilities": _safe("capabilities", lambda: state.tools.list_tools()),
        "action-handlers": _safe("action-handlers", lambda: state.handlers.list_handlers()),
        "skills": _safe("skills", lambda: state.skills.list_skills(limit=25)),
        "integrations": _safe("integrations", lambda: _collect_integrations(state)),
        "voice-loop": _safe("voice-loop", lambda: state.voice.status()),
        "runtime-prompt-wiring": _runtime_prompt_wiring_snapshot(doc_path),
        "recent-activity": collect_recent_activity(repo_root=root),
    }


def refresh_doc(
    *,
    doc_path: Path = DEFAULT_DOC_PATH,
    config: KernelConfig | None = None,
    repo_root: Path | None = None,
    snapshots: dict[str, Any] | None = None,
) -> dict[str, Any]:
    path = Path(doc_path)
    text = path.read_text(encoding="utf-8")
    collected = collect_snapshots(config=config, repo_root=repo_root, doc_path=path)
    if snapshots:
        collected.update(snapshots)
    rendered = render_self_knowledge(text, collected).rstrip() + "\n"
    changed = rendered != text
    if changed:
        path.write_text(rendered, encoding="utf-8")
    return {"path": str(path), "changed": changed}


def collect_recent_activity(*, repo_root: Path, days: int = 14, limit: int = 12) -> list[dict[str, str]]:
    try:
        completed = subprocess.run(
            [
                "git",
                "log",
                f"--since={days} days ago",
                f"--max-count={limit}",
                "--date=short",
                "--format=%h%x09%ad%x09%s",
            ],
            cwd=repo_root,
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except Exception:
        return []
    if completed.returncode != 0:
        return []
    commits: list[dict[str, str]] = []
    for line in completed.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        commits.append({"hash": parts[0], "date": parts[1], "subject": parts[2]})
    return commits


def _collect_integrations(state: Any) -> list[dict[str, str]]:
    cfg = state.config
    roles = cfg.ollama.roles()
    integrations = [
        {
            "name": "Ollama",
            "mode": "local",
            "source": "config.ollama",
            "summary": f"Models at {cfg.ollama.base_url}; chat={roles['general']}, reasoning={roles['reasoning']}.",
        },
        {
            "name": "SQLite memory",
            "mode": "local",
            "source": "config.paths.database_path",
            "summary": f"Structured memory, audit, work queue, and registry state at {cfg.paths.database_path}.",
        },
        {
            "name": "AI Brain hot/cold roots",
            "mode": "local",
            "source": "config.paths",
            "summary": f"Hot root {cfg.paths.hot_root}; cold root {cfg.paths.cold_root}.",
        },
        {
            "name": "Read-only connectors",
            "mode": "external-read",
            "source": "ConnectorService",
            "summary": "IMAP and ICS connector routes are mounted; sync dispatches through registered app handlers.",
        },
        {
            "name": "Trading-bot bridge",
            "mode": "local WSL",
            "source": "config.trading_bot",
            "summary": f"Enabled={cfg.trading_bot.enabled}; distro={cfg.trading_bot.wsl_distro}; repo={cfg.trading_bot.repo_path}.",
        },
        {
            "name": "Browser automation",
            "mode": "approved external action",
            "source": "config.browser",
            "summary": f"Enabled={cfg.browser.enabled}; engine={cfg.browser.browser}; headless={cfg.browser.headless}.",
        },
        {
            "name": "Web research",
            "mode": "approved external action",
            "source": "config.research",
            "summary": f"Enabled={cfg.research.enabled}; max URLs/run={cfg.research.max_urls_per_run}.",
        },
        {
            "name": "Durable product builds",
            "mode": "local-first governed execution",
            "source": "BuildOrchestrator",
            "summary": (
                "Discovery-through-release task graphs, background controls, governed commands, "
                "toolchain verification, artifacts, and calibration are persisted locally."
            ),
        },
        {
            "name": "Anthropic build delegation",
            "mode": "optional provider lease",
            "source": "config.anthropic + config.build",
            "summary": (
                f"Enabled={cfg.anthropic.enabled}; model={cfg.build.anthropic_pricing.model}; "
                "source-code egress and paid turns require a matching founder-approved lease."
            ),
        },
        {
            "name": "GitHub Actions build evidence",
            "mode": "governed external CI",
            "source": "GitHubCIClient",
            "summary": (
                f"Read-only run evidence is available through gh; writes require fresh approval; "
                f"configured iOS workflow={cfg.build.ios_workflow}."
            ),
        },
        {
            "name": "OpenAI build review",
            "mode": "optional advisory provider",
            "source": "config.openai_review",
            "summary": (
                f"Enabled={cfg.openai_review.enabled}; model={cfg.openai_review.model}; "
                "store=false, no hosted tools, separate provider lease."
            ),
        },
    ]
    # Voice is local-only (whisper.cpp + piper via [voice] command engines);
    # the cloud speech integrations (Deepgram/ElevenLabs) were removed 2026-07-19.
    return integrations


def _create_runtime_app(config: KernelConfig) -> Any:
    from cofounder_kernel import api as api_module

    # Introspection only: enumerate handlers/tools/skills. Never run serving-boot
    # maintenance — it would reindex and, worse, sweep/end conversations in the
    # DB this snapshot merely wants to read.
    return api_module.create_app(config, run_boot_maintenance=False)


def _runtime_prompt_wiring_snapshot(doc_path: Path) -> dict[str, str]:
    return {
        "prompt_builder": "cofounder_kernel.runtime.RuntimeService._build_governed_prompt",
        "self_knowledge_method": "cofounder_kernel.runtime.RuntimeService._render_self_knowledge",
        "doc_path": str(doc_path).replace("\\", "/"),
    }


def _safe(block_name: str, collector: Any) -> Any:
    try:
        return collector()
    except Exception as exc:
        return {"unavailable": block_name, "reason": str(exc)}
