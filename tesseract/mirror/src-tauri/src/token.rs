use std::path::{Path, PathBuf};

use tauri::{AppHandle, Emitter, State};

use crate::provision::{self, ProvisionError};
use crate::{shell_log, TesseractHome};

/// Strips surrounding whitespace and one layer of matching quotes (people
/// commonly paste a token wrapped in `"..."` or `'...'`), then rejects an
/// empty result. Deliberately does NOT validate the token's *content*
/// against a format pattern — GitHub's token formats change over time, and
/// a false rejection is worse than a failed clone with a clear message.
fn clean_pasted_token(raw: &str) -> Option<String> {
    let trimmed = raw.trim();
    let unquoted = trimmed
        .strip_prefix('"')
        .and_then(|s| s.strip_suffix('"'))
        .or_else(|| {
            trimmed
                .strip_prefix('\'')
                .and_then(|s| s.strip_suffix('\''))
        })
        .unwrap_or(trimmed)
        .trim();
    if unquoted.is_empty() {
        None
    } else {
        Some(unquoted.to_string())
    }
}

/// Creates `<home>/runtime/` (the folder a friend previously had to make by
/// hand) and writes the cleaned token into it — the same path
/// `repo::github_token` already reads. Never logs or returns the token
/// itself; only `io::Error`s propagate, which carry OS error text, not
/// secret content. Relies on `%LOCALAPPDATA%`'s default per-user NTFS ACL
/// as the confidentiality boundary — the same trust boundary the sibling
/// `.env` file (which already holds live API keys in this same directory)
/// relies on; no additional ACL-narrowing is layered on top of that
/// existing boundary.
fn save_github_token(home: &Path, token: &str) -> std::io::Result<()> {
    let runtime = crate::provision::runtime_dir(home);
    std::fs::create_dir_all(&runtime)?;
    std::fs::write(runtime.join("github_token"), token)
}

/// Shared by the first provisioning attempt (`lib.rs`'s setup thread) and
/// every retry after a token is saved: turns a `provision()` outcome into
/// the right window/event transition, so the match isn't duplicated at both
/// call sites.
pub(crate) fn handle_provision_result(
    app: &AppHandle,
    home: &PathBuf,
    result: Result<PathBuf, ProvisionError>,
) {
    match result {
        Ok(_) => crate::finish_provisioning_success(app, home),
        Err(ProvisionError::NeedsToken(msg)) => {
            shell_log::log(&format!("clone requires a GitHub token: {msg}"));
            let _ = app.emit("provision-needs-token", msg);
        }
        Err(ProvisionError::Other(msg)) => {
            // Scrubbed here rather than trusting the producer: clone-stage
            // errors arrive pre-scrubbed via `classify_clone_error`, but every
            // other stage forwards raw `uv`/`python` stderr, which can embed a
            // credentialed URL from an index or proxy env var. This is the last
            // point before the text reaches the log file and the screen.
            let msg = provision::scrub_credentials(&msg);
            shell_log::log_error(&format!("provisioning failed: {msg}"));
            let _ = app.emit("provision-progress", format!("Setup failed: {msg}"));
        }
    }
}

/// Invoked by the splash screen's token form. Fire-and-forget from the
/// caller's perspective, like the first provisioning attempt: the actual
/// retry runs on a background thread and reports back purely through
/// `provision-progress`/`provision-needs-token` events, so the webview IPC
/// call itself returns immediately rather than blocking on a clone + full
/// re-provision (which can take minutes).
#[tauri::command]
pub fn submit_github_token(app: AppHandle, home: State<TesseractHome>, token: String) {
    let Some(cleaned) = clean_pasted_token(&token) else {
        let _ = app.emit(
            "provision-needs-token",
            "That didn't look like a token — paste it again.".to_string(),
        );
        return;
    };
    let home_path = home.0.clone();
    std::thread::spawn(move || {
        if let Err(e) = save_github_token(&home_path, &cleaned) {
            shell_log::log_error(&format!("failed to save GitHub token: {e}"));
            let _ = app.emit(
                "provision-needs-token",
                "Could not save the token to disk — try again.".to_string(),
            );
            return;
        }
        shell_log::log("github token saved; retrying provisioning");
        let result = provision::provision(&app, &home_path);
        handle_provision_result(&app, &home_path, result);
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::test_support::TempDir;

    #[test]
    fn clean_pasted_token_trims_surrounding_whitespace() {
        assert_eq!(
            clean_pasted_token("  ghp_abc123  "),
            Some("ghp_abc123".to_string())
        );
    }

    #[test]
    fn clean_pasted_token_strips_matching_double_quotes() {
        assert_eq!(
            clean_pasted_token("\"ghp_abc123\""),
            Some("ghp_abc123".to_string())
        );
    }

    #[test]
    fn clean_pasted_token_strips_matching_single_quotes() {
        assert_eq!(
            clean_pasted_token("'ghp_abc123'"),
            Some("ghp_abc123".to_string())
        );
    }

    #[test]
    fn clean_pasted_token_does_not_strip_a_lone_unmatched_quote() {
        // A single leading quote with no matching trailing quote is not a
        // wrapped paste — stripping it would silently mangle the token.
        assert_eq!(
            clean_pasted_token("\"ghp_abc123"),
            Some("\"ghp_abc123".to_string())
        );
    }

    #[test]
    fn clean_pasted_token_rejects_empty_and_whitespace_only_input() {
        assert_eq!(clean_pasted_token(""), None);
        assert_eq!(clean_pasted_token("   "), None);
        assert_eq!(clean_pasted_token("\"\""), None);
        assert_eq!(clean_pasted_token("  \t\n  "), None);
    }

    #[test]
    fn clean_pasted_token_never_validates_content_shape() {
        // Deliberately no format validation — GitHub token shapes change,
        // and a false rejection is worse than a failed clone with a clear
        // message. Anything non-empty after trimming/unquoting passes.
        assert_eq!(
            clean_pasted_token("not-a-real-token-shape-at-all"),
            Some("not-a-real-token-shape-at-all".to_string())
        );
    }

    #[test]
    fn save_github_token_creates_runtime_dir_and_writes_the_token_file() {
        let base = TempDir::new("save");

        save_github_token(base.path(), "secret-token-value").expect("save should succeed");

        let contents = std::fs::read_to_string(base.join("runtime").join("github_token")).unwrap();
        assert_eq!(contents, "secret-token-value");
    }

    #[test]
    fn save_github_token_overwrites_a_stale_previously_saved_token() {
        let base = TempDir::new("overwrite");

        save_github_token(base.path(), "old-token").expect("first save should succeed");
        save_github_token(base.path(), "new-token").expect("second save should succeed");

        let contents = std::fs::read_to_string(base.join("runtime").join("github_token")).unwrap();
        assert_eq!(
            contents, "new-token",
            "a retry with a corrected token must replace the stale one, not append to it"
        );
    }
}
