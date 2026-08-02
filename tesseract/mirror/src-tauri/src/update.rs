use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};

use serde::Serialize;
use tauri::State;

use crate::provision;
use crate::{
    lock_or_recover, repo, request_supervisor_stop, shell_log, spawn_supervisor, SupervisorProc,
    TesseractHome,
};

#[derive(Serialize)]
pub struct UpdateStatus {
    pub behind: usize,
    pub summaries: Vec<String>,
    pub version: String,
    /// `Some` only when the checked-out branch actually diverged from
    /// `origin/main` (local commits and/or uncommitted changes) — the
    /// condition that would make `update_apply`'s fast-forward refuse. Lets
    /// the UI warn and offer `update_force_apply` before the user ever
    /// clicks Apply, instead of only finding out from a failed attempt.
    pub divergence: Option<repo::Divergence>,
}

/// Guards `update_apply` against a second concurrent invocation — a repeat
/// Apply click while the first call is still mid-flight (which can take up
/// to ~30s, dominated by `request_supervisor_stop`'s graceful-stop wait)
/// would otherwise race two supervisor stop/respawn lifecycles against each
/// other. A plain `AtomicBool` rather than a `Mutex`: the second call must
/// REJECT immediately with an error, not queue and silently redo the whole
/// stop/fast-forward/respawn sequence a second time once the first finishes.
pub struct UpdateInProgress(AtomicBool);

impl UpdateInProgress {
    pub fn new() -> Self {
        UpdateInProgress(AtomicBool::new(false))
    }
}

impl Default for UpdateInProgress {
    fn default() -> Self {
        Self::new()
    }
}

/// Releases the in-progress flag whenever `update_apply` returns — success,
/// an early error return, or a panic unwind — so a failure can never leave
/// the guard permanently stuck "in progress".
struct ApplyGuard<'a>(&'a AtomicBool);

impl Drop for ApplyGuard<'_> {
    fn drop(&mut self) {
        self.0.store(false, Ordering::SeqCst);
    }
}

fn app_dir(home: &Path) -> PathBuf {
    crate::provision::app_dir(home)
}

/// Human-readable version for the Settings row and HUD: the pyproject
/// semver with the short SHA in parentheses — `1.0.3 (a1b2c3d)`. The
/// bare SHA it used to show answers "which commit", but an operator (or
/// a friend reporting a problem) thinks in release numbers. Falls back
/// to the SHA alone when pyproject.toml is unreadable — version display
/// must never fail an update check.
fn display_version(dir: &Path) -> Result<String, String> {
    let sha = repo::head_short(dir)?;
    Ok(match pyproject_version(dir) {
        Some(v) => format!("{v} ({sha})"),
        None => sha,
    })
}

/// First `version = "…"` line of `<dir>/tesseract/pyproject.toml`. Plain
/// line scan, not a TOML parser: the file is ours, `[project] version`
/// is its first table entry, and a parser dependency for one key is not
/// worth it.
fn pyproject_version(dir: &Path) -> Option<String> {
    let text = std::fs::read_to_string(dir.join("tesseract").join("pyproject.toml")).ok()?;
    for line in text.lines() {
        let line = line.trim();
        if let Some(rest) = line.strip_prefix("version") {
            let rest = rest.trim_start();
            if let Some(rest) = rest.strip_prefix('=') {
                let v = rest.trim().trim_matches('"').trim_matches('\'');
                if !v.is_empty() {
                    return Some(v.to_string());
                }
            }
        }
    }
    None
}

/// Acquires the concurrency guard, or rejects if an `update_apply` call is
/// already in flight. Split out from `update_apply` so the exact mechanism
/// it relies on (not a reimplementation of it) is directly unit-testable.
fn try_acquire(guard: &AtomicBool) -> Result<(), String> {
    if guard
        .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
        .is_err()
    {
        return Err("an update is already in progress".to_string());
    }
    Ok(())
}

/// Distinguishes which stage of `diff_and_reinstall` failed, since the two
/// stages leave the repo in different states: a `FastForward` failure means
/// `dir` never moved (still the previous SHA); a `Reinstall` failure means
/// `dir` already sits at `sha` with the *previous* dependencies still
/// installed. `update_apply` needs this distinction to report an accurate
/// failure message instead of a vague "might be either" one.
#[derive(Debug)]
enum ApplyStageError {
    FastForward(String),
    Reinstall { sha: String, err: String },
}

