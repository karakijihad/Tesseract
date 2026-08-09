use std::path::{Path, PathBuf};
use std::sync::OnceLock;
use tauri::path::BaseDirectory;
use tauri::{AppHandle, Emitter, Manager};

/// Bump when the bundled deps change so an upgraded app re-provisions.
///
/// Deliberately NOT bumped for the GPU-wheel work: a mismatch here sends
/// every existing install through `uv venv --clear`, and the corrected
/// `[voice-local]` bound reaches them through `provision_hardware` on the
/// next launch instead — same outcome, without rebuilding a working venv.
pub const DEPS_VERSION: &str = "5";

/// Where `uv` was found, remembered so the Python stages can be handed it.
///
/// `uv.exe` ships as a Tauri resource, which puts it OUTSIDE the state root —
/// so `provision_hardware.py`, which has to install the GPU wheels this
/// machine turns out to want, cannot derive the path from `TESSERACT_HOME`
/// the way it derives every other one. Resolved once by `resolve_uv` and
/// exported to every child by `point_at_state_root`, rather than threaded
/// through call sites that have no other use for it.
static UV_PATH: OnceLock<PathBuf> = OnceLock::new();

/// The INSTALL ROOT: %LOCALAPPDATA%\com.tesseract.mirror (writable).
/// `app/`, `home/` and `runtime/` hang off it. Falls back to an explicit
/// TESSERACT_HOME env override (dev), then to the app_local_data_dir. Never
/// returns the read-only resource/install dir.
pub fn tesseract_home(app: &AppHandle) -> PathBuf {
    if let Ok(explicit) = std::env::var("TESSERACT_HOME") {
        return PathBuf::from(explicit);
    }
    app.path()
        .app_local_data_dir()
        .expect("no app_local_data_dir available")
}

/// The three siblings, mirroring `tesseract/paths.py`. Python derives its own
/// `install_root()` as `home_dir().parent`, so the `TESSERACT_HOME` this shell
/// exports must be `home_dir(root)` — not the root — or every Python path
/// lands one level too high.
pub fn home_dir(root: &Path) -> PathBuf {
    root.join("home")
}

pub fn app_dir(root: &Path) -> PathBuf {
    root.join("app")
}

pub fn runtime_dir(root: &Path) -> PathBuf {
    root.join("runtime")
}

/// The provisioned venv. Machine-local, so it lives under `runtime/` and is
/// never synced between PCs.
pub fn venv_dir(root: &Path) -> PathBuf {
    runtime_dir(root).join("venv")
}

/// The venv interpreter (Windows layout). Derived from `venv_dir` so the
/// location the venv is CREATED at and the one it is looked for at cannot
/// drift apart.
pub fn venv_python(root: &Path) -> PathBuf {
    venv_dir(root).join("Scripts").join("python.exe")
}

fn marker_path(root: &Path) -> PathBuf {
    runtime_dir(root).join("provisioned.json")
}

/// True iff the provisioning marker matches AND the artifacts it claims exist
/// are actually on disk.
///
/// The marker alone is not proof of a working install: a user can delete
/// `venv/`, antivirus can quarantine the interpreter, a disk error can eat the
/// clone, and an installer reinstall that preserves user data leaves the
/// marker behind regardless. Trusting it blindly meant a damaged install
/// respawned against a missing interpreter on every launch forever, with no
/// path back — the health check is what routes those cases into `provision()`,
/// which is already written to repair rather than duplicate work
/// (`clone_app_dir` no-ops on a complete clone; `uv` reuses its download
/// cache).
pub fn is_provisioned(home: &Path) -> bool {
    marker_matches(home) && install_is_intact(home)
}

fn marker_matches(home: &Path) -> bool {
    let Ok(text) = std::fs::read_to_string(marker_path(home)) else {
        return false;
    };
    match serde_json::from_str::<serde_json::Value>(&text) {
        Ok(v) => v.get("deps_version").and_then(|d| d.as_str()) == Some(DEPS_VERSION),
        Err(_) => false,
    }
}

/// The two artifacts the supervisor cannot start without: the source clone
/// and the venv interpreter that runs it. Deliberately a cheap existence
/// check, not an import or a health probe — this runs on the launch hot path
/// before any window is shown, and a deeper check would trade startup latency
/// for cases `provision()` already handles.
fn install_is_intact(root: &Path) -> bool {
    app_dir(root).join(".git").exists() && venv_python(root).exists()
}

/// Drops the provisioning marker so the next launch re-provisions.
///
/// The escape hatch for a state `update_apply` cannot repair in place: with
/// the marker present, a wedged install respawns the same broken combination
/// forever. Removing it is what turns that dead end into a self-repair on the
/// next start. Failure to remove is logged, never propagated — the caller is
/// already on an error path and has a worse failure to report.
pub fn invalidate_marker(home: &Path) {
    match std::fs::remove_file(marker_path(home)) {
        Ok(()) => {
            crate::shell_log::log("provisioning marker cleared — next launch will re-provision")
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => crate::shell_log::log_error(&format!("could not clear provisioning marker: {e}")),
    }
}

/// Windows-only: stops a spawned subprocess from flashing a console window.
/// One helper rather than the same `#[cfg(windows)]` + magic-constant block
/// repeated at every spawn site in the shell.
pub(crate) fn hide_console(cmd: &mut std::process::Command) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    #[cfg(not(windows))]
    {
        let _ = cmd;
    }
}

/// The optional assets provisioning fetches best-effort, retried on EVERY
/// launch. Each entry is a python `-m` module that is a cheap no-op once its
/// artifact is on disk.
///
/// `provision()` writes the `provisioned.json` marker unconditionally, and the
/// marker check does not look at whether any of these landed — so a fetch that
/// failed during first run (offline, transient upstream outage, an interrupted
/// download) would otherwise never be retried and the capability would stay
/// off forever with no path back.
///
/// `ensure_ollama` runs with `--no-install` here, unlike at provisioning time:
/// re-running the vendor installer download on every launch would mean
/// re-fetching hundreds of megabytes for a machine where the install was
/// declined. `--no-install` still starts a present-but-stopped Ollama and
/// pulls the embedding model when the earlier pull was the part that failed.
///
/// The three voice fetchers are listed unconditionally even though first-run
/// setup lets the operator decline speech entirely. They are not gated here
/// because each one reads the operator's own config and downloads only what
/// `roles.yaml` names with its provider enabled — declining a lane writes
/// that decision into config, and these then have nothing to fetch, on this
/// launch and every later one. Gating in the shell instead would put the same
/// decision in two places and let them disagree.
/// `provision_hardware` is here for its package half: it is how a machine
/// that was offline on first run — or one whose wheels were wrong before this
/// version shipped — reaches the GPU path without a reinstall. When they
/// already resolve it returns without invoking the installer at all, which is
/// what keeps an every-launch retry free.
///
/// That it can therefore pull ~2.2 GB of CUDA wheels in the background on an
/// already-provisioned machine is a WEIGHED DECISION, not an oversight: the
/// standing rule is that the install detects what the machine can sustain and
/// then gives it that, and a computer whose speech is unusably slow is not
/// better off waiting to be asked. It is bounded — nothing is fetched when
/// the packages already load, when there is no CUDA device, or when the
/// operator declined the engines the wheels would accelerate. Surfacing the
/// download while it happens is the provisioning-transparency work, not a
/// reason to withhold the capability until then.
///
/// These are spawned CONCURRENTLY, so nothing here may depend on running
/// before another entry. The model-choice half is ordered in
/// `provision_stages` instead. The one case where that matters is an install
/// whose first run never got to profile: the fetcher can then read
/// `providers.yaml` in the same moment the profiler is rewriting it and
/// fetch the outgoing model. It settles on the next launch — the profile is
/// recorded by then, and the fetcher is a no-op once the snapshot is present.
const LAUNCH_REFRESH_ASSETS: [&[&str]; 6] = [
    &["-m", "tesseract.scripts.provision_hardware"],
    &["-m", "tesseract.scripts.fetch_whisper_model"],
    &["-m", "tesseract.scripts.fetch_kokoro_voice"],
    &["-m", "tesseract.scripts.fetch_piper_voice"],
    &["-m", "tesseract.scripts.fetch_reranker_model"],
    &["-m", "tesseract.scripts.ensure_ollama", "--no-install"],
];

/// Fire-and-forget retry of every `LAUNCH_REFRESH_ASSETS` entry, on EVERY
/// launch (not gated by `is_provisioned`).
///
/// Deliberately uses `spawn()`, not `output()`: this must never add
/// perceptible latency to launch, so it is never waited on — the caller
/// starts the supervisor immediately afterward regardless of whether these
/// subprocesses have finished, or even whether they could be spawned at all.
pub fn refresh_optional_assets(root: &Path) {
    for args in LAUNCH_REFRESH_ASSETS {
        let mut cmd = std::process::Command::new(venv_python(root));
        cmd.args(args);
        point_at_state_root(&mut cmd, root);
        hide_console(&mut cmd);
        let _ = cmd.spawn();
    }
}

