"""Budget-authorized Anthropic streaming adapter for the confined coding loop."""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable, Mapping, Sequence

from .build_budget import BuildBudgetExceeded, BuildBudgetService, ProviderUsage
from .model_client import CodingModelError
from .ollama import GenerateResult


class BuildLeaseRequired(CodingModelError):
    pass


class BuildEgressRequired(CodingModelError):
    pass


class AnthropicBuildModelClient:
    """One-session adapter; it cannot authorize or enlarge its own lease."""

    def __init__(
        self,
        *,
        session_id: int,
        budget: BuildBudgetService,
        sdk_client: Any,
        authorize_egress: Callable[[int, dict[str, Any]], bool],
        provider_overhead_tokens: int = 1024,
        cache_ttl: str = "1h",
    ):
        if provider_overhead_tokens < 0:
            raise ValueError("provider_overhead_tokens cannot be negative")
        if cache_ttl not in {"5m", "1h"}:
            raise ValueError("cache_ttl must be '5m' or '1h'")
        self.session_id = session_id
        self.budget = budget
        self.sdk_client = sdk_client
        self.authorize_egress = authorize_egress
        self.provider_overhead_tokens = provider_overhead_tokens
        self.cache_ttl = cache_ttl

    def provider_info(self) -> dict[str, Any]:
        lease = self.budget.store.get_active_lease(
            self.session_id, provider="anthropic"
        )
        return {
            "provider": "anthropic",
            "model": lease.model if lease else "",
            "verified_local": False,
            "cloud_authorized": bool(
                lease and lease.state in {"active", "warning"}
            ),
            "lease_id": lease.id if lease else None,
            "fallback_attempted": False,
        }

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
    ) -> GenerateResult:
        del think, temperature
        if format is not None:
            raise CodingModelError("Anthropic build turns do not accept Ollama format schemas")
        if num_predict <= 0:
            raise ValueError("num_predict must be positive")
        try:
            lease = self.budget.preflight(self.session_id)
        except BuildBudgetExceeded as exc:
            raise BuildLeaseRequired(str(exc)) from exc
        selected_model = (model or lease.model).strip()
        if selected_model != lease.model:
            raise BuildLeaseRequired(
                f"Requested model {selected_model!r} does not match lease model {lease.model!r}"
            )

        system, converted_messages = _convert_messages(messages, cache_ttl=self.cache_ttl)
        converted_tools = _convert_tools(tools or (), cache_ttl=self.cache_ttl)
        request_id = f"build-{self.session_id}-{uuid.uuid4().hex}"
        egress_summary = {
            "session_id": self.session_id,
            "lease_id": lease.id,
            "usage_request_id": request_id,
            "provider": lease.provider,
            "model": lease.model,
            "message_count": len(converted_messages),
            "tool_names": [tool["name"] for tool in converted_tools],
        }
        try:
            authorized = bool(self.authorize_egress(self.session_id, egress_summary))
        except Exception as exc:  # noqa: BLE001 - authorization adapters vary
            raise BuildEgressRequired(f"Build egress authorization failed: {exc}") from exc
        if not authorized:
            raise BuildEgressRequired("Build source egress was not authorized")

        count_request: dict[str, Any] = {
            "model": selected_model,
            "messages": converted_messages,
        }
        if system:
            count_request["system"] = system
        if converted_tools:
            count_request["tools"] = converted_tools
        input_upper = self._input_upper(count_request)
        cache_mode = "write_1h" if self.cache_ttl == "1h" and (
            system or converted_tools
        ) else "write_5m" if system or converted_tools else "none"
        try:
            reservation = self.budget.reserve(
                session_id=self.session_id,
                request_id=request_id,
                input_upper=input_upper,
                max_output=num_predict,
                cache_mode=cache_mode,
            )
        except BuildBudgetExceeded as exc:
            raise CodingModelError(f"Anthropic build budget refused the request: {exc}") from exc

        stream_request = dict(count_request)
        stream_request["max_tokens"] = num_predict
        try:
            stream_manager = self.sdk_client.messages.stream(**stream_request)
        except Exception as exc:  # noqa: BLE001 - no request context was entered
            self.budget.release_pre_send(reservation.id)
            raise CodingModelError(f"Anthropic stream was not started: {exc}") from exc

        try:
            with stream_manager as stream:
                final_message = stream.get_final_message()
        except Exception as exc:  # noqa: BLE001 - post-send state may be ambiguous
            self.budget.mark_uncertain(reservation.id, str(exc))
            raise CodingModelError(f"Anthropic stream failed after send: {exc}") from exc

        usage = _provider_usage(final_message)
        try:
            self.budget.settle(reservation.id, usage)
        except Exception as exc:  # noqa: BLE001 - preserve the unsettled reservation
            self.budget.mark_uncertain(
                reservation.id, f"response received but usage settlement failed: {exc}"
            )
            raise CodingModelError(f"Anthropic usage settlement failed: {exc}") from exc
        return _generate_result(final_message, selected_model)

    def _input_upper(self, count_request: dict[str, Any]) -> int:
        try:
            counted = self.sdk_client.messages.count_tokens(**count_request)
            tokens = int(_value(counted, "input_tokens", 0))
            if tokens <= 0:
                raise ValueError("count_tokens returned no input count")
        except Exception:  # noqa: BLE001 - conservative local fallback is intentional
            serialized = json.dumps(
                count_request, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("utf-8")
            tokens = len(serialized)
        return max(1, tokens + self.provider_overhead_tokens)


def _convert_tools(
    tools: Sequence[Mapping[str, Any]], *, cache_ttl: str
) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for tool in tools:
        function = tool.get("function") if isinstance(tool, Mapping) else None
        if not isinstance(function, Mapping):
            continue
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        parameters = function.get("parameters")
        converted.append(
            {
                "name": name,
                "description": str(function.get("description") or ""),
                "input_schema": dict(parameters)
                if isinstance(parameters, Mapping)
                else {"type": "object", "properties": {}},
            }
        )
    if converted:
        converted[-1]["cache_control"] = {"type": "ephemeral", "ttl": cache_ttl}
    return converted


def _convert_messages(
    messages: Sequence[Any], *, cache_ttl: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    system: list[dict[str, Any]] = []
    converted: list[dict[str, Any]] = []
    pending_tools: list[tuple[str, str]] = []

    def append(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if converted and converted[-1]["role"] == role:
            converted[-1]["content"].extend(blocks)
        else:
            converted.append({"role": role, "content": blocks})

    for message in messages:
        if not isinstance(message, Mapping):
            continue
        role = str(message.get("role") or "").lower()
        content = str(message.get("content") or "")
        if role == "system":
            if content:
                system.append({"type": "text", "text": content})
            continue
        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            if content:
                blocks.append({"type": "text", "text": content})
            calls = message.get("tool_calls")
            if isinstance(calls, Sequence) and not isinstance(calls, (str, bytes)):
                for index, call in enumerate(calls):
                    if not isinstance(call, Mapping):
                        continue
                    function = call.get("function")
                    if not isinstance(function, Mapping):
                        continue
                    name = str(function.get("name") or "").strip()
                    if not name:
                        continue
                    call_id = str(call.get("id") or f"toolu_local_{len(pending_tools) + index}")
                    arguments = function.get("arguments")
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except json.JSONDecodeError:
                            arguments = {"input": arguments}
                    if not isinstance(arguments, Mapping):
                        arguments = {}
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": call_id,
                            "name": name,
                            "input": dict(arguments),
                        }
                    )
                    pending_tools.append((call_id, name))
            append("assistant", blocks)
            continue
        if role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            tool_name = str(message.get("tool_name") or "")
            if not call_id:
                match = next(
                    (
                        (candidate_id, candidate_name)
                        for candidate_id, candidate_name in pending_tools
                        if not tool_name or candidate_name == tool_name
                    ),
                    None,
                )
                if match:
                    call_id = match[0]
            if call_id:
                pending_tools = [item for item in pending_tools if item[0] != call_id]
                append(
                    "user",
                    [
                        {
                            "type": "tool_result",
                            "tool_use_id": call_id,
                            "content": content,
                        }
                    ],
                )
            elif content:
                append("user", [{"type": "text", "text": content}])
            continue
        if role == "user" and content:
            append("user", [{"type": "text", "text": content}])

    if system:
        system[-1]["cache_control"] = {"type": "ephemeral", "ttl": cache_ttl}
    if not converted:
        raise CodingModelError("Anthropic build request has no user or assistant messages")
    return system, converted


def _provider_usage(message: Any) -> ProviderUsage | None:
    usage = _value(message, "usage", None)
    if usage is None:
        return None
    cache_creation = _value(usage, "cache_creation", None)
    total_creation = int(_value(usage, "cache_creation_input_tokens", 0) or 0)
    write_5m = int(_value(cache_creation, "ephemeral_5m_input_tokens", 0) or 0)
    write_1h = int(_value(cache_creation, "ephemeral_1h_input_tokens", 0) or 0)
    if total_creation and write_5m + write_1h == 0:
        write_1h = total_creation
    return ProviderUsage(
        input_tokens=int(_value(usage, "input_tokens", 0) or 0),
        cache_write_5m_tokens=write_5m,
        cache_write_1h_tokens=write_1h,
        cache_read_tokens=int(_value(usage, "cache_read_input_tokens", 0) or 0),
        output_tokens=int(_value(usage, "output_tokens", 0) or 0),
    )


def _generate_result(message: Any, selected_model: str) -> GenerateResult:
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    content = _value(message, "content", [])
    for block in content if isinstance(content, Sequence) else []:
        block_type = str(_value(block, "type", ""))
        if block_type == "text":
            text_parts.append(str(_value(block, "text", "")))
        elif block_type == "tool_use":
            tool_calls.append(
                {
                    "id": str(_value(block, "id", "")),
                    "type": "function",
                    "function": {
                        "name": str(_value(block, "name", "")),
                        "arguments": _value(block, "input", {}) or {},
                    },
                }
            )
    text = "".join(text_parts)
    raw_message: dict[str, Any] = {"role": "assistant", "content": text}
    if tool_calls:
        raw_message["tool_calls"] = tool_calls
    return GenerateResult(
        response=text,
        model=str(_value(message, "model", selected_model) or selected_model),
        raw={"message": raw_message},
    )


def _value(value: Any, name: str, default: Any) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)
