//! Shared scaffolding for the crate's unit tests.
//!
//! Every module that needed a throwaway directory or a throwaway git repo used
//! to carry its own copy of these helpers — five near-identical `TempDir`
//! structs and two `write_commit`s that had already drifted apart (one created
//! parent directories, the other did not). One definition each, here.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use git2::{Repository, RepositoryInitOptions, Signature};

/// A uniquely named directory under the system temp dir that removes itself on
/// drop, so test repos never linger. The name mixes a timestamp with a process
/// -wide counter because `cargo test` runs these concurrently.
pub struct TempDir(PathBuf);

impl TempDir {
    pub fn new(label: &str) -> Self {
        static COUNTER: AtomicU64 = AtomicU64::new(0);
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let n = COUNTER.fetch_add(1, Ordering::SeqCst);
        let dir = std::env::temp_dir().join(format!("tesseract-test-{label}-{nanos}-{n}"));
        std::fs::create_dir_all(&dir).unwrap();
        TempDir(dir)
    }

    pub fn path(&self) -> &Path {
        &self.0
    }

    pub fn join(&self, sub: &str) -> PathBuf {
        self.0.join(sub)
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// Test fixtures never carry a real identity — see the project's fixture rule.
pub fn configure_identity(repo: &Repository) {
    let mut config = repo.config().unwrap();
    config.set_str("user.name", "John Doe").unwrap();
    config
        .set_str("user.email", "john.doe@example.com")
        .unwrap();
}

/// Writes `filename` (creating parent directories) and commits it onto the
/// current HEAD, or as the root commit if the repo has none yet.
pub fn write_commit(repo: &Repository, filename: &str, content: &str, message: &str) {
    let workdir = repo.workdir().unwrap().to_path_buf();
    let target = workdir.join(filename);
    std::fs::create_dir_all(target.parent().unwrap()).unwrap();
    std::fs::write(&target, content).unwrap();

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

/// A repo on branch `main` with an identity configured, ready for
/// `write_commit`. `main` specifically, because that is the branch
/// `repo::fast_forward` and `repo::check_behind` hard-code.
pub fn init_repo(path: &Path) -> Repository {
    std::fs::create_dir_all(path).unwrap();
    let mut opts = RepositoryInitOptions::new();
    opts.initial_head("main");
    let repo = Repository::init_opts(path, &opts).unwrap();
    configure_identity(&repo);
    repo
}
