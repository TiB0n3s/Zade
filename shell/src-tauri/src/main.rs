// Zade desktop universe shell — Phases 1–3, Option B (UI as Tauri assets).
//
// The kernel (FastAPI on 127.0.0.1:8787) stays a separate loopback service and
// PURE API; this shell is a client that spawns and supervises it as a sidecar,
// then renders the UI as bundled Tauri assets (tauri://localhost). Quitting the
// shell leaves the kernel running: Zade is resident, the window is one surface.
//
// Option B transport: the UI lives on the tauri:// origin, cross-origin to the
// kernel. Rather than reopen the kernel's deliberate no-CORS posture, every
// kernel call is proxied through the `kernel_request` command over loopback from
// Rust. A `window.fetch` override (injected as an init script, so it runs before
// any page script) rebases root-relative calls to the kernel and routes them
// through that command — so the existing hand-written pages need no rewrite.
//
// Phase 3 keeps the "product" layer: autostart (resident, boots with Windows —
// quietly to the tray), an L2 immersive full-screen mode, and an Ollama-aware
// tray tooltip so the prerequisite "brain" surfaces gracefully.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};

use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Emitter, Manager, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_autostart::{ManagerExt, MacosLauncher};
use tauri_plugin_global_shortcut::{Code, Modifiers, ShortcutState};
use tauri_plugin_notification::NotificationExt;
use tauri_plugin_updater::UpdaterExt;
use tauri_plugin_window_state::StateFlags;

const KERNEL_BASE: &str = "http://127.0.0.1:8787";
/// Global summon chord. Z for Zade; Ctrl+Alt avoids the common Ctrl+Shift app space.
const SUMMON_SHORTCUT: &str = "ctrl+alt+z";
/// Immersive (L2) toggle — F for full-screen. Same Ctrl+Alt family as the summon.
const IMMERSIVE_SHORTCUT: &str = "ctrl+alt+f";
/// Passed to the autostart entry so a login-boot comes up quietly to the tray.
const START_HIDDEN_FLAG: &str = "--start-hidden";
const SUPERVISE_INTERVAL: Duration = Duration::from_secs(20);
const RESPAWN_COOLDOWN: Duration = Duration::from_secs(45);
const STATUS_INTERVAL: Duration = Duration::from_secs(15);
const NOTIFY_INTERVAL: Duration = Duration::from_secs(15);

/// Injected into every page BEFORE its own scripts run. Overrides `window.fetch`
/// so the hand-written pages keep calling `fetch("/health")`, `fetch("/runtime/
/// respond", …)` etc. unchanged: root-relative (kernel) calls are routed through
/// the `kernel_request` command over loopback, preserving the kernel's no-CORS
/// posture. Non-kernel/absolute URLs fall through to the native fetch. Resolves
/// the Tauri IPC lazily at call time, so init-script ordering can't race it.
const BRIDGE_JS: &str = r#"
(function () {
  if (window.__ZADE_BRIDGE__) return;
  window.__ZADE_BRIDGE__ = true;
  var KERNEL = "http://127.0.0.1:8787";
  // Frameless only in production. The window is decorations:false and the UI
  // draws its own titlebar ONLY when it is NOT on the kernel (dev) origin. Expose
  // that as a definitive flag — compared against the known, fixed kernel origin,
  // so it's correct whatever Tauri's asset host turns out to be (tauri://localhost
  // vs http://tauri.localhost) — for zade-ui.js to gate the custom titlebar on.
  window.__ZADE_FRAMELESS__ = (window.location.origin !== KERNEL);
  // Dev loop (ZADE_DEV_UI): the UI is served same-origin straight from the
  // kernel, so native fetch already reaches it — don't hijack. The bridge is
  // only needed from the tauri:// asset origin (production).
  if (window.location.origin === KERNEL) return;
  var nativeFetch = window.fetch ? window.fetch.bind(window) : null;
  function toHeaders(h) {
    var out = {};
    if (!h) return out;
    if (typeof h.forEach === "function" && !Array.isArray(h)) { h.forEach(function (v, k) { out[k] = v; }); }
    else if (Array.isArray(h)) { h.forEach(function (p) { out[p[0]] = p[1]; }); }
    else { Object.keys(h).forEach(function (k) { out[k] = h[k]; }); }
    return out;
  }
  window.fetch = function (input, init) {
    init = init || {};
    var url = typeof input === "string" ? input : (input && input.url) || "";
    var isRoot = url.charAt(0) === "/" && url.charAt(1) !== "/";
    var isKernel = isRoot || url.indexOf(KERNEL) === 0;
    if (!isKernel) { return nativeFetch ? nativeFetch(input, init) : Promise.reject(new Error("fetch unavailable")); }
    var path = isRoot ? url : url.slice(KERNEL.length);
    var method = String(init.method || (typeof input !== "string" && input && input.method) || "GET").toUpperCase();
    var headers = toHeaders(init.headers || (typeof input !== "string" && input && input.headers));
    var body = init.body != null ? String(init.body) : null;
    var t = window.__TAURI__;
    if (!t || !t.core || !t.core.invoke) { return Promise.reject(new Error("Zade bridge: Tauri IPC unavailable")); }
    return t.core.invoke("kernel_request", { path: path, method: method, headers: headers, body: body })
      .then(function (res) {
        var h = {};
        if (res && res.contentType) h["Content-Type"] = res.contentType;
        return new Response(res && res.body != null ? res.body : "", { status: (res && res.status) || 502, headers: h });
      });
  };
})();
"#;

