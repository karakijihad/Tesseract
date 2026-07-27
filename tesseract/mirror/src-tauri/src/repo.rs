use std::path::Path;

use git2::{build::RepoBuilder, Cred, FetchOptions, RemoteCallbacks, Repository};

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
        return Err("non-fast-forward: local history diverged from origin/main".into());
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
    let repo = Repository::open(dest).map_err(|e| format!("open: {e}"))?;
    let head = repo
        .head()
        .map_err(|e| e.to_string())?
        .peel_to_commit()
        .map_err(|e| e.to_string())?;
    Ok(head.id().to_string()[..8].to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use git2::{RepositoryInitOptions, Signature};
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
            let dir = std::env::temp_dir().join(format!("tesseract-repo-test-{label}-{nanos}-{n}"));
            std::fs::create_dir_all(&dir).unwrap();
            TempDir(dir)
        }

        fn path(&self) -> &Path {
            &self.0
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

    #[test]
    fn offline_clone_behind_and_fast_forward() {
        let base = TempDir::new("flow");
        let origin_path = base.join("origin");
        std::fs::create_dir_all(&origin_path).unwrap();

        let mut opts = RepositoryInitOptions::new();
        opts.initial_head("main");
        let origin = Repository::init_opts(&origin_path, &opts).unwrap();
        configure_identity(&origin);
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
}
