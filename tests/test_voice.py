import base64
import io
import json
import shutil
import sys
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import cofounder_kernel.voice as voice_module
from cofounder_kernel.api import create_app
from cofounder_kernel.config import (
    AppConfig,
    EgressConfig,
    KernelConfig,
    OllamaConfig,
    PathConfig,
    VoiceConfig,
)
from cofounder_kernel.ollama import GenerateResult, OllamaClient


def _wav_bytes(*, sample_rate: int = 16000, channels: int = 1, frames: int = 1600) -> bytes:
    """A real (silent) PCM WAV. The local STT path inspects the audio header now —
    whisper.cpp only reads 16 kHz mono PCM WAV — so test audio must actually parse."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * frames * channels)
    return buffer.getvalue()


FAKE_AUDIO = base64.b64encode(_wav_bytes()).decode("ascii")

# Real subprocess engines: tiny Python scripts standing in for whisper.cpp and piper.
STT_SCRIPT = (
    "import sys, pathlib; "
    "pathlib.Path(sys.argv[2]).write_text('what should we prioritize next', encoding='utf-8')"
)
TTS_SCRIPT = (
    "import sys, pathlib; "
    "text = sys.stdin.read(); "
    "pathlib.Path(sys.argv[1]).write_bytes(b'FAKEWAV:' + text.encode('utf-8'))"
)
FAILING_SCRIPT = "import sys; sys.stderr.write('engine exploded'); sys.exit(3)"

CRITIC_JSON = (
    '{"verdict": "proceed_with_changes", "weakest_assumption": "Focus holds", '
    '"missing_evidence": "Retention data", "downside_risk": "Polish slips", "confidence_adjustment": -10}'
)


def fake_health(self: OllamaClient) -> dict:
    return {"version": "test"}


def fake_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
    if model == "deepseek-r1:14b":
        return GenerateResult(response=CRITIC_JSON, model=model, raw={})
    return GenerateResult(response="Prioritize evidence intake this week.", model=model or "qwen3:14b", raw={})


def _messages_to_prompt(messages: object) -> str:
    return "\n\n".join(str(getattr(message, "content", "")) for message in messages)


def _chat_from_generate(generate_func):
    def fake_chat(self, *, messages, model=None, think=None, temperature=None, num_predict=512, tools=None):
        return generate_func(
            self,
            prompt=_messages_to_prompt(messages),
            model=model,
            think=think,
            temperature=temperature,
            num_predict=num_predict,
        )

    return fake_chat


def patch_ollama_model(monkeypatch, generate_func) -> None:
    monkeypatch.setattr(OllamaClient, "generate", generate_func)
    monkeypatch.setattr(OllamaClient, "chat", _chat_from_generate(generate_func))


def _voice_config() -> VoiceConfig:
    return VoiceConfig(
        stt_command=(sys.executable, "-c", STT_SCRIPT, "{audio}", "{transcript}"),
        tts_command=(sys.executable, "-c", TTS_SCRIPT, "{output}"),
        timeout_seconds=60.0,
    )


def _config(tmp_path: Path, voice: VoiceConfig | None = None) -> KernelConfig:
    return KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
        voice=voice or VoiceConfig(),
    )


def _cloud_ready_config(tmp_path: Path, voice: VoiceConfig) -> KernelConfig:
    """Cloud voice deliberately re-enabled: provider_policy raised off local_only
    and the two voice standing grants enabled. This is the explicit opt-in the
    egress gate requires — the default posture ([ollama] local_only, no grants)
    refuses cloud voice (see test_cloud_voice_refused_by_default_egress_policy)."""
    return KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1", provider_policy="local_preferred"),
        voice=voice,
        egress=EgressConfig(standing_grants=("founder_audio:deepgram", "reply_text:elevenlabs")),
    )


def test_unconfigured_voice_reports_unavailable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path)))

    status = client.get("/voice/status")
    transcribe = client.post("/voice/transcribe", json={"audio_base64": FAKE_AUDIO})
    speak = client.post("/voice/speak", json={"text": "hello"})
    inventory = client.get("/self-inventory")

    assert status.status_code == 200
    assert status.json()["ready"] is False
    assert status.json()["stt"]["configured"] is False
    assert transcribe.status_code == 503
    assert "stt_command" in transcribe.json()["detail"]
    assert speak.status_code == 503
    assert "POST /voice/converse" in inventory.json()["voice_layer"]["routes"]
    assert inventory.json()["voice_layer"]["engines"]["stt_configured"] is False


def test_transcribe_and_speak_run_real_engine_subprocesses(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    client = TestClient(create_app(_config(tmp_path, _voice_config())))

    status = client.get("/voice/status")
    transcribed = client.post("/voice/transcribe", json={"audio_base64": FAKE_AUDIO})
    spoken = client.post("/voice/speak", json={"text": "Evidence first."})
    bad_audio = client.post("/voice/transcribe", json={"audio_base64": "not-base64!!"})
    audit = client.get("/audit/recent")

    assert status.json()["ready"] is True
    assert status.json()["stt"]["binary_found"] is True
    assert transcribed.status_code == 200
    assert transcribed.json()["text"] == "what should we prioritize next"
    assert Path(transcribed.json()["audio_path"]).is_file()
    assert Path(transcribed.json()["transcript_path"]).is_file()
    assert spoken.status_code == 200
    audio = base64.b64decode(spoken.json()["audio_base64"])
    assert audio == b"FAKEWAV:Evidence first."
    assert Path(spoken.json()["audio_path"]).is_file()
    assert bad_audio.status_code == 400
    actions = {event["action"] for event in audit.json()["events"]}
    assert {"voice.transcribe", "voice.speak"} <= actions


def test_converse_runs_governed_loop_with_memory_and_contrarian(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)
    client = TestClient(create_app(_config(tmp_path, _voice_config())))

    conversation = client.post("/conversations", json={"title": "Voice thread"})
    conversation_id = conversation.json()["conversation"]["id"]
    converse = client.post(
        "/voice/converse",
        json={
            "audio_base64": FAKE_AUDIO,
            "conversation_id": conversation_id,
            "use_semantic_memory": False,
        },
    )
    turns = client.get(f"/conversations/{conversation_id}/turns")

    assert converse.status_code == 200
    payload = converse.json()
    assert payload["transcript"] == "what should we prioritize next"
    # "prioritize" triggers the contrarian pass; the text response carries it...
    assert "Contrarian check" in payload["response"]
    assert payload["contrarian"]["verdict"] == "proceed_with_changes"
    # ...but the spoken audio does not read the red-team block aloud.
    assert "Contrarian check" not in payload["spoken_text"]
    spoken_audio = base64.b64decode(payload["speech"]["audio_base64"]).decode("utf-8")
    assert spoken_audio == "FAKEWAV:Prioritize evidence intake this week."
    assert payload["speech_error"] == ""
    assert "episodic_conversation_memory" in payload["governor"]["applied_rules"]
    # The exchange landed in episodic memory: user turn is the transcript.
    turn_list = turns.json()["turns"]
    assert [turn["role"] for turn in turn_list] == ["user", "assistant"]
    assert turn_list[0]["content"] == "what should we prioritize next"


def test_converse_returns_tier1_latency_timing_breakdown(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)
    client = TestClient(create_app(_config(tmp_path, _voice_config())))

    converse = client.post(
        "/voice/converse",
        json={
            "audio_base64": FAKE_AUDIO,
            "use_semantic_memory": False,
            "client_timing": {"speech_stopped_at_ms": 10.0, "request_started_at_ms": 25.0},
        },
    )

    assert converse.status_code == 200
    payload = converse.json()
    timing = payload["timing"]

    assert timing["pipeline"] == "batch_non_streaming"
    assert timing["streaming"] == {"stt": False, "model": False, "tts": False, "playback": False}
    assert timing["client_reported"] == {"speech_stopped_at_ms": 10.0, "request_started_at_ms": 25.0}
    assert timing["segments_ms"]["transcription"] == payload["transcription"]["latency_ms"]
    assert timing["segments_ms"]["runtime_response"] >= 0
    assert timing["segments_ms"]["speech_synthesis"] == payload["speech"]["latency_ms"]
    assert timing["segments_ms"]["server_total"] >= timing["segments_ms"]["transcription"]
    assert timing["milestones_ms"]["server_received"] == 0
    assert timing["milestones_ms"]["transcript_final"] >= timing["segments_ms"]["transcription"]
    assert timing["milestones_ms"]["model_first_token"] is None
    assert timing["milestones_ms"]["model_response_complete"] >= timing["milestones_ms"]["transcript_final"]
    assert timing["milestones_ms"]["first_audio_byte"] is None
    assert timing["milestones_ms"]["audio_ready"] >= timing["milestones_ms"]["model_response_complete"]
    assert timing["milestones_ms"]["server_response_ready"] >= timing["milestones_ms"]["audio_ready"]
    assert timing["milestones_ms"]["playback_started"] is None
    assert "model_first_token" in timing["unavailable"]
    assert "first_audio_byte" in timing["unavailable"]
    assert "playback_started" in timing["unavailable"]


def test_voice_surfaces_send_and_render_latency_timing() -> None:
    standalone = Path("ui/voice.html").read_text(encoding="utf-8")
    dashboard = Path("ui/index.html").read_text(encoding="utf-8")

    for html in (standalone, dashboard):
        assert "client_timing" in html
        assert "speech_stopped_at_ms" in html
        assert "request_started_at_ms" in html
        assert "First sound" in html

    assert "renderTiming" in standalone
    assert "timing.segments_ms" in dashboard


def test_converse_prompt_carries_personality_contract(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    prompts: list[str] = []

    def capturing_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512, format=None):
        prompts.append(prompt)
        if model == "deepseek-r1:14b":
            return GenerateResult(response=CRITIC_JSON, model=model, raw={})
        return GenerateResult(response="Review the gate. Then move.", model=model or "qwen3:14b", raw={})

    patch_ollama_model(monkeypatch, capturing_generate)
    client = TestClient(create_app(_config(tmp_path, _voice_config())))
    client.post("/identity/charter", json={
        "name": "Zade",
        "source": "test",
        "mission": "Relentless purpose. No drifting.",
    })
    client.post("/identity/voice", json={
        "name": "Zade",
        "source": "test",
        "overall_voice": "He does not negotiate. He states.",
    })

    converse = client.post(
        "/voice/converse",
        json={
            "audio_base64": FAKE_AUDIO,
            "use_semantic_memory": False,
            "contrarian": False,
        },
    )

    assert converse.status_code == 200
    assert prompts
    assert "====================  WHO YOU ARE  ====================" in prompts[0]
    assert "The identity charter defines who you are, not a style overlay." in prompts[0]
    assert "Relentless purpose. No drifting." in prompts[0]
    assert "He does not negotiate. He states." in prompts[0]
    assert converse.json()["spoken_text"] == "Review the gate. Then move."


def test_converse_speak_full_includes_contrarian_block(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)
    client = TestClient(create_app(_config(tmp_path, _voice_config())))

    converse = client.post(
        "/voice/converse",
        json={"audio_base64": FAKE_AUDIO, "speak_full": True, "use_semantic_memory": False},
    )

    assert converse.status_code == 200
    assert "Contrarian check" in converse.json()["spoken_text"]
    spoken_audio = base64.b64decode(converse.json()["speech"]["audio_base64"]).decode("utf-8")
    assert "Contrarian check" in spoken_audio


def _cloud_voice_config() -> VoiceConfig:
    return VoiceConfig(stt_engine="deepgram", tts_engine="elevenlabs", tts_voice="voice123")


class FakeHttpResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_cloud_engines_report_status_and_missing_keys(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    client = TestClient(create_app(_cloud_ready_config(tmp_path, _cloud_voice_config())))

    status = client.get("/voice/status")
    transcribe = client.post("/voice/transcribe", json={"audio_base64": FAKE_AUDIO})
    speak = client.post("/voice/speak", json={"text": "hello"})

    payload = status.json()
    assert payload["ready"] is True
    assert payload["cloud_engines_in_use"] is True
    assert payload["stt"]["engine"] == "deepgram"
    assert payload["stt"]["credential_env"] == "DEEPGRAM_API_KEY"
    assert payload["stt"]["credential_set"] is False
    assert payload["tts"]["engine"] == "elevenlabs"
    assert payload["tts"]["credential_set"] is False
    # Missing keys fail loudly with the exact env var to set.
    assert transcribe.status_code == 503
    assert "DEEPGRAM_API_KEY" in transcribe.json()["detail"]
    assert speak.status_code == 503
    assert "ELEVENLABS_API_KEY" in speak.json()["detail"]


def test_deepgram_and_elevenlabs_adapters(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-key")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-key")
    calls = {}

    def fake_urlopen(request, timeout=120):
        calls.setdefault("requests", []).append(request)
        if "api.deepgram.com" in request.full_url:
            body = {"results": {"channels": [{"alternatives": [{"transcript": "what should we prioritize next"}]}]}}
            return FakeHttpResponse(json.dumps(body).encode("utf-8"))
        if "api.elevenlabs.io" in request.full_url:
            return FakeHttpResponse(b"MP3FAKE-AUDIO-BYTES")
        raise AssertionError(f"Unexpected URL: {request.full_url}")

    monkeypatch.setattr(voice_module.urllib.request, "urlopen", fake_urlopen)
    client = TestClient(create_app(_cloud_ready_config(tmp_path, _cloud_voice_config())))

    transcribed = client.post("/voice/transcribe", json={"audio_base64": FAKE_AUDIO})
    spoken = client.post("/voice/speak", json={"text": "Evidence first."})

    assert transcribed.status_code == 200
    assert transcribed.json()["text"] == "what should we prioritize next"
    assert transcribed.json()["engine"] == "deepgram"
    assert Path(transcribed.json()["transcript_path"]).read_text(encoding="utf-8") == "what should we prioritize next"
    assert spoken.status_code == 200
    assert spoken.json()["engine"] == "elevenlabs"
    assert spoken.json()["format"] == "mp3"
    assert base64.b64decode(spoken.json()["audio_base64"]) == b"MP3FAKE-AUDIO-BYTES"
    assert Path(spoken.json()["audio_path"]).suffix == ".mp3"

    deepgram_request = calls["requests"][0]
    assert "model=nova-2" in deepgram_request.full_url
    assert deepgram_request.get_header("Authorization") == "Token dg-key"
    assert deepgram_request.get_header("Content-type") == "audio/wav"  # default mime
    assert deepgram_request.data == base64.b64decode(FAKE_AUDIO)
    elevenlabs_request = calls["requests"][1]
    assert "/v1/text-to-speech/voice123" in elevenlabs_request.full_url
    assert elevenlabs_request.get_header("Xi-api-key") == "el-key"
    assert json.loads(elevenlabs_request.data.decode("utf-8"))["text"] == "Evidence first."


def test_browser_webm_mime_reaches_deepgram(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-key")
    seen = {}

    def fake_urlopen(request, timeout=120):
        seen["content_type"] = request.get_header("Content-type")
        body = {"results": {"channels": [{"alternatives": [{"transcript": "hello from the browser"}]}]}}
        return FakeHttpResponse(json.dumps(body).encode("utf-8"))

    monkeypatch.setattr(voice_module.urllib.request, "urlopen", fake_urlopen)
    cfg = _cloud_ready_config(tmp_path, VoiceConfig(stt_engine="deepgram", tts_engine="elevenlabs"))
    client = TestClient(create_app(cfg))

    # The browser's MediaRecorder produces webm/opus; the codecs suffix is stripped.
    result = client.post(
        "/voice/transcribe",
        json={"audio_base64": FAKE_AUDIO, "audio_mime": "audio/webm;codecs=opus"},
    )

    assert result.status_code == 200
    assert result.json()["text"] == "hello from the browser"
    assert result.json()["audio_mime"] == "audio/webm;codecs=opus"
    assert seen["content_type"] == "audio/webm"
    # The saved input artifact carries the right extension for the format.
    assert Path(result.json()["audio_path"]).suffix == ".webm"


def test_cloud_converse_end_to_end_and_http_errors(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-key")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-key")

    def fake_urlopen(request, timeout=120):
        if "api.deepgram.com" in request.full_url:
            body = {"results": {"channels": [{"alternatives": [{"transcript": "summarize the current memory state"}]}]}}
            return FakeHttpResponse(json.dumps(body).encode("utf-8"))
        return FakeHttpResponse(b"MP3FAKE")

    monkeypatch.setattr(voice_module.urllib.request, "urlopen", fake_urlopen)
    client = TestClient(create_app(_cloud_ready_config(tmp_path, _cloud_voice_config())))

    converse = client.post("/voice/converse", json={"audio_base64": FAKE_AUDIO, "use_semantic_memory": False})

    assert converse.status_code == 200
    assert converse.json()["transcript"] == "summarize the current memory state"
    assert converse.json()["speech"]["format"] == "mp3"
    assert converse.json()["response"]

    # Cloud HTTP failures come back as loud 400s with the status code.
    import urllib.error

    def failing_urlopen(request, timeout=120):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(b"invalid api key"))

    monkeypatch.setattr(voice_module.urllib.request, "urlopen", failing_urlopen)
    failed = client.post("/voice/transcribe", json={"audio_base64": FAKE_AUDIO})
    assert failed.status_code == 400
    assert "HTTP 401" in failed.json()["detail"]
    assert "invalid api key" in failed.json()["detail"]


def test_engine_failure_and_tts_degradation_are_handled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)
    broken_stt = VoiceConfig(
        stt_command=(sys.executable, "-c", FAILING_SCRIPT, "{audio}", "{transcript}"),
        tts_command=(sys.executable, "-c", TTS_SCRIPT, "{output}"),
    )
    client = TestClient(create_app(_config(tmp_path, broken_stt)))

    failed = client.post("/voice/transcribe", json={"audio_base64": FAKE_AUDIO})
    assert failed.status_code == 400
    assert "exit 3" in failed.json()["detail"]
    assert "engine exploded" in failed.json()["detail"]

    # STT works but TTS fails: converse still answers in text with a speech_error note.
    stt_only = VoiceConfig(
        stt_command=(sys.executable, "-c", STT_SCRIPT, "{audio}", "{transcript}"),
        tts_command=(sys.executable, "-c", FAILING_SCRIPT, "{output}"),
    )
    client2 = TestClient(create_app(_config(tmp_path / "second", stt_only)))
    converse = client2.post(
        "/voice/converse",
        json={"audio_base64": FAKE_AUDIO, "use_semantic_memory": False},
    )

    assert converse.status_code == 200
    assert converse.json()["response"]
    assert converse.json()["speech"] is None
    assert "exit 3" in converse.json()["speech_error"]


# --------------------------------------------------------------------------
# Local STT audio normalization: whisper.cpp only reads 16 kHz mono PCM WAV,
# and browsers record webm/opus. whisper exits 0 and writes NOTHING for audio
# it cannot decode, so this must be caught before the engine runs.
# --------------------------------------------------------------------------
def _service(tmp_path: Path, voice: VoiceConfig | None = None):
    """VoiceService with a stub runtime — the audio-normalization path never
    touches the runtime, so there is no need to build the whole stack."""
    from cofounder_kernel.config import ensure_local_paths
    from cofounder_kernel.db import KernelDatabase

    config = _config(tmp_path, voice or _voice_config())
    ensure_local_paths(config)
    db = KernelDatabase(config.paths.database_path)
    db.migrate()
    return voice_module.VoiceService(config=config, db=db, runtime=None)  # type: ignore[arg-type]


def test_whisper_ready_wav_detection(tmp_path: Path) -> None:
    """Only 16 kHz mono 16-bit PCM passes; the browser's webm and a 48 kHz stereo
    WAV both fail — which is exactly what whisper.cpp silently chokes on."""
    good = tmp_path / "good.wav"
    good.write_bytes(_wav_bytes())
    stereo = tmp_path / "stereo48k.wav"
    stereo.write_bytes(_wav_bytes(sample_rate=48000, channels=2))
    webm = tmp_path / "mic.webm"
    webm.write_bytes(b"\x1aE\xdf\xa3-not-really-webm")

    assert voice_module._is_whisper_ready_wav(good) is True
    assert voice_module._is_whisper_ready_wav(stereo) is False
    assert voice_module._is_whisper_ready_wav(webm) is False


def test_correct_wav_passes_through_without_ffmpeg(tmp_path: Path, monkeypatch) -> None:
    """A well-formed 16 kHz mono WAV needs no conversion — and therefore no ffmpeg
    dependency at all. Proven by making ffmpeg unavailable and expecting no error."""
    monkeypatch.setattr(voice_module, "_resolve_ffmpeg", lambda configured="": None)
    service = _service(tmp_path)
    audio = tmp_path / "good.wav"
    audio.write_bytes(_wav_bytes())

    path, converted = service._normalize_for_local_stt(audio)

    assert path == audio
    assert converted is False


def test_non_wav_audio_without_ffmpeg_raises_a_clear_error(tmp_path: Path, monkeypatch) -> None:
    """The webm/opus case with no ffmpeg: fail loudly naming the real cause rather
    than letting whisper silently produce nothing."""
    monkeypatch.setattr(voice_module, "_resolve_ffmpeg", lambda configured="": None)
    service = _service(tmp_path)
    audio = tmp_path / "mic.webm"
    audio.write_bytes(b"\x1aE\xdf\xa3-not-really-webm")

    with pytest.raises(ValueError) as excinfo:
        service._normalize_for_local_stt(audio)

    message = str(excinfo.value)
    assert "16 kHz mono PCM WAV" in message
    assert "ffmpeg" in message


def test_wrong_shape_wav_is_also_converted(tmp_path: Path, monkeypatch) -> None:
    """48 kHz stereo WAV is still unreadable by whisper — header shape matters,
    not just the container."""
    monkeypatch.setattr(voice_module, "_resolve_ffmpeg", lambda configured="": None)
    service = _service(tmp_path)
    audio = tmp_path / "stereo48k.wav"
    audio.write_bytes(_wav_bytes(sample_rate=48000, channels=2))

    with pytest.raises(ValueError, match="16 kHz mono PCM WAV"):
        service._normalize_for_local_stt(audio)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_ffmpeg_converts_wrong_shape_wav(tmp_path: Path) -> None:
    """End-to-end with the real binary: the ffmpeg argv actually works and yields
    audio whisper would accept."""
    service = _service(tmp_path)
    audio = tmp_path / "stereo48k.wav"
    audio.write_bytes(_wav_bytes(sample_rate=48000, channels=2))

    path, converted = service._normalize_for_local_stt(audio)

    assert converted is True
    assert path != audio
    assert voice_module._is_whisper_ready_wav(path)


def test_empty_local_transcript_blames_the_format_not_silence(tmp_path: Path, monkeypatch) -> None:
    """whisper exiting 0 with no transcript must not surface as a bare
    'produced no text' — that sent us looking for the wrong bug."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    silent_stt = VoiceConfig(
        stt_command=(sys.executable, "-c", "pass", "{audio}", "{transcript}"),
        tts_command=(sys.executable, "-c", TTS_SCRIPT, "{output}"),
    )
    client = TestClient(create_app(_config(tmp_path, silent_stt)))

    failed = client.post("/voice/transcribe", json={"audio_base64": FAKE_AUDIO})

    assert failed.status_code == 400
    detail = failed.json()["detail"]
    assert "16 kHz mono PCM WAV" in detail
    assert "stt_command" in detail