/// Proxy a single kernel call over loopback from Rust. Keeps the kernel's
/// no-CORS posture intact (it only ever sees a same-process loopback client) and
/// gives the tauri:// UI a working transport without a CORS reopening. Non-2xx
/// responses are returned with their real status + body so the kernel's 401
/// token hint still reaches the UI.
#[tauri::command]
async fn kernel_request(
    path: String,
    method: String,
    headers: HashMap<String, String>,
    body: Option<String>,
) -> Result<serde_json::Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let url = format!("{KERNEL_BASE}{path}");
        let agent = ureq::AgentBuilder::new()
            .timeout(Duration::from_secs(190))
            .build();
        let mut req = agent.request(&method, &url);
        let mut has_content_type = false;
        for (k, v) in headers.iter() {
            if k.eq_ignore_ascii_case("content-type") {
                has_content_type = true;
            }
            req = req.set(k, v);
        }
        let result = match body {
            Some(b) => {
                if !has_content_type {
                    req = req.set("Content-Type", "application/json");
                }
                req.send_string(&b)
            }
            None => req.call(),
        };
        let (status, ct, text) = match result {
            Ok(resp) => {
                let status = resp.status();
                let ct = resp.content_type().to_string();
                let text = resp.into_string().unwrap_or_default();
                (status, ct, text)
            }
            // Non-2xx: ureq surfaces it as Error::Status but the response body is
            // intact — pass status + body through so the UI sees the real error.
            Err(ureq::Error::Status(code, resp)) => {
                let ct = resp.content_type().to_string();
                let text = resp.into_string().unwrap_or_default();
                (code, ct, text)
            }
            Err(err) => return Err(format!("kernel request failed: {err}")),
        };
        Ok(serde_json::json!({ "status": status, "body": text, "contentType": ct }))
    })
    .await
    .map_err(|err| err.to_string())?
}

/// Native window controls for the (future) custom chrome — invoked from the UI
/// through the same IPC bridge as the kernel proxy.
#[tauri::command]
fn win_minimize(app: AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.minimize();
    }
}

/// Close-to-tray from a custom titlebar button: hide, keep the universe resident.
#[tauri::command]
fn win_hide(app: AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.hide();
    }
}

#[tauri::command]
fn win_toggle_immersive(app: AppHandle) {
    toggle_immersive(&app);
}

/// Maximize/restore for the custom titlebar's control (and titlebar double-click).
#[tauri::command]
fn win_toggle_maximize(app: AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        if w.is_maximized().unwrap_or(false) {
            let _ = w.unmaximize();
        } else {
            let _ = w.maximize();
        }
    }
}

