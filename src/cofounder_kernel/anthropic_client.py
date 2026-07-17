"""Anthropic Messages API client — the first non-local inference client.

Deliberately minimal and dependency-free (urllib, like the voice cloud client):
one call, one governed egress host, key from the environment. It is NOT a general
cloud escape hatch — nothing here decides *whether* to call Anthropic. That is the
egress gate's job (a per-request ``founder_brief → anthropic`` grant); this client
only performs the send once a caller has been authorized, and refuses at the
transport under ``provider_policy = local_only`` as defense in depth.

Every request funnels through ``netguard`` (https + a one-host allowlist) so a
tampered base_url cannot redirect the payload elsewhere. The API key is read from
the configured env var and never stored in config or the database.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request

from . import netguard
from .config import AnthropicConfig


class AnthropicError(RuntimeError):
    pass


class AnthropicNotConfigured(AnthropicError):
    """Anthropic is disabled, or its API key/env is not set."""


class AnthropicPolicyError(AnthropicError):
    """A cloud inference call was refused by provider policy at the transport,
    before any bytes left the process — never a silent send."""


def _allowed_host(base_url: str) -> frozenset[str]:
    host = (urllib.parse.urlparse(base_url).hostname or "").lower()
    return frozenset({host}) if host else frozenset()


class AnthropicClient:
    def __init__(self, config: AnthropicConfig, *, provider_policy: str = "local_only"):
        self.config = config
        self.provider_policy = (provider_policy or "local_only").strip().lower()

    def available(self) -> bool:
        return bool(self.config.enabled)

    def provider_info(self) -> dict[str, object]:
        """Redacted descriptor for telemetry — no prompt, no key."""
        parsed = urllib.parse.urlparse(self.config.base_url)
        import os

        return {
            "provider": "anthropic",
            "enabled": bool(self.config.enabled),
            "model": self.config.model,
            "endpoint_host": (parsed.hostname or "").lower(),
            "provider_policy": self.provider_policy,
            "key_present": bool(os.environ.get(self.config.api_key_env)),
        }

    def review(self, *, prompt: str, system: str = "", max_tokens: int | None = None) -> str:
        """Send one message and return the assistant's text. The caller MUST have
        already cleared the egress gate — this method assumes authorization and
        only re-checks the transport-level policy invariant."""
        if not self.config.enabled:
            raise AnthropicNotConfigured("Anthropic is disabled ([anthropic] enabled = false).")
        if self.provider_policy == "local_only":
            raise AnthropicPolicyError(
                "provider_policy is local_only: cloud inference is refused at the transport. "
                "No request was sent."
            )
        import os

        api_key = os.environ.get(self.config.api_key_env, "").strip()
        if not api_key:
            raise AnthropicNotConfigured(
                f"Anthropic API key env var is not set: {self.config.api_key_env}"
            )
        body: dict[str, object] = {
            "model": self.config.model,
            "max_tokens": max_tokens or self.config.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            body["system"] = system
        request = urllib.request.Request(
            self.config.base_url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "x-api-key": api_key,
                "anthropic-version": self.config.anthropic_version,
                "content-type": "application/json",
            },
            method="POST",
        )
        raw = self._http_call(request)
        return _extract_text(raw)

    def _http_call(self, request: urllib.request.Request) -> dict[str, object]:
        netguard.assert_allowed(
            request.full_url, require_https=True, allowed_hosts=_allowed_host(self.config.base_url)
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:  # noqa: S310 - allowlisted https endpoint
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                detail = ""
            raise AnthropicError(f"Anthropic request failed (HTTP {exc.code}): {detail}") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise AnthropicError(f"Anthropic request failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise AnthropicError("Anthropic returned invalid JSON.") from exc


def _extract_text(raw: dict[str, object]) -> str:
    """Pull the text out of a Messages API response, tolerant of shape drift."""
    content = raw.get("content")
    if isinstance(content, list):
        parts = [
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        text = "".join(parts).strip()
        if text:
            return text
    raise AnthropicError(f"Anthropic response had no text content: {json.dumps(raw)[:200]}")
