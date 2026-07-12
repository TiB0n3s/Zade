from __future__ import annotations

import base64
import binascii
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import KernelConfig
from .db import KernelDatabase
from .runtime import RuntimeService


CONTRARIAN_MARKER = "\n---\nContrarian check"
STDERR_EXCERPT_CHARS = 400
SPOKEN_TEXT_MAX_CHARS = 4000

STT_ENGINES = {"command", "deepgram"}
TTS_ENGINES = {"command", "elevenlabs"}
DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"
ELEVENLABS_URL = "https://api.elevenlabs.io/v1/text-to-speech"


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
        voice = self.config.voice
        stt: dict[str, Any] = {"engine": voice.stt_engine, "configured": voice.stt_configured}
        if voice.stt_engine == "command":
            stt |= {"command": list(voice.stt_command), "binary_found": _binary_found(voice.stt_command)}
        else:
            stt |= {
                "model": voice.stt_model,
                "credential_env": voice.stt_api_key_env,
                "credential_set": bool(os.environ.get(voice.stt_api_key_env)),
                "cloud": True,
            }
        tts: dict[str, Any] = {"engine": voice.tts_engine, "configured": voice.tts_configured}
        if voice.tts_engine == "command":
            tts |= {"command": list(voice.tts_command), "binary_found": _binary_found(voice.tts_command)}
        else:
            tts |= {
                "model": voice.tts_model,
                "voice_id": voice.tts_voice,
                "credential_env": voice.tts_api_key_env,
                "credential_set": bool(os.environ.get(voice.tts_api_key_env)),
                "cloud": True,
            }
        return {
            "stt": stt,
            "tts": tts,
            "ready": voice.stt_configured and voice.tts_configured,
            "cloud_engines_in_use": voice.stt_engine != "command" or voice.tts_engine != "command",
            "voice_dir": str(self.voice_dir),
            "timeout_seconds": voice.timeout_seconds,
        }

    def transcribe(self, *, audio_base64: str, audio_mime: str = "audio/wav") -> dict[str, Any]:
        engine = self.config.voice.stt_engine
        if engine not in STT_ENGINES:
            raise ValueError(f"Unknown STT engine: {engine}. Supported: {', '.join(sorted(STT_ENGINES))}")
        if not self.config.voice.stt_configured:
            raise VoiceNotConfigured("STT engine is not configured; set [voice] stt_command in config.toml.")
        audio_bytes = _decode_audio(audio_base64)
        stamp = _stamp()
        audio_path = self._write_artifact(f"{stamp}-in{_ext_for_mime(audio_mime)}", audio_bytes)
        transcript_path = self.voice_dir / f"{stamp}-transcript.txt"
        started = time.perf_counter()
        if engine == "deepgram":
            text = self._transcribe_deepgram(audio_bytes, mime=audio_mime)
            transcript_path.write_text(text, encoding="utf-8")
        else:
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
            details={"engine": engine, "latency_ms": latency_ms, "chars": len(text), "audio_bytes": len(audio_bytes)},
        )
        return {
            "text": text,
            "engine": engine,
            "audio_mime": audio_mime,
            "latency_ms": latency_ms,
            "audio_path": str(audio_path),
            "transcript_path": str(transcript_path),
        }

    def speak(self, *, text: str) -> dict[str, Any]:
        engine = self.config.voice.tts_engine
        if engine not in TTS_ENGINES:
            raise ValueError(f"Unknown TTS engine: {engine}. Supported: {', '.join(sorted(TTS_ENGINES))}")
        if not self.config.voice.tts_configured:
            raise VoiceNotConfigured("TTS engine is not configured; set [voice] tts_command in config.toml.")
        spoken = text.strip()[:SPOKEN_TEXT_MAX_CHARS]
        if not spoken:
            raise ValueError("Nothing to speak.")
        stamp = _stamp()
        audio_format = "mp3" if engine == "elevenlabs" else "wav"
        output_path = self.voice_dir / f"{stamp}-out.{audio_format}"
        self.voice_dir.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()
        if engine == "elevenlabs":
            audio_bytes = self._speak_elevenlabs(spoken)
            output_path.write_bytes(audio_bytes)
        else:
            self._run_engine(
                self.config.voice.tts_command,
                replacements={"{output}": str(output_path)},
                engine="tts",
                stdin_text=spoken,
            )
            if not output_path.is_file() or output_path.stat().st_size == 0:
                raise ValueError("TTS engine produced no audio output.")
            audio_bytes = output_path.read_bytes()
        latency_ms = int((time.perf_counter() - started) * 1000)
        self.db.audit(
            actor="voice",
            action="voice.speak",
            target=str(output_path),
            permission_tier="L0_READ",
            status="ok",
            details={"engine": engine, "latency_ms": latency_ms, "chars": len(spoken), "audio_bytes": len(audio_bytes)},
        )
        return {
            "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
            "audio_path": str(output_path),
            "format": audio_format,
            "engine": engine,
            "latency_ms": latency_ms,
            "spoken_chars": len(spoken),
        }

    def converse(
        self,
        *,
        audio_base64: str,
        audio_mime: str = "audio/wav",
        conversation_id: int | None = None,
        task_type: str = "general",
        contrarian: bool | None = None,
        use_semantic_memory: bool = True,
        speak_response: bool = True,
        speak_full: bool = False,
    ) -> dict[str, Any]:
        transcription = self.transcribe(audio_base64=audio_base64, audio_mime=audio_mime)
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

    def _transcribe_deepgram(self, audio_bytes: bytes, *, mime: str = "audio/wav") -> str:
        api_key = self._require_api_key(self.config.voice.stt_api_key_env, engine="Deepgram")
        params = urllib.parse.urlencode({"model": self.config.voice.stt_model, "smart_format": "true"})
        request = urllib.request.Request(
            f"{DEEPGRAM_URL}?{params}",
            data=audio_bytes,
            headers={"Authorization": f"Token {api_key}", "Content-Type": _clean_mime(mime)},
            method="POST",
        )
        payload = json.loads(self._http_call(request, engine="Deepgram STT").decode("utf-8", errors="replace"))
        try:
            transcript = payload["results"]["channels"][0]["alternatives"][0]["transcript"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"Deepgram STT returned an unexpected response shape: {exc}") from exc
        return str(transcript).strip()

    def _speak_elevenlabs(self, text: str) -> bytes:
        api_key = self._require_api_key(self.config.voice.tts_api_key_env, engine="ElevenLabs")
        voice_id = urllib.parse.quote(self.config.voice.tts_voice, safe="")
        request = urllib.request.Request(
            f"{ELEVENLABS_URL}/{voice_id}",
            data=json.dumps({"text": text, "model_id": self.config.voice.tts_model}).encode("utf-8"),
            headers={"xi-api-key": api_key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
            method="POST",
        )
        audio = self._http_call(request, engine="ElevenLabs TTS")
        if not audio:
            raise ValueError("ElevenLabs TTS returned no audio.")
        return audio

    def _require_api_key(self, env_name: str, *, engine: str) -> str:
        api_key = os.environ.get(env_name, "").strip()
        if not api_key:
            raise VoiceNotConfigured(f"{engine} API key environment variable is not set: {env_name}")
        return api_key

    def _http_call(self, request: urllib.request.Request, *, engine: str) -> bytes:
        try:
            with urllib.request.urlopen(request, timeout=self.config.voice.timeout_seconds) as response:  # noqa: S310 - fixed https endpoints
                return response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            raise ValueError(f"{engine} request failed (HTTP {exc.code}): {detail}") from exc
        except Exception as exc:
            raise ValueError(f"{engine} request failed: {exc}") from exc

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


_MIME_EXT = {
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/flac": ".flac",
}


def _clean_mime(mime: str) -> str:
    # Browsers send values like "audio/webm;codecs=opus"; Deepgram wants the base type.
    return (mime or "audio/wav").split(";", 1)[0].strip().lower() or "audio/wav"


def _ext_for_mime(mime: str) -> str:
    return _MIME_EXT.get(_clean_mime(mime), ".bin")
