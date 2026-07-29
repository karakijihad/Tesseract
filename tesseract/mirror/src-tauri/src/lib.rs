use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{Emitter, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};

mod exe_update;
mod provision;
mod repo;
mod shell_log;
#[cfg(test)]
mod test_support;
mod token;
mod update;
use provision::{hide_console, is_provisioned, refresh_piper_voice, tesseract_home, venv_python};

struct SupervisorProc(Mutex<Option<Child>>);
struct TesseractHome(PathBuf);

/// Poison-tolerant lock, matching `shell_log`'s existing recovery.
///
/// A panic anywhere inside a critical section would otherwise poison the
/// mutex and turn every later `lock()` into a second panic — including the
/// one in the `RunEvent::Exit` handler that stops the supervisor, which would
/// leak the whole backend process tree on shutdown. The data behind these
/// locks is a single `Option<Child>`; there is no invariant a panic could
/// leave half-updated, so recovering the guard is strictly better than
/// cascading.
pub(crate) fn lock_or_recover<T>(m: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

/// True when `TESSERACT_PYTHON` names the interpreter to use — the dev-run
/// contract (`pnpm tauri dev` against the repo `.venv`).
///
/// Gates provisioning as well as interpreter choice. It used to gate only the
/// latter, so a dev launch on a machine with no `provisioned.json` would
/// clone the production repo and download a Python toolchain, dependencies,
/// and Chromium into `%LOCALAPPDATA%` — minutes of work whose output the run
/// then ignored, because `resolve_python` had already picked the override.
fn dev_interpreter_override() -> Option<String> {
    std::env::var("TESSERACT_PYTHON")
        .ok()
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
}

fn resolve_python(home: &Path) -> String {
    // Dev override wins (so `pnpm tauri dev` keeps using the repo .venv).
    if let Some(explicit) = dev_interpreter_override() {
        return explicit;
    }
    // Provisioned per-user venv (the installed-app path).
    if is_provisioned(home) {
        return venv_python(home).to_string_lossy().into_owned();
    }
    // Last resort — a Python on PATH (dev machines without the env var).
    "python".to_string()
}

/// Deletes a leftover stop-request file before a supervisor is started.
///
/// `request_supervisor_stop` writes `runtime/supervisor_stop_request` and
/// relies on the supervisor to consume it. If the supervisor is already dead
/// when that write happens — it crashed, or never got past boot — nobody
/// consumes it, and the file survives into the next launch. The new
/// supervisor's stop-watcher then sees a stop order it never asked for and
/// shuts down within milliseconds, on every launch, forever.
///
/// That is a self-perpetuating wedge: one crash makes the app permanently
/// unstartable, and it survives an update that fixes the original crash. It is
/// how a 2026-07-29 install stayed dead *after* the bug that killed it had been
/// patched — each boot logged "supervisor exited gracefully" 2 ms after spawn,
/// which read like a clean shutdown rather than a refusal to start.
///
/// A stop request is only ever meaningful for the process it was aimed at, so
/// clearing it at spawn time is always correct.
fn clear_stale_stop_request(home: &Path) {
    let path = home.join("runtime").join("supervisor_stop_request");
    match std::fs::remove_file(&path) {
        Ok(()) => shell_log::log("cleared a stale supervisor stop-request from a previous run"),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => shell_log::log_error(&format!("could not clear stale stop-request: {e}")),
    }
}

/// Archives a crash-storm latch left behind by a previous run.
///
/// After 3 backend crashes in 45s the supervisor's breaker writes
/// `runtime/crash_storm.json` and every later supervisor start refuses to run
/// until the marker is cleared — which in a dev tree means running
/// `python -m tesseract.scripts.clear_crash_storm` from a console. A packaged
/// install has no console: the 2026-07-29 pywinpty crash storm left the app
/// permanently dead even after the fix was published, because launch, update
/// respawn, and re-provision all funnel into `spawn_supervisor` and all hit
/// the latch.
///
/// Every `spawn_supervisor` call is operator-driven (a double-click, an update
/// click, a provisioning run), so clearing here preserves what the latch
/// actually guards — an unattended crash/respawn loop within one supervisor
/// run — while making "restart TESSERACT" the recovery action. The marker is
/// archived to the same directory the Python-side clear uses, not deleted, so
/// the record of past storms survives.
fn clear_stale_crash_storm(home: &Path) {
    let marker = home.join("runtime").join("crash_storm.json");
    if !marker.exists() {
        return;
    }
    let archive_dir = home
        .join("logs")
        .join("supervisor")
        .join("crash-storm-archive");
    let _ = std::fs::create_dir_all(&archive_dir);
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let target = archive_dir.join(format!("shell-cleared-{stamp}.json"));
    match std::fs::rename(&marker, &target) {
        Ok(()) => shell_log::log(
            "cleared a crash-storm latch from a previous run — archived; \
             the supervisor gets a fresh start",
        ),
        // Rename can fail across odd filesystem states; plain removal still
        // unwedges the app, which is the part that matters.
        Err(rename_err) => match std::fs::remove_file(&marker) {
            Ok(()) => shell_log::log(
                "cleared a crash-storm latch from a previous run (archive failed; removed)",
            ),
            Err(remove_err) => shell_log::log_error(&format!(
                "could not clear crash-storm latch: rename: {rename_err}; remove: {remove_err}"
            )),
        },
    }
}

fn spawn_supervisor(home: &PathBuf) -> std::io::Result<Child> {
    clear_stale_stop_request(home);
    clear_stale_crash_storm(home);
    let mut cmd = Command::new(resolve_python(home));
    cmd.args(["-m", "tesseract.supervisor"])
        .env("TESSERACT_HOME", home)
        .env("SUPERVISOR_HEADLESS", "1")
        .env("SUPERVISOR_DEV_VITE", "0");
    hide_console(&mut cmd);
    let result = cmd.spawn();
    match &result {
        Ok(child) => shell_log::log(&format!("supervisor spawned (pid {})", child.id())),
        Err(e) => shell_log::log_error(&format!("failed to spawn supervisor: {e}")),
    }
    result
}

fn request_supervisor_stop(home: &Path, child: &mut Child) {
    shell_log::log("requesting supervisor stop");
    // Graceful: write the stop-request file Task 1 watches.
    let runtime = home.join("runtime");
    let _ = std::fs::create_dir_all(&runtime);
    let _ = std::fs::write(runtime.join("supervisor_stop_request"), "stop\n");

    // Wait up to 30s for the supervisor to exit on its own.
    let started = Instant::now();
    let deadline = started + Duration::from_secs(30);
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                // Report what actually happened, not what we hoped for. This
                // used to log "exited gracefully" for ANY observed exit — the
                // status was discarded — so a supervisor that refused to start
                // and died in 2 ms read exactly like a clean shutdown. That is
                // precisely how a stale stop-request wedge stayed invisible
                // through several launches on 2026-07-29.
                let elapsed = started.elapsed().as_millis();
                if status.success() && elapsed >= 50 {
                    shell_log::log(&format!("supervisor exited gracefully after {elapsed}ms"));
                } else {
                    shell_log::log_error(&format!(
                        "supervisor exited {elapsed}ms after the stop request (status {status}) — \
                         too fast or unsuccessful to be a clean shutdown; it likely never started"
                    ));
                }
                return;
            }
            Ok(None) if Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(200));
            }
            _ => break,
        }
    }
    // Backstop: the supervisor's own boot-time orphan reap + port cleanup
    // are the safety net if this force-kill leaves anything behind.
    shell_log::log_error("supervisor did not exit within 30s — force-killing");
    kill_process_tree(child);
}

