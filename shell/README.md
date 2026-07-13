# Zade Shell — the desktop universe frame (Phase 1)

Tauri v2 native shell around the co-founder kernel. The kernel stays a separate
loopback service (FastAPI, `127.0.0.1:8787`); the shell spawns and supervises it
as a sidecar, then frames the kernel-served web UI (`/ui`). See
`../DESKTOP-UNIVERSE-DESIGN.md` for the full design and ambition ladder.

## What Phase 1 gives you

- **Resident window** — closing the window hides it to the tray; Zade stays running.
  Real exit is the tray's *Quit shell* (which still leaves the **kernel** running).
- **Kernel sidecar supervision** — on boot and every 20s the shell checks
  `GET /health`; if the kernel is down it spawns `.venv\Scripts\python.exe -m
  cofounder_kernel` (45s respawn cooldown). Mirrors `scripts/supervise.ps1`.
- **Global summon** — `Ctrl+Alt+Z` from anywhere toggles the window
  (show+focus, or hide if already focused).
- **Single instance** — launching a second copy focuses the existing window.
- **Window-state continuity** — geometry/position persist across runs.
- **Splash → universe** — a local splash probes the kernel (opaque `no-cors`
  fetch; the kernel keeps its no-CORS loopback posture) and hands off to
  `http://127.0.0.1:8787/ui/index.html` once it answers.

## Layout

- `dist/` — the splash page (the only bundled frontend; the real UI is kernel-served)
- `src-tauri/` — Rust shell: `src/main.rs` is all of it
- `src-tauri/icons/` — generated icon set (see repo scratch script `make_icons.py` pattern)

## Build & run

Prereqs: Rust stable-msvc (rustup), Node (for the Tauri CLI), WebView2 (in Win11),
VS Build Tools C++.

```powershell
cd shell
npm install            # once — installs @tauri-apps/cli
npx tauri build        # release exe + NSIS installer
# or for a dev loop:
cd src-tauri; cargo build; .\target\debug\zade-shell.exe
```

Artifacts land in `src-tauri/target/release/zade-shell.exe` and
`src-tauri/target/release/bundle/nsis/`.

The kernel repo root is found by walking up from the exe (works for in-repo
builds); override with the `ZADE_ROOT` env var for an installed copy.

## Phase 2 (next)

Command palette + global capture, home-as-place, panels for the API-only
capabilities (browser flows, vault, research), notification cards, theme/motion
identity. These are web-UI + kernel work rendered inside this frame.
