"""Optional, lease-bounded OpenAI Responses API reviewer."""

from __future__ import annotations

import importlib.util
import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import urlparse
from uuid import uuid4

from .build_budget import BuildBudgetService, ProviderUsage
from .config import OpenAIReviewConfig


class OpenAIReviewUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class OpenAIReviewEgressRequest:
    session_id: int
    provider: str
    purpose: str
    byte_estimate: int
    request_id: str


@dataclass(frozen=True)
class ReviewResult:
    ok: bool
    summary: str
    findings: tuple[str, ...]
    recommendation: str
    raw_text: str
    model: str
    request_id: str
    provider_request_id: str
    usage: ProviderUsage


class OpenAIReviewClient:
    def __init__(
        self,
        *,
        config: OpenAIReviewConfig,
        budget: BuildBudgetService,
        authorize_egress: Callable[[OpenAIReviewEgressRequest], bool],
        environ: Mapping[str, str] | None = None,
        client_factory: Callable[..., Any] | None = None,
        sdk_available: Callable[[], bool] | None = None,
    ):
        self.config = config
        self.budget = budget
        self.authorize_egress = authorize_egress
        self.environ = os.environ if environ is None else environ
        self.client_factory = client_factory or _default_client_factory
        self.sdk_available = sdk_available or _openai_sdk_available

    def status(self) -> dict[str, Any]:
        blockers: list[str] = []
        if not self.config.enabled:
            blockers.append("openai_review_disabled")
        if not str(self.environ.get(self.config.api_key_env) or "").strip():
            blockers.append(f"missing_{self.config.api_key_env}")
        if not self.sdk_available():
            blockers.append("openai_sdk_unavailable")
        if not self.config.pricing.is_current():
            blockers.append("openai_pricing_review_required")
        try:
            self._validate_host()
        except OpenAIReviewUnavailable as exc:
            blockers.append(str(exc))
        return {
            "ready": not blockers,
            "enabled": self.config.enabled,
            "model": self.config.model,
            "base_url": self.config.base_url,
            "store": False,
            "blockers": blockers,
        }

    def review(
        self,
        *,
        session_id: int,
        prompt: str,
        context: str,
        request_id: str = "",
    ) -> ReviewResult:
        self._require_ready()
        lease = self.budget.preflight(session_id)
        clean_prompt = prompt.strip()
        if not clean_prompt:
            raise ValueError("OpenAI review prompt is required")
        bounded_context = context[:48_000]
        input_text = f"{clean_prompt}\n\nReview context:\n{bounded_context}"
        normalized_request_id = request_id.strip() or f"openai-review-{uuid4().hex}"
        egress = OpenAIReviewEgressRequest(
            session_id=session_id,
            provider="openai",
            purpose="independent build review",
            byte_estimate=len(input_text.encode("utf-8")),
            request_id=normalized_request_id,
        )
        if not self.authorize_egress(egress):
            raise PermissionError("OpenAI review egress was not authorized")
        key = str(self.environ.get(self.config.api_key_env) or "").strip()
        client = self.client_factory(
            api_key=key,
            base_url=self.config.base_url,
            timeout=self.config.timeout_seconds,
        )
        input_upper = len(input_text.encode("utf-8")) + 1024
        reservation = self.budget.reserve(
            session_id=session_id,
            request_id=normalized_request_id,
            input_upper=input_upper,
            max_output=self.config.max_output_tokens,
            cache_mode="none",
        )
        try:
            response = client.responses.create(
                model=lease.model,
                instructions=(
                    "Act as an independent software release reviewer. Return JSON with "
                    "summary, findings (array of strings), and recommendation. Do not "
                    "execute tools, modify code, or claim evidence not present in context."
                ),
                input=input_text,
                max_output_tokens=self.config.max_output_tokens,
                reasoning={"effort": self.config.reasoning_effort},
                store=False,
            )
        except Exception as exc:
            self.budget.mark_uncertain(reservation.id, str(exc) or type(exc).__name__)
            raise
        raw_usage = getattr(response, "usage", None)
        if raw_usage is None:
            self.budget.settle(reservation.id, None)
            usage = ProviderUsage(
                input_tokens=reservation.input_upper_tokens,
                output_tokens=reservation.max_output_tokens,
            )
        else:
            usage = _provider_usage(raw_usage)
            self.budget.settle(reservation.id, usage)
        raw_text = str(getattr(response, "output_text", "") or "")
        parsed = _parse_review(raw_text)
        return ReviewResult(
            ok=bool(raw_text.strip()),
            summary=parsed["summary"],
            findings=tuple(parsed["findings"]),
            recommendation=parsed["recommendation"],
            raw_text=raw_text,
            model=lease.model,
            request_id=normalized_request_id,
            provider_request_id=str(getattr(response, "_request_id", "") or ""),
            usage=usage,
        )

    def _require_ready(self) -> None:
        self._validate_host()
        if not self.config.enabled:
            raise OpenAIReviewUnavailable("OpenAI review is disabled")
        if not str(self.environ.get(self.config.api_key_env) or "").strip():
            raise OpenAIReviewUnavailable(
                f"OpenAI review requires {self.config.api_key_env}"
            )
        if not self.sdk_available():
            raise OpenAIReviewUnavailable(
                "OpenAI review requires the optional OpenAI Python SDK"
            )
        if not self.config.pricing.is_current():
            raise OpenAIReviewUnavailable("OpenAI review pricing requires founder review")

    def _validate_host(self) -> None:
        parsed = urlparse(self.config.base_url)
        if parsed.scheme != "https" or parsed.hostname != "api.openai.com":
            raise OpenAIReviewUnavailable(
                "OpenAI review base_url must use https://api.openai.com"
            )


def _default_client_factory(**kwargs: Any) -> Any:
    from openai import OpenAI

    return OpenAI(**kwargs)


def _openai_sdk_available() -> bool:
    return importlib.util.find_spec("openai") is not None


def _provider_usage(raw: Any) -> ProviderUsage:
    if raw is None:
        return ProviderUsage()
    input_tokens = int(getattr(raw, "input_tokens", 0) or 0)
    output_tokens = int(getattr(raw, "output_tokens", 0) or 0)
    details = getattr(raw, "input_tokens_details", None)
    cached_tokens = int(getattr(details, "cached_tokens", 0) or 0)
    cached_tokens = max(0, min(cached_tokens, input_tokens))
    return ProviderUsage(
        input_tokens=input_tokens - cached_tokens,
        cache_read_tokens=cached_tokens,
        output_tokens=output_tokens,
    )


def _parse_review(text: str) -> dict[str, Any]:
    fallback = {
        "summary": text.strip()[:2000],
        "findings": [],
        "recommendation": "review",
    }
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return fallback
    if not isinstance(payload, dict):
        return fallback
    findings = payload.get("findings")
    return {
        "summary": str(payload.get("summary") or "")[:2000],
        "findings": [
            str(item)[:2000] for item in findings if str(item).strip()
        ]
        if isinstance(findings, list)
        else [],
        "recommendation": str(payload.get("recommendation") or "review")[:200],
    }
