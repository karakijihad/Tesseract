use std::collections::VecDeque;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::{mpsc, Mutex, OnceLock};
use serde::Serialize;
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

/// Whether this install finished provisioning without anyone answering the
/// setup form.
///
/// The marker is written on BOTH exits from `provision_extras`, deliberately:
/// an unanswered run has installed the app, and without a marker every later
/// launch would re-clone and rebuild it. That left one file carrying two
/// facts — "do not reinstall" and "do not ask" — and only the first of them
/// was ever true. The `scope` field separates them, so a launch can skip the
/// install and still offer the form.
///
/// A marker without the field is an install provisioned before this shipped:
/// answered, because treating the whole existing population as unanswered
/// would put a setup form in front of every one of them on their next launch.
/// The genuinely-deferred ones among those keep today's behaviour — the HUD
/// advice pointing at Settings — rather than gaining a form.
///
/// Says nothing about whether the app is installed; `is_provisioned` answers
/// that, and the launch path asks both.
pub fn setup_unanswered(home: &Path) -> bool {
    let Ok(text) = std::fs::read_to_string(marker_path(home)) else {
        return false;
    };
    match serde_json::from_str::<serde_json::Value>(&text) {
        Ok(v) => {
            v.get("scope").and_then(|s| s.as_str()) == Some(ProvisionScope::Unanswered.as_str())
        }
        Err(_) => false,
    }
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
/// ONE entry, and it used to be six. `launch_refresh` runs the same work in a
/// single interpreter, in a deliberate order, and that order is the fix:
///
/// - The six were spawned CONCURRENTLY and shared no result, so each fetcher
///   re-derived which lane it wanted, from config, in its own process — six
///   imports of the runtime to answer one question six ways.
/// - `provision_hardware` WRITES the speech model into `providers.yaml` while
///   the fetchers READ it to decide what to download. Racing them meant a
///   first run could fetch the model it was in the middle of replacing. The
///   comment that used to sit here described that race and could only promise
///   it "settles on the next launch".
///
/// What did NOT change: this is still spawned and never waited on, so launch
/// pays no latency for it; the fetchers still run concurrently with each
/// other inside the pass; and Ollama is still handled without the vendor
/// installer, so a machine where that install was declined does not re-fetch
/// hundreds of megabytes on every start.
const LAUNCH_REFRESH_ASSETS: [&[&str]; 1] = [&["-m", "tesseract.scripts.launch_refresh"]];

/// Fire-and-forget retry of every `LAUNCH_REFRESH_ASSETS` entry, on EVERY
/// launch (not gated by `is_provisioned`).
///
/// Deliberately uses `spawn()`, not `output()`: this must never add
/// perceptible latency to launch, so it is never waited on — the caller
/// starts the supervisor immediately afterward regardless of whether these
/// subprocesses have finished, or even whether they could be spawned at all.
pub fn refresh_optional_assets(root: &Path) {
    // A quit that latched while this was being reached must not start six new
    // downloads on the way out.
    if stopping() {
        return;
    }
    for args in LAUNCH_REFRESH_ASSETS {
        let mut cmd = std::process::Command::new(venv_python(root));
        cmd.args(args);
        point_at_state_root(&mut cmd, root);
        hide_console(&mut cmd);
        // Kept, not dropped: the `Child` is what holds the process handle, and
        // the handle is what makes stopping it on quit safe. Still never
        // waited on — this must add no latency to launch.
        if let Ok(child) = cmd.spawn() {
            // The registry covers a clean quit; the job covers the crash that
            // never reaches one. This fetcher can be pulling ~2.2 GB.
            crate::job::adopt(child.id());
            if let Ok(mut children) = REFRESH_CHILDREN.lock() {
                children.push(child);
            }
        }
    }
}

/// The result of a failed `provision()` call: an operator-facing, already
/// credential-scrubbed message. Every stage — clone, python install, venv,
/// deps, playwright, marker write — produces the same shape, because no
/// failure here is recoverable from inside the shell.
#[derive(Debug)]
pub struct ProvisionError(pub String);

impl std::fmt::Display for ProvisionError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

/// Every non-clone provisioning stage already produces a bare `String`
/// (`run_uv`/`run_python`/plain filesystem errors); this lets every existing
/// `?` call site in `provision()` keep compiling unchanged while only the
/// clone stage constructs `ProvisionError` explicitly.
impl From<String> for ProvisionError {
    fn from(s: String) -> Self {
        ProvisionError(s)
    }
}

/// Emits the progress event the splash screen listens for AND appends the
/// same line to the shell's durable log, so every stage a user sees on
/// screen also lands in `<install root>/runtime/logs/shell.log` for later
/// diagnosis. Not under `TESSERACT_HOME` — that is the `home/` sibling, and
/// naming it here sent debugging to a path nothing ever writes.
fn emit_progress(app: &AppHandle, msg: &str) {
    crate::shell_log::log(msg);
    let _ = app.emit("provision-progress", msg.to_string());
}

/// Whether anyone answered the setup form this run.
///
/// The form is where consent is given, so a run that never had one may
/// install the app and nothing else. The alternative — provisioning the
/// shipped defaults because the window failed to open — is several gigabytes
/// arriving on a stranger's machine because a window failed to open, and the
/// consent ledger exists precisely so that cannot happen.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProvisionScope {
    /// The form was submitted. Its answers are staged and govern everything.
    Answered,
    /// The splash could not open, so nothing was asked. Optional artifacts
    /// wait for an answer given inside the app.
    Unanswered,
}

impl ProvisionScope {
    /// The word the completion marker carries, and the one `setup_unanswered`
    /// reads back. Written out rather than derived from `Debug`, which is a
    /// developer convenience no on-disk format should depend on.
    fn as_str(self) -> &'static str {
        match self {
            ProvisionScope::Answered => "answered",
            ProvisionScope::Unanswered => "unanswered",
        }
    }
}

/// How many stages a first run walks through: the clone, then every
/// `progress(...)` call in `base_stages` and `extras_stages` except the
/// terminal "Ready.".
///
/// Guarded by a unit test rather than trusted, because a stage added without
/// touching this constant would silently make every "step N of TOTAL" wrong
/// for the rest of the run.
pub const TOTAL_STAGES: u32 = 11;

/// The stages that run before anyone is asked anything: the clone, Python,
/// the venv and the dependency install.
///
/// They are shown as progress and never as a question — declining Python is
/// not a lighter install, it is a broken one. They also have to come first
/// for the form to be answerable at all: `setup_manifest` needs an
/// interpreter and a config tree to read this machine's real figures from,
/// and before this reorder there was neither, which is why the page carried
/// literals and quoted the wrong speech model to every machine.
pub const BASE_STAGES: u32 = 4;

/// The five stages an unanswered run skips: speech recognition, the voice
/// model, the browser engine, the reranker, and embeddings — which is the
/// Ollama vendor installer. Everything before them builds the app itself and
/// asks nobody for anything.
///
/// The browser engine joined this list when it stopped being required. It is
/// ~700 MB, nothing at boot depends on it, and `os_open_url` hands a link to
/// the machine's own browser without it — so it is an optional capability
/// like the rest, and it moved BELOW "Applying your setup…" for the reason
/// every stage down here is below it: a decline has to reach the stage that
/// would otherwise download it.
///
/// **Not subtracted from the denominator.** It was, and that was the defect:
/// the base phase runs before anyone has been asked, so it can only count out
/// of the full eleven — and an extras phase counting out of six then made the
/// total SHRINK from 11 to 6 partway through the one run that is already
/// degraded. An unanswered run ends at "Ready." on step 6 of 11, which is
/// true: it skipped five stages, and the headline says it is finished.
pub const DEFERRED_STAGES: u32 = 5;

/// The marker that tells the Python side nobody was asked.
///
/// Written before the stages run, and read wherever consent is derived: with
/// it present, `enabled: true` in the shipped catalog is a default rather than
/// an answer, so the launch pass does not fetch on the next start what this
/// run just declined to fetch. Recording a real answer — a Settings toggle —
/// is what ends it.
fn setup_deferred_path(root: &Path) -> PathBuf {
    runtime_dir(root).join("setup-deferred.json")
}

fn write_setup_deferred(root: &Path, reason: &str) {
    let runtime = runtime_dir(root);
    if let Err(e) = std::fs::create_dir_all(&runtime) {
        crate::shell_log::log_error(&format!("could not create {}: {e}", runtime.display()));
        return;
    }
    let body = serde_json::json!({ "reason": reason });
    let path = setup_deferred_path(root);
    // Written, then READ BACK. This run skips the fetch stages whatever
    // happens here, so a failed write costs nothing today — it costs the NEXT
    // launch, where the marker's absence is indistinguishable from an
    // ordinary install and the launch pass fetches the shipped defaults. That
    // is the whole hole, one start later, and a write that silently produced
    // nothing would reopen it while the log said the opposite.
    let written = std::fs::write(&path, body.to_string()).and_then(|()| {
        // Not `.exists()`: a truncated or unreadable file satisfies that and
        // fails the read the Python side will do.
        std::fs::read_to_string(&path).map(|_| ())
    });
    match written {
        Ok(()) => crate::shell_log::log(
            "setup was not answered — optional downloads wait for an answer in the app",
        ),
        // Never fatal — failing the install over a marker would trade a
        // repairable state for no app at all — but said at the level that
        // matches the consequence, and naming it, because the operator is the
        // only one who can act on it.
        Err(e) => crate::shell_log::log_error(&format!(
            "COULD NOT RECORD that setup was skipped ({e}). This run downloads nothing \
             optional, but the next launch will treat the shipped defaults as answers \
             and may fetch several gigabytes. Create {} by hand, or answer setup in \
             Settings before restarting.",
            path.display()
        )),
    }
}

/// Removes the deferral marker once the answers it stood in for have landed.
///
/// The marker's whole meaning is "nobody was asked, so the shipped catalog's
/// `enabled: true` is a default rather than an answer". A form that has now
/// been answered — on a first run, or on the launch that re-offered it —
/// makes that a lie, and a lie in this direction outranks the answer just
/// given: `consent.py` would keep reading real choices as absent ones.
///
/// Only a submitted form clears it. A Settings toggle still outranks it per
/// dependency without clearing it, because answering one question is not the
/// same as having been asked all of them.
///
/// Never fatal: a marker that cannot be removed costs the operator advice
/// they have already acted on, which is worse than tidy and better than a
/// failed provision.
fn clear_setup_deferred(root: &Path) {
    let path = setup_deferred_path(root);
    match std::fs::remove_file(&path) {
        Ok(()) => crate::shell_log::log("setup was answered — the deferral marker is cleared"),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => crate::shell_log::log_error(&format!(
            "could not clear {} ({e}) — the answers just given are on record, but this \
             install will go on reporting that its setup was never opened",
            path.display()
        )),
    }
}

/// The machine-readable line `pinned_fetch` writes to STDOUT while it streams
/// a model file. Human-facing log lines go to stderr, so the two channels
/// cannot interleave into each other: everything on stdout matching this
/// prefix is byte progress and is never shown verbatim or logged, and
/// everything else is text for the operator.
const PROGRESS_MARKER: &str = "TESSERACT_PROGRESS ";

/// The prefix `scripts/setup_manifest.py` puts on its one line of JSON.
///
/// Marked for the same reason the byte counters above are: stdout is a shared
/// channel, and anything a transitively imported library prints on it would
/// otherwise be read as part of the manifest.
const MANIFEST_MARKER: &str = "TESSERACT_SETUP_MANIFEST ";

