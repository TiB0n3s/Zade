# Zade as Its Own Universe — Desktop Environment Design & Decision Doc

Date: 2026-07-13
Owner: founder (decision) · drafted for the Deep Thought decommission plan (the resident-shell capstone)
Status: **DECIDED 2026-07-13** — shell: **Tauri** (founder delegated the pick with the criterion "memory unconstrained, learning organic" — both live in the kernel, so the shell that keeps the kernel first-class as a sidecar wins); ambition: **L2 now, architect for L3**; no pywebview Phase-0 (straight to the real shell). Phase 1 build lives in `shell/`.

---

## 0. The ask, restated

Not "a native window around the web UI." You want Zade to be **its own universe** — a place you *inhabit*: resident, immersive, always a keystroke away, with its own identity and continuity, that orchestrates your memory, decisions, and governed actions. The desktop app is the body; the universe is the feeling of living inside it.

This doc exists so you can **weigh options** deliberately, across two separate decisions that usually get tangled:

1. **The universe** — the product/design/continuity that makes it feel like a world. *(This is ~70% of the outcome and is mostly shell-agnostic.)*
2. **The shell** — the technology that hosts and renders it. *(This is ~30%, and it's reversible-ish — see §5.)*

The single most important idea in this document: **don't let the shell debate eat the vision.** A soulless wrapper is possible in any framework; a genuine universe is achievable in most of them. Decide the *universe* first, then pick the frame that best serves it.

---

## 1. What "its own universe" means (the North Star)

Concrete properties, so we're designing against something real:

- **Resident, not visited.** Always running (kernel + shell), lives in the tray, boots with the machine. You don't "open Zade" — Zade is already there. *(Tray + boot already built.)*
- **One keystroke away.** A global summon hotkey + command palette: capture a thought, ask, or act from anywhere, over any app, without context-switching.
- **A place, not a form.** Its own chrome, spatial "home," consistent design language, motion, and identity. You return to somewhere, not to a list of endpoints.
- **Continuity.** Remembers window/session state; greets you (morning digest); is proactive (surfacing, notifications, nudges) — the environment has memory and initiative, not just screens.
- **Ambient senses (future).** Voice (wake-word, parked) and screen-awareness plug in as the universe's ears and eyes.
- **Everything-is-a-surface.** Browser flows, vault, research, ledger, approvals become *panels/apps within the world*, not separate URLs.
- **Sovereign & private.** Local-first, yours, governed — the universe answers to you and runs on your machine.
- **Extensible.** New capabilities dock into the universe as surfaces without reshaping it.

If a design choice doesn't advance one of these, it's shell-polish, not universe.

## 2. Current reality (grounded, 2026-07-13)

- **Kernel:** 36-module FastAPI service on loopback — memory, ledger, authority/approvals, and the browser/vault/research handlers built this month. API-first and complete.
- **UI:** 14 hand-built static HTML pages served at `/ui`.
- **Native integration already built:** system tray, OS toasts, boot-on-login (pystray + Scheduled Task), plus a mature `scripts/` suite (start/stop/status/supervise/install-\*).
- **The gap:** no native window, no summon/palette, no cohesive shell, no packaging. Today Zade is "browser-UI-first + a status tray." That is the *opposite* of a universe — it's the thing you explicitly don't want it to stay.

**The good news:** the kernel + web UI are the reusable core under *every* option below. The universe is built on top of them; the shell wraps them. Almost nothing already built is thrown away by going universe-scale.

## 3. How far is "universe"? (the ambition spectrum)

Pick a target on this axis — it bounds everything else:

| Level | What it is | Feel | Reversible? |
|---|---|---|---|
| **L1 — Resident app** | Native window + tray + palette; you alt-tab to it | "A real app that's always there" | Fully |
| **L2 — Immersive mode** | L1 + a full-screen, chrome-light "world" mode you can live in all day | "An environment I switch into" | Fully |
| **L3 — Primary environment** | L2 + deep OS hooks: global capture, share targets, file associations, always-on ambient layer | "My daily driver surface" | Mostly |
| **L4 — Custom Windows shell** | Replace `explorer.exe` / boot-to-Zade kiosk on a dedicated machine | "A device that *is* Zade" | Risky/hard |

**Recommendation on ambition:** aim for **L2 now, architect so L3 is reachable, treat L4 as a someday-maybe on dedicated hardware.** L4 (replacing the Windows shell) is technically possible but brittle, hard to recover from, and buys little over a great L2/L3 you can also full-screen. Don't design your primary machine into a corner.

## 4. Architecture stance (independent of shell choice)

Keep the **kernel as a separate loopback service; the shell is a client.** Reasons:
- Everything built this month is API-first — a shell just consumes it.
- Multiple surfaces (main window, tray, palette, CLI, a future phone app) all talk to the *one* kernel.
- Lifecycle is clean: the shell **spawns and supervises** the kernel as a sidecar (the `supervise.ps1`/`start.ps1` logic already exists to lift).
- Swapping shells later doesn't touch the kernel — which is what makes the shell choice reversible-ish.

```
        ┌───────────── the universe (surfaces) ─────────────┐
Shell → │ home · palette · panels · ambient · notifications │  ← renders web UI and/or native
        └───────────────────────────────────────────────────┘
                 │ spawns/supervises        │ HTTP (loopback)
                 ▼                           ▼
        Zade Kernel (FastAPI)  ◀── tray (built) ── same kernel
                 │
              Ollama (local models — the brain, separate service)
```

## 5. The shell decision — weighing the options

Five real contenders. The two axes that matter: **universe ceiling** (how immersive/bespoke it can feel) and **effort/reuse** (does it reuse the Python kernel + web UI, and what language tax).

| Shell | Language tax | Reuses web UI? | Universe ceiling | Footprint | Native depth | Effort |
|---|---|---|---|---|---|---|
| **pywebview** | none (Python) | Full | Low–Med | Small | Modest | **Low** |
| **Tauri** (v2) | Rust glue (thin) | Full | **High** | **Smallest** | High | Med |
| **Electron** | JS/Node | Full | **High** | Heavy (~150MB) | High | Med |
| **Flutter** desktop | Dart (UI rewrite) | **No** | **Highest** | Med | High | High |
| **Avalonia / WinUI** | C#/XAML (UI rewrite) | **No** | High–Highest | Med | Native | High |

### pywebview — the fast stopgap
Pure-Python native window around `/ui`, using Win11's built-in WebView2. Composes with the tray you built.
- **For:** ships resident in days; zero new language; least risk.
- **Against:** you get a *windowed web app*, not a *universe*. Limited control of chrome, multi-window, overlays, effects. Given your explicit ask, this is a **Phase-0 proof-of-life, not the destination.**

### Tauri (v2) — the recommended universe shell
Rust core + system webview + a rich native plugin set (multi-window, global-shortcut, tray, notifications, autostart, deep-link, auto-updater), and **sidecar** support to bundle/run the Python kernel.
- **For:** best *universe-per-effort that reuses your web UI*; smallest, most secure, modern; frontend can stay HTML now and grow into Svelte/React later; the Rust is mostly config + spawning the sidecar, not app logic.
- **Against:** a little Rust in your life; smaller example-corpus than Electron for the most exotic chrome.

### Electron — the pragmatic powerhouse
Chromium + Node bundled. Powers VS Code, Obsidian, Slack, Discord, Notion — the canonical "apps that feel like their own world." **This is what Deep Thought used.**
- **For:** maximum control over bespoke/frameless chrome and overlays; largest ecosystem and docs; JS only; DT-familiar.
- **Against:** heavy (~150MB, higher RAM); Node runtime to manage; the "everyone ships Electron" footprint tax.

### Flutter desktop — the immersive ceiling
Dart + GPU rendering (Skia/Impeller). Not a webview — a fully custom, native-rendered UI with buttery motion. Talks to the kernel over HTTP. **Rewrites the 14 pages in Dart.**
- **For:** the highest "own universe" ceiling — bespoke, gorgeous, 120fps, and you get a mobile app essentially for free.
- **Against:** new language; full UI rewrite; the biggest commitment. Justified only if *feel* is the top priority and you'll invest in it.

### Avalonia / WinUI 3 — the native route
C#/XAML, deeply native (Avalonia is cross-platform). High effort, UI rewrite, best "native Windows citizen" feel. Reasonable if you'd rather bet on the .NET ecosystem than web or Dart. Otherwise dominated by Tauri (for reuse) or Flutter (for feel).

### The call
- **Primary recommendation: Tauri.** It's the best fit for "own universe" *given your constraints* — reuses the kernel and the web UI, stays lean and secure, and has the native depth (multi-window, global hotkey, overlays, updater) that L2/L3 need. The kernel-as-sidecar pattern is exactly how Tauri wants to run a Python backend.
- **Choose Electron instead if** JS comfort, the largest ecosystem, the most bespoke chrome with the least fighting, or DT-familiarity outweigh footprint. It is a completely defensible pick and reaches the same universe ceiling.
- **Choose Flutter only if** immersive *feel* is the non-negotiable top priority and you accept a Dart UI rewrite (bonus: mobile).
- **Use pywebview only** for a Phase-0 "feel it resident this week" if you want momentum while deciding — knowing it's throwaway shell glue (the kernel/UI work survives the switch).

**Decision framework in one line:** *Reuse + lean + modern → Tauri. JS-first + max ecosystem → Electron. Bespoke native feel, willing to rewrite the UI → Flutter.*

## 6. The universe surfaces (what to build ON the shell — the 70%)

These make it a world, and most are shell-agnostic (they're UI + kernel features):

1. **Command palette + global summon** — one hotkey, capture/ask/act from anywhere. The soul of "always there."
2. **Home as a place** — a spatial dashboard you return to: today's brief, attention queue, recent decisions, quick actions. Not a menu.
3. **Panels/apps** — ledger, vault browser, research/daydream, browser-flow console, approvals — as docked surfaces in one world (this is also the missing UI for the browser/vault/research builds).
4. **Ambient presence** — tray (built) → optional slim always-on overlay; morning/away digests (surfacing exists); notification cards as first-class citizens.
5. **Continuity & identity** — window/session memory, greeting, a coherent theme/motion/name/sound. It should feel like *Zade*, a specific place.
6. **Senses (future)** — dock the parked wake-word voice loop and screen-awareness build in as ambient input.

**These deserve the majority of the investment.** The shell is the frame; this is the painting.

## 7. Distribution, packaging, and the Ollama question

- **Installer:** a signed `.exe` (Tauri has a bundler/updater; Electron has electron-builder; pywebview → PyInstaller). Single-instance lock, autostart (extend the tray Scheduled Task), auto-update.
- **The kernel sidecar:** ship the Python kernel bundled (PyInstaller-frozen) or as a managed venv the shell supervises. Lift the existing `start/stop/supervise` scripts into the shell's lifecycle.
- **Ollama is the real wrinkle.** The "brain" is a separate local service the universe depends on. Options: (a) **prerequisite** — detect it, guide install, degrade gracefully if down (the tray already shows an "error" state when it's unreachable); (b) **managed** — the installer installs/starts Ollama for you; (c) **bundled** — ship model + runtime (large). Recommend **(a) → (b)**: prerequisite now, managed later. Flag this early because it defines how "one-click" the universe can be.

## 8. Phased roadmap

- **Phase 0 (optional, days): proof-of-life.** pywebview window at `/ui` + minimize-to-tray + summon hotkey. Feel it resident. Throwaway shell glue; keep only if you don't commit to Tauri/Electron immediately.
- **Phase 1 (the shell): the frame. ✅ BUILT + LIVE-VERIFIED 2026-07-13** — Tauri 2.11.5 app in `shell/` (see `shell/README.md`): kernel-as-sidecar supervision, single-instance, tray, Ctrl+Alt+Z global summon, window-state plugin, close-to-tray, splash→`/ui` handoff, release exe + NSIS installer. Not yet observed: geometry persistence across graceful quit; installer not run.
- **Phase 2 (the universe, ongoing): the world.** Command palette, home-as-place, the panels (incl. the missing browser/vault/research UIs), notification cards, theme/motion/identity, digests. *This is where "universe" is won.*
- **Phase 3 (distribution): the product.** Signed installer, auto-update, Ollama prerequisite/manager, autostart, L2 immersive full-screen mode.
- **Later / optional:** senses (voice + screen), L3 deep OS hooks, Flutter mobile companion, L4 only on dedicated hardware.

## 9. What this means for decommissioning Deep Thought

This is the **capstone** of the DT replacement. The tray closed part of "resident desktop shell parity"; this closes the rest — and more importantly, it's what makes Zade the *emotional* successor to DT, not just the functional one. DT's whole identity was "a resident presence you live with." Shipping the universe is what lets you finally retire DT without feeling a loss of *place*.

## 10. Open questions for you (these gate the build)

1. **Shell:** Tauri (recommended), Electron (JS-first), or Flutter (bespoke feel, UI rewrite)? — or a pywebview Phase-0 first?
2. **Ambition level:** target **L2 (immersive mode)** as planned, or push for **L3 (primary environment)** sooner?
3. **UI reuse:** keep/evolve the 14 HTML pages (Tauri/Electron), or invest in a fresh frontend (a rebuild in web, or Dart/native)?
4. **Ollama distribution:** prerequisite (detect + guide) for v1, or managed installer from the start?
5. **Do you want Phase 0** (pywebview, feel-it-this-week), or go straight to the real shell?

Answer 1, 2, and 5 and Phase 1 is unblocked.