def test_cloud_voice_refused_by_default_egress_policy(tmp_path: Path, monkeypatch) -> None:
    """Default posture ([ollama] local_only, no [egress] standing grants): cloud
    STT/TTS is refused by the egress gate BEFORE any bytes leave — even with the
    API keys set — and the refusal names the local engine as the way forward.
    The refusal is audited (redacted). No network call is made."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-key")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-key")

    def exploding_urlopen(request, timeout=120):
        raise AssertionError("egress gate must refuse before any network call")

    monkeypatch.setattr(voice_module.urllib.request, "urlopen", exploding_urlopen)
    # Cloud engines selected, but provider_policy stays local_only and no grants.
    client = TestClient(create_app(_config(tmp_path, _cloud_voice_config())))

    transcribe = client.post("/voice/transcribe", json={"audio_base64": FAKE_AUDIO})
    speak = client.post("/voice/speak", json={"text": "hello"})
    audit = client.get("/audit/recent")

    assert transcribe.status_code == 503
    assert "egress policy" in transcribe.json()["detail"]
    assert "command" in transcribe.json()["detail"]
    assert speak.status_code == 503
    assert "egress policy" in speak.json()["detail"]
    # The refusal is recorded, and the audit row carries no audio/text payload.
    events = audit.json()["events"]
    egress_events = [e for e in events if e["action"] == "egress.decision"]
    assert egress_events and all(e["status"] == "refused" for e in egress_events)
