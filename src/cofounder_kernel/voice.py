from __future__ import annotations

import base64
import binascii
import queue
import re
import shutil
import subprocess
import threading
import time
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .config import KernelConfig
from .db import KernelDatabase
from .runtime import RuntimeService


CONTRARIAN_MARKER = "\n---\nContrarian check"
STDERR_EXCERPT_CHARS = 400
SPOKEN_TEXT_MAX_CHARS = 4000

# Voice is local-only. The cloud engines (Deepgram STT / ElevenLabs TTS) were
# torn down in two stages: 2026-07-17 flipped their egress-matrix cells to
# FORBIDDEN; 2026-07-19 removed the adapter code entirely. Re-adding cloud
# voice is a deliberate rebuild, not a config flip.
STT_ENGINES = {"command"}
TTS_ENGINES = {"command"}
_REMOVED_CLOUD_ENGINES = {"deepgram", "elevenlabs"}
# whisper.cpp reads 16 kHz mono PCM WAV and nothing else.
WHISPER_SAMPLE_RATE = 16000


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
            stt |= {"supported": False, "reason": _unsupported_engine_reason("STT", voice.stt_engine)}
        tts: dict[str, Any] = {"engine": voice.tts_engine, "configured": voice.tts_configured}
        if voice.tts_engine == "command":
            tts |= {"command": list(voice.tts_command), "binary_found": _binary_found(voice.tts_command)}
        else:
            tts |= {"supported": False, "reason": _unsupported_engine_reason("TTS", voice.tts_engine)}
        return {
            "stt": stt,
            "tts": tts,
            "ready": voice.stt_configured and voice.tts_configured,
            "cloud_engines_in_use": False,
            "voice_dir": str(self.voice_dir),
            "timeout_seconds": voice.timeout_seconds,
        }

    def transcribe(self, *, audio_base64: str, audio_mime: str = "audio/wav") -> dict[str, Any]:
        engine = self.config.voice.stt_engine
        if engine not in STT_ENGINES:
            raise VoiceNotConfigured(_unsupported_engine_reason("STT", engine))
        if not self.config.voice.stt_configured:
            raise VoiceNotConfigured("STT engine is not configured; set [voice] stt_command in config.toml.")
        audio_bytes = _decode_audio(audio_base64)
        stamp = _stamp()
        audio_path = self._write_artifact(f"{stamp}-in{_ext_for_mime(audio_mime)}", audio_bytes)
        transcript_path = self.voice_dir / f"{stamp}-transcript.txt"
        started = time.perf_counter()
        engine_audio_path, converted = self._normalize_for_local_stt(audio_path)
        self._run_engine(
            self.config.voice.stt_command,
            replacements={
                "{audio}": str(engine_audio_path),
                "{transcript}": str(transcript_path),
                "{transcript_base}": str(transcript_path.with_suffix("")),
            },
            engine="stt",
        )
        text = _read_transcript(transcript_path)
        latency_ms = int((time.perf_counter() - started) * 1000)
        if not text:
            # whisper.cpp exits 0 on audio it cannot decode, so an empty
            # transcript is usually a format/command problem, not silence.
            raise ValueError(
                "Local STT produced no transcript. Check the [voice] stt_command and that "
                f"{engine_audio_path.name} is 16 kHz mono PCM WAV — whisper.cpp exits "
                "successfully but writes nothing for audio it cannot decode."
            )
        self.db.audit(
            actor="voice",
            action="voice.transcribe",
            target=str(audio_path),
            permission_tier="L0_READ",
            status="ok",
            details={
                "engine": engine, "latency_ms": latency_ms, "chars": len(text),
                "audio_bytes": len(audio_bytes), "converted_audio": converted,
            },
        )
        return {
            "text": text,
            "engine": engine,
            "audio_mime": audio_mime,
            "latency_ms": latency_ms,
            "audio_path": str(audio_path),
            "engine_audio_path": str(engine_audio_path),
            "converted_audio": converted,
            "transcript_path": str(transcript_path),
        }

    def speak(self, *, text: str) -> dict[str, Any]:
        engine = self.config.voice.tts_engine
        if engine not in TTS_ENGINES:
            raise VoiceNotConfigured(_unsupported_engine_reason("TTS", engine))
        if not self.config.voice.tts_configured:
            raise VoiceNotConfigured("TTS engine is not configured; set [voice] tts_command in config.toml.")
        spoken = text.strip()[:SPOKEN_TEXT_MAX_CHARS]
        if not spoken:
            raise ValueError("Nothing to speak.")
        stamp = _stamp()
        audio_format = "wav"
        output_path = self.voice_dir / f"{stamp}-out.{audio_format}"
        started = time.perf_counter()
        audio_bytes = self._synthesize_wav(spoken, output_path)
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
        client_timing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        server_started = time.perf_counter()
        transcription = self.transcribe(audio_base64=audio_base64, audio_mime=audio_mime)
        transcript_final_ms = _elapsed_ms(server_started)
        runtime_started = time.perf_counter()
        response = self.runtime.respond(
            message=transcription["text"],
            task_type=task_type,  # type: ignore[arg-type]
            conversation_id=conversation_id,
            contrarian=contrarian,
            use_semantic_memory=use_semantic_memory,
            # Voice is latency-critical: every investigation round is a full
            # model call, so voice answers from pre-injected context only.
            use_tools=False,
        )
        runtime_latency_ms = _elapsed_ms(runtime_started)
        model_response_complete_ms = _elapsed_ms(server_started)
        spoken_text = response["response"] if speak_full else _strip_contrarian_block(response["response"])
        speech: dict[str, Any] | None = None
        speech_error = ""
        speech_synthesis_ms: int | None = None
        audio_ready_ms: int | None = None
        if speak_response:
            speech_started = time.perf_counter()
            try:
                speech = self.speak(text=spoken_text)
                speech_synthesis_ms = int(speech["latency_ms"])
                audio_ready_ms = _elapsed_ms(server_started)
            except VoiceNotConfigured as exc:
                speech_synthesis_ms = _elapsed_ms(speech_started)
                speech_error = str(exc)
            except ValueError as exc:
                speech_synthesis_ms = _elapsed_ms(speech_started)
                speech_error = str(exc)
        server_response_ready_ms = _elapsed_ms(server_started)
        timing = _converse_timing(
            client_timing=client_timing,
            transcription_ms=int(transcription["latency_ms"]),
            transcript_final_ms=transcript_final_ms,
            runtime_response_ms=runtime_latency_ms,
            model_response_complete_ms=model_response_complete_ms,
            speech_synthesis_ms=speech_synthesis_ms,
            audio_ready_ms=audio_ready_ms,
            server_response_ready_ms=server_response_ready_ms,
        )
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
                "timing": timing,
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
            "timing": timing,
        }

    def converse_stream(
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
        client_timing: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """The streaming voice loop: same governed pipeline as converse(), but
        the client hears/sees progress as it happens instead of one batch reply.

        Event order on the wire (NDJSON, one JSON object per line):
          transcript -> token* -> response -> audio* -> done   (or: error)

        Governance note: ``token`` events are the model's DRAFT, streamed for
        display only. What is SPOKEN (the ``audio`` events) is synthesized from
        the final governed response — repair, regulator, citations audit,
        contrarian — exactly the text converse() would return. Streaming
        changes when you hear it, never what governs it. TTS is chunked at
        sentence boundaries so the first audio arrives after the first
        sentence is synthesized, not after the whole reply."""
        server_started = time.perf_counter()
        try:
            transcription = self.transcribe(audio_base64=audio_base64, audio_mime=audio_mime)
        except ValueError as exc:
            yield {"type": "error", "detail": str(exc), "stage": "transcribe"}
            return
        transcript_final_ms = _elapsed_ms(server_started)
        yield {
            "type": "transcript",
            "text": transcription["text"],
            "latency_ms": transcription["latency_ms"],
            "transcript_final_ms": transcript_final_ms,
        }

        # The model call is synchronous and long; run respond() in a worker so
        # draft tokens can be forwarded to the client while it generates.
        events: queue.Queue[Any] = queue.Queue()
        first_token_ms: list[int] = []

        def on_token(delta: str) -> None:
            if not first_token_ms:
                first_token_ms.append(_elapsed_ms(server_started))
            events.put({"type": "token", "text": delta})

        result_box: dict[str, Any] = {}

        def worker() -> None:
            try:
                result_box["response"] = self.runtime.respond(
                    message=transcription["text"],
                    task_type=task_type,  # type: ignore[arg-type]
                    conversation_id=conversation_id,
                    contrarian=contrarian,
                    use_semantic_memory=use_semantic_memory,
                    # Voice stays tool-loop-free (latency) — and the tool loop
                    # could not stream one coherent draft anyway.
                    use_tools=False,
                    on_token=on_token,
                )
            except Exception as exc:  # surfaced as an error event by the drain loop
                result_box["error"] = exc
            finally:
                events.put(None)  # sentinel: generation finished either way

        thread = threading.Thread(target=worker, name="voice-converse-stream", daemon=True)
        thread.start()
        while True:
            item = events.get()
            if item is None:
                break
            yield item
        thread.join()
        if "error" in result_box:
            yield {"type": "error", "detail": str(result_box["error"]), "stage": "respond"}
            return
        response = result_box["response"]
        runtime_response_ms = _elapsed_ms(server_started) - transcript_final_ms
        model_response_complete_ms = _elapsed_ms(server_started)
        spoken_text = response["response"] if speak_full else _strip_contrarian_block(response["response"])
        yield {
            "type": "response",
            "transcript": transcription["text"],
            "response": response["response"],
            "spoken_text": spoken_text,
            "event_id": response["event_id"],
            "model": response["model"],
            "authority": response["authority"],
            "governor": response["governor"],
            "conversation": response.get("conversation"),
            "contrarian": response.get("contrarian"),
        }

        speech_error = ""
        chunk_count = 0
        first_audio_byte_ms: int | None = None
        audio_ready_ms: int | None = None
        if speak_response:
            stamp = _stamp()
            for seq, chunk_text in enumerate(_speech_chunks(spoken_text)):
                try:
                    chunk_path = self.voice_dir / f"{stamp}-out-{seq}.wav"
                    audio_bytes = self._synthesize_wav(chunk_text, chunk_path)
                except (VoiceNotConfigured, ValueError) as exc:
                    speech_error = str(exc)
                    break
                if first_audio_byte_ms is None:
                    first_audio_byte_ms = _elapsed_ms(server_started)
                chunk_count += 1
                yield {
                    "type": "audio",
                    "seq": seq,
                    "format": "wav",
                    "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
                    "text": chunk_text,
                }
            if chunk_count:
                audio_ready_ms = _elapsed_ms(server_started)

        server_response_ready_ms = _elapsed_ms(server_started)
        timing = {
            "pipeline": "streaming",
            "client_reported": dict(client_timing or {}),
            "streaming": {"stt": False, "model": True, "tts": True, "playback": True},
            "segments_ms": {
                "transcription": int(transcription["latency_ms"]),
                "runtime_response": runtime_response_ms,
                "speech_synthesis": (
                    (audio_ready_ms - model_response_complete_ms)
                    if audio_ready_ms is not None
                    else None
                ),
                "server_total": server_response_ready_ms,
            },
            "milestones_ms": {
                "server_received": 0,
                "transcript_final": transcript_final_ms,
                "model_first_token": first_token_ms[0] if first_token_ms else None,
                "model_response_complete": model_response_complete_ms,
                "first_audio_byte": first_audio_byte_ms,
                "audio_ready": audio_ready_ms,
                "server_response_ready": server_response_ready_ms,
                "playback_started": None,  # filled client-side when audio starts
            },
            "unavailable": {
                "playback_started": "Playback starts in the browser and is filled by the UI when audio begins.",
            },
        }
        self.db.audit(
            actor="voice",
            action="voice.converse_stream",
            target=f"conversation:{conversation_id}" if conversation_id else "local_runtime",
            permission_tier="L0_READ",
            status="ok" if not speech_error else "degraded",
            details={
                "event_id": response["event_id"],
                "transcript_chars": len(transcription["text"]),
                "audio_chunks": chunk_count,
                "speech_error": speech_error,
                "timing": timing,
            },
        )
        yield {"type": "done", "timing": timing, "speech_error": speech_error, "audio_chunks": chunk_count}

    def _synthesize_wav(self, text: str, output_path: Path) -> bytes:
        """Run the local TTS engine for one piece of text and return the WAV
        bytes. The shared core of speak() and the streaming chunk synthesizer."""
        self.voice_dir.mkdir(parents=True, exist_ok=True)
        self._run_engine(
            self.config.voice.tts_command,
            replacements={"{output}": str(output_path)},
            engine="tts",
            stdin_text=text,
        )
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise ValueError("TTS engine produced no audio output.")
        return output_path.read_bytes()

    def _write_artifact(self, name: str, data: bytes) -> Path:
        self.voice_dir.mkdir(parents=True, exist_ok=True)
        path = self.voice_dir / name
        path.write_bytes(data)
        return path

    def _normalize_for_local_stt(self, audio_path: Path) -> tuple[Path, bool]:
        """Coerce captured audio into the 16 kHz mono PCM WAV whisper.cpp requires.

        The browser records webm/opus (MediaRecorder's default), which whisper.cpp
        cannot decode — and it does not say so: it exits 0 and writes no transcript,
        which would surface as a misleading "produced no text". Cloud STT hid this
        because cloud STT decoded server-side. Audio already in the right shape is
        passed through untouched, so a well-formed client needs no ffmpeg at all.

        Returns the path to hand the engine and whether a conversion happened."""
        if _is_whisper_ready_wav(audio_path):
            return audio_path, False
        ffmpeg = _resolve_ffmpeg(self.config.voice.ffmpeg_path)
        if not ffmpeg:
            raise ValueError(
                f"Local STT needs 16 kHz mono PCM WAV, but got {audio_path.suffix or 'unknown'} "
                "audio and no ffmpeg was found to convert it. Install ffmpeg, set "
                "[voice] ffmpeg_path, or have the client send WAV."
            )
        converted = audio_path.with_name(f"{audio_path.stem}-16k.wav")
        argv = [
            ffmpeg, "-y", "-loglevel", "error", "-i", str(audio_path),
            "-ar", str(WHISPER_SAMPLE_RATE), "-ac", "1", "-c:a", "pcm_s16le", str(converted),
        ]
        try:
            completed = subprocess.run(  # noqa: S603 - resolved ffmpeg path, no shell
                argv, capture_output=True, timeout=self.config.voice.timeout_seconds,
                shell=False, check=False,
            )
        except FileNotFoundError as exc:
            raise ValueError(f"ffmpeg not found for local STT conversion: {ffmpeg}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ValueError("ffmpeg timed out converting audio for local STT.") from exc
        if completed.returncode != 0 or not converted.is_file():
            stderr = completed.stderr.decode("utf-8", errors="replace")[:STDERR_EXCERPT_CHARS]
            raise ValueError(
                f"ffmpeg could not convert the audio for local STT (exit {completed.returncode}): {stderr}"
            )
        return converted, True

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


def _unsupported_engine_reason(label: str, engine: str) -> str:
    if engine in _REMOVED_CLOUD_ENGINES:
        return (
            f"Cloud {label} engine '{engine}' was removed; voice is local-only "
            "(whisper.cpp / piper via the 'command' engine). Set [voice] "
            f"{label.lower()}_engine = \"command\" in config.toml."
        )
    return f"Unknown {label} engine: {engine}. Supported: {', '.join(sorted(STT_ENGINES))}."


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


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")
# The first chunk is deliberately small — usually the first sentence alone — so
# the first audio byte lands as fast as the engine can say one sentence; later
# chunks pack more sentences for better prosody and fewer engine spawns.
FIRST_CHUNK_TARGET_CHARS = 60
CHUNK_TARGET_CHARS = 320


def _speech_chunks(text: str) -> list[str]:
    """Split spoken text into sentence-boundary chunks for streaming TTS.

    Greedy packing: sentences are appended to the current chunk until it
    reaches the target size (a smaller target for the first chunk, so the
    first audio byte arrives after roughly one sentence of synthesis). Never
    splits inside a sentence; total output respects SPOKEN_TEXT_MAX_CHARS."""
    spoken = text.strip()[:SPOKEN_TEXT_MAX_CHARS]
    if not spoken:
        return []
    sentences = [part.strip() for part in _SENTENCE_SPLIT_RE.split(spoken) if part.strip()]
    if not sentences:
        return [spoken]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        target = FIRST_CHUNK_TARGET_CHARS if not chunks else CHUNK_TARGET_CHARS
        if current and len(current) + 1 + len(sentence) > target:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    return chunks


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


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _converse_timing(
    *,
    client_timing: dict[str, Any] | None,
    transcription_ms: int,
    transcript_final_ms: int,
    runtime_response_ms: int,
    model_response_complete_ms: int,
    speech_synthesis_ms: int | None,
    audio_ready_ms: int | None,
    server_response_ready_ms: int,
) -> dict[str, Any]:
    return {
        "pipeline": "batch_non_streaming",
        "client_reported": dict(client_timing or {}),
        "streaming": {"stt": False, "model": False, "tts": False, "playback": False},
        "segments_ms": {
            "transcription": transcription_ms,
            "runtime_response": runtime_response_ms,
            "speech_synthesis": speech_synthesis_ms,
            "server_total": server_response_ready_ms,
        },
        "milestones_ms": {
            "server_received": 0,
            "transcript_final": transcript_final_ms,
            "model_first_token": None,
            "model_response_complete": model_response_complete_ms,
            "first_audio_byte": None,
            "audio_ready": audio_ready_ms,
            "server_response_ready": server_response_ready_ms,
            "playback_started": None,
        },
        "unavailable": {
            "model_first_token": "Ollama is called with stream=false, so the first token is not observable yet.",
            "first_audio_byte": "TTS audio is read as a complete response before the server returns it.",
            "playback_started": "Playback starts in the browser and is filled by the UI when audio begins.",
        },
    }


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
    # Browsers send values like "audio/webm;codecs=opus"; keep the base type.
    return (mime or "audio/wav").split(";", 1)[0].strip().lower() or "audio/wav"


def _ext_for_mime(mime: str) -> str:
    return _MIME_EXT.get(_clean_mime(mime), ".bin")


def _is_whisper_ready_wav(path: Path) -> bool:
    """True only for 16 kHz mono 16-bit PCM WAV — the one shape whisper.cpp reads.

    Anything else (a browser's webm/opus, a 48 kHz stereo WAV) must be converted
    first. This matters because whisper.cpp does NOT fail loudly on audio it can't
    decode: it exits 0 and writes no transcript at all."""
    try:
        with wave.open(str(path), "rb") as handle:
            return (
                handle.getnchannels() == 1
                and handle.getsampwidth() == 2
                and handle.getframerate() == WHISPER_SAMPLE_RATE
                and handle.getcomptype() == "NONE"
            )
    except (wave.Error, EOFError, OSError):
        return False


def _resolve_ffmpeg(configured: str = "") -> str | None:
    """The configured ffmpeg, else one on PATH. None when unavailable."""
    candidate = (configured or "").strip()
    if candidate:
        return candidate if Path(candidate).is_file() else None
    return shutil.which("ffmpeg")