/// Advances `dir` to `origin/main` (caller must have already fetched, e.g.
/// via `update_check`/`check_behind`) and invokes `reinstall` only if
/// `tesseract/pyproject.toml` differs before vs. after. `advance` is
/// injected so the same decision logic serves both the normal update path
/// (`repo::fast_forward`, which refuses on divergence) and the explicit
/// force path (`repo::reset_to_remote`, which discards it). `reinstall` is
/// injected too — production passes a closure over
/// `provision::reinstall_deps` (which needs a live Tauri `AppHandle` to
/// resolve the bundled `uv.exe`), while tests substitute a counting stub so
/// the decision logic can be exercised against real throwaway git repos
/// without a Tauri runtime.
fn diff_and_reinstall_with(
    dir: &std::path::Path,
    advance: impl FnOnce(&std::path::Path) -> Result<String, String>,
    reinstall: impl FnOnce() -> Result<(), String>,
) -> Result<String, ApplyStageError> {
    let pyproject = dir.join("tesseract").join("pyproject.toml");
    let before = std::fs::read(&pyproject).unwrap_or_default();
    let sha = advance(dir).map_err(ApplyStageError::FastForward)?;
    let after = std::fs::read(&pyproject).unwrap_or_default();
    if before != after {
        if let Err(err) = reinstall() {
            return Err(ApplyStageError::Reinstall { sha, err });
        }
    }
    Ok(sha)
}

#[tauri::command]
pub fn update_check(home: State<TesseractHome>) -> Result<UpdateStatus, String> {
    let dir = app_dir(&home.0);
    let token = repo::github_token(&home.0);
    // check_behind fetches over the network; a libgit2/libcurl transport
    // error can echo the remote URL back verbatim, so scrub before it ever
    // reaches the frontend.
    let (behind, summaries) =
        repo::check_behind(&dir, token).map_err(|e| provision::scrub_credentials(&e))?;
    // Best-effort: a divergence-reporting failure must not block reporting
    // behind/summaries/version, which are the primary purpose of this
    // command. `None` (rather than surfacing the error) also correctly
    // covers the ordinary case of no divergence at all.
    let divergence = repo::local_divergence(&dir)
        .ok()
        .filter(|d| d.ahead > 0 || d.dirty_total > 0);
    Ok(UpdateStatus {
        behind,
        summaries,
        version: display_version(&dir)?,
        divergence,
    })
}

/// Shared preamble for both update commands: logs invocation, resolves
/// `<home>/app`, and stops the running supervisor. From this point until the
/// caller's `apply_update`/`force_apply_update` call respawns it, nothing is
/// running.
fn begin_update(label: &str, home: &Path, proc: &SupervisorProc) -> PathBuf {
    shell_log::log(&format!("{label}: invoked"));
    let dir = app_dir(home);
    if let Some(mut child) = lock_or_recover(&proc.0).take() {
        request_supervisor_stop(home, &mut child);
    }
    dir
}

/// The `respawn` closure both commands pass into `apply_update`/
/// `force_apply_update`: spawns a fresh supervisor and stores it in the
/// shared `SupervisorProc` slot.
fn respawn_into(home: PathBuf, proc: &SupervisorProc) -> impl Fn() -> Result<(), String> + '_ {
    move || match spawn_supervisor(&home) {
        Ok(child) => {
            *lock_or_recover(&proc.0) = Some(child);
            Ok(())
        }
        Err(e) => Err(e.to_string()),
    }
}

fn log_update_result(label: &str, result: &Result<String, String>) {
    match result {
        Ok(sha) => shell_log::log(&format!("{label}: succeeded, now on {sha}")),
        Err(msg) => shell_log::log_error(&format!("{label}: {msg}")),
    }
}

/// Stops the running supervisor, fast-forwards `<home>/app` to `origin/main`,
/// reinstalls Python deps only if `pyproject.toml` changed, then respawns.
///
/// Ordering is the whole design: once the supervisor is stopped there is no
/// running app until this function spawns a new one, so every failure branch
/// that runs after that point makes a best-effort respawn attempt before
/// returning an error — the caller must never be left with a dead app when a
/// respawn is possible.
#[tauri::command]
pub fn update_apply(
    app: tauri::AppHandle,
    home: State<TesseractHome>,
    proc: State<SupervisorProc>,
    guard: State<UpdateInProgress>,
) -> Result<String, String> {
    try_acquire(&guard.0)?;
    let _release = ApplyGuard(&guard.0);

    let dir = begin_update("update_apply", &home.0, &proc);
    let respawn = respawn_into(home.0.clone(), &proc);

    let result = apply_update(
        &dir,
        &|| provision::reinstall_deps(&app, &home.0),
        &respawn,
        &|| provision::invalidate_marker(&home.0),
    );

    log_update_result("update_apply", &result);
    result
}

