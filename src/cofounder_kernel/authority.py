from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .config import KernelConfig


POLICY_VERSION = "2026-07-12.local-first.v3"


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

        Order (each step justified against a conflicting constraint):
          1. deny tokens on the action (unambiguous denied capabilities)
          2. financial *execution* on the action (noun + verb) — discussion is allowed
          3. deny phrases on the normalized action
          4. deny phrases / financial-execution on the target, but ONLY for
             non-local actions, so ingesting a file named "wire transfer notes"
             is not blocked
          5. external/system approval screening on the action — BEFORE the local
             allow, so an external capability named under a local prefix
             (self.install, work.http) cannot ride the prefix to auto-allow
          6. known-local read/memory allow at L0/L1
          7. tier defaults
        The action string is authoritative; request metadata never flips a decision.
        """
        action = request.action.strip().lower()
        target = request.target.strip().lower()
        tier = request.permission_tier.strip().upper()
        action_norm = _normalize(action)
        action_tokens = set(_action_tokens(action))

        deny_token = _first_token_match(action, DENY_ACTION_TOKENS)
        if deny_token:
            return AuthorityResult(
                decision=AuthorityDecision.DENY,
                reason=f"Blocked by hard safety boundary: action names a denied capability ({deny_token}).",
                matched_rule="deny.action_token",
            )
        financial = _financial_execution(action_tokens)
        if financial:
            return AuthorityResult(
                decision=AuthorityDecision.DENY,
                reason=f"Blocked by hard safety boundary: financial execution is not permitted ({financial}).",
                matched_rule="deny.financial_execution",
            )
        deny_phrase = _first_phrase(action_norm, DENY_PHRASES)
        if deny_phrase:
            return AuthorityResult(
                decision=AuthorityDecision.DENY,
                reason=f"Blocked by hard safety boundary: {deny_phrase}",
                matched_rule="deny.action_phrase",
            )

        known_local = _is_local_memory_action(action)
        if not known_local and target:
            target_norm = _normalize(target)
            deny_target_token = _first_token_match(target, DENY_ACTION_TOKENS)
            if deny_target_token:
                return AuthorityResult(
                    decision=AuthorityDecision.DENY,
                    reason=f"Blocked by hard safety boundary: target names a denied capability ({deny_target_token}).",
                    matched_rule="deny.target_token",
                )
            deny_target = _first_phrase(target_norm, DENY_PHRASES)
            if deny_target:
                return AuthorityResult(
                    decision=AuthorityDecision.DENY,
                    reason=f"Blocked by hard safety boundary: {deny_target}",
                    matched_rule="deny.target_phrase",
                )
            financial_target = _financial_execution(set(_action_tokens(target)))
            if financial_target:
                return AuthorityResult(
                    decision=AuthorityDecision.DENY,
                    reason=f"Blocked by hard safety boundary: financial execution in target ({financial_target}).",
                    matched_rule="deny.target_financial",
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
        approval_phrase = _first_phrase(action_norm, APPROVAL_PHRASES)
        if approval_phrase:
            return AuthorityResult(
                decision=AuthorityDecision.APPROVAL_REQUIRED,
                reason=f"Action touches files, systems, or external services: {approval_phrase}",
                requires_typed_phrase=True,
                typed_phrase=self.typed_confirmation_phrase,
                matched_rule="approval.phrase",
            )

        if tier in {"L0_READ", "L1_MEMORY_WRITE"} and known_local:
            return AuthorityResult(
                decision=AuthorityDecision.ALLOW,
                reason="Known local read/memory/ingestion action is autonomous.",
                matched_rule="local_action.autonomous",
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
                    "deny tokens on the action (unambiguous denied capabilities)",
                    "financial execution on the action (a financial noun plus an execution verb)",
                    "deny phrases on the normalized action",
                    "deny phrases / financial execution on the target (non-local actions only)",
                    "external/system approval screening on the action (before the local allow)",
                    "known-local read/memory allow for L0/L1 tiers",
                    "tier defaults",
                ],
                "metadata_scanned": False,
                "notes": (
                    "The action string is authoritative; request metadata never changes a decision. "
                    "Financial vocabulary (order/payment/trade) is allowed for analysis and only denied when "
                    "paired with an execution verb, so a founder can review finances but Zade can never transact. "
                    "External-capability screening runs before the local-prefix allow, so a name like "
                    "self.install cannot ride a local prefix to auto-allow."
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


def _normalize(text: str) -> str:
    """Collapse every non-alphanumeric run to a single space so multi-word
    phrases match dotted/underscored action names (broker.place_order ->
    'broker place order') and free-text targets ('rm -rf C:\\' -> 'rm rf c')."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _first_phrase(normalized_text: str, phrases: tuple[str, ...]) -> str:
    padded = f" {normalized_text} "
    for phrase in phrases:
        if f" {phrase} " in padded or phrase in normalized_text:
            return phrase
    return ""


