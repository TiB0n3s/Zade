# Zade Codebase Audit — 2026-07-12

Full audit of ~21.4k lines of source (30 modules) + 5.8k lines of tests, six parallel subsystem reviews. Baseline: **149 tests passing, clean tree.** This audits a *working* system for hardening, not a broken one.

## Remediation progress (suite: 149 → 191, green throughout)
- **Stage 1 — P0 security: DONE.** Kernel-state write/ingest exclusion; SSRF closed (URL parse + private-IP block + no redirects); constant-time token compare + loud posture warning; ingest `ValueError→400`. Tests: `test_security_hardening.py`.
- **Stage 2 — RC2 authority (v3): DONE.** Financial *execution* denied (noun+verb) while vocabulary is allowed; local-prefix bypass closed (approval screening before local allow); danger verbs + `dev.git.*` classified; text normalization. Tests in `test_authority.py`.
- **Stage 3 — P1 bugs: DONE.** Phantom thesis conflicts gated; date-only overdue fixed; contrarian clamp `[-50,0]`; empty-check evals rejected + deterministic generation; SMS host check; connector secret substring detection; devtools write-flag rejection. Tests: `test_stage3_fixes.py`.
- **Stage 4 — RC3 + P2: DONE.** Version-aware migrations + `COLUMN_PATCHES` mechanism + `PRAGMA user_version`; atomic `claim_next_work_item` (RETURNING); `busy_timeout`; Ollama timeout/IncompleteRead → `OllamaError`; 2 UI escaping sinks; error-body leaks cleaned; defensive limit clamp in `founder._list`. Tests: `test_stage4_fixes.py`.
- **Stage 5 — safety-critical coverage gaps: DONE.** Trading SQLite read-only validator (writes/escalation/stacked/`load_extension` blocked; literal+comment masking anti-bypass), live-trade deny boundary (`_normalize_dt_trigger_proposal`), and denied/irreversible dispatch. Tests: `test_stage5_coverage.py`. (Ollama HTTP + forward-migration covered in Stage 4.) Remaining lower-risk gap: `teaching.py` unit coverage.
- **Deferred to a dedicated UI pass:** RC1 full (mandatory-token CSRF bootstrap) and `index.html` external-CDN removal + CSP — coordinated backend+UI changes, done carefully rather than mid-session.
- **Stage 6 — Synthetic Intelligence Engine: IN PROGRESS (7 increments done).** (1) Bayesian belief updating in log-odds space (LLR by grade×strength, no forced floor, prior-weighted); (2) contrarian loop closed — a red-team review of an assumption/bet now actually moves its confidence + logs a confidence event; (3) surfacing dedup (one row per subject) + datetime-correct "oldest" ordering; (4) prediction calibration upgraded to Brier score + over/under-confidence directional bias; (5) **multi-object belief updating** — evidence added to an experiment now Bayesian-moves *every* linked belief (goal, bet, and assumption), each logged as its own confidence event, not just the one assumption the evidence names (`founder.apply_evidence_confidence_to`, wired through `experiments._link_evidence_to_targets`, guards against double-moving the evidence's own assumption); (6) **audit-payload redaction** — secret-named keys (`password`/`token`/`api_key`/…) are `[redacted]` at any depth before hitting the plaintext audit table, `*_env` pointers exempt (`db._redact_secrets`); (7) **central egress allowlist** — new `netguard.py` is the one chokepoint every outbound call funnels through (loopback-http carve-out for ICS, `require_https`+host-allowlist for the two cloud voice APIs, `allow_private` for local Ollama + LAN SMS gateway; DNS resolved fail-closed, redirects refused). Tests: `test_stage6_si.py` (8), `test_netguard.py` (6), redaction case in `test_security_hardening.py`.
  - **Encryption-at-rest: DEFERRED by decision (2026-07-12).** Founder chose to skip it for now rather than take on SQLCipher/OS-level/field-encryption trade-offs mid-remediation; audit-payload redaction landed as the contained piece of that track. Revisit as a deliberate RC4 feature.
- **Stage 7 — UI pass (RC1 + CSP): DONE, with a live-caught follow-up fix.** (1) **Mandatory-token bootstrap** — an install left at defaults no longer leaves mutations silently open: the kernel mints + persists a random `local_token` under the state dir on first boot (`api._resolve_local_token`), the loopback-only `GET /session/token` hands it to the UI, and all five hand-written pages auto-load it into `localStorage.zadeKernelToken` so `X-Zade-Token` rides every mutation with no manual paste (an explicit token or `protect_mutations=false` still wins). (2) **Strict security headers on every response** (incl. 401s) via one middleware: a `default-src 'self'`/`connect-src 'self'` **CSP** that blocks every external load or exfil the browser could attempt, plus `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`, `COOP`. Tests: `test_ui_security.py` (5). Functional suite kept green via a conftest fixture that no-ops the mint (models a trusted local client); auth behavior is tested explicitly.
  - **Correction (2026-07-12, found via live browser verification, not automated tests):** the initial CSP (`script-src 'self' 'unsafe-inline'`) silently broke the Command dashboard (`ui/index.html`) — my read of it as a static, `eval`-free artifact was wrong. It dynamically `import()`s its own compiled component modules from same-origin `blob:` URLs it creates itself, and its component JSX is compiled client-side through an in-browser Babel transform that executes via `eval`. Without `blob:` and `'unsafe-eval'` in `script-src`, the dashboard silently stuck on its pre-hydration placeholder (raw `{{ binding }}` text visible, no console error) — a regression that shipped invisibly because no automated test exercises a real browser's dynamic-import path. Fixed: `script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:`. `connect-src`/`default-src` still block all external egress, so the widened eval/blob allowance doesn't open a network exfil path. Regression test updated in `test_ui_security.py`.
- **Stage 8 — Voice wired into the Command dashboard's Conversation tab: DONE.** A floating mic + auto-speak widget now lives on the dashboard's built-in chat screen (not just the standalone `voice.html`). Because `ui/index.html` is a separately-compiled artifact (no accessible source tree, only the bundled output — see Stage 7 correction above), voice is wired at the DOM/network boundary rather than inside the compiled chat component: a small inline `<script>` appended to `index.html`'s (uncompiled) outer HTML (a) intercepts `window.fetch` for `/runtime/respond` POSTs to catch replies and speak them via `/voice/speak` when auto-speak is on, without scraping unstable message-bubble markup; (b) drives the real `[data-testid="chat-input"]`/`chat-send"]` elements after `/voice/transcribe`, so voice-originated messages flow through the dashboard's actual pipeline and appear in the real thread, not a parallel one; (c) polls to re-attach the widget after the bundler's `documentElement.replaceWith` swap and to show/hide it based on whether the chat input is currently mounted. Two prior native-integration attempts (editing the dashboard's own JSX-like template) were abandoned after hitting real, undocumented rendering quirks in that closed framework — reverted cleanly rather than shipped partially-working. Verified live end-to-end: token bootstrap, a real chat round-trip, and the fetch-interception → `/voice/speak` call firing on every reply.
- **Lower-priority:** `teaching.py` coverage, pervasive `limit` Query bounds, response-envelope consistency, `restore-backup.ps1` validation.

