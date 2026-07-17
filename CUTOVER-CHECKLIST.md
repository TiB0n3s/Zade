# Deep Thought → Zade cutover checklist

**Status: NOT YET RUN.** First materialized 2026-07-16 — until now this gate
existed only as "a live cutover checklist passes" in the decommission decision
(2026-07-13 audit session). Deep Thought stays **installed-but-idle** (do not
delete its scheduled tasks, startup links, secrets, or runtime data) until every
gate below passes in a live run, top to bottom, in one sitting.

Founder decisions baked in: wake-word voice is PARKED (identity decision, not a
blocker while parked); Outlook send/mutation is accepted as lost; safety-spine
hardening is deferred (residual L3 risk accepted).

---

## Gate A — capability parity (the 7 required builds)

| # | Capability | Build status (as of 2026-07-16) | Cutover check |
|---|------------|--------------------------------|---------------|
| 1 | Wake-word / hands-free voice loop | **PARKED** by founder pending a wake word | Excluded while parked. Before *deleting* DT (not just idling it), founder re-confirms the park or the loop ships. |
| 2 | Screen awareness | Shipped + live-verified 2026-07-15 (`screen.py`, Ops · Screen) | [x] **2026-07-16:** `POST /screen/capture` 200 — real focused window ("3D Prints - File Explorer") + 30 window titles |
| 3 | Resident presence: tray, boot-on-login, OS toasts | Shipped; superseded in practice by the Tauri shell (autostart, tray, native WinRT toasts) | [x] **2026-07-16:** Run key "Zade" present, shell resident, fresh `POST /notify` visible unread in `/tray/state` within one poll cycle. **Caught + fixed a real defect:** the Run key targets `target\release\zade-shell.exe` but only the debug exe existed (release deleted at some point) → next login would have silently failed; release exe rebuilt same day. Toast *display* remains founder-verified 2026-07-15 (not headlessly assertable). |
| 4 | Headed browser automation | Shipped + live-verified 2026-07-13 (`browser.py`, L3 gated) | [x] **2026-07-16:** approved `external.browser.run` (navigate→read→screenshot on example.com) dispatched ok; real PNG at `C:\AI Brain\Zade\browser-captures\capture-2.png` |
| 5 | Vault move/delete operator (trash + dry-run) | Shipped + live-verified 2026-07-13 (`vault.py`, L2 gated) | [x] **2026-07-16:** scratch file in `inbox/`: dry-run plan (guards passed) → approved move → approved delete (trash) → restore — sentinel content intact end-to-end |
| 6 | Specialist agent swarm (hybrid + auto-invoke) | First slice shipped + live-verified 2026-07-15 (`roles.py` + `delegation.py`); native local coding agent is now the default engine | [x] **2026-07-16:** red_team role pass on qwen3:14b (5.4s, verdict "Unsound assumption" — fittingly, that one checklist pass isn't sufficient). Delegated run: work item 46 / evidence 40 same day (Gate B live flip). |
| 7 | Autonomous web research / daydream | Shipped + live-verified 2026-07-13 (`research.py`, egress L3 gated) | [x] **2026-07-16:** daydream derived 3 real topics from assumption gaps; approved fetch (example.com) dispatched ok and filed graded `web_research` evidence id 42 (reliability C, strength 30) |

## Gate B — delegated-work integrity (verification layer, added 2026-07-16)

The first real delegated run (work item #43, evidence 36) filed a fabricated
"Passed TypeScript type checks with `npx tsc --noEmit`" claim verbatim. The
verification layer (commit `465a5e8`) closes this: Zade's delegated evidence can
no longer silently carry verification claims that were never executed.

- [x] **Artifact claim cross-check** — `delegation.find_unverified_claims()`
      scans every delegated artifact (native AND bridge) for verification
      claims (tsc/type check, npm test, npm audit, build, tests pass/green) and
      cross-checks them against the run's audited ok `run_command` argv.
      Unbacked claims get an explicit **UNVERIFIED CLAIM** marker in the
      evidence notes/metadata, `unverified_claims` in the dispatch result, and
      an audit-trail entry. Bridge artifacts (no audited steps) are unverified
      by construction. *Shipped 2026-07-16.*
- [x] **Kernel-run auto-verification** — after a native run that changed files,
      the kernel itself executes the workspace's real check (`npm test` when
      package.json declares a test script; `python -m pytest -q` for
      pyproject/tests) through the allowlisted, audited, timeout-capped
      `run_command` path and appends the REAL output (exit code + streams) to
      the artifact as a kernel-authored block the model cannot fake.
      *Shipped 2026-07-16.*
- [x] **Regression tests** — 7 tests across `tests/test_delegation.py` +
      `tests/test_coding_agent.py`, including the exact item-43 artifact.
      Suite 425 passed / 1 skipped.
- [x] **Live flip confirmed** — item #43's task re-run as work item 46
      (2026-07-16): kernel auto-ran `npm test` (jest, exit 0) and appended the
      real output; `unverified_claims: []` because every claim was backed by
      the audited step; evidence 40 filed clean. The original item-43 artifact,
      re-checked through the new layer, correctly flags the tsc claim.
- [ ] **At cutover time:** run one fresh delegated task end-to-end and confirm
      the filed evidence carries either a clean cross-check or explicit
      UNVERIFIED CLAIM markers — never an unmarked, unexecuted claim.

## Gate C — shell / universe stability

- [x] Option B bundled-asset shell (v0.4.0) launches, renders all pages,
      frameless chrome, native toasts, autostart — verified 2026-07-15.
- [x] **Mutation path through the Tauri bridge — VERIFIED 2026-07-16.** Driven
      from inside the real shell webview (`http://tauri.localhost/index.html`,
      attached via WebView2 CDP + Playwright, page's own bridged `window.fetch`):
      bridged GET `/session/token` 200 → bridged `POST /runtime/respond` with
      `X-Zade-Token` returned **200** in 3.8s ("Hello, Ellie.", qwen3:14b,
      event 301 / model call 300, full governed response). Negative control:
      the same POST **without** the token → 401 "Local mutation token
      required" — the guard runs on bridged requests and the forwarded header
      is what passes it. (Native cross-origin fetch could never return a
      readable 200 from the no-CORS kernel, so the readable responses are
      themselves proof the requests rode `invoke("kernel_request")`.)
      Operational note: the first attempts hit 503 "Ollama request timed out"
      (180s client cap) — NOT a bridge failure; timed-out generations keep
      running server-side and `OLLAMA_NUM_PARALLEL=1` queues retries behind
      them. Let the GPU drain before judging a 503.
- [ ] Kernel survives a shell force-kill; shell respawns a stopped kernel.

## Gate D — the live cutover run (decommission steps, in order)

- [ ] Run Gates A–C above in one session; every box checked.
- [ ] Inventory DT's footprint: scheduled tasks, Startup links
      (`DeepThought.lnk` — keep `Ollama.lnk`), secrets, runtime data
      (`C:\DeepThought\data`).
- [ ] Archive DT runtime data + secrets before touching anything.
- [ ] Remove DT autostart only (installed-but-idle → idle-and-dormant).
- [ ] Observe an agreed Zade-only period (founder sets the length) with no
      reach-back to DT.
- [ ] Founder signs off; only then delete the DT install.

---

*Update discipline: when a gate item ships or a live check runs, edit this file
in the same commit as the change (or the same session as the verification) and
note the date. This file is the single source of truth for "can DT go yet?" —
the session-memory notes mirror it, not the other way around.*
