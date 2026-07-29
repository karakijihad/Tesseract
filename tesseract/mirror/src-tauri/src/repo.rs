use std::path::Path;

use git2::{build::RepoBuilder, Cred, FetchOptions, RemoteCallbacks, Repository, StatusOptions};
use serde::Serialize;

/// Caps how many dirty paths / ahead-commit summaries `local_divergence`
/// collects in detail — a repo with hundreds of untracked files must not
/// blow up the divergence report (or the UI rendering it). `dirty_total`
/// and `ahead` always carry the true counts regardless of this cap.
const DIVERGENCE_CAP: usize = 20;

/// What actually differs between the checked-out branch and `origin/main`
/// when a fast-forward is refused: uncommitted working-tree/index changes
/// (`dirty`) and local-only commits (`ahead`). Both lists are capped at
/// `DIVERGENCE_CAP`; `dirty_total` carries the true count so a capped list
/// never silently underreports.
#[derive(Debug, Serialize)]
pub struct Divergence {
    pub dirty: Vec<String>,
    pub dirty_total: usize,
    pub ahead: usize,
    pub ahead_summaries: Vec<String>,
}

pub const DEFAULT_REPO_URL: &str = "https://github.com/karakijihad/Tesseract.git";

pub fn repo_url() -> String {
    std::env::var("TESSERACT_REPO_URL").unwrap_or_else(|_| DEFAULT_REPO_URL.to_string())
}

pub fn github_token(home: &Path) -> Option<String> {
    if let Ok(t) = std::env::var("GITHUB_TOKEN") {
        let t = t.trim().to_string();
        if !t.is_empty() {
            return Some(t);
        }
    }
    std::fs::read_to_string(home.join("runtime").join("github_token"))
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
}

fn fetch_options(token: Option<String>) -> FetchOptions<'static> {
    let mut cb = RemoteCallbacks::new();
    if let Some(tok) = token {
        cb.credentials(move |_url, _user, _| Cred::userpass_plaintext("x-access-token", &tok));
    }
    let mut fo = FetchOptions::new();
    fo.remote_callbacks(cb);
    fo
}

pub fn clone(url: &str, dest: &Path, token: Option<String>) -> Result<(), String> {
    RepoBuilder::new()
        .fetch_options(fetch_options(token))
        .clone(url, dest)
        .map(|_| ())
        .map_err(|e| format!("clone {url}: {e}"))
}

pub fn check_behind(dest: &Path, token: Option<String>) -> Result<(usize, Vec<String>), String> {
    let repo = Repository::open(dest).map_err(|e| format!("open: {e}"))?;
    let mut remote = repo
        .find_remote("origin")
        .map_err(|e| format!("origin: {e}"))?;
    remote
        .fetch(&["main"], Some(&mut fetch_options(token)), None)
        .map_err(|e| format!("fetch: {e}"))?;
    let head = repo
        .head()
        .map_err(|e| e.to_string())?
        .target()
        .ok_or("unborn HEAD")?;
    let fetched = repo
        .find_reference("refs/remotes/origin/main")
        .map_err(|e| e.to_string())?
        .target()
        .ok_or("origin/main unresolved")?;
    let (_ahead, behind) = repo
        .graph_ahead_behind(head, fetched)
        .map_err(|e| e.to_string())?;
    let mut summaries = Vec::new();
    if behind > 0 {
        let mut walk = repo.revwalk().map_err(|e| e.to_string())?;
        walk.push(fetched).map_err(|e| e.to_string())?;
        walk.hide(head).map_err(|e| e.to_string())?;
        for oid in walk.flatten() {
            if let Ok(c) = repo.find_commit(oid) {
                summaries.push(c.summary().unwrap_or("(no message)").to_string());
            }
        }
    }
    Ok((behind, summaries))
}

