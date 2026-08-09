//! Updating `app/` by building the new revision beside it and renaming.
//!
//! The old path ran `checkout_head(force)` against the live tree, so a death
//! mid-checkout left a half-updated install with no way to tell that had
//! happened. It was also the only reason the `app/` seal needed an updater
//! exemption at all.
//!
//! Here the working tree is only ever swapped by rename:
//!
//! 1. copy `app` → `app.next`
//! 2. advance `app.next` to the target revision (the git work, off to one side)
//! 3. rename `app` → `app.old`
//! 4. rename `app.next` → `app`
//! 5. delete `app.old`
//!
//! Every step is recoverable. A crash before 3 leaves `app` untouched and a
//! stale `app.next` the next run clears. A crash after 4 leaves a stale
//! `app.old`, likewise cleared. The one window where `app` does not exist is
//! between 3 and 4 — two renames on the same volume, microseconds apart — and
//! [`recover_interrupted`] closes it by putting `app.old` back.
//!
//! **Dependencies install after the swap, never before.** `reinstall_deps`
//! runs `uv pip install -e <app_dir>/tesseract`, which records an ABSOLUTE
//! path into the venv; installing against `app.next` would bake a directory
//! that is about to be renamed away. The venv itself lives outside `app/`,
//! which is what makes the rename safe in the first place.
//!
//! The payoff is that rolling back no longer reinstalls anything. The
//! previous tree still exists as `app.old` and the installed dependencies
//! already match it, so recovery is the reverse rename.

use std::path::{Path, PathBuf};

use crate::provision::remove_tree;

/// Sibling of `dir` with `suffix` appended to its final component.
fn sibling(dir: &Path, suffix: &str) -> PathBuf {
    let name = dir
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("app")
        .to_string();
    dir.with_file_name(format!("{name}{suffix}"))
}

pub fn staging_dir(dir: &Path) -> PathBuf {
    sibling(dir, ".next")
}

pub fn previous_dir(dir: &Path) -> PathBuf {
    sibling(dir, ".old")
}

/// Put `app` back if an update died between the two renames.
///
/// Called before anything else touches the tree. The check is deliberately
/// narrow — `app` absent AND `app.old` present — because that pair can only
/// be produced by an interrupted swap. Any other combination is either
/// normal or a problem this must not paper over.
///
/// Returns `Ok(true)` when it actually restored something.
pub fn recover_interrupted(dir: &Path) -> Result<bool, String> {
    let previous = previous_dir(dir);
    if dir.exists() || !previous.exists() {
        return Ok(false);
    }
    std::fs::rename(&previous, dir).map_err(|e| {
        format!(
            "an update was interrupted and {} could not be restored from {}: {e}",
            dir.display(),
            previous.display()
        )
    })?;
    Ok(true)
}

/// Delete staging and previous trees left by an earlier run.
///
/// Failure is not fatal: a leftover directory costs disk, not correctness,
/// and refusing to update because a stale copy could not be deleted would
/// turn a cosmetic problem into a stuck install.
fn clear_leftovers(dir: &Path) {
    for path in [staging_dir(dir), previous_dir(dir)] {
        if path.exists() {
            let _ = remove_tree(&path);
        }
    }
}

fn copy_tree(source: &Path, destination: &Path) -> std::io::Result<()> {
    std::fs::create_dir_all(destination)?;
    for entry in std::fs::read_dir(source)? {
        let entry = entry?;
        let from = entry.path();
        let to = destination.join(entry.file_name());
        // Symlinks are not followed: copying through one would pull a tree
        // from outside `app/` into what becomes the sealed install.
        let meta = std::fs::symlink_metadata(&from)?;
        if meta.file_type().is_symlink() {
            continue;
        }
        if meta.is_dir() {
            copy_tree(&from, &to)?;
        } else {
            std::fs::copy(&from, &to)?;
        }
    }
    Ok(())
}

