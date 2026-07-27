use std::path::{Path, PathBuf};
use tauri::path::BaseDirectory;
use tauri::{AppHandle, Emitter, Manager};

/// Bump when the bundled deps change so an upgraded app re-provisions.
pub const DEPS_VERSION: &str = "3";

/// Per-user state root: %LOCALAPPDATA%\com.tesseract.mirror (writable).
/// Falls back to an explicit TESSERACT_HOME env override (dev), then to the
/// app_local_data_dir. Never returns the read-only resource/install dir.
pub fn tesseract_home(app: &AppHandle) -> PathBuf {
    if let Ok(explicit) = std::env::var("TESSERACT_HOME") {
        return PathBuf::from(explicit);
    }
    app.path()
        .app_local_data_dir()
        .expect("no app_local_data_dir available")
}

/// The provisioned venv interpreter under the per-user home (Windows layout).
pub fn venv_python(home: &Path) -> PathBuf {
    home.join("venv").join("Scripts").join("python.exe")
}

/// True iff the provisioning marker exists and its deps_version matches.
pub fn is_provisioned(home: &Path) -> bool {
    let marker = home.join("runtime").join("provisioned.json");
    let Ok(text) = std::fs::read_to_string(&marker) else {
        return false;
    };
    match serde_json::from_str::<serde_json::Value>(&text) {
        Ok(v) => v.get("deps_version").and_then(|d| d.as_str()) == Some(DEPS_VERSION),
        Err(_) => false,
    }
}

/// Clone the source repo into the per-user home, download Python + deps
/// online, editable-install tesseract, and fetch the browser engine. On
/// success writes the marker and returns the venv python path. Emits
/// "provision-progress" events (payload: a String line).
pub fn provision(app: &AppHandle, home: &Path) -> Result<PathBuf, String> {
    let res = |rel: &str| {
        app.path()
            .resolve(rel, BaseDirectory::Resource)
            .map_err(|e| format!("resource {rel}: {e}"))
    };

    let uv = res("resources/binaries/uv.exe")?;
    let venv = home.join("venv");
    let app_dir = home.join("app");

    let _ = app.emit("provision-progress", "Downloading TESSERACT…".to_string());
    clone_app_dir(&app_dir, home)?;

    let _ = app.emit("provision-progress", "Downloading Python…");
    run_uv(&uv, &["python", "install", "3.12"])?;

    let _ = app.emit("provision-progress", "Creating Python environment…");
    run_uv(
        &uv,
        &[
            "venv",
            "--clear",
            "--python",
            "3.12",
            &venv.to_string_lossy(),
        ],
    )?;

    let _ = app.emit("provision-progress", "Downloading dependencies…");
    reinstall_deps(app, home)?;

    let _ = app.emit("provision-progress", "Downloading browser engine…");
    run_python(
        &venv_python(home),
        &["-m", "playwright", "install", "chromium"],
    )?;

    // Marker last, so a partial provision never reads as complete.
    let runtime = home.join("runtime");
    std::fs::create_dir_all(&runtime).map_err(|e| e.to_string())?;
    let marker = runtime.join("provisioned.json");
    let body = serde_json::json!({ "deps_version": DEPS_VERSION });
    std::fs::write(&marker, body.to_string()).map_err(|e| e.to_string())?;

    let _ = app.emit("provision-progress", "Ready.");
    Ok(venv_python(home))
}

