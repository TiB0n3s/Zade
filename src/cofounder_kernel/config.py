from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


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
    # Warmer temperature for the conversational persona turn only (Zade's chat
    # voice). Summarization, distillation, extraction, and the critic keep the
    # low `temperature` above for determinism. Raising this unsticks the voice
    # from flat/generic phrasing that a low temperature tends to produce.
    chat_temperature: float = 0.65
    # Agentic investigation loop: let the chat model call whitelisted read-only
    # tools (trading-bot reads, memory search) before answering, instead of
    # narrating checks it cannot perform. Each round is a full model call, so
    # the cap bounds worst-case latency.
    tool_loop: bool = True
    tool_loop_max_rounds: int = 3
    # Server-side structured output: JSON-contract calls (contrarian critic,
    # role passes, distillation) send their JSON schema as Ollama's `format`
    # field, so the shape is enforced at sampling time instead of resting on
    # prompt discipline alone. Kill switch — turn off if a model misbehaves
    # under grammar constraints; prompts and tolerant parsers work unchanged.
    structured_output: bool = True
    # ---- provider policy (local-first, default local-only) ----
    # local_only:      loopback Ollama + verified local models only; no cloud,
    #                  no remote Ollama, no fallback; failures fail closed.
    # local_preferred: local Ollama first; cloud still needs an explicit
    #                  per-request authorization; never an automatic fallback.
    # cloud_allowed:   cloud possible only when configuration permits it AND the
    #                  request explicitly opts in. Installed API keys never count
    #                  as authorization and never change routing.
    provider_policy: str = "local_only"
    allow_remote_ollama: bool = False
    allow_ollama_cloud: bool = False
    allow_cloud_inference: bool = False
    cloud_fallback: str = "never"
    # Model for the native local coding agent (tool-calling loop). Empty =
    # resolve at runtime: probe the configured coding_model, then chat_model,
    # for native tool-call support; fail with candidates if neither passes.
    # Not defaulted to a specific machine's model on purpose.
    coding_agent_model: str = ""

    def think_for_role(self, role: ModelRole) -> bool:
        return role in {"reasoning", "coding"}

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
    # Dev-only: browser origins allowed to call the kernel cross-origin (CORS).
    # Empty = disabled (the shipping no-CORS loopback posture). Non-loopback
    # origins and "*" are refused at startup; see api._validated_cors_origins.
    cors_dev_origins: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillConfig:
    source_dir: Path = DEFAULT_SKILLS_DIR
    lock_file: Path = DEFAULT_PROJECT_ROOT / "skills-lock.json"
    enable_defaults: bool = True
    max_prompt_chars: int = 1800


@dataclass(frozen=True)
class VoiceConfig:
    """Founder-configured speech engines.

    Engine "command" runs local argv arrays without a shell (e.g. whisper.cpp
    and piper). STT commands may use the placeholders {audio}, {transcript},
    and {transcript_base}; TTS commands may use {output} and receive the text
    to speak on stdin.

    Engines "deepgram" (STT) and "elevenlabs" (TTS) call the founder's cloud
    speech APIs. Selecting one is an explicit standing grant: audio and reply
    text leave the machine. API keys are read from the referenced environment
    variables and are never stored in config files or the database.
    """

    stt_engine: str = "command"
    tts_engine: str = "command"
    stt_command: tuple[str, ...] = ()
    tts_command: tuple[str, ...] = ()
    stt_api_key_env: str = "DEEPGRAM_API_KEY"
    tts_api_key_env: str = "ELEVENLABS_API_KEY"
    stt_model: str = "nova-2"
    tts_model: str = "eleven_turbo_v2_5"
    tts_voice: str = "21m00Tcm4TlvDq8ikWAM"
    timeout_seconds: float = 120.0

    @property
    def stt_configured(self) -> bool:
        if self.stt_engine == "command":
            return bool(self.stt_command)
        return True

    @property
    def tts_configured(self) -> bool:
        if self.tts_engine == "command":
            return bool(self.tts_command)
        return True


@dataclass(frozen=True)
class TradingBotConfig:
    enabled: bool = True
    wsl_distro: str = "Ubuntu-TradingBot-C"
    repo_path: str = "/home/tradingbot/trading-bot"
    python: str = "./venv/bin/python"
    timeout_seconds: float = 120.0


@dataclass(frozen=True)
class DevToolsConfig:
    """Workspace Zade may act in through approved developer action handlers.

    workspace_root is a trusted local grant, like the Ollama endpoint. Every
    dev action still dispatches only through approval + the typed confirmation
    phrase; this just bounds where those actions run.
    """

    workspace_root: Path = DEFAULT_PROJECT_ROOT
    default_branch: str = "main"
    command_timeout_seconds: float = 300.0