/// The result of a failed `provision()` call. `NeedsToken` is narrowly
/// scoped to the one clone-stage failure shape the shell can act on without
/// operator intervention — a 401/403/404-shaped libgit2 error — mirroring
/// the auth-vs-network-vs-disk classification `classify_clone_error` already
/// performs, rather than inventing a parallel scheme. Every other stage
/// (python install, venv, deps, playwright, marker write) can only ever
/// produce `Other`.
#[derive(Debug)]
pub enum ProvisionError {
    NeedsToken(String),
    Other(String),
}

impl std::fmt::Display for ProvisionError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ProvisionError::NeedsToken(m) | ProvisionError::Other(m) => write!(f, "{m}"),
        }
    }
}

/// Every non-clone provisioning stage already produces a bare `String`
/// (`run_uv`/`run_python`/plain filesystem errors); this lets every existing
/// `?` call site in `provision()` keep compiling unchanged while only the
/// clone stage constructs `ProvisionError` explicitly.
impl From<String> for ProvisionError {
    fn from(s: String) -> Self {
        ProvisionError::Other(s)
    }
}

/// Emits the progress event the splash screen listens for AND appends the
/// same line to the shell's durable log, so every stage a user sees on
/// screen also lands in `<TESSERACT_HOME>/logs/shell.log` for later
/// diagnosis.
fn emit_progress(app: &AppHandle, msg: &str) {
    crate::shell_log::log(msg);
    let _ = app.emit("provision-progress", msg.to_string());
}

/// Clone the source repo into the per-user home, download Python + deps
/// online, editable-install tesseract, and fetch the browser engine. On
/// success writes the marker and returns the venv python path. Emits
/// "provision-progress" events (payload: a String line); on a clone auth
/// failure with no working token, returns `ProvisionError::NeedsToken`
/// instead of failing outright, so the caller can prompt for one and retry.
pub fn provision(app: &AppHandle, home: &Path) -> Result<PathBuf, ProvisionError> {
    let uv = resolve_uv(app)?;

    // Before anything looks at `app/`: an update that died between its two
    // renames leaves no `app/` at all, and `clone_app_dir` would read that
    // as a fresh install and re-download over a tree that is sitting right
    // there as `app.old`.
    match crate::app_swap::recover_interrupted(&app_dir(home)) {
        Ok(true) => {
            emit_progress(app, "Recovering from an interrupted update…");
            crate::shell_log::log("provision: restored app/ after an interrupted update");
        }
        Ok(false) => {}
        Err(e) => crate::shell_log::log(&format!("provision: {e}")),
    }

    emit_progress(app, "Downloading TESSERACT…");
    clone_app_dir(&app_dir(home), home)?;

    provision_stages(
        home,
        &uv,
        &|msg| emit_progress(app, msg),
        &|program, args| run_uv(program, args),
        &|program, args| run_python(program, args, home),
    )
    .map_err(ProvisionError::from)
}

/// Locates the bundled `uv.exe` the installer ships as a Tauri resource.
/// Shared by `provision()` and `reinstall_deps` so the resource path and its
/// error text exist in exactly one place.
pub fn resolve_uv(app: &AppHandle) -> Result<PathBuf, String> {
    let uv = app
        .path()
        .resolve("resources/binaries/uv.exe", BaseDirectory::Resource)
        .map_err(|e| format!("resource resources/binaries/uv.exe: {e}"))?;
    let _ = UV_PATH.set(uv.clone());
    Ok(uv)
}

/// Every post-clone provisioning stage, in order, with the progress sink and
/// both subprocess runners injected.
///
/// Split out from `provision()` because `provision()` needs a live Tauri
/// `AppHandle` (for resource resolution and event emission) that no unit test
/// can construct — which left the entire first-run sequence, the stage
/// ordering, the argument construction, and the marker write untested. Here
/// the same logic runs against recording stubs.
fn provision_stages(
    home: &Path,
    uv: &Path,
    progress: &dyn Fn(&str),
    run_uv: &dyn Fn(&Path, &[&str]) -> Result<(), String>,
    run_python: &dyn Fn(&Path, &[&str]) -> Result<(), String>,
) -> Result<PathBuf, String> {
    let venv = venv_dir(home);
    let py = venv_python(home);

    progress("Downloading Python…");
    run_uv(uv, &["python", "install", "3.12"])?;

    progress("Creating Python environment…");
    run_uv(
        uv,
        &[
            "venv",
            "--clear",
            "--python",
            "3.12",
            &venv.to_string_lossy(),
        ],
    )?;

    progress("Downloading dependencies…");
    reinstall_deps_with(uv, home, run_uv)?;

    progress("Downloading browser engine…");
    run_python(&py, &["-m", "playwright", "install", "chromium"])?;

    // The first stage that can run Python against the operator's state: it
    // seeds `home/config/` from the freshly cloned templates and applies the
    // setup form's staged answers to it. Must come BEFORE the fetch stages,
    // which read that config to decide what to download — that ordering is
    // the whole reason declining a lane costs nothing.
    //
    // Best-effort: a setup that could not be applied leaves the shipped
    // defaults, which is a working install with a name the operator did not
    // choose. The Identity tab can set every one of these afterwards, so
    // failing the install over it would be a bad trade.
    progress("Applying your setup…");
    let _ = run_python(&py, &["-m", "tesseract.scripts.apply_first_run_setup"]);

    // Between the config seed and the fetchers, and that position is the
    // whole point: this decides WHICH speech model this machine should have,
    // and the fetch stage below downloads whatever `providers.yaml` names by
    // then. Run it after and the machine downloads one model and uses
    // another; run it before the seed and there is no config to write into.
    //
    // Also installs the GPU wheels the profile calls for — the step that was
    // missing entirely, which is why a laptop with an NVIDIA card ran its
    // speech models on the CPU and paid 75 seconds a turn for it.
    //
    // Best-effort like every stage around it: a machine that could not be
    // profiled keeps the shipped defaults and the CPU path, which is slow
    // rather than broken.
    progress("Checking your hardware…");
    let _ = run_python(&py, &["-m", "tesseract.scripts.provision_hardware"]);

    // Best-effort, and each one reads the config the setup form just wrote:
    // an operator who declined speech has no lane named in `roles.yaml`, so
    // these find nothing to fetch and download nothing. Never propagated as
    // a provisioning failure — the scripts always exit 0 and any spawn error
    // here is swallowed too, so an offline first run still finishes install
    // with voice simply unavailable (text-only replies), same as today.
    // `refresh_optional_assets` retries them on every later launch.
    //
    // Speech-to-text first: without this snapshot faster-whisper downloads
    // an unpinned one at the operator's first utterance, which is the one
    // moment a multi-minute stall is least acceptable.
    progress("Downloading speech recognition model…");
    let _ = run_python(&py, &["-m", "tesseract.scripts.fetch_whisper_model"]);

    progress("Downloading voice models…");
    let _ = run_python(&py, &["-m", "tesseract.scripts.fetch_kokoro_voice"]);
    let _ = run_python(&py, &["-m", "tesseract.scripts.fetch_piper_voice"]);

    // Best-effort, same contract: fetches the pinned cross-encoder reranker
    // (~23 MB, sha256-pinned in the providers catalog) so retrieval ranks with
    // the reranker instead of pure RRF. Absent files are a supported degraded
    // mode, which is exactly why this must not be fatal — and also why it was
    // worth adding: without it every shipped install ran degraded in silence.
    progress("Downloading reranker model…");
    let _ = run_python(&py, &["-m", "tesseract.scripts.fetch_reranker_model"]);

    // Best-effort, same contract as the voice model above: installs Ollama
    // if absent and pulls the configured embedding model, so semantic
    // search works on a fresh machine instead of silently degrading to
    // keyword-only. The script always exits 0 and any spawn error here is
    // swallowed, so an offline first run still completes.
    progress("Setting up embeddings…");
    let _ = run_python(&py, &["-m", "tesseract.scripts.ensure_ollama"]);

    // Marker last, so a partial provision never reads as complete.
    write_marker(home)?;

    progress("Ready.");
    Ok(py)
}

/// Writes the completion marker `is_provisioned` reads. Only ever called as
/// the final step of a fully successful provision.
fn write_marker(home: &Path) -> Result<(), String> {
    let runtime = runtime_dir(home);
    std::fs::create_dir_all(&runtime).map_err(|e| e.to_string())?;
    let body = serde_json::json!({ "deps_version": DEPS_VERSION });
    std::fs::write(marker_path(home), body.to_string()).map_err(|e| e.to_string())
}

/// Editable install: pulls tesseract's CPU-only core deps from
/// pyproject.toml (CUDA is an opt-in extra) and drops a `.pth` so `import
/// tesseract` works without cwd/PYTHONPATH wiring. pyproject.toml lives
/// inside the package dir (`app/tesseract/`); package-dir puts `app/` on
/// sys.path. Shared by `provision()` (first run) and Task 12's `update_apply`
/// (re-run only when `pyproject.toml` changed across an update).
pub fn reinstall_deps(app: &AppHandle, home: &Path) -> Result<(), String> {
    let uv = resolve_uv(app)?;
    reinstall_deps_with(&uv, home, &|program, args| run_uv(program, args))
}

