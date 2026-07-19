from cofounder_kernel.self_knowledge.generators.action_handlers import render_action_handlers
from cofounder_kernel.self_knowledge.generators.capabilities import render_capabilities
from cofounder_kernel.self_knowledge.generators.integrations import render_integrations
from cofounder_kernel.self_knowledge.generators.recent_activity import render_recent_activity
from cofounder_kernel.self_knowledge.generators.runtime_prompt_wiring import render_runtime_prompt_wiring
from cofounder_kernel.self_knowledge.generators.skills import render_skills
from cofounder_kernel.self_knowledge.generators.voice_loop import render_voice_loop
from cofounder_kernel.self_knowledge.renderer import render_self_knowledge


def test_capabilities_generator_renders_fixture_tools() -> None:
    markdown = render_capabilities(
        [
            {
                "name": "memory.search",
                "description": "Search local memory using SQLite FTS.",
                "permission_tier": "L0_READ",
            },
            {
                "name": "audit.recent",
                "description": "Read recent local audit events.",
                "permission_tier": "L0_READ",
            },
        ]
    )

    assert markdown == (
        "| Name | Category | Permission | Description |\n"
        "| --- | --- | --- | --- |\n"
        "| `audit.recent` | audit | `L0_READ` | Read recent local audit events. |\n"
        "| `memory.search` | memory | `L0_READ` | Search local memory using SQLite FTS. |"
    )


def test_action_handlers_generator_renders_fixture_handlers() -> None:
    markdown = render_action_handlers(
        [
            {"action": "local.memory.write", "description": "Write a memory.", "enabled": True},
            {"action": "external.browser.run", "description": "Run browser flow.", "enabled": False},
        ]
    )

    assert markdown == (
        "| Action | Category | Enabled | Description |\n"
        "| --- | --- | --- | --- |\n"
        "| `external.browser.run` | external | no | Run browser flow. |\n"
        "| `local.memory.write` | local | yes | Write a memory. |"
    )


def test_generators_clip_long_descriptions() -> None:
    long_description = "A" * 220

    markdown = render_capabilities(
        [{"name": "memory.write", "description": long_description, "permission_tier": "L1_MEMORY_WRITE"}]
    )

    assert ("A" * 157) + "..." in markdown
    assert long_description not in markdown


def test_integrations_generator_renders_fixture_integrations() -> None:
    markdown = render_integrations(
        [
            {
                "name": "Ollama",
                "mode": "local",
                "source": "config.ollama",
                "summary": "Chat model qwen3:14b at http://127.0.0.1:11434.",
            },
            {
                "name": "Trading-bot bridge",
                "mode": "local WSL",
                "source": "config.trading_bot",
                "summary": "Read-only activity snapshot bridge.",
            },
        ]
    )

    assert markdown == (
        "| Name | Mode | Source | Summary |\n"
        "| --- | --- | --- | --- |\n"
        "| Ollama | local | `config.ollama` | Chat model qwen3:14b at http://127.0.0.1:11434. |\n"
        "| Trading-bot bridge | local WSL | `config.trading_bot` | Read-only activity snapshot bridge. |"
    )


def test_voice_loop_generator_renders_fixture_status() -> None:
    markdown = render_voice_loop(
        {
            "stt": {"engine": "command", "configured": True},
            "tts": {"engine": "command", "configured": True},
            "ready": True,
            "cloud_engines_in_use": False,
            "timeout_seconds": 120.0,
        }
    )

    assert markdown == (
        "- Pipeline: browser audio -> STT -> governed `runtime.respond()` -> TTS -> browser playback.\n"
        "- Streaming posture: `/voice/converse/stream` streams draft tokens + sentence-chunked TTS; "
        "spoken audio is always the governed final text. Batch `/voice/converse` remains.\n"
        "- STT: `command` (configured, local).\n"
        "- TTS: `command` (configured, local).\n"
        "- Ready: yes; cloud engines in use: no; timeout: 120s."
    )


