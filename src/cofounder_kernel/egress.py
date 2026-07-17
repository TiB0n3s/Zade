"""Data-class egress gate — the missing authorization axis.

The kernel already guards outbound work along three orthogonal axes:

  * ``authority.AuthorityPolicy`` — was the *action* authorized (allow / approve /
    deny), from the action taxonomy.
  * ``netguard.assert_allowed``  — is the *network target* safe (scheme, https,
    private/SSRF, host allowlist).
  * ``ollama.OllamaClient``      — may this *model endpoint / model* run, under
    ``provider_policy`` (local_only / local_preferred / cloud_allowed).

None of them answers the question this module exists for:

    "This authorized action wants to send data of class X to vendor Y.
     Is *that specific egress* permitted for *this specific request*?"

``provider_policy = "local_preferred"`` already promises in its own config
docstring that "cloud still needs an explicit per-request authorization; never
an automatic fallback" — but no mechanism enforced that promise. This is it.

Design invariants
-----------------
1. **Local-only is inert-by-default.** Under ``provider_policy = "local_only"``
   every non-local destination is DENY, regardless of the matrix, standing
   grants, or any authorization token. The matrix only becomes live when the
   founder deliberately raises the policy. Landing this module changes no live
   behavior — nothing egresses that did not egress before.
2. **No silent fallback.** A local failure never becomes a cloud call. The gate
   only ever *permits* egress that a caller explicitly asked to perform to a
   named vendor; it never redirects or retries elsewhere.
3. **Authorization comes from the founder, never from payload.** A per-request
   grant is matched to the exact ``(request_id, data_class, vendor)`` and is
   single-purpose. Content observed through tools (web pages, emails, channel
   messages) can never author a grant — mirrors the instruction-source boundary.
4. **Fail closed.** Any unclassified data, unknown vendor, or missing matrix
   cell is DENY, not ALLOW.
5. **Redacted audit only.** The decision record carries class/vendor/verdict/
   purpose/request_id — never the payload, never a secret. Mirrors
   ``ollama.OllamaClient.provider_info``.

This module has no imports from call sites (call sites import it, not the other
way round). The voice lane is wired (see ``voice.VoiceService._assert_egress_allowed``);
research and future cloud lanes follow — see EGRESS-DESIGN.md §6.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Iterable, Mapping

if TYPE_CHECKING:
    from .config import KernelConfig


# ---------------------------------------------------------------------------
# Taxonomy: what is leaving, and where it is going.
# ---------------------------------------------------------------------------
class DataClass(StrEnum):
    """What kind of data a request would send off the local process, ascending
    in sensitivity. The classifier lives at the call site — the caller declares
    the class; the gate never inspects the payload to guess it."""

    PUBLIC_DERIVED = "public_derived"   # a research query, a public URL — derived to be public
    OPERATIONAL = "operational"         # non-sensitive status text ("your run finished")
    REPLY_TEXT = "reply_text"           # Zade's generated reply (may embed answers/strategy)
    FOUNDER_AUDIO = "founder_audio"     # raw microphone audio
    SCREEN_PIXELS = "screen_pixels"     # screenshots / captured pixels
    SOURCE_CODE = "source_code"         # repo code, diffs, build briefs
    FOUNDER_BRIEF = "founder_brief"     # a founder-curated excerpt assembled for ONE review (never the authority policy)
    FOUNDER_STATE = "founder_state"     # RAW charter, strategy ledger, founder OS, memory, authority policy — never leaves
    CREDENTIALS = "credentials"         # secrets, API keys, tokens, passwords — never leaves


class VendorTier(StrEnum):
    """The columns of the matrix. Concrete vendors map onto a tier so the matrix
    stays small and reviewable; policy is expressed per tier, not per vendor."""

    LOCAL = "local"                 # loopback: Ollama, local files (always allowed)
    LAN = "lan"                     # founder's own private/LAN device (e.g. SMS gateway)
    PUBLIC_WEB = "public_web"       # outbound pull to an arbitrary public https host
    CLOUD_MODEL = "cloud_model"     # hosted model inference: OpenAI, Anthropic, Ollama Cloud
    CLOUD_SERVICE = "cloud_service" # fixed hosted service: Deepgram, ElevenLabs, cloud web-search
    CHANNEL = "channel"             # messaging gateway (OpenClaw): WhatsApp/Telegram/Slack/Discord


@dataclass(frozen=True)
class Vendor:
    """A concrete egress destination and the tier whose policy governs it."""

    key: str
    tier: VendorTier
    label: str


# The known destinations. LOCAL is the only tier the matrix never restricts.
VENDORS: dict[str, Vendor] = {
    "local_ollama": Vendor("local_ollama", VendorTier.LOCAL, "Local Ollama (loopback)"),
    "local_files": Vendor("local_files", VendorTier.LOCAL, "Local filesystem"),
    "sms_gateway": Vendor("sms_gateway", VendorTier.LAN, "Founder SMS gateway"),
    "public_web": Vendor("public_web", VendorTier.PUBLIC_WEB, "Public web (research fetch)"),
    "openai": Vendor("openai", VendorTier.CLOUD_MODEL, "OpenAI"),
    "anthropic": Vendor("anthropic", VendorTier.CLOUD_MODEL, "Anthropic"),
    "ollama_cloud": Vendor("ollama_cloud", VendorTier.CLOUD_MODEL, "Ollama Cloud"),
    "deepgram": Vendor("deepgram", VendorTier.CLOUD_SERVICE, "Deepgram STT"),
    "elevenlabs": Vendor("elevenlabs", VendorTier.CLOUD_SERVICE, "ElevenLabs TTS"),
    "openai_web_search": Vendor("openai_web_search", VendorTier.CLOUD_SERVICE, "OpenAI web/file search"),
    "openclaw": Vendor("openclaw", VendorTier.CHANNEL, "OpenClaw channel gateway"),
}


# ---------------------------------------------------------------------------
# The matrix: (DataClass, VendorTier) -> disposition.
# ---------------------------------------------------------------------------
class Disposition(StrEnum):
    """A matrix cell. It is *not* the final verdict — it is the rule that,
    combined with configured standing grants and any per-request authorization,
    produces the verdict."""

    FORBIDDEN = "forbidden"       # never, regardless of any grant or authorization
    STANDING = "standing"         # allowed only when a durable config grant is active for (class, vendor)
    PER_REQUEST = "per_request"   # allowed only with a matching per-request founder authorization


class Verdict(StrEnum):
    ALLOW = "allow"
    AUTH_REQUIRED = "auth_required"   # a per-request founder grant would unlock it
    DENY = "deny"                     # hard no; no grant can unlock it


# Default policy matrix. Rows = data class, cols = vendor tier (LOCAL omitted:
# loopback is always ALLOW). Deliberately conservative — this is the reviewable
# artifact the founder tunes. Anything not stated is FORBIDDEN (fail closed).
#
#                        LAN           PUBLIC_WEB     CLOUD_MODEL    CLOUD_SERVICE  CHANNEL
DEFAULT_MATRIX: dict[DataClass, dict[VendorTier, Disposition]] = {
    DataClass.PUBLIC_DERIVED: {
        VendorTier.LAN: Disposition.STANDING,
        VendorTier.PUBLIC_WEB: Disposition.STANDING,
        VendorTier.CLOUD_MODEL: Disposition.PER_REQUEST,
        VendorTier.CLOUD_SERVICE: Disposition.PER_REQUEST,
        VendorTier.CHANNEL: Disposition.PER_REQUEST,
    },
    DataClass.OPERATIONAL: {
        VendorTier.LAN: Disposition.STANDING,
        VendorTier.PUBLIC_WEB: Disposition.PER_REQUEST,
        VendorTier.CLOUD_MODEL: Disposition.PER_REQUEST,
        VendorTier.CLOUD_SERVICE: Disposition.PER_REQUEST,
        # Channel egress (OpenClaw) is per-request until cross-channel founder
        # authentication ships — the gate blocks payload-authored grants, but it
        # cannot prove the human on the far end of a channel is the founder.
        VendorTier.CHANNEL: Disposition.PER_REQUEST,
    },
    DataClass.REPLY_TEXT: {
        VendorTier.LAN: Disposition.STANDING,
        VendorTier.PUBLIC_WEB: Disposition.FORBIDDEN,
        VendorTier.CLOUD_MODEL: Disposition.PER_REQUEST,
        VendorTier.CLOUD_SERVICE: Disposition.STANDING,   # TTS (ElevenLabs) rides a standing voice grant
        VendorTier.CHANNEL: Disposition.PER_REQUEST,
    },
    DataClass.FOUNDER_AUDIO: {
        VendorTier.LAN: Disposition.PER_REQUEST,
        VendorTier.PUBLIC_WEB: Disposition.FORBIDDEN,
        VendorTier.CLOUD_MODEL: Disposition.PER_REQUEST,
        VendorTier.CLOUD_SERVICE: Disposition.STANDING,   # STT (Deepgram) rides a standing voice grant
        VendorTier.CHANNEL: Disposition.FORBIDDEN,
    },
    DataClass.SCREEN_PIXELS: {
        VendorTier.LAN: Disposition.FORBIDDEN,
        VendorTier.PUBLIC_WEB: Disposition.FORBIDDEN,
        VendorTier.CLOUD_MODEL: Disposition.PER_REQUEST,  # founder-approved vision, one request at a time
        VendorTier.CLOUD_SERVICE: Disposition.PER_REQUEST,
        VendorTier.CHANNEL: Disposition.FORBIDDEN,
    },
    DataClass.SOURCE_CODE: {
        VendorTier.LAN: Disposition.PER_REQUEST,
        VendorTier.PUBLIC_WEB: Disposition.FORBIDDEN,
        VendorTier.CLOUD_MODEL: Disposition.PER_REQUEST,  # cloud coding delegate, explicit per run
        VendorTier.CLOUD_SERVICE: Disposition.FORBIDDEN,
        VendorTier.CHANNEL: Disposition.FORBIDDEN,
    },
    # A founder-curated brief MAY go to a named model, per request. This is the
    # only path by which strategic context reaches a cloud model — and it is a
    # deliberately assembled excerpt, never a wholesale export of live state, and
    # never the authority policy. That is the resolution to "long-context
    # strategic review": possible, explicit each time, and over curated material.
    DataClass.FOUNDER_BRIEF: {
        VendorTier.LAN: Disposition.PER_REQUEST,
        VendorTier.PUBLIC_WEB: Disposition.FORBIDDEN,
        VendorTier.CLOUD_MODEL: Disposition.PER_REQUEST,
        VendorTier.CLOUD_SERVICE: Disposition.FORBIDDEN,
        VendorTier.CHANNEL: Disposition.FORBIDDEN,
    },
    # Raw founder state — charter, strategy ledger, founder OS, memory, and the
    # authority policy itself — NEVER leaves the machine, to any destination.
    # Sending a model the definition of your own guardrails is uniquely bad.
    # Route curated material through FOUNDER_BRIEF instead.
    DataClass.FOUNDER_STATE: {
        VendorTier.LAN: Disposition.FORBIDDEN,
        VendorTier.PUBLIC_WEB: Disposition.FORBIDDEN,
        VendorTier.CLOUD_MODEL: Disposition.FORBIDDEN,
        VendorTier.CLOUD_SERVICE: Disposition.FORBIDDEN,
        VendorTier.CHANNEL: Disposition.FORBIDDEN,
    },
    DataClass.CREDENTIALS: {
        VendorTier.LAN: Disposition.FORBIDDEN,
        VendorTier.PUBLIC_WEB: Disposition.FORBIDDEN,
        VendorTier.CLOUD_MODEL: Disposition.FORBIDDEN,
        VendorTier.CLOUD_SERVICE: Disposition.FORBIDDEN,
        VendorTier.CHANNEL: Disposition.FORBIDDEN,
    },
}


# ---------------------------------------------------------------------------
# Requests, grants, decisions.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EgressRequest:
    """A caller's declared intent to send data off-process."""

    request_id: str
    data_class: DataClass
    vendor: str          # a key into VENDORS
    purpose: str = ""
    byte_estimate: int = 0