/// Locate the kernel repo root: env override, then walk up from the exe (works
/// for target/debug builds inside the repo), then the canonical path.
fn kernel_root() -> PathBuf {
    if let Ok(v) = std::env::var("ZADE_ROOT") {
        return PathBuf::from(v);
    }
    if let Ok(exe) = std::env::current_exe() {
        let mut dir = exe.parent().map(|p| p.to_path_buf());
        while let Some(d) = dir {
            if d.join("pyproject.toml").exists() && d.join("src").join("cofounder_kernel").exists() {
                return d;
            }
            dir = d.parent().map(|p| p.to_path_buf());
        }
    }
    PathBuf::from(r"C:\LocalAICofounder")
}

/// Read /health once and report (kernel_ok, ollama_ok). The kernel keeps its
/// no-CORS loopback posture, so only a native client (this shell) can read the
/// body — which is exactly why the Ollama prerequisite surfaces here.
fn kernel_status() -> (bool, bool) {
    let agent = ureq::AgentBuilder::new()
        .timeout(Duration::from_secs(3))
        .build();
    match agent.get(&format!("{KERNEL_BASE}/health")).call() {
        Ok(resp) => match resp
            .into_string()
            .ok()
            .and_then(|body| serde_json::from_str::<serde_json::Value>(&body).ok())
        {
            Some(v) => (
                v["ok"].as_bool().unwrap_or(false),
                v["ollama"]["ok"].as_bool().unwrap_or(false),
            ),
            None => (false, false),
        },
        Err(_) => (false, false),
    }
}

fn spawn_kernel() {
    let root = kernel_root();
    let python = root.join(".venv").join("Scripts").join("python.exe");
    if !python.exists() {
        eprintln!("zade-shell: no venv python at {}", python.display());
        return;
    }
    #[cfg(windows)]
    let spawned = {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        Command::new(&python)
            .args(["-m", "cofounder_kernel"])
            .current_dir(&root)
            .creation_flags(CREATE_NO_WINDOW)
            .spawn()
    };
    #[cfg(not(windows))]
    let spawned = Command::new(&python)
        .args(["-m", "cofounder_kernel"])
        .current_dir(&root)
        .spawn();
    match spawned {
        Ok(child) => eprintln!("zade-shell: spawned kernel pid {}", child.id()),
        Err(err) => eprintln!("zade-shell: kernel spawn failed: {err}"),
    }
}

/// Background supervision: if the kernel stops answering, respawn it (with a
/// cooldown so a crash-looping kernel doesn't get hammered). Mirrors
/// scripts/supervise.ps1, lifted into the shell's lifecycle.
fn supervise() {
    std::thread::spawn(|| {
        // Fire immediately on boot so the splash has a kernel to find.
        let mut last_spawn = Instant::now() - RESPAWN_COOLDOWN;
        loop {
            if !kernel_status().0 && last_spawn.elapsed() >= RESPAWN_COOLDOWN {
                spawn_kernel();
                last_spawn = Instant::now();
            }
            std::thread::sleep(SUPERVISE_INTERVAL);
        }
    });
}

/// Poll the kernel and keep the tray tooltip honest about the brain (Ollama).
/// Managed prerequisite: when the kernel is up but Ollama is down AND Ollama is
/// installed, auto-start it (with a cooldown so a broken Ollama isn't hammered).
fn watch_status(app: AppHandle) {
    std::thread::spawn(move || {
        let mut last_ollama_start = Instant::now() - RESPAWN_COOLDOWN;
        loop {
            let (kernel_ok, ollama_ok) = kernel_status();
            let tip = if !kernel_ok {
                "Zade — waking the kernel…"
            } else if ollama_ok {
                "Zade — online"
            } else {
                "Zade — brain offline · start Ollama"
            };
            if let Some(tray) = app.tray_by_id("zade-shell") {
                let _ = tray.set_tooltip(Some(tip));
            }
            if kernel_ok && !ollama_ok && last_ollama_start.elapsed() >= RESPAWN_COOLDOWN {
                if let Some(exe) = ollama_exe() {
                    start_ollama(&exe);
                    last_ollama_start = Instant::now();
                }
            }
            std::thread::sleep(STATUS_INTERVAL);
        }
    });
}