## Verdict headlines

- **Trading-bot is safe within Zade's boundary.** No code path places an order, executes a trade, or moves money. Single subprocess entrypoint (`shell=False`, `shlex.quote`d args), read-only command allowlist, SQLite worker opened `mode=ro` + `PRAGMA query_only=ON`, autonomous runner provably cannot reach the trading handler, and a guard rejecting `place order`/`live trade`/`broker` operations. The one external write is an advisory-row append via a bot-owned CLI, genuinely approval + typed-phrase gated. **Residual risk is the external, unpinned bot component**, not Zade.
- **SQL is fully parameterized; UI is mostly well-escaped; tests are high quality** (no mock-of-our-own-code, no clock flakiness). These are strengths to preserve.
- The real exposure clusters into **four cross-cutting root causes** (below) plus **several concrete bugs in modules built this session**.

---

## Cross-cutting root causes (each flagged by multiple agents)

### RC1 — The mutation guard is off by default, and the "human gate" is a public constant
`SecurityConfig.local_token` defaults to `""` (config.py:88), which makes `_mutation_requires_token` return `False` (api.py:1668-1673) — so on a default install **every POST/PUT/PATCH/DELETE is unauthenticated**, even though `protect_mutations=True`. Compounding: the typed confirmation phrase (`"make the jump to hyperspace"`) is a fixed constant *returned by* `/health` and `/authority`. So the approve→dispatch "founder types the phrase" control is a UI speed-bump, not access control — any local process can drive it. Reads are never token-gated at all. Safe today only because of the `127.0.0.1` bind (itself a config default, `COFOUNDER_HOST`-overridable). Token compare also uses `!=`, not `hmac.compare_digest` (api.py:234).
**Fix:** auto-generate a `local_token` on first run; stop returning the phrase from `/health`/`/authority`; constant-time compare; refuse (or loudly warn) at startup if host is non-loopback.

