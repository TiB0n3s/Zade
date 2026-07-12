from __future__ import annotations

import http.client
import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from . import netguard
from .config import OllamaConfig


class OllamaError(RuntimeError):
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
        body = {
            "model": selected_model,
            "prompt": prompt,
            "stream": False,
            "think": self.config.think if think is None else think,
            "options": {
                "temperature": self.config.temperature if temperature is None else temperature,
                "num_predict": num_predict,
            },
        }
        raw = self._post_json("/api/generate", body)
        return GenerateResult(response=raw.get("response", ""), model=selected_model, raw=raw)

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
