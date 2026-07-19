from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from .build_types import BuildTier, LeaseLimits, PricingSnapshot


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
    # Read timeout (seconds) for a generate/chat/embed request. Non-streaming
    # Ollama sends nothing until generation finishes, so this budget must cover
    # a COLD start too: loading a ~12GB model into VRAM (or reloading after an
    # eviction under OLLAMA_MAX_LOADED_MODELS) plus the generation itself. 180s
    # was too tight for a cold load and produced spurious "Ollama request timed
    # out" failures on the first message after idle. Health/version/tags probes
    # use a separate short timeout and are unaffected.
    request_timeout_seconds: float = 600.0
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
    """Founder-configured speech engines. Local-only.

    The single supported engine, "command", runs local argv arrays without a
    shell (e.g. whisper.cpp and piper). STT commands may use the placeholders
    {audio}, {transcript}, and {transcript_base}; TTS commands may use {output}
    and receive the text to speak on stdin. The former cloud engines
    (deepgram/elevenlabs) were removed 2026-07-19; a config still naming one
    fails closed as unconfigured.

    ``ffmpeg_path`` is only consulted for STT input conversion, which needs
    16 kHz mono PCM WAV; leave it empty to find ffmpeg on PATH.
    """

    stt_engine: str = "command"
    tts_engine: str = "command"
    stt_command: tuple[str, ...] = ()
    tts_command: tuple[str, ...] = ()
    timeout_seconds: float = 120.0
    ffmpeg_path: str = ""

    @property
    def stt_configured(self) -> bool:
        return self.stt_engine == "command" and bool(self.stt_command)

    @property
    def tts_configured(self) -> bool:
        return self.tts_engine == "command" and bool(self.tts_command)


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
    #   hybrid  — assess locally, require a project-scoped paid lease, and route
    #             each step local-first through the governed build lifecycle.
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
class AnthropicConfig:
    """Cloud inference via Anthropic — the FIRST non-local model client.

    Off by default. A strategic review reaches Anthropic only when ALL hold:
      * ``enabled = true`` here (a deliberate config opt-in), and
      * the egress gate issues a per-request founder grant for
        ``founder_brief → anthropic`` (typed-phrase approval), and
      * ``[ollama] provider_policy`` is not ``local_only``, and
      * ``ANTHROPIC_API_KEY`` is set in the environment.
    The key is read from the env var and is never written to config or the DB.
    Only a curated founder_brief may go — raw founder_state is FORBIDDEN by the
    egress matrix regardless of this section.
    """

    enabled: bool = False
    base_url: str = "https://api.anthropic.com/v1/messages"
    model: str = "claude-opus-4-8"
    api_key_env: str = "ANTHROPIC_API_KEY"
    anthropic_version: str = "2023-06-01"
    max_tokens: int = 2048
    timeout_seconds: float = 120.0


@dataclass(frozen=True)
class OpenAIPricingConfig:
    """Conservative GPT-5.6 Terra pricing snapshot for optional build review."""

    model: str = "gpt-5.6-terra"
    base_input_per_mtok: Decimal = Decimal("2.5")
    cache_write_5m_per_mtok: Decimal = Decimal("2.5")
    cache_write_1h_per_mtok: Decimal = Decimal("2.5")
    cache_read_per_mtok: Decimal = Decimal("2.5")
    output_per_mtok: Decimal = Decimal("15")
    review_after: str = "2026-08-31"

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("OpenAI review pricing model must not be empty")
        for field_name in (
            "base_input_per_mtok",
            "cache_write_5m_per_mtok",
            "cache_write_1h_per_mtok",
            "cache_read_per_mtok",
            "output_per_mtok",
        ):
            try:
                value = Decimal(str(getattr(self, field_name)))
            except InvalidOperation as exc:
                raise ValueError(f"OpenAI review pricing {field_name} must be numeric") from exc
            if value <= 0:
                raise ValueError(f"OpenAI review pricing {field_name} must be positive")
            object.__setattr__(self, field_name, value)
        try:
            date.fromisoformat(self.review_after)
        except ValueError as exc:
            raise ValueError("OpenAI review pricing review_after must be YYYY-MM-DD") from exc

    def snapshot(self) -> PricingSnapshot:
        return PricingSnapshot(
            provider="openai",
            model=self.model,
            base_input_per_mtok=self.base_input_per_mtok,
            cache_write_5m_per_mtok=self.cache_write_5m_per_mtok,
            cache_write_1h_per_mtok=self.cache_write_1h_per_mtok,
            cache_read_per_mtok=self.cache_read_per_mtok,
            output_per_mtok=self.output_per_mtok,
            review_after=self.review_after,
        )

    def is_current(self, *, at: str | None = None) -> bool:
        checked = date.fromisoformat(at[:10]) if at else datetime.now(timezone.utc).date()
        return checked <= date.fromisoformat(self.review_after)


