from __future__ import annotations

import http.client
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from . import netguard
from .config import OllamaConfig


class OllamaError(RuntimeError):
    pass


class OllamaThinkingUnsupported(OllamaError):
    pass


class ProviderPolicyError(OllamaError):
    """A model request violated the provider policy (local-first enforcement).

    Raised at the transport boundary, before any bytes leave the process, so a
    misconfigured endpoint or cloud-tagged model is a clear local failure —
    never a silent cloud call and never a fallback.
    """


# Hosts that are cloud model providers. This client speaks to a local Ollama
# server; these are never legitimate targets for it, under ANY policy. The list
# is a backstop — under local_only every non-loopback host is refused anyway.
_BLOCKED_MODEL_PROVIDER_HOSTS = frozenset(
    {
        "api.anthropic.com",
        "api.openai.com",
        "ollama.com",
        "www.ollama.com",
        "api.ollama.com",
    }
)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def is_cloud_model(name: str) -> bool:
    """True when the model identifier denotes an Ollama Cloud variant.

    Cloud models are published with a ``-cloud`` suffix or a ``cloud`` tag
    (e.g. ``qwen3-coder:480b-cloud``, ``deepseek-v3.1:cloud``). Name-based
    detection is the strongest signal /api/tags exposes; /api/show inspection
    in the inventory service refines it with the ``remote`` marker.
    """
    lowered = (name or "").strip().lower()
    if not lowered:
        return False
    tag = lowered.split(":", 1)[1] if ":" in lowered else ""
    return lowered.endswith("-cloud") or tag == "cloud" or tag.endswith("-cloud")


@dataclass(frozen=True)
class GenerateResult:
    response: str
    model: str
    raw: dict[str, Any]


