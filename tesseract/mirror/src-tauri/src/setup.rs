//! The first-run setup form: staging its answers, and resuming provisioning.
//!
//! The form asks what the operator should not have to discover in a config
//! file afterwards: what to call each other (including both halves of the
//! wake phrase), whether the agent speaks and with which engine, whether it
//! listens, and which optional artifacts to fetch.
//!
//! It runs BETWEEN the two halves of provisioning. The app itself installs
//! first, as progress — declining Python is not a lighter install, it is a
//! broken one — and every stage that downloads something optional waits for
//! this form, because declining a lane is only meaningful before the model
//! would have been fetched.
//!
//! That order is what lets the form be honest. Asked first, it ran before
//! Python and before the config tree, so it carried hardcoded sizes and a
//! hardcoded key list and could not know what the machine was — which is why
//! it quoted 1,600 MB of speech recognition to a laptop about to download
//! 148. Asked here, it reads `scripts/setup_manifest.py`.
//!
//! The shell still cannot write the config directly: the answers are staged
//! as JSON and `scripts/apply_first_run_setup.py` applies them, because a
//! consent record and a YAML round-trip belong on the side that owns the
//! schema.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Emitter, Manager, State};

use crate::provision::ProvisionScope;
use crate::{shell_log, TesseractHome};

/// One completed setup form.
///
/// `tts` is `"kokoro" | "cloud" | "none"` and `gender` filters the voice list
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
    /// The step-2 download choices: `embeddings` / `reranker`, each a
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

/// Invoked by the splash screen's setup form. Fire-and-forget: the fetch
/// stages run on a background thread and report back through
/// `provision-progress` events, so the webview IPC call returns immediately
/// rather than blocking for the several minutes the model downloads take.
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
        "first-run setup submitted (tts={}, stt={}, api keys={}) — fetching what it covers",
        answers.tts,
        answers.stt,
        answers.api_keys.values().filter(|v| !v.trim().is_empty()).count()
    ));
    let scope = scope_for(save_answers(&home_path, &answers));
    clear_pending_form();
    start_extras(app, home_path, scope);
}

/// Which scope a submitted form actually earns.
///
/// Not fatal, but not `Answered` either, and that distinction is the whole
/// finding. The staged file IS the answers: if it could not be written, the
/// Python side finds nothing, logs "shipped defaults kept", and records no
/// consent — so provisioning with `Answered` would fetch every shipped-default
/// lane and run the vendor installer on the strength of a form whose answers
/// were lost. That is the unanswered-provision hole through a different door,
/// and it is worse than the original, because here the operator DID answer and
/// would have no reason to suspect otherwise.
///
/// Treating it as unanswered installs the app and defers the downloads, which
/// is exactly what the answers being lost means. Every one of those choices is
/// still available in Settings, and the app says so.
fn scope_for(staged: std::io::Result<()>) -> ProvisionScope {
    match staged {
        Ok(()) => ProvisionScope::Answered,
        Err(e) => {
            shell_log::log_error(&format!(
                "could not stage first-run setup answers: {e} — installing the app only; \
                 the answers are lost, so nothing optional is downloaded on their strength"
            ));
            ProvisionScope::Unanswered
        }
    }
}

/// True once each half of provisioning has been started, and never reset.
///
/// A second run is never wanted: two would race through the same
/// `app.clone-tmp` staging directory, run the dependency and model stages
/// concurrently, and share the single `ACTIVE_PID` slot that quit reads — so
/// stopping the download would reach only whichever subprocess wrote to it
/// last. `update.rs` already guards its analogous path with an
/// `UpdateInProgress` flag; provisioning had no equivalent.
///
/// Two flags rather than one, because the halves are now started by different
/// things: the base half by the launch, the extras half by a button the
/// operator can press twice before the window changes. One flag would let the
/// second press through whenever the first had already cleared the base half,
/// which is always.
///
/// Never reset, deliberately: a failed provision is terminal for the run
/// (there is no retry control on the splash), so "started once" is the whole
/// lifetime this needs to cover.
static BASE_STARTED: AtomicBool = AtomicBool::new(false);
static EXTRAS_STARTED: AtomicBool = AtomicBool::new(false);