/// Read the kernel's newest unread notifications from /tray/state (loopback, no
/// token — it's a GET). Returns (id, title, body) triples, newest-unread first.
fn fetch_tray_notifications() -> Option<Vec<(i64, String, String)>> {
    let agent = ureq::AgentBuilder::new()
        .timeout(Duration::from_secs(3))
        .build();
    let body = agent
        .get(&format!("{KERNEL_BASE}/tray/state"))
        .call()
        .ok()?
        .into_string()
        .ok()?;
    let value: serde_json::Value = serde_json::from_str(&body).ok()?;
    let arr = value.get("notifications")?.as_array()?;
    let mut out = Vec::new();
    for note in arr {
        let Some(id) = note.get("id").and_then(|x| x.as_i64()) else {
            continue;
        };
        let title = note.get("title").and_then(|x| x.as_str()).unwrap_or("").trim();
        let title = if title.is_empty() { "Zade".to_string() } else { title.to_string() };
        let body = note.get("body").and_then(|x| x.as_str()).unwrap_or("").trim();
        let body = if body.is_empty() { title.clone() } else { body.to_string() };
        out.push((id, title, body));
    }
    Some(out)
}

/// Watch the kernel for new unread notifications and raise a native OS toast for
/// each one the founder hasn't seen — but only when the window isn't focused, so
/// we never double up with the in-app notification center. The first successful
/// poll seeds the seen-set silently (no toast-flood for notifications that were
/// already unread when the shell started); only genuinely new ones toast after.
fn watch_notifications(app: AppHandle) {
    std::thread::spawn(move || {
        let mut seen: std::collections::HashSet<i64> = std::collections::HashSet::new();
        let mut seeded = false;
        loop {
            if let Some(notes) = fetch_tray_notifications() {
                let focused = app
                    .get_webview_window("main")
                    .and_then(|w| w.is_focused().ok())
                    .unwrap_or(false);
                for (id, title, body) in notes {
                    let is_new = seen.insert(id);
                    if is_new && seeded && !focused {
                        let _ = app
                            .notification()
                            .builder()
                            .title(title)
                            .body(body)
                            .show();
                    }
                }
                seeded = true;
            }
            std::thread::sleep(NOTIFY_INTERVAL);
        }
    });
}

fn show_main(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
}

/// Summon semantics: bring Zade forward from anywhere; if it's already the
/// focused foreground window, tuck it away again.
fn toggle_main(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        if w.is_visible().unwrap_or(false) && w.is_focused().unwrap_or(false) {
            let _ = w.hide();
        } else {
            let _ = w.show();
            let _ = w.unminimize();
            let _ = w.set_focus();
        }
    }
}

/// L2 immersive mode: full-screen the world (OS strips the chrome). Emits
/// `zade://immersive` (the new fullscreen state) so the frontend can hide its
/// custom titlebar and reclaim the top strip while immersed.
fn toggle_immersive(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let now = w.is_fullscreen().unwrap_or(false);
        let _ = w.set_fullscreen(!now);
        if !now {
            let _ = w.show();
            let _ = w.set_focus();
        }
        let _ = app.emit("zade://immersive", !now);
    }
}

/// Enable boot-with-Windows on the very first run (Zade is meant to be
/// resident), but never fight the founder: a marker file means we only
/// auto-enable once, so a later "off" from the tray sticks.
fn ensure_first_run_autostart(app: &AppHandle) {
    let marker = match app.path().app_config_dir() {
        Ok(dir) => dir.join(".autostart-initialized"),
        Err(_) => return,
    };
    if marker.exists() {
        return;
    }
    if let Some(parent) = marker.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let _ = app.autolaunch().enable();
    let _ = std::fs::write(&marker, "1");
}