@dataclass(frozen=True)
class EgressAuthorization:
    """A founder-granted, single-purpose unlock for one PER_REQUEST cell.

    Bound to the exact request. ``granted_by`` MUST be the founder — a grant
    whose provenance is anything else (a tool result, a channel message) is
    rejected, mirroring the instruction-source boundary. ``typed_phrase_ok``
    records that the typed-confirmation gate (authority.AuthorityPolicy) was
    satisfied, so this composes with — rather than bypasses — the existing
    approval console."""

    request_id: str
    data_class: DataClass
    vendor: str
    granted_by: str = "founder"
    typed_phrase_ok: bool = False

    def matches(self, request: EgressRequest) -> bool:
        return (
            self.granted_by == "founder"
            and self.request_id == request.request_id
            and self.data_class == request.data_class
            and self.vendor == request.vendor
        )


@dataclass(frozen=True)
class EgressDecision:
    verdict: Verdict
    reason: str
    matched_rule: str
    data_class: DataClass
    vendor: str
    vendor_tier: VendorTier | None = None
    provider_policy: str = "local_only"

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW

    def audit_record(self) -> dict[str, Any]:
        """Redacted — class/vendor/verdict/rule only. Never the payload."""
        return {
            "kind": "egress_decision",
            "verdict": self.verdict.value,
            "data_class": self.data_class.value,
            "vendor": self.vendor,
            "vendor_tier": self.vendor_tier.value if self.vendor_tier else None,
            "matched_rule": self.matched_rule,
            "provider_policy": self.provider_policy,
            "reason": self.reason,
        }