@dataclass(frozen=True)
class OpenAIReviewConfig:
    enabled: bool = False
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-5.6-terra"
    api_key_env: str = "OPENAI_API_KEY"
    max_output_tokens: int = 4000
    timeout_seconds: float = 120.0
    reasoning_effort: str = "medium"
    pricing: OpenAIPricingConfig = OpenAIPricingConfig()

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("OpenAI review model must not be empty")
        if self.model != self.pricing.model:
            raise ValueError("OpenAI review model must match its pricing snapshot")
        if self.max_output_tokens <= 0 or self.timeout_seconds <= 0:
            raise ValueError("OpenAI review output and timeout limits must be positive")
        if self.reasoning_effort not in {"none", "low", "medium", "high"}:
            raise ValueError("OpenAI review reasoning_effort is invalid")


@dataclass(frozen=True)
class BuildTierConfig:
    dollar_micro: int
    input_tokens: int
    output_tokens: int
    cloud_turns: int
    duration_seconds: int

    def __post_init__(self) -> None:
        for field_name in (
            "dollar_micro",
            "input_tokens",
            "output_tokens",
            "cloud_turns",
            "duration_seconds",
        ):
            if int(getattr(self, field_name)) <= 0:
                raise ValueError(f"build tier {field_name} must be positive")

    def as_limits(self) -> LeaseLimits:
        return LeaseLimits(
            dollar_micro=self.dollar_micro,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cloud_turns=self.cloud_turns,
            duration_seconds=self.duration_seconds,
        )


@dataclass(frozen=True)
class AnthropicPricingConfig:
    model: str = "claude-opus-4-8"
    base_input_per_mtok: Decimal = Decimal("5")
    cache_write_5m_per_mtok: Decimal = Decimal("6.25")
    cache_write_1h_per_mtok: Decimal = Decimal("10")
    cache_read_per_mtok: Decimal = Decimal("0.5")
    output_per_mtok: Decimal = Decimal("25")
    review_after: str = "2026-08-31"

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("build Anthropic pricing model must not be empty")
        for field_name in (
            "base_input_per_mtok",
            "cache_write_5m_per_mtok",
            "cache_write_1h_per_mtok",
            "cache_read_per_mtok",
            "output_per_mtok",
        ):
            try:
                value = Decimal(str(getattr(self, field_name)))
            except InvalidOperation as exc:
                raise ValueError(f"build Anthropic pricing {field_name} must be numeric") from exc
            if value <= 0:
                raise ValueError(f"build Anthropic pricing {field_name} must be positive")
            object.__setattr__(self, field_name, value)
        try:
            date.fromisoformat(self.review_after)
        except ValueError as exc:
            raise ValueError("build Anthropic pricing review_after must be YYYY-MM-DD") from exc

    def is_current(self, *, at: str | None = None) -> bool:
        if at:
            checked = date.fromisoformat(at[:10])
        else:
            checked = datetime.now(timezone.utc).date()
        return checked <= date.fromisoformat(self.review_after)

    def snapshot(self) -> PricingSnapshot:
        return PricingSnapshot(
            provider="anthropic",
            model=self.model,
            base_input_per_mtok=self.base_input_per_mtok,
            cache_write_5m_per_mtok=self.cache_write_5m_per_mtok,
            cache_write_1h_per_mtok=self.cache_write_1h_per_mtok,
            cache_read_per_mtok=self.cache_read_per_mtok,
            output_per_mtok=self.output_per_mtok,
            review_after=self.review_after,
        )


