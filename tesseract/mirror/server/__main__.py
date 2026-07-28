from __future__ import annotations

import logging
import json
import os
import faulthandler
import signal
import sys
import threading
import time
from pathlib import Path

from aiohttp import web

from tesseract.config_seed import (
    ensure_agents_seeded,
    ensure_config_seeded,
    ensure_env_seeded,
    ensure_memory_store_seeded,
    ensure_tars_workshop_seeded,
    ensure_vault_seeded,
    ensure_workspace_seeded,
)
from tesseract.mirror.server.app import create_app
from tesseract.mirror.server.config import load_server_config
from tesseract.paths import TESSERACT_HOME
from tesseract.scheduler.alarms import ensure_alarms_state_migrated


def _install_windows_break_handler() -> None:
    """Bridge SIGBREAK to SIGINT on Windows.

    AU-1: the supervisor delivers CTRL_BREAK_EVENT (not CTRL_C_EVENT)
    to the backend so its own console isn't taken down with it. Windows
    surfaces CTRL_BREAK_EVENT as SIGBREAK — aiohttp only listens for
    SIGINT/SIGTERM and would otherwise hard-exit with
    STATUS_CONTROL_C_EXIT, skipping ``_on_shutdown`` and losing the
    intent.json write. Reraising as SIGINT lets aiohttp's existing
    shutdown chain run unchanged.
    """
    if sys.platform != "win32" or not hasattr(signal, "SIGBREAK"):
        return

    def _on_break(_signum, _frame):  # type: ignore[no-untyped-def]
        # Best-effort raise on SIGINT so aiohttp's installed handler
        # catches it. If raising fails (no SIGINT handler yet), at
        # least translate to KeyboardInterrupt so the event loop
        # unblocks.
        try:
            signal.raise_signal(signal.SIGINT)
        except Exception:
            raise KeyboardInterrupt()

    signal.signal(signal.SIGBREAK, _on_break)  # type: ignore[attr-defined]


_STOP_REQUEST_POLL_S = 1.0
_STACK_DUMP_REQUEST_POLL_S = 1.0


def _stop_request_path() -> Path:
    home = Path(os.environ.get("TESSERACT_HOME") or TESSERACT_HOME).resolve()
    return home / "runtime" / "stop_request"


def _watch_stop_request() -> None:
    """Background thread that polls for a supervisor-written stop file.

    The supervisor can't deliver CTRL_BREAK_EVENT to a backend that has
    its own console (CREATE_NEW_CONSOLE). When it wants the backend to
    stop cleanly across that boundary, it writes
    ``<TESSERACT_HOME>/runtime/stop_request``; this thread polls for it
    and synthesizes a local SIGINT so aiohttp's existing on_shutdown
    chain (including ``lifecycle.write_shutdown_intent``) runs unchanged.

    Started before ``web.run_app`` so the watcher is live throughout the
    server's lifetime. Daemon thread — exits with the process.
    """
    path = _stop_request_path()
    log = logging.getLogger(__name__)
    log.info("mirror: stop-request watcher armed at %s", path)
    while True:
        try:
            if path.exists():
                log.info("mirror: stop_request seen — raising SIGINT")
                try:
                    path.unlink()
                except OSError:
                    pass
                try:
                    signal.raise_signal(signal.SIGINT)
                except Exception:
                    os._exit(0)
                return
        except OSError:
            pass
        time.sleep(_STOP_REQUEST_POLL_S)


def _watch_stack_dump_requests() -> None:
    """Service supervisor diagnostics requests without touching aiohttp.

    The supervisor cannot inspect another Python process portably. This
    backend-side thread polls for a request file and writes all Python thread
    stacks via faulthandler, which still works when the asyncio loop is blocked
    but the interpreter can schedule this watchdog thread.
    """
    home = Path(os.environ.get("TESSERACT_HOME") or TESSERACT_HOME).resolve()
    request_dir = home / "runtime" / "diagnostics"
    log_dir = home / "logs" / "supervisor"
    log = logging.getLogger(__name__)
    pid = os.getpid()
    log.info("mirror: stack-dump watcher armed at %s", request_dir)
    while True:
        try:
            for path in sorted(request_dir.glob(f"stack-dump-{pid}-*.json")):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    output_raw = payload.get("output_path")
                    if isinstance(output_raw, str) and output_raw:
                        output_path = Path(output_raw).resolve()
                        if not output_path.is_relative_to(home):
                            output_path = log_dir / f"backend-stack-{pid}.txt"
                    else:
                        output_path = log_dir / f"backend-stack-{pid}.txt"
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    with output_path.open("w", encoding="utf-8") as f:
                        f.write(
                            f"backend_pid={pid}\n"
                            f"request_path={path}\n"
                            f"reason={payload.get('reason')}\n\n"
                        )
                        faulthandler.dump_traceback(file=f, all_threads=True)
                    path.unlink(missing_ok=True)
                    log.warning("mirror: wrote supervisor-requested stack dump to %s", output_path)
                except Exception:
                    log.exception("mirror: stack-dump request failed for %s", path)
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
        except OSError:
            pass
        time.sleep(_STACK_DUMP_REQUEST_POLL_S)


def main() -> None:
    ensure_config_seeded()
    ensure_workspace_seeded()
    ensure_agents_seeded()
    ensure_env_seeded()
    ensure_memory_store_seeded()
    ensure_vault_seeded()
    ensure_tars_workshop_seeded()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Durable rotating file — the console handler above dies with the
    # supervisor's console; crash forensics need a file (logsetup.py).
    from tesseract.logsetup import attach_file_logging

    attach_file_logging("mirror-backend")
    # After logging is attached, not before: `ensure_alarms_state_migrated()`
    # logs its outcome, and a log call before any handler exists falls
    # through to `logging.lastResort` (bare stderr) — invisible once this
    # process detaches from its parent's console.
    ensure_alarms_state_migrated()
    # Janitor claim: a detached backend has a dead parent by design; the
    # pidfile keeps the orphan sweep off it (janitor/pidfile.py).
    from tesseract.janitor.pidfile import write_pidfile

    write_pidfile("mirror-backend")
    _install_windows_break_handler()
    threading.Thread(target=_watch_stop_request, name="stop-request-watcher", daemon=True).start()
    threading.Thread(target=_watch_stack_dump_requests, name="stack-dump-watcher", daemon=True).start()
    config = load_server_config()
    app = create_app(config)
    logging.getLogger(__name__).info("mirror server starting on %s:%s", config.host, config.port)
    web.run_app(app, host=config.host, port=config.port, print=None)


if __name__ == "__main__":  # pragma: no cover
    main()