class EgressRefused(PermissionError):
    """Raised by a call site when ``EgressPolicy`` refuses a send.

    Carries the decision so the caller can audit it. A caller that wants a softer
    surface (e.g. voice degrading to no-speech) can translate this to its own
    exception type; the decision travels either way."""

    def __init__(self, decision: EgressDecision):
        self.decision = decision
        super().__init__(decision.reason)


def parse_standing_grants(items: Iterable[str]) -> frozenset[tuple[DataClass, str]]:
    """Parse ``"data_class:vendor"`` config strings into validated grant pairs.

    Fail loud on a malformed or unknown entry — a typo in a standing grant must
    never silently widen or narrow what may egress."""
    grants: set[tuple[DataClass, str]] = set()
    for item in items:
        raw = str(item).strip()
        if not raw:
            continue
        if ":" not in raw:
            raise ValueError(f"Invalid egress standing grant {raw!r}: expected 'data_class:vendor'.")
        cls_name, vendor = (part.strip() for part in raw.split(":", 1))
        try:
            data_class = DataClass(cls_name)
        except ValueError as exc:
            raise ValueError(f"Invalid egress standing grant {raw!r}: unknown data class {cls_name!r}.") from exc
        if vendor not in VENDORS:
            raise ValueError(f"Invalid egress standing grant {raw!r}: unknown vendor {vendor!r}.")
        grants.add((data_class, vendor))
    return frozenset(grants)


