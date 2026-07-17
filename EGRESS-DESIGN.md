# Egress Classification Matrix + Per-Request Provider-Authorization Gate

**Status:** matrix decided (§7 resolved); gate wired into the voice lane (Phase 2).
**Date:** 2026-07-17
**Owner:** founder (Ellie) / Zade kernel
**Prereq for:** any OpenAI / Anthropic / OpenClaw integration. Nothing cloud is safe before this.

---

## 1. Why this exists

The kernel already guards outbound work along **three orthogonal axes**:

| Layer | File | Question it answers | Blind to |
|---|---|---|---|
| Authority policy | [authority.py](src/cofounder_kernel/authority.py) | Was the *action* authorized? (allow / approve / deny) | what data, which vendor |
| Net guard (SSRF) | [netguard.py:66](src/cofounder_kernel/netguard.py) | Is the *network target* safe? (scheme, https, private, host allowlist) | what data, which vendor, who authorized |
| Provider policy | [ollama.py:98](src/cofounder_kernel/ollama.py) | May this *model endpoint/model* run? (`local_only` / `local_preferred` / `cloud_allowed`) | non-model egress (voice, research, screenshots); *data class* |

None of them answers the question every cloud integration actually raises:

> **This authorized action wants to send data of class _X_ to vendor _Y_. Is _that specific egress_ permitted for _this specific request_?**

And there's a live promise with no enforcement behind it. From [config.py:82](src/cofounder_kernel/config.py) — the `local_preferred` policy docstring:

> "local Ollama first; **cloud still needs an explicit per-request authorization**; never an automatic fallback."

That mechanism did not exist. This is it. It's the fourth axis: **data-class × destination-vendor**.

---

## 2. The real egress lanes today (ground truth)

Enumerated from the actual `netguard.assert_allowed` call sites, not from memory:

| Lane | Call site | Data leaving | Destination | Current gate |
|---|---|---|---|---|
| Model inference | [ollama.py:262](src/cofounder_kernel/ollama.py) | **everything** in the prompt (founder state, code, memory) | loopback Ollama | provider_policy + netguard(private) |
| Voice STT | [voice.py:265](src/cofounder_kernel/voice.py) | **raw founder audio** | Deepgram (cloud) | netguard(https, host allowlist) + standing engine selection |
| Voice TTS | [voice.py:281](src/cofounder_kernel/voice.py) | **reply text** | ElevenLabs (cloud) | netguard(https, host allowlist) + standing engine selection |
| Research fetch | [research.py:341](src/cofounder_kernel/research.py) | outbound GET (query/URL only) | arbitrary public https | approval + netguard(https) |
| Connector sync | [connectors.py:531](src/cofounder_kernel/connectors.py) | inbound pull | IMAP/ICS | netguard(https) |
| SMS notify | [notify.py:263](src/cofounder_kernel/notify.py) | notification text | founder LAN gateway | netguard(private) |
| Delegation (bridge) | [delegation.py] | build brief + code | subprocess env → **loopback** Ollama | provider policy sanitization |

**The only cloud egress today is the two voice lanes** (Deepgram audio, ElevenLabs reply text). Everything else is loopback / LAN / approval-gated *pull*. Voice is therefore the one existing hole in "nothing leaves the machine" — worth naming plainly (see §7).

---

## 3. The egress classification matrix

**Rows = data class** (what's leaving, ascending sensitivity). **Columns = vendor tier** (where it's going). `LOCAL` (loopback) is omitted — it is always `ALLOW`.

Cell dispositions:
- **FORBIDDEN** — never, regardless of any grant. No authorization can unlock it.
- **STANDING** — allowed only when a *durable config grant* for that exact `(class, vendor)` is enabled (e.g. selecting the Deepgram voice engine enables `(founder_audio, deepgram)`).
- **PER_REQUEST** — allowed only with an explicit, founder-issued, single-purpose authorization matched to this request.

This is the matrix as shipped, after the §7 decisions were resolved (see §7 for the rationale on each):