/// `reinstall_deps` with the `uv` path and runner supplied by the caller, so
/// `provision_stages` reuses the exact argv the update path uses instead of
/// rebuilding it, and tests can assert on it.
fn reinstall_deps_with(
    uv: &Path,
    home: &Path,
    run_uv: &dyn Fn(&Path, &[&str]) -> Result<(), String>,
) -> Result<(), String> {
    let pkg = app_dir(home).join("tesseract");
    run_uv(
        uv,
        &[
            "pip",
            "install",
            "-e",
            &pkg.to_string_lossy(),
            "--python",
            &venv_python(home).to_string_lossy(),
        ],
    )
}

/// The .exe is a thin shell: the source tree and all third-party deps come
/// from the network on first run. A `.git` dir marks a complete clone; its
/// absence (missing entirely, or left behind by an interrupted first run)
/// means we clear whatever is there and re-clone, so a partial `app_dir` can
/// never wedge the app in an unrecoverable state. No-ops if already cloned.
fn clone_app_dir(app_dir: &Path, home: &Path) -> Result<(), ProvisionError> {
    clone_app_dir_with(app_dir, home, &crate::repo::repo_url())
}

/// `clone_app_dir` with the repo URL passed explicitly, so tests can drive
/// the real guard/clear/clone/error-mapping logic against a local throwaway
/// repo without mutating the process-global `TESSERACT_REPO_URL` env var
/// (which `repo::tests` also exercises and would race with under parallel
/// `cargo test`).
fn clone_app_dir_with(app_dir: &Path, home: &Path, url: &str) -> Result<(), ProvisionError> {
    if app_dir.join(".git").exists() {
        return Ok(());
    }
    if app_dir.exists() {
        remove_tree(app_dir).map_err(|e| fs_error_message(&e.to_string()))?;
    }

    // Clone into a sibling staging dir, never `app_dir` itself: libgit2
    // creates `.git`'s on-disk structure before the fetch/checkout
    // completes, so a clone that dies mid-transfer (dropped connection,
    // killed process, full disk) would otherwise leave a `.git` at
    // `app_dir` that the guard above would then mistake for a complete
    // clone on the very next run — wedging the app with no recovery path.
    // Renaming only on success keeps `app_dir` either absent or a fully
    // cloned tree, never partial.
    let staging_name = format!(
        "{}.clone-tmp",
        app_dir
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("app")
    );
    let staging = app_dir.with_file_name(staging_name);
    if staging.exists() {
        // A staging dir carrying a complete `.git` is a clone that finished
        // downloading but never got renamed into place — the rename lost a
        // race with an antivirus scan of its own freshly written files. The
        // bytes are already here and valid, so adopt them instead of deleting
        // and re-downloading the whole repo. Without this the app re-fetches
        // everything on every launch and can lose the same race forever, which
        // is exactly how a first run wedged on 2026-07-29.
        if staging_clone_is_complete(&staging) {
            match finalize_clone(&staging, app_dir) {
                Ok(()) => return Ok(()),
                Err(_) => {
                    // Still locked, or the tree is unusable — fall through to
                    // the clean re-clone path rather than reporting an error
                    // the next launch could have fixed on its own.
                    crate::shell_log::log(
                        "staging clone could not be adopted; clearing and re-cloning",
                    );
                }
            }
        }
        remove_tree(&staging).map_err(|e| fs_error_message(&e.to_string()))?;
    }
    let token = crate::repo::github_token(home);
    if let Err(e) = crate::repo::clone(url, &staging, token) {
        // Best-effort: a failed clone's partial staging dir is cleared so the
        // next attempt starts clean. `remove_tree` (not `remove_dir_all`)
        // because libgit2 leaves read-only objects behind even on failure, and
        // a plain delete would silently leave them for the next run to trip on.
        let _ = remove_tree(&staging);
        return Err(classify_clone_error(&e));
    }
    finalize_clone(&staging, app_dir).map_err(ProvisionError::from)
}

/// Whether a staging directory holds a clone that actually FINISHED, and is
/// therefore safe to rename into place instead of re-downloading.
///
/// `.git` alone is not evidence: libgit2 creates it at the very START of a
/// clone — init, then fetch, then checkout — so `.git` is present for nearly
/// the entire download. Adopting on that signal alone would promote a clone
/// interrupted at any point (app killed mid-download, sleep, crash) into
/// `app_dir`, where the guard at the top of `clone_app_dir_with` treats any
/// `app_dir/.git` as a finished clone forever. `is_provisioned` looks no
/// deeper either, so the install would fail its dependency step on every
/// launch with no repair path — re-creating the permanent wedge this adoption
/// path exists to prevent, just from a different trigger.
///
/// Completeness therefore means both halves of libgit2's sequence: HEAD
/// resolves to a real commit (fetch finished and a branch was written), and
/// the working tree actually has content (checkout finished).
fn staging_clone_is_complete(staging: &Path) -> bool {
    if !staging.join(".git").exists() {
        return false;
    }
    if crate::repo::head_oid(staging).is_err() {
        return false;
    }
    let Ok(entries) = std::fs::read_dir(staging) else {
        return false;
    };
    entries
        .flatten()
        .any(|e| e.file_name() != std::ffi::OsStr::new(".git"))
}

/// How many times a filesystem step that lost a race with another process is
/// retried, and how long the backoff sleeps grow. Windows denies renames and
/// deletions while *any* handle on the tree is open, and a freshly written
/// clone attracts exactly that: an antivirus real-time scan walking 1000+ new
/// files, a search indexer, or libgit2's own memory-mapped pack files not yet
/// released. Each is transient and clears in well under a second — but a
/// single un-retried failure aborts provisioning outright and, worse, leaves a
/// complete clone stranded in staging.
const FS_RETRY_DELAYS_MS: [u64; 5] = [50, 150, 400, 1000, 2500];

/// Runs a filesystem mutation, retrying while the error looks like a transient
/// lock. Non-lock errors (a full disk, a bad path) fail immediately — retrying
/// those just delays an unavoidable message.
fn retry_while_locked(mut op: impl FnMut() -> std::io::Result<()>) -> std::io::Result<()> {
    let mut last = match op() {
        Ok(()) => return Ok(()),
        Err(e) => e,
    };
    for delay in FS_RETRY_DELAYS_MS {
        if !is_transient_lock(&last) {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(delay));
        match op() {
            Ok(()) => return Ok(()),
            Err(e) => last = e,
        }
    }
    Err(last)
}

fn is_transient_lock(e: &std::io::Error) -> bool {
    if matches!(e.kind(), std::io::ErrorKind::PermissionDenied) {
        return true;
    }
    let msg = e.to_string().to_lowercase();
    msg.contains("being used by another process")
        || msg.contains("access is denied")
        || msg.contains("permission denied")
}

/// `remove_dir_all` that survives Windows' two standard obstacles: read-only
/// files and transient locks.
///
/// libgit2 writes pack files and loose objects **read-only**, and Windows
/// refuses to delete a read-only file — so a plain `remove_dir_all` over an
/// abandoned `.git` fails with Access Denied. The read-only bit is cleared
/// across the tree first, then the delete is retried through
/// `retry_while_locked`.
pub(crate) fn remove_tree(dir: &Path) -> std::io::Result<()> {
    clear_readonly_recursive(dir);
    retry_while_locked(|| match std::fs::remove_dir_all(dir) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        other => other,
    })
}

fn clear_readonly_recursive(dir: &Path) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() && !path.is_symlink() {
            clear_readonly_recursive(&path);
        }
        if let Ok(meta) = std::fs::symlink_metadata(&path) {
            let mut perms = meta.permissions();
            if perms.readonly() {
                #[allow(clippy::permissions_set_readonly_false)]
                perms.set_readonly(false);
                let _ = std::fs::set_permissions(&path, perms);
            }
        }
    }
}

/// Renames the completed staging clone into place. Its own function so tests
/// can force a rename failure (e.g. destination path occupied) without
/// fighting the guard/clear logic above.
///
/// Retried: this is the step most likely to lose a race with an antivirus scan
/// of the just-downloaded tree, and failing here is the worst possible moment
/// — the clone succeeded, the bytes are on disk, and giving up strands them.
fn finalize_clone(staging: &Path, app_dir: &Path) -> Result<(), String> {
    retry_while_locked(|| std::fs::rename(staging, app_dir))
        .map_err(|e| fs_error_message(&e.to_string()))
}

/// Points a Python subprocess at the same state root the supervisor gets.
///
/// Every `tesseract.scripts.*` helper resolves its target directories through
/// `paths.py`, which reads `TESSERACT_HOME` and otherwise falls back to the
/// package directory. In a packaged install that fallback is `app/tesseract` —
/// inside the sealed tree an update overwrites — so a fetch script run without
/// this installs its artifact where the runtime will never look for it, and
/// reads the shipped config instead of the operator's.
pub(crate) fn point_at_state_root(cmd: &mut std::process::Command, root: &Path) {
    cmd.current_dir(app_dir(root))
        .env("TESSERACT_HOME", home_dir(root));
    // Only `provision_hardware` reads it; every other stage ignores it. Set
    // here rather than at that one call site so the launch-refresh path and
    // the first-run path cannot disagree about whether it was exported.
    if let Some(uv) = UV_PATH.get() {
        cmd.env("TESSERACT_UV", uv);
    }
}