@dataclass(frozen=True)
class BuildConfig:
    enabled: bool = True
    warning_percent: int = 80
    provider_overhead_tokens: int = 1024
    max_workers: int = 2
    ios_workflow: str = "ios.yml"
    small: BuildTierConfig = BuildTierConfig(1_000_000, 120_000, 16_000, 6, 7200)
    medium: BuildTierConfig = BuildTierConfig(3_000_000, 400_000, 40_000, 16, 14400)
    large: BuildTierConfig = BuildTierConfig(7_000_000, 1_000_000, 80_000, 32, 28800)
    anthropic_pricing: AnthropicPricingConfig = AnthropicPricingConfig()

    def __post_init__(self) -> None:
        if not 1 <= self.warning_percent <= 99:
            raise ValueError("build warning_percent must be between 1 and 99")
        if self.provider_overhead_tokens < 0:
            raise ValueError("build provider_overhead_tokens must be non-negative")
        if not 1 <= self.max_workers <= 8:
            raise ValueError("build max_workers must be between 1 and 8")
        if not self.ios_workflow.strip():
            raise ValueError("build ios_workflow must not be empty")
        ordered = (self.small, self.medium, self.large)
        for field_name in (
            "dollar_micro",
            "input_tokens",
            "output_tokens",
            "cloud_turns",
            "duration_seconds",
        ):
            values = [int(getattr(tier, field_name)) for tier in ordered]
            if values != sorted(values):
                raise ValueError(f"build tier {field_name} values must be monotonic")

    def limits(self, tier: BuildTier | str) -> LeaseLimits:
        selected = BuildTier(str(tier))
        return {
            BuildTier.SMALL: self.small,
            BuildTier.MEDIUM: self.medium,
            BuildTier.LARGE: self.large,
        }[selected].as_limits()


@dataclass(frozen=True)
class OpenClawConfig:
    """Channel gateway bridge (OpenClaw). OFF by default.

    When enabled, a background client connects to the local OpenClaw gateway's
    WebSocket, receives inbound channel messages (WhatsApp/Telegram/etc.), routes
    each through the SAME governed ``/channels/message`` path (channel auth +
    capped authority + optional HMAC), and sends Zade's reply back via the
    gateway. The gateway is a LOCAL process (loopback); the token authenticates
    Zade to it and is read from ``token_env`` — never stored in config or the DB.

    Zade connects as an OPERATOR client (observe + reply), not a registered
    agent: it does not run the channel's model, it governs and answers.
    """

    enabled: bool = False
    ws_url: str = "ws://127.0.0.1:18789"
    token_env: str = "OPENCLAW_GATEWAY_TOKEN"
    channel_prefix: str = "openclaw"
    reconnect_min_seconds: float = 1.0
    reconnect_max_seconds: float = 30.0
    # Only loopback gateways are allowed unless this is set — a channel bridge to
    # a non-local host would route the founder's messages off-machine.
    allow_remote_gateway: bool = False


@dataclass(frozen=True)
class TelegramConfig:
    """Direct Telegram Bot API adapter. OFF by default.

    When enabled, a background long-poll loop calls Telegram's ``getUpdates`` for
    inbound messages, routes each through the SAME governed ``/channels/message``
    flow (channel auth + capped authority + optional HMAC), and replies via
    ``sendMessage``. No OpenClaw, no external gateway — Zade is the transport AND
    the brain, matching its local-first, dependency-light design.

    The bot token is read from ``token_env`` (never stored in config or the DB).
    Zade's reply is REPLY_TEXT leaving to a cloud channel, so it passes the data-
    class egress gate: a ``reply_text:telegram`` standing grant is required, and
    without it the channel is fail-closed (inbound still binds, but no reply
    leaves). Enabling the adapter is opting into that grant.
    """

    enabled: bool = False
    token_env: str = "TELEGRAM_BOT_TOKEN"
    api_base: str = "https://api.telegram.org"
    # Long-poll seconds Telegram holds getUpdates open with no messages.
    poll_timeout_seconds: int = 25
    # Cap on a single outbound reply; Telegram hard-limits at 4096 chars.
    max_reply_chars: int = 4000
    reconnect_min_seconds: float = 1.0
    reconnect_max_seconds: float = 30.0


