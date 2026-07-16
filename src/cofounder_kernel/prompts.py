from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo


DEFAULT_PROFILE_ID = "general"

PERSONA_PROFILE_IDS = (
    "companion",
    "dark-comedian",
    "loyal-confidant",
    "study-mentor",
    "medical-information",
    "therapeutic-support",
)

PROFILE_IDS = (
    "general",
    "build",
    "expert",
    "account",
    "api",
    *PERSONA_PROFILE_IDS,
)

SUPPORTED_PLACEHOLDERS = ("{ZADE_HOME}", "{SKILLS_ROOT}", "{CURRENT_TIME}", "{CURRENTDATE}")

_ASSET_PACKAGE = "cofounder_kernel.prompt_assets.zade"
_INCOMPATIBLE_ACTIVE_PROMPT_TERMS = (
    "remote sandbox",
    "web_search",
    "x_keyword_search",
    "browse_page",
    "generate_image",
    "todo_write",
    "run_terminal_command",
    "grok",
    "xai",
    "elon musk",
)

_LOCAL_CAPABILITY_ADAPTER = """---------- Local runtime capability adapter ----------
This profile is active inside Zade's local cofounder kernel.
Source prompt tool lists, render components, sandbox claims, X/search/media tools, shell tools, schedulers, and subagent tools are not active instructions here.
Use only the runtime capability block assembled below from the local registry. If a capability is not listed there, say it is unavailable or route it through the existing approval/work-queue path.
Keep user content out of this profile block; the runtime supplies it as a separate user-role message."""


@dataclass(frozen=True)
class PromptProfile:
    id: str
    source_file: str
    purpose: str
    source_heading: str = ""


@dataclass(frozen=True)
class PromptRuntimeBindings:
    zade_home: Path
    skills_root: Path
    now: datetime
    timezone_name: str = "America/Chicago"

    def local_now(self) -> datetime:
        current = self.now
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        try:
            return current.astimezone(ZoneInfo(self.timezone_name))
        except Exception:
            return current.astimezone(timezone.utc)

    def replacements(self) -> dict[str, str]:
        local_now = self.local_now()
        return {
            "{ZADE_HOME}": str(self.zade_home),
            "{SKILLS_ROOT}": str(self.skills_root),
            "{CURRENT_TIME}": local_now.strftime("%A %Y-%m-%d %H:%M %Z"),
            "{CURRENTDATE}": local_now.strftime("%Y-%m-%d"),
        }


@dataclass(frozen=True)
class RenderedPromptProfile:
    profile_id: str
    source_file: str
    purpose: str
    content: str


@dataclass(frozen=True)
class ModelMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: str


class PromptProfileError(ValueError):
    pass


_PROFILES: dict[str, PromptProfile] = {
    "general": PromptProfile(
        id="general",
        source_file="zade-4.3-beta.md",
        purpose="Default full local profile.",
    ),
    "build": PromptProfile(
        id="build",
        source_file="zade-build.md",
        purpose="Local software-engineering operator.",
    ),
    "expert": PromptProfile(
        id="expert",
        source_file="zade-expert.md",
        purpose="Research and synthesis mode adapted to local runtime capabilities.",
    ),
    "account": PromptProfile(
        id="account",
        source_file="zade-account.md",
        purpose="Compact account or X-style replies without unavailable X/media tool claims.",
    ),
    "api": PromptProfile(
        id="api",
        source_file="zade-api.md",
        purpose="Compact policy and identity profile.",
    ),
    "companion": PromptProfile(
        id="companion",
        source_file="zade-personas.md",
        purpose="Adult companion persona profile.",
        source_heading="Companion",
    ),
    "dark-comedian": PromptProfile(
        id="dark-comedian",
        source_file="zade-personas.md",
        purpose="Dry dark-comedy persona profile.",
        source_heading="Dark Comedian",
    ),
    "loyal-confidant": PromptProfile(
        id="loyal-confidant",
        source_file="zade-personas.md",
        purpose="Loyal confidant persona profile.",
        source_heading="Loyal Confidant",
    ),
    "study-mentor": PromptProfile(
        id="study-mentor",
        source_file="zade-personas.md",
        purpose="Exacting study mentor persona profile.",
        source_heading="Study Mentor",
    ),
    "medical-information": PromptProfile(
        id="medical-information",
        source_file="zade-personas.md",
        purpose="Medical-information support profile.",
        source_heading="Medical Information",
    ),
    "therapeutic-support": PromptProfile(
        id="therapeutic-support",
        source_file="zade-personas.md",
        purpose="Grounded emotional and behavioral support profile.",
        source_heading="Therapeutic Support",
    ),
}


