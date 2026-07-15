from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .common import code, unavailable, yes_no


def render_voice_loop(status: Mapping[str, Any]) -> str:
    try:
        stt = _engine_line("STT", status.get("stt") or {})
        tts = _engine_line("TTS", status.get("tts") or {})
        timeout = status.get("timeout_seconds", "")
        timeout_text = _format_seconds(timeout)
        return "\n".join(
            [
                "- Pipeline: browser audio -> STT -> governed `runtime.respond()` -> TTS -> browser playback.",
                "- Streaming posture: batch non-streaming; first model token and streaming TTS are not exposed yet.",
                stt,
                tts,
                (
                    f"- Ready: {yes_no(status.get('ready', False))}; "
                    f"cloud engines in use: {yes_no(status.get('cloud_engines_in_use', False))}; "
                    f"timeout: {timeout_text}."
                ),
            ]
        )
    except Exception as exc:
        return unavailable("voice-loop", str(exc))


def _engine_line(label: str, engine: Mapping[str, Any]) -> str:
    name = str(engine.get("engine") or "unknown")
    configured = "configured" if bool(engine.get("configured")) else "not configured"
    locality = "cloud" if bool(engine.get("cloud")) else "local"
    model = str(engine.get("model") or "").strip()
    model_text = f", model {code(model)}" if model else ""
    return f"- {label}: {code(name)} ({configured}, {locality}{model_text})."


def _format_seconds(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value) or "unknown"
    if number.is_integer():
        return f"{int(number)}s"
    return f"{number:g}s"