class OllamaClient:
    def __init__(self, config: OllamaConfig):
        self.config = config

    # ---- provider policy -------------------------------------------------
    def _policy(self) -> str:
        return str(getattr(self.config, "provider_policy", "local_only") or "local_only")

    def endpoint_host(self) -> str:
        return (urllib.parse.urlparse(self.config.base_url).hostname or "").lower()

    def verified_local(self) -> bool:
        return self.endpoint_host() in _LOOPBACK_HOSTS

    def provider_info(self) -> dict[str, Any]:
        """Redacted provider descriptor for telemetry — no prompts, no secrets."""
        parsed = urllib.parse.urlparse(self.config.base_url)
        return {
            "provider": "ollama",
            "endpoint_scheme": (parsed.scheme or "").lower(),
            "endpoint_host": (parsed.hostname or "").lower(),
            "verified_local": self.verified_local(),
            "provider_policy": self._policy(),
            "cloud_authorized": bool(getattr(self.config, "allow_cloud_inference", False)),
            "fallback_attempted": False,
        }

    def _assert_endpoint_allowed(self, url: str) -> None:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        if host in _BLOCKED_MODEL_PROVIDER_HOSTS:
            raise ProviderPolicyError(
                f"Refused model endpoint {host!r}: cloud model providers are never a valid "
                "target for the local Ollama client. No request was sent."
            )
        if host in _LOOPBACK_HOSTS:
            return
        policy = self._policy()
        if policy == "local_only":
            raise ProviderPolicyError(
                f"Refused non-loopback model endpoint {host!r}: provider_policy is local_only. "
                "Point [ollama] base_url at 127.0.0.1/localhost/::1, or deliberately change "
                "provider_policy and allow_remote_ollama. No request was sent."
            )
        if not bool(getattr(self.config, "allow_remote_ollama", False)):
            raise ProviderPolicyError(
                f"Refused remote Ollama host {host!r}: allow_remote_ollama is false. "
                "No request was sent."
            )

    def _assert_model_allowed(self, model: str) -> None:
        if not is_cloud_model(model):
            return
        policy = self._policy()
        if policy == "local_only" or not bool(getattr(self.config, "allow_ollama_cloud", False)):
            raise ProviderPolicyError(
                f"Refused cloud model {model!r}: it executes on Ollama Cloud, not this machine "
                f"(provider_policy={policy}, allow_ollama_cloud="
                f"{bool(getattr(self.config, 'allow_ollama_cloud', False))}). "
                "Choose an installed local model. No request was sent."
            )

    # ---- API -------------------------------------------------------------
    def health(self) -> dict[str, Any]:
        return self._get_json("/api/version")

    def tags(self) -> dict[str, Any]:
        return self._get_json("/api/tags")

    def show(self, model: str) -> dict[str, Any]:
        return self._post_json("/api/show", {"model": model})

    def generate(
        self,
        *,
        prompt: str,
        model: str | None = None,
        think: bool | None = None,
        temperature: float | None = None,
        num_predict: int = 512,
    ) -> GenerateResult:
        selected_model = model or self.config.chat_model
        self._assert_model_allowed(selected_model)
        requested_think = self.config.think if think is None else think
        body = {
            "model": selected_model,
            "prompt": prompt,
            "stream": False,
            "think": requested_think,
            "options": {
                "temperature": self.config.temperature if temperature is None else temperature,
                "num_predict": num_predict,
            },
        }
        try:
            raw = self._post_json("/api/generate", body)
        except OllamaThinkingUnsupported as exc:
            if requested_think is not True:
                raise
            body["think"] = False
            raw = self._post_json("/api/generate", body)
            raw["_zade_effective_think"] = False
            raw["_zade_think_fallback"] = f"thinking_not_supported: {exc}"
        else:
            raw["_zade_effective_think"] = requested_think
        return GenerateResult(response=raw.get("response", ""), model=selected_model, raw=raw)

    def chat(
        self,
        *,
        messages: Sequence[Any],
        model: str | None = None,
        think: bool | None = None,
        temperature: float | None = None,
        num_predict: int = 512,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> GenerateResult:
        selected_model = model or self.config.chat_model
        self._assert_model_allowed(selected_model)
        requested_think = self.config.think if think is None else think
        body: dict[str, Any] = {
            "model": selected_model,
            "messages": [_chat_message_payload(message) for message in messages],
            "stream": False,
            "think": requested_think,
            "options": {
                "temperature": self.config.temperature if temperature is None else temperature,
                "num_predict": num_predict,
            },
        }
        if tools:
            body["tools"] = [dict(tool) for tool in tools]
        try:
            raw = self._post_json("/api/chat", body)
        except OllamaThinkingUnsupported as exc:
            if requested_think is not True:
                raise
            body["think"] = False
            raw = self._post_json("/api/chat", body)
            raw["_zade_effective_think"] = False
            raw["_zade_think_fallback"] = f"thinking_not_supported: {exc}"
        else:
            raw["_zade_effective_think"] = requested_think
        message = raw.get("message") or {}
        response = message.get("content", "") if isinstance(message, dict) else ""
        return GenerateResult(response=response, model=selected_model, raw=raw)

    def embed(self, *, text: str, model: str | None = None) -> list[float]:
        selected_model = model or self.config.embedding_model
        self._assert_model_allowed(selected_model)
        body = {"model": selected_model, "input": text}
        raw = self._post_json("/api/embed", body)
        embeddings = raw.get("embeddings") or []
        if not embeddings:
            return []
        return list(embeddings[0])

    def _get_json(self, path: str) -> dict[str, Any]:
        request = urllib.request.Request(f"{self.config.base_url}{path}", method="GET")
        return self._request_json(request)

    def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{self.config.base_url}{path}",
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        return self._request_json(request)

    def _request_json(self, request: urllib.request.Request) -> dict[str, Any]:
        # Provider policy first: under local_only only loopback is a valid model
        # endpoint, and known cloud provider hosts are refused under any policy.
        # This runs before any bytes leave the process.
        self._assert_endpoint_allowed(request.full_url)
        # Ollama is a local server, but funnel through the same egress policy so
        # every outbound call in the kernel is checked in exactly one place.
        try:
            netguard.assert_allowed(request.full_url, allow_private=True)
        except netguard.EgressError as exc:
            raise OllamaError(str(exc)) from exc
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:
                detail = ""
            if "does not support thinking" in detail.lower():
                raise OllamaThinkingUnsupported(detail or str(exc)) from exc
            message = f"Ollama request failed: HTTP Error {exc.code}: {exc.reason}"
            if detail:
                message = f"{message}: {detail}"
            raise OllamaError(message) from exc
        except urllib.error.URLError as exc:
            raise OllamaError(f"Ollama request failed: {exc}") from exc
        except (TimeoutError, socket.timeout) as exc:
            # A stalled read raises TimeoutError, which is NOT a URLError subclass,
            # so callers catching OllamaError would otherwise miss it.
            raise OllamaError("Ollama request timed out.") from exc
        except http.client.IncompleteRead as exc:
            raise OllamaError("Ollama returned a truncated response.") from exc
        except json.JSONDecodeError as exc:
            raise OllamaError("Ollama returned invalid JSON") from exc


def _chat_message_payload(message: Any) -> dict[str, Any]:
    if isinstance(message, Mapping):
        role = str(message.get("role", "")).strip()
        content = str(message.get("content", ""))
        payload = {key: value for key, value in message.items() if key in {"images", "tool_calls", "tool_name", "name"}}
    else:
        role = str(getattr(message, "role", "")).strip()
        content = str(getattr(message, "content", ""))
        payload = {}
    if role not in {"system", "user", "assistant", "tool"}:
        raise OllamaError(f"Unsupported chat message role: {role or '<empty>'}")
    payload.update({"role": role, "content": content})
    return payload
