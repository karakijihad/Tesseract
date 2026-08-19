use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{Emitter, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};

mod app_swap;
mod exe_update;
mod job;
mod provision;
mod repo;
mod setup;
mod shell_log;
#[cfg(test)]
mod test_support;
mod update;
use provision::{
    app_dir, hide_console, home_dir, is_provisioned, refresh_optional_assets, runtime_dir,
    tesseract_home, venv_python,
};

struct SupervisorProc(Mutex<Option<Child>>);
struct TesseractHome(PathBuf);

/// Whether the cockpit has already been revealed.
///
/// Three things race to reveal it — the cockpit's own `cockpit_ready`, the
/// watchdog that covers a cockpit which never calls, and the fatal path, which
/// must not reveal at all once an error is on screen. The flag makes the first
/// one win and the rest no-ops, so a slow warm-up followed by a late
/// `cockpit_ready` cannot re-hide the splash over a running app.
struct CockpitRevealed(AtomicBool);

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
    let path = runtime_dir(home).join("supervisor_stop_request");
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
    let marker = runtime_dir(home).join("crash_storm.json");
    if !marker.exists() {
        return;
    }
    let archive_dir = runtime_dir(home)
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

/// Routes the supervisor's own stdout/stderr into
/// `logs/supervisor-console.log`. The supervisor captures its CHILDREN's
/// consoles, but its own pre-logging output was discarded by this
/// console-less shell — a supervisor that died on config validation
/// (observed live 2026-07-30) left literally zero trace. Truncated when
/// it grows past a small cap: after boot the supervisor logs to its own
/// rotating file, so this only holds early-boot output and tracebacks.
fn attach_supervisor_console(home: &Path, cmd: &mut Command) {
    const MAX_BYTES: u64 = 2 * 1024 * 1024;
    let dir = runtime_dir(home).join("logs");
    let _ = std::fs::create_dir_all(&dir);
    let path = dir.join("supervisor-console.log");
    let oversized = std::fs::metadata(&path)
        .map(|m| m.len() > MAX_BYTES)
        .unwrap_or(false);
    let mut opts = std::fs::OpenOptions::new();
    opts.create(true).write(true);
    if oversized {
        opts.truncate(true);
    } else {
        opts.append(true);
    }
    if let Ok(out) = opts.open(&path) {
        if let Ok(err) = out.try_clone() {
            cmd.stdout(std::process::Stdio::from(out));
            cmd.stderr(std::process::Stdio::from(err));
        }
    }
}

/// `python -m tesseract.supervisor` exit code meaning "another supervisor
/// already holds the pid file". Kept in sync by hand with
/// `supervisor/__main__.py`'s refusal path — same pattern as
/// `provision::DEPS_VERSION`, since the shell has no way to import it.
const EXIT_ALREADY_RUNNING: i32 = 3;

/// Poll interval and budget for deciding the supervisor actually started.
///
/// Polled rather than slept flat: a healthy launch — every launch that is not
/// a double-start — pays one interval and moves on, instead of the whole
/// window. The budget still has to cover the refusal path (a pid-file claim
/// and a log line), which exits almost immediately.
const SUPERVISOR_PROBE_INTERVAL_MS: u64 = 50;
const SUPERVISOR_PROBE_ATTEMPTS: u32 = 8;

