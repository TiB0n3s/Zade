from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


DEFAULT_HOT_ROOT = Path(r"C:\AI Brain")
DEFAULT_COLD_ROOT = Path(r"D:\AI Brain-Cold")
DEFAULT_DATA_DIR = DEFAULT_HOT_ROOT / "memory-hot" / "cofounder-kernel"
DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SKILLS_DIR = DEFAULT_PROJECT_ROOT / ".agents" / "skills"


@dataclass(frozen=True)
class AppConfig:
    host: str = "127.0.0.1"
    port: int = 8787


@dataclass(frozen=True)
class IdentityConfig:
    name: str = "Zade"
    description: str = "Local-first AI co-founder and private operating partner."


@dataclass(frozen=True)
class PathConfig:
    hot_root: Path = DEFAULT_HOT_ROOT
    cold_root: Path = DEFAULT_COLD_ROOT
    data_dir: Path = DEFAULT_DATA_DIR

    @property
    def database_path(self) -> Path:
        return self.data_dir / "cofounder.sqlite"

    @property
    def blob_dir(self) -> Path:
        return self.data_dir / "blobs"

    @property
    def inbox_dir(self) -> Path:
        return self.hot_root / "inbox"

    @property
    def cold_raw_ingest_dir(self) -> Path:
        return self.cold_root / "raw-ingest"


@dataclass(frozen=True)
class OllamaConfig:
    base_url: str = "http://127.0.0.1:11434"
    chat_model: str = "qwen3:14b"
    reasoning_model: str = "deepseek-r1:14b"
    coding_model: str = "qwen2.5-coder:14b"
    embedding_model: str = "nomic-embed-text"
    think: bool = False
    temperature: float = 0.2

    def think_for_role(self, role: ModelRole) -> bool:
        return role == "reasoning"

    def model_for_role(self, role: ModelRole) -> str:
        if role == "reasoning":
            return self.reasoning_model
        if role == "coding":
            return self.coding_model
        if role == "embedding":
            return self.embedding_model
        return self.chat_model

    def roles(self) -> dict[str, str]:
        return {
            "general": self.chat_model,
            "reasoning": self.reasoning_model,
            "coding": self.coding_model,
            "embedding": self.embedding_model,
        }


ModelRole = Literal["general", "reasoning", "coding", "embedding"]


@dataclass(frozen=True)
class SecurityConfig:
    local_token: str = ""
    protect_mutations: bool = True


@dataclass(frozen=True)
class SkillConfig:
    source_dir: Path = DEFAULT_SKILLS_DIR
    lock_file: Path = DEFAULT_PROJECT_ROOT / "skills-lock.json"
    enable_defaults: bool = True
    max_prompt_chars: int = 1800


@dataclass(frozen=True)
class VoiceConfig:
    """Founder-configured local speech engines (e.g. whisper.cpp and piper).

    Commands are argv arrays run without a shell. STT commands may use the
    placeholders {audio}, {transcript}, and {transcript_base}; TTS commands may
    use {output} and receive the text to speak on stdin.
    """

    stt_command: tuple[str, ...] = ()
    tts_command: tuple[str, ...] = ()
    timeout_seconds: float = 120.0

    @property
    def stt_configured(self) -> bool:
        return bool(self.stt_command)

    @property
    def tts_configured(self) -> bool:
        return bool(self.tts_command)


@dataclass(frozen=True)
class KernelConfig:
    app: AppConfig = AppConfig()
    identity: IdentityConfig = IdentityConfig()
    paths: PathConfig = PathConfig()
    ollama: OllamaConfig = OllamaConfig()
    security: SecurityConfig = SecurityConfig()
    skills: SkillConfig = SkillConfig()
    voice: VoiceConfig = VoiceConfig()


def _read_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _path(value: str | os.PathLike[str] | None, fallback: Path) -> Path:
    return Path(value).expanduser() if value else fallback