/// Editable install: pulls tesseract's CPU-only core deps from
/// pyproject.toml (CUDA is an opt-in extra) and drops a `.pth` so `import
/// tesseract` works without cwd/PYTHONPATH wiring. pyproject.toml lives
/// inside the package dir (`app/tesseract/`); package-dir puts `app/` on
/// sys.path. Shared by `provision()` (first run) and Task 12's `update_apply`
/// (re-run only when `pyproject.toml` changed across an update).
pub fn reinstall_deps(app: &AppHandle, home: &Path) -> Result<(), String> {
    let uv = app
        .path()
        .resolve("resources/binaries/uv.exe", BaseDirectory::Resource)
        .map_err(|e| format!("resource resources/binaries/uv.exe: {e}"))?;
    let pkg = home.join("app").join("tesseract");
    run_uv(
        &uv,
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
fn clone_app_dir(app_dir: &Path, home: &Path) -> Result<(), String> {
    clone_app_dir_with(app_dir, home, &crate::repo::repo_url())
}

/// `clone_app_dir` with the repo URL passed explicitly, so tests can drive
/// the real guard/clear/clone/error-mapping logic against a local throwaway
/// repo without mutating the process-global `TESSERACT_REPO_URL` env var
/// (which `repo::tests` also exercises and would race with under parallel
/// `cargo test`).
fn clone_app_dir_with(app_dir: &Path, home: &Path, url: &str) -> Result<(), String> {
    if app_dir.join(".git").exists() {
        return Ok(());
    }
    if app_dir.exists() {
        std::fs::remove_dir_all(app_dir).map_err(|e| fs_error_message(&e.to_string()))?;
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
        std::fs::remove_dir_all(&staging).map_err(|e| fs_error_message(&e.to_string()))?;
    }
    let token = crate::repo::github_token(home);
    if let Err(e) = crate::repo::clone(url, &staging, token) {
        let _ = std::fs::remove_dir_all(&staging);
        return Err(clone_error_message(&e, home));
    }
    finalize_clone(&staging, app_dir)
}

/// Renames the completed staging clone into place. Its own function so tests
/// can force a rename failure (e.g. destination path occupied) without
/// fighting the guard/clear logic above.
fn finalize_clone(staging: &Path, app_dir: &Path) -> Result<(), String> {
    std::fs::rename(staging, app_dir).map_err(|e| fs_error_message(&e.to_string()))
}

fn run_uv(uv: &Path, args: &[&str]) -> Result<(), String> {
    let mut cmd = std::process::Command::new(uv);
    cmd.args(args);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    let out = cmd.output().map_err(|e| format!("uv spawn failed: {e}"))?;
    if !out.status.success() {
        return Err(format!(
            "uv {:?} failed: {}",
            args,
            String::from_utf8_lossy(&out.stderr)
        ));
    }
    Ok(())
}

fn run_python(py: &Path, args: &[&str]) -> Result<(), String> {
    let mut cmd = std::process::Command::new(py);
    cmd.args(args);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    let out = cmd
        .output()
        .map_err(|e| format!("python spawn failed: {e}"))?;
    if !out.status.success() {
        return Err(format!(
            "python {:?} failed: {}",
            args,
            String::from_utf8_lossy(&out.stderr)
        ));
    }
    Ok(())
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

/// Turns a raw `repo::clone` error (which may embed a git2/libcurl message)
/// into an actionable, credential-free string for the splash screen.
fn clone_error_message(raw: &str, home: &Path) -> String {
    let scrubbed = scrub_credentials(raw);
    let lower = scrubbed.to_lowercase();
    if lower.contains("401")
        || lower.contains("403")
        || lower.contains("404")
        || lower.contains("authentication")
        || lower.contains("not found")
    {
        format!(
            "could not access the TESSERACT repository — if it's private, add a GitHub token at {} and restart",
            home.join("runtime").join("github_token").display()
        )
    } else if lower.contains("resolve host")
        || lower.contains("resolve address")
        || lower.contains("could not connect")
        || lower.contains("network")
        || lower.contains("timed out")
        || lower.contains("timeout")
    {
        "could not reach GitHub — check your internet connection and try again".to_string()
    } else if lower.contains("no space left") || lower.contains("not enough space") {
        "not enough disk space to download TESSERACT — free up space and try again".to_string()
    } else {
        format!("could not download TESSERACT ({scrubbed}) — check your connection and try again")
    }
}

/// Turns a raw local-filesystem error (from clearing `app_dir`/staging or
/// finalizing the clone via rename) into an actionable, credential-free
/// string — the local-filesystem counterpart to `clone_error_message`. These
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
    } else if lower.contains("no space left") || lower.contains("not enough space") {
        "not enough disk space to download TESSERACT — free up space and try again".to_string()
    } else {
        format!("could not prepare the download folder ({scrubbed}) — try restarting TESSERACT and try again")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use git2::{Repository, RepositoryInitOptions, Signature};
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::{SystemTime, UNIX_EPOCH};

    /// Removes its directory on drop so offline test repos never linger in the temp dir.
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
                std::env::temp_dir().join(format!("tesseract-provision-test-{label}-{nanos}-{n}"));
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

    /// Builds a throwaway local git repo (the "production repo" stand-in)
    /// with one commit, so `clone_app_dir_with` can clone a real, working
    /// tree without any network access.
    fn make_origin(base: &TempDir) -> std::path::PathBuf {
        let origin_path = base.join("origin");
        std::fs::create_dir_all(&origin_path).unwrap();
        let mut opts = RepositoryInitOptions::new();
        opts.initial_head("main");
        let origin = Repository::init_opts(&origin_path, &opts).unwrap();
        let mut config = origin.config().unwrap();
        config.set_str("user.name", "John Doe").unwrap();
        config
            .set_str("user.email", "john.doe@example.com")
            .unwrap();
        std::fs::write(
            origin_path.join("pyproject.toml"),
            "[project]\nname=\"tesseract\"\n",
        )
        .unwrap();
        let mut index = origin.index().unwrap();
        index.add_path(Path::new("pyproject.toml")).unwrap();
        index.write().unwrap();
        let tree_id = index.write_tree().unwrap();
        let tree = origin.find_tree(tree_id).unwrap();
        let sig: Signature = origin.signature().unwrap();
        origin
            .commit(Some("HEAD"), &sig, &sig, "c1", &tree, &[])
            .unwrap();
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
            .expect_err("cloning a nonexistent origin must fail");
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
            .expect_err("clearing a file where a directory is expected must fail");
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
            .expect_err("clearing a file where the staging directory is expected must fail");
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
    fn clone_error_message_never_leaks_a_token_and_points_at_the_token_file() {
        let home = TempDir::new("err-home");
        let raw = "clone https://x-access-token:ghp_supersecret@github.com/karakijihad/Tesseract.git: unexpected http status code: 401 Unauthorized";
        let msg = clone_error_message(raw, Path::new(&home.0));
        assert!(!msg.contains("ghp_supersecret"));
        assert!(msg.contains("GitHub token"));
        assert!(msg.contains("runtime"));
        assert!(msg.contains("github_token"));
    }

    #[test]
    fn clone_error_message_classifies_network_failures() {
        let home = TempDir::new("err-net");
        let raw = "clone https://github.com/karakijihad/Tesseract.git: failed to resolve host";
        let msg = clone_error_message(raw, Path::new(&home.0));
        assert!(msg.contains("internet connection"));
    }

    #[test]
    fn clone_error_message_classifies_disk_space_failures() {
        let home = TempDir::new("err-disk");
        let raw = "clone https://github.com/karakijihad/Tesseract.git: no space left on device";
        let msg = clone_error_message(raw, Path::new(&home.0));
        assert!(msg.contains("disk space"));
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
}
