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


def _chat_stream_from_generate(generate_func):
    """Fake of OllamaClient.chat_stream: same result as chat(), but the reply
    text arrives through on_token in two deltas first — enough to prove the
    token events flow end-to-end."""

    def fake_chat_stream(self, *, messages, on_token, model=None, think=None, temperature=None, num_predict=512):
        result = generate_func(
            self,
            prompt=_messages_to_prompt(messages),
            model=model,
            think=think,
            temperature=temperature,
            num_predict=num_predict,
        )
        text = result.response
        middle = max(1, len(text) // 2)
        for delta in (text[:middle], text[middle:]):
            if delta:
                on_token(delta)
        return result

    return fake_chat_stream


def patch_ollama_model(monkeypatch, generate_func) -> None:
    monkeypatch.setattr(OllamaClient, "generate", generate_func)
    monkeypatch.setattr(OllamaClient, "chat", _chat_from_generate(generate_func))
    monkeypatch.setattr(OllamaClient, "chat_stream", _chat_stream_from_generate(generate_func))


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
    """The strongest posture a config could ever assert toward cloud voice:
    provider_policy raised off local_only and the two dead voice standing grants
    re-added. After the 2026-07-19 adapter removal, even this must change
    nothing — there is no cloud code left for the grants to reach."""
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
    # "prioritize" auto-triggers the contrarian pass; the review persists to the
    # founder layer but the reply stays in Zade's voice — no visible memo block.
    assert "Contrarian check" not in payload["response"]
    assert payload["contrarian"]["verdict"] == "proceed_with_changes"
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


def _stream_events(client, payload: dict) -> list[dict]:
    response = client.post("/voice/converse/stream", json=payload)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def test_converse_stream_emits_governed_event_sequence(tmp_path: Path, monkeypatch) -> None:
    """The streaming loop: transcript -> token* -> response -> audio* -> done.
    Tokens carry the model draft; the audio is synthesized from the GOVERNED
    final text (identical to what the response event carries); timing exposes
    the real first-token and first-audio milestones batch mode never had."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)
    client = TestClient(create_app(_config(tmp_path, _voice_config())))

    events = _stream_events(client, {"audio_base64": FAKE_AUDIO, "use_semantic_memory": False, "contrarian": False})

    kinds = [e["type"] for e in events]
    assert kinds[0] == "transcript"
    assert kinds[-1] == "done"
    assert "response" in kinds and "token" in kinds and "audio" in kinds
    # strict order: all tokens before the response event, all audio after it
    assert max(i for i, k in enumerate(kinds) if k == "token") < kinds.index("response")
    assert min(i for i, k in enumerate(kinds) if k == "audio") > kinds.index("response")

    transcript = events[0]
    assert transcript["text"] == "what should we prioritize next"
    draft = "".join(e["text"] for e in events if e["type"] == "token")
    response_event = next(e for e in events if e["type"] == "response")
    assert draft == "Prioritize evidence intake this week."
    assert response_event["response"]
    assert response_event["event_id"]
    # Spoken audio is the governed final text, chunked at sentence boundaries.
    audio_events = [e for e in events if e["type"] == "audio"]
    spoken = "".join(
        base64.b64decode(e["audio_base64"]).decode("utf-8").removeprefix("FAKEWAV:") for e in audio_events
    )
    assert spoken == response_event["spoken_text"]
    done = events[-1]
    assert done["speech_error"] == ""
    assert done["audio_chunks"] == len(audio_events)
    timing = done["timing"]
    assert timing["pipeline"] == "streaming"
    assert timing["streaming"] == {"stt": False, "model": True, "tts": True, "playback": True}
    assert timing["milestones_ms"]["model_first_token"] is not None
    assert timing["milestones_ms"]["first_audio_byte"] is not None
    assert timing["milestones_ms"]["first_audio_byte"] >= timing["milestones_ms"]["model_response_complete"]
    # The exchange is audited as a streaming converse.
    audit = client.get("/audit/recent")
    assert "voice.converse_stream" in {e["action"] for e in audit.json()["events"]}


def test_converse_stream_records_episodic_turns(tmp_path: Path, monkeypatch) -> None:
    """Streaming answers land in conversation memory exactly like batch ones."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)
    client = TestClient(create_app(_config(tmp_path, _voice_config())))
    conversation = client.post("/conversations", json={"title": "Streamed voice thread"})
    conversation_id = conversation.json()["conversation"]["id"]

    events = _stream_events(
        client,
        {"audio_base64": FAKE_AUDIO, "conversation_id": conversation_id, "use_semantic_memory": False},
    )

    assert events[-1]["type"] == "done"
    turns = client.get(f"/conversations/{conversation_id}/turns").json()["turns"]
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[0]["content"] == "what should we prioritize next"


def test_converse_stream_surfaces_errors_as_events(tmp_path: Path, monkeypatch) -> None:
    """A stream cannot change its HTTP status mid-flight, so failures arrive as
    an explicit error event — never a silent truncation."""
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)
    broken_stt = VoiceConfig(
        stt_command=(sys.executable, "-c", FAILING_SCRIPT, "{audio}", "{transcript}"),
        tts_command=(sys.executable, "-c", TTS_SCRIPT, "{output}"),
    )
    client = TestClient(create_app(_config(tmp_path, broken_stt)))

    events = _stream_events(client, {"audio_base64": FAKE_AUDIO, "use_semantic_memory": False})

    assert [e["type"] for e in events] == ["error"]
    assert "exit 3" in events[0]["detail"]

    # TTS failure mid-stream: the reply still arrives in text, with the error named.
    stt_only = VoiceConfig(
        stt_command=(sys.executable, "-c", STT_SCRIPT, "{audio}", "{transcript}"),
        tts_command=(sys.executable, "-c", FAILING_SCRIPT, "{output}"),
    )
    client2 = TestClient(create_app(_config(tmp_path / "second", stt_only)))
    events2 = _stream_events(client2, {"audio_base64": FAKE_AUDIO, "use_semantic_memory": False})
    kinds2 = [e["type"] for e in events2]
    assert "response" in kinds2 and "audio" not in kinds2
    assert events2[-1]["type"] == "done"
    assert "exit 3" in events2[-1]["speech_error"]