@dataclass(frozen=True)
class BrowserConfig:
    """Headed browser automation via approved dispatch.

    Like the connector layer, every flow still runs only through founder
    approval + the typed confirmation phrase; these settings just bound how the
    browser runs. ``headless`` defaults to false because the whole point is a
    visible browser the founder can watch. Navigation to private/internal hosts
    is refused unless ``allow_private_navigation`` is set, matching netguard's
    SSRF stance for the kernel's own egress.
    """

    enabled: bool = True
    headless: bool = False
    browser: str = "chromium"
    nav_timeout_seconds: float = 30.0
    action_timeout_seconds: float = 15.0
    max_steps: int = 25
    allow_private_navigation: bool = False


@dataclass(frozen=True)
class VaultConfig:
    """Whole-vault file operator (move/delete) via approved dispatch.

    Deletes and clobbered move targets go to a trash snapshot under the kernel
    state dir (restorable), never a hard unlink. Guards layer on top of the
    approval + typed-phrase gate: any path segment in ``guard_segments`` (raw
    source-of-truth folders) is refused; a ``protected_marker`` file protects
    its whole subtree (per-project instruction precedence over the global
    allow); and operations on a top-level folder (a direct child of a root)
    require explicit confirmation so a single mis-scoped approval cannot wipe a
    whole project area.
    """

    enabled: bool = True
    guard_segments: tuple[str, ...] = ("01-raw", "raw-ingest")
    protected_marker: str = ".zade-protected"
    instructions_marker: str = ".zade-instructions.md"
    list_limit: int = 500
    search_limit: int = 200


@dataclass(frozen=True)
class TrayConfig:
    """Resident desktop tray shell.

    A separate process (installed at logon) that polls the kernel over loopback
    and shows status + OS toasts. Read-only: it never mutates, so it needs no
    token. ``poll_interval_seconds`` bounds how often it re-checks state.
    """

    enabled: bool = True
    poll_interval_seconds: float = 15.0
    toasts: bool = True
    max_toast_notifications: int = 5


@dataclass(frozen=True)
class ResearchConfig:
    """Autonomous web research.

    Topic derivation is local and needs nothing here. The web-fetch lane is the
    kernel's one deliberate outbound-to-the-open-web exception, so its bounds
    live here: how many URLs per approved run, timeouts, a byte cap, an optional
    host allowlist (empty = any public https host), and the default reliability
    grade for filed web evidence. Every fetch is still approval-gated.
    """

    enabled: bool = True
    max_urls_per_run: int = 5
    fetch_timeout_seconds: float = 20.0
    max_fetch_bytes: int = 2_000_000
    max_text_chars: int = 8000
    allow_hosts: tuple[str, ...] = ()
    default_reliability: str = "C"


@dataclass(frozen=True)
class RolesConfig:
    """Local specialist role panel (swarm, local half). Fully local; no bounds to
    set beyond an on/off — each role is one governed pass on a local model."""

    enabled: bool = True


@dataclass(frozen=True)
class DelegationConfig:
    """Delegated specialist work (swarm, frontier half).

    Auto-invoke is on by founder decision, bounded by a daily budget; past it,
    invocation falls back to typed-phrase approval. The external agent is a
    configured argv command (no shell). Empty command = brief-only (can't invoke).
    """

    enabled: bool = True
    auto_invoke: bool = True
    agent_command: tuple[str, ...] = ()
    daily_budget: int = 25
    timeout_seconds: float = 600.0
    max_output_chars: int = 20_000
    default_reliability: str = "C"
    # Directory the external agent runs in (created on first use). Empty = the
    # kernel's own working directory. Point it OUTSIDE this repo so delegated
    # builds can never write into Zade's own code.
    workspace_root: str = ""
    # Delegated-build engine:
    #   native  (default) — Zade's own coding-agent loop on the local Ollama
    #             model; no external agent process at all.
    #   bridge  — launch agent_command as a LOCAL COMPATIBILITY BRIDGE: under a
    #             local provider policy its subprocess env is sanitized to the
    #             loopback Ollama Anthropic-compatible API (never a cloud key).
    #   brief   — prepare-not-send: package the brief only.
    # There is NO automatic fallback between engines.
    engine: str = "native"


