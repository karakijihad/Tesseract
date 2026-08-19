//! The one thing no exit handler can cover: a crash or a Task-Manager kill.
//!
//! Quitting cleanly is already handled — `stop_active` latches, kills the
//! provisioning subprocess tree, and reaps the launch fetchers. All of it runs
//! from `RunEvent::Exit`, which means all of it depends on this process living
//! long enough to run something. A crash, or an end-task from the taskbar,
//! runs nothing at all, and what is left behind is `uv` still resolving or
//! `provision_hardware` still pulling ~2.2 GB of CUDA wheels against a home
//! directory whose owner believes the app is closed.
//!
//! A Windows Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` is the
//! kernel doing what no handler can: every process assigned to the job dies
//! when the last handle to it closes, and our handle closes when this process
//! does, however it does. Descendants are covered without being registered —
//! a child of a child inherits the job — which is the same reach `taskkill /T`
//! gets by walking the tree, without needing the tree to still exist.
//!
//! **It is a backstop, not a replacement.** The existing registries stay:
//! they are what makes a clean quit report which download it stopped, and
//! they run while the process is alive, where they can wait for a child and
//! log what happened. This layer only speaks when nothing else can.
//!
//! Deliberately NOT applied to the supervisor. Its shutdown is a protocol —
//! the shell writes a stop request and the supervisor drains its own state —
//! and a job that kills it the instant this process exits would truncate that
//! on every ordinary quit, trading an orphan on a rare crash for a lost flush
//! on every close. Adopting it is a separate decision with its own evidence.

/// Assigns `pid` to the kill-on-close job, creating the job on first use.
///
/// Returns whether the process is now covered. Best-effort by construction:
/// every caller has already spawned the child, and a job that could not be
/// created must not turn a working download into a failed one. A false answer
/// means "no backstop", never "no download".
#[cfg(windows)]
pub fn adopt(pid: u32) -> bool {
    imp::adopt(pid)
}

/// Non-Windows builds have no job objects and no orphan problem of this shape
/// — a POSIX parent's children are reparented to init, and the supervisor's
/// own process-group handling covers what matters there. Answering `false`
/// keeps every caller's logic identical across platforms.
#[cfg(not(windows))]
pub fn adopt(_pid: u32) -> bool {
    false
}

#[cfg(all(test, windows))]
mod tests {
    use std::process::{Command, Stdio};