/// Advance `dir` to a new revision without ever mutating it in place.
///
/// `advance` is the git move (`repo::fast_forward` or `repo::reset_to_remote`)
/// and runs against the STAGING copy, so a refusal or a mid-checkout death
/// leaves the live tree untouched.
pub fn advance_by_swap(
    dir: &Path,
    advance: impl FnOnce(&Path) -> Result<String, String>,
) -> Result<String, String> {
    clear_leftovers(dir);

    let staging = staging_dir(dir);
    let previous = previous_dir(dir);

    copy_tree(dir, &staging).map_err(|e| {
        let _ = remove_tree(&staging);
        format!("could not stage a copy of the app tree: {e}")
    })?;

    let sha = match advance(&staging) {
        Ok(sha) => sha,
        Err(e) => {
            // The live tree never moved, so this is the cheap failure: drop
            // the staging copy and report. The caller respawns on the
            // version that is still installed.
            let _ = remove_tree(&staging);
            return Err(e);
        }
    };

    std::fs::rename(dir, &previous).map_err(|e| {
        let _ = remove_tree(&staging);
        format!("could not move the current app tree aside: {e}")
    })?;

    if let Err(e) = std::fs::rename(&staging, dir) {
        // The only genuinely dangerous window. Put it back immediately
        // rather than leaving the install without an `app/`.
        if let Err(restore) = std::fs::rename(&previous, dir) {
            return Err(format!(
                "could not install the updated app tree ({e}), and restoring the \
                 previous one also failed ({restore}) — TESSERACT will repair \
                 itself on the next launch"
            ));
        }
        let _ = remove_tree(&staging);
        return Err(format!("could not install the updated app tree: {e}"));
    }

    Ok(sha)
}

/// Undo a completed swap by renaming the previous tree back.
///
/// This is the whole rollback: no reinstall, because the dependencies still
/// installed are the ones `app.old` was built against. Only valid while
/// `app.old` is still present — that is, before [`commit_swap`].
pub fn rollback_swap(dir: &Path) -> Result<(), String> {
    let previous = previous_dir(dir);
    if !previous.exists() {
        return Err("the previous app tree is no longer available".to_string());
    }
    let discarded = staging_dir(dir);
    let _ = remove_tree(&discarded);
    std::fs::rename(dir, &discarded)
        .map_err(|e| format!("could not move the updated app tree aside: {e}"))?;
    std::fs::rename(&previous, dir).map_err(|e| {
        format!("could not restore the previous app tree: {e}")
    })?;
    let _ = remove_tree(&discarded);
    Ok(())
}