/// The explicit "discard local changes & update" path. Reachable only from
/// an operator-confirmed UI action — never from `update_check` or any
/// automatic path — because it discards local divergence instead of
/// refusing on it. Acquires the same `UpdateInProgress` guard as
/// `update_apply` (there is only ever one update in flight, regardless of
/// which command started it) and otherwise mirrors it exactly, differing
/// only in the `advance` function `force_apply_update` injects.
#[tauri::command]
pub fn update_force_apply(
    app: tauri::AppHandle,
    home: State<TesseractHome>,
    proc: State<SupervisorProc>,
    guard: State<UpdateInProgress>,
) -> Result<String, String> {
    try_acquire(&guard.0)?;
    let _release = ApplyGuard(&guard.0);

    let dir = begin_update("update_force_apply", &home.0, &proc);
    let respawn = respawn_into(home.0.clone(), &proc);

    let result = force_apply_update(
        &dir,
        &|| provision::reinstall_deps(&app, &home.0),
        &respawn,
        &|| provision::invalidate_marker(&home.0),
    );

    log_update_result("update_force_apply", &result);
    result
}

/// The whole update decision tree, with every side effect the Tauri layer
/// owns injected: `advance` moves the repo forward (`repo::fast_forward` for
/// the normal path, `repo::reset_to_remote` for the explicit force path),
/// `reinstall` (needs an `AppHandle` to find `uv.exe`), `respawn` (owns the
/// `SupervisorProc` state slot), and `invalidate` (drops the provisioning
/// marker). The caller must have already stopped the supervisor — from here
/// to the end of this function nothing is running, so every branch respawns
/// before returning.
///
/// Split out from the `#[tauri::command]`s for the same reason
/// `diff_and_reinstall` was: none of these branches were reachable from a
/// test while they lived inside a command, and the reinstall-failure branch
/// is the one that can leave a user with an app that never works again.
///
/// The invariant it enforces: **an update never leaves code and dependencies
/// mismatched.** A dependency install that fails after `advance` moved the
/// repo is rolled back to the exact revision the installed dependencies
/// match, rather than respawning the mismatched pair. If the rollback itself
/// fails, the provisioning marker is dropped so the next launch re-provisions
/// instead of respawning that mismatch forever.
fn apply_update_with(
    dir: &std::path::Path,
    advance: impl FnOnce(&std::path::Path) -> Result<String, String>,
    reinstall: &dyn Fn() -> Result<(), String>,
    respawn: &dyn Fn() -> Result<(), String>,
    invalidate: &dyn Fn(),
) -> Result<String, String> {
    // Captured before anything moves; `None` means rollback is impossible
    // and the marker-invalidation path is the only remaining recovery.
    let previous = repo::head_oid(dir).ok();

    match diff_and_reinstall_with(dir, advance, reinstall) {
        Ok(sha) => match respawn() {
            Ok(()) => Ok(sha),
            Err(e) => Err(format!(
                "updated to {sha}, but restarting the app failed ({e}) — restart TESSERACT manually"
            )),
        },

        // The repo never moved — bring the previous version back up.
        Err(ApplyStageError::FastForward(e)) => Err(match respawn() {
            Ok(()) => format!("update failed ({e}); restarted on the previous version"),
            Err(_) => format!(
                "update failed ({e}); additionally failed to restart the app — \
                 restart TESSERACT manually"
            ),
        }),

        // Code moved to `sha`; dependencies did not follow.
        Err(ApplyStageError::Reinstall { sha, err }) => {
            let rollback = match previous.as_deref() {
                Some(oid) => repo::reset_hard(dir, oid),
                None => Err("the pre-update revision was never recorded".to_string()),
            };
            Err(match rollback {
                Ok(()) => {
                    shell_log::log("update: rolled back to the pre-update revision");
                    match respawn() {
                        Ok(()) => format!(
                            "update to {sha} failed ({err}); TESSERACT was rolled back to the \
                             previous version and restarted — retry the update later"
                        ),
                        Err(_) => format!(
                            "update to {sha} failed ({err}); TESSERACT was rolled back to the \
                             previous version, but restarting failed — restart TESSERACT manually"
                        ),
                    }
                }
                Err(rb) => {
                    // Neither forward nor back: the tree is at `sha` with the
                    // previous dependencies. Dropping the marker is what stops
                    // this from repeating on every launch.
                    invalidate();
                    match respawn() {
                        Ok(()) => format!(
                            "update to {sha} failed ({err}) and rolling back failed ({rb}); \
                             TESSERACT will repair itself the next time you restart it"
                        ),
                        Err(_) => format!(
                            "update to {sha} failed ({err}) and rolling back failed ({rb}); \
                             restart TESSERACT — it will repair itself on the next launch"
                        ),
                    }
                }
            })
        }
    }
}