/// Runs a provisioning subprocess to completion, mapping a non-zero exit into
/// an error carrying its stderr. `label` names the tool in both error shapes
/// so `run_uv`/`run_python` stay one line each rather than two copies of this.
/// `root` is `Some` only for the Python stages, which resolve state paths.
fn run_tool(program: &Path, args: &[&str], label: &str, root: Option<&Path>) -> Result<(), String> {
    let mut cmd = std::process::Command::new(program);
    cmd.args(args);
    if let Some(root) = root {
        point_at_state_root(&mut cmd, root);
    }
    hide_console(&mut cmd);
    let out = cmd
        .output()
        .map_err(|e| format!("{label} spawn failed: {e}"))?;
    if !out.status.success() {
        return Err(format!(
            "{label} {:?} failed: {}",
            args,
            String::from_utf8_lossy(&out.stderr)
        ));
    }
    Ok(())
}

fn run_uv(uv: &Path, args: &[&str]) -> Result<(), String> {
    run_tool(uv, args, "uv", None)
}

fn run_python(py: &Path, args: &[&str], root: &Path) -> Result<(), String> {
    run_tool(py, args, "python", Some(root))
}

/// Redacts any `user:token@` userinfo segment from URLs embedded in an error
/// string, so a git2 error that echoes back the remote URL never surfaces a
/// credential to logs or the splash screen. `pub(crate)` so Task 12's
/// `update.rs` can reuse it for `check_behind` errors, which can likewise
/// embed the remote URL, rather than duplicating the redaction logic.
pub(crate) fn scrub_credentials(s: &str) -> String {
    let mut out = String::new();
    let mut rest = s;
    while let Some(scheme_pos) = rest.find("://") {
        let split_at = scheme_pos + 3;
        out.push_str(&rest[..split_at]);
        let after = &rest[split_at..];
        let slash_pos = after.find('/').unwrap_or(after.len());
        match after[..slash_pos].find('@') {
            Some(at) => {
                out.push_str("<redacted>@");
                rest = &after[at + 1..];
            }
            None => {
                rest = after;
            }
        }
    }
    out.push_str(rest);
    out
}

fn is_auth_failure(lower: &str) -> bool {
    lower.contains("401")
        || lower.contains("403")
        || lower.contains("404")
        || lower.contains("authentication")
        || lower.contains("not found")
}

fn is_network_failure(lower: &str) -> bool {
    lower.contains("resolve host")
        || lower.contains("resolve address")
        || lower.contains("could not be resolved")
        || lower.contains("failed to send request")
        || lower.contains("could not connect")
        || lower.contains("network")
        || lower.contains("timed out")
        || lower.contains("timeout")
}

fn is_disk_space_failure(lower: &str) -> bool {
    lower.contains("no space left") || lower.contains("not enough space")
}

/// Turns a raw `repo::clone` error (which may embed a git2/libcurl message)
/// into an actionable, credential-free `ProvisionError`. Auth/not-found
/// shaped errors (401/403/404/"authentication"/"not found") are the one
/// class the shell can recover from without operator intervention — they
/// become `NeedsToken` so the caller can show the in-app token prompt and
/// retry instead of telling the user to hand-create a folder. Every other
/// class (network, disk space, unrecognized) becomes `Other`, matching the
/// splash's pre-existing dead-end display.
fn classify_clone_error(raw: &str) -> ProvisionError {
    let scrubbed = scrub_credentials(raw);
    let lower = scrubbed.to_lowercase();
    if is_auth_failure(&lower) {
        return ProvisionError::NeedsToken(
            "could not access the TESSERACT repository — it's private, so it needs a GitHub \
             personal access token to download its source. Paste one below."
                .to_string(),
        );
    }
    let msg = if is_network_failure(&lower) {
        "could not reach GitHub — check your internet connection and try again".to_string()
    } else if is_disk_space_failure(&lower) {
        "not enough disk space to download TESSERACT — free up space and try again".to_string()
    } else {
        format!("could not download TESSERACT ({scrubbed}) — check your connection and try again")
    };
    ProvisionError::Other(msg)
}