/// Force-kills the supervisor *and its descendants*.
///
/// `Child::kill` on Windows terminates only the named PID, so everything the
/// supervisor had spawned (Mirror backend, Vite, headless CLI agents) survived
/// as an orphan holding its port until the next launch's reap sweep noticed.
/// `taskkill /T` walks the tree. The plain `kill` remains the fallback when
/// `taskkill` is unavailable or fails, and is the only path on non-Windows.
fn kill_process_tree(child: &mut Child) {
    #[cfg(windows)]
    {
        let mut cmd = Command::new("taskkill");
        cmd.args(["/T", "/F", "/PID", &child.id().to_string()]);
        hide_console(&mut cmd);
        if matches!(cmd.status(), Ok(status) if status.success()) {
            let _ = child.wait();
            return;
        }
        shell_log::log_error("taskkill /T failed — falling back to a single-process kill");
    }
    let _ = child.kill();
    let _ = child.wait();
}

/// Shared "provisioning just succeeded" tail: retries the voice-model fetch,
/// starts the supervisor, and only then closes the splash and reveals the
/// cockpit. Called for an already-provisioned launch, a first-run success,
/// and a token-retry success (`token::submit_github_token`), so the three
/// converge on identical behavior instead of duplicating it.
///
/// The window is revealed *after* the spawn succeeds, never before. Showing
/// it first meant a failed spawn (missing interpreter, quarantined venv,
/// wedged install) presented a fully rendered cockpit whose every request
/// silently failed, with the only diagnostic an `eprintln!` that goes nowhere
/// in a console-less GUI process. A backend that did not start is now a
/// visible error instead of a dead-looking app.
fn finish_provisioning_success(handle: &tauri::AppHandle, home: &PathBuf) {
    refresh_piper_voice(home);
    match spawn_supervisor(home) {
        Ok(child) => {
            if let Some(state) = handle.try_state::<SupervisorProc>() {
                *lock_or_recover(&state.0) = Some(child);
            }
            if let Some(splash) = handle.get_webview_window("splash") {
                let _ = splash.close();
            }
            if let Some(main) = handle.get_webview_window("main") {
                let _ = main.show();
            }
        }
        Err(e) => report_fatal(
            handle,
            &format!(
                "TESSERACT could not start its backend ({e}). The main window is not shown \
                 because nothing in it would work. See logs\\shell.log for details."
            ),
        ),
    }
}

