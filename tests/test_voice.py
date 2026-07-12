import base64
import io
import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

import cofounder_kernel.voice as voice_module
from cofounder_kernel.api import create_app
from cofounder_kernel.config import AppConfig, KernelConfig, OllamaConfig, PathConfig, VoiceConfig
from cofounder_kernel.ollama import GenerateResult, OllamaClient


FAKE_AUDIO = base64.b64encode(b"RIFF-fake-wav-bytes").decode("ascii")

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


def fake_generate(self, *, prompt, model=None, think=None, temperature=None, num_predict=512):
    if model == "deepseek-r1:14b":
        return GenerateResult(response=CRITIC_JSON, model=model, raw={})
    return GenerateResult(response="Prioritize evidence intake this week.", model=model or "qwen3:14b", raw={})


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
    monkeypatch.setattr(OllamaClient, "generate", fake_generate)
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


def test_converse_speak_full_includes_contrarian_block(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "generate", fake_generate)
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
    client = TestClient(create_app(_config(tmp_path, _cloud_voice_config())))

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
    client = TestClient(create_app(_config(tmp_path, _cloud_voice_config())))

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
    assert deepgram_request.data == base64.b64decode(FAKE_AUDIO)
    elevenlabs_request = calls["requests"][1]
    assert "/v1/text-to-speech/voice123" in elevenlabs_request.full_url
    assert elevenlabs_request.get_header("Xi-api-key") == "el-key"
    assert json.loads(elevenlabs_request.data.decode("utf-8"))["text"] == "Evidence first."


def test_cloud_converse_end_to_end_and_http_errors(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    monkeypatch.setattr(OllamaClient, "generate", fake_generate)
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-key")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-key")

    def fake_urlopen(request, timeout=120):
        if "api.deepgram.com" in request.full_url:
            body = {"results": {"channels": [{"alternatives": [{"transcript": "summarize the current memory state"}]}]}}
            return FakeHttpResponse(json.dumps(body).encode("utf-8"))
        return FakeHttpResponse(b"MP3FAKE")

    monkeypatch.setattr(voice_module.urllib.request, "urlopen", fake_urlopen)
    client = TestClient(create_app(_config(tmp_path, _cloud_voice_config())))

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
    monkeypatch.setattr(OllamaClient, "generate", fake_generate)
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