@dataclass(frozen=True)
class EgressConfig:
    """Data-class egress gate (see egress.py and EGRESS-DESIGN.md).

    ``standing_grants`` are durable ``"data_class:vendor"`` authorizations for
    the matrix's STANDING cells — e.g. ``"reply_text:telegram"`` lets governed
    channel replies leave. Empty by default: under the shipped local-first
    posture nothing cloud egresses, and the gate is inert anyway while
    ``[ollama] provider_policy`` stays ``local_only``. The gate reads
    provider_policy from the ollama section; it is not duplicated here.
    """

    standing_grants: tuple[str, ...] = ()


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
    egress: EgressConfig = EgressConfig()
    anthropic: AnthropicConfig = AnthropicConfig()
    openai_review: OpenAIReviewConfig = OpenAIReviewConfig()
    build: BuildConfig = BuildConfig()
    openclaw: OpenClawConfig = OpenClawConfig()
    telegram: TelegramConfig = TelegramConfig()
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
        request_timeout_seconds=float(
            os.getenv("COFOUNDER_OLLAMA_REQUEST_TIMEOUT", ollama_raw.get("request_timeout_seconds", 600.0))
        ),
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
        timeout_seconds=float(voice_raw.get("timeout_seconds", 120.0)),
        ffmpeg_path=str(voice_raw.get("ffmpeg_path", "")).strip(),
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
    egress_raw = raw.get("egress", {})
    egress = EgressConfig(
        standing_grants=_segments(egress_raw.get("standing_grants"), ()),
    )
    anthropic_raw = raw.get("anthropic", {})
    anthropic = AnthropicConfig(
        enabled=_bool(os.getenv("ZADE_ANTHROPIC_ENABLED", anthropic_raw.get("enabled", False))),
        base_url=str(anthropic_raw.get("base_url", "https://api.anthropic.com/v1/messages")).rstrip("/"),
        model=str(anthropic_raw.get("model", "claude-opus-4-8")).strip(),
        api_key_env=str(anthropic_raw.get("api_key_env", "ANTHROPIC_API_KEY")).strip(),
        anthropic_version=str(anthropic_raw.get("anthropic_version", "2023-06-01")).strip(),
        max_tokens=int(anthropic_raw.get("max_tokens", 2048)),
        timeout_seconds=float(anthropic_raw.get("timeout_seconds", 120.0)),
    )
    openai_raw = raw.get("openai_review", {})
    openai_model = str(openai_raw.get("model", "gpt-5.6-terra")).strip()
    openai_pricing = _openai_pricing_config(
        openai_raw.get("pricing", {}), model=openai_model
    )
    openai_review = OpenAIReviewConfig(
        enabled=_bool(
            os.getenv("ZADE_OPENAI_REVIEW_ENABLED", openai_raw.get("enabled", False))
        ),
        base_url=str(
            openai_raw.get("base_url", "https://api.openai.com/v1")
        ).rstrip("/"),
        model=openai_model,
        api_key_env=str(openai_raw.get("api_key_env", "OPENAI_API_KEY")).strip(),
        max_output_tokens=int(openai_raw.get("max_output_tokens", 4000)),
        timeout_seconds=float(openai_raw.get("timeout_seconds", 120.0)),
        reasoning_effort=str(openai_raw.get("reasoning_effort", "medium")).strip(),
        pricing=openai_pricing,
    )
    build_raw = raw.get("build", {})
    tier_raw = build_raw.get("tiers", {})
    build = BuildConfig(
        enabled=_bool(os.getenv("ZADE_BUILD_ENABLED", build_raw.get("enabled", True))),
        warning_percent=int(build_raw.get("warning_percent", 80)),
        provider_overhead_tokens=int(build_raw.get("provider_overhead_tokens", 1024)),
        max_workers=int(build_raw.get("max_workers", 2)),
        ios_workflow=str(build_raw.get("ios_workflow", "ios.yml")).strip(),
        small=_build_tier_config(tier_raw.get("small", {}), BuildConfig().small),
        medium=_build_tier_config(tier_raw.get("medium", {}), BuildConfig().medium),
        large=_build_tier_config(tier_raw.get("large", {}), BuildConfig().large),
        anthropic_pricing=_anthropic_pricing_config(build_raw.get("anthropic_pricing", {})),
    )
    openclaw_raw = raw.get("openclaw", {})
    openclaw = OpenClawConfig(
        enabled=_bool(os.getenv("ZADE_OPENCLAW_ENABLED", openclaw_raw.get("enabled", False))),
        ws_url=str(os.getenv("ZADE_OPENCLAW_WS_URL", openclaw_raw.get("ws_url", "ws://127.0.0.1:18789"))).strip(),
        token_env=str(openclaw_raw.get("token_env", "OPENCLAW_GATEWAY_TOKEN")).strip(),
        channel_prefix=str(openclaw_raw.get("channel_prefix", "openclaw")).strip() or "openclaw",
        reconnect_min_seconds=float(openclaw_raw.get("reconnect_min_seconds", 1.0)),
        reconnect_max_seconds=float(openclaw_raw.get("reconnect_max_seconds", 30.0)),
        allow_remote_gateway=_bool(openclaw_raw.get("allow_remote_gateway", False)),
    )
    telegram_raw = raw.get("telegram", {})
    telegram = TelegramConfig(
        enabled=_bool(os.getenv("ZADE_TELEGRAM_ENABLED", telegram_raw.get("enabled", False))),
        token_env=str(telegram_raw.get("token_env", "TELEGRAM_BOT_TOKEN")).strip() or "TELEGRAM_BOT_TOKEN",
        api_base=str(telegram_raw.get("api_base", "https://api.telegram.org")).rstrip("/"),
        poll_timeout_seconds=int(telegram_raw.get("poll_timeout_seconds", 25)),
        max_reply_chars=int(telegram_raw.get("max_reply_chars", 4000)),
        reconnect_min_seconds=float(telegram_raw.get("reconnect_min_seconds", 1.0)),
        reconnect_max_seconds=float(telegram_raw.get("reconnect_max_seconds", 30.0)),
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
        egress=egress,
        anthropic=anthropic,
        openai_review=openai_review,
        build=build,
        openclaw=openclaw,
        telegram=telegram,
        prompt_profiles=prompt_profiles,
    )


