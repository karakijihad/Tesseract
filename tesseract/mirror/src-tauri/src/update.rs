use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};

use serde::Serialize;
use tauri::State;

use crate::provision;
use crate::{repo, request_supervisor_stop, spawn_supervisor, SupervisorProc, TesseractHome};

#[derive(Serialize)]
pub struct UpdateStatus {
    pub behind: usize,
    pub summaries: Vec<String>,
    pub version: String,
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

fn app_dir(home: &PathBuf) -> PathBuf {
    home.join("app")
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

/// Fast-forwards `dir` to `origin/main` (caller must have already fetched,
/// e.g. via `update_check`/`check_behind`) and invokes `reinstall` only if
/// `tesseract/pyproject.toml` differs before vs. after. `reinstall` is
/// injected — production passes a closure over `provision::reinstall_deps`
/// (which needs a live Tauri `AppHandle` to resolve the bundled `uv.exe`),
/// while tests substitute a counting stub so the decision logic can be
/// exercised against real throwaway git repos without a Tauri runtime.
fn diff_and_reinstall(
    dir: &std::path::Path,
    reinstall: impl FnOnce() -> Result<(), String>,
) -> Result<String, ApplyStageError> {
    let pyproject = dir.join("tesseract").join("pyproject.toml");
    let before = std::fs::read(&pyproject).unwrap_or_default();
    let sha = repo::fast_forward(dir).map_err(ApplyStageError::FastForward)?;
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
    Ok(UpdateStatus {
        behind,
        summaries,
        version: repo::head_short(&dir)?,
    })
}

/// Stops the running supervisor, fast-forwards `<home>/app` to `origin/main`,
/// reinstalls Python deps only if `pyproject.toml` changed, then respawns.
///
/// Ordering is the whole design: once the supervisor is stopped there is no
/// running app until this function spawns a new one, so every failure branch
/// that runs after that point makes a best-effort respawn attempt before
/// returning an error — the caller must never be left with a dead app when a
/// respawn is possible. See `task-12-report.md` for the full failure-state
/// table (what the app looks like after a failure at each step).
#[tauri::command]
pub fn update_apply(
    app: tauri::AppHandle,
    home: State<TesseractHome>,
    proc: State<SupervisorProc>,
    guard: State<UpdateInProgress>,
) -> Result<String, String> {
    try_acquire(&guard.0)?;
    let _release = ApplyGuard(&guard.0);

    let dir = app_dir(&home.0);

    if let Some(mut child) = proc.0.lock().unwrap().take() {
        request_supervisor_stop(&home.0, &mut child);
    }

    // Both failure branches below run after the supervisor is already
    // stopped, so each makes a best-effort respawn before returning — the
    // caller must never be left with a dead app when a respawn is possible.
    let sha = match diff_and_reinstall(&dir, || provision::reinstall_deps(&app, &home.0)) {
        Ok(sha) => sha,
        Err(ApplyStageError::FastForward(e)) => {
            // The repo never moved — bring the previous version back up.
            return Err(respawn_after_failure(
                &home.0,
                &proc,
                format!("update failed ({e}); restarted on the previous version"),
                format!(
                    "update failed ({e}); additionally failed to restart the app — \
                     restart TESSERACT manually"
                ),
            ));
        }
        Err(ApplyStageError::Reinstall { sha, err }) => {
            // Code is already fast-forwarded to `sha`; deps did not
            // reinstall. Respawn anyway (best effort, old deps against new
            // code) rather than leaving nothing running at all.
            return Err(respawn_after_failure(
                &home.0,
                &proc,
                format!(
                    "updated to {sha}, but dependency install failed ({err}); the app was \
                     restarted on {sha} with the previous dependencies — it may not work \
                     correctly until you retry the update"
                ),
                format!(
                    "updated to {sha}, but dependency install failed ({err}); additionally \
                     failed to restart the app — restart TESSERACT manually"
                ),
            ));
        }
    };

    match spawn_supervisor(&home.0) {
        Ok(child) => {
            *proc.0.lock().unwrap() = Some(child);
            Ok(sha)
        }
        Err(e) => Err(format!(
            "updated to {sha}, but restarting the app failed ({e}) — restart TESSERACT manually"
        )),
    }
}

/// Best-effort respawn shared by every `update_apply` failure branch that
/// runs after the supervisor has already been stopped. Returns
/// `on_respawn_ok` if the respawn succeeded (the app is back up, just not on
/// the intended end state), or `on_respawn_err` if even that failed (the app
/// is down and needs a manual restart).
fn respawn_after_failure(
    home: &PathBuf,
    proc: &State<SupervisorProc>,
    on_respawn_ok: String,
    on_respawn_err: String,
) -> String {
    match spawn_supervisor(home) {
        Ok(child) => {
            *proc.0.lock().unwrap() = Some(child);
            on_respawn_ok
        }
        Err(_) => on_respawn_err,
    }
}

#[tauri::command]
pub fn app_version(home: State<TesseractHome>) -> Result<String, String> {
    repo::head_short(&app_dir(&home.0))
}

#[cfg(test)]
mod tests {
    use super::*;
    use git2::{Repository, RepositoryInitOptions, Signature};
    use std::cell::Cell;
    use std::path::Path;
    use std::sync::atomic::AtomicU64;
    use std::time::{SystemTime, UNIX_EPOCH};

    /// Removes its directory on drop so offline test repos never linger in
    /// the temp dir — same pattern as `repo.rs`'s and `provision.rs`'s tests.
    struct TempDir(std::path::PathBuf);

    impl TempDir {
        fn new(label: &str) -> Self {
            static COUNTER: AtomicU64 = AtomicU64::new(0);
            let nanos = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos();
            let n = COUNTER.fetch_add(1, Ordering::SeqCst);
            let dir =
                std::env::temp_dir().join(format!("tesseract-update-test-{label}-{nanos}-{n}"));
            std::fs::create_dir_all(&dir).unwrap();
            TempDir(dir)
        }

        fn join(&self, sub: &str) -> std::path::PathBuf {
            self.0.join(sub)
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn configure_identity(repo: &Repository) {
        let mut config = repo.config().unwrap();
        config.set_str("user.name", "John Doe").unwrap();
        config
            .set_str("user.email", "john.doe@example.com")
            .unwrap();
    }

    fn write_commit(repo: &Repository, filename: &str, content: &str, message: &str) {
        let workdir = repo.workdir().unwrap().to_path_buf();
        std::fs::create_dir_all(workdir.join(filename).parent().unwrap()).unwrap();
        std::fs::write(workdir.join(filename), content).unwrap();
        let mut index = repo.index().unwrap();
        index.add_path(Path::new(filename)).unwrap();
        index.write().unwrap();
        let tree_id = index.write_tree().unwrap();
        let tree = repo.find_tree(tree_id).unwrap();
        let sig: Signature = repo.signature().unwrap();
        let parents: Vec<_> = match repo.head() {
            Ok(head) => vec![head.peel_to_commit().unwrap()],
            Err(_) => vec![],
        };
        let parent_refs: Vec<&git2::Commit> = parents.iter().collect();
        repo.commit(Some("HEAD"), &sig, &sig, message, &tree, &parent_refs)
            .unwrap();
    }

    /// Clones a fresh origin (one commit, `tesseract/pyproject.toml` +
    /// `tesseract/other.py` present) into `dest` and fetches so
    /// `repo::fast_forward` has `refs/remotes/origin/main` to read, mirroring
    /// how `update_check`'s `check_behind` call always precedes
    /// `update_apply` in production.
    fn make_origin_and_synced_clone(base: &TempDir) -> (std::path::PathBuf, std::path::PathBuf) {
        let origin_path = base.join("origin");
        std::fs::create_dir_all(&origin_path).unwrap();
        let mut opts = RepositoryInitOptions::new();
        opts.initial_head("main");
        let origin = Repository::init_opts(&origin_path, &opts).unwrap();
        configure_identity(&origin);
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
        let sha = diff_and_reinstall(&dest, || {
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
        let sha = diff_and_reinstall(&dest, || {
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
        let result = diff_and_reinstall(&dest, || {
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

        let result = diff_and_reinstall(&dest, || Err("uv pip install failed".to_string()));

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
}