| data class ↓ / vendor tier → | LAN | PUBLIC_WEB | CLOUD_MODEL | CLOUD_SERVICE | CHANNEL |
|---|---|---|---|---|---|
| **public_derived** (research query, public URL) | STANDING | STANDING | PER_REQUEST | PER_REQUEST | PER_REQUEST |
| **operational** (status text) | STANDING | PER_REQUEST | PER_REQUEST | PER_REQUEST | **PER_REQUEST** ‹#3› |
| **reply_text** (Zade's answer) | STANDING | **FORBIDDEN** | PER_REQUEST | STANDING *(TTS)* ‹#1› | PER_REQUEST |
| **founder_audio** (raw mic) | PER_REQUEST | **FORBIDDEN** | PER_REQUEST | STANDING *(STT)* ‹#1› | **FORBIDDEN** |
| **screen_pixels** (screenshots) | **FORBIDDEN** | **FORBIDDEN** | PER_REQUEST | PER_REQUEST | **FORBIDDEN** |
| **source_code** (repo, diffs, briefs) | PER_REQUEST | **FORBIDDEN** | PER_REQUEST | **FORBIDDEN** | **FORBIDDEN** |
| **founder_brief** (curated excerpt for one review) ‹#2› | PER_REQUEST | **FORBIDDEN** | PER_REQUEST | **FORBIDDEN** | **FORBIDDEN** |
| **founder_state** (RAW charter, strategy, memory, authority policy) ‹#2› | **FORBIDDEN** | **FORBIDDEN** | **FORBIDDEN** | **FORBIDDEN** | **FORBIDDEN** |
| **credentials** (secrets, keys, tokens) | **FORBIDDEN** | **FORBIDDEN** | **FORBIDDEN** | **FORBIDDEN** | **FORBIDDEN** |

`‹#n›` marks a cell set by the correspondingly-numbered §7 decision.

Vendor→tier mapping (`egress.VENDORS`): `sms_gateway`→LAN; `public_web`→PUBLIC_WEB; `openai`/`anthropic`/`ollama_cloud`→CLOUD_MODEL; `deepgram`/`elevenlabs`/`openai_web_search`→CLOUD_SERVICE; `openclaw`→CHANNEL; `local_ollama`/`local_files`→LOCAL.

Notable stances:
- **Raw `founder_state` NEVER leaves — to anywhere.** The authority policy itself lives in this class; a model must never receive the definition of its own guardrails. Strategic context reaches a cloud model *only* through the separate **`founder_brief`** class (PER_REQUEST, cloud-model-only): a deliberately curated excerpt, never a wholesale export, never cached-by-default. That is the honest resolution of the Anthropic "long-context strategic review" recommendation.
- **`source_code` and `founder_brief` reach a cloud model only per request** — the surviving cloud-model paths are exactly these two plus `screen_pixels` (approved vision).
- **`credentials` is FORBIDDEN across the board** — reinforces the existing authority.py deny-tokens (`exfiltrate`, secret dump) at the data layer.
- **The voice cells are the only STANDING cloud cells** — and their standing grants are *disabled by default*, so cloud voice is off out of the box (see §7 #1).

---

## 4. The gate

Implementation: [egress.py](src/cofounder_kernel/egress.py). `EgressPolicy.decide` is pure and side-effect-free — it returns a decision; the caller records the audit row and (only on ALLOW) proceeds to `netguard` and the send. The voice lane is the first wired call site.

### Decision flow (`EgressPolicy.decide`)

```
request = EgressRequest(request_id, data_class, vendor, purpose)
                 │
                 ▼
1. vendor unknown?              ─► DENY (fail closed)
2. vendor tier == LOCAL?        ─► ALLOW (loopback is always fine)
3. data_class == CREDENTIALS?   ─► DENY (no grant can unlock)
4. provider_policy local_only?  ─► DENY (matrix is INERT under local_only)
5. matrix[class][tier] disposition:
      FORBIDDEN     ─► DENY
      STANDING      ─► ALLOW iff (class,vendor) in configured standing_grants
                       else DENY (not configured)
      PER_REQUEST   ─► ALLOW iff a matching founder authorization is present
                       else AUTH_REQUIRED
                 │
                 ▼ (only on ALLOW)
        netguard.assert_allowed(...)   ← SSRF layer still runs after
                 │
                 ▼
        actual send
```

### Five invariants (each pinned by a test in [test_egress.py](tests/test_egress.py))

1. **Local-only is inert-by-default.** Under `provider_policy=local_only`, step 4 denies *every* non-local destination before the matrix is even consulted — even with a valid authorization. The matrix only comes alive when the founder deliberately raises the policy. **This is why the module is safe to land today: it forbids exactly what is already forbidden.**
2. **No silent fallback.** The gate only permits egress a caller *explicitly asked to send to a named vendor*. It never redirects a local failure to a cloud vendor.
3. **Authorization comes from the founder, never from payload.** `EgressAuthorization.matches()` requires `granted_by == "founder"` **and** an exact `(request_id, data_class, vendor)` match. A grant "authored" by a channel message or tool result is rejected — the instruction-source boundary, enforced in code. This is the direct answer to the OpenClaw cross-channel-authentication risk: a Telegram message cannot mint its own egress grant.
4. **Fail closed.** Unknown vendor, unclassified data, or a missing matrix cell → DENY.
5. **Redacted audit.** `EgressDecision.audit_record()` emits class/vendor/verdict/rule only — never the payload, never a secret. Mirrors `ollama.provider_info()`.

### How it composes (does not replace)

- **Authority policy** decides *the action* runs → **egress gate** decides *the data may leave to that vendor* → **netguard** decides *the target is network-safe*. All three must pass. The gate slots between the existing approval console and the existing SSRF check; it does not duplicate either.
- The per-request grant carries `typed_phrase_ok`, so a PER_REQUEST unlock rides the *existing* typed-confirmation gate (`authority.AuthorityPolicy.typed_confirmation_phrase`) rather than inventing a parallel approval path.

---

## 5. Config surface (proposed — not yet wired)

A new `[egress]` block, loaded like every other section in [config.py](src/cofounder_kernel/config.py) (`EgressConfig`):

```toml
[egress]
# Durable "data_class:vendor" grants — these unlock the matrix's STANDING cells.
# EMPTY BY DEFAULT: nothing cloud egresses out of the box. Uncomment a pair to
# deliberately re-enable it (and raise [ollama] provider_policy off local_only).
standing_grants = [
  # "founder_audio:deepgram",   # re-enable cloud STT
  # "reply_text:elevenlabs",    # re-enable cloud TTS
]
```

A malformed or unknown grant fails loud at load (`egress.parse_standing_grants`) — a typo must never silently widen egress. `provider_policy` stays in `[ollama]` (it governs model endpoints too); the egress gate *reads* it via `EgressPolicy.from_config`, it does not own it.

---

## 6. Rollout

| Phase | Scope | Status |
|---|---|---|
| **1** | Reference module + tests, decoupled. | ✅ landed |
| **2** | `[egress]` config + load (`EgressConfig`); gate wired into the **voice** cloud methods ([voice.py](src/cofounder_kernel/voice.py) `_assert_egress_allowed`) as the first real call site. Each decision audited (redacted, `action="egress.decision"`). Cloud voice now refused by default; local `command` engine unaffected. | ✅ landed |
| **3** | Wire the **research** lane; classify the fetch as `public_derived` (STANDING — the gate adds classification + audit, deferring to research's own approval). Surface AUTH_REQUIRED decisions in the approval console as a founder card. | next |
| **4** | First *new* cloud vendor (whichever the founder picks) behind PER_REQUEST. Grant issuance flows through the approval console + typed phrase. | gated by that vendor's design review |
| **—** | **Cross-channel founder authentication** (prereq for any CHANNEL egress; see §7 #3). | not started |

**Nothing in any phase flips `provider_policy` off `local_only` on its own.** Raising the policy stays a deliberate, separate founder act. Phase 2 changed no default runtime behavior: the default config ships local voice, and the gate refuses the cloud lanes it was already the case no one had enabled.

---

## 7. Decisions (resolved 2026-07-17)

1. **Voice → local.** Cloud voice is off by default. Implemented as: the two voice cells stay STANDING but their standing grants are **disabled by default**, so `EgressPolicy.from_config` refuses cloud STT/TTS under the shipped posture, and the local `command` engine (whisper.cpp / piper) is the intended path. *(Kept as reversible STANDING rather than hard-FORBIDDEN to preserve the Deepgram/ElevenLabs adapter code + its test coverage and leave a one-line re-enable. Hardening to FORBIDDEN + removing the cloud adapters is an available follow-up.)*
2. **Raw `founder_state` never leaves; add `founder_brief`.** `founder_state` → FORBIDDEN everywhere (the authority policy is in this class). Strategic context reaches a cloud model only via the new `founder_brief` class (PER_REQUEST, cloud-model-only) — a curated excerpt, never the raw ledger, never the authority policy.
3. **Channels are outbound-only until auth ships.** `operational → channel` downgraded STANDING → PER_REQUEST; every other channel cell FORBIDDEN. No inbound authority over a channel until **cross-channel founder authentication** is built (a registered per-channel signed token — *not* trusting the sender handle). Logged as a rollout prerequisite.
4. **Research stays STANDING.** `public_derived → public_web` remains STANDING: research already runs approval-gated at its own layer, the data leaving is only a query/URL, and a second per-request prompt would be friction that erodes the meaning of a grant. Here the gate's job is classification + an audit row, not a second approval.

---

## 8. What this deliberately does **not** do

- It does not add any cloud client, key handling, or network code. It is a *decision* module.
- It does not classify data for the caller — the call site declares the `DataClass`. (A future helper can assist, but the boundary is: caller asserts intent, gate rules on it — same shape as `netguard` taking `allow_private`/`require_https` from the caller.)
- It does not change `provider_policy`, the authority policy, or netguard. It is the fourth, missing axis, sitting alongside them.