def load_config(config_path: str | os.PathLike[str] | None = None) -> KernelConfig:
    path = Path(config_path) if config_path else Path.cwd() / "config.toml"
    raw = _read_toml(path)

    app_raw = raw.get("app", {})
    identity_raw = raw.get("identity", {})
    paths_raw = raw.get("paths", {})
    ollama_raw = raw.get("ollama", {})
    security_raw = raw.get("security", {})
    skills_raw = raw.get("skills", {})

    app = AppConfig(
        host=os.getenv("COFOUNDER_HOST", app_raw.get("host", "127.0.0.1")),
        port=int(os.getenv("COFOUNDER_PORT", app_raw.get("port", 8787))),
    )
    identity = IdentityConfig(
        name=os.getenv("COFOUNDER_NAME", identity_raw.get("name", "Zade")),
        description=os.getenv(
            "COFOUNDER_DESCRIPTION",
            identity_raw.get("description", "Local-first AI co-founder and private operating partner."),
        ),
    )
    paths = PathConfig(
        hot_root=_path(os.getenv("COFOUNDER_HOT_ROOT", paths_raw.get("hot_root")), DEFAULT_HOT_ROOT),
        cold_root=_path(os.getenv("COFOUNDER_COLD_ROOT", paths_raw.get("cold_root")), DEFAULT_COLD_ROOT),
        data_dir=_path(os.getenv("COFOUNDER_DATA_DIR", paths_raw.get("data_dir")), DEFAULT_DATA_DIR),
    )
    ollama = OllamaConfig(
        base_url=os.getenv("OLLAMA_BASE_URL", ollama_raw.get("base_url", "http://127.0.0.1:11434")).rstrip("/"),
        chat_model=os.getenv("COFOUNDER_CHAT_MODEL", ollama_raw.get("chat_model", "qwen3:14b")),
        reasoning_model=os.getenv("COFOUNDER_REASONING_MODEL", ollama_raw.get("reasoning_model", "deepseek-r1:14b")),
        coding_model=os.getenv("COFOUNDER_CODING_MODEL", ollama_raw.get("coding_model", "qwen2.5-coder:14b")),
        embedding_model=os.getenv("COFOUNDER_EMBEDDING_MODEL", ollama_raw.get("embedding_model", "nomic-embed-text")),
        think=_bool(os.getenv("COFOUNDER_THINK", ollama_raw.get("think", False))),
        temperature=float(os.getenv("COFOUNDER_TEMPERATURE", ollama_raw.get("temperature", 0.2))),
    )
    security = SecurityConfig(
        local_token=str(os.getenv("COFOUNDER_LOCAL_TOKEN", security_raw.get("local_token", "")) or ""),
        protect_mutations=_bool(os.getenv("COFOUNDER_PROTECT_MUTATIONS", security_raw.get("protect_mutations", True))),
    )
    skills = SkillConfig(
        source_dir=_path(os.getenv("COFOUNDER_SKILLS_DIR", skills_raw.get("source_dir")), DEFAULT_SKILLS_DIR),
        lock_file=_path(os.getenv("COFOUNDER_SKILLS_LOCK", skills_raw.get("lock_file")), DEFAULT_PROJECT_ROOT / "skills-lock.json"),
        enable_defaults=_bool(os.getenv("COFOUNDER_SKILLS_ENABLE_DEFAULTS", skills_raw.get("enable_defaults", True))),
        max_prompt_chars=int(os.getenv("COFOUNDER_SKILLS_MAX_PROMPT_CHARS", skills_raw.get("max_prompt_chars", 1800))),
    )
    voice_raw = raw.get("voice", {})
    voice = VoiceConfig(
        stt_command=_command(voice_raw.get("stt_command")),
        tts_command=_command(voice_raw.get("tts_command")),
        timeout_seconds=float(voice_raw.get("timeout_seconds", 120.0)),
    )
    return KernelConfig(app=app, identity=identity, paths=paths, ollama=ollama, security=security, skills=skills, voice=voice)


def ensure_local_paths(config: KernelConfig) -> None:
    config.paths.data_dir.mkdir(parents=True, exist_ok=True)
    config.paths.blob_dir.mkdir(parents=True, exist_ok=True)
    config.paths.inbox_dir.mkdir(parents=True, exist_ok=True)
    config.paths.cold_raw_ingest_dir.mkdir(parents=True, exist_ok=True)


def _bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _command(value: object) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if str(item).strip())
    raise ValueError("Voice commands must be TOML arrays of argv strings (no shell parsing).")
