"""Port-release helper called when the supervisor exits.

Operator-requested 2026-05-18: when the supervisor goes down (operator_quit,
crash-storm latch, max-respawns) anything still bound to the Mirror backend
port (default 8000) or the Vite dev-server port (default 1420) should be
killed too. The supervisor's normal teardown already waits for the backend
to exit cleanly, but:

* on Windows, SIGKILL'd children can leave the LISTEN socket in TIME_WAIT
  or briefly held by the OS before the next supervisor start; this helper
  hard-frees the port,
* the Vite dev server is a sibling process the operator launches by hand;
  the supervisor doesn't manage it but the operator wants both windows to
  close together,
* zombie helpers (a stale ``pnpm run dev`` from a previous session, a
  python that crashed mid-handler and left the listener half-open) are
  cleared so the next ``tesseract-start.bat`` starts clean.

Cross-platform: on Windows uses ``netstat -ano`` + ``taskkill``; on POSIX
uses ``lsof`` + ``kill``. Both paths are best-effort — failure to free a
port logs and returns; the supervisor exit code is unaffected.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import sys

log = logging.getLogger(__name__)


# Defaults match `tesseract/config/mirror.yaml::port` (backend) and the
# Vite dev-server's hardcoded 1420 in `tesseract/mirror/vite.config.ts`.
# Operator can override via SUPERVISOR_RELEASE_PORTS=8000,1420,...
DEFAULT_PORTS: tuple[int, ...] = (8000, 1420)

_NETSTAT_TIMEOUT_S = 3.0
_TASKKILL_TIMEOUT_S = 3.0


def _resolve_ports() -> tuple[int, ...]:
    override = os.environ.get("SUPERVISOR_RELEASE_PORTS", "").strip()
    if not override:
        return DEFAULT_PORTS
    out: list[int] = []
    for chunk in override.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(int(chunk))
        except ValueError:
            log.warning("port_cleanup: ignoring non-int port %r", chunk)
    return tuple(out) if out else DEFAULT_PORTS


def _windows_pids_on_port(port: int) -> set[int]:
    """``netstat -ano`` then filter for ``LISTEN`` rows on the port.

    Output lines look like::
        TCP    127.0.0.1:8000         0.0.0.0:0       LISTENING       38972
    """
    pids: set[int] = set()
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            timeout=_NETSTAT_TIMEOUT_S,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        log.exception("port_cleanup: netstat failed")
        return pids
    needle = f":{port}"
    for line in result.stdout.splitlines():
        if needle not in line or "LISTENING" not in line:
            continue
        match = re.search(r"\s+(\d+)\s*$", line)
        if match:
            try:
                pids.add(int(match.group(1)))
            except ValueError:
                continue
    return pids


def _windows_kill(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            capture_output=True,
            text=True,
            timeout=_TASKKILL_TIMEOUT_S,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        log.exception("port_cleanup: taskkill failed for pid=%d", pid)
        return False
    if result.returncode == 0:
        return True
    log.warning(
        "port_cleanup: taskkill pid=%d returned %d: %s",
        pid, result.returncode, (result.stderr or "").strip(),
    )
    return False


def _posix_pids_on_port(port: int) -> set[int]:
    try:
        result = subprocess.run(
            ["lsof", "-t", "-iTCP", f"-i:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=_NETSTAT_TIMEOUT_S,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return set()
    pids: set[int] = set()
    for token in result.stdout.split():
        try:
            pids.add(int(token))
        except ValueError:
            continue
    return pids


def _posix_kill(pid: int) -> bool:
    try:
        os.kill(pid, signal.SIGKILL)
        return True
    except OSError:
        log.exception("port_cleanup: kill(%d, SIGKILL) failed", pid)
        return False


def release_ports(ports: tuple[int, ...] | None = None) -> dict[int, list[int]]:
    """Free every supplied port. Returns ``{port: [killed_pid, …]}`` so
    the caller can log a summary. Idempotent — empty list for ports that
    had nothing bound."""
    targets = ports if ports is not None else _resolve_ports()
    if not targets:
        return {}
    self_pid = os.getpid()
    summary: dict[int, list[int]] = {}
    for port in targets:
        if sys.platform == "win32":
            pids = _windows_pids_on_port(port)
        else:
            pids = _posix_pids_on_port(port)
        # Don't shoot ourselves if the supervisor happens to share a port
        # (it never does today; this is paranoia for future bind tests).
        pids.discard(self_pid)
        killed: list[int] = []
        for pid in sorted(pids):
            ok = _windows_kill(pid) if sys.platform == "win32" else _posix_kill(pid)
            if ok:
                killed.append(pid)
                log.info("port_cleanup: released port %d (pid=%d)", port, pid)
        summary[port] = killed
    return summary


__all__ = ["DEFAULT_PORTS", "release_ports"]