/// One update for the splash: which stage is running, how far in, and either
/// the latest line of output or the byte counts of the file being fetched.
///
/// Kept separate from `provision-progress`, which stays a bare `String`: the
/// fatal path and the already-provisioned window both render that payload
/// directly, and widening it would have meant changing three consumers to add
/// a detail line to one.
#[derive(Clone, Serialize)]
pub struct ProvisionDetail {
    pub stage: String,
    pub index: u32,
    pub total: u32,
    /// Empty for a pure byte update, so the splash keeps the previous line
    /// on screen instead of blanking it between counter ticks.
    pub line: String,
    pub received_bytes: Option<u64>,
    /// `None` whenever the source did not say — libgit2 reports no byte
    /// total at all, and not every HTTP response carries a Content-Length.
    /// The splash shows a rising figure rather than inventing a percentage.
    pub expected_bytes: Option<u64>,
}

/// Reads `TESSERACT_PROGRESS file=<name> received=<n> expected=<n|->`.
/// Returns None for anything else, which is how a normal line is told apart
/// from a counter tick.
fn parse_progress_marker(line: &str) -> Option<(String, u64, Option<u64>)> {
    let rest = line.strip_prefix(PROGRESS_MARKER)?;
    let (mut file, mut received, mut expected) = (None, None, None);
    for field in rest.split_whitespace() {
        match field.split_once('=') {
            Some(("file", v)) => file = Some(v.to_string()),
            Some(("received", v)) => received = v.parse::<u64>().ok(),
            Some(("expected", v)) => expected = v.parse::<u64>().ok(),
            _ => {}
        }
    }
    Some((file?, received?, expected))
}

/// Byte thresholds below which an update is dropped.
///
/// libgit2 calls its transfer callback per packet, which would emit thousands
/// of events for one clone; the fetch scripts already throttle their own
/// markers, so this only bounds the in-process source.
const CLONE_EMIT_EVERY_BYTES: u64 = 4 * 1024 * 1024;

#[derive(Default)]
struct StageState {
    index: u32,
    name: String,
    received: Option<u64>,
    expected: Option<u64>,
    last_emit: u64,
}

/// Tracks which stage is running so a line of subprocess output can be
/// attributed to it.
///
/// Deliberately lives here and not in the stage functions: those take a
/// `&dyn Fn(&str)` progress sink and their unit tests assert on the exact
/// strings emitted. Counting stages on this side keeps the staged sequence,
/// its argv and its ordering tests untouched.
struct StageTracker {
    state: Mutex<StageState>,
    /// The denominator the splash renders. `TOTAL_STAGES` for every run, in
    /// both phases: the base half runs before the scope is known, so any
    /// scope-dependent total could only ever apply to the second half — and a
    /// denominator that changes mid-run is a bar walking backwards.
    total: u32,
}

impl StageTracker {
    /// `done` is how many stages have already been counted by an earlier
    /// phase. The run is split by a form in the middle of it, so the second
    /// half has to resume the counter rather than restart it — a tracker that
    /// began again at 1 would show "step 1 of 11" after four stages had
    /// visibly completed.
    fn starting_at(done: u32) -> Self {
        Self {
            state: Mutex::new(StageState {
                index: done,
                ..StageState::default()
            }),
            total: TOTAL_STAGES,
        }
    }

    /// A new stage started: bump the counter and forget the previous stage's
    /// byte counts, so a stage that reports none does not inherit them.
    fn begin(&self, name: &str) {
        if let Ok(mut s) = self.state.lock() {
            s.index = (s.index + 1).min(self.total);
            s.name = name.to_string();
            s.received = None;
            s.expected = None;
            s.last_emit = 0;
        }
    }

    /// One line of subprocess output. A counter marker never reaches
    /// `shell.log` — a 1.6 GB download would be thousands of rows of it — and
    /// what the splash sees of it is the file name, as the tail's line, so
    /// the bytes on the meter say what they are about. Anything else is
    /// operator-facing text that lands in both.
    ///
    /// The marker repeats that name every 4 MB or every second, and a stalled
    /// transfer deliberately keeps re-reporting, so whatever renders the line
    /// owes it a same-as-last check; the splash tail does one.
    fn line(&self, app: &AppHandle, line: &str) {
        let Ok(mut s) = self.state.lock() else {
            return;
        };
        match parse_progress_marker(line) {
            Some((file, received, expected)) => {
                s.received = Some(received);
                s.expected = expected;
                let detail = Self::detail(&s, &file, self.total);
                drop(s);
                let _ = app.emit("provision-detail", detail);
            }
            None => {
                crate::shell_log::log(line);
                let detail = Self::detail(&s, line, self.total);
                drop(s);
                let _ = app.emit("provision-detail", detail);
            }
        }
    }

    /// libgit2's transfer statistics for the clone stage, throttled.
    fn transfer(&self, app: &AppHandle, received: u64, objects: u64, total_objects: u64) {
        let Ok(mut s) = self.state.lock() else {
            return;
        };
        let done = total_objects > 0 && objects >= total_objects;
        if received < s.last_emit.saturating_add(CLONE_EMIT_EVERY_BYTES) && !done {
            return;
        }
        s.last_emit = received;
        s.received = Some(received);
        s.expected = None;
        let line = if total_objects > 0 {
            format!("{objects} of {total_objects} objects")
        } else {
            String::new()
        };
        let detail = Self::detail(&s, &line, self.total);
        drop(s);
        let _ = app.emit("provision-detail", detail);
    }

    fn detail(s: &StageState, line: &str, total: u32) -> ProvisionDetail {
        ProvisionDetail {
            stage: s.name.clone(),
            index: s.index,
            total,
            line: line.to_string(),
            received_bytes: s.received,
            expected_bytes: s.expected,
        }
    }
}

/// Install the app itself: clone the source repo into the per-user home,
/// download Python, build the venv and editable-install tesseract. Returns
/// the venv python path. Emits "provision-progress" events (payload: a String
/// line). Every failure is terminal for the run: the clone is anonymous, so
/// there is no credential the caller could supply to retry with.
///
/// **Nothing here is optional and nothing here is asked about.** The form
/// runs after this, on a tree that exists — which is what lets it quote this
/// machine's real figures instead of the literals it used to carry.
pub fn provision_base(app: &AppHandle, home: &Path) -> Result<PathBuf, ProvisionError> {
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

    // `progress` names the stage and `on_line` attributes every subsequent
    // line of output to it, so a stall shows which step it stalled in rather
    // than the last headline that happened to fire.
    let tracker = StageTracker::starting_at(0);
    let progress = |msg: &str| {
        tracker.begin(msg);
        emit_progress(app, msg);
    };
    let on_line = |line: &str| tracker.line(app, line);

    progress("Downloading TESSERACT…");
    clone_with_progress(app, &tracker, &app_dir(home))?;

    base_stages(home, &uv, &progress, &|program, args| {
        run_uv(program, args, &on_line)
    })
    .map_err(ProvisionError::from)
}

/// Apply the operator's answers and fetch what they agreed to. Writes the
/// completion marker and returns the venv python path.
///
/// Split from `provision_base` by the form, and that split is the whole
/// reorder: everything here reads a config the answers have just written, so
/// declining a lane costs nothing, and nothing here runs on a machine where
/// nobody was asked.
pub fn provision_extras(
    app: &AppHandle,
    home: &Path,
    scope: ProvisionScope,
) -> Result<PathBuf, ProvisionError> {
    // Before any stage runs, so a provision that dies half way still leaves
    // the record that nobody was asked. The alternative ordering — writing it
    // at the end — would let a crashed unanswered run look answered to the
    // launch pass, which is the one reader that must never get this wrong.
    if scope == ProvisionScope::Unanswered {
        write_setup_deferred(home, "the setup form was never answered");
    }

    let tracker = StageTracker::starting_at(BASE_STAGES);
    let progress = |msg: &str| {
        tracker.begin(msg);
        emit_progress(app, msg);
    };
    let on_line = |line: &str| tracker.line(app, line);

    extras_stages(home, scope, &progress, &|program, args| {
        run_python(program, args, home, &on_line)
    })
    .map_err(ProvisionError::from)
}

/// The form's questions, answered for THIS machine, as the JSON the splash
/// renders from.
///
/// Buffered rather than streamed, unlike every other Python call here: this
/// one is read by a program, not by a person, and `run_tool`'s line splitter
/// truncates at 4 KB — a manifest is comfortably longer than that. It is also
/// short enough that there is nothing to show progress for.
/// Takes no `AppHandle`: `point_at_state_root` reads the `uv` path from
/// `UV_PATH`, which `provision_base` has already populated by the time this
/// can run at all.
pub fn setup_manifest(home: &Path) -> Result<serde_json::Value, String> {
    let mut cmd = manifest_command(home);

    // Spawned rather than `output()`, and the difference is the quit path.
    // `output()` gives no pid to adopt, so this was the one child in this file
    // outside the Job Object and unknown to `ACTIVE_PID` — a crash or an
    // end-task orphaned it, and a clean quit could not signal it. Everything
    // else here goes through `run_tool`, which does exactly this.
    if stopping() {
        return Err("the setup manifest was not started: TESSERACT is shutting down".into());
    }
    let mut child = cmd
        .spawn()
        .map_err(|e| format!("the setup manifest could not be built: {e}"))?;
    crate::job::adopt(child.id());
    set_active(Some(child.id()));
    if stopping() {
        stop_active();
        let _ = child.wait();
        return Err("the setup manifest stopped: TESSERACT is shutting down".into());
    }
    let out = child
        .wait_with_output()
        .map_err(|e| format!("the setup manifest did not complete: {e}"));
    set_active(None);
    let out = out?;

    parse_manifest(
        &out.stdout,
        &out.stderr,
        out.status.success(),
        out.status.code(),
    )
}

/// The command, built and not yet spawned — split out so a test can assert
/// the interpreter, the module and the environment without needing a venv.
fn manifest_command(home: &Path) -> std::process::Command {
    let mut cmd = std::process::Command::new(venv_python(home));
    cmd.args(["-m", "tesseract.scripts.setup_manifest"])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    point_at_state_root(&mut cmd, home);
    hide_console(&mut cmd);
    cmd
}

/// The manifest, or why it is not one — split from the spawn so it can be
/// tested without a venv.
///
/// stderr is the script's diagnostic channel and is logged whatever the exit
/// code: a manifest that succeeded while warning that the graphics probe
/// failed is the case where the warning is the whole story.
fn parse_manifest(
    stdout: &[u8],
    stderr: &[u8],
    success: bool,
    code: Option<i32>,
) -> Result<serde_json::Value, String> {
    for line in String::from_utf8_lossy(stderr).lines() {
        let line = line.trim();
        if !line.is_empty() {
            crate::shell_log::log(&scrub_credentials(line));
        }
    }
    if !success {
        return Err(format!(
            "the setup manifest exited {}",
            code.unwrap_or(-1)
        ));
    }
    // The MARKED line, not the whole stream. Anything printed to stdout by a
    // library the script imports — a vendor banner, a deprecation notice —
    // would otherwise prefix the JSON and cost the operator the entire form,
    // on a machine that is working perfectly. Same shape as the byte-progress
    // marker the fetch stages already use.
    let text = String::from_utf8_lossy(stdout);
    let body = text
        .lines()
        .rev()
        .find_map(|line| line.trim().strip_prefix(MANIFEST_MARKER))
        .ok_or_else(|| {
            format!("the setup manifest printed no {MANIFEST_MARKER} line")
        })?;
    serde_json::from_str(body)
        .map_err(|e| format!("the setup manifest was not readable JSON: {e}"))
}

