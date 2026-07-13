// Zade desktop universe shell — Phase 1.
//
// The kernel (FastAPI on 127.0.0.1:8787) stays a separate loopback service;
// this shell is a client that spawns and supervises it as a sidecar, then
// frames the kernel-served UI. Quitting the shell leaves the kernel running:
// Zade is resident, the window is just one surface.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::path::PathBuf;
use std::process::Command;
use std::time::{Duration, Instant};

use tauri::menu::{Menu, MenuItem};
use tauri::tray::{TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager};
use tauri_plugin_global_shortcut::ShortcutState;

const KERNEL_BASE: &str = "http://127.0.0.1:8787";
/// Global summon chord. Z for Zade; Ctrl+Alt avoids the common Ctrl+Shift app space.
const SUMMON_SHORTCUT: &str = "ctrl+alt+z";
const SUPERVISE_INTERVAL: Duration = Duration::from_secs(20);
const RESPAWN_COOLDOWN: Duration = Duration::from_secs(45);

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

fn kernel_healthy() -> bool {
    let agent = ureq::AgentBuilder::new()
        .timeout(Duration::from_secs(3))
        .build();
    match agent.get(&format!("{KERNEL_BASE}/health")).call() {
        Ok(resp) => resp
            .into_string()
            .ok()
            .and_then(|body| serde_json::from_str::<serde_json::Value>(&body).ok())
            .map(|v| v["ok"].as_bool().unwrap_or(false))
            .unwrap_or(false),
        Err(_) => false,
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
            if !kernel_healthy() && last_spawn.elapsed() >= RESPAWN_COOLDOWN {
                spawn_kernel();
                last_spawn = Instant::now();
            }
            std::thread::sleep(SUPERVISE_INTERVAL);
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

fn main() {
    supervise();

    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            // Second launch = a summon, not a second universe.
            show_main(app);
        }))
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_shortcuts([SUMMON_SHORTCUT])
                .expect("summon shortcut failed to parse")
                .with_handler(|app, _shortcut, event| {
                    if event.state() == ShortcutState::Pressed {
                        toggle_main(app);
                    }
                })
                .build(),
        )
        .setup(|app| {
            let show = MenuItem::with_id(app, "show", "Show Zade\tCtrl+Alt+Z", true, None::<&str>)?;
            let hide = MenuItem::with_id(app, "hide", "Hide", true, None::<&str>)?;
            let quit = MenuItem::with_id(
                app,
                "quit",
                "Quit shell (kernel keeps running)",
                true,
                None::<&str>,
            )?;
            let menu = Menu::with_items(app, &[&show, &hide, &quit])?;
            TrayIconBuilder::with_id("zade-shell")
                .icon(app.default_window_icon().expect("bundled icon").clone())
                .tooltip("Zade")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "show" => show_main(app),
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