def test_speech_chunks_split_on_sentences_first_chunk_small() -> None:
    from cofounder_kernel.voice import _speech_chunks

    assert _speech_chunks("") == []
    assert _speech_chunks("One short reply.") == ["One short reply."]
    long_first = "This is the opening sentence of a reply. " + " ".join(
        f"Sentence number {i} carries more of the answer." for i in range(2, 10)
    )
    chunks = _speech_chunks(long_first)
    assert len(chunks) >= 2
    assert chunks[0] == "This is the opening sentence of a reply."  # fast first audio
    assert all(len(c) <= 400 for c in chunks)
    # Nothing lost, nothing reordered, sentences never split mid-way.
    assert " ".join(chunks) == long_first


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
        json={"audio_base64": FAKE_AUDIO, "speak_full": True, "contrarian": True, "use_semantic_memory": False},
    )

    assert converse.status_code == 200
    assert "Contrarian check" in converse.json()["spoken_text"]
    spoken_audio = base64.b64decode(converse.json()["speech"]["audio_base64"]).decode("utf-8")
    assert "Contrarian check" in spoken_audio


def _cloud_voice_config() -> VoiceConfig:
    return VoiceConfig(stt_engine="deepgram", tts_engine="elevenlabs")


def test_cloud_voice_engines_are_gone_and_refuse_as_removed(tmp_path: Path, monkeypatch) -> None:
    """Stage-2 teardown: the Deepgram/ElevenLabs adapter code is DELETED. Even
    the strongest possible opt-in — cloud engine names configured, BOTH dead
    standing grants present, provider_policy raised, API keys set — refuses
    every voice operation without attempting a network call, and the refusal
    names the removal and the local path forward."""
    import urllib.request

    monkeypatch.setattr(OllamaClient, "health", fake_health)
    patch_ollama_model(monkeypatch, fake_generate)
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-key")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-key")

    def forbidden_urlopen(request, *args, **kwargs):
        raise AssertionError(f"cloud voice attempted a network call: {request}")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden_urlopen)
    client = TestClient(create_app(_cloud_ready_config(tmp_path, _cloud_voice_config())))

    status = client.get("/voice/status")
    transcribe = client.post("/voice/transcribe", json={"audio_base64": FAKE_AUDIO})
    speak = client.post("/voice/speak", json={"text": "hello"})
    converse = client.post("/voice/converse", json={"audio_base64": FAKE_AUDIO, "use_semantic_memory": False})

    # status reports the config as unusable, not ready
    assert status.status_code == 200
    assert status.json()["ready"] is False
    assert status.json()["stt"]["supported"] is False
    assert "removed" in status.json()["stt"]["reason"]
    # every operation refuses loudly; forbidden_urlopen proves zero bytes left
    assert transcribe.status_code == 503
    assert "removed" in transcribe.json()["detail"]
    assert "command" in transcribe.json()["detail"]
    assert speak.status_code == 503
    assert "removed" in speak.json()["detail"]
    assert converse.status_code == 503


def test_voice_module_has_no_cloud_remnants() -> None:
    """The adapter removal is total: no cloud hosts, no API-key plumbing, no
    urllib in the voice module. Guards against a partial revert."""
    import inspect

    source = inspect.getsource(voice_module)
    for remnant in ("api.deepgram.com", "api.elevenlabs.io", "urllib", "api_key", "_http_call"):
        assert remnant not in source, f"cloud-voice remnant in voice.py: {remnant}"


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


def test_cloud_engine_names_refuse_under_default_posture_too(tmp_path: Path, monkeypatch) -> None:
    """Default posture ([ollama] local_only, no [egress] grants) with cloud
    engine names in config: same refusal, no network call — the removal does
    not depend on egress posture; the code simply is not there."""
    import urllib.request

    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-key")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-key")

    def exploding_urlopen(request, *args, **kwargs):
        raise AssertionError("removed cloud voice must never reach the network")

    monkeypatch.setattr(urllib.request, "urlopen", exploding_urlopen)
    client = TestClient(create_app(_config(tmp_path, _cloud_voice_config())))

    transcribe = client.post("/voice/transcribe", json={"audio_base64": FAKE_AUDIO})
    speak = client.post("/voice/speak", json={"text": "hello"})

    assert transcribe.status_code == 503
    assert "removed" in transcribe.json()["detail"]
    assert "command" in transcribe.json()["detail"]
    assert speak.status_code == 503
    assert "removed" in speak.json()["detail"]