pub fn fast_forward(dest: &Path) -> Result<String, String> {
    let repo = Repository::open(dest).map_err(|e| format!("open: {e}"))?;
    let fetched = repo
        .find_reference("refs/remotes/origin/main")
        .map_err(|e| e.to_string())?
        .target()
        .ok_or("origin/main unresolved")?;
    let annotated = repo
        .find_annotated_commit(fetched)
        .map_err(|e| e.to_string())?;
    let (analysis, _) = repo
        .merge_analysis(&[&annotated])
        .map_err(|e| e.to_string())?;
    if analysis.is_up_to_date() {
        return head_short(dest);
    }
    if !analysis.is_fast_forward() {
        return Err(format!("non-fast-forward: {}", describe_divergence(dest)));
    }
    let mut reference = repo
        .find_reference("refs/heads/main")
        .map_err(|e| e.to_string())?;
    reference
        .set_target(fetched, "tesseract update")
        .map_err(|e| e.to_string())?;
    repo.set_head("refs/heads/main")
        .map_err(|e| e.to_string())?;
    repo.checkout_head(Some(git2::build::CheckoutBuilder::default().force()))
        .map_err(|e| e.to_string())?;
    head_short(dest)
}

pub fn head_short(dest: &Path) -> Result<String, String> {
    Ok(head_oid(dest)?[..8].to_string())
}

/// The full HEAD object id. `head_short` is for display; this is what
/// `reset_hard` needs — an unambiguous id, captured before an update so a
/// stage that fails *after* the fast-forward can restore the exact revision
/// the installed dependencies were built against.
pub fn head_oid(dest: &Path) -> Result<String, String> {
    let repo = Repository::open(dest).map_err(|e| format!("open: {e}"))?;
    let head = repo
        .head()
        .map_err(|e| e.to_string())?
        .peel_to_commit()
        .map_err(|e| e.to_string())?;
    Ok(head.id().to_string())
}

/// Hard-resets the checked-out branch and working tree back to `oid`.
///
/// Used for exactly one thing: undoing a fast-forward whose follow-on
/// dependency install failed. Respawning new code against the previous
/// dependencies leaves a mismatched pair that nothing ever repairs, so the
/// update instead returns the tree to the revision those dependencies match.
pub fn reset_hard(dest: &Path, oid: &str) -> Result<(), String> {
    let repo = Repository::open(dest).map_err(|e| format!("open: {e}"))?;
    let id = git2::Oid::from_str(oid).map_err(|e| format!("bad revision {oid}: {e}"))?;
    let object = repo
        .find_object(id, None)
        .map_err(|e| format!("revision {oid} not found: {e}"))?;
    repo.reset(&object, git2::ResetType::Hard, None)
        .map_err(|e| format!("reset to {oid}: {e}"))
}

/// Reports what actually differs between the checked-out branch and
/// `refs/remotes/origin/main` — uncommitted changes and local-only commits.
/// Assumes the caller already fetched (same contract as `fast_forward`):
/// this never talks to the network itself.
pub fn local_divergence(dest: &Path) -> Result<Divergence, String> {
    let repo = Repository::open(dest).map_err(|e| format!("open: {e}"))?;

    let mut status_opts = StatusOptions::new();
    status_opts.include_untracked(true).include_ignored(false);
    let statuses = repo
        .statuses(Some(&mut status_opts))
        .map_err(|e| e.to_string())?;
    let dirty_total = statuses.len();
    let dirty: Vec<String> = statuses
        .iter()
        .filter_map(|entry| entry.path().map(str::to_string))
        .take(DIVERGENCE_CAP)
        .collect();

    let head = repo
        .head()
        .map_err(|e| e.to_string())?
        .target()
        .ok_or("unborn HEAD")?;
    let fetched = repo
        .find_reference("refs/remotes/origin/main")
        .map_err(|e| e.to_string())?
        .target()
        .ok_or("origin/main unresolved")?;
    let (ahead, _behind) = repo
        .graph_ahead_behind(head, fetched)
        .map_err(|e| e.to_string())?;

    let mut ahead_summaries = Vec::new();
    if ahead > 0 {
        let mut walk = repo.revwalk().map_err(|e| e.to_string())?;
        walk.push(head).map_err(|e| e.to_string())?;
        walk.hide(fetched).map_err(|e| e.to_string())?;
        for oid in walk.flatten().take(DIVERGENCE_CAP) {
            if let Ok(c) = repo.find_commit(oid) {
                ahead_summaries.push(c.summary().unwrap_or("(no message)").to_string());
            }
        }
    }

    Ok(Divergence {
        dirty,
        dirty_total,
        ahead,
        ahead_summaries,
    })
}

