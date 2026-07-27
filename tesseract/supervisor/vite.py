"""Run the Mirror Vite dev server as a supervised child of the supervisor.

Operator-requested: ``python -m tesseract.supervisor`` should also start
``pnpm run dev`` in ``tesseract/mirror/`` so the operator gets the
full stack (supervisor → Mirror backend → Vite dev server) with one
command. Vite's lifecycle is tied to the supervisor: when the
supervisor exits, Vite exits too. The port_cleanup helper already
frees 1420 on supervisor exit (commit ``2e27329``); this module makes
the spawn side symmetric.

Fail-soft: if ``pnpm`` is not on ``PATH``, the mirror dir is missing,
or the spawn raises for any other reason, log and continue — the
backend still works without Vite (operator can run it by hand in a
separate terminal).

Off by default in non-Windows environments to avoid surprising CI
runs; opt out anywhere with ``SUPERVISOR_DEV_VITE=0``.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)


VITE_PORT_DEFAULT = 1420


def _vite_enabled() -> bool:
    override = os.environ.get("SUPERVISOR_DEV_VITE")
    if override is None:
        # Default ON on Windows (operator's day-to-day environment) and
        # POSIX desktop; tests / CI override via SUPERVISOR_DEV_VITE=0.
        return True
    return override.lower() not in {"0", "false", "no", "off"}


def _resolve_pnpm() -> str | None:
    """Find ``pnpm`` on PATH. Windows installs it as ``pnpm.CMD``;
    ``shutil.which`` handles PATHEXT correctly so it returns the right
    extension on each platform."""
    return shutil.which("pnpm")


def _mirror_dir(repo_root: Path) -> Path:
    return repo_root / "tesseract" / "mirror"


def start_vite(repo_root: Path) -> subprocess.Popen | None:
    """Spawn ``pnpm run dev`` in ``tesseract/mirror/`` if enabled. Returns
    the live ``Popen`` (so the supervisor can terminate it on exit) or
    ``None`` when Vite is disabled / unavailable.

    The child inherits stdio from the supervisor's console so operator
    sees Vite's HMR output alongside supervisor logs — matches the
    "one supervisor tab + one Mirror tab" preference and keeps Vite
    out of yet another console.
    """
    if not _vite_enabled():
        log.info("vite: SUPERVISOR_DEV_VITE off — skipping spawn")
        return None
    mirror = _mirror_dir(repo_root)
    if not (mirror / "package.json").exists():
        log.info("vite: %s/package.json missing — skipping spawn", mirror)
        return None
    pnpm = _resolve_pnpm()
    if pnpm is None:
        log.info("vite: pnpm not on PATH — skipping spawn (run `pnpm run dev` manually)")
        return None
    cmd = [pnpm, "run", "dev"]
    kwargs: dict = {
        "cwd": str(mirror),
        "env": os.environ.copy(),
        # Inherit stdin/out/err so HMR output reaches the supervisor's
        # terminal. Operator wants supervisor + Vite in one tab.
        "stdin": subprocess.DEVNULL,
        "stdout": None,
        "stderr": None,
    }
    if sys.platform == "win32":
        # Same separate process-group pattern as the backend spawn so we
        # can deliver CTRL_BREAK_EVENT without taking down the supervisor.
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    try:
        proc = subprocess.Popen(cmd, **kwargs)  # noqa: S603
        log.info("vite: spawned `pnpm run dev` (pid=%d, cwd=%s)", proc.pid, mirror)
        return proc
    except (OSError, ValueError):
        log.exception("vite: spawn failed — backend continues without Vite")
        return None


def stop_vite(proc: subprocess.Popen | None, *, grace_seconds: float = 5.0) -> None:
    """Terminate a Vite child cleanly. Best-effort: a SIGKILL or stuck
    process must not block supervisor exit. Port_cleanup runs after
    this in the supervisor's finally block as a safety net."""
    if proc is None or proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            import signal as _signal
            os.kill(proc.pid, _signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
        else:
            proc.terminate()
    except OSError:
        log.exception("vite: graceful stop signal raised")
    try:
        proc.wait(timeout=grace_seconds)
        log.info("vite: exited cleanly (pid=%d)", proc.pid)
        return
    except subprocess.TimeoutExpired:
        pass
    log.warning("vite: did not exit gracefully — escalating to kill")
    try:
        proc.kill()
        proc.wait(timeout=2.0)
    except (OSError, subprocess.TimeoutExpired):
        log.exception("vite: kill() raised or timed out")


__all__ = ["VITE_PORT_DEFAULT", "start_vite", "stop_vite"]
