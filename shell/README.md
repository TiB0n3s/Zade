# Zade Shell — the desktop universe

Tauri v2 native shell around the co-founder kernel. The kernel stays a separate
loopback service (FastAPI, `127.0.0.1:8787`) and is a **pure API**; the shell
spawns and supervises it as a sidecar, **bundles the UI as its own assets**
(`frontendDist` = `../../ui`, served from `tauri://localhost`), and reaches the
kernel through a Rust proxy bridge. See `../DESKTOP-UNIVERSE-DESIGN.md` for the
full design and ambition ladder.

## Architecture (Option B — UI as bundled assets)

The UI lives on the `tauri://` origin, cross-origin to the kernel. Rather than
reopen the kernel's deliberate no-CORS posture, every kernel call is proxied over
loopback from Rust:

- **`kernel_request` command** — forwards a call to `127.0.0.1:8787` via `ureq`
  and returns status + body. The kernel only ever sees a same-process loopback
  client, so its no-CORS stance stays intact.
- **`window.fetch` override** — injected as a Tauri init script (runs before any
  page script), it rebases root-relative calls (`/health`, `/runtime/respond`, …)
  and routes them through `kernel_request`. The hand-written pages keep their
  existing `fetch("/…")` calls unchanged; the token is fetched over the same
  loopback proxy, so nothing sensitive is exposed to a browser origin.

## What the shell gives you

**The frame**
- **Frameless custom chrome** — the window is `decorations:false`; the UI draws
  its own titlebar (full-width drag region + minimize / maximize / close-to-tray),
  injected by `zade-ui.js` only on the `tauri://` asset origin.
- **Resident window** — closing hides it to the tray; Zade stays running. Real
  exit is the tray's *Quit shell* (which still leaves the **kernel** running).
- **Kernel sidecar supervision** — on boot and every 20s the shell checks
  `GET /health`; if the kernel is down it spawns `.venv\Scripts\python.exe -m
  cofounder_kernel` (45s respawn cooldown). Mirrors `scripts/supervise.ps1`.
- **Global summon** — `Ctrl+Alt+Z` from anywhere toggles the window.
- **Single instance** — launching a second copy focuses the existing window.
- **Window-state continuity** — geometry/position persist across runs.
- **Splash → universe** — `splash.html` (a bundled asset) probes `/health`
  through the bridge, then hands off to `index.html` once the kernel answers.

**The product layer**
- **Native OS notifications** — a watcher polls `/tray/state`, dedups by id (seeded
  silently on start, so no boot-flood), and raises a Windows toast for each new
  unread notification **when the window isn't focused** — so it complements, not
  duplicates, the in-app notification center.
- **Autostart (resident)** — boots with Windows via `tauri-plugin-autostart`.
  Enabled once on first run (a `.autostart-initialized` marker means we never
  re-enable after you turn it off). Toggle from the tray's *Start with Windows*.
  A login-boot passes `--start-hidden`, so Zade comes up quietly in the tray.
- **L2 immersive mode** — `Ctrl+Alt+F` (or the tray's *Immersive mode*) toggles
  full-screen; the OS strips its chrome and the custom titlebar hides (Rust emits
  `zade://immersive`), so you're fully inside the world.
- **Ollama-aware tray tooltip** — a status thread reads `/health` every 15s and
  keeps the tooltip honest: *online* / *brain offline · start Ollama* /
  *waking the kernel…*.

**Phase 3 completion (v0.4.0)**
- **Code signing** — `scripts/sign-shell.ps1` signs the exe + NSIS installer with
  `Set-AuthenticodeSignature` (no SDK needed). Self-signed by default (`CN=Zade
  Local Shell`); set `ZADE_SIGN_THUMBPRINT` to a real OV/EV cert to ship trusted.
  Self-signed shows `UnknownError`/unknown-publisher on other machines — expected
  until the cert is trusted; the pipeline is real.
- **Auto-update** — `tauri-plugin-updater` with a signing keypair
  (`zade-update.key*`, gitignored) and `createUpdaterArtifacts`. The tray's *Check
  for updates* polls a **local update channel**: the kernel serves the manifest at
  `GET /shell/latest.json` (loopback, so `dangerousInsecureTransportProtocol` is
  on — payloads are still signature-verified). Inert by default (version `0.0.0`);
  drop a Tauri manifest at `data_dir/shell-update.json` to publish locally, or
  point `updater.endpoints` at GitHub Releases for remote updates.
- **Ollama managed** — the status watcher auto-starts Ollama when it's installed
  but down; the tray's *Install / start Ollama* installs it via winget (or opens
  the download page) when it's missing.

Build the signed installer + updater artifacts:
```powershell
$env:TAURI_SIGNING_PRIVATE_KEY = Get-Content .\zade-update.key -Raw
npx tauri build
.\scripts\sign-shell.ps1   # Authenticode-sign the exe + installer
```

## Layout

- `../ui/` — the full UI. Bundled as `frontendDist`; also still served by the
  kernel at `/ui` as a browser fallback and as the dev-loop source.
- `dist/` — legacy redirect splash, superseded by `../ui/splash.html` (kept only
  as an unused placeholder; `frontendDist` points at `../../ui`).
- `src-tauri/` — Rust shell: `src/main.rs` is all of it.
- `src-tauri/icons/` — generated icon set.

## Build & run

Prereqs: Rust stable-msvc (rustup), Node (for the Tauri CLI), WebView2 (in Win11),
VS Build Tools C++.

```powershell
cd shell
npm install            # once — installs @tauri-apps/cli
npx tauri build        # release exe + NSIS installer
# or a debug binary:
cargo build --manifest-path src-tauri/Cargo.toml; .\src-tauri\target\debug\zade-shell.exe
```

Artifacts land in `src-tauri/target/release/zade-shell.exe` and
`src-tauri/target/release/bundle/nsis/`. Because the UI is embedded at compile
time, a UI edit needs a rebuild — unless you use the dev loop below.

The kernel repo root is found by walking up from the exe (works for in-repo
builds); override with the `ZADE_ROOT` env var for an installed copy.

### Live UI dev loop (no rebuild per edit)

Option B embeds the UI (`frontendDist` = `../../ui`) into the binary at compile
time, so a normal build must be recompiled to see a UI change. For a fast loop,
point the window at the kernel's **live** `/ui` instead — the kernel already
serves those files from disk with `Cache-Control: no-cache`:

```powershell
# the kernel must be running (it usually is — it's resident)
$env:ZADE_DEV_UI = 'http://127.0.0.1:8787/ui/splash.html'
.\src-tauri\target\debug\zade-shell.exe
```

Edit any file under `ui/` and press **F5** in the window — no rebuild. In this
mode the window keeps **OS chrome** (the frameless custom titlebar needs prod's
`tauri://` IPC, so it's suppressed off the asset origin) and the `fetch` bridge
**self-disables** (the page is same-origin with the kernel, so native `fetch`
reaches it directly). Unset `ZADE_DEV_UI` for the real frameless build. Rust
changes still require `cargo build`.

## Status

Phases 1–3 and the Option B relocation are shipped and live-verified: the frame,
command palette, panels, home-as-place, notification cards, theme/motion
identity, digests, autostart, immersive mode, frameless chrome, the live dev
loop, and native OS notifications. Remaining optional work: signed installer +
auto-update (deferred), managed Ollama install, and the senses (voice + screen).