/// Build the resident window in Rust so we can attach the fetch-bridge init
/// script (it must run before any page script). The UI is bundled Tauri assets;
/// the splash probes the kernel then hands off to index.html.
fn build_main_window(app: &AppHandle) -> tauri::Result<()> {
    // Dev loop: set ZADE_DEV_UI to the kernel's live UI (e.g.
    // http://127.0.0.1:8787/ui/splash.html) and the window loads pages straight
    // from disk via the kernel — edit a page, hit F5, no rebuild. In that mode we
    // keep OS chrome (the borderless custom titlebar needs prod's tauri:// IPC)
    // and the fetch bridge self-disables on the kernel origin. Unset = production:
    // bundled tauri:// assets + frameless custom chrome.
    let dev_ui = std::env::var("ZADE_DEV_UI").ok().filter(|s| !s.is_empty());
    let (url, decorations) = match &dev_ui {
        Some(u) => (
            WebviewUrl::External(u.parse().expect("ZADE_DEV_UI is not a valid URL")),
            true,
        ),
        None => (WebviewUrl::App("splash.html".into()), false),
    };
    WebviewWindowBuilder::new(app, "main", url)
        .title("Zade")
        .inner_size(1280.0, 840.0)
        .min_inner_size(900.0, 600.0)
        .center()
        // Frameless in prod: the OS chrome is gone; the UI draws its own titlebar
        // (a full-width drag region + Zade window controls in zade-ui.js).
        .decorations(decorations)
        .initialization_script(BRIDGE_JS)
        .build()?;
    Ok(())
}

/// Check the update endpoint and toast the result. Async (the check is network),
/// spawned from the sync tray handler.
fn check_updates(app: &AppHandle) {
    let handle = app.clone();
    tauri::async_runtime::spawn(async move {
        let message = match handle.updater() {
            Ok(updater) => match updater.check().await {
                Ok(Some(update)) => format!("Update available: v{}. Restart to install.", update.version),
                Ok(None) => "Zade is up to date.".to_string(),
                Err(err) => format!("Update check failed: {err}"),
            },
            Err(err) => format!("Updater unavailable: {err}"),
        };
        let _ = handle
            .notification()
            .builder()
            .title("Zade — updates")
            .body(message)
            .show();
    });
}

/// Locate the installed Ollama executable: the known install path, then PATH.
fn ollama_exe() -> Option<PathBuf> {
    if let Ok(local) = std::env::var("LOCALAPPDATA") {
        let candidate = PathBuf::from(local).join("Programs").join("Ollama").join("ollama.exe");
        if candidate.exists() {
            return Some(candidate);
        }
    }
    if let Ok(out) = Command::new("where").arg("ollama").output() {
        if out.status.success() {
            if let Some(line) = String::from_utf8_lossy(&out.stdout).lines().next() {
                let candidate = PathBuf::from(line.trim());
                if candidate.exists() {
                    return Some(candidate);
                }
            }
        }
    }
    None
}

fn start_ollama(exe: &Path) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        let _ = Command::new(exe).arg("serve").creation_flags(CREATE_NO_WINDOW).spawn();
    }
    #[cfg(not(windows))]
    {
        let _ = Command::new(exe).arg("serve").spawn();
    }
}

/// Managed prerequisite: start Ollama if installed, else install it (winget, or
/// open the download page as a fallback). Toasts what it did.
fn manage_ollama(app: &AppHandle) {
    let message = if let Some(exe) = ollama_exe() {
        start_ollama(&exe);
        "Ollama is installed — starting it now."
    } else if Command::new("winget")
        .args([
            "install", "--id", "Ollama.Ollama", "-e", "--source", "winget",
            "--accept-package-agreements", "--accept-source-agreements",
        ])
        .spawn()
        .is_ok()
    {
        "Installing Ollama via winget…"
    } else {
        let _ = Command::new("cmd").args(["/c", "start", "", "https://ollama.com/download"]).spawn();
        "Opened the Ollama download page."
    };
    let _ = app.notification().builder().title("Zade — Ollama").body(message).show();
}