/// Drop the previous tree once the update is known good.
pub fn commit_swap(dir: &Path) {
    let previous = previous_dir(dir);
    if previous.exists() {
        let _ = remove_tree(&previous);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tree(root: &Path, marker: &str) {
        std::fs::create_dir_all(root.join("tesseract")).unwrap();
        std::fs::write(root.join("tesseract").join("pyproject.toml"), marker).unwrap();
        std::fs::create_dir_all(root.join(".git")).unwrap();
        std::fs::write(root.join(".git").join("HEAD"), marker).unwrap();
    }

    fn marker(root: &Path) -> String {
        std::fs::read_to_string(root.join("tesseract").join("pyproject.toml")).unwrap()
    }

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("app-swap-{name}-{}", std::process::id()));
        let _ = remove_tree(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn advance_installs_the_staged_tree_and_keeps_the_previous_one() {
        let root = scratch("installs");
        let app = root.join("app");
        tree(&app, "before");

        let sha = advance_by_swap(&app, |staging| {
            // Whatever the git step does, it does to the staging copy.
            std::fs::write(staging.join("tesseract").join("pyproject.toml"), "after").unwrap();
            Ok("abc12345".to_string())
        })
        .unwrap();

        assert_eq!(sha, "abc12345");
        assert_eq!(marker(&app), "after");
        // The previous tree survives until the update is known good.
        assert_eq!(marker(&previous_dir(&app)), "before");
        let _ = remove_tree(&root);
    }

    #[test]
    fn a_failed_advance_leaves_the_live_tree_untouched() {
        let root = scratch("failed-advance");
        let app = root.join("app");
        tree(&app, "before");

        let err = advance_by_swap(&app, |staging| {
            // Mutate the staging copy, THEN fail — the shape a mid-checkout
            // death takes. None of it may reach `app/`.
            std::fs::write(staging.join("tesseract").join("pyproject.toml"), "half").unwrap();
            Err("non-fast-forward".to_string())
        })
        .unwrap_err();

        assert_eq!(err, "non-fast-forward");
        assert_eq!(marker(&app), "before");
        assert!(!staging_dir(&app).exists(), "staging copy is cleaned up");
        assert!(!previous_dir(&app).exists(), "nothing was moved aside");
        let _ = remove_tree(&root);
    }

    #[test]
    fn rollback_restores_the_previous_tree_without_reinstalling() {
        let root = scratch("rollback");
        let app = root.join("app");
        tree(&app, "before");

        advance_by_swap(&app, |staging| {
            std::fs::write(staging.join("tesseract").join("pyproject.toml"), "after").unwrap();
            Ok("abc12345".to_string())
        })
        .unwrap();
        assert_eq!(marker(&app), "after");

        rollback_swap(&app).unwrap();

        assert_eq!(marker(&app), "before");
        assert!(!previous_dir(&app).exists());
        let _ = remove_tree(&root);
    }

    #[test]
    fn rollback_refuses_once_the_swap_is_committed() {
        let root = scratch("rollback-after-commit");
        let app = root.join("app");
        tree(&app, "before");

        advance_by_swap(&app, |staging| {
            std::fs::write(staging.join("tesseract").join("pyproject.toml"), "after").unwrap();
            Ok("abc12345".to_string())
        })
        .unwrap();
        commit_swap(&app);

        assert!(!previous_dir(&app).exists());
        assert!(rollback_swap(&app).is_err());
        assert_eq!(marker(&app), "after", "the committed tree stays put");
        let _ = remove_tree(&root);
    }

    #[test]
    fn recover_restores_app_after_a_death_between_the_two_renames() {
        let root = scratch("recover");
        let app = root.join("app");
        tree(&app, "before");

        // Exactly the state a crash between rename 1 and rename 2 leaves:
        // no `app/`, the previous tree parked, the new one still staged.
        tree(&staging_dir(&app), "after");
        std::fs::rename(&app, previous_dir(&app)).unwrap();
        assert!(!app.exists());

        assert!(recover_interrupted(&app).unwrap());

        assert_eq!(marker(&app), "before", "the install comes back, not half of it");
        assert!(!previous_dir(&app).exists());
        let _ = remove_tree(&root);
    }

    #[test]
    fn recover_is_a_no_op_when_the_install_is_intact() {
        let root = scratch("recover-noop");
        let app = root.join("app");
        tree(&app, "before");
        // A stale `app.old` alone is not an interrupted swap — it is the
        // residue of a successful one, and `app/` must win.
        tree(&previous_dir(&app), "stale");

        assert!(!recover_interrupted(&app).unwrap());

        assert_eq!(marker(&app), "before");
        let _ = remove_tree(&root);
    }

    #[test]
    fn a_stale_staging_tree_does_not_poison_the_next_update() {
        let root = scratch("stale-staging");
        let app = root.join("app");
        tree(&app, "before");
        tree(&staging_dir(&app), "abandoned");

        advance_by_swap(&app, |staging| {
            assert_eq!(
                marker(staging),
                "before",
                "staging must be a fresh copy of the live tree, not the abandoned one"
            );
            std::fs::write(staging.join("tesseract").join("pyproject.toml"), "after").unwrap();
            Ok("abc12345".to_string())
        })
        .unwrap();

        assert_eq!(marker(&app), "after");
        let _ = remove_tree(&root);
    }

    #[test]
    fn commit_drops_the_previous_tree() {
        let root = scratch("commit");
        let app = root.join("app");
        tree(&app, "before");
        tree(&previous_dir(&app), "before");

        commit_swap(&app);

        assert!(!previous_dir(&app).exists());
        assert!(app.exists());
        let _ = remove_tree(&root);
    }
}
