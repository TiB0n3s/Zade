import base64
import sys
from pathlib import Path

from fastapi.testclient import TestClient

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