/// Turns a raw local-filesystem error (from clearing `app_dir`/staging or
/// finalizing the clone via rename) into an actionable, credential-free
/// string — the local-filesystem counterpart to `classify_clone_error`. These
/// paths carry no URL, but scrubbing stays applied for consistency and in
/// case a path ever gets embedded together with one.
fn fs_error_message(raw: &str) -> String {
    let scrubbed = scrub_credentials(raw);
    let lower = scrubbed.to_lowercase();
    if lower.contains("access is denied")
        || lower.contains("permission denied")
        || lower.contains("being used by another process")
    {
        "a file in the download folder was locked (often antivirus or another program \
         scanning it) — restarting TESSERACT usually resolves this; if it keeps happening, \
         pause any antivirus/backup scan and try again"
            .to_string()
    } else if is_disk_space_failure(&lower) {
        "not enough disk space to download TESSERACT — free up space and try again".to_string()
    } else {
        format!("could not prepare the download folder ({scrubbed}) — try restarting TESSERACT and try again")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::test_support::{init_repo, write_commit, TempDir};
    use std::cell::RefCell;

    /// Builds a throwaway local git repo (the "production repo" stand-in)
    /// with one commit, so `clone_app_dir_with` can clone a real, working
    /// tree without any network access.
    fn make_origin(base: &TempDir) -> std::path::PathBuf {
        let origin_path = base.join("origin");
        let origin = init_repo(&origin_path);
        write_commit(
            &origin,
            "pyproject.toml",
            "[project]\nname=\"tesseract\"\n",
            "c1",
        );
        origin_path
    }

    #[test]
    fn clone_app_dir_with_clones_into_a_missing_dir() {
        let base = TempDir::new("fresh");
        let origin = make_origin(&base);
        let home = base.join("home");
        let app_dir = home.join("app");

        clone_app_dir_with(&app_dir, &home, &origin.to_string_lossy())
            .expect("clone into a missing app_dir should succeed");

        assert!(app_dir.join(".git").exists());
        assert!(app_dir.join("pyproject.toml").exists());
    }

    #[test]
    fn clone_app_dir_with_recovers_from_an_interrupted_clone() {
        let base = TempDir::new("interrupted");
        let origin = make_origin(&base);
        let home = base.join("home");
        let app_dir = home.join("app");

        // Simulate a first run that died mid-copy: app_dir exists with a
        // stray file but no `.git` — the guard this task added must clear
        // it and re-clone rather than leaving the app permanently wedged.
        std::fs::create_dir_all(&app_dir).unwrap();
        std::fs::write(app_dir.join("partial.tmp"), b"leftover").unwrap();

        clone_app_dir_with(&app_dir, &home, &origin.to_string_lossy())
            .expect("clone should recover from a .git-less app_dir");

        assert!(app_dir.join(".git").exists());
        assert!(app_dir.join("pyproject.toml").exists());
        assert!(
            !app_dir.join("partial.tmp").exists(),
            "the stray leftover from the interrupted run must be cleared"
        );
    }

    /// Regression — first-run failure observed on a real machine 2026-07-29.
    ///
    /// The clone finished (complete `.git`, 15 MB on disk) but the rename into
    /// place lost a race with an antivirus scan of its own freshly written
    /// files. Provisioning aborted, and every relaunch deleted the good clone
    /// and re-downloaded it, able to lose the same race forever. A staging dir
    /// holding a complete clone must now be adopted, not discarded.
    #[test]
    fn clone_app_dir_with_adopts_a_complete_staging_clone_instead_of_re_downloading() {
        let base = TempDir::new("adopt-staging");
        let home = base.join("home");
        let app_dir = home.join("app");
        let origin = base.join("origin");
        let repo = crate::test_support::init_repo(&origin);
        crate::test_support::write_commit(
            &repo,
            "README.md",
            "hello
",
            "c1",
        );
        drop(repo);

        // Simulate the stranded state: a complete clone sitting in staging,
        // with no `app/` and an unreachable URL so any re-download would fail.
        let staging = home.join("app.clone-tmp");
        std::fs::create_dir_all(&home).unwrap();
        crate::repo::clone(&origin.to_string_lossy(), &staging, None).expect("seed staging");
        assert!(staging.join(".git").exists());

        clone_app_dir_with(&app_dir, &home, "https://127.0.0.1:1/unreachable.git")
            .expect("a complete staging clone must be adopted, not re-downloaded");

        assert!(app_dir.join(".git").exists(), "staging must land at app/");
        assert!(
            app_dir.join("README.md").exists(),
            "the adopted tree must be the real content, not an empty dir"
        );
        assert!(!staging.exists(), "staging must not survive adoption");
    }

    /// Reviewer catch, 2026-07-29: adoption keyed on `.git` alone was WRONG.
    /// libgit2 creates `.git` at the START of a clone (init -> fetch ->
    /// checkout), so an interrupted download looks identical to a finished one.
    /// Adopting it would move a partial tree into `app_dir`, where the guard at
    /// the top of `clone_app_dir_with` treats any `.git` as complete forever —
    /// re-creating the permanent wedge adoption exists to prevent.
    #[test]
    fn a_partial_staging_clone_is_never_adopted() {
        let base = TempDir::new("partial-staging");
        let home = base.join("home");
        let staging = home.join("app.clone-tmp");

        // Interrupted before checkout: `.git` exists, nothing else does.
        std::fs::create_dir_all(staging.join(".git")).unwrap();
        assert!(
            !staging_clone_is_complete(&staging),
            "a bare .git with no commit and no working tree must not count as complete"
        );

        // Interrupted after fetch but before checkout: HEAD unresolvable.
        let origin = base.join("origin");
        let repo = crate::test_support::init_repo(&origin);
        crate::test_support::write_commit(
            &repo,
            "README.md",
            "hi
",
            "c1",
        );
        drop(repo);
        let good = base.join("good");
        crate::repo::clone(&origin.to_string_lossy(), &good, None).expect("seed");
        assert!(
            staging_clone_is_complete(&good),
            "a genuinely finished clone must still be adoptable"
        );

        // Strip the working tree: fetch done, checkout never happened.
        for entry in std::fs::read_dir(&good).unwrap().flatten() {
            if entry.file_name() != std::ffi::OsStr::new(".git") {
                let p = entry.path();
                if p.is_dir() {
                    let _ = std::fs::remove_dir_all(&p);
                } else {
                    let _ = std::fs::remove_file(&p);
                }
            }
        }
        assert!(
            !staging_clone_is_complete(&good),
            "a repo with no checked-out working tree must not count as complete"
        );
    }

    /// `remove_tree` must handle the read-only files libgit2 writes into
    /// `.git/objects`; a plain `remove_dir_all` fails on them with Access
    /// Denied on Windows, which was the other half of the same wedge.
    #[test]
    fn remove_tree_deletes_read_only_files() {
        let base = TempDir::new("readonly-rm");
        let dir = base.join("tree/nested");
        std::fs::create_dir_all(&dir).unwrap();
        let file = dir.join("locked.pack");
        std::fs::write(&file, b"objects").unwrap();
        let mut perms = std::fs::metadata(&file).unwrap().permissions();
        perms.set_readonly(true);
        std::fs::set_permissions(&file, perms).unwrap();

        remove_tree(&base.join("tree")).expect("read-only contents must not block removal");
        assert!(!base.join("tree").exists());
    }

    #[test]
    fn remove_tree_is_a_no_op_on_a_missing_directory() {
        let base = TempDir::new("rm-missing");
        remove_tree(&base.join("never-existed")).expect("absent dir must not be an error");
    }

    #[test]
    fn is_transient_lock_matches_windows_sharing_violations_only() {
        use std::io::{Error, ErrorKind};
        assert!(is_transient_lock(&Error::new(
            ErrorKind::PermissionDenied,
            "denied"
        )));
        assert!(is_transient_lock(&Error::other(
            "The process cannot access the file because it is being used by another process."
        )));
        assert!(
            !is_transient_lock(&Error::other("There is not enough space on the disk.")),
            "a full disk must fail fast, not burn the retry budget"
        );
    }

    #[test]
    fn clone_app_dir_with_leaves_no_partial_git_after_a_failed_clone() {
        let base = TempDir::new("failed-clone");
        // No real origin at this path — repo::clone will init a repo, add
        // the remote, then fail at fetch. This is exactly the scenario the
        // staging-dir fix guards: libgit2 creates `.git` before the fetch
        // completes, so without staging this would leave a broken `.git`
        // directly at `app_dir`.
        let bogus_origin = base.join("does-not-exist");
        let home = base.join("home");
        let app_dir = home.join("app");

        let err = clone_app_dir_with(&app_dir, &home, &bogus_origin.to_string_lossy())
            .expect_err("cloning a nonexistent origin must fail")
            .to_string();
        assert!(!err.is_empty());

        assert!(
            !app_dir.join(".git").exists(),
            "a failed clone must never leave a partial `.git` at app_dir — \
             that would make the next run's guard mistake it for complete \
             and wedge the app permanently"
        );
        let staging = app_dir.with_file_name("app.clone-tmp");
        assert!(
            !staging.exists(),
            "the staging dir must be cleaned up after a failed clone"
        );

        // Recovery: a subsequent call against a real origin must still succeed.
        let origin = make_origin(&base);
        clone_app_dir_with(&app_dir, &home, &origin.to_string_lossy())
            .expect("a real clone after a failed one must succeed");
        assert!(app_dir.join(".git").exists());
        assert!(app_dir.join("pyproject.toml").exists());
    }

    /// The three real failure paths a fs-level error can surface through
    /// (clearing a `.git`-less `app_dir`, clearing a leftover staging dir,
    /// finalizing via rename) each used to `map_err` into a bare
    /// `format!` string carrying the raw OS error text — the exact "Setup
    /// failed: Access is denied. (os error 5)" the coordinator flagged. All
    /// three now route through `fs_error_message`. These three tests force
    /// each one with a real (not synthetic) filesystem error.
    #[test]
    fn clone_app_dir_with_classifies_a_real_clear_failure_on_app_dir() {
        let base = TempDir::new("clear-appdir-fail");
        let home = base.join("home");
        let app_dir = home.join("app");
        // app_dir exists as a plain FILE, not a directory: remove_dir_all
        // on it is a real, deterministic io::Error (not a directory).
        std::fs::create_dir_all(&home).unwrap();
        std::fs::write(&app_dir, b"not a directory").unwrap();

        let err = clone_app_dir_with(&app_dir, &home, "https://example.invalid/unused.git")
            .expect_err("clearing a file where a directory is expected must fail")
            .to_string();
        assert!(
            !err.starts_with("clear "),
            "must not be the old raw 'clear <path>: <os error>' format: {err}"
        );
        assert!(!err.is_empty());
    }

    #[test]
    fn clone_app_dir_with_classifies_a_real_clear_failure_on_staging() {
        let base = TempDir::new("clear-staging-fail");
        let home = base.join("home");
        let app_dir = home.join("app");
        // app_dir absent (so the first clear is skipped), but a leftover
        // staging dir exists as a plain FILE from some prior run.
        std::fs::create_dir_all(&home).unwrap();
        std::fs::write(home.join("app.clone-tmp"), b"not a directory").unwrap();

        let err = clone_app_dir_with(&app_dir, &home, "https://example.invalid/unused.git")
            .expect_err("clearing a file where the staging directory is expected must fail")
            .to_string();
        assert!(
            !err.starts_with("clear "),
            "must not be the old raw 'clear <path>: <os error>' format: {err}"
        );
        assert!(!err.is_empty());
    }

    #[test]
    fn finalize_clone_classifies_a_real_rename_failure() {
        let base = TempDir::new("rename-fail");
        let staging = base.join("staging");
        std::fs::create_dir_all(&staging).unwrap();
        std::fs::write(staging.join("f.txt"), b"x").unwrap();
        // Destination exists as a non-empty directory: renaming another
        // directory onto it is a real, deterministic failure on Windows
        // (unlike an existing plain file, which rename can replace).
        let app_dir = base.join("app_dir_as_existing_nonempty_dir");
        std::fs::create_dir_all(&app_dir).unwrap();
        std::fs::write(app_dir.join("occupant.txt"), b"already here").unwrap();

        let err = finalize_clone(&staging, &app_dir)
            .expect_err("renaming a directory onto an existing non-empty directory must fail");
        assert!(
            !err.starts_with("finalize clone"),
            "must not be the old raw 'finalize clone <a> -> <b>: <os error>' format: {err}"
        );
        assert!(!err.is_empty());
    }

    #[test]
    fn fs_error_message_classifies_a_locked_file() {
        let msg = fs_error_message("Access is denied. (os error 5)");
        assert!(msg.to_lowercase().contains("antivirus") || msg.to_lowercase().contains("locked"));
        assert!(!msg.contains("os error 5"));
    }

    #[test]
    fn fs_error_message_classifies_disk_space_failures() {
        let msg = fs_error_message("There is not enough space on the disk. (os error 112)");
        assert!(msg.contains("disk space"));
    }

    #[test]
    fn fs_error_message_falls_back_for_unrecognized_errors() {
        let msg = fs_error_message("some other io failure");
        assert!(msg.contains("could not prepare the download folder"));
        assert!(msg.contains("some other io failure"));
    }

    #[test]
    fn clone_app_dir_with_is_a_noop_when_already_cloned() {
        let base = TempDir::new("noop");
        let origin = make_origin(&base);
        let home = base.join("home");
        let app_dir = home.join("app");

        clone_app_dir_with(&app_dir, &home, &origin.to_string_lossy()).expect("initial clone");
        // Mark the existing clone so a second call touching it would be observable.
        std::fs::write(app_dir.join("venv-marker.txt"), b"do-not-touch").unwrap();

        clone_app_dir_with(&app_dir, &home, &origin.to_string_lossy())
            .expect("second call against an already-cloned app_dir should no-op");

        assert!(
            app_dir.join("venv-marker.txt").exists(),
            "an already-cloned app_dir must not be cleared or re-cloned"
        );
    }

    #[test]
    fn scrub_credentials_redacts_userinfo_but_keeps_the_rest() {
        let raw = "clone https://x-access-token:ghp_supersecret@github.com/karakijihad/Tesseract.git: unexpected http status code: 401";
        let scrubbed = scrub_credentials(raw);
        assert!(!scrubbed.contains("ghp_supersecret"));
        assert!(!scrubbed.contains("x-access-token"));
        assert!(scrubbed.contains("https://<redacted>@github.com/karakijihad/Tesseract.git"));
        assert!(scrubbed.contains("401"));
    }

    #[test]
    fn scrub_credentials_is_a_noop_without_userinfo() {
        let raw = "clone https://github.com/karakijihad/Tesseract.git: could not resolve host";
        assert_eq!(scrub_credentials(raw), raw);
    }

    #[test]
    fn classify_clone_error_maps_auth_failures_to_needs_token_without_leaking_the_token() {
        let raw = "clone https://x-access-token:ghp_supersecret@github.com/karakijihad/Tesseract.git: unexpected http status code: 401 Unauthorized";
        match classify_clone_error(raw) {
            ProvisionError::NeedsToken(msg) => {
                assert!(!msg.contains("ghp_supersecret"));
                assert!(msg.to_lowercase().contains("token"));
            }
            ProvisionError::Other(msg) => panic!("401 must classify as NeedsToken, got: {msg}"),
        }
    }

    #[test]
    fn classify_clone_error_maps_not_found_to_needs_token() {
        // A private repo an unauthenticated clone can't see typically 404s
        // rather than 401/403 — this must classify the same as an explicit
        // auth failure, not fall through to the generic "Other" message.
        let raw = "clone https://github.com/karakijihad/Tesseract.git: unexpected http status code: 404 Not Found";
        match classify_clone_error(raw) {
            ProvisionError::NeedsToken(_) => {}
            ProvisionError::Other(msg) => panic!("404 must classify as NeedsToken, got: {msg}"),
        }
    }

    #[test]
    fn classify_clone_error_classifies_network_failures_as_other() {
        let raw = "clone https://github.com/karakijihad/Tesseract.git: failed to resolve host";
        match classify_clone_error(raw) {
            ProvisionError::Other(msg) => assert!(msg.contains("internet connection")),
            ProvisionError::NeedsToken(msg) => {
                panic!("network failure must not classify as NeedsToken, got: {msg}")
            }
        }
    }

    /// Regression fixture: the exact (credential-free) error text libgit2's
    /// WinHTTP backend produces for a real, unresolvable host on Windows —
    /// captured from a live run against
    /// `https://this-host-can-never-resolve.invalid/...`.
    /// The original phrase list (`"resolve host"`/`"resolve address"`) did
    /// not match this real wording and fell through to the generic fallback
    /// message instead of the friendlier network one — still `Other`, never
    /// `NeedsToken`, but worth fixing for message accuracy.
    #[test]
    fn classify_clone_error_matches_the_real_windows_dns_failure_wording() {
        let raw = "clone https://this-host-can-never-resolve.invalid/owner/repo.git: failed to \
                   send request: The server name or address could not be resolved\r\n; class=Os (2)";
        match classify_clone_error(raw) {
            ProvisionError::Other(msg) => assert!(msg.contains("internet connection")),
            ProvisionError::NeedsToken(msg) => {
                panic!("a DNS failure must not classify as NeedsToken, got: {msg}")
            }
        }
    }

    #[test]
    fn classify_clone_error_classifies_disk_space_failures_as_other() {
        let raw = "clone https://github.com/karakijihad/Tesseract.git: no space left on device";
        match classify_clone_error(raw) {
            ProvisionError::Other(msg) => assert!(msg.contains("disk space")),
            ProvisionError::NeedsToken(msg) => {
                panic!("disk space failure must not classify as NeedsToken, got: {msg}")
            }
        }
    }

    /// Manual, network-touching proof that the auth-failure/retry loop
    /// works against a REAL private GitHub repo, at the exact layer the
    /// shell drives (clone + classify + token file), against a private repo.
    /// `#[ignore]`d because it requires
    /// outbound internet access and hits github.com; run manually:
    /// `cargo test --lib clone_app_dir_with_needs_token_then_retries_after_a_saved_token -- --ignored`
    #[test]
    #[ignore]
    fn clone_app_dir_with_needs_token_then_retries_after_a_saved_token() {
        let base = TempDir::new("real-private-repo");
        let home = base.join("home");
        let app_dir = home.join("app");
        let url = "https://github.com/karakijihad/tesseract-dev.git";

        // 1) No token file exists yet: the real GitHub response for a
        //    private repo the caller can't see must classify as NeedsToken.
        match clone_app_dir_with(&app_dir, &home, url) {
            Err(ProvisionError::NeedsToken(msg)) => {
                assert!(msg.to_lowercase().contains("token"));
            }
            other => panic!(
                "expected NeedsToken against a real private repo with no token, got {other:?}"
            ),
        }
        assert!(
            !app_dir.exists(),
            "a NeedsToken failure must not leave a partial app_dir"
        );

        // 2) Save a (deliberately invalid) token to the exact path
        //    `repo::github_token` reads — mirrors `token::save_github_token`.
        let runtime = home.join("runtime");
        std::fs::create_dir_all(&runtime).unwrap();
        std::fs::write(runtime.join("github_token"), "bogus-test-token-not-real").unwrap();

        // 3) Retry: the saved token is genuinely read and sent, the real
        //    GitHub call repeats, and — since the token is invalid — it
        //    must classify as NeedsToken AGAIN (not get stuck in some other
        //    error shape), proving the "try again" loop holds for a bad
        //    token too, not just a missing one. Never leaks the token.
        match clone_app_dir_with(&app_dir, &home, url) {
            Err(ProvisionError::NeedsToken(msg)) => {
                assert!(!msg.contains("bogus-test-token-not-real"));
            }
            other => panic!("expected NeedsToken again with an invalid token, got {other:?}"),
        }
    }

    /// Manual, network-touching proof of the other half of Part 1's
    /// requirement: a non-auth failure (here, real DNS resolution failure
    /// against a host that cannot exist) must classify as `Other`, never
    /// `NeedsToken` — so the shell never shows the token prompt for a
    /// network outage. `#[ignore]`d because DNS resolution can be slow to
    /// fail depending on the network; run manually:
    /// `cargo test --lib clone_app_dir_with_unreachable_host_never_needs_a_token -- --ignored`
    #[test]
    #[ignore]
    fn clone_app_dir_with_unreachable_host_never_needs_a_token() {
        let base = TempDir::new("unreachable-host");
        let home = base.join("home");
        let app_dir = home.join("app");
        // `.invalid` is reserved by RFC 2606 — guaranteed never to resolve.
        let url = "https://this-host-can-never-resolve.invalid/owner/repo.git";

        match clone_app_dir_with(&app_dir, &home, url) {
            Err(ProvisionError::Other(msg)) => {
                assert!(
                    !msg.to_lowercase().contains("token"),
                    "a network failure must never mention a token, got: {msg}"
                );
            }
            Err(ProvisionError::NeedsToken(msg)) => {
                panic!("a DNS failure must never classify as NeedsToken, got: {msg}")
            }
            Ok(()) => panic!("cloning an unresolvable host must fail"),
        }
    }

    /// End-to-end proof of the real, env-var-driven path (`clone_app_dir`,
    /// which reads `TESSERACT_REPO_URL`/`GITHUB_TOKEN` via `repo::repo_url`/
    /// `repo::github_token` exactly as `provision()` does) against a local
    /// throwaway repo — no network access, and the production repo does not
    /// need to exist. `#[ignore]`d because it mutates process-global env
    /// vars that `repo::tests::repo_url_and_github_token_precedence` also
    /// exercises; run manually and serially to verify:
    /// `cargo test --lib clone_app_dir_end_to_end_via_env_vars -- --ignored --test-threads=1`
    #[test]
    #[ignore]
    fn clone_app_dir_end_to_end_via_env_vars() {
        let base = TempDir::new("env-e2e");
        let origin = make_origin(&base);
        let home = base.join("home");
        let app_dir = home.join("app");

        std::env::set_var("TESSERACT_REPO_URL", origin.to_string_lossy().into_owned());
        std::env::remove_var("GITHUB_TOKEN");

        clone_app_dir(&app_dir, &home).expect("env-var-driven clone should succeed");
        assert!(app_dir.join(".git").exists());
        assert!(app_dir.join("pyproject.toml").exists());

        // Second call against the now-cloned dir must no-op, matching
        // provision()'s real re-launch behavior.
        clone_app_dir(&app_dir, &home).expect("second call should no-op, not error");

        std::env::remove_var("TESSERACT_REPO_URL");
    }

    // -- is_provisioned / marker_matches / install_is_intact --------------

    #[test]
    fn is_provisioned_false_when_marker_absent() {
        let home = TempDir::new("no-marker");
        assert!(!is_provisioned(home.path()));
    }

    #[test]
    fn is_provisioned_false_when_marker_is_malformed_json() {
        let home = TempDir::new("bad-json");
        std::fs::create_dir_all(home.join("runtime")).unwrap();
        std::fs::write(home.join("runtime").join("provisioned.json"), "{not json").unwrap();
        assert!(!is_provisioned(home.path()));
    }

    #[test]
    fn is_provisioned_false_when_deps_version_mismatches() {
        let home = TempDir::new("deps-mismatch");
        std::fs::create_dir_all(home.join("runtime")).unwrap();
        std::fs::write(
            home.join("runtime").join("provisioned.json"),
            serde_json::json!({ "deps_version": "not-the-current-version" }).to_string(),
        )
        .unwrap();
        std::fs::create_dir_all(home.join("app").join(".git")).unwrap();
        let py = venv_python(home.path());
        std::fs::create_dir_all(py.parent().unwrap()).unwrap();
        std::fs::write(&py, "").unwrap();

        assert!(!is_provisioned(home.path()));
    }

    #[test]
    fn is_provisioned_false_when_app_git_is_missing() {
        let home = TempDir::new("no-app-git");
        write_marker(home.path()).unwrap();
        let py = venv_python(home.path());
        std::fs::create_dir_all(py.parent().unwrap()).unwrap();
        std::fs::write(&py, "").unwrap();

        assert!(!is_provisioned(home.path()));
    }

    #[test]
    fn is_provisioned_false_when_venv_python_is_missing() {
        let home = TempDir::new("no-venv-python");
        write_marker(home.path()).unwrap();
        std::fs::create_dir_all(home.join("app").join(".git")).unwrap();

        assert!(!is_provisioned(home.path()));
    }

    #[test]
    fn is_provisioned_true_when_marker_matches_and_both_artifacts_exist() {
        let home = TempDir::new("intact");
        write_marker(home.path()).unwrap();
        std::fs::create_dir_all(home.join("app").join(".git")).unwrap();
        let py = venv_python(home.path());
        std::fs::create_dir_all(py.parent().unwrap()).unwrap();
        std::fs::write(&py, "").unwrap();

        assert!(is_provisioned(home.path()));
    }

    // -- invalidate_marker --------------------------------------------------

    #[test]
    fn invalidate_marker_flips_is_provisioned_to_false() {
        let home = TempDir::new("invalidate");
        write_marker(home.path()).unwrap();
        std::fs::create_dir_all(home.join("app").join(".git")).unwrap();
        let py = venv_python(home.path());
        std::fs::create_dir_all(py.parent().unwrap()).unwrap();
        std::fs::write(&py, "").unwrap();
        assert!(is_provisioned(home.path()));

        invalidate_marker(home.path());

        assert!(!is_provisioned(home.path()));
        assert!(!marker_path(home.path()).exists());
    }

    #[test]
    fn invalidate_marker_is_a_silent_noop_when_no_marker_exists() {
        let home = TempDir::new("invalidate-noop");
        invalidate_marker(home.path()); // must not panic
        assert!(!marker_path(home.path()).exists());
    }

    // -- write_marker ---------------------------------------------------

    #[test]
    fn write_marker_creates_runtime_dir_and_a_marker_that_marker_matches_accepts() {
        let home = TempDir::new("write-marker");

        write_marker(home.path()).expect("write_marker should succeed");

        assert!(home.join("runtime").exists());
        assert!(marker_matches(home.path()));
    }

    // -- provision_stages -------------------------------------------------

    type Recorded = Vec<(PathBuf, Vec<String>)>;

    fn record(calls: &RefCell<Recorded>, program: &Path, args: &[&str]) {
        calls.borrow_mut().push((
            program.to_path_buf(),
            args.iter().map(|s| s.to_string()).collect(),
        ));
    }

    /// `provision_hardware.py` cannot install the GPU wheels without being
    /// handed the `uv` path, and `uv.exe` ships as a resource outside the
    /// state root, so it is the one thing the Python side cannot derive for
    /// itself. The stage-order test cannot cover this: it injects bare
    /// closures that never build a `Command`, so nothing there exercises the
    /// env at all. Without this, the handoff could be dropped and every
    /// affected machine would quietly stay on the CPU path — which is
    /// precisely the failure the stage exists to end, and it fails silently.
    /// The launch-refresh list is what carries the hardware check to an
    /// ALREADY-provisioned machine — the one that was offline during setup,
    /// or whose wheels were wrong before this version. Dropping it from here
    /// leaves those installs on the CPU path with no path back short of a
    /// reinstall, and nothing else would fail.
    #[test]
    fn the_launch_refresh_list_carries_the_hardware_check() {
        let modules: Vec<&str> = LAUNCH_REFRESH_ASSETS
            .iter()
            .filter_map(|args| args.last().copied())
            .collect();
        assert!(
            modules.contains(&"tesseract.scripts.provision_hardware"),
            "hardware profiling must be retried on every launch, got {modules:?}",
        );
        // Every entry is a `python -m <module>` pair; a malformed one would
        // spawn silently and do nothing, since these are never awaited.
        for args in LAUNCH_REFRESH_ASSETS {
            assert_eq!(args[0], "-m", "launch-refresh entries must be `-m <module>`");
        }
    }

    #[test]
    fn point_at_state_root_hands_the_uv_path_to_the_python_stages() {
        let home = TempDir::new("uv-handoff");
        let _ = UV_PATH.set(home.path().join("resources").join("uv.exe"));
        // Read back what the OnceLock actually holds rather than trusting our
        // own `set`: cargo runs the whole file in one process, so a future
        // test setting it first would win, and asserting against the local
        // value would then fail for a reason unrelated to the handoff.
        let uv = UV_PATH.get().expect("UV_PATH must be set by now").clone();

        let mut cmd = std::process::Command::new("python");
        point_at_state_root(&mut cmd, home.path());

        let envs: Vec<_> = cmd.get_envs().collect();
        let uv_env = envs
            .iter()
            .find(|(k, _)| *k == std::ffi::OsStr::new("TESSERACT_UV"))
            .map(|(_, v)| v.expect("TESSERACT_UV must carry a value"));
        assert_eq!(
            uv_env,
            Some(uv.as_os_str()),
            "the python stages must receive the resolved uv path",
        );

        // The state root still has to arrive too — this must not have been
        // bought by breaking what was already there.
        let home_env = envs
            .iter()
            .find(|(k, _)| *k == std::ffi::OsStr::new("TESSERACT_HOME"))
            .map(|(_, v)| v.expect("TESSERACT_HOME must carry a value"));
        assert_eq!(home_env, Some(home_dir(home.path()).as_os_str()));
    }

    #[test]
    fn provision_stages_runs_every_stage_in_order_with_the_exact_argv() {
        let home = TempDir::new("stages-order");
        let uv = PathBuf::from("uv-stub-path");

        let progress_log: RefCell<Vec<String>> = RefCell::new(Vec::new());
        let calls: RefCell<Recorded> = RefCell::new(Vec::new());

        let progress = |msg: &str| progress_log.borrow_mut().push(msg.to_string());
        let run_uv = |program: &Path, args: &[&str]| -> Result<(), String> {
            record(&calls, program, args);
            Ok(())
        };
        let run_python = |program: &Path, args: &[&str]| -> Result<(), String> {
            record(&calls, program, args);
            Ok(())
        };

        let py = provision_stages(home.path(), &uv, &progress, &run_uv, &run_python)
            .expect("all stages should succeed");

        assert_eq!(py, venv_python(home.path()));

        let venv = venv_dir(home.path());
        let pkg = home.join("app").join("tesseract");
        let py_path = venv_python(home.path());
        let expected: Recorded = vec![
            (
                uv.clone(),
                vec![
                    "python".to_string(),
                    "install".to_string(),
                    "3.12".to_string(),
                ],
            ),
            (
                uv.clone(),
                vec![
                    "venv".to_string(),
                    "--clear".to_string(),
                    "--python".to_string(),
                    "3.12".to_string(),
                    venv.to_string_lossy().into_owned(),
                ],
            ),
            (
                uv.clone(),
                vec![
                    "pip".to_string(),
                    "install".to_string(),
                    "-e".to_string(),
                    pkg.to_string_lossy().into_owned(),
                    "--python".to_string(),
                    py_path.to_string_lossy().into_owned(),
                ],
            ),
            (
                py_path.clone(),
                vec![
                    "-m".to_string(),
                    "playwright".to_string(),
                    "install".to_string(),
                    "chromium".to_string(),
                ],
            ),
            (
                py_path.clone(),
                vec![
                    "-m".to_string(),
                    "tesseract.scripts.apply_first_run_setup".to_string(),
                ],
            ),
            // After the config seed, before the fetchers: it chooses the
            // speech model they then download. This position is the contract.
            (
                py_path.clone(),
                vec![
                    "-m".to_string(),
                    "tesseract.scripts.provision_hardware".to_string(),
                ],
            ),
            (
                py_path.clone(),
                vec![
                    "-m".to_string(),
                    "tesseract.scripts.fetch_whisper_model".to_string(),
                ],
            ),
            (
                py_path.clone(),
                vec![
                    "-m".to_string(),
                    "tesseract.scripts.fetch_kokoro_voice".to_string(),
                ],
            ),
            (
                py_path.clone(),
                vec![
                    "-m".to_string(),
                    "tesseract.scripts.fetch_piper_voice".to_string(),
                ],
            ),
            (
                py_path.clone(),
                vec![
                    "-m".to_string(),
                    "tesseract.scripts.fetch_reranker_model".to_string(),
                ],
            ),
            (
                py_path,
                vec![
                    "-m".to_string(),
                    "tesseract.scripts.ensure_ollama".to_string(),
                ],
            ),
        ];
        assert_eq!(
            *calls.borrow(),
            expected,
            "stage order and argv must match exactly"
        );

        assert_eq!(
            *progress_log.borrow(),
            vec![
                "Downloading Python…".to_string(),
                "Creating Python environment…".to_string(),
                "Downloading dependencies…".to_string(),
                "Downloading browser engine…".to_string(),
                "Applying your setup…".to_string(),
                "Checking your hardware…".to_string(),
                "Downloading speech recognition model…".to_string(),
                "Downloading voice models…".to_string(),
                "Downloading reranker model…".to_string(),
                "Setting up embeddings…".to_string(),
                "Ready.".to_string(),
            ],
            "progress messages must fire in stage order"
        );

        assert!(marker_path(home.path()).exists());
    }

    #[test]
    fn provision_stages_aborts_when_python_install_fails() {
        let home = TempDir::new("fail-python-install");
        let uv = PathBuf::from("uv-stub");
        let call_count = RefCell::new(0u32);

        let run_uv = |_: &Path, _: &[&str]| -> Result<(), String> {
            *call_count.borrow_mut() += 1;
            Err("python install boom".to_string())
        };
        let run_python = |_: &Path, _: &[&str]| -> Result<(), String> {
            panic!("run_python must not be called when python install fails")
        };
        let progress = |_: &str| {};

        let err = provision_stages(home.path(), &uv, &progress, &run_uv, &run_python)
            .expect_err("a python-install failure must abort");

        assert_eq!(err, "python install boom");
        assert_eq!(
            *call_count.borrow(),
            1,
            "must abort after the very first stage"
        );
        assert!(!marker_path(home.path()).exists());
    }

    #[test]
    fn provision_stages_aborts_when_venv_create_fails() {
        let home = TempDir::new("fail-venv-create");
        let uv = PathBuf::from("uv-stub");
        let call_count = RefCell::new(0u32);

        let run_uv = |_: &Path, _: &[&str]| -> Result<(), String> {
            *call_count.borrow_mut() += 1;
            if *call_count.borrow() == 1 {
                Ok(())
            } else {
                Err("venv create boom".to_string())
            }
        };
        let run_python = |_: &Path, _: &[&str]| -> Result<(), String> {
            panic!("run_python must not be called when venv creation fails")
        };
        let progress = |_: &str| {};

        let err = provision_stages(home.path(), &uv, &progress, &run_uv, &run_python)
            .expect_err("a venv-create failure must abort");

        assert_eq!(err, "venv create boom");
        assert_eq!(
            *call_count.borrow(),
            2,
            "must abort right after the second stage"
        );
        assert!(!marker_path(home.path()).exists());
    }

    #[test]
    fn provision_stages_aborts_when_dependency_install_fails() {
        let home = TempDir::new("fail-deps");
        let uv = PathBuf::from("uv-stub");
        let call_count = RefCell::new(0u32);

        let run_uv = |_: &Path, _: &[&str]| -> Result<(), String> {
            *call_count.borrow_mut() += 1;
            if *call_count.borrow() < 3 {
                Ok(())
            } else {
                Err("pip install boom".to_string())
            }
        };
        let run_python = |_: &Path, _: &[&str]| -> Result<(), String> {
            panic!("run_python must not be called when dependency install fails")
        };
        let progress = |_: &str| {};

        let err = provision_stages(home.path(), &uv, &progress, &run_uv, &run_python)
            .expect_err("a dependency-install failure must abort");

        assert_eq!(err, "pip install boom");
        assert_eq!(
            *call_count.borrow(),
            3,
            "must abort right after the third stage"
        );
        assert!(!marker_path(home.path()).exists());
    }

    #[test]
    fn provision_stages_aborts_when_playwright_install_fails() {
        let home = TempDir::new("fail-playwright");
        let uv = PathBuf::from("uv-stub");
        let python_calls = RefCell::new(0u32);

        let run_uv = |_: &Path, _: &[&str]| -> Result<(), String> { Ok(()) };
        let run_python = |_: &Path, _: &[&str]| -> Result<(), String> {
            *python_calls.borrow_mut() += 1;
            Err("playwright install boom".to_string())
        };
        let progress = |_: &str| {};

        let err = provision_stages(home.path(), &uv, &progress, &run_uv, &run_python)
            .expect_err("a playwright-install failure must abort");

        assert_eq!(err, "playwright install boom");
        assert_eq!(
            *python_calls.borrow(),
            1,
            "must abort at the first run_python call, never reaching the voice-model stage"
        );
        assert!(!marker_path(home.path()).exists());
    }

    /// The best-effort stages are keyed by MODULE NAME, not by call index.
    /// These assertions used to count `run_python` calls positionally, so
    /// adding a stage anywhere ahead of them broke tests that had nothing to
    /// do with the change and said nothing useful about what went wrong.
    fn failing_module_run<'a>(
        failing: &'static str,
        seen: &'a RefCell<Vec<String>>,
    ) -> impl Fn(&Path, &[&str]) -> Result<(), String> + 'a {
        move |_: &Path, args: &[&str]| {
            let module = args.last().copied().unwrap_or_default().to_string();
            seen.borrow_mut().push(module.clone());
            if module == failing {
                Err(format!("{failing} boom"))
            } else {
                Ok(())
            }
        }
    }

    /// Every optional asset behind a failure must still be attempted: an
    /// offline machine that cannot reach one upstream is not evidence it
    /// cannot reach the next, and a first run that quietly stopped fetching
    /// at the first failure would leave capabilities off with no path back
    /// short of a reinstall.
    #[test]
    fn a_failed_optional_stage_never_skips_the_ones_behind_it() {
        for failing in [
            "tesseract.scripts.apply_first_run_setup",
            "tesseract.scripts.provision_hardware",
            "tesseract.scripts.fetch_whisper_model",
            "tesseract.scripts.fetch_kokoro_voice",
            "tesseract.scripts.fetch_piper_voice",
            "tesseract.scripts.fetch_reranker_model",
            "tesseract.scripts.ensure_ollama",
        ] {
            let home = TempDir::new("optional-stage-fails");
            let uv = PathBuf::from("uv-stub");
            let seen: RefCell<Vec<String>> = RefCell::new(Vec::new());
            let run_uv = |_: &Path, _: &[&str]| -> Result<(), String> { Ok(()) };
            let progress = |_: &str| {};

            let py = provision_stages(
                home.path(),
                &uv,
                &progress,
                &run_uv,
                &failing_module_run(failing, &seen),
            )
            .unwrap_or_else(|e| panic!("a failed {failing} must not be fatal: {e}"));

            assert_eq!(py, venv_python(home.path()));
            let seen = seen.borrow();
            assert!(
                seen.iter().any(|m| m == failing),
                "{failing} was never attempted; got {seen:?}"
            );
            assert_eq!(
                seen.last().map(String::as_str),
                Some("tesseract.scripts.ensure_ollama"),
                "a failure at {failing} must not stop the stages behind it; got {seen:?}"
            );
            assert!(
                marker_path(home.path()).exists(),
                "the marker must still be written despite the {failing} failure"
            );
        }
    }

    // -- reinstall_deps_with ---------------------------------------------

    #[test]
    fn reinstall_deps_with_builds_the_expected_pip_install_argv() {
        let home = TempDir::new("reinstall-argv");
        let uv = PathBuf::from("uv-stub");
        let calls: RefCell<Recorded> = RefCell::new(Vec::new());

        let run_uv = |program: &Path, args: &[&str]| -> Result<(), String> {
            record(&calls, program, args);
            Ok(())
        };

        reinstall_deps_with(&uv, home.path(), &run_uv).expect("reinstall should succeed");

        let expected_pkg = home.join("app").join("tesseract");
        let expected_py = venv_python(home.path());
        assert_eq!(
            *calls.borrow(),
            vec![(
                uv,
                vec![
                    "pip".to_string(),
                    "install".to_string(),
                    "-e".to_string(),
                    expected_pkg.to_string_lossy().into_owned(),
                    "--python".to_string(),
                    expected_py.to_string_lossy().into_owned(),
                ]
            )]
        );
    }
}
