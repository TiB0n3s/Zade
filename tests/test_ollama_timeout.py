"""The Ollama read timeout: inference endpoints get the long, configurable
budget (covers a cold model load); probes fail fast on a short fixed timeout.
"""
from __future__ import annotations

import io
import json

from cofounder_kernel.config import OllamaConfig
from cofounder_kernel.ollama import OllamaClient
from cofounder_kernel import ollama as ollama_mod


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _client(**overrides) -> OllamaClient:
    cfg = OllamaConfig(base_url="http://127.0.0.1:11434", provider_policy="local_only", **overrides)
    return OllamaClient(cfg)


def _capture_timeouts(monkeypatch):
    seen: list[tuple[str, float]] = []

    def fake_urlopen(request, timeout=0):
        seen.append((request.full_url, timeout))
        if request.full_url.endswith("/api/generate"):
            payload = {"response": "hi"}
        elif request.full_url.endswith("/api/embed"):
            payload = {"embeddings": [[0.1, 0.2]]}
        else:
            payload = {"version": "test", "models": []}
        return _FakeResp(json.dumps(payload).encode())

    monkeypatch.setattr(ollama_mod.urllib.request, "urlopen", fake_urlopen)
    return seen


def test_inference_uses_long_timeout_probes_use_short(monkeypatch) -> None:
    seen = _capture_timeouts(monkeypatch)
    client = _client(request_timeout_seconds=600.0)

    client.health()  # probe -> /api/version
    client.generate(prompt="hello")  # inference
    client.embed(text="x")  # inference

    by_path = {url.rsplit("/", 1)[-1]: t for url, t in seen}
    assert by_path["version"] == 15.0
    assert by_path["generate"] == 600.0
    assert by_path["embed"] == 600.0


def test_timeout_is_configurable(monkeypatch) -> None:
    seen = _capture_timeouts(monkeypatch)
    client = _client(request_timeout_seconds=300.0)

    client.generate(prompt="hello")

    generate_timeout = next(t for url, t in seen if url.endswith("/api/generate"))
    assert generate_timeout == 300.0
