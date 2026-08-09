//! The first-run setup form: staging its answers, and starting provisioning.
//!
//! The form asks four things the operator should not have to discover in a
//! config file afterwards: what to call each other (including both halves of
//! the wake phrase), whether the agent speaks and with which engine, and
//! whether it listens. It runs BEFORE provisioning
//! because those answers decide what gets downloaded — declining speech is
//! only meaningful if it happens before the 1.6 GB model would have been
//! fetched.
//!
//! The shell cannot write the config directly: `home/config/` is seeded from
//! templates that live in the source tree, and the source tree does not exist
//! until the clone stage has run. So the answers are staged here as JSON and
//! `scripts/apply_first_run_setup.py` applies them once `tesseract` is
//! importable, before the fetch stages that read from that config.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, State};

use crate::{shell_log, TesseractHome};

/// One completed setup form.
///
/// `tts` is `"kokoro" | "piper" | "none"` and `gender` filters the voice list
/// only — neither is validated into an enum here. The Python side reads both
/// against the live catalog, which is the only place that knows which voices
/// actually exist; a Rust enum would be a second, staler copy of that list.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct SetupAnswers {
    #[serde(default)]
    pub operator_name: String,
    #[serde(default)]
    pub agent_name: String,
    /// The first word of the wake phrase, which is always two words:
    /// `<prefix> <name>`. Asked here because a phrase whose first half the
    /// operator never chose is not really theirs — "hey" is only a default
    /// until someone says "ciao".
    #[serde(default)]
    pub wake_prefix: String,
    #[serde(default)]
    pub gender: String,
    #[serde(default)]
    pub tts: String,
    #[serde(default)]
    pub stt: bool,
}

/// Where the answers wait for the Python side.
///
/// Under `runtime/` rather than `home/`: this describes what happened on THIS
/// machine's install and must not travel with `home/` to another PC.
pub fn answers_path(home: &Path) -> PathBuf {
    crate::provision::runtime_dir(home).join("first-run-setup.json")
}

pub fn save_answers(home: &Path, answers: &SetupAnswers) -> std::io::Result<()> {
    let runtime = crate::provision::runtime_dir(home);
    std::fs::create_dir_all(&runtime)?;
    let body = serde_json::to_string_pretty(answers)
        .map_err(|e| std::io::Error::other(e.to_string()))?;
    std::fs::write(answers_path(home), body)
}

/// Invoked by the splash screen's setup form. Fire-and-forget, like the token
/// prompt: provisioning runs on a background thread and reports back through
/// `provision-progress` / `provision-needs-token` events, so the webview IPC
/// call returns immediately rather than blocking for the several minutes a
/// clone plus dependency install takes.
#[tauri::command]
pub fn submit_first_run_setup(
    app: AppHandle,
    home: State<TesseractHome>,
    answers: SetupAnswers,
) {
    let home_path = home.0.clone();
    // Logged without the names: they are the operator's, and shell.log is the
    // first thing attached to a bug report.
    shell_log::log(&format!(
        "first-run setup submitted (tts={}, stt={}) — starting provisioning",
        answers.tts, answers.stt
    ));
    if let Err(e) = save_answers(&home_path, &answers) {
        // Not fatal: provisioning still produces a working install on the
        // shipped defaults, and every one of these answers can be changed
        // later in the Identity tab. Stopping here would trade a wrong name
        // for no app at all.
        shell_log::log_error(&format!(
            "could not stage first-run setup answers: {e} — continuing with shipped defaults"
        ));
    }
    start_provisioning(app, home_path);
}

/// Runs `provision()` on a background thread and routes its outcome.
///
/// Shared by the setup-form submit and by the fallback path that skips the
/// form entirely, so both reach provisioning through exactly one code path.
pub fn start_provisioning(app: AppHandle, home: PathBuf) {
    std::thread::spawn(move || {
        // On a clone auth failure with no working token, `provision` returns
        // `NeedsToken` instead of failing outright — `handle_provision_result`
        // shows the in-app prompt rather than leaving the splash on a dead end.
        let result = crate::provision::provision(&app, &home);
        crate::token::handle_provision_result(&app, &home, result);
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::test_support::TempDir;

    fn answers() -> SetupAnswers {
        SetupAnswers {
            operator_name: "Jane Doe".to_string(),
            agent_name: "Ada".to_string(),
            wake_prefix: "ciao".to_string(),
            gender: "female".to_string(),
            tts: "kokoro".to_string(),
            stt: true,
        }
    }

    #[test]
    fn save_answers_creates_runtime_dir_and_writes_readable_json() {
        let base = TempDir::new("setup-save");

        save_answers(base.path(), &answers()).expect("save should succeed");

        let text = std::fs::read_to_string(answers_path(base.path())).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&text).unwrap();
        assert_eq!(parsed["agent_name"], "Ada");
        assert_eq!(parsed["operator_name"], "Jane Doe");
        assert_eq!(parsed["tts"], "kokoro");
        assert_eq!(parsed["stt"], true);
        assert_eq!(parsed["gender"], "female");
    }

    #[test]
    fn save_answers_replaces_a_previous_submission() {
        let base = TempDir::new("setup-resubmit");
        save_answers(base.path(), &answers()).expect("first save");

        let mut second = answers();
        second.tts = "none".to_string();
        second.stt = false;
        save_answers(base.path(), &second).expect("second save");

        let text = std::fs::read_to_string(answers_path(base.path())).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&text).unwrap();
        assert_eq!(
            parsed["tts"], "none",
            "a resubmission must replace the staged answers, not append to them"
        );
        assert_eq!(parsed["stt"], false);
    }

    /// The form is the operator's first interaction with the app, and a
    /// half-filled one must still stage cleanly — the Python side treats a
    /// blank name as "keep the shipped default" rather than writing an empty
    /// one, so nothing here needs to reject it.
    #[test]
    fn save_answers_accepts_empty_fields() {
        let base = TempDir::new("setup-empty");
        let empty = SetupAnswers {
            operator_name: String::new(),
            agent_name: String::new(),
            wake_prefix: String::new(),
            gender: String::new(),
            tts: "none".to_string(),
            stt: false,
        };

        save_answers(base.path(), &empty).expect("an empty form must still stage");

        let text = std::fs::read_to_string(answers_path(base.path())).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&text).unwrap();
        assert_eq!(parsed["agent_name"], "");
    }

    /// `answers_path` must sit under `runtime/`, not `home/`: the operator's
    /// data-sync repo copies `home/` between machines, and a setup record
    /// following it would describe the wrong install.
    #[test]
    fn answers_are_staged_under_runtime_not_home() {
        let base = TempDir::new("setup-location");
        let path = answers_path(base.path());
        assert!(path.starts_with(base.join("runtime")));
        assert!(!path.starts_with(base.join("home")));
    }
}
