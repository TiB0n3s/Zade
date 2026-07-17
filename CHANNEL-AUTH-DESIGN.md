# Cross-Channel Founder Authentication

**Status:** identity primitive + kernel-side adapter ingress + runtime cap enforcement all built and tested (`channel_auth.py`, `/channels/*`, `POST /channels/message`, `runtime.respond(authority_ceiling=…)`). Only the external channel transport (the platform webhook side) is out of scope.
**Date:** 2026-07-17
**Prereq for:** any messaging-channel ingress into the runtime (OpenClaw: WhatsApp/Telegram/Slack/Discord → `/runtime/respond`).

---

## 1. The problem

A channel adapter would route inbound messages into `/runtime/respond`, which has **founder-command-as-authorization** semantics and real authority (git commit, trading advisory, vault delete). So the runtime must know: **is the human on the far end actually the founder?**

The obvious answer — trust the sender handle/username — is wrong:
- Handles are **spoofable** (anyone can DM a bot; usernames change).
- **Forwarded** messages carry the original sender.
- A bot handle is discoverable; whoever finds it can message it.

The egress gate already closed the *injection* half (a channel message can't mint its own egress grant — grants are founder-only and payload-authored ones are rejected). This module closes the *authentication* half: binding a channel identity to the founder soundly.

## 2. The mechanism — challenge-response binding

```
founder (local, token-gated):  POST /channels/enroll {channel}  ─▶ one-time code (hash stored, raw shown once)
founder (via the channel):     sends the code from their real account
adapter:                       POST /channels/confirm {channel, external_id, code}
                                  code matches a live enrollment ─▶ bind (channel, external_id) → founder
per message thereafter:        channel_auth.authenticate(channel, external_id) ─▶ ChannelIdentity
```

A completed enrollment proves control of **both** sides: the local kernel (who saw the code — the endpoint is mutation-token gated) *and* the channel account (who echoed it back). The binding is keyed on the **`external_id`** (the account id — Telegram chat id, Slack user id), never the handle.

## 3. Guarantees (each test-pinned in `test_channel_auth.py`)

1. **Fail-closed** — an unbound `(channel, external_id)` authenticates to *nothing*: `authenticated=False`, `max_tier=None`. Untrusted input, like a web page.
2. **Handle is never trusted** — a different account on the same channel gets nothing; the same id on a different channel is separate. Only the bound `external_id` counts.
3. **Codes are secrets** — only the SHA-256 hash is stored; the raw code is shown once, expires (10 min default), and is single-use (consumed on bind).
4. **Capped authority** — a binding carries a `max_tier` ceiling, **default `L0_READ`** (converse + read). Raising it is a deliberate founder act (`/channels/bindings/{id}/tier`).
5. **Revocable + audited** — every enroll/bind/revoke/tier change is in the audit ledger; revocation immediately removes authority.

## 4. The authority model — "channels propose, only the local surface approves"

This is the load-bearing policy, and it exists because **ongoing trust is only as strong as the channel account**: if the founder's Telegram is later compromised, the attacker inherits the binding. That can't be engineered away at this layer — so authority is bounded:

- A channel identity's `max_tier` caps what it may do **autonomously**.
- **L2+/L3 actions route to LOCAL approval regardless** — the mutation-token'd console, which a channel does not have. A channel message can *create* an approval request (a proposal); only the local trusted surface can *approve/dispatch* it.
- Result: a compromised channel can spam proposals; it **cannot execute a destructive/external action**, because that still needs the local typed-phrase approval.

The default `L0_READ` means: out of the box, a bound channel can ask and read, nothing more. The founder widens per binding, knowingly.

## 5. What's built vs. not

**Built** (decoupled, tested, no adapter):
- `ChannelAuth`: `begin_enrollment` / `confirm_enrollment` / `authenticate` / `caps` / `revoke` / `set_max_tier` / `list_bindings`.
- Tables `channel_enrollments`, `channel_bindings` (in `SCHEMA_SQL`).
- Founder endpoints: `POST /channels/enroll`, `POST /channels/confirm`, `GET /channels/bindings`, `POST /channels/bindings/{id}/revoke`, `POST /channels/bindings/{id}/tier`.

**Adapter ingress + runtime cap — BUILT:**
- `POST /channels/message {channel, external_id, text}` — the kernel-side adapter ingress. **Mutation-token gated**, so only a trusted *local* adapter (OpenClaw / a native bridge) can inject messages; a random external caller cannot. It: (a) completes a binding on a `/bind <code>` message (`parse_bind_command`), (b) `authenticate`s the identity, (c) **refuses unbound identities** (they never reach the runtime), (d) routes bound identities into `runtime.respond` with `authority_ceiling = max_tier`.
- **Runtime cap enforced** — `runtime.respond(authority_ceiling=…)`: below L3, the three action routes (chat action / research / build) are skipped entirely, so a channel message converses but never autonomously triggers an action. Response carries `channel_capped: true`. `None` ceiling = the local founder, uncapped (unchanged). Test-pinned: default L0 identity caps a research command; raising the binding to L3 un-caps it.

**Still the external adapter's job (out of kernel scope):**
- The actual channel transport (OpenClaw / a Telegram/Slack bridge) that receives platform webhooks and calls `POST /channels/message`. The kernel side is complete; this is a deployment/integration concern.
- Optional per-binding HMAC (for adapters/bots that can sign each message) — the enrollment binding is the human-typed path; signing is a future hardening.
- Per-(channel, external_id) conversation continuity (channel messages are currently standalone turns).

## 6. Honest limitations (stated plainly)

- Binding authenticates the **channel account**, not each keystroke. Channel-account compromise = binding compromise. The capped-authority + local-approval model is the mitigation, not a cure.
- The enrollment code travels through the channel in plaintext — if the channel itself is being surveilled at enrollment time, the code could be captured before the founder uses it. Codes are short-lived and single-use to bound this.
- This is deliberately the **narrowest** useful primitive: known identity + bounded authority + fail-closed. It makes channel ingress *possible to do safely*, not *unconditionally safe*. Widening authority over channels is a per-binding, eyes-open founder decision.
