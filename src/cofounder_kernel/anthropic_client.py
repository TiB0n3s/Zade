"""Checked, lazy Anthropic SDK construction and strategic review client."""

from __future__ import annotations

import os
import urllib.parse
from typing import Any, Callable, Mapping, Sequence

from . import netguard
from .config import AnthropicConfig


_ANTHROPIC_HOST = "api.anthropic.com"


class AnthropicError(RuntimeError):
    pass


class AnthropicNotConfigured(AnthropicError):
    """Anthropic is disabled, missing its SDK, or missing its API key."""


class AnthropicPolicyError(AnthropicError):
    """Cloud inference was refused before the SDK could send bytes."""


def create_sdk_client(
    config: AnthropicConfig,
    *,
    provider_policy: str,
    sdk_factory: Callable[..., Any] | None = None,
) -> Any:
    """Construct a no-retry SDK client after transport policy validation."""
    policy = (provider_policy or "local_only").strip().lower()
    if not config.enabled:
        raise AnthropicNotConfigured("Anthropic is disabled ([anthropic] enabled = false).")
    if policy == "local_only":
        raise AnthropicPolicyError(
            "provider_policy is local_only: cloud inference is refused at the transport. "
            "No request was sent."
        )
    parsed = urllib.parse.urlparse(config.base_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() != "https" or host != _ANTHROPIC_HOST:
        raise AnthropicPolicyError(
            f"Refused Anthropic endpoint {config.base_url!r}; expected https://{_ANTHROPIC_HOST}. "
            "No request was sent."
        )
    try:
        netguard.assert_allowed(
            config.base_url,
            require_https=True,
            allowed_hosts=frozenset({_ANTHROPIC_HOST}),
        )
    except netguard.EgressError as exc:
        raise AnthropicPolicyError(str(exc)) from exc
    api_key = os.environ.get(config.api_key_env, "").strip()
    if not api_key:
        raise AnthropicNotConfigured(
            f"Anthropic API key env var is not set: {config.api_key_env}"
        )
    if sdk_factory is None:
        try:
            from anthropic import Anthropic as sdk_factory
        except ImportError as exc:
            raise AnthropicNotConfigured(
                'Anthropic SDK is not installed; install the "cloud" project extra.'
            ) from exc
    return sdk_factory(
        api_key=api_key,
        base_url=f"https://{_ANTHROPIC_HOST}",
        timeout=config.timeout_seconds,
        max_retries=0,
    )


class AnthropicClient:
    def __init__(
        self,
        config: AnthropicConfig,
        *,
        provider_policy: str = "local_only",
        sdk_factory: Callable[..., Any] | None = None,
    ):
        self.config = config
        self.provider_policy = (provider_policy or "local_only").strip().lower()
        self._sdk_factory = sdk_factory
        self._sdk_client: Any | None = None

    def available(self) -> bool:
        return bool(self.config.enabled)

    def provider_info(self) -> dict[str, object]:
        parsed = urllib.parse.urlparse(self.config.base_url)
        return {
            "provider": "anthropic",
            "enabled": bool(self.config.enabled),
            "model": self.config.model,
            "endpoint_host": (parsed.hostname or "").lower(),
            "provider_policy": self.provider_policy,
            "key_present": bool(os.environ.get(self.config.api_key_env)),
            "sdk_retries": 0,
        }

    def sdk_client(self) -> Any:
        if self._sdk_client is None:
            self._sdk_client = create_sdk_client(
                self.config,
                provider_policy=self.provider_policy,
                sdk_factory=self._sdk_factory,
            )
        return self._sdk_client

    def review(
        self,
        *,
        prompt: str,
        system: str = "",
        max_tokens: int | None = None,
    ) -> str:
        """Send one already-authorized strategic review request."""
        request: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": max_tokens or self.config.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            request["system"] = system
        try:
            response = self.sdk_client().messages.create(**request)
        except (AnthropicNotConfigured, AnthropicPolicyError):
            raise
        except Exception as exc:  # noqa: BLE001 - normalize SDK transport errors
            raise AnthropicError(f"Anthropic request failed: {exc}") from exc
        return _extract_text(response)


def _extract_text(raw: Any) -> str:
    content = _value(raw, "content", None)
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        parts = [
            str(_value(block, "text", ""))
            for block in content
            if str(_value(block, "type", "")) == "text"
        ]
        text = "".join(parts).strip()
        if text:
            return text
    raise AnthropicError("Anthropic response had no text content.")


def _value(value: Any, name: str, default: Any) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)