### RC2 — Authority taxonomy: over-matches business words, under-covers danger, wrong ordering
- **False denials:** `DENY_ACTION_TOKENS` matches bare `order/pay/buy/sell/transfer` — so a benign `memory.order_by_date` or `founder.sales_order.review` gets **DENIED** as a "hard safety boundary." (authority.py:304-328)
- **Bypass:** the local-prefix allow (authority.py:98-103) runs *before* external-capability screening (:113), so `self.install`, `runtime.upload`, `skills.download`, `work.http` at L0/L1 short-circuit to **ALLOW**. `dev.git.commit`/`dev.git.branch` (repo mutations) are graded non-external because `git` isn't in `APPROVAL_ACTION_TOKENS`.
- **Under-coverage:** `exec/execute/run/subprocess/eval/wsl/docker/ssh/curl/wget/rm` absent from approval tokens; `stripe/paypal/charge/checkout/refund` absent from deny tokens. `summary()` claims payments are "denied" — overstated.
Safe today only because the dispatch table is tiny and every registry handler independently requires the typed phrase. The *gate decisions themselves are wrong.*
**Fix:** run approval-token screening before the local-prefix allow; scope deny-tokens so they don't trip on local-prefixed actions; add the missing danger verbs; classify `dev.git.*` as approval-required; ideally move to an explicit capability registry keyed off the handler's declared capability, not the free-form action string.

### RC3 — `migrate()` is not version-aware
db.py:132 runs only `CREATE TABLE IF NOT EXISTS` (66 of them) — **0 `ALTER TABLE`, 0 `PRAGMA user_version` read**; `SCHEMA_VERSION=21` is stamped but never checked. New *tables* upgrade fine (which is why every schema change this session was safe — they were all new tables), but the **next time anyone adds a column to an existing table, upgraded DBs silently won't get it** → `no such column` at runtime. No live impact today; a latent upgrade trap.
**Fix:** version-aware incremental migrations (or `PRAGMA user_version` branching) + a startup `PRAGMA table_info` self-check that fails loudly on drift.

### RC4 — Plaintext sensitive data at rest (the biggest gap vs. the privacy thesis)
`cofounder.sqlite` holds memories, connector-imported **email/calendar bodies**, message/PR **drafts**, conversation transcripts, and the **audit log** — all plaintext. The product's thesis is "security is the product." (This is the "encryption at rest" track we discussed earlier.)
**Fix:** SQLCipher or OS-level (BitLocker covers disk-at-rest today; app-level adds process isolation + the E2EE-sync foundation). Note the real tension with semantic search over ciphertext — needs a careful design.

---

## Confirmed defects (ranked)

### P0 — fix first (real exposure)
| # | File:line | Defect | Fix |
|---|-----------|--------|-----|
| 1 | config.py:88 / api.py:1668 | RC1 — mutation guard off by default; phrase is public | auto-gen token, hide phrase, constant-time compare |
| 2 | api.py:1539-1551, models.py:30-38 | `/ingest/file\|folder` accept absolute paths + `..`, read arbitrary files outside authority/approval | confine to configured roots; reject traversal (mirror devtools) |
| 3 | handlers.py:199-211 | `local.file.write` can overwrite `data_dir/cofounder.sqlite` + audit trail | exclude DB/blobs/voice/config from writable roots |
| 4 | connectors.py:504-507, handlers.py:167 | SSRF — `startswith` host check (`localhost.evil.com` passes); internal/loopback/cloud-metadata reachable; redirects followed; approval never shows the URL | `urlparse`+exact host match, block private/link-local IPs, disable redirects, surface URL in approval |

