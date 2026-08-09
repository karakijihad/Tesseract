"""Startup orphan reaping for the supervised Tesseract stack.

Why this exists: the supervisor reaps its ``controller`` daemon child in
``run()``'s ``finally`` block. But a hard kill (``taskkill /F``, a crash,
a closed console) skips ``finally`` entirely, so those children orphan and
linger. A later supervisor then starts on top of them; the stacked
generations contend and their overlapping ``port_cleanup`` passes kill
each other's *healthy* backend/Vite — surfacing as an exit ``code=1`` with
no traceback and an ``ELIFECYCLE`` Vite death (observed 2026-07-01: three
generations of orphaned daemons alive at once).

At startup — BEFORE spawning fresh children — the supervisor reaps any
pre-existing Tesseract daemon/backend process. Nothing this supervisor owns
exists yet, so every match is an orphan from a prior generation.

Best-effort and stdlib-only (matches ``port_cleanup`` / ``process_probe``):
Windows enumerates via PowerShell ``Get-CimInstance`` + ``taskkill``; POSIX
via ``ps`` + ``SIGKILL``. Failure logs and returns — supervisor boot is
unaffected. Opt out with ``SUPERVISOR_DISABLE_REAP=1`` (hermetic test runs).
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys

log = logging.getLogger(__name__)


# Module invocations only the supervisor should own. A python process running
# one of these that we did NOT just spawn is an orphan from a dead supervisor.
# NB: ``tesseract.supervisor`` is deliberately absent — we never kill another
# supervisor, only leaked daemon/backend children.
_ORPHAN_MARKERS: tuple[str, ...] = (
    "tesseract.scripts.agent_controller",
    "tesseract.mirror.server",
)

_PS_TIMEOUT_S = 5.0
_KILL_TIMEOUT_S = 3.0


def orphan_pids(processes: list[tuple[int, str]], self_pid: int) -> list[int]:
    """Pure filter: ``(pid, cmdline)`` pairs → pids to reap.

    A pid is an orphan when it is not ``self_pid``, is positive, and its
    command line carries one of :data:`_ORPHAN_MARKERS`. Kept pure so the
    matching logic is unit-tested without touching the OS.
    """
    out: list[int] = []
    for pid, cmdline in processes:
        if pid == self_pid or pid <= 0:
            continue
        if any(marker in cmdline for marker in _ORPHAN_MARKERS):
            out.append(pid)
    return out


def _enumerate_windows() -> list[tuple[int, str]]:
    """``python.exe`` processes as ``(pid, cmdline)`` via ``Get-CimInstance``.

    PowerShell (not ``wmic``, which is absent on newer Win11) emits one
    ``<pid>\\t<cmdline>`` line per process.
    """
    script = (
        "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | "
        "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT_S,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        log.exception("reap: powershell process enumeration failed")
        return []
    procs: list[tuple[int, str]] = []
    for line in result.stdout.splitlines():
        pid_str, sep, cmdline = line.partition("\t")
        if not sep:
            continue
        try:
            procs.append((int(pid_str.strip()), cmdline))
        except ValueError:
            continue
    return procs


def _enumerate_posix() -> list[tuple[int, str]]:
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT_S,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        log.exception("reap: ps process enumeration failed")
        return []
    procs: list[tuple[int, str]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        pid_str, sep, cmdline = line.partition(" ")
        if not sep:
            continue
        try:
            procs.append((int(pid_str), cmdline))
        except ValueError:
            continue
    return procs


def _kill(pid: int) -> bool:
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                text=True,
                timeout=_KILL_TIMEOUT_S,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            log.exception("reap: taskkill failed for pid=%d", pid)
            return False
        return result.returncode == 0
    try:
        os.kill(pid, signal.SIGKILL)
        return True
    except OSError:
        log.exception("reap: kill(%d, SIGKILL) failed", pid)
        return False


def reap_orphans() -> list[int]:
    """Kill orphaned Tesseract daemon/backend processes from prior supervisors.

    Called at supervisor startup, before any child is spawned. Best-effort —
    returns the list of pids successfully reaped. ``SUPERVISOR_DISABLE_REAP=1``
    disables it entirely.
    """
    if os.environ.get("SUPERVISOR_DISABLE_REAP") == "1":
        return []
    processes = _enumerate_windows() if sys.platform == "win32" else _enumerate_posix()
    targets = orphan_pids(processes, os.getpid())
    reaped: list[int] = []
    for pid in targets:
        if _kill(pid):
            reaped.append(pid)
            log.warning("reap: killed orphaned Tesseract process pid=%d (prior supervisor)", pid)
    if reaped:
        log.warning("reap: cleared %d orphaned process(es) before boot: %s", len(reaped), reaped)
    return reaped


__all__ = ["orphan_pids", "reap_orphans"]