fn main() {
    supervise();

    tauri::Builder::default()
        // single-instance must be registered first.
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            // Second launch = a summon, not a second universe.
            show_main(app);
        }))
        .plugin(tauri_plugin_autostart::init(
            MacosLauncher::LaunchAgent,
            Some(vec![START_HIDDEN_FLAG]),
        ))
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        // Persist ONLY geometry. The default flags also save/restore `decorations`,
        // which poisons the frameless window: a dev-mode run (decorations:true)
        // gets restored over the builder's decorations(false), resurrecting the OS
        // title bar on top of our custom one. Size/position/maximized only.
        .plugin(
            tauri_plugin_window_state::Builder::default()
                .with_state_flags(StateFlags::SIZE | StateFlags::POSITION | StateFlags::MAXIMIZED)
                .build(),
        )
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_shortcuts([SUMMON_SHORTCUT, IMMERSIVE_SHORTCUT])
                .expect("global shortcuts failed to parse")
                .with_handler(|app, shortcut, event| {
                    if event.state() != ShortcutState::Pressed {
                        return;
                    }
                    let ctrl_alt = Modifiers::CONTROL | Modifiers::ALT;
                    if shortcut.matches(ctrl_alt, Code::KeyZ) {
                        toggle_main(app);
                    } else if shortcut.matches(ctrl_alt, Code::KeyF) {
                        toggle_immersive(app);
                    }
                })
                .build(),
        )
        .invoke_handler(tauri::generate_handler![
            kernel_request,
            win_minimize,
            win_hide,
            win_toggle_immersive,
            win_toggle_maximize
        ])
        .setup(|app| {
            build_main_window(app.handle())?;
            ensure_first_run_autostart(app.handle());
            let start_on = app.autolaunch().is_enabled().unwrap_or(false);

            let show = MenuItem::with_id(app, "show", "Show Zade\tCtrl+Alt+Z", true, None::<&str>)?;
            let immersive =
                MenuItem::with_id(app, "immersive", "Immersive mode\tCtrl+Alt+F", true, None::<&str>)?;
            let autostart = CheckMenuItem::with_id(
                app,
                "autostart",
                "Start with Windows",
                true,
                start_on,
                None::<&str>,
            )?;
            let updates = MenuItem::with_id(app, "updates", "Check for updates", true, None::<&str>)?;
            let ollama = MenuItem::with_id(app, "ollama", "Install / start Ollama", true, None::<&str>)?;
            let sep1 = PredefinedMenuItem::separator(app)?;
            let sep2 = PredefinedMenuItem::separator(app)?;
            let hide = MenuItem::with_id(app, "hide", "Hide", true, None::<&str>)?;
            let quit = MenuItem::with_id(
                app,
                "quit",
                "Quit shell (kernel keeps running)",
                true,
                None::<&str>,
            )?;
            let menu = Menu::with_items(
                app,
                &[&show, &immersive, &autostart, &sep1, &updates, &ollama, &sep2, &hide, &quit],
            )?;

            let autostart_check = autostart.clone();
            TrayIconBuilder::with_id("zade-shell")
                .icon(app.default_window_icon().expect("bundled icon").clone())
                .tooltip("Zade")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(move |app, event| match event.id.as_ref() {
                    "show" => show_main(app),
                    "immersive" => toggle_immersive(app),
                    "updates" => check_updates(app),
                    "ollama" => manage_ollama(app),
                    "autostart" => {
                        let al = app.autolaunch();
                        let enabled = al.is_enabled().unwrap_or(false);
                        let _ = if enabled { al.disable() } else { al.enable() };
                        let _ = autostart_check.set_checked(al.is_enabled().unwrap_or(!enabled));
                    }
                    "hide" => {
                        if let Some(w) = app.get_webview_window("main") {
                            let _ = w.hide();
                        }
                    }
                    "quit" => app.exit(0),
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if matches!(event, TrayIconEvent::DoubleClick { .. }) {
                        toggle_main(tray.app_handle());
                    }
                })
                .build(app)?;

            // A login-boot (autostart passes --start-hidden) comes up resident in
            // the tray, not with the window in your face.
            if std::env::args().any(|a| a == START_HIDDEN_FLAG) {
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.hide();
                }
            }

            watch_status(app.handle().clone());
            watch_notifications(app.handle().clone());
            Ok(())
        })
        .on_window_event(|window, event| {
            // Closing the window hides it: the universe stays resident in the
            // tray. Real exit is the tray's Quit.
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .run(tauri::generate_context!())
        .expect("zade shell failed to start");
}
