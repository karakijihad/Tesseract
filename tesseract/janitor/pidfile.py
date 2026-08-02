"""Claim files — how intentionally-detached processes survive the janitor.

A detached supervisor / backend has a dead parent by design, so the
orphan rule alone would flag it. Long-lived entry points call
`write_pidfile(name)` at boot; the process sweep skips any live pid
claimed in `<home>/run/*.pid` whose command line still looks like
Tesseract (pid-reuse guard). Stale files are harmless — a dead or
reused pid claims nothing."""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def _run_dir() -> Path:
    from tesseract.paths import TESSERACT_HOME, runtime_dir

    env = os.environ.get("TESSERACT_HOME")
    home = Path(env).resolve() if env else TESSERACT_HOME
    return runtime_dir() / "run"


def write_pidfile(name: str) -> None:
    """Best-effort — never raises into the caller's boot path."""
    try:
        path = _run_dir() / f"{name}.pid"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        log.warning("janitor: could not write pidfile %s", name, exc_info=True)


def claimed_pids() -> set[int]:
    import psutil

    claimed: set[int] = set()
    run_dir = _run_dir()
    if not run_dir.is_dir():
        return claimed
    for path in run_dir.glob("*.pid"):
        try:
            pid = int(path.read_text(encoding="utf-8").strip())
            proc = psutil.Process(pid)
            # Pid-reuse guard: the claim only holds while the pid still
            # runs something Tesseract-shaped.
            if "tesseract" in " ".join(proc.cmdline()).lower():
                claimed.add(pid)
        except (OSError, ValueError, psutil.Error):
            continue
    return claimed