/// Surfaces an unrecoverable startup failure instead of leaving the user with
/// either a backend-less cockpit or no window at all.
///
/// Reuses the splash webview as the error surface: during first-run it is
/// still open, so the message is emitted to it. On an already-provisioned
/// launch no splash exists, so one is opened with the message in its query
/// string — passing it by URL rather than by event avoids racing the
/// webview's listener registration, which a freshly built window would
/// otherwise lose.
fn report_fatal(handle: &tauri::AppHandle, msg: &str) {
    shell_log::log_error(msg);
    if let Some(splash) = handle.get_webview_window("splash") {
        let _ = splash.emit("shell-fatal", msg.to_string());
        let _ = splash.set_focus();
        return;
    }
    let url = format!("splash.html?fatal={}", query_escape(msg));
    let _ = WebviewWindowBuilder::new(handle, "splash", WebviewUrl::App(url.into()))
        .title("TESSERACT — startup failed")
        .inner_size(420.0, 280.0)
        .center()
        .resizable(false)
        .build();
}

/// Percent-encodes a message for use in `report_fatal`'s query string.
/// Hand-rolled rather than pulling in a URL crate for one call site.
fn query_escape(s: &str) -> String {
    s.bytes()
        .map(|b| match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                (b as char).to_string()
            }
            _ => format!("%{b:02X}"),
        })
        .collect()
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
    // As early as possible — before the log path is even known — so a panic
    // anywhere later in setup/provisioning/update still gets a durable
    // record once `shell_log::init` points it at a real file.
    shell_log::install_panic_hook();

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
            update::update_force_apply,
            update::app_version,
            update::app_info,
            exe_update::exe_update_check,
            exe_update::exe_update_apply,
            token::submit_github_token
        ])
        .setup(|app| {
            let home = tesseract_home(app.handle());
            shell_log::init(&home);
            app.manage(SupervisorProc(Mutex::new(None)));
            app.manage(TesseractHome(home.clone()));
            app.manage(update::UpdateInProgress::new());

            // Decide window flow up front: already-provisioned skips the splash
            // entirely and shows the cockpit immediately; a fresh install shows
            // the splash and the background thread swaps it for main on success.
            // A dev run counts as provisioned — it uses the repo checkout and
            // must never provision a per-user tree it will not read.
            let already = dev_interpreter_override().is_some() || is_provisioned(&home);
            if !already {
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
                if already {
                    // Every launch, not just first-run provisioning: retries
                    // the voice model fetch if a previous attempt failed
                    // (offline, transient outage) and spawns the supervisor.
                    // See `refresh_piper_voice`'s own doc comment.
                    finish_provisioning_success(&handle, &home);
                } else {
                    // On a clone auth failure with no working token,
                    // `provision` returns `NeedsToken` instead of failing
                    // outright — `token::handle_provision_result` shows the
                    // in-app prompt (see `token::submit_github_token` for the
                    // retry) rather than leaving the splash on a dead end.
                    let result = provision::provision(&handle, &home);
                    token::handle_provision_result(&handle, &home, result);
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(|app_handle, event| {
        if let RunEvent::Exit = event {
            let home = app_handle.state::<TesseractHome>().0.clone();
            let child = lock_or_recover(&app_handle.state::<SupervisorProc>().0).take();
            if let Some(mut child) = child {
                request_supervisor_stop(&home, &mut child);
            }
        }
    });
}

// `kill_process_tree` is deliberately not covered here: it force-kills a real
// OS process (and on Windows shells out to `taskkill`), which a unit test
// has no safe way to exercise without spawning and then killing a real child
// process as a side effect.
#[cfg(test)]
mod tests {
    use super::*;

    /// Regression — a 2026-07-29 install stayed dead after the bug that killed
    /// it was already fixed. `request_supervisor_stop` writes a stop-request
    /// file for the supervisor to consume; if the supervisor is already dead
    /// nobody consumes it, and the next launch's supervisor obeys a stop order
    /// it never asked for. One crash therefore made the app permanently
    /// unstartable, surviving the update that repaired the original fault.
    #[test]
    fn clear_stale_stop_request_removes_a_leftover_order() {
        let home = crate::test_support::TempDir::new("stale-stop");
        let runtime = home.path().join("runtime");
        std::fs::create_dir_all(&runtime).unwrap();
        let marker = runtime.join("supervisor_stop_request");
        std::fs::write(
            &marker, "stop
",
        )
        .unwrap();

        clear_stale_stop_request(home.path());

        assert!(
            !marker.exists(),
            "a stop request from a previous run must never reach a new supervisor"
        );
    }

    #[test]
    fn clear_stale_stop_request_is_a_no_op_when_there_is_none() {
        let home = crate::test_support::TempDir::new("no-stop");
        std::fs::create_dir_all(home.path().join("runtime")).unwrap();
        clear_stale_stop_request(home.path());
    }

    /// Regression — the 2026-07-29 pywinpty crash storm latched
    /// `runtime/crash_storm.json`, and because every supervisor start refuses
    /// while the marker exists, the app stayed dead even after the update that
    /// fixed the crash. An operator-driven spawn must clear the latch, and the
    /// marker's contents must survive in the archive dir rather than vanish.
    #[test]
    fn clear_stale_crash_storm_archives_the_latch() {
        let home = crate::test_support::TempDir::new("stale-storm");
        let runtime = home.path().join("runtime");
        std::fs::create_dir_all(&runtime).unwrap();
        let marker = runtime.join("crash_storm.json");
        std::fs::write(&marker, "{\"reason\":\"3 crashes\"}").unwrap();

        clear_stale_crash_storm(home.path());

        assert!(
            !marker.exists(),
            "a crash-storm latch from a previous run must not block a fresh operator-driven start"
        );
        let archive_dir = home
            .path()
            .join("logs")
            .join("supervisor")
            .join("crash-storm-archive");
        let archived: Vec<_> = std::fs::read_dir(&archive_dir)
            .expect("archive dir must exist")
            .filter_map(Result::ok)
            .collect();
        assert_eq!(
            archived.len(),
            1,
            "the marker must be archived, not deleted"
        );
        assert_eq!(
            std::fs::read_to_string(archived[0].path()).unwrap(),
            "{\"reason\":\"3 crashes\"}"
        );
    }

    #[test]
    fn clear_stale_crash_storm_is_a_no_op_when_there_is_none() {
        let home = crate::test_support::TempDir::new("no-storm");
        std::fs::create_dir_all(home.path().join("runtime")).unwrap();
        clear_stale_crash_storm(home.path());
        assert!(!home.path().join("logs").exists());
    }

    #[test]
    fn query_escape_percent_encodes_reserved_and_non_ascii_bytes() {
        assert_eq!(query_escape("abcXYZ019-_.~"), "abcXYZ019-_.~");
        assert_eq!(query_escape(" "), "%20");
        assert_eq!(query_escape("&"), "%26");
        assert_eq!(query_escape("="), "%3D");
        assert_eq!(query_escape("%"), "%25");
        // The em dash used in `report_fatal`'s real "Setup failed —" message —
        // UTF-8 encodes to bytes 0xE2 0x80 0x94, each percent-encoded on its own.
        assert_eq!(query_escape("—"), "%E2%80%94");
    }

    #[test]
    fn lock_or_recover_returns_the_guard_normally() {
        let m = Mutex::new(5);
        let guard = lock_or_recover(&m);
        assert_eq!(*guard, 5);
    }

    #[test]
    fn lock_or_recover_recovers_a_poisoned_mutex_instead_of_panicking() {
        let m = std::sync::Arc::new(Mutex::new(0));
        let m2 = m.clone();

        // Poison the mutex: panic on another thread while holding the guard.
        // `thread::spawn` catches the unwind at the thread boundary (the same
        // mechanism `std::panic::catch_unwind` uses internally), but the
        // guard's `Drop` still runs *during* the unwind, before that catch,
        // which is the moment the mutex is marked poisoned.
        let handle = std::thread::spawn(move || {
            let _guard = m2.lock().unwrap();
            panic!("poisoning the mutex on purpose");
        });
        assert!(handle.join().is_err());
        assert!(m.is_poisoned());

        let guard = lock_or_recover(&m);
        assert_eq!(
            *guard, 0,
            "must recover the poisoned guard rather than panicking"
        );
    }

    // `TESSERACT_PYTHON` is a process-global env var and `cargo test` runs
    // concurrently; this is the only test in the crate touching it, but a
    // lock plus save/restore of any prior value keeps it safe against that
    // changing later.
    static ENV_LOCK: Mutex<()> = Mutex::new(());

    #[test]
    fn dev_interpreter_override_handles_unset_blank_and_set_values() {
        let _guard = lock_or_recover(&ENV_LOCK);
        let prior = std::env::var("TESSERACT_PYTHON").ok();

        std::env::remove_var("TESSERACT_PYTHON");
        assert_eq!(dev_interpreter_override(), None, "unset must be None");

        std::env::set_var("TESSERACT_PYTHON", "   ");
        assert_eq!(
            dev_interpreter_override(),
            None,
            "whitespace-only must be treated as unset"
        );

        std::env::set_var("TESSERACT_PYTHON", "  C:\\Python312\\python.exe  ");
        assert_eq!(
            dev_interpreter_override(),
            Some("C:\\Python312\\python.exe".to_string()),
            "must trim surrounding whitespace"
        );

        match prior {
            Some(v) => std::env::set_var("TESSERACT_PYTHON", v),
            None => std::env::remove_var("TESSERACT_PYTHON"),
        }
    }
}