fn spawn_supervisor(home: &PathBuf) -> std::io::Result<Child> {
    clear_stale_stop_request(home);
    clear_stale_crash_storm(home);
    // Python derives install_root() as home_dir().parent, so it must be
    // handed the `home/` sibling — handing it the root would put every state
    // path one level too high. Created first: the very first launch spawns
    // before anything Python-side has made it.
    let state_home = home_dir(home);
    let _ = std::fs::create_dir_all(&state_home);
    let mut cmd = Command::new(resolve_python(home));
    cmd.args(["-m", "tesseract.supervisor"])
        // Deterministic cwd: without one, a relative default anchored on the
        // process working directory can land outside the install entirely.
        .current_dir(app_dir(home))
        .env("TESSERACT_HOME", &state_home)
        .env("SUPERVISOR_HEADLESS", "1")
        .env("SUPERVISOR_DEV_VITE", "0");
    attach_supervisor_console(home, &mut cmd);
    hide_console(&mut cmd);
    let result = cmd.spawn();
    match result {
        Ok(mut child) => {
            shell_log::log(&format!("supervisor spawned (pid {})", child.id()));
            // A successful spawn is not a successful start, and until now
            // nothing here distinguished them: the splash closes immediately
            // after this returns, so a supervisor that exits at once left a
            // cockpit with no backend and no error anywhere the operator
            // could see it. That was survivable while the only such exit was
            // the losing side of a rare pid race; once that claim became
            // atomic it is the GUARANTEED outcome of every double-launch, so
            // the two changes have to travel together.
            //
            // EXIT_ALREADY_RUNNING (3) is the one code with a specific
            // meaning; anything else non-zero is reported as an early death.
            let mut exited = None;
            for _ in 0..SUPERVISOR_PROBE_ATTEMPTS {
                std::thread::sleep(std::time::Duration::from_millis(
                    SUPERVISOR_PROBE_INTERVAL_MS,
                ));
                match child.try_wait() {
                    Ok(Some(status)) => {
                        exited = Some(status);
                        break;
                    }
                    // Still running: the normal path, and the common one.
                    // Stop paying for the rest of the window.
                    Ok(None) => break,
                    Err(_) => break,
                }
            }
            match exited {
                Some(status) => {
                    let code = status.code().unwrap_or(-1);
                    if code == EXIT_ALREADY_RUNNING {
                        shell_log::log_error(
                            "supervisor refused to start: another supervisor is \
                             already running. Close the other TESSERACT window, \
                             or stop it before launching again.",
                        );
                    } else {
                        shell_log::log_error(&format!(
                            "supervisor exited immediately with code {code} — the \
                             backend is not running; see runtime/logs/supervisor.log"
                        ));
                    }
                    Err(std::io::Error::other(format!(
                        "supervisor exited immediately (code {code})"
                    )))
                }
                // Still running, or we could not tell — either way carry on
                // rather than block the launch.
                None => Ok(child),
            }
        }
        Err(e) => {
            shell_log::log_error(&format!("failed to spawn supervisor: {e}"));
            Err(e)
        }
    }
}

