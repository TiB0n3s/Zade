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
        # Was STANDING for cloud TTS (ElevenLabs). Voice went actually-local
        # (whisper.cpp + piper) on 2026-07-17; the cloud voice lane is dead code
        # and its cell is now FORBIDDEN — a standing grant in config can no
        # longer resurrect it. Reverting cloud voice is a deliberate two-step:
        # flip this cell back AND re-add the grant.
        VendorTier.CLOUD_SERVICE: Disposition.FORBIDDEN,
        VendorTier.CHANNEL: Disposition.PER_REQUEST,
    },
    DataClass.FOUNDER_AUDIO: {
        VendorTier.LAN: Disposition.PER_REQUEST,
        VendorTier.PUBLIC_WEB: Disposition.FORBIDDEN,
        VendorTier.CLOUD_MODEL: Disposition.PER_REQUEST,
        # Was STANDING for cloud STT (Deepgram) — same teardown as REPLY_TEXT
        # above: the founder's voice never rides to a cloud service.
        VendorTier.CLOUD_SERVICE: Disposition.FORBIDDEN,
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


# ---------------------------------------------------------------------------
# Per-request grant flow: turning AUTH_REQUIRED into a founder decision.
# ---------------------------------------------------------------------------
# The gate can DEMAND a per-request authorization (Verdict.AUTH_REQUIRED) but it
# cannot MINT one — a grant must come from the founder. These functions carry a
# PER_REQUEST egress from "the gate wants a grant" to "the founder issued one",
# reusing the same approval_requests table as the mcp_memory_write flow. Nothing
# here egresses; they only decide whether an egress MAY proceed. Lifecycle:
#   1. authorize_egress(...)      -> AUTH_REQUIRED, and a pending grant is filed
#   2. approve_egress_grant(...)  -> founder issues it (with the typed phrase)
#   3. authorize_egress(...)      -> ALLOW (the stored grant matches the request)
#   4. consume_grant(...)         -> after the send, so the grant can't be replayed
#
# `db` is the KernelDatabase (duck-typed here to keep this module import-light).
GRANT_SOURCE_TYPE = "egress_grant"


def _grant_matches(meta: Mapping[str, Any], request: EgressRequest) -> bool:
    return (
        meta.get("request_id") == request.request_id
        and meta.get("data_class") == request.data_class.value
        and meta.get("vendor") == request.vendor
    )


def request_egress_grant(db: Any, request: EgressRequest, *, preview: str = "") -> Any:
    """File (idempotently) a pending founder grant request for one PER_REQUEST
    egress, and return the ApprovalRequest. A matching pending request is reused,
    so a retrying operation never stacks duplicates. ``preview`` is a short,
    founder-facing description of WHAT would be sent — never the payload itself."""
    for r in db.list_approval_requests(status="pending", limit=500):
        if r.source_type == GRANT_SOURCE_TYPE and _grant_matches(r.metadata or {}, request):
            return r
    approval, _created = db.ensure_approval_request(
        source_type=GRANT_SOURCE_TYPE,
        source_id=None,
        title=f"Egress: {request.data_class.value} → {request.vendor}",
        detail=(request.purpose or preview)[:500],
        action="egress.grant",
        target=request.vendor,
        permission_tier="L3_EXTERNAL_ACTION",
        authority_decision="approval_required",
        authority={"reason": "Per-request egress requires explicit founder authorization."},
        requested_by="egress",
        metadata={
            "request_id": request.request_id,
            "data_class": request.data_class.value,
            "vendor": request.vendor,
            "purpose": request.purpose,
            "preview": preview[:2000],
            "byte_estimate": request.byte_estimate,
        },
    )
    db.audit(
        actor="egress", action="egress.grant.requested", target=request.vendor,
        permission_tier="L3_EXTERNAL_ACTION", status="pending",
        details={"approval_request_id": approval.id, "data_class": request.data_class.value, "vendor": request.vendor},
    )
    return approval


def list_pending_grants(db: Any) -> list[dict[str, Any]]:
    """Per-request egress grants awaiting the founder's decision (redacted: the
    preview, never the payload)."""
    out: list[dict[str, Any]] = []
    for r in db.list_approval_requests(status="pending", limit=500):
        if r.source_type != GRANT_SOURCE_TYPE:
            continue
        m = r.metadata or {}
        out.append(
            {
                "approval_request_id": r.id,
                "data_class": m.get("data_class"),
                "vendor": m.get("vendor"),
                "purpose": m.get("purpose"),
                "preview": m.get("preview"),
                "byte_estimate": m.get("byte_estimate"),
                "requested_by": r.requested_by,
                "created_at": r.created_at,
            }
        )
    return out


def _load_pending_grant(db: Any, request_id: int) -> Any:
    r = db.get_approval_request(request_id)
    if r is None or r.source_type != GRANT_SOURCE_TYPE:
        raise ValueError(f"Not an egress grant request: {request_id}")
    if r.status not in {"pending", "deferred"}:
        raise ValueError(f"Egress grant request already {r.status}.")
    return r


def approve_egress_grant(
    db: Any,
    request_id: int,
    *,
    resolved_by: str = "founder",
    typed_phrase: str = "",
    typed_confirmation_phrase: str = "make the jump to hyperspace",
) -> dict[str, Any]:
    """Founder issues the per-request grant. Requires the typed confirmation
    phrase — the same ritual as any external action, so a grant is never minted
    from a click alone. The grant persists as the approved approval_request;
    ``active_grant_for`` reconstitutes the EgressAuthorization from it."""
    r = _load_pending_grant(db, request_id)
    if typed_phrase.strip() != typed_confirmation_phrase:
        raise ValueError(f"Egress grant requires the typed confirmation phrase: {typed_confirmation_phrase}")
    m = r.metadata or {}
    db.resolve_approval_request(request_id, status="approved", resolved_by=resolved_by, resolution_note="egress grant issued")
    db.audit(
        actor=resolved_by, action="egress.grant.approved", target=m.get("vendor"),
        permission_tier="L3_EXTERNAL_ACTION", status="approved",
        details={"approval_request_id": request_id, "data_class": m.get("data_class"), "vendor": m.get("vendor")},
    )
    return {
        "approval_request_id": request_id,
        "granted": True,
        "request_id": m.get("request_id"),
        "data_class": m.get("data_class"),
        "vendor": m.get("vendor"),
    }


def deny_egress_grant(db: Any, request_id: int, *, resolved_by: str = "founder", note: str = "") -> dict[str, Any]:
    r = _load_pending_grant(db, request_id)
    m = r.metadata or {}
    db.resolve_approval_request(request_id, status="denied", resolved_by=resolved_by, resolution_note=note or "egress grant denied")
    db.audit(
        actor=resolved_by, action="egress.grant.denied", target=m.get("vendor"),
        permission_tier="L3_EXTERNAL_ACTION", status="denied",
        details={"approval_request_id": request_id, "data_class": m.get("data_class"), "vendor": m.get("vendor")},
    )
    return {"approval_request_id": request_id, "status": "denied"}


def active_grant_for(db: Any, request: EgressRequest) -> EgressAuthorization | None:
    """The founder-approved, not-yet-consumed grant matching this EXACT request,
    reconstituted as an EgressAuthorization the gate will accept. None otherwise."""
    for r in db.list_approval_requests(status="approved", limit=500):
        if r.source_type != GRANT_SOURCE_TYPE:
            continue
        m = r.metadata or {}
        if _grant_matches(m, request) and not m.get("consumed"):
            return EgressAuthorization(
                request_id=request.request_id,
                data_class=request.data_class,
                vendor=request.vendor,
                granted_by="founder",
                typed_phrase_ok=True,
            )
    return None


def consume_grant(db: Any, request: EgressRequest) -> bool:
    """Mark the matching approved grant consumed so it can't authorize a second
    send — single-use is the EgressAuthorization contract, enforced across calls.
    Returns True if a grant was consumed."""
    for r in db.list_approval_requests(status="approved", limit=500):
        if r.source_type != GRANT_SOURCE_TYPE:
            continue
        m = r.metadata or {}
        if _grant_matches(m, request) and not m.get("consumed"):
            db.update_approval_request(r.id, metadata={"consumed": True})
            db.audit(
                actor="egress", action="egress.grant.consumed", target=request.vendor,
                permission_tier="L3_EXTERNAL_ACTION", status="ok",
                details={"approval_request_id": r.id, "data_class": request.data_class.value, "vendor": request.vendor},
            )
            return True
    return False


def egress_ledger(db: Any, *, limit: int = 500) -> dict[str, Any]:
    """Founder-facing rollup of everything the egress gate has decided: what left
    the machine, to whom, under which grant — and what the gate stopped.

    Every row already exists in audit_events (each decision/grant transition is
    audited, redacted); this view only aggregates. It answers the three founder
    questions the raw ledger buries: (1) has anything actually left, (2) was every
    send covered by a live authorization, (3) what tried to leave and was refused.
    Payloads are never stored in audit rows, so none can appear here."""
    events = db.audit_events_by_action_prefix("egress.", limit=limit)
    events.reverse()  # chronological

    sends: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    awaiting: list[dict[str, Any]] = []
    by_vendor: dict[str, dict[str, int]] = {}
    for e in events:
        details = e.get("details") or {}
        vendor = str(e.get("target") or details.get("vendor") or "?")
        action = str(e.get("action"))
        if action == "egress.decision":
            verdict = str(details.get("verdict", e.get("status", "?")))
            counts = by_vendor.setdefault(vendor, {})
            counts[verdict] = counts.get(verdict, 0) + 1
            entry = {
                "at": e.get("created_at"),
                "vendor": vendor,
                "data_class": details.get("data_class"),
                "rule": details.get("matched_rule"),
                "reason": details.get("reason"),
            }
            if verdict == "allow":
                sends.append(entry)
            elif verdict in {"deny", "forbidden"}:
                blocked.append(entry | {"verdict": verdict})
            elif verdict == "auth_required":
                awaiting.append(entry)
        elif action == "egress.grant.consumed":
            # tie the consumption to the most recent ALLOW for the same vendor,
            # so a send row shows which grant covered it
            for entry in reversed(sends):
                if entry["vendor"] == vendor and "grant_request_id" not in entry:
                    entry["grant_request_id"] = details.get("approval_request_id")
                    break

    grants: list[dict[str, Any]] = []
    for r in db.list_approval_requests(status=None, limit=limit):
        if r.source_type != GRANT_SOURCE_TYPE:
            continue
        m = r.metadata or {}
        grants.append(
            {
                "approval_request_id": r.id,
                "status": "consumed" if m.get("consumed") else r.status,
                "vendor": m.get("vendor"),
                "data_class": m.get("data_class"),
                "requested_at": r.created_at,
                "resolved_by": r.resolved_by or None,
                "preview": r.detail or "",
            }
        )

    return {
        "summary": {
            "decisions_by_vendor": by_vendor,
            "left_the_machine": len(sends),
            "blocked": len(blocked),
            "grants_total": len(grants),
            "grants_pending": sum(1 for g in grants if g["status"] == "pending"),
            "note": "Counts cover the audit window scanned, not all time."
            if len(events) >= limit
            else "Counts cover the full audit history for egress actions.",
        },
        "left_the_machine": sends,
        "blocked": blocked,
        "awaiting_authorization": awaiting[-10:],
        "grants": grants,
    }


def authorize_egress(db: Any, policy: EgressPolicy, request: EgressRequest, *, preview: str = "") -> EgressDecision:
    """Decide a (possibly PER_REQUEST) egress against the gate AND persisted
    founder grants. On AUTH_REQUIRED it files a pending founder grant request
    (idempotent) so it can be authorized; the caller re-runs this after approval
    to get ALLOW, then egresses and calls ``consume_grant``. Every decision is
    audited (redacted). This is the one call an egressing caller needs."""
    grant = active_grant_for(db, request)
    decision = policy.decide(request, authorization=grant)
    if decision.verdict is Verdict.AUTH_REQUIRED:
        request_egress_grant(db, request, preview=preview)
    db.audit(
        actor="egress", action="egress.decision", target=request.vendor,
        permission_tier="L3_EXTERNAL_ACTION", status=decision.verdict.value,
        details=decision.audit_record(),
    )
    return decision