def _financial_execution(tokens: set[str]) -> str:
    """A financial *noun* plus an *execution verb* is a transaction Zade must
    never initiate. Analysis/review of the same noun (no execution verb) is
    ordinary co-founder work and is allowed."""
    nouns = tokens & FINANCIAL_NOUNS
    verbs = tokens & EXECUTION_VERBS
    if nouns and verbs:
        return f"{sorted(nouns)[0]}+{sorted(verbs)[0]}"
    return ""


# Capabilities that have no benign use as an action segment — denied outright.
DENY_ACTION_TOKENS = frozenset(
    {
        "exfiltrate",
        "exfiltration",
        "keylog",
        "keylogger",
        "ransomware",
        "stripe",
        "paypal",
        "venmo",
        "coinbase",
        "brokerage",
    }
)


# Financial nouns and execution verbs. The DENY tier fires only when BOTH are
# present (a transaction), so a co-founder can freely review orders, payments,
# and trading strategy while never executing one.
FINANCIAL_NOUNS = frozenset(
    {
        "broker",
        "trade",
        "trades",
        "trading",
        "order",
        "orders",
        "payment",
        "payments",
        "payout",
        "payouts",
        "purchase",
        "purchases",
        "invoice",
        "invoices",
        "wire",
        "transfer",
        "transfers",
        "withdrawal",
        "withdrawals",
        "deposit",
        "deposits",
        "securities",
        "shares",
        "stock",
        "stocks",
        "crypto",
        "cryptocurrency",
        "bitcoin",
        "funds",
    }
)
EXECUTION_VERBS = frozenset(
    {
        "execute",
        "place",
        "submit",
        "initiate",
        "buy",
        "sell",
        "send",
        "pay",
        "withdraw",
        "move",
        "short",
        "liquidate",
        "cancel",
        "fill",
        "settle",
        "remit",
    }
)


# Denied multi-word patterns (matched against normalized action/target).
DENY_PHRASES = (
    "wire transfer",
    "bank transfer",
    "transfer funds",
    "send funds",
    "move funds",
    "wire funds",
    "send money",
    "send payment",
    "withdraw funds",
    "make purchase",
    "complete purchase",
    "place order",
    "order placement",
    "live trade",
    "credential exfiltration",
    "dump secrets",
    "dump credentials",
    "steal token",
    "steal credentials",
    "password dump",
    "api key dump",
    "delete vault",
    "format disk",
    "rm rf",
    "disable firewall",
    "disable defender",
    "disable security",
    "registry delete",
    "delete registry",
    "drop database",
    "drop table",
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
        "git",
        "clone",
        "deploy",
        "deployment",
        "publish",
        "release",
        "shell",
        "powershell",
        "cmd",
        "command",
        "exec",
        "execute",
        "subprocess",
        "process",
        "service",
        "sudo",
        "chmod",
        "chown",
        "wsl",
        "docker",
        "ssh",
        "scp",
        "curl",
        "wget",
        "ftp",
        "install",
        "uninstall",
        "download",
        "upload",
        "draft",
        "webhook",
        "oauth",
        "registry",
        "firewall",
        "defender",
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
)