### P1 — high (bugs in this session's modules + safety-adjacent)
| # | File:line | Defect |
|---|-----------|--------|
| 5 | actions.py:397-416 + founder.py:677 | Failed action steps write grade-A `claim_contradicted` evidence → `detect_thesis_conflict` fires with no linked assumption → **phantom "Evidence contradicts an assumption" in the thesis ledger + attention queue** |
| 6 | commitments.py:299-301 | `_is_overdue` string-compares a date-only `due_at` against a full timestamp → **a commitment due *today* is overdue from 00:00** + fires a notification |
| 7 | critic.py:213-218 | `confidence_adjustment` clamp allows `+10` — the contrarian pass can *raise* confidence, inverting its purpose (contract is `-50..0`) |
| 8 | evals.py:313 | A case with an empty `checks` list is graded **`fail` unconditionally** → drags pass-rate, emits phantom `newly_failing` |
| 9 | evals.py (generation) | The "regression harness" runs generation at `temperature=0.2` (non-deterministic) → `newly_failing`/`newly_passing` flip on sampling noise, defeating the determinism guarantee |
| 10 | notify.py:252-271 | SMS `gateway_url` has no scheme/host validation — recipient is whitelisted, **destination host is not** |
| 11 | devtools.py:248-263 | Arg validation permits leading-`-` flags → `pytest --junit-xml=x` / `git diff --output=x` write files; "read-only diagnostics" claim not strictly true |
| 12 | connectors.py:25,66 | Secret-blocking is an incomplete denylist — `app_password`, `client_secret_value`, `pwd` slip through → stored plaintext in `config_json`, returned by `get_connector` |
| 13 | ui/index.html | The 796 KB `/ui` bundle pulls **external** unpkg React + Google Fonts into the token-bearing origin → breaks local-first, CDN compromise = token theft |
| 14 | db.py:132 | RC3 — no migration engine |

### P2 — medium (robustness, correctness, consistency)
- **autonomy.py:187-219** — work-item claim is not atomic (SELECT pending then separate UPDATE) → double-dispatch under concurrency (scheduler + API).
- **ollama.py:78-85** — read `TimeoutError`/`IncompleteRead` not wrapped in `OllamaError`; callers catching `OllamaError` miss stalls.
- **db.py:117-130** — no `PRAGMA busy_timeout`; check-then-insert (`enqueue_work_item`, `upsert_document`, `ensure_approval_request`) races → `IntegrityError` instead of returning the existing row.
- **founder.py:674-679** — evidence only updates *assumption* confidence; goals/bets/predictions never move; `linked_decision_id` stored but never used.
- **founder.py:1894** — `_row_to_dict` deserializes NULL list-columns to `{}` not `[]` → `== []` checks silently miss (latent).
- **critic.py:221-232** — `unparsed` verdict shown to founder but silently rewritten to `proceed_with_changes` on persist; `rfind("}")` can over-span into trailing prose.
- **surfacing.py:519-547** — "oldest"/delta rely on lexicographic ordering across mixed date formats (date-only vs full timestamp); same-second `created_at > since` can drop changes; overdue+drifting commitment surfaces as two rows.
- **API validation (models.py + api.py):** every GET `limit` is unbounded (`?limit=-1` → SQLite "no limit" = full-table dump); most free-text fields have no `max_length`; lists/metadata unbounded; `permission_tier`/`reliability`/`severity` are free strings not enums; many `POST /founder/*` creates miss `ValueError→400` (→ 500 + stack leak); response envelopes inconsistent (`{item}` vs `{id,item}` vs bare vs list).
- **UI escaping inconsistencies:** founder.html:257 (`status`) and skills.html:233 (`risk_tier`) interpolate into `innerHTML` unescaped (every sibling field is escaped).
- **Error bodies leak internals:** api.py:1517/1536/1543/1550 return `result.__dict__` / raw result as `detail`.
- **scripts/restore-backup.ps1** — hardcoded DB path, `Copy-Item -Force` overwrites live DB without validating the source is a SQLite file.

---

