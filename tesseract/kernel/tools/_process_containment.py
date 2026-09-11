"""Making a command and everything it starts die together.

Written after the same code was found twice. The first answer was to kill the
process on the way out, which is a patch: it only fires on the paths where the
process is still alive, and it only reaches the one process this runtime holds
a handle to. Killing is the wrong verb. The thing that has to be true is
CONTAINMENT, decided when the command starts rather than when it ends.

**The properties, and they hold at once.** A later change is checked against
the list, not against whichever one prompted it:

1. **Everything the command started dies when the call ends.** Not the direct
   child: a script that starts a background worker and exits is the ordinary
   shape of the problem, and on that path the leader's own exit code is
   already set, so anything that reads it first does nothing at all.
2. **On EVERY exit path.** It finished, it failed, it ran past the timeout, it
   printed past the ceiling, the turn was cancelled, the turn was cancelled
   twice while the cleanup was running.
3. **The teardown cannot be interrupted.** This is why it is a handle close
   and not an `await`. A second cancellation arriving during an awaited
   cleanup aborts it, and there is no way to await something uncancellably;
   closing a handle is synchronous and finishes.
4. **This process is never in the job.** Only the child is assigned. A job
   holding the runtime would take the whole application down with the command.
5. **It degrades, it does not fail.** Where the mechanism is unavailable, the
   command still runs and the caller is told what it did not get. A tool that
   refused to work on a machine whose job objects were unavailable would be
   worse than one that ran without them and said so.
6. **It costs the normal path nothing.** Two handle calls at spawn and one at
   the end.

**The one gap, stated rather than hidden.** The child is assigned to the job
immediately after it is created, not before, because asyncio gives no way to
start a process suspended. A grandchild started in that window escapes the
job. It is microseconds wide and it is the reason `_reap` still exists behind
this rather than being deleted.

**Windows only, and that is not a hole here.** The credential store is DPAPI,
which raises off Windows rather than degrading to plaintext, so a command
carrying an account cannot run on a platform where this returns nothing.

**`reap` is the other mechanism in the file, and it is the weaker one.** It
runs at the END rather than deciding at the start, and it is cross-platform.
It is here rather than beside one of its callers because there are two, and a
second copy of "stop this and everything under it" is how the first one drifts.
A tool uses it alone when containment would be wrong for that tool: `bash` is a
shell, and a shell is where a person deliberately starts something meant to
outlive the call.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import subprocess
import sys
from typing import Any

log = logging.getLogger(__name__)

#: How long stopping a command may take before the attempt is given up on and
#: reported. Not a `runtime.yaml` number: this is not a policy an operator
#: tunes, it is the bound that keeps a `finally` from hanging a turn.
REAP_TIMEOUT_S = 5.0

# Plain ctypes primitives rather than `ctypes.wintypes`, which raises on
# import off Windows. This module is imported by `command_run`, which is
# imported by the tool registry, which is imported at boot: a module that
# cannot be imported on Linux would take the whole runtime down there rather
# than degrading to no containment, which is the opposite of property 5.
_DWORD = ctypes.c_uint32
_HANDLE = ctypes.c_void_p
_LARGE_INTEGER = ctypes.c_int64

# JOBOBJECTINFOCLASS::JobObjectExtendedLimitInformation
_EXTENDED_LIMIT_INFORMATION = 9

# JOBOBJECT_BASIC_LIMIT_INFORMATION::LimitFlags
_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", _LARGE_INTEGER),
        ("PerJobUserTimeLimit", _LARGE_INTEGER),
        ("LimitFlags", _DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", _DWORD),
        ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
        ("PriorityClass", _DWORD),
        ("SchedulingClass", _DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _kernel32() -> Any:
    """Resolved at call time, never at import, for the reason above."""
    return ctypes.WinDLL("kernel32", use_last_error=True)


def open_job() -> Any | None:
    """A job whose closing kills everything in it, or ``None``.

    ``None`` is a working answer, not an error: the caller runs the command
    anyway and keeps whatever weaker cleanup it has. Every failure here is
    logged once and swallowed, because a command the operator approved must
    not be refused over a handle.
    """
    if sys.platform != "win32":
        return None
    try:
        kernel32 = _kernel32()
        kernel32.CreateJobObjectW.restype = _HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())

        limits = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = _LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            job,
            _EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        )
        if not ok:
            error = ctypes.WinError(ctypes.get_last_error())
            kernel32.CloseHandle(job)
            raise error
        return job
    except Exception:  # noqa: BLE001 — a missing job is a weaker run, not a failed one
        log.warning(
            "command containment: no job object, so a process this command "
            "starts and detaches could outlive it",
            exc_info=True,
        )
        return None


def contain(job: Any | None, pid: int) -> bool:
    """Put ``pid`` and its future children in ``job``. Never raises."""
    if job is None or sys.platform != "win32":
        return False
    try:
        kernel32 = _kernel32()
        # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        handle = kernel32.OpenProcess(0x0100 | 0x0001, False, pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not kernel32.AssignProcessToJobObject(job, handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel32.CloseHandle(handle)
        return True
    except Exception:  # noqa: BLE001
        log.warning("command containment: pid %s could not be contained", pid, exc_info=True)
        return False


def close_job(job: Any | None) -> None:
    """Kill everything in ``job`` and let it go.

    **Synchronous on purpose.** This is the teardown, and the teardown runs in
    a `finally` while a cancellation may be propagating. Anything awaited there
    can be interrupted by a second cancellation and leave the command running;
    a handle close cannot be.
    """
    if job is None or sys.platform != "win32":
        return
    try:
        _kernel32().CloseHandle(job)
    except Exception:  # noqa: BLE001 — nothing above can act on this
        log.warning("command containment: the job would not close", exc_info=True)


async def reap(process: "asyncio.subprocess.Process") -> None:
    """Stop the command and everything it started, or say why it could not.

    The weaker half of this module, and the one every tool that starts a
    process can use. Containment decides at spawn time and holds only on
    Windows; this runs at the end, reaches whatever it can, and is what a
    tool falls back on when it must not contain the command in the first
    place. `bash` is that case: a shell is where an operator deliberately
    starts something meant to outlive the call, so it kills the tree only
    when the shell has NOT exited on its own, which is the timeout and the
    cancel.

    **The tree, not the process.** A command that starts children and then
    ends leaves them running, and killing only the one this runtime holds a
    handle to reports success while they carry on. `supervisor/reap.py`
    already answers this for orphaned daemons and its answer is reused here:
    `taskkill /F /T` on Windows, which walks the tree, and `SIGKILL`
    elsewhere, which does not, and is named as the weaker half rather than
    presented as equivalent.

    **Never raises, and never waits forever.** This runs in a `finally`, so
    an exception here would replace whatever was actually being reported,
    including a cancellation that has to propagate.

    **Nothing happens on a clean exit.** The command that finished has a
    return code, and this returns on the first line.
    """
    if process.returncode is not None:
        return
    try:
        if sys.platform == "win32":
            # A THREAD, for the reason `brain/cli_auth.py` gives at its own
            # call: the proactor loop builds a subprocess transport by calling
            # `Popen` inline, so spawning here would run `CreateProcess` ON the
            # loop. Measured on this machine, worst single block 47 ms and
            # 60 ms that way against 14 ms and 12 ms through a thread. This one
            # runs in a `finally`, so it blocks the loop on the way out of
            # every command that had to be stopped.
            await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=REAP_TIMEOUT_S,
            )
        else:
            process.kill()
    except (OSError, ProcessLookupError, asyncio.TimeoutError, subprocess.TimeoutExpired):
        # Best effort by construction: the process may already be gone, and
        # `taskkill` may be missing on a stripped image. Logged rather than
        # raised, because the caller is on its way out with something to say.
        log.warning(
            "reap: could not stop pid %s and its children",
            process.pid,
            exc_info=True,
        )
    try:
        await asyncio.wait_for(process.wait(), timeout=REAP_TIMEOUT_S)
    except asyncio.TimeoutError:
        log.warning("reap: pid %s did not exit after being stopped", process.pid)


__all__ = ["REAP_TIMEOUT_S", "close_job", "contain", "open_job", "reap"]