/// Builds `fast_forward`'s non-fast-forward error text. Falls back to the
/// generic phrase if `local_divergence` itself can't be computed (e.g. the
/// repo state is unreadable) so a reporting failure never masks the
/// original fast-forward refusal.
fn describe_divergence(dest: &Path) -> String {
    match local_divergence(dest) {
        Ok(d) if d.ahead > 0 || d.dirty_total > 0 => {
            let mut parts = Vec::new();
            if d.ahead > 0 {
                parts.push(format!(
                    "{} local commit{} not on origin/main ({})",
                    d.ahead,
                    if d.ahead == 1 { "" } else { "s" },
                    d.ahead_summaries.join("; ")
                ));
            }
            if d.dirty_total > 0 {
                let more = d.dirty_total.saturating_sub(d.dirty.len());
                let suffix = if more > 0 {
                    format!(", +{more} more")
                } else {
                    String::new()
                };
                parts.push(format!(
                    "{} uncommitted change{} ({}{suffix})",
                    d.dirty_total,
                    if d.dirty_total == 1 { "" } else { "s" },
                    d.dirty.join(", ")
                ));
            }
            format!(
                "local history diverged from origin/main — {}",
                parts.join("; ")
            )
        }
        _ => "local history diverged from origin/main".to_string(),
    }
}