@dataclass(frozen=True)
class ScreenConfig:
    """Local screen awareness. Explicit, on-demand; the textual read is free, the
    pixel snapshot is the optional 'screen' extra and is confined + pruned."""

    enabled: bool = True
    storage_subdir: str = "screen-captures"
    keep_last: int = 20
    max_windows: int = 60


@dataclass(frozen=True)
class PromptProfileConfig:
    default: str = "general"


@dataclass(frozen=True)
class KernelConfig:
    app: AppConfig = AppConfig()
    identity: IdentityConfig = IdentityConfig()
    paths: PathConfig = PathConfig()
    ollama: OllamaConfig = OllamaConfig()
    security: SecurityConfig = SecurityConfig()
    skills: SkillConfig = SkillConfig()
    voice: VoiceConfig = VoiceConfig()
    trading_bot: TradingBotConfig = TradingBotConfig()
    devtools: DevToolsConfig = DevToolsConfig()
    browser: BrowserConfig = BrowserConfig()
    vault: VaultConfig = VaultConfig()
    tray: TrayConfig = TrayConfig()
    research: ResearchConfig = ResearchConfig()
    roles: RolesConfig = RolesConfig()
    delegation: DelegationConfig = DelegationConfig()
    screen: ScreenConfig = ScreenConfig()
    prompt_profiles: PromptProfileConfig = PromptProfileConfig()


def _read_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _path(value: str | os.PathLike[str] | None, fallback: Path) -> Path:
    return Path(value).expanduser() if value else fallback


