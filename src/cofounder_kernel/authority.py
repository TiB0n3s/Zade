from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .config import KernelConfig


POLICY_VERSION = "2026-07-11.local-first.v1"


class AuthorityDecision(StrEnum):
    ALLOW = "allow"
    APPROVAL_REQUIRED = "approval_required"
    DENY = "deny"


@dataclass(frozen=True)
class AuthorityRequest:
    action: str
    permission_tier: str
    target: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AuthorityResult:
    decision: AuthorityDecision
    reason: str
    policy_version: str = POLICY_VERSION
    requires_typed_phrase: bool = False
    typed_phrase: str | None = None
    matched_rule: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "reason": self.reason,
            "policy_version": self.policy_version,
            "requires_typed_phrase": self.requires_typed_phrase,
            "typed_phrase": self.typed_phrase,
            "matched_rule": self.matched_rule,
        }


class AuthorityPolicy:
    def __init__(
        self,
        *,
        hot_root: Path,
        cold_root: Path,
        data_dir: Path,
        typed_confirmation_phrase: str = "make the jump to hyperspace",
    ):
        self.hot_root = hot_root
        self.cold_root = cold_root
        self.data_dir = data_dir
        self.typed_confirmation_phrase = typed_confirmation_phrase

    @classmethod
    def from_config(cls, config: KernelConfig) -> AuthorityPolicy:
        return cls(
            hot_root=config.paths.hot_root,
            cold_root=config.paths.cold_root,
            data_dir=config.paths.data_dir,
        )

    def evaluate(self, request: AuthorityRequest) -> AuthorityResult:
        tier = request.permission_tier.upper()
        if tier == "L0_READ":
            return AuthorityResult(
                decision=AuthorityDecision.ALLOW,
                reason="Local read/search/generation actions are autonomous.",
                matched_rule="tier.L0_READ.autonomous",
            )

        if tier == "L1_MEMORY_WRITE":
            if _is_local_memory_action(request.action):
                return AuthorityResult(
                    decision=AuthorityDecision.ALLOW,
                    reason="Local memory and local ingestion writes are autonomous.",
                    matched_rule="tier.L1_MEMORY_WRITE.local_memory",
                )
            return AuthorityResult(
                decision=AuthorityDecision.APPROVAL_REQUIRED,
                reason="Memory write tier is not a known local-memory action.",
                requires_typed_phrase=True,
                typed_phrase=self.typed_confirmation_phrase,
                matched_rule="tier.L1_MEMORY_WRITE.unknown",
            )

        normalized = _search_text(request)
        deny_match = _first_keyword_match(normalized, DENY_KEYWORDS)
        if deny_match:
            return AuthorityResult(
                decision=AuthorityDecision.DENY,
                reason=f"Blocked by hard safety boundary: {deny_match}",
                matched_rule="deny.keyword",
            )

        approval_match = _first_keyword_match(normalized, APPROVAL_KEYWORDS)
        if approval_match:
            return AuthorityResult(
                decision=AuthorityDecision.APPROVAL_REQUIRED,
                reason=f"Action changes files, systems, or external services: {approval_match}",
                requires_typed_phrase=True,
                typed_phrase=self.typed_confirmation_phrase,
                matched_rule="approval.keyword",
            )

        if tier == "L2_FILE_WRITE":
            return AuthorityResult(
                decision=AuthorityDecision.APPROVAL_REQUIRED,
                reason="Generic file writes need an explicit grant.",
                requires_typed_phrase=True,
                typed_phrase=self.typed_confirmation_phrase,
                matched_rule="tier.L2_FILE_WRITE.default",
            )

        if tier == "L3_EXTERNAL_ACTION":
            return AuthorityResult(
                decision=AuthorityDecision.APPROVAL_REQUIRED,
                reason="External actions need an explicit grant.",
                requires_typed_phrase=True,
                typed_phrase=self.typed_confirmation_phrase,
                matched_rule="tier.L3_EXTERNAL_ACTION.default",
            )

        return AuthorityResult(
            decision=AuthorityDecision.APPROVAL_REQUIRED,
            reason=f"Unknown permission tier: {request.permission_tier}",
            requires_typed_phrase=True,
            typed_phrase=self.typed_confirmation_phrase,
            matched_rule="tier.unknown",
        )

    def summary(self) -> dict[str, Any]:
        return {
            "policy_version": POLICY_VERSION,
            "typed_confirmation_phrase": self.typed_confirmation_phrase,
            "roots": {
                "hot_root": str(self.hot_root),
                "cold_root": str(self.cold_root),
                "data_dir": str(self.data_dir),
            },
            "autonomous": [
                "Local model chat and reasoning through Ollama",
                "Local memory search",
                "Local semantic document search",
                "Local audit and daily brief reads",
                "Local memory writes",
                "Text/file/folder ingestion under configured memory roots",
                "Cold archive copies for ingested files",
            ],
            "approval_required": [
                "Generic file writes or edits",
                "Shell commands and process control",
                "Installing software or changing services",
                "Browser, email, calendar, messaging, GitHub, or other external actions",
                "Network/API calls outside the configured local Ollama endpoint",
                "Source-control publishing or deployment",
            ],
            "denied": [
                "Live trading, broker mutation, order placement, or account-risk changes",
                "Credential, token, password, or secret exfiltration",
                "Destructive disk, vault, registry, system, or security-control changes",
                "Payments, transfers, purchases, or irreversible external commitments",
            ],
        }