/// Clones on a worker thread so this one can pump transfer statistics.
///
/// libgit2's callback must be `'static` (it is owned by `FetchOptions`), which
/// rules out a closure borrowing the `AppHandle` and the tracker. Sending the
/// numbers over a channel and rendering them here keeps the borrow local: the
/// channel closes when the worker returns, which is what ends the loop.
fn clone_with_progress(
    app: &AppHandle,
    tracker: &StageTracker,
    app_dir: &Path,
) -> Result<(), ProvisionError> {
    let (tx, rx) = mpsc::channel::<(u64, u64, u64)>();
    let dir = app_dir.to_path_buf();
    let url = crate::repo::repo_url();
    let worker = std::thread::spawn(move || clone_app_dir_reporting(&dir, &url, Some(tx)));
    for (received, objects, total_objects) in rx {
        tracker.transfer(app, received, objects, total_objects);
    }
    worker
        .join()
        .map_err(|_| ProvisionError("the clone did not complete".to_string()))?
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

/// Every post-clone stage that installs the app, in order, with the progress
/// sink and the subprocess runner injected.
///
/// Split out from `provision_base` because that needs a live Tauri
/// `AppHandle` (for resource resolution and event emission) that no unit test
/// can construct — which left the entire first-run sequence, the stage
/// ordering, the argument construction and the marker write untested. Here
/// the same logic runs against recording stubs.
fn base_stages(
    home: &Path,
    uv: &Path,
    progress: &dyn Fn(&str),
    run_uv: &dyn Fn(&Path, &[&str]) -> Result<(), String>,
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

    Ok(py)
}

/// Every stage that acts on the operator's answers, in order.
///
/// Nothing above this line asked anyone anything and nothing below it runs
/// without an answer — which is what `scope` carries. Same injection as
/// `base_stages`, for the same reason.
fn extras_stages(
    home: &Path,
    scope: ProvisionScope,
    progress: &dyn Fn(&str),
    run_python: &dyn Fn(&Path, &[&str]) -> Result<(), String>,
) -> Result<PathBuf, String> {
    let py = venv_python(home);

    // The stage that writes the setup form's staged answers into
    // `home/config/`. Must come BEFORE the fetch stages, which read that
    // config to decide what to download — that ordering is the whole reason
    // declining a lane costs nothing. It seeds the config tree too, which is
    // idempotent and matters on the unanswered path: `setup_manifest` has
    // usually done it already, and a run whose form never opened has nobody
    // to have done it.
    //
    // Best-effort for the IDENTITY half: a setup that could not be applied
    // leaves the shipped defaults, which is a working install with a name the
    // operator did not choose. The Identity tab can set every one of these
    // afterwards, so failing the install over it would be a bad trade.
    //
    // Not best-effort for the CONSENT half, and that is the difference this
    // return value carries. This stage is what writes the operator's answers
    // into the config the fetch stages read, AND what records them in the
    // ledger. If it did not run, the config below is the shipped default and
    // the ledger is empty — so proceeding to fetch would download every
    // default lane on the strength of answers that never landed anywhere. An
    // answered run whose answers were not applied is, for the purposes of
    // what may be downloaded, an unanswered one.
    progress("Applying your setup…");
    let applied = run_python(&py, &["-m", "tesseract.scripts.apply_first_run_setup"]);
    // Named rather than shadowing the parameter: every read below this line
    // must be the downgraded value, and a shadow makes that invisible at the
    // point a future stage is inserted above it.
    let effective_scope = match applied {
        Ok(()) => scope,
        Err(e) => {
            crate::shell_log::log_error(&format!(
                "the setup answers could not be applied ({e}) — the config holds the \
                 shipped defaults and the ledger is empty, so nothing optional is \
                 downloaded on their strength"
            ));
            write_setup_deferred(home, "the setup answers could not be applied");
            ProvisionScope::Unanswered
        }
    };

    // Conditioned on the EFFECTIVE scope, never on `applied` alone. The
    // unanswered path runs this same stage — it seeds the config tree, which
    // is why it is above the scope check at all — and finds no staged answers
    // to apply, which is a success. Clearing on `Ok(())` would therefore
    // delete the marker `provision_extras` wrote four lines into this run.
    if effective_scope == ProvisionScope::Answered {
        clear_setup_deferred(home);
    }

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

    // Everything below this line downloads something optional, and the form
    // is where consent for it is given. A run that never had a form asks
    // nobody, so it fetches nothing: the marker written in `provision()` is
    // what stops the next launch quietly doing it instead, and Settings →
    // Local models is where the operator says yes at their own pace.
    //
    // The app itself is already installed by the stages above, which is the
    // trade this path exists to make — a working app with no models beats
    // several gigabytes nobody agreed to, and it also beats an install that
    // stops dead because a window would not open.
    if effective_scope == ProvisionScope::Unanswered {
        // Said in the log, because the counter cannot say it: the run finishes
        // at "Ready." on step 6 of 11, and the missing five are the whole
        // reason this install has no models. The denominator deliberately does
        // not shrink to match — a total that changes partway through a run is
        // a bar walking backwards.
        crate::shell_log::log(&format!(
            "nobody was asked, so {DEFERRED_STAGES} optional stages were skipped — \
             turn on what you want in Settings and it downloads then"
        ));
        write_marker(home, effective_scope)?;
        progress("Ready.");
        return Ok(py);
    }

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

    // The wake-word front end (~2.3 MB). Same best-effort contract: the script
    // fetches nothing unless the wake word is enabled in config, so leaving it
    // off costs zero bytes, and missing files leave the gate open rather than
    // deaf.
    let _ = run_python(&py, &["-m", "tesseract.scripts.fetch_wake_models"]);

    // Best-effort, same contract: fetches the pinned cross-encoder reranker
    // (~23 MB, sha256-pinned in the providers catalog) so retrieval ranks with
    // the reranker instead of pure RRF. Absent files are a supported degraded
    // mode, which is exactly why this must not be fatal — and also why it was
    // worth adding: without it every shipped install ran degraded in silence.
    // Below "Applying your setup…", and that position is the whole reason it
    // can be declined. It used to run ABOVE it, before any answer had been
    // written, so the operator's "no" reached a stage that had already
    // downloaded ~700 MB of Chromium. Best-effort like its neighbours: the
    // browser tools report the switch that is false, and `os_open_url` needs
    // none of this.
    progress("Downloading browser engine…");
    let _ = run_python(
        &py,
        &["-m", "tesseract.orchestrator.browser.provision"],
    );

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
    write_marker(home, effective_scope)?;

    progress("Ready.");
    Ok(py)
}

/// Writes the completion marker `is_provisioned` reads. Only ever called as
/// the final step of a fully successful provision.
///
/// `scope` is the one that actually governed the run, not the one the caller
/// hoped for: an answered run whose answers could not be applied is stamped
/// unanswered, because that is what its config and its ledger say. The stamp
/// is what a later launch reads to decide whether there is still a question
/// outstanding on this machine.
fn write_marker(home: &Path, scope: ProvisionScope) -> Result<(), String> {
    let runtime = runtime_dir(home);
    std::fs::create_dir_all(&runtime).map_err(|e| e.to_string())?;
    let body = serde_json::json!({ "deps_version": DEPS_VERSION, "scope": scope.as_str() });
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
    // The update path has no splash to emit to, so its lines go to the log
    // only — but they DO go there now. A dependency re-install that hung
    // during an update previously left nothing behind but the stage headline.
    reinstall_deps_with(&uv, home, &|program, args| {
        run_uv(program, args, &|line| crate::shell_log::log(line))
    })
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

/// The clone with the URL passed explicitly and no progress reporting, so
/// tests drive the real guard/clear/clone/error-mapping logic against a local
/// throwaway repo without mutating the process-global `TESSERACT_REPO_URL`
/// env var (which `repo::tests` also exercises and would race with under
/// parallel `cargo test`).
#[cfg(test)]
fn clone_app_dir_with(app_dir: &Path, url: &str) -> Result<(), ProvisionError> {
    clone_app_dir_reporting(app_dir, url, None)
}

/// The URL resolution `clone_with_progress` performs, without the worker
/// thread and channel — so the env-var end-to-end test still covers the step
/// where `TESSERACT_REPO_URL` is read, which is the only part of the real path
/// a progress channel does not change.
#[cfg(test)]
fn clone_app_dir(app_dir: &Path) -> Result<(), ProvisionError> {
    clone_app_dir_reporting(app_dir, &crate::repo::repo_url(), None)
}

/// The .exe is a thin shell: the source tree and all third-party deps come
/// from the network on first run. A `.git` dir marks a complete clone; its
/// absence (missing entirely, or left behind by an interrupted first run)
/// means we clear whatever is there and re-clone, so a partial `app_dir` can
/// never wedge the app in an unrecoverable state. No-ops if already cloned.
///
/// `progress` is `Some` only on the real first-run path; every recovery and
/// adoption branch below returns without transferring anything, so a `None`
/// here means "nothing to report", never "reporting suppressed".
fn clone_app_dir_reporting(
    app_dir: &Path,
    url: &str,
    progress: Option<mpsc::Sender<(u64, u64, u64)>>,
) -> Result<(), ProvisionError> {
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
    let cloned = match progress {
        Some(tx) => crate::repo::clone_reporting(url, &staging, move |received, objects, total| {
            // A closed receiver just means nobody is rendering any more —
            // never a reason to interrupt a transfer that is working.
            let _ = tx.send((received, objects, total));
        }),
        None => crate::repo::clone(url, &staging),
    };
    if let Err(e) = cloned {
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
    // Turns on `pinned_fetch`'s byte-progress markers. Set here rather than at
    // the fetch call sites so the first-run and launch-refresh paths cannot
    // disagree about it; on the refresh path nothing reads stdout and the
    // markers go nowhere, which costs a formatted string per 4 MB.
    cmd.env("TESSERACT_PROVISION_PROGRESS", "1");
}

/// How many output lines are kept for a failure message.
///
/// The buffered runner this replaced put the WHOLE of stderr into the error,
/// which was bounded only by how much the tool chose to say. A resolver
/// failure can run to hundreds of lines, and that string is rendered on a
/// 460px splash — so the tail is capped and the full text is in `shell.log`,
/// which now receives every line as it arrives rather than only on failure.
const TAIL_LINES: usize = 40;

/// A single line is flushed at this length even without a terminator, so a
/// tool that renders an unterminated progress bar cannot grow one "line"
/// without bound in memory.
const MAX_LINE_BYTES: usize = 4096;

/// The pid of the provisioning subprocess currently running, if any.
///
/// Provisioning shells out to `uv` and `python`, which Windows does not reap
/// when the parent exits — so quitting mid-download left them running against
/// a staging directory the next launch would try to adopt. Recording the pid
/// is what makes `stop_active` possible at all; the buffered runner never had
/// a handle to hold, because `output()` consumes the child.
static ACTIVE_PID: Mutex<Option<u32>> = Mutex::new(None);

/// The launch-refresh fetchers, held as `Child` values rather than pids.
///
/// `refresh_optional_assets` spawns six of these on EVERY launch and never
/// waits on them — that is deliberate, it must add no latency to startup — so
/// none of them pass through `run_tool` and nothing recorded them. Quitting
/// left them running, and one of them (`provision_hardware`) can be pulling
/// ~2.2 GB of CUDA wheels.
///
/// Holding the `Child` and not the pid is the load-bearing detail: a `Child`
/// keeps the Windows process handle open, and Windows will not recycle a pid
/// while a handle to it exists. The previous code dropped the `Child` at the
/// end of the spawn loop, so a stored pid could have been reused by an
/// unrelated process by the time quit read it — the recycling hazard that is
/// unreachable in `run_tool` precisely because it holds its `Child`.
static REFRESH_CHILDREN: Mutex<Vec<std::process::Child>> = Mutex::new(Vec::new());

/// Latched by `stop_active()` and never cleared: the process is going away.
///
/// Killing the running child is not enough on its own. Every stage after the
/// dependency install is best-effort (`let _ = run_python(...)`), so a killed
/// stage's error is swallowed and the provisioning thread proceeds straight to
/// the next `run_tool` — which would spawn a fresh child that nothing is left
/// to signal, since `RunEvent::Exit` has already run its own `stop_active()`.
/// That is the abandoned-child case reappearing one stage later.
static STOPPING: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

pub(crate) fn stopping() -> bool {
    STOPPING.load(std::sync::atomic::Ordering::SeqCst)
}

fn set_active(pid: Option<u32>) {
    if let Ok(mut guard) = ACTIVE_PID.lock() {
        *guard = pid;
    }
}

/// Stops the provisioning subprocess tree, if one is running.
///
/// Called from the quit path. `taskkill /T` walks the tree because `uv` itself
/// spawns children (its own resolver and, for the Python stages, whatever the
/// script shelled out to), and killing only the named pid would leave those
/// behind — the exact failure this exists to prevent, one level down.
///
/// Returns whether anything was signalled, so the caller can log the
/// difference between "stopped a download" and "nothing was running".
pub fn stop_active() -> bool {
    // Latch FIRST, so a stage that is between subprocesses right now finds the
    // flag already set when it tries to start the next one. Setting it after
    // the kill would leave exactly the gap this closes.
    STOPPING.store(true, std::sync::atomic::Ordering::SeqCst);
    // Both, and the provisioning child first: a first run has one of these and
    // an already-provisioned launch has the other, but `RunEvent::Exit` cannot
    // know which and must cover both.
    let stopped_stage = stop_provisioning_stage();
    let stopped_refresh = stop_refresh_children();
    stopped_stage || stopped_refresh
}

fn stop_provisioning_stage() -> bool {
    let Ok(mut guard) = ACTIVE_PID.lock() else {
        return false;
    };
    let Some(pid) = guard.take() else {
        return false;
    };
    kill_tree(pid);
    crate::shell_log::log(&format!("provisioning subprocess {pid} stopped"));
    true
}

/// Stops whatever the launch refresh still has running.
///
/// `try_wait` first: most launches find every fetcher already finished, and
/// `taskkill` on a pid whose process has exited is at best noise and at worst
/// a kill aimed at whatever inherited that number — which cannot happen while
/// the `Child` is held, but only because we check before releasing it.
fn stop_refresh_children() -> bool {
    let Ok(mut children) = REFRESH_CHILDREN.lock() else {
        return false;
    };
    let mut stopped = 0usize;
    for mut child in children.drain(..) {
        match child.try_wait() {
            Ok(Some(_)) => {}
            _ => {
                kill_tree(child.id());
                let _ = child.wait();
                stopped += 1;
            }
        }
    }
    if stopped > 0 {
        crate::shell_log::log(&format!(
            "stopped {stopped} launch-refresh fetcher(s) still running"
        ));
    }
    stopped > 0
}

/// Kills a process AND its descendants. `uv` spawns children of its own, so
/// killing only the named pid leaves the download running one level down.
fn kill_tree(pid: u32) {
    #[cfg(windows)]
    {
        let mut cmd = std::process::Command::new("taskkill");
        cmd.args(["/T", "/F", "/PID", &pid.to_string()]);
        hide_console(&mut cmd);
        let _ = cmd.status();
    }
    #[cfg(not(windows))]
    {
        let _ = std::process::Command::new("kill")
            .args(["-9", &pid.to_string()])
            .status();
    }
}

/// Splits a byte stream into lines on BOTH `\n` and `\r`, handing each one to
/// `emit` as it completes.
///
/// `\r` is treated as a terminator, not as text: measured against the shipped
/// `uv` and `playwright`, neither emits a bare `\r` through a pipe — but a
/// tool that renders a redrawing progress bar would otherwise arrive as one
/// unbounded line that never terminates until the process exits, which is the
/// failure mode this whole change exists to remove.
fn split_lines(mut stream: impl Read, is_stderr: bool, out: mpsc::Sender<(bool, String)>) {
    let mut buf = [0u8; 8192];
    let mut pending: Vec<u8> = Vec::new();
    let flush = |pending: &mut Vec<u8>| {
        if pending.is_empty() {
            return true;
        }
        let line = String::from_utf8_lossy(pending).trim().to_string();
        pending.clear();
        if line.is_empty() {
            return true;
        }
        out.send((is_stderr, line)).is_ok()
    };
    loop {
        match stream.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => {
                for &byte in &buf[..n] {
                    if byte == b'\n' || byte == b'\r' {
                        if !flush(&mut pending) {
                            return;
                        }
                    } else {
                        pending.push(byte);
                        if pending.len() >= MAX_LINE_BYTES && !flush(&mut pending) {
                            return;
                        }
                    }
                }
            }
            Err(_) => break,
        }
    }
    flush(&mut pending);
}

/// Runs a provisioning subprocess, forwarding every line it writes to
/// `on_line` WHILE IT RUNS, and mapping a non-zero exit into an error carrying
/// the tail of its stderr. `label` names the tool in both error shapes so
/// `run_uv`/`run_python` stay one line each rather than two copies of this.
/// `root` is `Some` only for the Python stages, which resolve state paths.
///
/// Both streams are read, and that is not defensive: measured through a pipe,
/// `uv` writes every informational line — including "Downloading x (1.2MiB)" —
/// to **stderr** and nothing at all to stdout, while `playwright install`
/// writes its ~49 progress lines to **stdout**. Watching either one alone
/// would leave one of the two longest stages silent.
///
/// Each line is scrubbed before it leaves this function: with the output now
/// forwarded continuously rather than only on failure, a credentialed index or
/// proxy URL echoed by a tool would otherwise reach the screen on the SUCCESS
/// path too.
fn run_tool(
    program: &Path,
    args: &[&str],
    label: &str,
    root: Option<&Path>,
    on_line: &dyn Fn(&str),
) -> Result<(), String> {
    let mut cmd = std::process::Command::new(program);
    cmd.args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if let Some(root) = root {
        point_at_state_root(&mut cmd, root);
    }
    hide_console(&mut cmd);
    if stopping() {
        return Err(format!("{label} not started: TESSERACT is shutting down"));
    }
    let mut child = cmd
        .spawn()
        .map_err(|e| format!("{label} spawn failed: {e}"))?;
    // Before `set_active`, so the backstop is in place even if this thread
    // never reaches another line. `uv` spawns children of its own and they
    // inherit the job, which is the same reach `kill_tree` gets by walking —
    // without needing anything of ours to still be running.
    crate::job::adopt(child.id());
    set_active(Some(child.id()));
    // Re-checked after recording, not only before: a quit that latched the
    // flag between the check above and this line would already have taken its
    // one look at `ACTIVE_PID` and found it empty, so nothing else would ever
    // kill this child.
    if stopping() {
        stop_active();
        let _ = child.wait();
        return Err(format!("{label} stopped: TESSERACT is shutting down"));
    }

    let (tx, rx) = mpsc::channel::<(bool, String)>();
    let readers: Vec<_> = [
        child.stdout.take().map(|s| {
            let tx = tx.clone();
            std::thread::spawn(move || split_lines(s, false, tx))
        }),
        child.stderr.take().map(|s| {
            let tx = tx.clone();
            std::thread::spawn(move || split_lines(s, true, tx))
        }),
    ]
    .into_iter()
    .flatten()
    .collect();
    // The last live sender, or the loop below would never see the channel
    // close and would block forever after the child had exited.
    drop(tx);

    let mut tail: VecDeque<String> = VecDeque::with_capacity(TAIL_LINES);
    for (_is_stderr, line) in rx {
        let line = scrub_credentials(&line);
        // BOTH streams, for the same reason both are read at all: the tail is
        // what the operator is shown when a stage fails, and `playwright`
        // writes to stdout. Keeping it to stderr — which is what the buffered
        // runner did, since it only ever had `out.stderr` — would have made a
        // playwright failure surface an empty "Setup failed:" while the real
        // text sat in `shell.log`.
        if tail.len() == TAIL_LINES {
            tail.pop_front();
        }
        tail.push_back(line.clone());
        on_line(&line);
    }
    for reader in readers {
        let _ = reader.join();
    }

    let status = child.wait();
    set_active(None);
    let status = status.map_err(|e| format!("{label} did not complete: {e}"))?;
    if !status.success() {
        let detail: Vec<&str> = tail.iter().map(String::as_str).collect();
        return Err(format!(
            "{label} {:?} failed: {}",
            args,
            detail.join(" | ")
        ));
    }
    Ok(())
}

fn run_uv(uv: &Path, args: &[&str], on_line: &dyn Fn(&str)) -> Result<(), String> {
    run_tool(uv, args, "uv", None, on_line)
}

fn run_python(py: &Path, args: &[&str], root: &Path, on_line: &dyn Fn(&str)) -> Result<(), String> {
    run_tool(py, args, "python", Some(root), on_line)
}

/// Redacts any `user:token@` userinfo segment from URLs embedded in an error
/// string, so a git2 error that echoes back the remote URL never surfaces a
/// credential to logs or the splash screen. `pub(crate)` so Task 12's
/// `update.rs` can reuse it for `check_behind` errors, which can likewise
/// embed the remote URL, rather than duplicating the redaction logic.
pub(crate) fn scrub_credentials(s: &str) -> String {
    // Query strings go FIRST. The userinfo pass below writes a `<redacted>`
    // marker into the URL, and `<` is one of the characters the query pass
    // treats as the end of a URL — so running it second would make it stop at
    // the marker and walk straight past the query it was meant to remove.
    let queryless = scrub_query_strings(s);
    let mut out = String::new();
    let mut rest = queryless.as_str();
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

/// Userinfo is not the only place a secret rides in a URL. GitHub answers an
/// asset request with a redirect to a pre-signed URL whose authorisation IS
/// its query string, and a transport error can carry that whole URL into the
/// log and onto the screen. A URL therefore loses everything from the first
/// `?` or `#`, whichever comes first — the fragment is included because it is
/// the other half of the same class and costs one character to cover.
///
/// The whole query goes rather than named-parameter matching: the parameter
/// names differ per signing scheme, and a redaction list that has to be kept
/// in step with someone else's URL format is one that eventually misses.
/// Nothing downstream parses these strings — they exist to be read by a
/// person — so losing the query costs nothing an operator needed.
fn scrub_query_strings(s: &str) -> String {
    let mut out = String::new();
    let mut rest = s;
    while let Some(scheme_pos) = rest.find("://") {
        let after_scheme = scheme_pos + 3;
        let tail = &rest[after_scheme..];
        // A URL ends at the first character that cannot appear in one; that
        // boundary is what keeps surrounding prose intact.
        let url_end = tail
            .find(|c: char| c.is_whitespace() || matches!(c, '"' | '\'' | '<' | '>' | ')' | ','))
            .unwrap_or(tail.len());
        match tail[..url_end].find(['?', '#']) {
            Some(q) => {
                out.push_str(&rest[..after_scheme + q]);
                out.push_str("?<redacted>");
                rest = &tail[url_end..];
            }
            None => {
                out.push_str(&rest[..after_scheme + url_end]);
                rest = &tail[url_end..];
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
/// into an actionable, credential-free `ProvisionError`.
///
/// The repository is public, so an auth/not-found shaped failure is no longer
/// something the user can fix by supplying anything: it means the repository
/// moved, was renamed, or is temporarily unreachable. It gets its own message
/// rather than being folded into the generic one, because "check your
/// connection" is misleading advice for a 404.
fn classify_clone_error(raw: &str) -> ProvisionError {
    let scrubbed = scrub_credentials(raw);
    let lower = scrubbed.to_lowercase();
    if is_auth_failure(&lower) {
        return ProvisionError(
            "could not reach the TESSERACT repository — it may have moved or be temporarily \
             unavailable. Try again shortly; if it persists, a newer installer is needed."
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
    ProvisionError(msg)
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

        clone_app_dir_with(&app_dir, &origin.to_string_lossy())
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

        clone_app_dir_with(&app_dir, &origin.to_string_lossy())
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
        crate::repo::clone(&origin.to_string_lossy(), &staging).expect("seed staging");
        assert!(staging.join(".git").exists());

        clone_app_dir_with(&app_dir, "https://127.0.0.1:1/unreachable.git")
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
        crate::repo::clone(&origin.to_string_lossy(), &good).expect("seed");
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

        let err = clone_app_dir_with(&app_dir, &bogus_origin.to_string_lossy())
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
        clone_app_dir_with(&app_dir, &origin.to_string_lossy())
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

        let err = clone_app_dir_with(&app_dir, "https://example.invalid/unused.git")
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

        let err = clone_app_dir_with(&app_dir, "https://example.invalid/unused.git")
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

        clone_app_dir_with(&app_dir, &origin.to_string_lossy()).expect("initial clone");
        // Mark the existing clone so a second call touching it would be observable.
        std::fs::write(app_dir.join("venv-marker.txt"), b"do-not-touch").unwrap();

        clone_app_dir_with(&app_dir, &origin.to_string_lossy())
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
    fn scrub_credentials_redacts_a_presigned_asset_url_query() {
        // The shape GitHub redirects an asset request to: the authorisation
        // IS the query string, and download_asset can carry that whole URL
        // into an error message.
        let raw = "download failed: https://objects.githubusercontent.com/repo/setup.exe\
                   ?X-Amz-Credential=AKIAEXAMPLE&X-Amz-Signature=deadbeefsecret got 403";
        let scrubbed = scrub_credentials(raw);
        assert!(!scrubbed.contains("deadbeefsecret"));
        assert!(!scrubbed.contains("AKIAEXAMPLE"));
        assert!(scrubbed.contains("?<redacted>"));
        assert!(
            scrubbed.contains("objects.githubusercontent.com/repo/setup.exe"),
            "the host and path must survive - they are what makes the error readable: {scrubbed}"
        );
        assert!(
            scrubbed.contains("got 403"),
            "prose after the URL must survive: {scrubbed}"
        );
    }

    #[test]
    fn scrub_credentials_redacts_a_url_fragment_as_well_as_a_query() {
        let raw = "download failed: https://host/path#access_token=fragmentsecret meanwhile";
        let scrubbed = scrub_credentials(raw);
        assert!(!scrubbed.contains("fragmentsecret"));
        assert!(scrubbed.contains("https://host/path?<redacted>"));
        assert!(
            scrubbed.contains("meanwhile"),
            "prose after the URL must survive: {scrubbed}"
        );
    }

    #[test]
    fn scrub_credentials_redacts_userinfo_and_query_together() {
        // Same fake token as the sibling tests. The production-tree PII gate
        // allowlists this exact literal and nothing else — a fixture that
        // invents its own token shape fails that gate, which is what guards
        // the production push.
        let raw = "clone https://x-access-token:ghp_supersecret@github.com/o/r.git?token=alsosecret: 401";
        let scrubbed = scrub_credentials(raw);
        assert!(!scrubbed.contains("ghp_supersecret"));
        assert!(!scrubbed.contains("alsosecret"));
        assert!(scrubbed.contains("<redacted>@"));
        assert!(scrubbed.contains("?<redacted>"));
    }

    #[test]
    fn scrub_credentials_is_a_noop_without_userinfo() {
        let raw = "clone https://github.com/karakijihad/Tesseract.git: could not resolve host";
        assert_eq!(scrub_credentials(raw), raw);
    }

    #[test]
    fn classify_clone_error_reports_auth_shaped_failures_without_leaking_a_credential() {
        // A credentialed URL can still appear in a libgit2 message if the user
        // set TESSERACT_REPO_URL by hand, so scrubbing stays load-bearing even
        // though the shell no longer supplies credentials of its own.
        let raw = "clone https://x-access-token:ghp_supersecret@github.com/karakijihad/Tesseract.git: unexpected http status code: 401 Unauthorized";
        let ProvisionError(msg) = classify_clone_error(raw);
        assert!(!msg.contains("ghp_supersecret"));
        assert!(
            !msg.to_lowercase().contains("token"),
            "a public repo must never ask the user for a token, got: {msg}"
        );
        assert!(msg.contains("moved or be temporarily unavailable"));
    }

    #[test]
    fn classify_clone_error_reports_not_found_as_unreachable_rather_than_a_network_fault() {
        // A renamed or deleted repository 404s. Telling that user to check
        // their internet connection sends them after the wrong problem.
        let raw = "clone https://github.com/karakijihad/Tesseract.git: unexpected http status code: 404 Not Found";
        let ProvisionError(msg) = classify_clone_error(raw);
        assert!(msg.contains("moved or be temporarily unavailable"));
        assert!(!msg.contains("internet connection"));
    }

    #[test]
    fn classify_clone_error_classifies_network_failures_as_a_connection_problem() {
        let raw = "clone https://github.com/karakijihad/Tesseract.git: failed to resolve host";
        let ProvisionError(msg) = classify_clone_error(raw);
        assert!(msg.contains("internet connection"));
    }

    /// Regression fixture: the exact (credential-free) error text libgit2's
    /// WinHTTP backend produces for a real, unresolvable host on Windows —
    /// captured from a live run against
    /// `https://this-host-can-never-resolve.invalid/...`.
    /// The original phrase list (`"resolve host"`/`"resolve address"`) did
    /// not match this real wording and fell through to the generic fallback
    /// message instead of the friendlier network one.
    #[test]
    fn classify_clone_error_matches_the_real_windows_dns_failure_wording() {
        let raw = "clone https://this-host-can-never-resolve.invalid/owner/repo.git: failed to \
                   send request: The server name or address could not be resolved\r\n; class=Os (2)";
        let ProvisionError(msg) = classify_clone_error(raw);
        assert!(msg.contains("internet connection"));
    }

    #[test]
    fn classify_clone_error_classifies_disk_space_failures_as_disk_space() {
        let raw = "clone https://github.com/karakijihad/Tesseract.git: no space left on device";
        let ProvisionError(msg) = classify_clone_error(raw);
        assert!(msg.contains("disk space"));
    }

    /// Manual, network-touching proof that a real DNS failure reports as a
    /// connection problem rather than an unreachable-repository one, so the
    /// user is sent after the right cause. `#[ignore]`d because DNS
    /// resolution can be slow to fail depending on the network; run manually:
    /// `cargo test --lib clone_app_dir_with_unreachable_host_reports_a_connection_problem -- --ignored`
    #[test]
    #[ignore]
    fn clone_app_dir_with_unreachable_host_reports_a_connection_problem() {
        let base = TempDir::new("unreachable-host");
        let home = base.join("home");
        let app_dir = home.join("app");
        // `.invalid` is reserved by RFC 2606 — guaranteed never to resolve.
        let url = "https://this-host-can-never-resolve.invalid/owner/repo.git";

        match clone_app_dir_with(&app_dir, url) {
            Err(ProvisionError(msg)) => {
                assert!(
                    !msg.to_lowercase().contains("token"),
                    "a network failure must never mention a token, got: {msg}"
                );
                assert!(msg.contains("internet connection"));
            }
            Ok(()) => panic!("cloning an unresolvable host must fail"),
        }
    }

    /// End-to-end proof of the real, env-var-driven path (`clone_app_dir`,
    /// which reads `TESSERACT_REPO_URL` via `repo::repo_url` exactly as
    /// `provision()` does) against a local throwaway repo — no network
    /// access, and the production repo does not need to exist. `#[ignore]`d
    /// because it mutates a process-global env var that
    /// `repo::tests::repo_url_prefers_the_env_override_then_the_default`
    /// also exercises; run manually and serially to verify:
    /// `cargo test --lib clone_app_dir_end_to_end_via_env_vars -- --ignored --test-threads=1`
    #[test]
    #[ignore]
    fn clone_app_dir_end_to_end_via_env_vars() {
        let base = TempDir::new("env-e2e");
        let origin = make_origin(&base);
        let home = base.join("home");
        let app_dir = home.join("app");

        std::env::set_var("TESSERACT_REPO_URL", origin.to_string_lossy().into_owned());

        clone_app_dir(&app_dir).expect("env-var-driven clone should succeed");
        assert!(app_dir.join(".git").exists());
        assert!(app_dir.join("pyproject.toml").exists());

        // Second call against the now-cloned dir must no-op, matching
        // provision()'s real re-launch behavior.
        clone_app_dir(&app_dir).expect("second call should no-op, not error");

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
        write_marker(home.path(), ProvisionScope::Answered).unwrap();
        let py = venv_python(home.path());
        std::fs::create_dir_all(py.parent().unwrap()).unwrap();
        std::fs::write(&py, "").unwrap();

        assert!(!is_provisioned(home.path()));
    }

    #[test]
    fn is_provisioned_false_when_venv_python_is_missing() {
        let home = TempDir::new("no-venv-python");
        write_marker(home.path(), ProvisionScope::Answered).unwrap();
        std::fs::create_dir_all(home.join("app").join(".git")).unwrap();

        assert!(!is_provisioned(home.path()));
    }

    #[test]
    fn is_provisioned_true_when_marker_matches_and_both_artifacts_exist() {
        let home = TempDir::new("intact");
        write_marker(home.path(), ProvisionScope::Answered).unwrap();
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
        write_marker(home.path(), ProvisionScope::Answered).unwrap();
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

        write_marker(home.path(), ProvisionScope::Answered).expect("write_marker should succeed");

        assert!(home.join("runtime").exists());
        assert!(marker_matches(home.path()));
    }

    // -- setup_unanswered ---------------------------------------------------

    /// The whole point of the field. An unanswered run has to write the marker
    /// — without it every later launch re-clones an app that is already
    /// installed — and before the scope was recorded, that same marker also
    /// told the launch path there was nothing left to ask. One transient
    /// failure to open a window therefore cost the operator the setup form
    /// permanently.
    #[test]
    fn an_unanswered_run_is_provisioned_and_still_has_a_question_outstanding() {
        let home = TempDir::new("scope-unanswered");
        write_marker(home.path(), ProvisionScope::Unanswered).unwrap();
        std::fs::create_dir_all(home.join("app").join(".git")).unwrap();
        let py = venv_python(home.path());
        std::fs::create_dir_all(py.parent().unwrap()).unwrap();
        std::fs::write(&py, "").unwrap();

        assert!(
            is_provisioned(home.path()),
            "the app IS installed — an unanswered run must never reinstall it"
        );
        assert!(
            setup_unanswered(home.path()),
            "and it must still be offered the form it never got"
        );
    }

    #[test]
    fn an_answered_run_has_nothing_outstanding() {
        let home = TempDir::new("scope-answered");
        write_marker(home.path(), ProvisionScope::Answered).unwrap();

        assert!(!setup_unanswered(home.path()));
    }

    /// Every install provisioned before the field existed. Reading a missing
    /// scope as unanswered would put a setup form in front of the entire
    /// existing population on their next launch.
    #[test]
    fn a_marker_without_a_scope_is_treated_as_answered() {
        let home = TempDir::new("scope-legacy");
        std::fs::create_dir_all(home.join("runtime")).unwrap();
        std::fs::write(
            marker_path(home.path()),
            serde_json::json!({ "deps_version": DEPS_VERSION }).to_string(),
        )
        .unwrap();

        assert!(marker_matches(home.path()), "the install itself is fine");
        assert!(!setup_unanswered(home.path()));
    }

    #[test]
    fn an_absent_or_unreadable_marker_asks_nothing() {
        let home = TempDir::new("scope-absent");
        assert!(!setup_unanswered(home.path()));

        std::fs::create_dir_all(home.join("runtime")).unwrap();
        std::fs::write(marker_path(home.path()), "{not json").unwrap();
        assert!(
            !setup_unanswered(home.path()),
            "a malformed marker is handled by is_provisioned, which re-provisions \
             and asks in the ordinary way — this must not also claim a deferral"
        );
    }

    // -- clear_setup_deferred -----------------------------------------------

    /// The second half of the fix. `consent.py` reads this file as "nobody was
    /// asked, so the shipped catalog's `enabled: true` is a default rather
    /// than an answer" — which outranks the answers just given if it survives
    /// them.
    #[test]
    fn answering_the_form_clears_the_marker_that_says_nobody_was_asked() {
        let home = TempDir::new("clear-deferred");
        write_setup_deferred(home.path(), "the setup form was never answered");
        assert!(setup_deferred_path(home.path()).exists());

        clear_setup_deferred(home.path());

        assert!(!setup_deferred_path(home.path()).exists());
    }

    #[test]
    fn clearing_a_deferral_that_was_never_written_is_a_silent_noop() {
        let home = TempDir::new("clear-deferred-noop");
        std::fs::create_dir_all(home.join("runtime")).unwrap();

        clear_setup_deferred(home.path()); // must not panic

        assert!(!setup_deferred_path(home.path()).exists());
    }

    // -- the provisioning stages -------------------------------------------------

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
    ///
    /// It used to assert `provision_hardware` was in this list directly. The
    /// hardware stage now runs INSIDE `launch_refresh`, which is what fixed
    /// the race between it and the fetchers — so this side can only guard the
    /// entry point, and the Python half of the same invariant is asserted by
    /// `test_launch_refresh.py::test_the_hardware_stage_runs_before_the_pass`.
    /// Both halves are needed: neither language can see the other's.
    #[test]
    fn the_launch_refresh_list_carries_the_maintenance_pass() {
        let modules: Vec<&str> = LAUNCH_REFRESH_ASSETS
            .iter()
            .filter_map(|args| args.last().copied())
            .collect();
        assert!(
            modules.contains(&"tesseract.scripts.launch_refresh"),
            "the launch maintenance pass must run on every launch, got {modules:?}",
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

    /// Both halves, back to back, because the sequence is one sequence even
    /// though a form now sits in the middle of it: the fetch stages read the
    /// config the base half installed and the form's answers wrote.
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

        base_stages(home.path(), &uv, &progress, &run_uv).expect("the app should install");
        let py = extras_stages(home.path(), ProvisionScope::Answered, &progress, &run_python)
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
                    "tesseract.scripts.fetch_wake_models".to_string(),
                ],
            ),
            (
                py_path.clone(),
                vec![
                    "-m".to_string(),
                    "tesseract.orchestrator.browser.provision".to_string(),
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
                "Applying your setup…".to_string(),
                "Checking your hardware…".to_string(),
                "Downloading speech recognition model…".to_string(),
                "Downloading voice models…".to_string(),
                "Downloading browser engine…".to_string(),
                "Downloading reranker model…".to_string(),
                "Setting up embeddings…".to_string(),
                "Ready.".to_string(),
            ],
            "progress messages must fire in stage order"
        );

        assert!(marker_path(home.path()).exists());
    }

    // -- streaming output, stage counting, byte markers --------------------

    /// `TOTAL_STAGES` is rendered to the operator as "Step N of TOTAL", so a
    /// stage added without touching the constant would make every counter in
    /// the run wrong — and nothing else would notice. Counted here against the
    /// real functions rather than by hand.
    #[test]
    fn total_stages_matches_what_the_stage_functions_actually_emit() {
        let home = TempDir::new("stage-count");
        let uv = PathBuf::from("uv-stub");
        let progress_log: RefCell<Vec<String>> = RefCell::new(Vec::new());

        let progress = |msg: &str| progress_log.borrow_mut().push(msg.to_string());
        let ok = |_: &Path, _: &[&str]| -> Result<(), String> { Ok(()) };

        base_stages(home.path(), &uv, &progress, &ok).expect("the app should install");
        extras_stages(home.path(), ProvisionScope::Answered, &progress, &ok)
            .expect("stages should succeed");

        // Every stage except the terminal "Ready.", plus the clone that
        // `provision_base()` performs before calling in.
        let staged = progress_log.borrow().len() as u32 - 1;
        assert_eq!(
            staged + 1,
            TOTAL_STAGES,
            "a stage was added or removed without updating TOTAL_STAGES"
        );
        assert_eq!(progress_log.borrow().last().map(String::as_str), Some("Ready."));
    }

    /// The split point, asserted rather than assumed. `BASE_STAGES` is what
    /// the extras half resumes its counter from, so a stage moved across the
    /// form without touching it would renumber the second half — the operator
    /// would watch "step 4 of 11" run twice.
    #[test]
    fn base_stages_emits_exactly_what_the_split_constant_claims() {
        let home = TempDir::new("base-count");
        let uv = PathBuf::from("uv-stub");
        let progress_log: RefCell<Vec<String>> = RefCell::new(Vec::new());

        let progress = |msg: &str| progress_log.borrow_mut().push(msg.to_string());
        let ok = |_: &Path, _: &[&str]| -> Result<(), String> { Ok(()) };

        base_stages(home.path(), &uv, &progress, &ok).expect("the app should install");

        // Plus the clone, which `provision_base` performs before calling in.
        assert_eq!(progress_log.borrow().len() as u32 + 1, BASE_STAGES);
        assert!(
            !marker_path(home.path()).exists(),
            "the app is not provisioned until the form has been answered — a marker \
             here would make every later launch skip the questions entirely"
        );
    }

    // -- the unanswered run: install the app, ask nobody for anything ------

    /// A first run whose splash window fails to open fell through
    /// to provisioning on the shipped defaults — every speech model, the
    /// reranker, and the Ollama vendor installer with `allow_install=True`.
    /// No consent, because the thing that would have asked is the thing that
    /// broke. Harmless while every install was the operator's own; public, it
    /// is several gigabytes arriving on a stranger's machine because a window
    /// failed to open.
    #[test]
    fn an_unanswered_run_downloads_nothing_optional() {
        let home = TempDir::new("unanswered");
        let uv = PathBuf::from("uv-stub");
        let modules: RefCell<Vec<String>> = RefCell::new(Vec::new());

        let progress = |_: &str| {};
        let ok_uv = |_: &Path, _: &[&str]| -> Result<(), String> { Ok(()) };
        let run_python = |_: &Path, args: &[&str]| -> Result<(), String> {
            if let Some(module) = args.iter().position(|a| *a == "-m").map(|i| args[i + 1]) {
                modules.borrow_mut().push(module.to_string());
            }
            Ok(())
        };

        base_stages(home.path(), &uv, &progress, &ok_uv).expect("the app should install");
        extras_stages(home.path(), ProvisionScope::Unanswered, &progress, &run_python)
            .expect("an unanswered run still installs the app");

        let modules = modules.borrow();
        for optional in [
            "tesseract.scripts.fetch_whisper_model",
            "tesseract.scripts.fetch_kokoro_voice",
            "tesseract.scripts.fetch_wake_models",
            "tesseract.scripts.fetch_reranker_model",
            "tesseract.scripts.ensure_ollama",
        ] {
            assert!(
                !modules.iter().any(|m| m == optional),
                "{optional} ran without anyone having been asked; got {modules:?}"
            );
        }
    }

    /// The app still gets built. Refusing to provision at all would trade an
    /// unasked download for no install on a machine whose splash could not
    /// open, which is the offline case the best-effort design exists to serve.
    #[test]
    fn an_unanswered_run_still_installs_the_app_and_marks_it_complete() {
        let home = TempDir::new("unanswered-app");
        let uv = PathBuf::from("uv-stub");
        let modules: RefCell<Vec<String>> = RefCell::new(Vec::new());

        let progress = |_: &str| {};
        let ok_uv = |_: &Path, _: &[&str]| -> Result<(), String> { Ok(()) };
        let run_python = |_: &Path, args: &[&str]| -> Result<(), String> {
            if let Some(module) = args.iter().position(|a| *a == "-m").map(|i| args[i + 1]) {
                modules.borrow_mut().push(module.to_string());
            }
            Ok(())
        };

        base_stages(home.path(), &uv, &progress, &ok_uv).expect("the app should install");
        let py = extras_stages(home.path(), ProvisionScope::Unanswered, &progress, &run_python)
            .expect("stages should succeed");

        assert_eq!(py, venv_python(home.path()));
        assert!(
            marker_path(home.path()).exists(),
            "an unanswered run is a COMPLETE install of the app; without the marker \
             every later launch would re-provision"
        );
        let modules = modules.borrow();
        assert!(
            !modules.iter().any(|m| m == "tesseract.orchestrator.browser.provision"),
            "the browser engine is ~700 MB and optional — an unanswered run must not              fetch it any more than it fetches a voice model"
        );
        assert!(
            modules.iter().any(|m| m == "tesseract.scripts.apply_first_run_setup"),
            "the config seed must still run — there is a config to write into either way"
        );
    }

    /// The marker an unanswered run leaves must say which run left it. This is
    /// the fact the launch path reads to offer the form a second time; without
    /// it, the install that most needs asking is the one that never gets asked.
    #[test]
    fn an_unanswered_run_stamps_its_scope_into_the_marker() {
        let home = TempDir::new("stamp-unanswered");
        let uv = PathBuf::from("uv-stub");
        let progress = |_: &str| {};
        let ok = |_: &Path, _: &[&str]| -> Result<(), String> { Ok(()) };

        base_stages(home.path(), &uv, &progress, &ok).expect("the app should install");
        extras_stages(home.path(), ProvisionScope::Unanswered, &progress, &ok)
            .expect("stages should succeed");

        assert!(setup_unanswered(home.path()));
    }

    /// The launch that finally asks. It runs the extras against an app that is
    /// already installed, and it has to leave behind an install that no longer
    /// claims to be waiting for an answer — in the marker AND in the file
    /// `consent.py` reads.
    #[test]
    fn answering_on_a_later_launch_stamps_answered_and_clears_the_deferral() {
        let home = TempDir::new("stamp-answered");
        let uv = PathBuf::from("uv-stub");
        let progress = |_: &str| {};
        let ok = |_: &Path, _: &[&str]| -> Result<(), String> { Ok(()) };

        // The state an earlier deferred run left on disk.
        base_stages(home.path(), &uv, &progress, &ok).expect("the app should install");
        extras_stages(home.path(), ProvisionScope::Unanswered, &progress, &ok).unwrap();
        write_setup_deferred(home.path(), "the setup form was never answered");
        assert!(setup_unanswered(home.path()));

        // The form is answered on a later launch: extras only, no base.
        extras_stages(home.path(), ProvisionScope::Answered, &progress, &ok)
            .expect("the extras must run against an app that is already installed");

        assert!(!setup_unanswered(home.path()));
        assert!(
            !setup_deferred_path(home.path()).exists(),
            "a deferral marker outliving the answer would keep every real choice \
             reading as never-asked"
        );
    }

    /// The near-miss this ordering exists to avoid. The unanswered path runs
    /// `apply_first_run_setup` too — it seeds the config tree, which is why it
    /// sits above the scope check — and that stage SUCCEEDS with nothing to
    /// apply. Clearing on the stage's exit code rather than on the effective
    /// scope would delete the deferral marker `provision_extras` wrote seconds
    /// earlier, and the next launch would read the shipped catalog's
    /// `enabled: true` as consent.
    #[test]
    fn an_unanswered_run_does_not_clear_the_deferral_it_just_wrote() {
        let home = TempDir::new("keep-deferral");
        let uv = PathBuf::from("uv-stub");
        let progress = |_: &str| {};
        let ok = |_: &Path, _: &[&str]| -> Result<(), String> { Ok(()) };

        base_stages(home.path(), &uv, &progress, &ok).expect("the app should install");
        write_setup_deferred(home.path(), "the setup form was never answered");
        extras_stages(home.path(), ProvisionScope::Unanswered, &progress, &ok)
            .expect("stages should succeed");

        assert!(setup_deferred_path(home.path()).exists());
    }

    /// An answered run whose answers did not land is an unanswered one, and
    /// that downgrade has to reach the marker as well as the fetch stages —
    /// otherwise the install records an answer nothing on disk reflects, and
    /// the form is never offered again.
    #[test]
    fn a_downgraded_run_is_stamped_unanswered_and_keeps_its_deferral() {
        let home = TempDir::new("stamp-downgraded");
        let uv = PathBuf::from("uv-stub");
        let progress = |_: &str| {};
        let ok_uv = |_: &Path, _: &[&str]| -> Result<(), String> { Ok(()) };
        let run_python = |_: &Path, args: &[&str]| -> Result<(), String> {
            if args.contains(&"tesseract.scripts.apply_first_run_setup") {
                return Err("the answers could not be applied".to_string());
            }
            Ok(())
        };

        base_stages(home.path(), &uv, &progress, &ok_uv).expect("the app should install");
        extras_stages(home.path(), ProvisionScope::Answered, &progress, &run_python)
            .expect("a failed apply must not fail the install");

        assert!(setup_unanswered(home.path()));
        assert!(setup_deferred_path(home.path()).exists());
    }

    // -- the setup manifest boundary ---------------------------------------
    //
    // The whole first-run form arrives through this parse. Nothing exercised
    // it: the Python tests call `build_manifest()` directly and the splash
    // test injects a synthetic event, so a regression here skipped the form
    // and fell to an unanswered install with every test green.

    /// The half `parse_manifest` cannot reach: which interpreter is spawned,
    /// which module, and that the state root travels with it. Asserted against
    /// the same `Command` the real call builds, one line above `.spawn()`.
    ///
    /// It reached the form through an untested boundary — a wrong argv or a
    /// dropped `TESSERACT_HOME` would skip the questions and fall to an
    /// unanswered install with the parser tests green.
    #[test]
    fn the_manifest_command_names_the_venv_python_and_carries_the_state_root() {
        let home = TempDir::new("manifest-cmd");
        let cmd = manifest_command(home.path());

        assert_eq!(
            std::path::Path::new(cmd.get_program()),
            venv_python(home.path()),
            "the manifest must run under the provisioned venv, not a PATH python"
        );
        let args: Vec<_> = cmd.get_args().map(|a| a.to_string_lossy().into_owned()).collect();
        assert_eq!(args, vec!["-m", "tesseract.scripts.setup_manifest"]);

        let envs: Vec<_> = cmd.get_envs().collect();
        let home_env = envs
            .iter()
            .find(|(k, _)| *k == std::ffi::OsStr::new("TESSERACT_HOME"))
            .map(|(_, v)| v.expect("TESSERACT_HOME must carry a value"));
        assert_eq!(
            home_env,
            Some(home_dir(home.path()).as_os_str()),
            "without the state root the script seeds and reads the wrong config"
        );
    }

    #[test]
    fn a_marked_manifest_line_parses() {
        let out = format!("{MANIFEST_MARKER}{{\"extras\":[],\"keys\":[]}}");

        let value = parse_manifest(out.as_bytes(), b"", true, Some(0))
            .expect("a marked line is the manifest");

        assert!(value["extras"].is_array());
    }

    /// The reason the line is marked at all. A vendor banner printed by any
    /// library the script imports used to prefix the JSON and cost the
    /// operator the entire form on a machine that was working perfectly.
    #[test]
    fn a_banner_on_stdout_does_not_cost_the_form() {
        let out = format!(
            "NVIDIA driver loaded\n{MANIFEST_MARKER}{{\"extras\":[]}}\ntrailing noise"
        );

        let value = parse_manifest(out.as_bytes(), b"", true, Some(0))
            .expect("the marked line is found among the noise");

        assert!(value["extras"].is_array());
    }

    #[test]
    fn a_non_zero_exit_is_not_a_manifest() {
        let err = parse_manifest(b"", b"boom", false, Some(1))
            .expect_err("a failed script has no manifest");

        assert!(err.contains("exited 1"), "got {err}");
    }

    /// Distinct from a failed script, and the message says which: the run
    /// falls to an unanswered install either way, and the log line is the only
    /// thing that tells the operator whether Python broke or something ate the
    /// output.
    #[test]
    fn stdout_with_no_marked_line_is_reported_as_such() {
        let err = parse_manifest(b"{\"extras\":[]}", b"", true, Some(0))
            .expect_err("an unmarked line is not the manifest");

        assert!(err.contains(MANIFEST_MARKER), "got {err}");
    }

    #[test]
    fn a_marked_line_that_is_not_json_is_reported_as_such() {
        let out = format!("{MANIFEST_MARKER}{{not json");

        let err = parse_manifest(out.as_bytes(), b"", true, Some(0))
            .expect_err("malformed JSON is not a manifest");

        assert!(err.contains("readable JSON"), "got {err}");
    }

    /// The Python and Rust halves of one string. Neither can see the other's,
    /// and a prefix changed on one side alone is a form that never opens.
    #[test]
    fn the_marker_matches_the_one_the_script_writes() {
        let script = std::fs::read_to_string(
            std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("../../scripts/setup_manifest.py"),
        )
        .expect("the manifest script must be readable from the shell crate");

        assert!(
            script.contains(&format!("MARKER = \"{MANIFEST_MARKER}\"")),
            "setup_manifest.py does not declare MARKER = \"{MANIFEST_MARKER}\""
        );
    }

    /// An unanswered run emits exactly the stages `DEFERRED_STAGES` says it
    /// skips — counted against the same denominator every other run uses.
    ///
    /// The denominator used to shrink here, from 11 to 6, because the extras
    /// tracker took a scope the base tracker could not know. The operator
    /// watched "step 4 of 11" become "step 5 of 6".
    #[test]
    fn the_unanswered_run_skips_exactly_the_deferred_stages() {
        let home = TempDir::new("unanswered-count");
        let uv = PathBuf::from("uv-stub");
        let progress_log: RefCell<Vec<String>> = RefCell::new(Vec::new());

        let progress = |msg: &str| progress_log.borrow_mut().push(msg.to_string());
        let ok = |_: &Path, _: &[&str]| -> Result<(), String> { Ok(()) };

        base_stages(home.path(), &uv, &progress, &ok).expect("the app should install");
        extras_stages(home.path(), ProvisionScope::Unanswered, &progress, &ok)
            .expect("stages should succeed");

        // Same accounting as the answered case: every stage but the terminal
        // "Ready.", plus the clone `provision_base()` performs before calling in.
        let staged = progress_log.borrow().len() as u32 - 1;
        assert_eq!(staged + 1, TOTAL_STAGES - DEFERRED_STAGES);
        assert_eq!(progress_log.borrow().last().map(String::as_str), Some("Ready."));
    }

    /// One denominator for the whole run, both phases, either scope. A total
    /// that changes partway through is a bar walking backwards, and the base
    /// phase cannot know the scope — it runs before anyone is asked.
    #[test]
    fn the_denominator_never_changes_mid_run() {
        for scope in [ProvisionScope::Answered, ProvisionScope::Unanswered] {
            assert_eq!(StageTracker::starting_at(0).total, TOTAL_STAGES);
            assert_eq!(StageTracker::starting_at(BASE_STAGES).total, TOTAL_STAGES);
            let _ = scope;
        }
    }

    /// Read by the Python side, where `enabled: true` in the shipped catalog
    /// stops counting as an answer. Without it the launch pass would fetch on
    /// the next start exactly what this run declined to fetch.
    #[test]
    fn the_deferred_marker_records_why_nobody_was_asked() {
        let home = TempDir::new("deferred-marker");

        write_setup_deferred(home.path(), "the setup window could not be opened");

        let text = std::fs::read_to_string(setup_deferred_path(home.path()))
            .expect("the marker should exist");
        let parsed: serde_json::Value = serde_json::from_str(&text).unwrap();
        assert_eq!(parsed["reason"], "the setup window could not be opened");
    }

    #[test]
    fn parse_progress_marker_reads_a_full_marker() {
        let parsed = parse_progress_marker(
            "TESSERACT_PROGRESS file=model.bin received=1048576 expected=1600000000",
        )
        .expect("a well-formed marker must parse");
        assert_eq!(parsed, ("model.bin".to_string(), 1_048_576, Some(1_600_000_000)));
    }

    /// A server that sends no Content-Length is the normal case for some
    /// mirrors, and the marker says so with `-` rather than omitting the
    /// field. Parsing it as "no total" is what makes the splash show a rising
    /// figure instead of a percentage of nothing.
    #[test]
    fn parse_progress_marker_accepts_an_unknown_total() {
        let parsed = parse_progress_marker(
            "TESSERACT_PROGRESS file=voices-v1.0.bin received=42 expected=-",
        )
        .expect("an unknown total must still parse");
        assert_eq!(parsed, ("voices-v1.0.bin".to_string(), 42, None));
    }

    #[test]
    fn parse_progress_marker_ignores_ordinary_output() {
        assert!(parse_progress_marker("Downloading pygments (1.2MiB)").is_none());
        assert!(parse_progress_marker("").is_none());
        // Right prefix, no usable numbers — must not be mistaken for a tick
        // and silently render as zero bytes.
        assert!(parse_progress_marker("TESSERACT_PROGRESS file=x").is_none());
    }

    fn collect_lines(input: &[u8]) -> Vec<String> {
        let (tx, rx) = mpsc::channel();
        split_lines(input, false, tx);
        rx.into_iter().map(|(_, line)| line).collect()
    }

    /// Measured against the shipped `uv` and `playwright`, neither emits a
    /// bare `\r` through a pipe — but `ollama pull` redraws its bar with one,
    /// and a reader that only split on `\n` would hold the whole download in
    /// a single line that arrives after it finishes.
    #[test]
    fn split_lines_treats_carriage_returns_as_terminators() {
        assert_eq!(
            collect_lines(b"first\nsecond\rthird\r\nfourth"),
            vec!["first", "second", "third", "fourth"],
            "both terminators split, and a trailing line with neither still arrives"
        );
    }

    #[test]
    fn split_lines_drops_blank_and_whitespace_only_lines() {
        assert_eq!(collect_lines(b"\n\n  \nreal\n\n"), vec!["real"]);
    }

    /// A tool rendering a bar with no terminator at all must not be able to
    /// grow one "line" without bound in memory.
    #[test]
    fn split_lines_flushes_an_unterminated_line_at_the_cap() {
        let oversized = vec![b'x'; MAX_LINE_BYTES + 10];
        let lines = collect_lines(&oversized);
        assert_eq!(lines.len(), 2, "must flush at the cap rather than buffer on");
        assert_eq!(lines[0].len(), MAX_LINE_BYTES);
        assert_eq!(lines[1].len(), 10);
    }

    /// The launch-refresh half of the same problem. `refresh_optional_assets`
    /// spawns six fetchers on every launch and never waits on them, so none
    /// reach `run_tool` and quit had nothing to stop — including the one that
    /// can be pulling ~2.2 GB of CUDA wheels.
    #[cfg(windows)]
    #[test]
    fn stop_active_kills_a_registered_refresh_child() {
        let _guard = run_tool_guard();
        REFRESH_CHILDREN.lock().unwrap().clear();

        let mut cmd = std::process::Command::new("cmd");
        cmd.args(["/C", "ping -n 30 127.0.0.1 >nul"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        hide_console(&mut cmd);
        let child = cmd.spawn().expect("the probe child should spawn");
        let pid = child.id();
        REFRESH_CHILDREN.lock().unwrap().push(child);

        assert!(stop_active(), "a running refresh child must be reported stopped");
        assert!(
            REFRESH_CHILDREN.lock().unwrap().is_empty(),
            "the registry must be drained, not left holding dead handles"
        );

        // The handle is released by now, so ask the OS rather than the struct.
        let listed = std::process::Command::new("tasklist")
            .args(["/FI", &format!("PID eq {pid}")])
            .output()
            .expect("tasklist should run");
        let listed = String::from_utf8_lossy(&listed.stdout);
        assert!(
            !listed.contains(&pid.to_string()),
            "pid {pid} still running after the quit; tasklist said: {listed}"
        );
    }

    /// A fetcher that finished on its own must not be taskkilled by pid. It
    /// cannot reach an unrelated process while the `Child` is held — but only
    /// because the exit is observed before the handle is released.
    #[cfg(windows)]
    #[test]
    fn stop_active_does_not_kill_a_refresh_child_that_already_exited() {
        let _guard = run_tool_guard();
        REFRESH_CHILDREN.lock().unwrap().clear();

        let mut cmd = std::process::Command::new("cmd");
        cmd.args(["/C", "exit 0"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        hide_console(&mut cmd);
        let mut child = cmd.spawn().expect("the probe child should spawn");
        let _ = child.wait();
        REFRESH_CHILDREN.lock().unwrap().push(child);

        assert!(
            !stop_active(),
            "nothing was running, so nothing should be reported stopped"
        );
        assert!(REFRESH_CHILDREN.lock().unwrap().is_empty());
    }

    /// Killing the running child was not enough. Every stage after the
    /// dependency install swallows its error, so a killed stage let the
    /// provisioning thread walk straight on to the next `run_tool` and spawn a
    /// child that `RunEvent::Exit` had already had its one look for — the
    /// abandoned-child case reappearing one stage later.
    #[cfg(windows)]
    #[test]
    fn run_tool_refuses_to_spawn_once_a_quit_has_latched() {
        let _guard = run_tool_guard();
        let (program, args) = cmd_args("echo should-never-run");
        let argv: Vec<&str> = args.iter().map(String::as_str).collect();
        let seen: RefCell<Vec<String>> = RefCell::new(Vec::new());

        // `stop_active` with nothing running still latches, which is exactly
        // the state a quit between two stages leaves behind.
        stop_active();
        let result = run_tool(&program, &argv, "cmd", None, &|line| {
            seen.borrow_mut().push(line.to_string())
        });
        assert!(result.is_err(), "a latched quit must refuse the next stage");
        assert!(
            seen.borrow().is_empty(),
            "nothing may be spawned after a quit; got {:?}",
            seen.borrow()
        );
    }

    /// Windows-only because the shell is: the test drives `cmd` directly
    /// rather than a stub, which is the point — the buffered runner this
    /// replaced passed every unit test it had while showing the operator
    /// nothing for ten minutes.
    #[cfg(windows)]
    fn cmd_args(script: &str) -> (PathBuf, Vec<String>) {
        (PathBuf::from("cmd"), vec!["/C".to_string(), script.to_string()])
    }

    /// `STOPPING` and `ACTIVE_PID` are process-global, and `cargo test` runs
    /// these in parallel threads — so the latch test would make its neighbours
    /// fail at random if they overlapped. Held by every test that touches
    /// either. Poison is recovered rather than propagated: one failing test
    /// must not turn into three.
    #[cfg(windows)]
    static RUN_TOOL_LOCK: Mutex<()> = Mutex::new(());

    #[cfg(windows)]
    fn run_tool_guard() -> std::sync::MutexGuard<'static, ()> {
        let guard = RUN_TOOL_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        // Cleared on ACQUIRE, not by the latch test on its way out: a panic
        // there would otherwise leave the flag set for every test behind it,
        // turning one failure into four. Production never clears it — a quit
        // is terminal — so this lives here rather than in `stop_active`.
        STOPPING.store(false, std::sync::atomic::Ordering::SeqCst);
        guard
    }

    #[cfg(windows)]
    #[test]
    fn run_tool_forwards_both_streams_because_the_tools_disagree_about_which_to_use() {
        let _guard = run_tool_guard();
        let (program, args) = cmd_args("echo out-line& echo err-line 1>&2");
        let argv: Vec<&str> = args.iter().map(String::as_str).collect();
        let seen: RefCell<Vec<String>> = RefCell::new(Vec::new());

        run_tool(&program, &argv, "cmd", None, &|line| {
            seen.borrow_mut().push(line.to_string())
        })
        .expect("a zero exit must succeed");

        let seen = seen.borrow();
        assert!(
            seen.iter().any(|l| l == "out-line"),
            "stdout must be forwarded — playwright writes its progress there; got {seen:?}"
        );
        assert!(
            seen.iter().any(|l| l == "err-line"),
            "stderr must be forwarded — uv writes EVERYTHING there; got {seen:?}"
        );
    }

    /// The tail must cover BOTH streams. `uv` puts its errors on stderr but
    /// `playwright` writes to stdout, so a stderr-only tail — which is what
    /// the buffered runner had, since it only ever kept `out.stderr` — would
    /// show an empty "Setup failed:" for a playwright failure while the real
    /// text sat in `shell.log`.
    #[cfg(windows)]
    #[test]
    fn run_tool_failure_carries_the_tail_of_both_streams() {
        let _guard = run_tool_guard();
        let (program, args) =
            cmd_args("echo stdout-error& echo stderr-error 1>&2& exit 1");
        let argv: Vec<&str> = args.iter().map(String::as_str).collect();

        let err = run_tool(&program, &argv, "cmd", None, &|_| {})
            .expect_err("a non-zero exit must fail");

        assert!(
            err.contains("stderr-error"),
            "the failure must carry what stderr said; got {err}"
        );
        assert!(
            err.contains("stdout-error"),
            "and what stdout said — playwright fails there; got {err}"
        );
        assert!(err.starts_with("cmd "), "the label must name the tool; got {err}");
    }

    /// Every line now reaches the screen on the SUCCESS path, not only inside
    /// a failure message — so a credentialed URL echoed by a tool would be
    /// newly visible if the scrub were not applied per line.
    #[cfg(windows)]
    #[test]
    fn run_tool_scrubs_credentials_out_of_every_forwarded_line() {
        let _guard = run_tool_guard();
        let (program, args) = cmd_args("echo fetching https://user:secret@example.invalid/x.git");
        let argv: Vec<&str> = args.iter().map(String::as_str).collect();
        let seen: RefCell<Vec<String>> = RefCell::new(Vec::new());

        run_tool(&program, &argv, "cmd", None, &|line| {
            seen.borrow_mut().push(line.to_string())
        })
        .expect("echo should succeed");

        let joined = seen.borrow().join("\n");
        assert!(!joined.contains("secret"), "a credential reached the sink: {joined}");
        assert!(joined.contains("<redacted>@example.invalid"));
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
        let progress = |_: &str| {};

        let err = base_stages(home.path(), &uv, &progress, &run_uv)
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
        let progress = |_: &str| {};

        let err = base_stages(home.path(), &uv, &progress, &run_uv)
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
        let progress = |_: &str| {};

        let err = base_stages(home.path(), &uv, &progress, &run_uv)
            .expect_err("a dependency-install failure must abort");

        assert_eq!(err, "pip install boom");
        assert_eq!(
            *call_count.borrow(),
            3,
            "must abort right after the third stage"
        );
        assert!(!marker_path(home.path()).exists());
    }

    /// The reverse of what this test used to assert, and the reversal is the
    /// point. A browser-engine failure used to abort the whole install,
    /// because the stage ran before anyone had been asked and the engine was
    /// treated as part of the app. It is optional now — ~700 MB, nothing at
    /// boot depends on it — so a failure here costs the browser tools and
    /// leaves a complete, marked install behind.
    #[test]
    fn a_browser_engine_failure_does_not_abort_the_install() {
        let home = TempDir::new("fail-browser");
        let uv = PathBuf::from("uv-stub");
        let seen: RefCell<Vec<String>> = RefCell::new(Vec::new());

        let run_uv = |_: &Path, _: &[&str]| -> Result<(), String> { Ok(()) };
        let run_python = |_: &Path, args: &[&str]| -> Result<(), String> {
            let module = args.get(1).copied().unwrap_or_default().to_string();
            seen.borrow_mut().push(module.clone());
            if module == "tesseract.orchestrator.browser.provision" {
                return Err("playwright install boom".to_string());
            }
            Ok(())
        };
        let progress = |_: &str| {};

        base_stages(home.path(), &uv, &progress, &run_uv).expect("the app should install");
        extras_stages(home.path(), ProvisionScope::Answered, &progress, &run_python)
            .expect("an optional stage must not fail the install");

        let seen = seen.borrow();
        assert!(
            seen.iter().any(|m| m == "tesseract.scripts.fetch_reranker_model"),
            "the stages after it must still run; got {seen:?}"
        );
        assert!(marker_path(home.path()).exists());
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
            // `apply_first_run_setup` is deliberately NOT in this list any
            // more; see the test below it. Its failure is the one that must
            // stop the fetches, because it is what writes the answers they
            // read and records the consent they rest on.
            "tesseract.scripts.provision_hardware",
            "tesseract.scripts.fetch_whisper_model",
            "tesseract.scripts.fetch_kokoro_voice",
            "tesseract.scripts.fetch_wake_models",
            "tesseract.scripts.fetch_reranker_model",
            "tesseract.scripts.ensure_ollama",
        ] {
            let home = TempDir::new("optional-stage-fails");
            let uv = PathBuf::from("uv-stub");
            let seen: RefCell<Vec<String>> = RefCell::new(Vec::new());
            let run_uv = |_: &Path, _: &[&str]| -> Result<(), String> { Ok(()) };
            let progress = |_: &str| {};

            base_stages(home.path(), &uv, &progress, &run_uv).expect("the app should install");
            let py = extras_stages(
                home.path(),
                ProvisionScope::Answered,
                &progress,
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

    /// The one exception, and the reason it is one.
    ///
    /// `apply_first_run_setup` is what writes the operator's answers into the
    /// config the fetch stages read, AND what records them in the ledger. If
    /// it did not run, that config is the shipped default and the ledger is
    /// empty — so carrying on would download every default lane on the
    /// strength of answers that never landed anywhere. An answered run whose
    /// answers were not applied is, for the purposes of what may be
    /// downloaded, an unanswered one.
    #[test]
    fn a_setup_that_could_not_be_applied_stops_the_optional_downloads() {
        let home = TempDir::new("apply-failed");
        let uv = PathBuf::from("uv-stub");
        let seen: RefCell<Vec<String>> = RefCell::new(Vec::new());
        let run_uv = |_: &Path, _: &[&str]| -> Result<(), String> { Ok(()) };
        let progress = |_: &str| {};

        base_stages(home.path(), &uv, &progress, &run_uv).expect("the app should install");
        let py = extras_stages(
            home.path(),
            ProvisionScope::Answered,
            &progress,
            &failing_module_run("tesseract.scripts.apply_first_run_setup", &seen),
        )
        .expect("a failed apply is still not fatal — the app installs");

        assert_eq!(py, venv_python(home.path()));
        let seen = seen.borrow();
        for optional in [
            "tesseract.scripts.fetch_whisper_model",
            "tesseract.scripts.fetch_kokoro_voice",
            "tesseract.scripts.fetch_wake_models",
            "tesseract.scripts.fetch_reranker_model",
            "tesseract.scripts.ensure_ollama",
        ] {
            assert!(
                !seen.iter().any(|m| m == optional),
                "{optional} ran on the strength of answers that were never applied; \
                 got {seen:?}"
            );
        }
        // The hardware stage still runs: it is what records the profile, and
        // its own consent gate is where the wheels are decided.
        assert!(seen.iter().any(|m| m == "tesseract.scripts.provision_hardware"));
        assert!(
            setup_deferred_path(home.path()).exists(),
            "the next launch must not treat the shipped defaults as answers either"
        );
        assert!(marker_path(home.path()).exists(), "the app is still installed");
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
