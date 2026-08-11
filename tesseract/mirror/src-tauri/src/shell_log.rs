use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};

use crate::provision::scrub_credentials;

/// Matches the Python side's rotation bounds (`tesseract/config/mirror.yaml::logging`,
/// `tesseract/logsetup.py::attach_file_logging`) so the shell's own log grows
/// and rotates the same way the Python process logs already do. Kept in
/// sync manually — same pattern as `provision::DEPS_VERSION` — since the
/// shell has no YAML parser and adding one just to read two constants would
/// be a bigger dependency than the constants themselves.
const MAX_BYTES: u64 = 5 * 1024 * 1024;
const BACKUP_COUNT: u32 = 3;

static LOG_PATH: OnceLock<PathBuf> = OnceLock::new();
static LOG_LOCK: Mutex<()> = Mutex::new(());

/// Installs a panic hook that appends the panic message/location to the
/// shell log (once `init` has pointed it at a file) before running the
/// previous hook, so a panic mid-provisioning or mid-update leaves a
/// durable trace instead of vanishing with the console-less packaged GUI
/// process. Call once, at the very top of `run()` — before `init`, which
/// needs an `AppHandle` and so can only run once Tauri has resolved the
/// per-user home directory. A panic before that point is not captured — the
/// gap is accepted because nothing durable exists yet to write a trace into.
pub fn install_panic_hook() {
    let previous = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        log_line("PANIC", &scrub_credentials(&info.to_string()));
        previous(info);
    }));
}

/// Points the shell log at `<root>/runtime/logs/shell.log`. Call once, as
/// early as possible after the install root is resolved and before
/// provisioning starts — nothing logged before this call is captured (the
/// path isn't known yet).
///
/// Under `runtime/`: this is machine-local ops output, and writing it at the
/// root would recreate a `logs/` directory beside the three siblings on every
/// launch.
pub fn init(root: &Path) {
    let _ = LOG_PATH.set(crate::provision::runtime_dir(root).join("logs").join("shell.log"));
    log("shell log started");
}

/// Appends an INFO line. `msg` is scrubbed for embedded credentials before
/// it touches disk — callers may pass text derived from a git2/libcurl
/// error, which can echo back a remote URL with embedded auth.
pub fn log(msg: &str) {
    log_line("INFO", &scrub_credentials(msg));
}

/// Appends an ERROR line. Same scrubbing as `log`.
pub fn log_error(msg: &str) {
    log_line("ERROR", &scrub_credentials(msg));
}

fn log_line(level: &str, msg: &str) {
    let Some(path) = LOG_PATH.get() else {
        return;
    };
    let _guard = LOG_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    write_line(path, level, msg);
}

fn write_line(path: &Path, level: &str, msg: &str) {
    if let Some(parent) = path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    rotate_if_needed(path);
    let ts = chrono::Local::now().format("%Y-%m-%d %H:%M:%S%.3f");
    // The pid is not decoration. `LOG_LOCK` serialises writers inside ONE
    // process, but a restart overlaps two shells — the new one started 6s
    // before the old one finished on the live install — and they append to
    // the same file holding separate locks. Cross-process locking could wedge
    // a boot on a lock the dying shell never releases, which is a worse
    // failure than the one it fixes. So the interleave is accepted and made
    // READABLE instead: every line names its writer, and the two runs can be
    // separated afterwards. That was the only thing the interleave cost.
    let line = format!("{ts} {level} shell[{}]: {msg}\n", std::process::id());
    if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(path) {
        let _ = f.write_all(line.as_bytes());
    }
}

/// Same rolling scheme as Python's `RotatingFileHandler`: once the current
/// file reaches the size bound, shift `.1..backup_count-1` up by one
/// (oldest dropped first) and move the live file to `.1`. The oldest backup
/// is removed before the shift so every rename lands on a path that has
/// just been freed — `fs::rename` on Windows fails if the destination
/// already exists.
fn rotate_if_needed(path: &Path) {
    rotate_if_needed_with(path, MAX_BYTES, BACKUP_COUNT);
}

fn rotate_if_needed_with(path: &Path, max_bytes: u64, backup_count: u32) {
    let size = fs::metadata(path).map(|m| m.len()).unwrap_or(0);
    if size < max_bytes {
        return;
    }
    let _ = fs::remove_file(backup_path(path, backup_count));
    for i in (1..backup_count).rev() {
        let src = backup_path(path, i);
        if src.exists() {
            let _ = fs::rename(&src, backup_path(path, i + 1));
        }
    }
    let _ = fs::rename(path, backup_path(path, 1));
}

fn backup_path(path: &Path, n: u32) -> PathBuf {
    let mut name = path.as_os_str().to_owned();
    name.push(format!(".{n}"));
    PathBuf::from(name)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::test_support::TempDir;

    #[test]
    fn write_line_creates_missing_parent_dirs_and_appends_level_and_message() {
        let base = TempDir::new("basic");
        let path = base.join("logs").join("shell.log");

        write_line(&path, "INFO", "hello world");

        let contents = fs::read_to_string(&path).unwrap();
        assert!(contents.contains("INFO"));
        assert!(contents.contains("hello world"));
        // Every line names the process that wrote it, so an overlapping
        // restart leaves two separable streams instead of one blended log.
        assert!(contents.contains(&format!("shell[{}]:", std::process::id())));
    }

    #[test]
    fn write_line_appends_rather_than_truncates() {
        let base = TempDir::new("append");
        let path = base.join("shell.log");

        write_line(&path, "INFO", "first");
        write_line(&path, "INFO", "second");

        let contents = fs::read_to_string(&path).unwrap();
        assert!(contents.contains("first"));
        assert!(contents.contains("second"));
        assert_eq!(contents.lines().count(), 2);
    }

    #[test]
    fn rotate_if_needed_with_is_a_noop_under_the_size_bound() {
        let base = TempDir::new("under-bound");
        let path = base.join("shell.log");
        fs::write(&path, "short").unwrap();

        rotate_if_needed_with(&path, 1024, 3);

        assert!(path.exists());
        assert!(!backup_path(&path, 1).exists());
    }

    #[test]
    fn rotate_if_needed_with_shifts_backups_and_drops_the_oldest() {
        let base = TempDir::new("shift");
        let path = base.join("shell.log");
        fs::write(&path, "live-content-over-bound").unwrap();
        fs::write(backup_path(&path, 1), "backup-1").unwrap();
        fs::write(backup_path(&path, 2), "backup-2").unwrap();

        // backup_count=2 with the live file already over the tiny bound.
        rotate_if_needed_with(&path, 10, 2);

        assert!(!path.exists(), "the live file must have been rotated away");
        assert_eq!(
            fs::read_to_string(backup_path(&path, 1)).unwrap(),
            "live-content-over-bound",
            "the live file becomes .1"
        );
        assert_eq!(
            fs::read_to_string(backup_path(&path, 2)).unwrap(),
            "backup-1",
            "old .1 shifts to .2, dropping the old .2"
        );
    }

    #[test]
    fn rotate_if_needed_with_survives_no_existing_backups() {
        let base = TempDir::new("first-rotation");
        let path = base.join("shell.log");
        fs::write(&path, "over-the-tiny-bound").unwrap();

        rotate_if_needed_with(&path, 5, 3);

        assert!(!path.exists());
        assert_eq!(
            fs::read_to_string(backup_path(&path, 1)).unwrap(),
            "over-the-tiny-bound"
        );
    }
}