def build_self_inventory(
    *,
    config: KernelConfig,
    authority: AuthorityPolicy,
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "identity": {
            "name": config.identity.name,
            "mode": "local-first",
            "purpose": config.identity.description,
        },
        "locality": {
            "local_only": True,
            "ollama_base_url": config.ollama.base_url,
            "external_api_required": False,
        },
        "models": config.ollama.roles(),
        "paths": {
            "hot_root": str(config.paths.hot_root),
            "cold_root": str(config.paths.cold_root),
            "data_dir": str(config.paths.data_dir),
            "database": str(config.paths.database_path),
            "inbox": str(config.paths.inbox_dir),
            "cold_raw_ingest": str(config.paths.cold_raw_ingest_dir),
        },
        "authority": authority.summary(),
        "tools": tools,
        "operating_rule": (
            "Act autonomously for local read, reasoning, memory, and ingestion work. "
            "Require explicit approval for generic file/system/external actions. "
            "Deny destructive, credential, financial, broker, and live-trading mutations."
        ),
    }


def _is_local_memory_action(action: str) -> bool:
    normalized = action.lower()
    return (
        normalized.startswith("memory.")
        or normalized.startswith("ingest.")
        or normalized.startswith("work.")
        or normalized.startswith("brief.")
        or normalized.startswith("goal.")
        or normalized.startswith("self.")
        or normalized.startswith("founder.")
        or normalized.startswith("tool.memory.")
        or normalized.startswith("tool.audit.")
    )


def _search_text(request: AuthorityRequest) -> str:
    return " ".join(
        [
            request.action,
            request.permission_tier,
            request.target,
            str(request.metadata),
        ]
    ).lower()


def _first_keyword_match(text: str, keywords: tuple[str, ...]) -> str:
    for keyword in keywords:
        if keyword in text:
            return keyword
    return ""


DENY_KEYWORDS = (
    "broker",
    "place order",
    "order placement",
    "live order",
    "live trade",
    "trade execution",
    "auto-buy",
    "auto buy",
    "auto-sell",
    "auto sell",
    "wire transfer",
    "bank transfer",
    "send payment",
    "purchase",
    "credential exfiltration",
    "exfiltrate",
    "dump secrets",
    "steal token",
    "password dump",
    "api key dump",
    "delete vault",
    "format disk",
    "rm -rf",
    "remove-item -recurse -force c:\\",
    "disable firewall",
    "disable defender",
    "disable security",
    "registry delete",
)


APPROVAL_KEYWORDS = (
    "shell",
    "powershell",
    "command",
    "process",
    "service",
    "install",
    "uninstall",
    "edit file",
    "write file",
    "move file",
    "delete file",
    "remove file",
    "browser",
    "email",
    "calendar",
    "slack",
    "teams",
    "github",
    "deploy",
    "publish",
    "http://",
    "https://",
    "api call",
    "external",
)
