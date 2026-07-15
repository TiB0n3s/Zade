from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable

from .generators.action_handlers import render_action_handlers
from .generators.capabilities import render_capabilities
from .generators.integrations import render_integrations
from .generators.recent_activity import render_recent_activity
from .generators.runtime_prompt_wiring import render_runtime_prompt_wiring
from .generators.skills import render_skills
from .generators.voice_loop import render_voice_loop
from .parser import render_auto_blocks


Generator = Callable[[Any], str]


GENERATORS: dict[str, Generator] = {
    "capabilities": render_capabilities,
    "action-handlers": render_action_handlers,
    "skills": render_skills,
    "integrations": render_integrations,
    "voice-loop": render_voice_loop,
    "runtime-prompt-wiring": render_runtime_prompt_wiring,
    "recent-activity": render_recent_activity,
}


def render_self_knowledge(text: str, snapshots: Mapping[str, Any]) -> str:
    replacements = {}
    for name, generator in GENERATORS.items():
        if name in snapshots:
            replacements[name] = generator(snapshots[name])
    return render_auto_blocks(text, replacements)
