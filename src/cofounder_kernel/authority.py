from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .config import KernelConfig


POLICY_VERSION = "2026-07-12.local-first.v2"


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
        """Decide from the action taxonomy and target — never from metadata.

        Order: deny taxonomy on the action, then known-local allow (L0/L1),
        then deny phrases on the target, then external approval taxonomy, then
        tier defaults. The action string is authoritative; request metadata is
        payload and must not flip a decision.
        """
        action = request.action.strip().lower()
        target = request.target.strip().lower()
        tier = request.permission_tier.strip().upper()

        deny_token = _first_token_match(action, DENY_ACTION_TOKENS)
        if deny_token:
            return AuthorityResult(
                decision=AuthorityDecision.DENY,
                reason=f"Blocked by hard safety boundary: action names a denied capability ({deny_token}).",
                matched_rule="deny.action_token",
            )
        deny_phrase = _first_keyword_match(action, DENY_PHRASES)
        if deny_phrase:
            return AuthorityResult(
                decision=AuthorityDecision.DENY,
                reason=f"Blocked by hard safety boundary: {deny_phrase}",
                matched_rule="deny.action_phrase",
            )

        if tier in {"L0_READ", "L1_MEMORY_WRITE"} and _is_local_memory_action(action):
            return AuthorityResult(
                decision=AuthorityDecision.ALLOW,
                reason="Known local read/memory/ingestion action is autonomous.",
                matched_rule="local_action.autonomous",
            )

        deny_target = _first_keyword_match(target, DENY_PHRASES)
        if deny_target:
            return AuthorityResult(
                decision=AuthorityDecision.DENY,
                reason=f"Blocked by hard safety boundary: {deny_target}",
                matched_rule="deny.target_phrase",
            )

        approval_token = _first_token_match(action, APPROVAL_ACTION_TOKENS)
        if approval_token:
            return AuthorityResult(
                decision=AuthorityDecision.APPROVAL_REQUIRED,
                reason=f"Action touches files, systems, or external services: {approval_token}",
                requires_typed_phrase=True,
                typed_phrase=self.typed_confirmation_phrase,
                matched_rule="approval.action_token",
            )
        approval_phrase = _first_keyword_match(f"{action} {target}", APPROVAL_PHRASES)
        if approval_phrase:
            return AuthorityResult(
                decision=AuthorityDecision.APPROVAL_REQUIRED,
                reason=f"Action touches files, systems, or external services: {approval_phrase}",
                requires_typed_phrase=True,
                typed_phrase=self.typed_confirmation_phrase,
                matched_rule="approval.phrase",
            )

        if tier == "L0_READ":
            return AuthorityResult(
                decision=AuthorityDecision.ALLOW,
                reason="Unrecognized action presumed local read; execution still requires a registered handler.",
                matched_rule="tier.L0_READ.default",
            )
        if tier == "L1_MEMORY_WRITE":
            return AuthorityResult(
                decision=AuthorityDecision.APPROVAL_REQUIRED,
                reason="Memory write tier is not a known local-memory action.",
                requires_typed_phrase=True,
                typed_phrase=self.typed_confirmation_phrase,
                matched_rule="tier.L1_MEMORY_WRITE.unknown",
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
            "evaluation": {
                "order": [
                    "deny taxonomy on the action (tokens and phrases)",
                    "known-local action allow for L0/L1 tiers",
                    "deny phrases on the target",
                    "external approval taxonomy on the action",
                    "approval phrases on action and target",
                    "tier defaults",
                ],
                "metadata_scanned": False,
                "notes": (
                    "The action string is authoritative. Request metadata never changes a decision, "
                    "so payload content cannot trip or evade the safety boundary. "
                    "Deny screening runs before any tier-based allow, so a claimed tier cannot bypass it."
                ),
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


LOCAL_ACTION_PREFIXES = (
    "memory.",
    "ingest.",
    "work.",
    "brief.",
    "goal.",
    "self.",
    "founder.",
    "experiments.",
    "teach.",
    "evidence.",
    "runtime.",
    "skills.",
    "conversation.",
    "surface.",
    "evals.",
    "tool.memory.",
    "tool.audit.",
)


def _is_local_memory_action(action: str) -> bool:
    normalized = action.lower()
    return normalized.startswith(LOCAL_ACTION_PREFIXES)


def _action_tokens(action: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", action)


def _first_token_match(action: str, tokens: frozenset[str]) -> str:
    for token in _action_tokens(action):
        if token in tokens:
            return token
    return ""


def _first_keyword_match(text: str, keywords: tuple[str, ...]) -> str:
    for keyword in keywords:
        if keyword in text:
            return keyword
    return ""


# Denied capabilities named in the action itself. Tokenized matching (split on
# . _ -) so file paths or notes mentioning these words elsewhere cannot trip
# the boundary, while any action *named* for a denied capability always does.
DENY_ACTION_TOKENS = frozenset(
    {
        "broker",
        "brokerage",
        "trade",
        "trades",
        "trading",
        "order",
        "orders",
        "payment",
        "payments",
        "pay",
        "payout",
        "purchase",
        "purchases",
        "buy",
        "sell",
        "wire",
        "transfer",
        "transfers",
        "exfiltrate",
        "exfiltration",
        "steal",
    }
)


# High-signal multi-word phrases. Checked against the action always, and
# against the target only for actions that are not known-local (so a local
# ingest of a file that merely mentions one of these is not blocked).
DENY_PHRASES = (
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


# External or system-touching capabilities named in the action.
APPROVAL_ACTION_TOKENS = frozenset(
    {
        "email",
        "mail",
        "smtp",
        "browser",
        "calendar",
        "slack",
        "teams",
        "discord",
        "sms",
        "whatsapp",
        "github",
        "gitlab",
        "deploy",
        "deployment",
        "publish",
        "release",
        "shell",
        "powershell",
        "cmd",
        "command",
        "process",
        "service",
        "install",
        "uninstall",
        "download",
        "upload",
        "network",
        "http",
        "https",
        "api",
        "external",
    }
)


APPROVAL_PHRASES = (
    "edit file",
    "write file",
    "move file",
    "delete file",
    "remove file",
    "api call",
    "http://",
    "https://",
)
