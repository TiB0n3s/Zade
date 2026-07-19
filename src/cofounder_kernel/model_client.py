"""Provider-neutral model interface for the confined coding loop."""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence

from .ollama import GenerateResult


class CodingModelError(RuntimeError):
    """A coding model request failed without authorizing provider fallback."""


class CodingModelClient(Protocol):
    def chat(
        self,
        *,
        messages: Sequence[Any],
        model: str | None = None,
        think: bool | None = None,
        temperature: float | None = None,
        num_predict: int = 512,
        tools: Sequence[Mapping[str, Any]] | None = None,
        format: str | Mapping[str, Any] | None = None,
    ) -> GenerateResult: ...

    def provider_info(self) -> dict[str, Any]: ...