fn request_supervisor_stop(home: &Path, child: &mut Child) {
    shell_log::log("requesting supervisor stop");
    // Graceful: write the stop-request file Task 1 watches.
    let runtime = runtime_dir(home);
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

/// How long the splash stays up when the cockpit never says it is ready.
///
/// Not the wait itself: the cockpit polls `/api/health` and calls
/// `cockpit_ready` the moment the backend reports itself warm, with its own
/// ceiling on that (`GIVE_UP_MS` in `lib/warmup.ts`). This covers the case
/// where the cockpit's own JS never runs at all — a broken bundle, a webview
/// that failed to load — where no signal is ever coming and the operator would
/// otherwise hold a splash forever. Comfortably past the frontend's own
/// give-up, so a slow boot is always reported rather than timed out behind
/// its back; raise this if that one is raised.
const REVEAL_WATCHDOG: Duration = Duration::from_secs(120);
const REVEAL_WATCHDOG_TICK: Duration = Duration::from_millis(250);

/// Close the splash and show the cockpit. Idempotent — the first caller wins.
fn reveal_cockpit(handle: &tauri::AppHandle, why: &str) {
    if let Some(state) = handle.try_state::<CockpitRevealed>() {
        if state.0.swap(true, Ordering::SeqCst) {
            return;
        }
    }
    shell_log::log(&format!("revealing the cockpit ({why})"));
    if let Some(splash) = handle.get_webview_window("splash") {
        let _ = splash.close();
    }
    if let Some(main) = handle.get_webview_window("main") {
        let _ = main.show();
        let _ = main.set_focus();
    }
}

/// The cockpit's own signal that the backend has finished preparing itself.
///
/// Checks its caller for the reason `quit_app` does: commands registered by
/// the app are not covered by the capability system, and only the window this
/// reveals has any business asking for it.
#[tauri::command]
fn cockpit_ready(app: tauri::AppHandle, window: tauri::Window) {
    if window.label() != "main" {
        shell_log::log_error(&format!(
            "cockpit_ready refused: only the cockpit may call it, not '{}'",
            window.label()
        ));
        return;
    }
    reveal_cockpit(&app, "backend warm");
}

/// Shared "provisioning just succeeded" tail: retries the voice-model fetch,
/// starts the supervisor, and then hands the splash over to the warm-up wait.
/// Called for both an already-provisioned launch and a first-run success, so
/// the two converge on identical behavior instead of duplicating it.
///
/// The window is revealed *after* the spawn succeeds, never before. Showing
/// it first meant a failed spawn (missing interpreter, quarantined venv,
/// wedged install) presented a fully rendered cockpit whose every request
/// silently failed, with the only diagnostic an `eprintln!` that goes nowhere
/// in a console-less GUI process. A backend that did not start is now a
/// visible error instead of a dead-looking app.
///
/// A spawn that succeeded is still not an app worth looking at: the backend
/// spends its first several seconds building the tool registry, the chat
/// chain, the voice runtime and the scheduler. Revealing on spawn is what put
/// that work in front of the operator instead of behind the splash — panels
/// filling in under them, a first message answered slower than the second.
/// So the splash stays, and the cockpit — hidden, loaded, polling — says when.
fn finish_provisioning_success(handle: &tauri::AppHandle, home: &PathBuf) {
    // Populates the remembered `uv` path before the refresh spawns anything:
    // `provision_hardware` needs it to install GPU wheels, and on an
    // already-provisioned launch nothing else would have resolved it. An
    // error here only costs that one stage, so it is logged, not propagated.
    if let Err(e) = provision::resolve_uv(handle) {
        shell_log::log_error(&format!("could not resolve uv for hardware profiling: {e}"));
    }
    refresh_optional_assets(home);
    match spawn_supervisor(home) {
        Ok(child) => {
            if let Some(state) = handle.try_state::<SupervisorProc>() {
                *lock_or_recover(&state.0) = Some(child);
            }
            match handle.get_webview_window("splash") {
                Some(splash) => {
                    let _ = splash.emit("shell-warming", ());
                    start_reveal_watchdog(handle.clone());
                }
                // No splash means nothing to wait behind, so waiting only
                // costs the operator an empty desktop for the length of the
                // watchdog. Reveal on spawn — the behaviour before the hold
                // existed, and the right one when there is no hold.
                None => reveal_cockpit(handle, "no splash to wait behind"),
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

/// Reveal the cockpit anyway if nothing ever asks.
///
/// Polled rather than slept flat so a `report_fatal` landing during the wait
/// ends the watchdog instead of throwing the main window up over the error
/// message a minute later.
fn start_reveal_watchdog(handle: tauri::AppHandle) {
    std::thread::spawn(move || {
        let deadline = Instant::now() + REVEAL_WATCHDOG;
        loop {
            if let Some(state) = handle.try_state::<CockpitRevealed>() {
                if state.0.load(Ordering::SeqCst) {
                    return;
                }
            }
            if Instant::now() >= deadline {
                break;
            }
            std::thread::sleep(REVEAL_WATCHDOG_TICK);
        }
        shell_log::log_error(
            "the cockpit never reported itself ready — revealing it anyway; \
             see runtime/logs/supervisor.log if it is not usable",
        );
        reveal_cockpit(&handle, "watchdog");
    });
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
    // Claims the reveal so nothing later takes it: an error on screen is the
    // final state, and the warm-up watchdog throwing the cockpit up over it a
    // minute later would bury the only explanation the operator has.
    if let Some(state) = handle.try_state::<CockpitRevealed>() {
        state.0.store(true, Ordering::SeqCst);
    }
    if let Some(splash) = handle.get_webview_window("splash") {
        let _ = splash.emit("shell-fatal", msg.to_string());
        let _ = splash.set_focus();
        return;
    }
    let url = format!("splash.html?fatal={}", query_escape(msg));
    let _ = WebviewWindowBuilder::new(handle, "splash", WebviewUrl::App(url.into()))
        .title("TESSERACT — startup failed")
        .inner_size(460.0, 340.0)
        .min_inner_size(400.0, 260.0)
        .center()
        .decorations(false)
        .resizable(true)
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

/// The splash's close control.
///
/// It cannot simply close its own window: `main` is declared with
/// `visible: false`, so on an already-provisioned launch it exists (hidden)
/// from startup, and closing only the splash would leave that hidden window
/// holding the process open with nothing on screen — worst of all on the
/// failure path, which is exactly when the splash is being used as the error
/// surface. Exiting through the app handle also runs the normal exit cleanup
/// rather than dropping a provisioning run on the floor.
/// Commands registered here are NOT covered by the capability system — that
/// gates `core:*` and plugin permissions, not the app's own handlers — so this
/// checks its caller rather than relying on `capabilities/splash.json` to
/// scope it. Without that, the cockpit webview (which renders model-influenced
/// HTML) could terminate the app, which is the opposite of what removing
/// `allow-close` from its capability was for.
#[tauri::command]
fn quit_app(app: tauri::AppHandle, window: tauri::Window) {
    if window.label() != "splash" {
        shell_log::log_error(&format!(
            "quit_app refused: only the splash may call it, not '{}'",
            window.label()
        ));
        return;
    }
    shell_log::log("splash close: quitting");
    // Before the exit, not after: `RunEvent::Exit` stops the SUPERVISOR, and
    // during a first run the supervisor does not exist yet — what is running
    // is the provisioning thread's `uv`/`python` tree, which Windows does not
    // reap when this process goes away. Left alone, a `uv` kept writing into
    // the staging clone that the next launch would then decide whether to
    // adopt.
    if provision::stop_active() {
        shell_log::log("splash close: stopped the provisioning download");
    }
    app.exit(0);
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
            setup::submit_first_run_setup,
            setup::pending_setup_form,
            cockpit_ready,
            quit_app
        ])
        .setup(|app| {
            let home = tesseract_home(app.handle());
            shell_log::init(&home);
            app.manage(SupervisorProc(Mutex::new(None)));
            app.manage(TesseractHome(home.clone()));
            app.manage(update::UpdateInProgress::new());
            app.manage(CockpitRevealed(AtomicBool::new(false)));

            // Every launch opens the splash, and both flows end the same way:
            // it stays up until the backend reports itself warm. A fresh
            // install fills it with the setup form and the provisioning run
            // first; an already-provisioned one goes straight to the warm-up
            // wait. A dev run counts as provisioned — it uses the repo
            // checkout and must never provision a per-user tree it will not
            // read.
            //
            // Undecorated, so drag/minimise/close come from the page's own
            // header strip. Opens at the PROVISIONING height rather than the
            // form's, because that is the state it opens in — building it
            // tall would flash a mostly-empty 940px window for as long as it
            // takes the page to resize itself down. Resizable rather than
            // fixed.
            //
            // The minimum height must stay BELOW the shortest state the page
            // resizes itself to (`HEIGHTS` in splash.html): Windows enforces a
            // window's minimum through WM_GETMINMAXINFO on every resize,
            // including a programmatic one, so a floor above those values
            // silently clamps them and the short states keep the tall window.
            // `set_size` still resolves, so nothing reports the failure.
            let dev = dev_interpreter_override().is_some();
            let already = dev || is_provisioned(&home);
            // An install that finished without anyone answering the form — a
            // splash that would not open, a manifest that would not build —
            // is provisioned and still has a question outstanding. It gets the
            // form on this launch instead of going straight to the cockpit,
            // because the marker that stops it reinstalling used to stop it
            // being asked as well, permanently, over a window that failed once.
            //
            // The dev override is excluded rather than merely unlikely to
            // qualify: a repo checkout has no per-user marker to read, and a
            // developer who has one from a real install must not be handed a
            // setup form by `pnpm tauri dev`.
            let resume_setup = already && !dev && provision::setup_unanswered(&home);
            // Warming and resuming are the two shapes of an already-installed
            // launch, and they are mutually exclusive — named once here so the
            // window's URL and its title cannot disagree about which is which.
            let warming = already && !resume_setup;
            let handle = app.handle().clone();
            // `?warming=1` rather than an event: the page would otherwise open
            // on the first-run wording ("Preparing first-run setup…") and only
            // correct itself once the supervisor spawn returns, which is a
            // visible half-second of the wrong sentence on every launch.
            //
            // A resume launch is NOT a warming one: the page would open on
            // "Getting everything ready…" and then be interrupted by a form,
            // which reads as the app changing its mind. It opens on the same
            // progress view a first run does, and the form replaces it.
            let splash_url = if warming {
                "splash.html?warming=1"
            } else {
                "splash.html"
            };
            let splash =
                WebviewWindowBuilder::new(app, "splash", WebviewUrl::App(splash_url.into()))
                    .title(if warming {
                        "Starting TESSERACT"
                    } else {
                        "Setting up TESSERACT"
                    })
                    .inner_size(460.0, 372.0)
                    .min_inner_size(400.0, 280.0)
                    .center()
                    .decorations(false)
                    .resizable(true)
                    .build();

            if resume_setup {
                // The app is installed and nothing here reinstalls it: this
                // opens the form the earlier run could not, and the operator's
                // answers run the fetch stages that run skipped. A window that
                // will not open again costs nothing — `offer_deferred_setup`
                // starts the backend as any other launch would, and the marker
                // still says `unanswered`, so the next launch tries again.
                if let Err(e) = &splash {
                    shell_log::log_error(&format!("could not open the setup window ({e})"));
                }
                setup::offer_deferred_setup(handle, home);
                return Ok(());
            }

            if already {
                // Every launch, not just first-run provisioning: retries the
                // optional-asset fetches (voice models, reranker, embeddings)
                // if a previous attempt failed (offline, transient outage) and
                // spawns the supervisor. See `refresh_optional_assets`'s own
                // doc comment. A splash that would not open is handled there,
                // by revealing on spawn rather than waiting behind a window
                // that does not exist.
                if let Err(e) = &splash {
                    shell_log::log_error(&format!("could not open the launch window ({e})"));
                }
                std::thread::spawn(move || finish_provisioning_success(&handle, &home));
                return Ok(());
            }

            // First run. The splash opens on the progress view and the app
            // installs immediately: the clone, Python, the venv and the
            // dependency set are required, so showing them as a question was
            // only ever a question with one answer. The form opens on top of
            // that install once there is an interpreter to build it from
            // (`setup::start_provisioning` → `setup-form`), and the stages that
            // download something optional wait for it — declining a lane is
            // still only meaningful before the model would have been fetched.
            if let Err(e) = &splash {
                // No window means no form, and no form means nobody was asked
                // anything. Provisioning still runs — waiting forever for a
                // form nobody can fill in is not an outcome — but it installs
                // the APP and nothing optional: no speech models, no reranker,
                // no Ollama installer. Several gigabytes arriving on a
                // stranger's machine because a window failed to open is the
                // one thing the consent ledger exists to prevent, and the
                // thing that would have asked is the thing that broke.
                //
                // Started the same way either way, and that is the change: the
                // fallback is no longer a second entry point into provisioning
                // but the same one finding no window to open a form in. It
                // says so in `setup::form_manifest`, once, rather than here
                // and there.
                shell_log::log_error(&format!("could not open the setup window ({e})"));
            }
            setup::start_provisioning(handle, home);
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(|app_handle, event| {
        if let RunEvent::Exit = event {
            // Covers every exit route, not just the splash's close control:
            // a first run that is killed from the taskbar or by a signal has
            // the same abandoned-download problem, and this is the one handler
            // all of them pass through.
            provision::stop_active();
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
        let archive_dir = runtime_dir(home.path())
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
