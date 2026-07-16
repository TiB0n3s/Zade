"""Model inventory: /api/tags + /api/show parsing, the live tool probe, and
role resolution that fails precisely instead of guessing or escalating."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cofounder_kernel.config import AppConfig, KernelConfig, OllamaConfig, PathConfig
from cofounder_kernel.inventory import ModelInventoryError, ModelInventoryService
from cofounder_kernel.ollama import GenerateResult, OllamaClient


def _config(tmp_path: Path, **ollama_kw) -> KernelConfig:
    return KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1", **ollama_kw),
    )


_TAGS = {
    "models": [
        {
            "name": "qwen3:14b",
            "details": {"family": "qwen3", "parameter_size": "14.8B", "quantization_level": "Q4_K_M"},
        },
        {
            "name": "qwen2.5-coder:14b",
            "details": {"family": "qwen2", "parameter_size": "14.8B", "quantization_level": "Q4_K_M"},
        },
        {
            "name": "nomic-embed-text:latest",
            "details": {"family": "nomic-bert", "parameter_size": "137M", "quantization_level": "F16"},
        },
        {
            "name": "giant:480b-cloud",
            "remote": True,
            "details": {"family": "qwen3"},
        },
    ]
}

_SHOW = {
    "qwen3:14b": {
        "capabilities": ["completion", "tools", "thinking"],
        "model_info": {"qwen3.context_length": 40960},
    },
    "qwen2.5-coder:14b": {
        "capabilities": ["completion", "tools", "insert"],
        "model_info": {"qwen2.context_length": 32768},
    },
    "nomic-embed-text:latest": {
        "capabilities": ["embedding"],
        "model_info": {"bert.context_length": 2048},
    },
    "giant:480b-cloud": {"capabilities": ["completion", "tools"], "remote": True},
}

# qwen3 answers the probe with NATIVE tool_calls; qwen2.5-coder answers with
# JSON-as-text (the real observed behavior); anything else answers prose.
_NATIVE_TOOL_MODELS = {"qwen3:14b"}


def _patch_fake_ollama(monkeypatch) -> None:
    def fake_get(self, path: str) -> dict[str, Any]:
        if path == "/api/tags":
            return _TAGS
        return {"version": "test"}

    def fake_post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if path == "/api/show":
            return dict(_SHOW.get(body.get("model", ""), {}))
        if path == "/api/chat":
            model = body.get("model", "")
            if model in _NATIVE_TOOL_MODELS and body.get("tools"):
                return {
                    "model": model,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {"function": {"name": "read_file", "arguments": {"path": "config.py"}}}
                        ],
                    },
                    "done": True,
                }
            return {
                "model": model,
                "message": {"role": "assistant", "content": '{"name": "read_file"}'},
                "done": True,
            }
        raise AssertionError(f"unexpected POST {path}")

    monkeypatch.setattr(OllamaClient, "_get_json", fake_get)
    monkeypatch.setattr(OllamaClient, "_post_json", fake_post)


# 6-7. tags/show parsing ---------------------------------------------------------

def test_tags_and_show_parsing(tmp_path: Path, monkeypatch) -> None:
    _patch_fake_ollama(monkeypatch)
    cfg = _config(tmp_path)
    svc = ModelInventoryService(config=cfg, ollama=OllamaClient(cfg.ollama))
    assert svc.installed() == [
        "qwen3:14b",
        "qwen2.5-coder:14b",
        "nomic-embed-text:latest",
        "giant:480b-cloud",
    ]
    record = svc.inspect("qwen3:14b")
    assert record.family == "qwen3"
    assert record.parameter_size == "14.8B"
    assert record.quantization == "Q4_K_M"
    assert record.capabilities == ["completion", "tools", "thinking"]
    assert record.context_length == 40960
    assert record.verified_local is True
    embed = svc.inspect("nomic-embed-text:latest")
    assert embed.roles_eligible() == ["embedding"]


def test_cloud_tagged_model_is_not_verified_local(tmp_path: Path, monkeypatch) -> None:
    _patch_fake_ollama(monkeypatch)
    cfg = _config(tmp_path)
    svc = ModelInventoryService(config=cfg, ollama=OllamaClient(cfg.ollama))
    record = svc.inspect("giant:480b-cloud")
    assert record.remote is True
    assert record.verified_local is False
    assert record.roles_eligible() == []


def test_probe_requires_native_tool_calls(tmp_path: Path, monkeypatch) -> None:
    _patch_fake_ollama(monkeypatch)
    cfg = _config(tmp_path)
    svc = ModelInventoryService(config=cfg, ollama=OllamaClient(cfg.ollama))
    assert svc.probe_tools("qwen3:14b") is True
    # Declares "tools" but answers with JSON text -> NOT capable.
    assert svc.probe_tools("qwen2.5-coder:14b") is False


# role resolution ------------------------------------------------------------------

def test_coding_agent_resolution_prefers_explicit_config(tmp_path: Path, monkeypatch) -> None:
    _patch_fake_ollama(monkeypatch)
    cfg = _config(tmp_path, coding_agent_model="qwen3:14b")
    svc = ModelInventoryService(config=cfg, ollama=OllamaClient(cfg.ollama))
    assert svc.resolve_coding_agent_model() == "qwen3:14b"


def test_coding_agent_resolution_falls_through_probe(tmp_path: Path, monkeypatch) -> None:
    """No explicit config: coding_model fails the probe, chat_model passes."""
    _patch_fake_ollama(monkeypatch)
    cfg = _config(tmp_path)  # coding_model=qwen2.5-coder:14b (probe fails), chat=qwen3:14b
    svc = ModelInventoryService(config=cfg, ollama=OllamaClient(cfg.ollama))
    assert svc.resolve_coding_agent_model() == "qwen3:14b"


# 8. unknown/failing models fail clearly ---------------------------------------------

def test_unknown_explicit_model_fails_listing_candidates(tmp_path: Path, monkeypatch) -> None:
    _patch_fake_ollama(monkeypatch)
    cfg = _config(tmp_path, coding_agent_model="mistral:7b")
    svc = ModelInventoryService(config=cfg, ollama=OllamaClient(cfg.ollama))
    with pytest.raises(ModelInventoryError) as excinfo:
        svc.resolve_coding_agent_model()
    message = str(excinfo.value)
    assert "not installed" in message
    assert "qwen3:14b" in message  # lists candidates
    assert "coding_agent_model" in message  # names the configuration key


def test_explicit_cloud_model_fails(tmp_path: Path, monkeypatch) -> None:
    _patch_fake_ollama(monkeypatch)
    cfg = _config(tmp_path, coding_agent_model="giant:480b-cloud")
    svc = ModelInventoryService(config=cfg, ollama=OllamaClient(cfg.ollama))
    with pytest.raises(ModelInventoryError, match="Cloud"):
        svc.resolve_coding_agent_model()


def test_no_capable_model_fails_without_cloud_escalation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        OllamaClient,
        "_get_json",
        lambda self, path: {"models": [{"name": "qwen2.5-coder:14b", "details": {}}]},
    )

    def fake_post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if path == "/api/show":
            return {"capabilities": ["completion", "tools"]}
        if path == "/api/chat":
            return {"model": body.get("model"), "message": {"role": "assistant", "content": "prose"}, "done": True}
        raise AssertionError(path)

    monkeypatch.setattr(OllamaClient, "_post_json", fake_post)
    cfg = _config(tmp_path)
    svc = ModelInventoryService(config=cfg, ollama=OllamaClient(cfg.ollama))
    with pytest.raises(ModelInventoryError) as excinfo:
        svc.resolve_coding_agent_model()
    message = str(excinfo.value)
    assert "tool-call probe" in message
    assert "qwen2.5-coder:14b" in message
    assert "anthropic" not in message.lower()
