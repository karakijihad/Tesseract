"""Cross-platform "is this PID alive" probe.

Why this module exists: ``os.kill(pid, 0)`` is the standard POSIX
liveness probe, but on Windows it is **unreliable**. Python's
``os.kill`` on Windows maps to ``TerminateProcess`` under the hood for
non-zero signals, and for signal ``0`` it goes through a path that
raises ``OSError: [WinError 87] The parameter is incorrect`` even when
the target process is genuinely alive. Treating that error as DEAD
(as a naive POSIX-style implementation does) produced the stale-PID
badge in the Mirror runtime panel even when the supervisor was
actively running.

The canonical Windows probe is ``OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,
False, pid)`` + ``GetExitCodeProcess`` checking for ``STILL_ACTIVE``.
That round-trip works against any live process the current token can
query (which includes any process the user owns).

This helper is the **single** liveness-check used by:

* ``mirror/server/routes/runtime.py::_read_supervisor_pid`` — the
  Runtime panel badge in the Mirror.
* ``supervisor/__main__.py::_pid_alive`` — stale-pid detection at
  supervisor boot.
* ``orchestrator/workers/recovery.py::is_pid_alive`` — AU-3 worker
  recovery: tells "interrupted" apart from "still running" so the
  RecoveryManager doesn't re-spawn live workers.

All three previously re-implemented the same POSIX-only logic; this
module deduplicates them and fixes the Windows path.
"""

from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger(__name__)


# Windows ``GetExitCodeProcess`` returns ``STILL_ACTIVE`` (259) for a
# running process. Documented in <winnt.h>.
_WIN_STILL_ACTIVE = 259
_WIN_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _alive_windows(pid: int) -> bool:
    """Windows: ``OpenProcess`` + ``GetExitCodeProcess``.

    ``OpenProcess`` returns a NULL handle if the PID does not exist or
    the current token lacks permission. ``PROCESS_QUERY_LIMITED_INFORMATION``
    is the minimum access right and is granted by default for any
    process the same user owns. Distinguishing "doesn't exist" from
    "permission denied" requires GetLastError, but for our purposes
    (was the supervisor / worker process I spawned still running?)
    same-user always opens cleanly — failure = dead.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(_WIN_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        if not ok:
            return False
        return exit_code.value == _WIN_STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _alive_posix(pid: int) -> bool:
    """POSIX: ``os.kill(pid, 0)``.

    ``ProcessLookupError`` (= ``ESRCH``) means the PID is gone. A
    ``PermissionError`` (= ``EPERM``) means the process exists but
    belongs to another user; we treat that as alive so we don't
    falsely declare a sibling process dead.
    """
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # Any other OSError (rare) treated as dead to fail-safe.
        return False


def pid_alive(pid: int | None) -> bool:
    """``True`` iff ``pid`` names a running process the current user can
    observe. ``None`` / non-positive PIDs are always ``False``.

    Cross-platform: Windows uses ``OpenProcess`` + ``GetExitCodeProcess``;
    POSIX uses ``os.kill(pid, 0)``.
    """
    if pid is None or pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            return _alive_windows(pid)
        return _alive_posix(pid)
    except Exception:
        log.exception("pid_alive: probe raised for pid=%s", pid)
        return False


__all__ = ["pid_alive"]
