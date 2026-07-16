"""Local model inventory and role resolution.

The provider policy says only verified local models may serve model-backed
roles; this service is how the kernel knows what "verified local" means on
this machine. It reads the installed set from the local Ollama server
(GET /api/tags), inspects each model (POST /api/show), and — because declared
capabilities are not reliable (qwen2.5-coder declares "tools" yet emits tool
calls as raw JSON text) — runs a REAL native tool-call probe before a model is
eligible to drive the coding agent.

Nothing here pulls or downloads models. An unusable configuration fails with a
precise error naming the installed candidates and the configuration key to set.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .config import KernelConfig
from .ollama import OllamaClient, OllamaError, is_cloud_model

# One shared probe: a trivial single-tool request. A model is "tool capable"
# only when it answers with a NATIVE tool_calls message, not prose or JSON text.
_PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a workspace file and return its contents.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Relative file path."}},
            "required": ["path"],
        },
    },
}
_PROBE_MESSAGE = "Use the read_file tool to read the file named config.py. Call the tool."

_PROBE_CACHE_TTL_SECONDS = 15 * 60


@dataclass
class ModelRecord:
    name: str
    family: str = ""
    parameter_size: str = ""
    quantization: str = ""
    capabilities: list[str] = field(default_factory=list)
    context_length: int | None = None
    remote: bool = False
    verified_local: bool = False
    tool_probe_ok: bool | None = None  # None = not probed yet
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "family": self.family,
            "parameter_size": self.parameter_size,
            "quantization": self.quantization,
            "capabilities": list(self.capabilities),
            "context_length": self.context_length,
            "remote": self.remote,
            "verified_local": self.verified_local,
            "tool_probe_ok": self.tool_probe_ok,
            "roles_eligible": self.roles_eligible(),
            "error": self.error,
        }

    def roles_eligible(self) -> list[str]:
        roles: list[str] = []
        caps = set(self.capabilities)
        if not self.verified_local:
            return roles
        if "embedding" in caps:
            roles.append("embedding")
        if "completion" in caps or not caps:
            roles += ["general", "reasoning", "coding"]
        if self.tool_probe_ok:
            roles.append("coding_agent")
        return roles


class ModelInventoryError(OllamaError):
    """A model-role resolution failed; the message names the fix."""


class ModelInventoryService:
    """Installed-model inventory + role resolution against the local Ollama."""

    def __init__(self, *, config: KernelConfig, ollama: OllamaClient):
        self.config = config
        self.ollama = ollama
        self._probe_cache: dict[str, tuple[float, bool]] = {}

    # ---- inventory --------------------------------------------------------
    def installed(self) -> list[str]:
        tags = self.ollama.tags()
        return [str(m.get("name") or m.get("model") or "") for m in tags.get("models", []) if m]

    def inspect(self, name: str) -> ModelRecord:
        record = ModelRecord(name=name)
        try:
            tags = self.ollama.tags()
        except OllamaError as exc:
            record.error = f"tags: {exc}"
            return record
        entry = next(
            (m for m in tags.get("models", []) if (m.get("name") or m.get("model")) == name),
            None,
        )
        if entry is None:
            record.error = "not installed"
            return record
        details = entry.get("details") or {}
        record.family = str(details.get("family") or "")
        record.parameter_size = str(details.get("parameter_size") or "")
        record.quantization = str(details.get("quantization_level") or "")
        # Ollama marks cloud models with a remote/remote_host field in newer
        # releases; the -cloud name suffix is the stable fallback signal.
        record.remote = bool(entry.get("remote") or entry.get("remote_host")) or is_cloud_model(name)
        try:
            show = self.ollama.show(name)
            record.capabilities = [str(c) for c in (show.get("capabilities") or [])]
            record.remote = record.remote or bool(show.get("remote") or show.get("remote_host"))
            model_info = show.get("model_info") or {}
            for key, value in model_info.items():
                if str(key).endswith(".context_length"):
                    record.context_length = int(value)
                    break
        except (OllamaError, TypeError, ValueError) as exc:
            record.error = f"show: {str(exc)[:200]}"
        record.verified_local = self.ollama.verified_local() and not record.remote
        return record

    def snapshot(self, *, probe: bool = False) -> list[dict[str, Any]]:
        records = []
        for name in self.installed():
            record = self.inspect(name)
            if probe and record.verified_local and "embedding" not in record.capabilities:
                record.tool_probe_ok = self.probe_tools(name)
            records.append(record.as_dict())
        return records

    # ---- capability probe --------------------------------------------------
    def probe_tools(self, name: str) -> bool:
        """Live probe: does the model return NATIVE tool_calls? Cached briefly."""
        cached = self._probe_cache.get(name)
        now = time.monotonic()
        if cached and now - cached[0] < _PROBE_CACHE_TTL_SECONDS:
            return cached[1]
        try:
            result = self.ollama.chat(
                messages=[{"role": "user", "content": _PROBE_MESSAGE}],
                model=name,
                think=False,
                temperature=0.0,
                num_predict=200,
                tools=[_PROBE_TOOL],
            )
            message = (result.raw or {}).get("message") or {}
            ok = bool(isinstance(message, dict) and message.get("tool_calls"))
        except OllamaError:
            ok = False
        self._probe_cache[name] = (now, ok)
        return ok

    # ---- role resolution ----------------------------------------------------
    def resolve_coding_agent_model(self) -> str:
        """Pick the coding-agent model: explicit config first, then configured
        role defaults that pass the native tool probe, else a precise failure.

        Never selects an arbitrary installed model, never pulls one, and never
        escalates to a cloud provider.
        """
        installed = set(self.installed())
        explicit = (getattr(self.config.ollama, "coding_agent_model", "") or "").strip()
        if explicit:
            if is_cloud_model(explicit):
                raise ModelInventoryError(
                    f"Configured coding_agent_model {explicit!r} is an Ollama Cloud variant, "
                    "which is forbidden under the local provider policy. Set [ollama] "
                    "coding_agent_model to an installed local model."
                )
            if explicit not in installed:
                raise ModelInventoryError(
                    f"Configured coding_agent_model {explicit!r} is not installed. "
                    f"Installed models: {sorted(installed)}. Set [ollama] coding_agent_model "
                    "to one of these (it must support native tool calls)."
                )
            if not self.probe_tools(explicit):
                raise ModelInventoryError(
                    f"Configured coding_agent_model {explicit!r} failed the native tool-call "
                    "probe (it does not emit tool_calls). Pick a tool-capable installed model "
                    f"for [ollama] coding_agent_model. Installed models: {sorted(installed)}."
                )
            return explicit
        candidates = [self.config.ollama.coding_model, self.config.ollama.chat_model]
        tried: list[str] = []
        for candidate in candidates:
            candidate = (candidate or "").strip()
            if not candidate or candidate in tried:
                continue
            tried.append(candidate)
            if candidate in installed and not is_cloud_model(candidate) and self.probe_tools(candidate):
                return candidate
        raise ModelInventoryError(
            "No configured local model passed the native tool-call probe for the coding agent "
            f"(tried: {tried}). Installed models: {sorted(installed)}. Set [ollama] "
            "coding_agent_model to an installed model that supports native tool calls."
        )

    # ---- status ---------------------------------------------------------------
    def ollama_cloud_disabled(self) -> bool | str:
        """Best-effort verdict on whether Ollama Cloud execution is disabled.

        True when OLLAMA_NO_CLOUD is set in this process's environment or no
        installed model is cloud-tagged AND policy forbids cloud models;
        "unknown" when we cannot verify the Ollama server's own environment.
        """
        import os

        if str(os.environ.get("OLLAMA_NO_CLOUD", "")).strip() in {"1", "true", "yes", "on"}:
            return True
        try:
            names = self.installed()
        except OllamaError:
            return "unknown"
        any_cloud_installed = any(is_cloud_model(name) for name in names)
        policy_forbids = (
            getattr(self.config.ollama, "provider_policy", "local_only") == "local_only"
            or not getattr(self.config.ollama, "allow_ollama_cloud", False)
        )
        if not any_cloud_installed and policy_forbids:
            # Nothing installed can execute remotely and the kernel would refuse
            # it anyway; still "unknown" at the server level, so report the
            # strongest honest claim.
            return True
        return "unknown"