def _csv_origins(env_value: str | None, raw: Any) -> tuple[str, ...]:
    """Parse dev CORS origins from an env CSV or a TOML list into a tuple.

    Env wins over the TOML value (matches every other override in this loader).
    Trailing slashes are stripped so "http://localhost:5173/" and the bare form
    compare equal to a browser Origin header, which never carries a path.
    """
    if env_value is not None:
        items: list[str] = env_value.split(",")
    elif isinstance(raw, (list, tuple)):
        items = [str(item) for item in raw]
    else:
        items = []
    return tuple(item.strip().rstrip("/") for item in items if item and item.strip())


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
        chat_temperature=float(os.getenv("COFOUNDER_CHAT_TEMPERATURE", ollama_raw.get("chat_temperature", 0.65))),
        tool_loop=_bool(os.getenv("COFOUNDER_TOOL_LOOP", ollama_raw.get("tool_loop", True))),
        tool_loop_max_rounds=int(os.getenv("COFOUNDER_TOOL_LOOP_MAX_ROUNDS", ollama_raw.get("tool_loop_max_rounds", 3))),
        structured_output=_bool(os.getenv("COFOUNDER_STRUCTURED_OUTPUT", ollama_raw.get("structured_output", True))),
        provider_policy=_provider_policy(
            os.getenv("COFOUNDER_PROVIDER_POLICY", ollama_raw.get("provider_policy", "local_only"))
        ),
        allow_remote_ollama=_bool(
            os.getenv("COFOUNDER_ALLOW_REMOTE_OLLAMA", ollama_raw.get("allow_remote_ollama", False))
        ),
        allow_ollama_cloud=_bool(
            os.getenv("COFOUNDER_ALLOW_OLLAMA_CLOUD", ollama_raw.get("allow_ollama_cloud", False))
        ),
        allow_cloud_inference=_bool(
            os.getenv("COFOUNDER_ALLOW_CLOUD_INFERENCE", ollama_raw.get("allow_cloud_inference", False))
        ),
        cloud_fallback=str(ollama_raw.get("cloud_fallback", "never")).strip() or "never",
        coding_agent_model=str(
            os.getenv("COFOUNDER_CODING_AGENT_MODEL", ollama_raw.get("coding_agent_model", ""))
        ).strip(),
    )
    security = SecurityConfig(
        local_token=str(os.getenv("COFOUNDER_LOCAL_TOKEN", security_raw.get("local_token", "")) or ""),
        protect_mutations=_bool(os.getenv("COFOUNDER_PROTECT_MUTATIONS", security_raw.get("protect_mutations", True))),
        cors_dev_origins=_csv_origins(os.getenv("COFOUNDER_CORS_DEV_ORIGINS"), security_raw.get("cors_dev_origins")),
    )
    skills = SkillConfig(
        source_dir=_path(os.getenv("COFOUNDER_SKILLS_DIR", skills_raw.get("source_dir")), DEFAULT_SKILLS_DIR),
        lock_file=_path(os.getenv("COFOUNDER_SKILLS_LOCK", skills_raw.get("lock_file")), DEFAULT_PROJECT_ROOT / "skills-lock.json"),
        enable_defaults=_bool(os.getenv("COFOUNDER_SKILLS_ENABLE_DEFAULTS", skills_raw.get("enable_defaults", True))),
        max_prompt_chars=int(os.getenv("COFOUNDER_SKILLS_MAX_PROMPT_CHARS", skills_raw.get("max_prompt_chars", 1800))),
    )
    voice_raw = raw.get("voice", {})
    voice = VoiceConfig(
        stt_engine=str(voice_raw.get("stt_engine", "command")).strip().lower(),
        tts_engine=str(voice_raw.get("tts_engine", "command")).strip().lower(),
        stt_command=_command(voice_raw.get("stt_command")),
        tts_command=_command(voice_raw.get("tts_command")),
        stt_api_key_env=str(voice_raw.get("stt_api_key_env", "DEEPGRAM_API_KEY")).strip(),
        tts_api_key_env=str(voice_raw.get("tts_api_key_env", "ELEVENLABS_API_KEY")).strip(),
        stt_model=str(voice_raw.get("stt_model", "nova-2")).strip(),
        tts_model=str(voice_raw.get("tts_model", "eleven_turbo_v2_5")).strip(),
        tts_voice=str(voice_raw.get("tts_voice", "21m00Tcm4TlvDq8ikWAM")).strip(),
        timeout_seconds=float(voice_raw.get("timeout_seconds", 120.0)),
    )
    trading_bot_raw = raw.get("trading_bot", {})
    trading_bot = TradingBotConfig(
        enabled=_bool(os.getenv("ZADE_TRADING_BOT_ENABLED", trading_bot_raw.get("enabled", True))),
        wsl_distro=str(os.getenv("ZADE_TRADING_BOT_WSL_DISTRO", trading_bot_raw.get("wsl_distro", "Ubuntu-TradingBot-C"))),
        repo_path=str(os.getenv("ZADE_TRADING_BOT_REPO_PATH", trading_bot_raw.get("repo_path", "/home/tradingbot/trading-bot"))),
        python=str(os.getenv("ZADE_TRADING_BOT_PYTHON", trading_bot_raw.get("python", "./venv/bin/python"))),
        timeout_seconds=float(os.getenv("ZADE_TRADING_BOT_TIMEOUT_SECONDS", trading_bot_raw.get("timeout_seconds", 120.0))),
    )
    devtools_raw = raw.get("devtools", {})
    devtools = DevToolsConfig(
        workspace_root=_path(
            os.getenv("COFOUNDER_WORKSPACE_ROOT", devtools_raw.get("workspace_root")), DEFAULT_PROJECT_ROOT
        ),
        default_branch=str(os.getenv("COFOUNDER_DEFAULT_BRANCH", devtools_raw.get("default_branch", "main"))),
        command_timeout_seconds=float(devtools_raw.get("command_timeout_seconds", 300.0)),
    )
    browser_raw = raw.get("browser", {})
    browser = BrowserConfig(
        enabled=_bool(os.getenv("ZADE_BROWSER_ENABLED", browser_raw.get("enabled", True))),
        headless=_bool(os.getenv("ZADE_BROWSER_HEADLESS", browser_raw.get("headless", False))),
        browser=str(os.getenv("ZADE_BROWSER_ENGINE", browser_raw.get("browser", "chromium"))).strip().lower(),
        nav_timeout_seconds=float(browser_raw.get("nav_timeout_seconds", 30.0)),
        action_timeout_seconds=float(browser_raw.get("action_timeout_seconds", 15.0)),
        max_steps=int(browser_raw.get("max_steps", 25)),
        allow_private_navigation=_bool(
            os.getenv("ZADE_BROWSER_ALLOW_PRIVATE", browser_raw.get("allow_private_navigation", False))
        ),
    )
    vault_raw = raw.get("vault", {})
    vault = VaultConfig(
        enabled=_bool(os.getenv("ZADE_VAULT_ENABLED", vault_raw.get("enabled", True))),
        guard_segments=_segments(vault_raw.get("guard_segments"), ("01-raw", "raw-ingest")),
        protected_marker=str(vault_raw.get("protected_marker", ".zade-protected")).strip(),
        instructions_marker=str(vault_raw.get("instructions_marker", ".zade-instructions.md")).strip(),
        list_limit=int(vault_raw.get("list_limit", 500)),
        search_limit=int(vault_raw.get("search_limit", 200)),
    )
    tray_raw = raw.get("tray", {})
    tray = TrayConfig(
        enabled=_bool(os.getenv("ZADE_TRAY_ENABLED", tray_raw.get("enabled", True))),
        poll_interval_seconds=float(tray_raw.get("poll_interval_seconds", 15.0)),
        toasts=_bool(tray_raw.get("toasts", True)),
        max_toast_notifications=int(tray_raw.get("max_toast_notifications", 5)),
    )
    research_raw = raw.get("research", {})
    research = ResearchConfig(
        enabled=_bool(os.getenv("ZADE_RESEARCH_ENABLED", research_raw.get("enabled", True))),
        max_urls_per_run=int(research_raw.get("max_urls_per_run", 5)),
        fetch_timeout_seconds=float(research_raw.get("fetch_timeout_seconds", 20.0)),
        max_fetch_bytes=int(research_raw.get("max_fetch_bytes", 2_000_000)),
        max_text_chars=int(research_raw.get("max_text_chars", 8000)),
        allow_hosts=_segments(research_raw.get("allow_hosts"), ()),
        default_reliability=str(research_raw.get("default_reliability", "C")).strip() or "C",
    )
    roles_raw = raw.get("roles", {})
    roles = RolesConfig(
        enabled=_bool(os.getenv("ZADE_ROLES_ENABLED", roles_raw.get("enabled", True))),
    )
    delegation_raw = raw.get("delegation", {})
    delegation = DelegationConfig(
        enabled=_bool(os.getenv("ZADE_DELEGATION_ENABLED", delegation_raw.get("enabled", True))),
        auto_invoke=_bool(os.getenv("ZADE_DELEGATION_AUTO_INVOKE", delegation_raw.get("auto_invoke", True))),
        agent_command=_command(delegation_raw.get("agent_command")),
        daily_budget=int(os.getenv("ZADE_DELEGATION_DAILY_BUDGET", delegation_raw.get("daily_budget", 25))),
        timeout_seconds=float(delegation_raw.get("timeout_seconds", 600.0)),
        max_output_chars=int(delegation_raw.get("max_output_chars", 20_000)),
        default_reliability=str(delegation_raw.get("default_reliability", "C")).strip() or "C",
        workspace_root=str(
            os.getenv("ZADE_DELEGATION_WORKSPACE_ROOT", delegation_raw.get("workspace_root", ""))
        ).strip(),
        engine=_delegation_engine(os.getenv("ZADE_DELEGATION_ENGINE", delegation_raw.get("engine", "native"))),
    )
    screen_raw = raw.get("screen", {})
    screen = ScreenConfig(
        enabled=_bool(os.getenv("ZADE_SCREEN_ENABLED", screen_raw.get("enabled", True))),
        storage_subdir=str(screen_raw.get("storage_subdir", "screen-captures")).strip() or "screen-captures",
        keep_last=int(screen_raw.get("keep_last", 20)),
        max_windows=int(screen_raw.get("max_windows", 60)),
    )
    prompt_profiles_raw = raw.get("prompt_profiles", {})
    prompt_profiles = PromptProfileConfig(
        default=str(os.getenv("ZADE_PROMPT_PROFILE", prompt_profiles_raw.get("default", "general"))).strip()
        or "general",
    )
    return KernelConfig(
        app=app,
        identity=identity,
        paths=paths,
        ollama=ollama,
        security=security,
        skills=skills,
        voice=voice,
        trading_bot=trading_bot,
        devtools=devtools,
        browser=browser,
        vault=vault,
        tray=tray,
        research=research,
        roles=roles,
        delegation=delegation,
        screen=screen,
        prompt_profiles=prompt_profiles,
    )


def ensure_local_paths(config: KernelConfig) -> None:
    config.paths.data_dir.mkdir(parents=True, exist_ok=True)
    config.paths.blob_dir.mkdir(parents=True, exist_ok=True)
    config.paths.inbox_dir.mkdir(parents=True, exist_ok=True)
    config.paths.cold_raw_ingest_dir.mkdir(parents=True, exist_ok=True)


def _delegation_engine(value: object) -> str:
    engine = str(value or "").strip().lower() or "native"
    if engine not in {"native", "bridge", "brief"}:
        raise ValueError(f"Invalid delegation engine {engine!r}: must be native, bridge, or brief.")
    return engine


def _provider_policy(value: object) -> str:
    policy = str(value or "").strip().lower() or "local_only"
    if policy not in {"local_only", "local_preferred", "cloud_allowed"}:
        raise ValueError(
            f"Invalid provider_policy {policy!r}: must be local_only, local_preferred, or cloud_allowed."
        )
    return policy


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


def _segments(value: object, fallback: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return fallback
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    raise ValueError("vault.guard_segments must be a TOML array of folder-name strings.")