    /// What the job DOES — killing its children when this process dies — is
    /// not something a unit test can observe without ending the test runner.
    /// What is testable, and what would actually break silently, is the
    /// plumbing: that the job is created, that the assignment is accepted, and
    /// that a second child joins the SAME job rather than a fresh one whose
    /// handle nothing holds.
    fn probe() -> std::process::Child {
        let mut cmd = Command::new("cmd");
        cmd.args(["/C", "ping -n 30 127.0.0.1 >nul"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        crate::provision::hide_console(&mut cmd);
        cmd.spawn().expect("the probe child should spawn")
    }

    #[test]
    fn a_spawned_child_is_adopted() {
        let mut child = probe();

        let adopted = super::adopt(child.id());

        let _ = child.kill();
        let _ = child.wait();
        assert!(adopted, "the job could not take a process we had just spawned");
    }

    #[test]
    fn two_children_share_one_job() {
        // A per-child job would be worse than none: each handle would drop
        // when the local went out of scope, killing the child immediately.
        let mut first = probe();
        let mut second = probe();

        let both = super::adopt(first.id()) && super::adopt(second.id());

        for child in [&mut first, &mut second] {
            let _ = child.kill();
            let _ = child.wait();
        }
        assert!(both);
    }

    /// The env var that turns the helper test below into the helper. Carries
    /// the file the helper writes its child's pid into, so the outer test
    /// knows which process it is waiting to see die.
    const HELPER_PID_FILE: &str = "TESSERACT_JOB_TEST_PID_FILE";

    /// Not a test when run normally — it returns immediately. Re-executed by
    /// `kill_on_close_reaps_a_grandchild` with the env var set, it becomes the
    /// second process the guarantee needs: the job is a private `OnceLock`, so
    /// proving that closing it kills anything means a process that creates one,
    /// adopts a child, and then exits. The harness cannot be that process for
    /// itself — it would have to end the run to close its own handle.
    #[test]
    fn job_helper_adopts_a_child_then_exits() {
        let Ok(pid_file) = std::env::var(HELPER_PID_FILE) else {
            return;
        };
        let child = probe();
        assert!(
            super::adopt(child.id()),
            "the helper could not adopt its own child"
        );
        std::fs::write(&pid_file, child.id().to_string()).expect("pid file should be writable");
        // Deliberately no kill and no wait. Returning ends the harness process,
        // which drops the only handle to the job — and that is the mechanism
        // under test. The child would otherwise run for 30 seconds.
    }

    /// Windows recycles pids, so this answers "still the process we adopted",
    /// not merely "some process has this id": an exit code of `STILL_ACTIVE`
    /// on a handle we opened by that id.
    fn is_running(pid: u32) -> bool {
        use windows_sys::Win32::Foundation::{CloseHandle, STILL_ACTIVE};
        use windows_sys::Win32::System::Threading::{
            GetExitCodeProcess, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
        };

        // SAFETY: a query-only handle, closed on every path; `code` is written
        // by the call and only read when it reports success.
        unsafe {
            let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
            if handle.is_null() {
                return false;
            }
            let mut code: u32 = 0;
            let ok = GetExitCodeProcess(handle, &mut code);
            CloseHandle(handle);
            ok != 0 && code == STILL_ACTIVE as u32
        }
    }

    #[test]
    fn kill_on_close_reaps_a_grandchild() {
        // The whole reason the module exists, and the one thing its other
        // tests cannot see: the plumbing can keep passing while the limit
        // silently stops being applied.
        let pid_file = std::env::temp_dir().join(format!(
            "tesseract-job-kill-on-close-{}.pid",
            std::process::id()
        ));
        let _ = std::fs::remove_file(&pid_file);

        let helper = std::env::current_exe().expect("the test binary should name itself");
        let status = Command::new(helper)
            .args([
                "--exact",
                "job::tests::job_helper_adopts_a_child_then_exits",
                "--test-threads=1",
            ])
            .env(HELPER_PID_FILE, &pid_file)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .expect("the helper process should run");
        assert!(status.success(), "the helper failed before it could adopt");

        let pid: u32 = std::fs::read_to_string(&pid_file)
            .expect("the helper should have written its child's pid")
            .trim()
            .parse()
            .expect("the pid file should hold a pid");
        let _ = std::fs::remove_file(&pid_file);

        // The kernel reaps asynchronously once the last handle closes, so this
        // waits rather than sampling once. The grandchild would otherwise be
        // alive for 30 seconds.
        let mut alive = true;
        for _ in 0..100 {
            if !is_running(pid) {
                alive = false;
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(50));
        }
        if alive {
            // Never leave a 30-second ping behind on a failing run.
            let _ = Command::new("taskkill")
                .args(["/F", "/PID", &pid.to_string()])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status();
        }
        assert!(
            !alive,
            "the grandchild outlived the process holding the job — kill-on-close is not applied"
        );
    }

    #[test]
    fn a_pid_that_is_not_a_process_is_answered_not_panicked() {
        // The race every caller has: the child can exit between `spawn` and
        // this call, and a backstop that panicked there would take the
        // download it was protecting with it.
        //
        // Deliberately NOT a real pid that has just exited. Windows recycles
        // pids, and adopting a recycled one would put an unrelated process
        // into a job that kills its members when this binary ends — a test
        // that can kill a bystander is worse than the bug it covers. A pid
        // that is not a multiple of four cannot name a Windows process.
        assert!(!super::adopt(u32::MAX - 1));
    }
}

#[cfg(windows)]
mod imp {
    use std::ffi::c_void;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::OnceLock;

    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };
    use windows_sys::Win32::System::Threading::{
        OpenProcess, PROCESS_SET_QUOTA, PROCESS_TERMINATE,
    };

    /// A handle that is deliberately never closed while the process runs:
    /// closing it is what kills the children, so it must outlive everything
    /// and then die with us. `OnceLock` gives it exactly that lifetime.
    struct Job(HANDLE);

    // SAFETY: a job handle is a kernel object usable from any thread; nothing
    // here mutates it after creation, and the Win32 calls that take it are
    // themselves thread-safe.
    unsafe impl Send for Job {}
    unsafe impl Sync for Job {}

    static JOB: OnceLock<Option<Job>> = OnceLock::new();

    fn create() -> Option<Job> {
        // SAFETY: an unnamed job with default security; both pointers are the
        // documented "use the defaults" nulls. The info struct is zeroed and
        // its size passed exactly, which is what the call validates.
        unsafe {
            let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if handle.is_null() {
                crate::shell_log::log_error(
                    "could not create the process job — a crash may leave provisioning \
                     downloads running",
                );
                return None;
            }
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            let applied = SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const c_void,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            );
            if applied == 0 {
                // A job without the limit is worse than none: it would report
                // success and cover nothing.
                CloseHandle(handle);
                crate::shell_log::log_error(
                    "could not set kill-on-close on the process job — a crash may leave \
                     provisioning downloads running",
                );
                return None;
            }
            crate::shell_log::log("process job created — a crash now takes its children with it");
            Some(Job(handle))
        }
    }

    /// Latches after the first assignment failure so a systematic one is said
    /// once rather than per child. An assignment that fails every time means
    /// there is NO crash backstop, and the shell reporting nothing left that
    /// indistinguishable from one that works — the exact silence this module
    /// exists to remove, one level up.
    static REPORTED_FAILURE: AtomicBool = AtomicBool::new(false);

    fn report_failure(what: &str) -> bool {
        if !REPORTED_FAILURE.swap(true, Ordering::SeqCst) {
            crate::shell_log::log_error(&format!(
                "could not put a child into the process job ({what}) — a crash may leave \
                 provisioning downloads running"
            ));
        }
        false
    }

    pub fn adopt(pid: u32) -> bool {
        let Some(job) = JOB.get_or_init(create).as_ref() else {
            return false;
        };
        // SAFETY: `pid` names a process this module's callers have just
        // spawned and still hold a `Child` for, so it cannot have been
        // recycled. The handle is closed on every path below; the job's is
        // not, deliberately.
        unsafe {
            let process = OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, 0, pid);
            if process.is_null() {
                return report_failure("the process could not be opened");
            }
            let assigned = AssignProcessToJobObject(job.0, process);
            CloseHandle(process);
            if assigned == 0 {
                return report_failure("the job refused the assignment");
            }
            true
        }
    }
}
