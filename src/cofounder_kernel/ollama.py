from __future__ import annotations

import http.client
import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from . import netguard
from .config import OllamaConfig


class OllamaError(RuntimeError):
    pass


class OllamaThinkingUnsupported(OllamaError):
    pass


@dataclass(frozen=True)
class GenerateResult:
    response: str
    model: str
    raw: dict[str, Any]


class OllamaClient:
    def __init__(self, config: OllamaConfig):
        self.config = config

    def health(self) -> dict[str, Any]:
        return self._get_json("/api/version")

    def tags(self) -> dict[str, Any]:
        return self._get_json("/api/tags")

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
        body = {"model": model or self.config.embedding_model, "input": text}
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
        payload = {key: value for key, value in message.items() if key in {"images", "tool_calls"}}
    else:
        role = str(getattr(message, "role", "")).strip()
        content = str(getattr(message, "content", ""))
        payload = {}
    if role not in {"system", "user", "assistant", "tool"}:
        raise OllamaError(f"Unsupported chat message role: {role or '<empty>'}")
    payload.update({"role": role, "content": content})
    return payload