# ---------------------------------------------------------------------------
# The gate.
# ---------------------------------------------------------------------------
class EgressPolicy:
    """Decides one egress request. Pure and side-effect free: it returns a
    decision; the caller records the audit row and (only on ALLOW) proceeds to
    ``netguard.assert_allowed`` and the actual send. netguard still runs after —
    this gate governs *whether data of this class may go to this vendor*, not
    whether the network target is safe. Both must pass."""

    def __init__(
        self,
        *,
        provider_policy: str = "local_only",
        standing_grants: frozenset[tuple[DataClass, str]] = frozenset(),
        matrix: Mapping[DataClass, Mapping[VendorTier, Disposition]] | None = None,
        vendors: Mapping[str, Vendor] | None = None,
    ):
        self.provider_policy = (provider_policy or "local_only").strip().lower()
        # Durable (class, vendor) grants the founder has enabled in config — e.g.
        # selecting the Deepgram voice engine enables (FOUNDER_AUDIO, "deepgram").
        self.standing_grants = frozenset(standing_grants)
        self.matrix = matrix or DEFAULT_MATRIX
        self.vendors = vendors or VENDORS

    @classmethod
    def from_config(cls, config: "KernelConfig") -> "EgressPolicy":
        """Build from the live kernel config. Reads ``provider_policy`` from the
        [ollama] section (it governs model endpoints too — the gate defers to it,
        it does not own it) and the durable standing grants from [egress]."""
        ollama = getattr(config, "ollama", None)
        egress_cfg = getattr(config, "egress", None)
        provider_policy = getattr(ollama, "provider_policy", "local_only") if ollama else "local_only"
        raw_grants = getattr(egress_cfg, "standing_grants", ()) if egress_cfg else ()
        return cls(
            provider_policy=provider_policy,
            standing_grants=parse_standing_grants(raw_grants),
        )

    def decide(
        self,
        request: EgressRequest,
        authorization: EgressAuthorization | None = None,
    ) -> EgressDecision:
        vendor = self.vendors.get(request.vendor)
        if vendor is None:
            return self._deny(request, None, "vendor.unknown",
                              f"Unknown egress vendor {request.vendor!r}; fail closed.")

        # 1. Loopback is always fine — the whole system is local-first.
        if vendor.tier is VendorTier.LOCAL:
            return self._allow(request, vendor, "local.loopback",
                               "Loopback/local destination is always permitted.")

        # 2. Credentials never leave the machine — no grant can unlock this.
        if request.data_class is DataClass.CREDENTIALS:
            return self._deny(request, vendor, "deny.credentials",
                              "Credentials/secrets never egress. No authorization can unlock this.")

        # 3. Hard local-only overlay. Under local_only the matrix is inert: every
        #    non-local destination is refused before any grant is consulted. This
        #    is why landing the gate changes nothing today.
        if self.provider_policy == "local_only":
            return self._deny(request, vendor, "policy.local_only",
                              "provider_policy is local_only: no non-local egress. "
                              "Raise the policy deliberately to enable the matrix.")

        # 4. Consult the matrix cell.
        disposition = self.matrix.get(request.data_class, {}).get(vendor.tier)
        if disposition is None or disposition is Disposition.FORBIDDEN:
            return self._deny(request, vendor, "matrix.forbidden",
                              f"{request.data_class.value} -> {vendor.tier.value} is forbidden "
                              "by the egress matrix.")

        if disposition is Disposition.STANDING:
            if (request.data_class, vendor.key) in self.standing_grants:
                return self._allow(request, vendor, "matrix.standing_grant",
                                   f"Active standing grant for ({request.data_class.value}, {vendor.key}).")
            return self._deny(request, vendor, "matrix.standing_missing",
                              f"{request.data_class.value} -> {vendor.key} needs a standing config grant "
                              "that is not enabled.")

        # disposition is PER_REQUEST
        if authorization is not None and authorization.matches(request):
            return self._allow(request, vendor, "matrix.per_request_granted",
                               "Matching founder per-request authorization present.")
        return EgressDecision(
            verdict=Verdict.AUTH_REQUIRED,
            reason=(f"{request.data_class.value} -> {vendor.key} requires an explicit per-request "
                    "founder authorization."),
            matched_rule="matrix.per_request_needed",
            data_class=request.data_class,
            vendor=vendor.key,
            vendor_tier=vendor.tier,
            provider_policy=self.provider_policy,
        )

    # -- helpers ----------------------------------------------------------
    def _allow(self, request: EgressRequest, vendor: Vendor, rule: str, reason: str) -> EgressDecision:
        return EgressDecision(
            verdict=Verdict.ALLOW, reason=reason, matched_rule=rule,
            data_class=request.data_class, vendor=vendor.key, vendor_tier=vendor.tier,
            provider_policy=self.provider_policy,
        )

    def _deny(self, request: EgressRequest, vendor: Vendor | None, rule: str, reason: str) -> EgressDecision:
        return EgressDecision(
            verdict=Verdict.DENY, reason=reason, matched_rule=rule,
            data_class=request.data_class, vendor=request.vendor,
            vendor_tier=vendor.tier if vendor else None,
            provider_policy=self.provider_policy,
        )