## Test coverage gaps (ranked by risk of the untested code)
1. **Filesystem-sandbox DENY branch — 0 coverage.** `_is_relative_to` reject paths (handlers.py:209, connectors.py:497, ingestion.py:364) — every test writes *inside* a root. The core sandbox's reject behavior is unverified.
2. **Trading SQLite read-only validator** — only `UPDATE` is tested; INSERT/DELETE/DROP/ATTACH/`load_extension`/stacked-`;`/comment-masking anti-bypass — the exact attack surface — untested.
3. **Live-trading deny boundary** (trading_bot.py:2824) — the trade-safety deny path has **no test**.
4. **approval.py:403** — dispatch of a denied/irreversible item is never exercised (wrong-phrase path *is* well covered).
5. **DB forward-migration** — only fresh DBs are migrated in tests.
6. **ollama.py** — the one external HTTP boundary is 100% monkeypatched (timeouts/non-200/malformed JSON untested).
7. **teaching.py (665 lines) — 0 test references**; experiments.py / ops.py / config-error-paths thin.

---

## Enhancement opportunities

### Security / privacy hardening
- **Encryption at rest** (RC4) · **central egress allowlist + private-IP guard** for every outbound call · **carve kernel state (DB/blobs/voice/config) out of writable roots** (fixes P0#3) · **positive secret schema** for connectors (allowlist non-secret keys; reject secret-shaped values) · **redact audit/error payloads** before persisting to the queryable plaintext audit table.

### Reasoning quality (the "co-founder brain")
- **Bayesian confidence updating** — replace the flat ±1 nudge (founder.py `_confidence_delta`) with log-odds/prior-weighted updates by reliability×strength; track per-assumption supporting/contradicting balance.
- **Close the contrarian loop** — actually *apply* `confidence_adjustment` to the linked object (today it's persisted but moves nothing), + one repair-reprompt on malformed critic JSON.
- **Deterministic evals** (P1#9) — generate at `temperature=0`, or grade N samples to a pass-rate threshold; without this the regression harness can't tell a real regression from noise.
- **Richer calibration** — add Brier score + binned reliability to prediction scoring.
- **Surfacing normalization + dedup** — one datetime helper for all comparisons; dedupe items pointing at the same subject.
- **Gate `detect_thesis_conflict`** — require a linked assumption so failures/unlinked observations stop manufacturing conflicts (fixes P1#5), and let severity actually reach `red`.

### Data / infra
- **Version-aware migrations** (RC3) · **atomic upserts** (`INSERT … ON CONFLICT … RETURNING`) to kill the check-then-insert races and double-dispatch · **indexes** on `work_items(status,priority,id)` and `approval_requests(status)` · `PRAGMA busy_timeout=5000` · **FTS delete/update triggers** before any delete path is added (latent today) · **`tests/conftest.py`** to de-duplicate fixtures across all 22 test files · declare **`pydantic`** explicitly in pyproject.

### Trading-bot trust boundary
- **Read back the appended `dt_recommendations` row** after ingest (read-only SQLite already available) + **pin/verify the external bot's git commit** before allowing dispatch.
- Replace **regex ticker-scraping** of free-text stdout with structured JSON from `ops_check.py` (spurious tokens currently become spurious buy advisories).
- Add `enable_load_extension(False)` + `PRAGMA trusted_schema=OFF` + a bot-side `timeout` wrapper.

### UI / DX
- **Vendor the two React files + self-host fonts + add `Content-Security-Policy: default-src 'self'`** (fixes P1#13 and neutralizes the two escaping sinks) · make `esc()` usage uniform · generate `/self-inventory` route lists from `app.routes` so docs can't drift.

---

## Recommended remediation order
1. **P0 batch (RC1 + #2 + #3 + #4)** — the default-off guard, arbitrary-path ingest, DB-overwrite, and SSRF. Highest exposure, mostly small changes. Add the missing DENY-branch tests alongside.
2. **RC2 authority hardening** — restructure the taxonomy + ordering; add tests for the bypass/false-deny cases.
3. **P1 session-module bugs (#5–#12)** — phantom conflicts, date-only overdue, contrarian clamp, eval determinism, SMS host check, devtools flags, connector secrets.
4. **RC3 migrations + P2 robustness** (atomic claim, busy_timeout, ollama wrap, validation bounds).
5. **UI local-first fix (#13)** + the two escaping sinks.
6. **Enhancement tracks** (encryption at rest, Bayesian reasoning, deterministic evals) as deliberate features.