/// Discards local-only commits and edits to already-tracked files, and lands
/// the checked-out branch on `origin/main`. This is a hard reset in the plain
/// git sense: untracked files reported by `local_divergence`'s `dirty` are
/// left on disk untouched (git's own `reset --hard` never removes them —
/// that's `git clean`'s job, which this deliberately does not do). Callers
/// presenting this to a user must not claim untracked files get discarded.
/// This is the "discard and update" primitive: it must only ever run from
/// an explicit, user-confirmed action, never from an automatic path.
pub fn reset_to_remote(dest: &Path) -> Result<String, String> {
    let repo = Repository::open(dest).map_err(|e| format!("open: {e}"))?;
    let fetched = repo
        .find_reference("refs/remotes/origin/main")
        .map_err(|e| e.to_string())?
        .target()
        .ok_or("origin/main unresolved")?;
    reset_hard(dest, &fetched.to_string())?;
    head_short(dest)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::test_support::{configure_identity, init_repo, write_commit, TempDir};

    #[test]
    fn offline_clone_behind_and_fast_forward() {
        let base = TempDir::new("flow");
        let origin_path = base.join("origin");
        let origin = init_repo(&origin_path);
        write_commit(&origin, "a.txt", "v1", "c1");
        drop(origin);

        let origin_url = origin_path.to_string_lossy().into_owned();

        // Two independent clones of the same state, so one can fast-forward
        // cleanly while the other diverges before the remote gains a commit.
        let dest_ff = base.join("dest_ff");
        clone(&origin_url, &dest_ff, None).expect("clone dest_ff");
        let dest_diverged = base.join("dest_diverged");
        clone(&origin_url, &dest_diverged, None).expect("clone dest_diverged");

        // Diverge dest_diverged with a local-only commit before the remote moves.
        {
            let repo = Repository::open(&dest_diverged).unwrap();
            configure_identity(&repo);
            write_commit(&repo, "a.txt", "diverged", "local-only");
        }
        let diverged_sha_before = head_short(&dest_diverged).unwrap();

        // Advance the remote past both clones' common ancestor.
        {
            let origin = Repository::open(&origin_path).unwrap();
            write_commit(&origin, "a.txt", "v2", "c2");
        }
        let origin_head = head_short(&origin_path).unwrap();

        // Clean fast-forward case.
        let (behind, summaries) = check_behind(&dest_ff, None).expect("check_behind dest_ff");
        assert_eq!(
            behind, 1,
            "dest_ff should be exactly one commit behind origin"
        );
        assert_eq!(summaries, vec!["c2".to_string()]);

        let new_sha = fast_forward(&dest_ff).expect("fast_forward dest_ff should succeed");
        assert_eq!(
            new_sha, origin_head,
            "fast-forwarded HEAD should match origin HEAD"
        );
        assert_eq!(head_short(&dest_ff).unwrap(), origin_head);
        assert_eq!(
            std::fs::read_to_string(dest_ff.join("a.txt")).unwrap(),
            "v2",
            "working tree should reflect the fast-forwarded commit"
        );

        // Diverged case: fetch sees the remote is ahead too, but fast_forward must refuse.
        let (behind, _) = check_behind(&dest_diverged, None).expect("check_behind dest_diverged");
        assert_eq!(
            behind, 1,
            "dest_diverged is behind by origin's one new commit"
        );

        let result = fast_forward(&dest_diverged);
        assert!(
            result.is_err(),
            "fast_forward must refuse a diverged history instead of resetting it"
        );
        assert!(result.unwrap_err().contains("non-fast-forward"));
        assert_eq!(
            head_short(&dest_diverged).unwrap(),
            diverged_sha_before,
            "a refused fast-forward must leave local HEAD untouched"
        );
    }

    #[test]
    fn repo_url_and_github_token_precedence() {
        // repo_url(): env override wins, then falls back to the default constant.
        std::env::remove_var("TESSERACT_REPO_URL");
        assert_eq!(repo_url(), DEFAULT_REPO_URL);
        std::env::set_var("TESSERACT_REPO_URL", "https://example.invalid/x.git");
        assert_eq!(repo_url(), "https://example.invalid/x.git");
        std::env::remove_var("TESSERACT_REPO_URL");

        // github_token(): env var wins (trimmed), then <home>/runtime/github_token
        // (trimmed, empty treated as absent), else None.
        let home = TempDir::new("token-home");

        std::env::remove_var("GITHUB_TOKEN");
        assert_eq!(github_token(home.path()), None, "no env, no file => None");

        std::fs::create_dir_all(home.join("runtime")).unwrap();
        std::fs::write(
            home.join("runtime").join("github_token"),
            "  file-token  \n",
        )
        .unwrap();
        assert_eq!(
            github_token(home.path()),
            Some("file-token".to_string()),
            "file token should be trimmed"
        );

        std::env::set_var("GITHUB_TOKEN", "  env-token  ");
        assert_eq!(
            github_token(home.path()),
            Some("env-token".to_string()),
            "env var should win over file and be trimmed"
        );

        std::env::set_var("GITHUB_TOKEN", "   ");
        assert_eq!(
            github_token(home.path()),
            Some("file-token".to_string()),
            "whitespace-only env var must be treated as absent, falling back to file"
        );

        std::fs::write(home.join("runtime").join("github_token"), "").unwrap();
        std::env::remove_var("GITHUB_TOKEN");
        assert_eq!(
            github_token(home.path()),
            None,
            "empty file content must be treated as absent"
        );

        std::env::remove_var("GITHUB_TOKEN");
    }

    #[test]
    fn head_oid_is_a_full_40_char_oid_and_head_short_is_its_first_8_chars() {
        let base = TempDir::new("oid-and-short");
        let origin_path = base.join("origin");
        let origin = init_repo(&origin_path);
        write_commit(&origin, "a.txt", "v1", "c1");
        drop(origin);

        let oid = head_oid(&origin_path).unwrap();
        assert_eq!(oid.len(), 40, "a git oid is 40 hex chars, got: {oid}");
        assert!(
            oid.chars().all(|c| c.is_ascii_hexdigit()),
            "oid must be all hex digits, got: {oid}"
        );

        let short = head_short(&origin_path).unwrap();
        assert_eq!(short, oid[..8]);
    }

    #[test]
    fn reset_hard_moves_head_and_working_tree_back_to_an_earlier_commit() {
        let base = TempDir::new("reset-hard");
        let origin_path = base.join("origin");
        let origin = init_repo(&origin_path);
        write_commit(&origin, "a.txt", "v1", "commit A");
        let oid_a = head_oid(&origin_path).unwrap();
        write_commit(&origin, "a.txt", "v2", "commit B");
        drop(origin);

        reset_hard(&origin_path, &oid_a).expect("reset to commit A should succeed");

        assert_eq!(head_oid(&origin_path).unwrap(), oid_a);
        assert_eq!(
            std::fs::read_to_string(origin_path.join("a.txt")).unwrap(),
            "v1",
            "the working tree must reflect commit A's content, not commit B's"
        );
    }

    #[test]
    fn reset_hard_with_an_unknown_oid_fails_and_leaves_the_repo_untouched() {
        let base = TempDir::new("reset-hard-bad-oid");
        let origin_path = base.join("origin");
        let origin = init_repo(&origin_path);
        write_commit(&origin, "a.txt", "v1", "commit A");
        drop(origin);
        let before = head_oid(&origin_path).unwrap();

        let err = reset_hard(&origin_path, "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
            .expect_err("an oid that does not exist in the repo must fail");
        assert!(!err.is_empty());
        assert_eq!(
            head_oid(&origin_path).unwrap(),
            before,
            "a failed reset must leave HEAD untouched"
        );
    }

    // -- local_divergence / reset_to_remote ---------------------------------

    /// Clones a fresh single-commit origin into `dest` and fetches, so
    /// `refs/remotes/origin/main` exists — the same precondition every
    /// `local_divergence`/`fast_forward` caller relies on in production.
    fn clean_synced_clone(base: &TempDir) -> (std::path::PathBuf, std::path::PathBuf) {
        let origin_path = base.join("origin");
        let origin = init_repo(&origin_path);
        write_commit(&origin, "a.txt", "v1", "c1");
        write_commit(&origin, "b.txt", "v1", "c1b");
        drop(origin);

        let dest = base.join("dest");
        clone(&origin_path.to_string_lossy(), &dest, None).expect("clone dest");
        check_behind(&dest, None).expect("fetch so origin/main exists");
        (origin_path, dest)
    }

    #[test]
    fn local_divergence_reports_nothing_on_a_clean_synced_clone() {
        let base = TempDir::new("divergence-clean");
        let (_origin_path, dest) = clean_synced_clone(&base);

        let d = local_divergence(&dest).expect("local_divergence should succeed");
        assert_eq!(d.ahead, 0);
        assert!(d.ahead_summaries.is_empty());
        assert_eq!(d.dirty_total, 0);
        assert!(d.dirty.is_empty());
    }

    #[test]
    fn local_divergence_reports_an_uncommitted_file_as_dirty() {
        let base = TempDir::new("divergence-dirty");
        let (_origin_path, dest) = clean_synced_clone(&base);
        std::fs::write(dest.join("untracked.txt"), "scratch").unwrap();

        let d = local_divergence(&dest).expect("local_divergence should succeed");
        assert_eq!(d.ahead, 0);
        assert_eq!(d.dirty_total, 1);
        assert_eq!(d.dirty, vec!["untracked.txt".to_string()]);
    }

    #[test]
    fn local_divergence_reports_a_local_commit_as_ahead_with_its_summary() {
        let base = TempDir::new("divergence-ahead");
        let (_origin_path, dest) = clean_synced_clone(&base);
        {
            let repo = Repository::open(&dest).unwrap();
            configure_identity(&repo);
            write_commit(&repo, "a.txt", "v2", "local-only commit");
        }

        let d = local_divergence(&dest).expect("local_divergence should succeed");
        assert_eq!(d.ahead, 1);
        assert_eq!(d.ahead_summaries, vec!["local-only commit".to_string()]);
        assert_eq!(d.dirty_total, 0, "a committed change is not dirty");
    }

    #[test]
    fn local_divergence_reports_both_ahead_and_dirty_together() {
        let base = TempDir::new("divergence-both");
        let (_origin_path, dest) = clean_synced_clone(&base);
        {
            let repo = Repository::open(&dest).unwrap();
            configure_identity(&repo);
            write_commit(&repo, "c.txt", "v1", "local-only commit");
        }
        std::fs::write(dest.join("untracked.txt"), "scratch").unwrap();

        let d = local_divergence(&dest).expect("local_divergence should succeed");
        assert_eq!(d.ahead, 1);
        assert_eq!(d.ahead_summaries, vec!["local-only commit".to_string()]);
        assert_eq!(d.dirty_total, 1);
        assert_eq!(d.dirty, vec!["untracked.txt".to_string()]);
    }

    #[test]
    fn local_divergence_caps_dirty_and_ahead_lists_but_keeps_true_counts() {
        let base = TempDir::new("divergence-capped");
        let (_origin_path, dest) = clean_synced_clone(&base);

        let extra_commits = DIVERGENCE_CAP + 5;
        {
            let repo = Repository::open(&dest).unwrap();
            configure_identity(&repo);
            for i in 0..extra_commits {
                write_commit(
                    &repo,
                    "a.txt",
                    &format!("v{i}"),
                    &format!("local commit {i}"),
                );
            }
        }
        let extra_untracked = DIVERGENCE_CAP + 3;
        for i in 0..extra_untracked {
            std::fs::write(dest.join(format!("scratch-{i}.txt")), "x").unwrap();
        }

        let d = local_divergence(&dest).expect("local_divergence should succeed");
        assert_eq!(d.ahead, extra_commits);
        assert_eq!(d.ahead_summaries.len(), DIVERGENCE_CAP);
        assert_eq!(d.dirty_total, extra_untracked);
        assert_eq!(d.dirty.len(), DIVERGENCE_CAP);
    }

    #[test]
    fn fast_forward_error_names_what_diverged() {
        let base = TempDir::new("divergence-message");
        let (origin_path, dest) = clean_synced_clone(&base);
        {
            let repo = Repository::open(&dest).unwrap();
            configure_identity(&repo);
            write_commit(&repo, "a.txt", "diverged", "local-only commit");
        }
        {
            let origin = Repository::open(&origin_path).unwrap();
            write_commit(&origin, "a.txt", "v2", "origin moved on");
        }
        check_behind(&dest, None).expect("fetch before fast_forward");

        let err = fast_forward(&dest).expect_err("a diverged history must refuse fast-forward");
        assert!(err.contains("non-fast-forward"), "got: {err}");
        assert!(
            err.contains("1 local commit") && err.contains("local-only commit"),
            "error must name the diverging commit, got: {err}"
        );
    }

    #[test]
    fn reset_to_remote_discards_local_commits_and_working_tree_changes() {
        let base = TempDir::new("reset-to-remote");
        let (origin_path, dest) = clean_synced_clone(&base);
        {
            let repo = Repository::open(&dest).unwrap();
            configure_identity(&repo);
            write_commit(&repo, "a.txt", "diverged", "local-only commit");
        }
        // An uncommitted edit to an already-tracked file — a "working-tree
        // change" in the git sense, as distinct from the untracked-file case
        // `local_divergence`'s dirty tests already cover. `git reset --hard`
        // discards this.
        std::fs::write(dest.join("b.txt"), "locally-edited").unwrap();
        {
            let origin = Repository::open(&origin_path).unwrap();
            write_commit(&origin, "a.txt", "v2", "origin moved on");
        }
        check_behind(&dest, None).expect("fetch before reset_to_remote");
        let origin_head = head_short(&origin_path).unwrap();

        assert!(
            fast_forward(&dest).is_err(),
            "sanity check: this history really is diverged"
        );

        let new_sha = reset_to_remote(&dest).expect("reset_to_remote should succeed");
        assert_eq!(new_sha, origin_head);
        assert_eq!(head_short(&dest).unwrap(), origin_head);
        assert_eq!(
            std::fs::read_to_string(dest.join("a.txt")).unwrap(),
            "v2",
            "working tree must reflect origin's content, not the discarded local commit"
        );
        assert_eq!(
            std::fs::read_to_string(dest.join("b.txt")).unwrap(),
            "v1",
            "an uncommitted edit to a tracked file must be discarded by the hard reset"
        );

        let d = local_divergence(&dest).expect("local_divergence after reset should succeed");
        assert_eq!(d.ahead, 0);
        assert_eq!(d.dirty_total, 0);
    }
}