/// Installs the app on a background thread, then opens the form on it.
///
/// The form is what decides everything optional, so this stops at the point
/// where there is something real to ask about — a working interpreter and a
/// seeded config tree — and hands the page the manifest built from them.
///
/// Both ways of not getting an answer converge on the same outcome, and it is
/// the one the unanswered-provision hole settled: install the app, fetch
/// nothing optional, and leave every one of those choices to Settings. A
/// window that would not open and a manifest that would not build are the same
/// fact — nobody was asked.
pub fn start_provisioning(app: AppHandle, home: PathBuf) {
    if BASE_STARTED.swap(true, Ordering::SeqCst) {
        shell_log::log("provisioning already running — ignoring a second start");
        return;
    }
    std::thread::spawn(move || {
        match crate::provision::provision_base(&app, &home) {
            Ok(_) => {}
            Err(crate::provision::ProvisionError(msg)) => {
                report_failure(&app, &msg);
                return;
            }
        }
        if offer_form(&app, &home) {
            return;
        }
        shell_log::log(
            "nobody could be asked — installing the app only; optional downloads wait \
             for an answer in Settings, or for a launch that can open the form",
        );
        start_extras(app, home, ProvisionScope::Unanswered);
    });
}

/// Offer the form to an install that already has one outstanding.
///
/// The other end of the run above: that one installs the app and then finds
/// it has nobody to ask, and this one is the next launch that does. The app
/// is already here, so nothing reinstalls — `provision_base` is not on this
/// path at all, and the marker it left behind is what says it does not need
/// to be.
///
/// Failing to offer it is not a failure of the launch. The install works; it
/// simply still has a question outstanding, which is exactly the state it was
/// already in — so this starts the backend as any other launch would and
/// leaves the marker saying `unanswered`, and the next launch tries again.
pub fn offer_deferred_setup(app: AppHandle, home: PathBuf) {
    // Before the form, not after: the answers reach `provision_hardware`
    // through `start_extras`, and that stage installs GPU wheels using the
    // `uv` this resolves. Only `provision_base` populates it on a first run,
    // and `provision_base` is precisely what this path skips — so without
    // this the operator answers the form and silently keeps the CPU path.
    if let Err(e) = crate::provision::resolve_uv(&app) {
        shell_log::log_error(&format!(
            "could not resolve uv for the deferred setup ({e}) — answering it will keep \
             this machine on the CPU path"
        ));
    }
    std::thread::spawn(move || {
        if offer_form(&app, &home) {
            return;
        }
        // The one reason the form can fail that must NOT be answered by
        // starting a backend: `setup_manifest` refuses once a quit has
        // latched, and this path waits seconds for it, so a quit during the
        // build lands here with `RunEvent::Exit` already past. Spawning the
        // supervisor then leaves it running after the app is gone — nothing
        // is left to take the child, because the handler that would have
        // taken it has already run.
        if crate::provision::stopping() {
            shell_log::log("quitting before the deferred setup form could be shown");
            return;
        }
        shell_log::log(
            "this install still has an unanswered setup form and could not be shown one \
             — starting normally and offering it again on the next launch",
        );
        crate::finish_provisioning_success(&app, &home);
    });
}

/// Builds the form, holds it, and emits it. True when the operator has one in
/// front of them.
///
/// Stored BEFORE it is emitted, and that order is the fix. A Tauri event has
/// no replay: a page that had not finished registering its listener would miss
/// this one and sit on the progress view forever, with the app installed and
/// no marker written. The base install takes minutes, so it was never seen —
/// but it was timing, not a guarantee. The page asks for this on load, and the
/// event stays as the fast path.
///
/// Shared by both callers so the store-then-emit order cannot be right in one
/// and wrong in the other; what a failure MEANS differs between them, so each
/// says that for itself.
fn offer_form(app: &AppHandle, home: &PathBuf) -> bool {
    let Some(manifest) = form_manifest(app, home) else {
        return false;
    };
    if let Ok(mut slot) = PENDING_FORM.lock() {
        *slot = Some(manifest.clone());
    }
    let _ = app.emit_to("splash", "setup-form", manifest);
    true
}