class PromptProfileRegistry:
    def list_profiles(self) -> list[dict[str, str]]:
        return [self.profile_summary(profile_id) for profile_id in PROFILE_IDS]

    def profile(self, profile_id: str) -> PromptProfile:
        resolved = self.resolve_profile_id(profile_id, configured_default=None)
        return _PROFILES[resolved]

    def profile_summary(self, profile_id: str) -> dict[str, str]:
        profile = self.profile(profile_id)
        summary = {
            "id": profile.id,
            "purpose": profile.purpose,
            "source_file": profile.source_file,
            "capability_source": "local_runtime_registry",
            "tool_claims": "source_tool_lists_excluded_from_active_prompt",
        }
        if profile.source_heading:
            summary["source_heading"] = profile.source_heading
        return summary

    def resolve_profile_id(self, requested: str | None, *, configured_default: str | None) -> str:
        candidate = (requested or configured_default or DEFAULT_PROFILE_ID).strip()
        if not candidate:
            candidate = DEFAULT_PROFILE_ID
        if candidate not in _PROFILES:
            valid = ", ".join(PROFILE_IDS)
            raise PromptProfileError(f"Unknown Zade prompt profile '{candidate}'. Valid profiles: {valid}.")
        return candidate

    def render_profile(self, profile_id: str, *, bindings: PromptRuntimeBindings) -> RenderedPromptProfile:
        profile = self.profile(profile_id)
        source = self._read_source(profile.source_file)
        active_text = self._active_text(profile, source)
        resolved = resolve_supported_placeholders(active_text, bindings=bindings)
        validate_no_supported_placeholders(resolved)
        validate_tool_compatibility(resolved, profile_id=profile.id)
        return RenderedPromptProfile(
            profile_id=profile.id,
            source_file=profile.source_file,
            purpose=profile.purpose,
            content=resolved.strip(),
        )

    def _read_source(self, source_file: str) -> str:
        try:
            asset = resources.files(_ASSET_PACKAGE).joinpath(source_file)
            return asset.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise PromptProfileError(f"Zade prompt asset is missing: {source_file}") from exc
        except UnicodeDecodeError as exc:
            raise PromptProfileError(f"Zade prompt asset is not valid UTF-8: {source_file}") from exc

    def _active_text(self, profile: PromptProfile, source: str) -> str:
        if profile.source_heading:
            body = _persona_profile_text(source, profile.source_heading)
        elif profile.id == "general":
            body = _before_required(source, "You have access to a remote sandbox computer.", profile.id)
        elif profile.id == "build":
            body = _between_required(source, "## 1. Core System Prompt", "## Task Management", profile.id)
        elif profile.id == "expert":
            body = _expert_profile_text(source)
        elif profile.id == "account":
            body = _account_profile_text(source)
        elif profile.id == "api":
            body = source
        else:
            raise PromptProfileError(f"No prompt adapter registered for profile '{profile.id}'.")
        return f"""Profile source excerpt:
{body.strip()}

{_LOCAL_CAPABILITY_ADAPTER}
"""


def resolve_supported_placeholders(text: str, *, bindings: PromptRuntimeBindings) -> str:
    resolved = text
    for token, value in bindings.replacements().items():
        resolved = resolved.replace(token, value)
    validate_no_supported_placeholders(resolved)
    return resolved


def validate_no_supported_placeholders(text: str) -> None:
    remaining = [token for token in SUPPORTED_PLACEHOLDERS if token in text]
    if remaining:
        raise PromptProfileError("Unresolved supported placeholder(s): " + ", ".join(remaining))


def validate_tool_compatibility(text: str, *, profile_id: str) -> None:
    lowered = text.lower()
    incompatible = [term for term in _INCOMPATIBLE_ACTIVE_PROMPT_TERMS if term in lowered]
    if incompatible:
        raise PromptProfileError(
            f"Prompt profile '{profile_id}' advertises unavailable local capability terms: "
            + ", ".join(incompatible)
        )


def _before_required(text: str, marker: str, profile_id: str) -> str:
    index = text.find(marker)
    if index < 0:
        raise PromptProfileError(f"Malformed prompt asset for profile '{profile_id}': missing marker '{marker}'.")
    return text[:index]


def _between_required(text: str, start_marker: str, end_marker: str, profile_id: str) -> str:
    start = text.find(start_marker)
    end = text.find(end_marker)
    if start < 0 or end < 0 or end <= start:
        raise PromptProfileError(
            f"Malformed prompt asset for profile '{profile_id}': expected markers '{start_marker}' and '{end_marker}'."
        )
    return text[start:end]


def _expert_profile_text(source: str) -> str:
    body = _before_required(source, "Available Tools:", "expert")
    first_line = (
        "You are Zade. Harper, Benjamin, and Lucas are your analysis team. "
        "You lead the work and deliver the final answer. The team receives the same prompt and tool access; "
        "only you can use render components."
    )
    body = body.replace(
        first_line,
        "You are Zade in expert research-and-synthesis mode. Lead the work and own the final judgment.",
    )
    return body


def _account_profile_text(source: str) -> str:
    dropped_phrases = (
        "real-time search",
        "x tools",
        "attached image or video",
        "media url",
        "search-derived claims",
    )
    kept = []
    for line in source.splitlines():
        lowered = line.lower()
        if any(phrase in lowered for phrase in dropped_phrases):
            continue
        kept.append(line)
    kept.append("")
    kept.append(
        "- This local runtime does not expose account, search, or media-inspection tools inside the model prompt. "
        "Do not claim those checks were performed."
    )
    return "\n".join(kept)


def _persona_profile_text(source: str, heading: str) -> str:
    baseline = _markdown_section(source, level=2, title="Shared Baseline")
    selected = _markdown_section(source, level=1, title=heading)
    return f"{baseline.strip()}\n\n{selected.strip()}"


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


def _markdown_section(text: str, *, level: int, title: str) -> str:
    matches = [
        match
        for match in _HEADING_RE.finditer(text)
        if len(match.group(1)) == level and _normalize_heading(match.group(2)) == _normalize_heading(title)
    ]
    if len(matches) != 1:
        raise PromptProfileError(
            f"Malformed persona prompt asset: expected exactly one heading {'#' * level} {title}, found {len(matches)}."
        )
    start_match = matches[0]
    end = len(text)
    for match in _HEADING_RE.finditer(text, start_match.end()):
        if len(match.group(1)) <= level:
            end = match.start()
            break
    return text[start_match.start() : end]


def _normalize_heading(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())
