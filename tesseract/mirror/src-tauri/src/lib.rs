use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{Emitter, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};

mod provision;
mod repo;
mod update;
use provision::{is_provisioned, refresh_piper_voice, tesseract_home, venv_python};

struct SupervisorProc(Mutex<Option<Child>>);
struct TesseractHome(PathBuf);

fn resolve_python(home: &Path) -> String {
    // Dev override wins (so `pnpm tauri dev` keeps using the repo .venv).
    if let Ok(explicit) = std::env::var("TESSERACT_PYTHON") {
        return explicit;
    }
    // Provisioned per-user venv (the installed-app path).
    if is_provisioned(home) {
        return venv_python(home).to_string_lossy().into_owned();
    }
    // Last resort — a Python on PATH (dev machines without the env var).
    "python".to_string()
}

fn spawn_supervisor(home: &PathBuf) -> std::io::Result<Child> {
    let mut cmd = Command::new(resolve_python(home));
    cmd.args(["-m", "tesseract.supervisor"])
        .env("TESSERACT_HOME", home)
        .env("SUPERVISOR_HEADLESS", "1")
        .env("SUPERVISOR_DEV_VITE", "0");
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    cmd.spawn()
}

fn request_supervisor_stop(home: &PathBuf, child: &mut Child) {
    // Graceful: write the stop-request file Task 1 watches.
    let runtime = home.join("runtime");
    let _ = std::fs::create_dir_all(&runtime);
    let _ = std::fs::write(runtime.join("supervisor_stop_request"), "stop\n");

    // Wait up to 30s for the supervisor to exit on its own.
    let deadline = Instant::now() + Duration::from_secs(30);
    loop {
        match child.try_wait() {
            Ok(Some(_)) => return,
            Ok(None) if Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(200));
            }
            _ => break,
        }
    }
    // Backstop: the supervisor's own boot-time orphan reap + port cleanup
    // are the safety net if this force-kill leaves anything behind.
    let _ = child.kill();
}

/// Focuses whichever window is currently shown (splash during first-run
/// provisioning, main afterward) instead of letting a second launch start a
/// second provisioning run against the same staging directory. Windows
/// visibility (`is_visible`) is independent of minimized state — a minimized
/// window still reports visible — so `unminimize` + `show` must run before
/// `set_focus`, or a minimized window makes a second launch look like a dead
/// click (tauri-apps/tauri#8361, #7977).
fn focus_existing_window(app: &tauri::AppHandle) {
    let restore = |window: &tauri::WebviewWindow| {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    };
    for label in ["main", "splash"] {
        if let Some(window) = app.get_webview_window(label) {
            if window.is_visible().unwrap_or(false) {
                restore(&window);
                return;
            }
        }
    }
    if let Some(window) = app.get_webview_window("main") {
        restore(&window);
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let mut builder = tauri::Builder::default();
    #[cfg(desktop)]
    {
        // Must be the first plugin registered: it needs to intercept a
        // second launch before anything else (provisioning, windows) spins up.
        builder = builder.plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            focus_existing_window(app);
        }));
    }
    let app = builder
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![
            update::update_check,
            update::update_apply,
            update::app_version
        ])
        .setup(|app| {
            let home = tesseract_home(app.handle());
            app.manage(SupervisorProc(Mutex::new(None)));
            app.manage(TesseractHome(home.clone()));
            app.manage(update::UpdateInProgress::new());

            // Decide window flow up front: already-provisioned skips the splash
            // entirely and shows the cockpit immediately; a fresh install shows
            // the splash and the background thread swaps it for main on success.
            let already = is_provisioned(&home);
            if already {
                if let Some(main) = app.get_webview_window("main") {
                    let _ = main.show();
                }
            } else {
                let _ =
                    WebviewWindowBuilder::new(app, "splash", WebviewUrl::App("splash.html".into()))
                        .title("Setting up TESSERACT")
                        .inner_size(420.0, 240.0)
                        .center()
                        .decorations(false)
                        .resizable(false)
                        .build();
            }

            let handle = app.handle().clone();
            std::thread::spawn(move || {
                if !already {
                    if let Err(e) = provision::provision(&handle, &home) {
                        eprintln!("provisioning failed: {e}");
                        let _ = handle.emit("provision-progress", format!("Setup failed: {e}"));
                        return; // leave splash showing the error; main never shown
                    }
                    if let Some(splash) = handle.get_webview_window("splash") {
                        let _ = splash.close();
                    }
                    if let Some(main) = handle.get_webview_window("main") {
                        let _ = main.show();
                    }
                }
                // Every launch, not just first-run provisioning: retries the
                // voice model fetch if a previous attempt failed (offline,
                // transient outage). No-ops fast when already present — see
                // `refresh_piper_voice`'s own doc comment. Fire-and-forget,
                // so this never delays `spawn_supervisor` below.
                refresh_piper_voice(&home);
                match spawn_supervisor(&home) {
                    Ok(child) => {
                        if let Some(state) = handle.try_state::<SupervisorProc>() {
                            *state.0.lock().unwrap() = Some(child);
                        }
                    }
                    Err(e) => eprintln!("failed to launch supervisor: {e}"),
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(|app_handle, event| {
        if let RunEvent::Exit = event {
            let home = app_handle.state::<TesseractHome>().0.clone();
            if let Some(mut child) = app_handle
                .state::<SupervisorProc>()
                .0
                .lock()
                .unwrap()
                .take()
            {
                request_supervisor_stop(&home, &mut child);
            }
        }
    });
}