/// The normal update path: `advance` is `repo::fast_forward`, which refuses
/// on a diverged history rather than moving anything.
fn apply_update(
    dir: &std::path::Path,
    reinstall: &dyn Fn() -> Result<(), String>,
    respawn: &dyn Fn() -> Result<(), String>,
    invalidate: &dyn Fn(),
) -> Result<String, String> {
    apply_update_with(dir, repo::fast_forward, reinstall, respawn, invalidate)
}

/// The explicit force path: `advance` is `repo::reset_to_remote`, which
/// discards any local divergence — uncommitted changes and local-only
/// commits alike — instead of refusing. Same decision tree from here: a
/// reinstall failure after the reset still rolls back and, failing that,
/// invalidates the provisioning marker exactly as the normal path does.
fn force_apply_update(
    dir: &std::path::Path,
    reinstall: &dyn Fn() -> Result<(), String>,
    respawn: &dyn Fn() -> Result<(), String>,
    invalidate: &dyn Fn(),
) -> Result<String, String> {
    apply_update_with(dir, repo::reset_to_remote, reinstall, respawn, invalidate)
}

#[tauri::command]
pub fn app_version(home: State<TesseractHome>) -> Result<String, String> {
    display_version(&app_dir(&home.0))
}

/// Everything the always-rendered Settings About block needs, independent
/// of the Python backend: semver, short SHA, and the installed HEAD's
/// commit time (the de-facto release date — no network involved).
#[derive(Serialize)]
pub struct AppInfo {
    pub semver: Option<String>,
    pub sha: String,
    /// Unix seconds of the installed HEAD commit; the frontend formats it.
    pub commit_epoch: i64,
}

