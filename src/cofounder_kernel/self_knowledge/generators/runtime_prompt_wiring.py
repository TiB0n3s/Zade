from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .common import code, unavailable


def render_runtime_prompt_wiring(snapshot: Mapping[str, Any]) -> str:
    try:
        return "\n".join(
            [
                f"- Prompt builder: {code(snapshot.get('prompt_builder', 'unknown'))}.",
                (
                    "- Current runtime self-knowledge source: "
                    f"{code(snapshot.get('self_knowledge_method', 'unknown'))}."
                ),
                f"- Living document path: {code(snapshot.get('doc_path', 'context/self/zade.md'))}.",
                "- Injection point: the `Your capabilities` section of the governed prompt.",
            ]
        )
    except Exception as exc:
        return unavailable("runtime-prompt-wiring", str(exc))
