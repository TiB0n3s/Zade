// Zade desktop universe shell — Phases 1–3.
//
// The kernel (FastAPI on 127.0.0.1:8787) stays a separate loopback service;
// this shell is a client that spawns and supervises it as a sidecar, then
// frames the kernel-served UI. Quitting the shell leaves the kernel running:
// Zade is resident, the window is just one surface.
//
// Phase 3 adds the "product" layer: autostart (resident, boots with Windows —
// quietly to the tray), an L2 immersive full-screen mode, and an Ollama-aware
// tray tooltip so the prerequisite "brain" surfaces gracefully.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::path::PathBuf;
use std::process::Command;
use std::time::{Duration, Instant};

use tauri::menu::{CheckMenuItem, Menu, MenuItem};
use tauri::tray::{TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager};
use tauri_plugin_autostart::{ManagerExt, MacosLauncher};
use tauri_plugin_global_shortcut::{Code, Modifiers, ShortcutState};

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

/// Locate the kernel repo root: env override, then walk up from the exe
/// (works for target/debug builds inside the repo), then the canonical path.
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
/// This is the graceful-degrade signal for the Ollama prerequisite: the
/// tooltip reads "brain offline · start Ollama" when the model host is down.
fn watch_status(app: AppHandle) {
    std::thread::spawn(move || loop {
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
        std::thread::sleep(STATUS_INTERVAL);
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

/// L2 immersive mode: full-screen the world (OS strips the chrome). Toggling in
/// also guarantees the window is visible and focused so the mode is enterable
/// straight from a summon.
fn toggle_immersive(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let now = w.is_fullscreen().unwrap_or(false);
        let _ = w.set_fullscreen(!now);
        if !now {
            let _ = w.show();
            let _ = w.set_focus();
        }
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
        .plugin(tauri_plugin_window_state::Builder::default().build())
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
        .setup(|app| {
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
            let hide = MenuItem::with_id(app, "hide", "Hide", true, None::<&str>)?;
            let quit = MenuItem::with_id(
                app,
                "quit",
                "Quit shell (kernel keeps running)",
                true,
                None::<&str>,
            )?;
            let menu = Menu::with_items(app, &[&show, &immersive, &autostart, &hide, &quit])?;

            let autostart_check = autostart.clone();
            TrayIconBuilder::with_id("zade-shell")
                .icon(app.default_window_icon().expect("bundled icon").clone())
                .tooltip("Zade")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(move |app, event| match event.id.as_ref() {
                    "show" => show_main(app),
                    "immersive" => toggle_immersive(app),
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
