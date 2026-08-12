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

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Emitter, State};

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
    /// Provider API keys, `ENV_NAME -> value`, every one of them optional.
    ///
    /// Staged rather than written straight to `.env`: that file does not
    /// exist yet — `config_seed.ensure_env_seeded` creates it from the
    /// shipped template, and the template is inside the source tree the
    /// clone stage has not fetched. Writing a bare `.env` here first would
    /// make the seeder skip its copy and cost the operator the documented
    /// file every other key is described in.
    ///
    /// The Python side removes these from the staged file once they are in
    /// `.env`, so the secrets do not survive in the archived record of what
    /// this install was set up with.
    #[serde(default)]
    pub api_keys: BTreeMap<String, String>,
    /// The step-2 download choices: `embeddings` / `reranker` / `gpu`, each a
    /// bool. Deliberately a map and not a struct of three named fields — the
    /// set of things the form offers is decided by the page and the Python
    /// side, and a Rust struct would be a third copy of that list which has to
    /// be edited in lockstep or silently drop whatever it does not know.
    ///
    /// **This field existing at all is the fix for a real defect.** The form
    /// sent `optional` before this struct had anywhere to put it, and serde
    /// drops unknown keys silently (there is no `deny_unknown_fields` here) —
    /// so the operator's answers were discarded between the click and the
    /// staged file, and every one of them read as "never asked" on the other
    /// side. Nothing failed; the choices simply evaporated.
    #[serde(default)]
    pub optional: BTreeMap<String, bool>,
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

/// Invoked by the splash screen's setup form. Fire-and-forget: provisioning
/// runs on a background thread and reports back through `provision-progress`
/// events, so the webview IPC call returns immediately rather than blocking
/// for the several minutes a clone plus dependency install takes.
#[tauri::command]
pub fn submit_first_run_setup(
    app: AppHandle,
    home: State<TesseractHome>,
    answers: SetupAnswers,
) {
    let home_path = home.0.clone();
    // Logged without the names: they are the operator's, and shell.log is the
    // first thing attached to a bug report. The keys are counted, never named
    // and never valued — how many were filled in is the whole diagnostic.
    shell_log::log(&format!(
        "first-run setup submitted (tts={}, stt={}, api keys={}) — starting provisioning",
        answers.tts,
        answers.stt,
        answers.api_keys.values().filter(|v| !v.trim().is_empty()).count()
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

/// True once provisioning has been started, and never reset.
///
/// A second run is never wanted: two would race through the same
/// `app.clone-tmp` staging directory, run the dependency and model stages
/// concurrently, and share the single `ACTIVE_PID` slot that quit reads — so
/// stopping the download would reach only whichever subprocess wrote to it
/// last. `update.rs` already guards its analogous path with an
/// `UpdateInProgress` flag; provisioning had no equivalent.
///
/// Never reset, deliberately: a failed provision is terminal for the run
/// (there is no retry control on the splash), so "started once" is the whole
/// lifetime this needs to cover.
static PROVISIONING_STARTED: AtomicBool = AtomicBool::new(false);

/// Runs `provision()` on a background thread and routes its outcome.
///
/// Shared by the setup-form submit and by the fallback path that skips the
/// form entirely, so both reach provisioning through exactly one code path —
/// which is also what makes one guard here enough.
pub fn start_provisioning(app: AppHandle, home: PathBuf) {
    if PROVISIONING_STARTED.swap(true, Ordering::SeqCst) {
        shell_log::log("provisioning already running — ignoring a second start");
        return;
    }
    std::thread::spawn(move || {
        let result = crate::provision::provision(&app, &home);
        match result {
            Ok(_) => crate::finish_provisioning_success(&app, &home),
            Err(crate::provision::ProvisionError(msg)) => {
                // Scrubbed here rather than trusting the producer: clone-stage
                // errors arrive pre-scrubbed via `classify_clone_error`, but
                // every other stage forwards raw `uv`/`python` stderr, which
                // can embed a credentialed URL from an index or proxy env var.
                // This is the last point before the text reaches the log file
                // and the screen.
                let msg = crate::provision::scrub_credentials(&msg);
                shell_log::log_error(&format!("provisioning failed: {msg}"));
                let _ = app.emit("provision-progress", format!("Setup failed: {msg}"));
            }
        }
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
            api_keys: BTreeMap::new(),
            optional: BTreeMap::from([
                ("embeddings".to_string(), true),
                ("reranker".to_string(), false),
            ]),
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
            api_keys: BTreeMap::new(),
            optional: BTreeMap::new(),
        };

        save_answers(base.path(), &empty).expect("an empty form must still stage");

        let text = std::fs::read_to_string(answers_path(base.path())).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&text).unwrap();
        assert_eq!(parsed["agent_name"], "");
    }

    /// The step-2 choices have to survive the hop, and this is the test that
    /// would have caught them not doing so.
    ///
    /// `SetupAnswers` had no `optional` field and carries no
    /// `deny_unknown_fields`, so serde dropped the whole block silently —
    /// the form sent it, nothing errored, and the operator's download
    /// decisions simply never reached the staged file.
    #[test]
    fn the_optional_download_choices_survive_staging() {
        let base = TempDir::new("setup-optional");
        save_answers(base.path(), &answers()).expect("save should succeed");

        let text = std::fs::read_to_string(answers_path(base.path())).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&text).unwrap();
        assert_eq!(parsed["optional"]["embeddings"], true);
        assert_eq!(parsed["optional"]["reranker"], false);
    }

    /// A payload from a splash that predates step 2 must still stage.
    #[test]
    fn answers_without_an_optional_block_still_deserialize() {
        let raw = r#"{"operator_name":"Jane Doe","tts":"piper","stt":true}"#;
        let parsed: SetupAnswers = serde_json::from_str(raw).expect("must deserialize");
        assert!(parsed.optional.is_empty());
    }

    /// The keys the operator typed on the form have to survive the trip to
    /// Python intact — a mangled one reads as a rejected key from the
    /// provider, which is the hardest kind of first-run failure to diagnose.
    #[test]
    fn save_answers_stages_api_keys_verbatim() {
        let base = TempDir::new("setup-keys");
        let mut with_keys = answers();
        with_keys
            .api_keys
            .insert("OPENAI_API_KEY".to_string(), "sk-test-value".to_string());
        with_keys
            .api_keys
            .insert("XAI_API_KEY".to_string(), String::new());

        save_answers(base.path(), &with_keys).expect("save should succeed");

        let text = std::fs::read_to_string(answers_path(base.path())).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&text).unwrap();
        assert_eq!(parsed["api_keys"]["OPENAI_API_KEY"], "sk-test-value");
        assert_eq!(parsed["api_keys"]["XAI_API_KEY"], "");
    }

    /// A form from a shell that predates the keys block, or a hand-written
    /// answers file, must still deserialize — `api_keys` is `#[serde(default)]`
    /// and an install that skipped the whole block is the common case.
    #[test]
    fn answers_deserialize_without_an_api_keys_field() {
        let parsed: SetupAnswers = serde_json::from_str(
            r#"{"operator_name":"Jane Doe","agent_name":"Ada","tts":"none","stt":false}"#,
        )
        .expect("a form without api_keys must still parse");
        assert!(parsed.api_keys.is_empty());
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