def test_skills_runtime_wiring_and_recent_activity_generators() -> None:
    assert render_skills(
        {
            "summary": {"total": 3, "enabled": 2, "by_risk_tier": {"low": 2, "high": 1}},
            "items": [
                {"name": "research", "description": "Investigate against sources.", "enabled": True},
                {"name": "ads", "description": "Plan paid campaigns.", "enabled": False},
            ],
        }
    ) == (
        "- Registered skills: 3 total, 2 enabled.\n"
        "- Risk tiers: high=1, low=2.\n"
        "| Name | Enabled | Description |\n"
        "| --- | --- | --- |\n"
        "| `ads` | no | Plan paid campaigns. |\n"
        "| `research` | yes | Investigate against sources. |"
    )

    assert render_runtime_prompt_wiring(
        {
            "prompt_builder": "cofounder_kernel.runtime.RuntimeService._build_governed_prompt",
            "self_knowledge_method": "cofounder_kernel.runtime.RuntimeService._render_self_knowledge",
            "doc_path": "context/self/zade.md",
        }
    ) == (
        "- Prompt builder: `cofounder_kernel.runtime.RuntimeService._build_governed_prompt`.\n"
        "- Current runtime self-knowledge source: `cofounder_kernel.runtime.RuntimeService._render_self_knowledge`.\n"
        "- Living document path: `context/self/zade.md`.\n"
        "- Injection point: the `Your capabilities` section of the governed prompt."
    )

    assert render_recent_activity(
        [
            {"hash": "abc1234", "date": "2026-07-15", "subject": "Add self-knowledge parser"},
            {"hash": "def5678", "date": "2026-07-14", "subject": "Fix runtime prompt"},
        ]
    ) == (
        "- `abc1234` 2026-07-15 - Add self-knowledge parser\n"
        "- `def5678` 2026-07-14 - Fix runtime prompt"
    )


def test_renderer_replaces_all_known_auto_blocks_from_snapshots() -> None:
    template = (
        "Intro stays.\n"
        "<!-- AUTO-START: capabilities -->\nold\n<!-- AUTO-END: capabilities -->\n"
        "Between stays.\n"
        "<!-- AUTO-START: action-handlers -->\nold\n<!-- AUTO-END: action-handlers -->\n"
        "<!-- AUTO-START: skills -->\nold\n<!-- AUTO-END: skills -->\n"
        "<!-- AUTO-START: integrations -->\nold\n<!-- AUTO-END: integrations -->\n"
        "<!-- AUTO-START: voice-loop -->\nold\n<!-- AUTO-END: voice-loop -->\n"
        "<!-- AUTO-START: runtime-prompt-wiring -->\nold\n<!-- AUTO-END: runtime-prompt-wiring -->\n"
        "<!-- AUTO-START: recent-activity -->\nold\n<!-- AUTO-END: recent-activity -->\n"
        "Outro stays.\n"
    )

    rendered = render_self_knowledge(
        template,
        {
            "capabilities": [{"name": "memory.search", "description": "Search.", "permission_tier": "L0_READ"}],
            "action-handlers": [{"action": "local.noop", "description": "No-op.", "enabled": True}],
            "skills": {"summary": {"total": 0, "enabled": 0}, "items": []},
            "integrations": [{"name": "Ollama", "mode": "local", "source": "config.ollama", "summary": "Local."}],
            "voice-loop": {"stt": {"engine": "command", "configured": False}, "tts": {"engine": "command", "configured": False}},
            "runtime-prompt-wiring": {
                "prompt_builder": "builder",
                "self_knowledge_method": "method",
                "doc_path": "context/self/zade.md",
            },
            "recent-activity": [{"hash": "abc1234", "date": "2026-07-15", "subject": "Commit"}],
        },
    )

    assert "Intro stays." in rendered
    assert "Between stays." in rendered
    assert "Outro stays." in rendered
    assert "`memory.search`" in rendered
    assert "`local.noop`" in rendered
    assert "Registered skills: 0 total, 0 enabled." in rendered
    assert "Ollama" in rendered
    assert "Pipeline: browser audio" in rendered
    assert "Prompt builder: `builder`." in rendered
    assert "`abc1234` 2026-07-15 - Commit" in rendered
