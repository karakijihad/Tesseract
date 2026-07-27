"""File-based stop trigger for the supervisor.

A GUI parent (the Tauri app) has no console, so it cannot deliver
CTRL_BREAK_EVENT to the supervisor's process group (see daemon.py). This
mirrors the backend's own ``runtime/stop_request`` pattern: the parent
writes ``<TESSERACT_HOME>/runtime/supervisor_stop_request`` and this
watcher turns its appearance into a graceful ``request_stop()``.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

from tesseract.supervisor.intent import runtime_dir

log = logging.getLogger(__name__)


def supervisor_stop_request_path(tesseract_home: Path) -> Path:
    return runtime_dir(tesseract_home) / "supervisor_stop_request"


class StopRequestWatcher:
    def __init__(
        self,
        tesseract_home: Path,
        on_stop: Callable[[], None],
        poll_interval_s: float = 1.0,
    ) -> None:
        self._home = tesseract_home
        self._on_stop = on_stop
        self._interval = poll_interval_s
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def _check_once(self) -> bool:
        req = supervisor_stop_request_path(self._home)
        if not req.exists():
            return False
        try:
            req.unlink()
        except OSError:
            log.warning("stop-request watcher: could not remove %s", req)
        log.info("stop-request watcher: stop file seen — requesting supervisor stop")
        self._on_stop()
        return True

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                if self._check_once():
                    return
            except Exception:  # noqa: BLE001 — watcher must never crash the supervisor
                log.exception("stop-request watcher: check raised")
            self._stop_event.wait(timeout=self._interval)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="supervisor-stop-watcher", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