def ensure_local_paths(config: KernelConfig) -> None:
    config.paths.data_dir.mkdir(parents=True, exist_ok=True)
    config.paths.blob_dir.mkdir(parents=True, exist_ok=True)
    config.paths.inbox_dir.mkdir(parents=True, exist_ok=True)
    config.paths.cold_raw_ingest_dir.mkdir(parents=True, exist_ok=True)


def _delegation_engine(value: object) -> str:
    engine = str(value or "").strip().lower() or "native"
    if engine not in {"native", "hybrid", "bridge", "brief"}:
        raise ValueError(
            f"Invalid delegation engine {engine!r}: must be native, hybrid, bridge, or brief."
        )
    return engine


def _build_tier_config(raw: dict[str, Any], default: BuildTierConfig) -> BuildTierConfig:
    return BuildTierConfig(
        dollar_micro=int(raw.get("dollar_micro", default.dollar_micro)),
        input_tokens=int(raw.get("input_tokens", default.input_tokens)),
        output_tokens=int(raw.get("output_tokens", default.output_tokens)),
        cloud_turns=int(raw.get("cloud_turns", default.cloud_turns)),
        duration_seconds=int(raw.get("duration_seconds", default.duration_seconds)),
    )


def _anthropic_pricing_config(raw: dict[str, Any]) -> AnthropicPricingConfig:
    default = AnthropicPricingConfig()
    return AnthropicPricingConfig(
        model=str(raw.get("model", default.model)).strip(),
        base_input_per_mtok=Decimal(str(raw.get("base_input_per_mtok", default.base_input_per_mtok))),
        cache_write_5m_per_mtok=Decimal(
            str(raw.get("cache_write_5m_per_mtok", default.cache_write_5m_per_mtok))
        ),
        cache_write_1h_per_mtok=Decimal(
            str(raw.get("cache_write_1h_per_mtok", default.cache_write_1h_per_mtok))
        ),
        cache_read_per_mtok=Decimal(str(raw.get("cache_read_per_mtok", default.cache_read_per_mtok))),
        output_per_mtok=Decimal(str(raw.get("output_per_mtok", default.output_per_mtok))),
        review_after=str(raw.get("review_after", default.review_after)).strip(),
    )


def _openai_pricing_config(
    raw: dict[str, Any], *, model: str
) -> OpenAIPricingConfig:
    default = OpenAIPricingConfig()
    return OpenAIPricingConfig(
        model=str(raw.get("model", model or default.model)).strip(),
        base_input_per_mtok=Decimal(
            str(raw.get("base_input_per_mtok", default.base_input_per_mtok))
        ),
        cache_write_5m_per_mtok=Decimal(
            str(raw.get("cache_write_5m_per_mtok", default.cache_write_5m_per_mtok))
        ),
        cache_write_1h_per_mtok=Decimal(
            str(raw.get("cache_write_1h_per_mtok", default.cache_write_1h_per_mtok))
        ),
        cache_read_per_mtok=Decimal(
            str(raw.get("cache_read_per_mtok", default.cache_read_per_mtok))
        ),
        output_per_mtok=Decimal(
            str(raw.get("output_per_mtok", default.output_per_mtok))
        ),
        review_after=str(raw.get("review_after", default.review_after)).strip(),
    )


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