/// The manifest waiting for the form, if the base install has finished and
/// nobody has answered it yet.
///
/// Held rather than only emitted, so the page has something to ask for. Not
/// cleared on READ — the splash may reload (a webview crash, a devtools
/// refresh), and a second read finding nothing would be the same stall the
/// event race caused. Cleared on SUBMIT, because after that a reload asking
/// for it would be handed a form that has already been answered, on top of
/// the downloads it started.
///
/// It carries no secret: sizes, labels, signup addresses and this machine's
/// GPU name. What is worth saying is that it is held for the process
/// lifetime rather than the window's, which is why clearing it has to be
/// deliberate rather than incidental.
static PENDING_FORM: Mutex<Option<serde_json::Value>> = Mutex::new(None);

/// Called when the form is answered, and by nothing else.
fn clear_pending_form() {
    if let Ok(mut slot) = PENDING_FORM.lock() {
        *slot = None;
    }
}

/// Invoked by the splash on load, so a listener registered after the emit is
/// not a lost form. Returns null while the app is still installing, which is
/// the page's cue to keep showing progress.
#[tauri::command]
pub fn pending_setup_form(window: tauri::Window) -> Option<serde_json::Value> {
    if window.label() != "splash" {
        shell_log::log_error(&format!(
            "pending_setup_form refused: only the splash may ask, not '{}'",
            window.label()
        ));
        return None;
    }
    PENDING_FORM.lock().ok().and_then(|slot| slot.clone())
}

/// The manifest to open the form on, or None when there is nobody to open it
/// for and nothing to open it with.
///
/// Says what went wrong and stops there. What it COSTS depends on which
/// launch asked — a first run installs the app and defers everything
/// optional, a later one starts an app that is already installed — so the
/// consequence is logged by the caller that knows it, rather than asserted
/// here by the half that does not.
fn form_manifest(app: &AppHandle, home: &PathBuf) -> Option<serde_json::Value> {
    if app.get_webview_window("splash").is_none() {
        shell_log::log_error("the setup window is not open — nobody can be asked anything");
        return None;
    }
    match crate::provision::setup_manifest(home) {
        Ok(manifest) => Some(manifest),
        Err(e) => {
            shell_log::log_error(&format!("the setup form could not be built ({e})"));
            None
        }
    }
}

/// Runs `provision_extras()` on a background thread and routes its outcome.
///
/// `scope` is the whole consent question: a submitted form has answers, and
/// every path that reaches here without one may install the app and nothing
/// else.
pub fn start_extras(app: AppHandle, home: PathBuf, scope: ProvisionScope) {
    if EXTRAS_STARTED.swap(true, Ordering::SeqCst) {
        shell_log::log("setup already submitted — ignoring a second submission");
        return;
    }
    std::thread::spawn(move || {
        match crate::provision::provision_extras(&app, &home, scope) {
            Ok(_) => crate::finish_provisioning_success(&app, &home),
            Err(crate::provision::ProvisionError(msg)) => report_failure(&app, &msg),
        }
    });
}

/// Scrubbed here rather than trusting the producer: clone-stage errors arrive
/// pre-scrubbed via `classify_clone_error`, but every other stage forwards raw
/// `uv`/`python` stderr, which can embed a credentialed URL from an index or
/// proxy env var. This is the last point before the text reaches the log file
/// and the screen.
fn report_failure(app: &AppHandle, msg: &str) {
    let msg = crate::provision::scrub_credentials(msg);
    shell_log::log_error(&format!("provisioning failed: {msg}"));
    let _ = app.emit("provision-progress", format!("Setup failed: {msg}"));
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

    /// The staged file IS the answers. Losing it and provisioning as
    /// `Answered` would fetch every shipped-default lane on the strength of a
    /// form whose answers no longer exist — the unanswered-provision hole
    /// through a door where the operator DID answer and has no reason to
    /// suspect otherwise.
    #[test]
    fn a_form_whose_answers_could_not_be_staged_is_not_an_answered_run() {
        let failed = Err(std::io::Error::other("disk full"));

        assert_eq!(scope_for(failed), ProvisionScope::Unanswered);
    }

    #[test]
    fn a_staged_form_governs_its_own_provisioning() {
        assert_eq!(scope_for(Ok(())), ProvisionScope::Answered);
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
        let raw = r#"{"operator_name":"Jane Doe","tts":"kokoro","stt":true}"#;
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
