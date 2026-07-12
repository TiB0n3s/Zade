from __future__ import annotations

import base64
import binascii
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import KernelConfig
from .db import KernelDatabase
from .runtime import RuntimeService


CONTRARIAN_MARKER = "\n---\nContrarian check"
STDERR_EXCERPT_CHARS = 400
SPOKEN_TEXT_MAX_CHARS = 4000


class VoiceService:
    """Local voice loop: founder-configured STT/TTS engines around the governed runtime.

    The engines are argv command templates (e.g. whisper.cpp and piper) run
    without a shell; text to speak reaches TTS via stdin and never a command
    line. Voice is an interface, not a bypass: converse() transcribes, then
    answers through runtime.respond so authority, charters, episodic memory,
    and the contrarian pass all apply, then synthesizes the reply.
    """

    def __init__(self, *, config: KernelConfig, db: KernelDatabase, runtime: RuntimeService):
        self.config = config
        self.db = db
        self.runtime = runtime

    @property
    def voice_dir(self) -> Path:
        return self.config.paths.data_dir / "voice"

    def status(self) -> dict[str, Any]:
        stt = self.config.voice.stt_command
        tts = self.config.voice.tts_command
        return {
            "stt": {
                "configured": bool(stt),
                "command": list(stt),
                "binary_found": _binary_found(stt),
            },
            "tts": {
                "configured": bool(tts),
                "command": list(tts),
                "binary_found": _binary_found(tts),
            },
            "ready": bool(stt) and bool(tts),
            "voice_dir": str(self.voice_dir),
            "timeout_seconds": self.config.voice.timeout_seconds,
        }

    def transcribe(self, *, audio_base64: str) -> dict[str, Any]:
        if not self.config.voice.stt_configured:
            raise VoiceNotConfigured("STT engine is not configured; set [voice] stt_command in config.toml.")
        audio_bytes = _decode_audio(audio_base64)
        stamp = _stamp()
        audio_path = self._write_artifact(f"{stamp}-in.wav", audio_bytes)
        transcript_path = self.voice_dir / f"{stamp}-transcript.txt"
        started = time.perf_counter()
        self._run_engine(
            self.config.voice.stt_command,
            replacements={
                "{audio}": str(audio_path),
                "{transcript}": str(transcript_path),
                "{transcript_base}": str(transcript_path.with_suffix("")),
            },
            engine="stt",
        )
        text = _read_transcript(transcript_path)
        latency_ms = int((time.perf_counter() - started) * 1000)
        if not text:
            raise ValueError("Transcription produced no text.")
        self.db.audit(
            actor="voice",
            action="voice.transcribe",
            target=str(audio_path),
            permission_tier="L0_READ",
            status="ok",
            details={"latency_ms": latency_ms, "chars": len(text), "audio_bytes": len(audio_bytes)},
        )
        return {
            "text": text,
            "latency_ms": latency_ms,
            "audio_path": str(audio_path),
            "transcript_path": str(transcript_path),
        }

    def speak(self, *, text: str) -> dict[str, Any]:
        if not self.config.voice.tts_configured:
            raise VoiceNotConfigured("TTS engine is not configured; set [voice] tts_command in config.toml.")
        spoken = text.strip()[:SPOKEN_TEXT_MAX_CHARS]
        if not spoken:
            raise ValueError("Nothing to speak.")
        stamp = _stamp()
        output_path = self.voice_dir / f"{stamp}-out.wav"
        self.voice_dir.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()
        self._run_engine(
            self.config.voice.tts_command,
            replacements={"{output}": str(output_path)},
            engine="tts",
            stdin_text=spoken,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise ValueError("TTS engine produced no audio output.")
        audio_bytes = output_path.read_bytes()
        self.db.audit(
            actor="voice",
            action="voice.speak",
            target=str(output_path),
            permission_tier="L0_READ",
            status="ok",
            details={"latency_ms": latency_ms, "chars": len(spoken), "audio_bytes": len(audio_bytes)},
        )
        return {
            "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
            "audio_path": str(output_path),
            "format": "wav",
            "latency_ms": latency_ms,
            "spoken_chars": len(spoken),
        }

    def converse(
        self,
        *,
        audio_base64: str,
        conversation_id: int | None = None,
        task_type: str = "general",
        contrarian: bool | None = None,
        use_semantic_memory: bool = True,
        speak_response: bool = True,
        speak_full: bool = False,
    ) -> dict[str, Any]:
        transcription = self.transcribe(audio_base64=audio_base64)
        response = self.runtime.respond(
            message=transcription["text"],
            task_type=task_type,  # type: ignore[arg-type]
            conversation_id=conversation_id,
            contrarian=contrarian,
            use_semantic_memory=use_semantic_memory,
        )
        spoken_text = response["response"] if speak_full else _strip_contrarian_block(response["response"])
        speech: dict[str, Any] | None = None
        speech_error = ""
        if speak_response:
            try:
                speech = self.speak(text=spoken_text)
            except VoiceNotConfigured as exc:
                speech_error = str(exc)
            except ValueError as exc:
                speech_error = str(exc)
        self.db.audit(
            actor="voice",
            action="voice.converse",
            target=f"conversation:{conversation_id}" if conversation_id else "local_runtime",
            permission_tier="L0_READ",
            status="ok" if not speech_error else "degraded",
            details={
                "event_id": response["event_id"],
                "transcript_chars": len(transcription["text"]),
                "spoke": speech is not None,
                "speech_error": speech_error,
            },
        )
        return {
            "transcript": transcription["text"],
            "transcription": transcription,
            "response": response["response"],
            "spoken_text": spoken_text,
            "speech": speech,
            "speech_error": speech_error,
            "event_id": response["event_id"],
            "model": response["model"],
            "authority": response["authority"],
            "governor": response["governor"],
            "conversation": response.get("conversation"),
            "contrarian": response.get("contrarian"),
        }

    def _write_artifact(self, name: str, data: bytes) -> Path:
        self.voice_dir.mkdir(parents=True, exist_ok=True)
        path = self.voice_dir / name
        path.write_bytes(data)
        return path

    def _run_engine(
        self,
        command: tuple[str, ...],
        *,
        replacements: dict[str, str],
        engine: str,
        stdin_text: str | None = None,
    ) -> None:
        argv = [_substitute(argument, replacements) for argument in command]
        try:
            completed = subprocess.run(  # noqa: S603 - argv from founder config, no shell
                argv,
                input=stdin_text.encode("utf-8") if stdin_text is not None else None,
                capture_output=True,
                timeout=self.config.voice.timeout_seconds,
                shell=False,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ValueError(f"Voice {engine} engine binary not found: {argv[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ValueError(
                f"Voice {engine} engine timed out after {self.config.voice.timeout_seconds:.0f}s."
            ) from exc
        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace")[:STDERR_EXCERPT_CHARS]
            raise ValueError(f"Voice {engine} engine failed (exit {completed.returncode}): {stderr}")


class VoiceNotConfigured(ValueError):
    """Raised when a voice endpoint is used without a configured engine."""


def _substitute(argument: str, replacements: dict[str, str]) -> str:
    for token, value in replacements.items():
        argument = argument.replace(token, value)
    return argument


def _decode_audio(audio_base64: str) -> bytes:
    try:
        data = base64.b64decode(audio_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("audio_base64 is not valid base64.") from exc
    if not data:
        raise ValueError("audio_base64 decoded to empty audio.")
    return data


def _read_transcript(transcript_path: Path) -> str:
    candidates = [transcript_path, transcript_path.with_suffix(transcript_path.suffix + ".txt")]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8", errors="replace").strip()
    return ""


def _strip_contrarian_block(text: str) -> str:
    index = text.find(CONTRARIAN_MARKER)
    if index == -1:
        return text
    return text[:index].strip()


def _binary_found(command: tuple[str, ...]) -> bool:
    if not command:
        return False
    executable = command[0]
    return bool(shutil.which(executable)) or Path(executable).is_file()


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