#[tauri::command]
pub fn app_info(home: State<TesseractHome>) -> Result<AppInfo, String> {
    let dir = app_dir(&home.0);
    Ok(AppInfo {
        semver: pyproject_version(&dir),
        sha: repo::head_short(&dir)?,
        commit_epoch: repo::head_commit_time(&dir)?,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::test_support::{configure_identity, init_repo, write_commit, TempDir};
    use git2::Repository;
    use std::cell::Cell;

    /// Clones a fresh origin (one commit, `tesseract/pyproject.toml` +
    /// `tesseract/other.py` present) into `dest` and fetches so
    /// `repo::fast_forward` has `refs/remotes/origin/main` to read, mirroring
    /// how `update_check`'s `check_behind` call always precedes
    /// `update_apply` in production.
    fn make_origin_and_synced_clone(base: &TempDir) -> (std::path::PathBuf, std::path::PathBuf) {
        let origin_path = base.join("origin");
        let origin = init_repo(&origin_path);
        write_commit(
            &origin,
            "tesseract/pyproject.toml",
            "[project]\nname=\"tesseract\"\nversion=\"1\"\n",
            "c1",
        );
        write_commit(&origin, "tesseract/other.py", "x = 1\n", "c1b");
        drop(origin);

        let dest = base.join("dest");
        repo::clone(&origin_path.to_string_lossy(), &dest, None).expect("clone dest");
        (origin_path, dest)
    }

    #[test]
    fn display_version_combines_pyproject_semver_with_the_short_sha() {
        let base = TempDir::new("display-version");
        let (_origin, dest) = make_origin_and_synced_clone(&base);

        let sha = repo::head_short(&dest).unwrap();
        assert_eq!(display_version(&dest).unwrap(), format!("1 ({sha})"));
    }

    #[test]
    fn display_version_falls_back_to_the_sha_when_pyproject_is_unreadable() {
        let base = TempDir::new("display-version-fallback");
        let (_origin, dest) = make_origin_and_synced_clone(&base);
        std::fs::remove_file(dest.join("tesseract").join("pyproject.toml")).unwrap();

        let sha = repo::head_short(&dest).unwrap();
        assert_eq!(display_version(&dest).unwrap(), sha);
    }

    #[test]
    fn diff_and_reinstall_fires_only_when_pyproject_changed() {
        let base = TempDir::new("pyproject-changed");
        let (origin_path, dest) = make_origin_and_synced_clone(&base);

        {
            let origin = Repository::open(&origin_path).unwrap();
            write_commit(
                &origin,
                "tesseract/pyproject.toml",
                "[project]\nname=\"tesseract\"\nversion=\"2\"\n",
                "c2 bumps pyproject",
            );
        }
        repo::check_behind(&dest, None).expect("fetch before fast_forward");

        let reinstalled = Cell::new(false);
        let sha = diff_and_reinstall_with(&dest, repo::fast_forward, || {
            reinstalled.set(true);
            Ok(())
        })
        .expect("fast-forward should succeed");

        assert!(
            reinstalled.get(),
            "changing tesseract/pyproject.toml must trigger a dep reinstall"
        );
        assert_eq!(sha, repo::head_short(&dest).unwrap());
    }

    #[test]
    fn diff_and_reinstall_skips_reinstall_when_pyproject_is_untouched() {
        let base = TempDir::new("pyproject-untouched");
        let (origin_path, dest) = make_origin_and_synced_clone(&base);

        {
            let origin = Repository::open(&origin_path).unwrap();
            write_commit(
                &origin,
                "tesseract/other.py",
                "x = 2\n",
                "c2 touches other.py only",
            );
        }
        repo::check_behind(&dest, None).expect("fetch before fast_forward");

        let reinstalled = Cell::new(false);
        let sha = diff_and_reinstall_with(&dest, repo::fast_forward, || {
            reinstalled.set(true);
            Ok(())
        })
        .expect("fast-forward should succeed");

        assert!(
            !reinstalled.get(),
            "a commit that never touches tesseract/pyproject.toml must not trigger a reinstall"
        );
        assert_eq!(sha, repo::head_short(&dest).unwrap());
    }

    #[test]
    fn diff_and_reinstall_never_reinstalls_when_fast_forward_fails() {
        let base = TempDir::new("diverged");
        let (origin_path, dest) = make_origin_and_synced_clone(&base);

        // Diverge dest with a local-only commit that also touches
        // pyproject.toml, then advance origin — fast_forward must refuse,
        // and the reinstall closure must never run even though the
        // would-be-fetched pyproject differs.
        {
            let repo = Repository::open(&dest).unwrap();
            configure_identity(&repo);
            write_commit(
                &repo,
                "tesseract/pyproject.toml",
                "[project]\nname=\"tesseract\"\nversion=\"local-diverged\"\n",
                "local-only",
            );
        }
        {
            let origin = Repository::open(&origin_path).unwrap();
            write_commit(&origin, "tesseract/other.py", "x = 3\n", "c2 on origin");
        }
        repo::check_behind(&dest, None).expect("fetch before fast_forward");

        let reinstalled = Cell::new(false);
        let result = diff_and_reinstall_with(&dest, repo::fast_forward, || {
            reinstalled.set(true);
            Ok(())
        });

        assert!(
            matches!(result, Err(ApplyStageError::FastForward(_))),
            "a diverged history must surface as a FastForward-stage error"
        );
        assert!(
            !reinstalled.get(),
            "a refused fast-forward must never trigger a reinstall"
        );
    }

    #[test]
    fn diff_and_reinstall_reports_reinstall_stage_failures_with_the_new_sha() {
        let base = TempDir::new("reinstall-fails");
        let (origin_path, dest) = make_origin_and_synced_clone(&base);

        {
            let origin = Repository::open(&origin_path).unwrap();
            write_commit(
                &origin,
                "tesseract/pyproject.toml",
                "[project]\nname=\"tesseract\"\nversion=\"3\"\n",
                "c2 bumps pyproject",
            );
        }
        repo::check_behind(&dest, None).expect("fetch before fast_forward");
        let expected_sha = {
            let origin = Repository::open(&origin_path).unwrap();
            drop(origin);
            // fast_forward will move dest to origin's head; compute it from
            // origin directly since dest hasn't moved yet at this point.
            repo::head_short(&origin_path).unwrap()
        };

        let result = diff_and_reinstall_with(&dest, repo::fast_forward, || {
            Err("uv pip install failed".to_string())
        });

        match result {
            Err(ApplyStageError::Reinstall { sha, err }) => {
                assert_eq!(
                    sha, expected_sha,
                    "the repo must have already moved to the new SHA"
                );
                assert_eq!(err, "uv pip install failed");
                assert_eq!(
                    repo::head_short(&dest).unwrap(),
                    expected_sha,
                    "a reinstall-stage failure must leave the repo fast-forwarded, not rolled back"
                );
            }
            other => panic!("expected an ApplyStageError::Reinstall, got {other:?}"),
        }
    }

    #[test]
    fn try_acquire_rejects_a_concurrent_second_call_and_releases_on_drop() {
        let flag = AtomicBool::new(false);

        try_acquire(&flag).expect("first call should acquire the guard");
        let err = try_acquire(&flag)
            .expect_err("a second call while the first is still in flight must be rejected");
        assert!(err.contains("already in progress"));

        // Simulate the first call finishing: ApplyGuard's Drop releases it.
        {
            let _release = ApplyGuard(&flag);
        }
        assert!(
            try_acquire(&flag).is_ok(),
            "after release, a later call must be able to acquire the guard again"
        );
    }

    // -- apply_update -------------------------------------------------------

    #[test]
    fn apply_update_success_reinstalls_and_respawns() {
        let base = TempDir::new("success");
        let (origin_path, dest) = make_origin_and_synced_clone(&base);
        {
            let origin = Repository::open(&origin_path).unwrap();
            write_commit(
                &origin,
                "tesseract/pyproject.toml",
                "[project]\nname=\"tesseract\"\nversion=\"2\"\n",
                "c2 bumps pyproject",
            );
        }
        repo::check_behind(&dest, None).expect("fetch before apply");
        let expected_sha = repo::head_short(&origin_path).unwrap();

        let reinstall_calls = Cell::new(0u32);
        let respawn_calls = Cell::new(0u32);
        let invalidate_calls = Cell::new(0u32);

        let result = apply_update(
            &dest,
            &|| {
                reinstall_calls.set(reinstall_calls.get() + 1);
                Ok(())
            },
            &|| {
                respawn_calls.set(respawn_calls.get() + 1);
                Ok(())
            },
            &|| invalidate_calls.set(invalidate_calls.get() + 1),
        );

        assert_eq!(result, Ok(expected_sha.clone()));
        assert_eq!(repo::head_short(&dest).unwrap(), expected_sha);
        assert_eq!(reinstall_calls.get(), 1);
        assert_eq!(respawn_calls.get(), 1);
        assert_eq!(invalidate_calls.get(), 0);
    }

    #[test]
    fn apply_update_success_skips_reinstall_when_pyproject_is_untouched() {
        let base = TempDir::new("success-no-reinstall");
        let (origin_path, dest) = make_origin_and_synced_clone(&base);
        {
            let origin = Repository::open(&origin_path).unwrap();
            write_commit(
                &origin,
                "tesseract/other.py",
                "x = 2\n",
                "c2 touches other.py only",
            );
        }
        repo::check_behind(&dest, None).expect("fetch before apply");
        let expected_sha = repo::head_short(&origin_path).unwrap();

        let reinstall_calls = Cell::new(0u32);
        let respawn_calls = Cell::new(0u32);

        let result = apply_update(
            &dest,
            &|| {
                reinstall_calls.set(reinstall_calls.get() + 1);
                Ok(())
            },
            &|| {
                respawn_calls.set(respawn_calls.get() + 1);
                Ok(())
            },
            &|| panic!("invalidate must not be called on a clean success"),
        );

        assert_eq!(result, Ok(expected_sha.clone()));
        assert_eq!(repo::head_short(&dest).unwrap(), expected_sha);
        assert_eq!(
            reinstall_calls.get(),
            0,
            "an update that never touches pyproject.toml must skip reinstall"
        );
        assert_eq!(respawn_calls.get(), 1);
    }

    #[test]
    fn apply_update_fast_forward_failure_restarts_the_previous_version() {
        let base = TempDir::new("ff-fails-respawn-ok");
        let (origin_path, dest) = make_origin_and_synced_clone(&base);

        {
            let repo = Repository::open(&dest).unwrap();
            configure_identity(&repo);
            write_commit(
                &repo,
                "tesseract/pyproject.toml",
                "[project]\nname=\"tesseract\"\nversion=\"local-diverged\"\n",
                "local-only",
            );
        }
        {
            let origin = Repository::open(&origin_path).unwrap();
            write_commit(&origin, "tesseract/other.py", "x = 3\n", "c2 on origin");
        }
        repo::check_behind(&dest, None).expect("fetch before apply");
        let old_sha = repo::head_short(&dest).unwrap();

        let respawn_calls = Cell::new(0u32);
        let invalidate_calls = Cell::new(0u32);

        let result = apply_update(
            &dest,
            &|| panic!("reinstall must not run when the fast-forward itself fails"),
            &|| {
                respawn_calls.set(respawn_calls.get() + 1);
                Ok(())
            },
            &|| invalidate_calls.set(invalidate_calls.get() + 1),
        );

        let err = result.expect_err("a diverged fast-forward must fail");
        assert!(err.contains("previous version"), "got: {err}");
        assert_eq!(
            repo::head_short(&dest).unwrap(),
            old_sha,
            "the repo must never have moved"
        );
        assert_eq!(respawn_calls.get(), 1);
        assert_eq!(invalidate_calls.get(), 0);
    }

    #[test]
    fn apply_update_fast_forward_failure_and_respawn_failure_asks_for_manual_restart() {
        let base = TempDir::new("ff-fails-respawn-fails");
        let (origin_path, dest) = make_origin_and_synced_clone(&base);

        {
            let repo = Repository::open(&dest).unwrap();
            configure_identity(&repo);
            write_commit(
                &repo,
                "tesseract/pyproject.toml",
                "[project]\nname=\"tesseract\"\nversion=\"local-diverged\"\n",
                "local-only",
            );
        }
        {
            let origin = Repository::open(&origin_path).unwrap();
            write_commit(&origin, "tesseract/other.py", "x = 3\n", "c2 on origin");
        }
        repo::check_behind(&dest, None).expect("fetch before apply");
        let old_sha = repo::head_short(&dest).unwrap();

        let result = apply_update(
            &dest,
            &|| panic!("reinstall must not run when the fast-forward itself fails"),
            &|| Err("spawn failed".to_string()),
            &|| panic!("invalidate must not be called on a fast-forward failure"),
        );

        let err = result.expect_err("both the update and the respawn failed");
        assert!(err.contains("restart TESSERACT manually"), "got: {err}");
        assert_eq!(
            repo::head_short(&dest).unwrap(),
            old_sha,
            "the repo must never have moved"
        );
    }

    #[test]
    fn apply_update_reinstall_failure_rolls_back_to_the_pre_update_oid() {
        let base = TempDir::new("reinstall-fails-rollback-ok");
        let (origin_path, dest) = make_origin_and_synced_clone(&base);
        let pre_update_oid = repo::head_oid(&dest).unwrap();

        {
            let origin = Repository::open(&origin_path).unwrap();
            write_commit(
                &origin,
                "tesseract/pyproject.toml",
                "[project]\nname=\"tesseract\"\nversion=\"2\"\n",
                "c2 bumps pyproject",
            );
        }
        repo::check_behind(&dest, None).expect("fetch before apply");

        let respawn_calls = Cell::new(0u32);
        let invalidate_calls = Cell::new(0u32);

        let result = apply_update(
            &dest,
            &|| Err("pip install boom".to_string()),
            &|| {
                respawn_calls.set(respawn_calls.get() + 1);
                Ok(())
            },
            &|| invalidate_calls.set(invalidate_calls.get() + 1),
        );

        assert!(result.is_err());
        assert_eq!(
            repo::head_oid(&dest).unwrap(),
            pre_update_oid,
            "a failed reinstall must roll the tree back to the revision the installed \
             dependencies match, not leave it fast-forwarded"
        );
        assert_eq!(respawn_calls.get(), 1);
        assert_eq!(invalidate_calls.get(), 0);
    }

    #[test]
    fn apply_update_reinstall_failure_rollback_ok_but_respawn_fails() {
        let base = TempDir::new("reinstall-fails-respawn-fails");
        let (origin_path, dest) = make_origin_and_synced_clone(&base);
        let pre_update_oid = repo::head_oid(&dest).unwrap();

        {
            let origin = Repository::open(&origin_path).unwrap();
            write_commit(
                &origin,
                "tesseract/pyproject.toml",
                "[project]\nname=\"tesseract\"\nversion=\"3\"\n",
                "c2 bumps pyproject",
            );
        }
        repo::check_behind(&dest, None).expect("fetch before apply");

        let result = apply_update(
            &dest,
            &|| Err("pip install boom".to_string()),
            &|| Err("spawn failed".to_string()),
            &|| panic!("invalidate must not be called when rollback succeeds"),
        );

        let err = result.expect_err("reinstall and respawn both failed");
        assert!(err.contains("restart TESSERACT manually"), "got: {err}");
        assert_eq!(
            repo::head_oid(&dest).unwrap(),
            pre_update_oid,
            "the repo must still be rolled back even though the respawn also failed"
        );
    }

    /// The core regression this whole decision tree exists for: a dependency
    /// install failure *and* a rollback failure must never leave the app
    /// silently wedged with mismatched code/deps. Rollback failure is forced
    /// by having the `reinstall` closure itself delete `dest/.git` before
    /// returning its error — simulating disk/antivirus damage between the
    /// fast-forward and the rollback attempt — so `repo::reset_hard`'s
    /// `Repository::open` has nothing left to open.
    #[test]
    fn apply_update_reinstall_and_rollback_both_failing_invalidates_the_marker() {
        let base = TempDir::new("reinstall-and-rollback-fail");
        let (origin_path, dest) = make_origin_and_synced_clone(&base);

        {
            let origin = Repository::open(&origin_path).unwrap();
            write_commit(
                &origin,
                "tesseract/pyproject.toml",
                "[project]\nname=\"tesseract\"\nversion=\"4\"\n",
                "c2 bumps pyproject",
            );
        }
        repo::check_behind(&dest, None).expect("fetch before apply");

        let respawn_calls = Cell::new(0u32);
        let invalidate_calls = Cell::new(0u32);

        let result = apply_update(
            &dest,
            &|| {
                std::fs::remove_dir_all(dest.join(".git")).unwrap();
                Err("pip install boom".to_string())
            },
            &|| {
                respawn_calls.set(respawn_calls.get() + 1);
                Ok(())
            },
            &|| invalidate_calls.set(invalidate_calls.get() + 1),
        );

        assert!(result.is_err());
        assert_eq!(
            invalidate_calls.get(),
            1,
            "when neither forward nor rollback succeeds, the marker must be invalidated \
             exactly once so the next launch re-provisions"
        );
        assert_eq!(respawn_calls.get(), 1);
    }

    #[test]
    fn apply_update_success_but_respawn_fails() {
        let base = TempDir::new("success-respawn-fails");
        let (origin_path, dest) = make_origin_and_synced_clone(&base);
        {
            let origin = Repository::open(&origin_path).unwrap();
            write_commit(
                &origin,
                "tesseract/pyproject.toml",
                "[project]\nname=\"tesseract\"\nversion=\"2\"\n",
                "c2 bumps pyproject",
            );
        }
        repo::check_behind(&dest, None).expect("fetch before apply");
        let expected_sha = repo::head_short(&origin_path).unwrap();

        let invalidate_calls = Cell::new(0u32);

        let result = apply_update(
            &dest,
            &|| Ok(()),
            &|| Err("spawn failed".to_string()),
            &|| invalidate_calls.set(invalidate_calls.get() + 1),
        );

        let err = result.expect_err("respawn failed after an otherwise successful update");
        assert!(err.contains("restart TESSERACT manually"), "got: {err}");
        assert_eq!(
            repo::head_short(&dest).unwrap(),
            expected_sha,
            "code must remain updated even though the respawn failed"
        );
        assert_eq!(invalidate_calls.get(), 0);
    }

    // -- force_apply_update ---------------------------------------------------

    #[test]
    fn force_apply_update_succeeds_on_a_diverged_repo_where_apply_update_would_refuse() {
        let base = TempDir::new("force-apply-diverged");
        let (origin_path, dest) = make_origin_and_synced_clone(&base);

        // Diverge dest with a local-only commit that also touches
        // pyproject.toml, then advance origin's pyproject.toml too, so the
        // normal fast-forward path refuses.
        {
            let repo = Repository::open(&dest).unwrap();
            configure_identity(&repo);
            write_commit(
                &repo,
                "tesseract/pyproject.toml",
                "[project]\nname=\"tesseract\"\nversion=\"local-diverged\"\n",
                "local-only",
            );
        }
        {
            let origin = Repository::open(&origin_path).unwrap();
            write_commit(
                &origin,
                "tesseract/pyproject.toml",
                "[project]\nname=\"tesseract\"\nversion=\"2\"\n",
                "c2 bumps pyproject on origin",
            );
        }
        repo::check_behind(&dest, None).expect("fetch before apply");
        let expected_sha = repo::head_short(&origin_path).unwrap();

        assert!(
            repo::fast_forward(&dest).is_err(),
            "sanity check: the normal path really does refuse this diverged history"
        );

        let reinstall_calls = Cell::new(0u32);
        let respawn_calls = Cell::new(0u32);
        let invalidate_calls = Cell::new(0u32);

        let result = force_apply_update(
            &dest,
            &|| {
                reinstall_calls.set(reinstall_calls.get() + 1);
                Ok(())
            },
            &|| {
                respawn_calls.set(respawn_calls.get() + 1);
                Ok(())
            },
            &|| invalidate_calls.set(invalidate_calls.get() + 1),
        );

        assert_eq!(
            result,
            Ok(expected_sha.clone()),
            "force_apply_update must succeed where apply_update refuses"
        );
        assert_eq!(repo::head_short(&dest).unwrap(), expected_sha);
        assert_eq!(
            reinstall_calls.get(),
            1,
            "pyproject.toml differs before/after the reset, so reinstall must run"
        );
        assert_eq!(respawn_calls.get(), 1);
        assert_eq!(invalidate_calls.get(), 0);
    }
}
