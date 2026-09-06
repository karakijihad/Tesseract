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
    unseed_copied_agents,
    unseed_copied_jobs,
    ensure_config_seeded,
    ensure_env_seeded,
    ensure_memory_store_seeded,
    ensure_workshop_seeded,
    ensure_vault_seeded,
    ensure_workspace_seeded,
)
from tesseract.mirror.server.app import create_app
from tesseract.mirror.server.config import load_server_config
from tesseract.paths import TESSERACT_HOME, install_root, runtime_dir
from tesseract.scheduler.alarms import ensure_alarms_state_migrated
from tesseract.supervisor.stack_dump import LOOP_WAS_DOING


def _install_windows_break_handler() -> None:
    """Bridge SIGBREAK to SIGINT on Windows.

    The supervisor delivers CTRL_BREAK_EVENT (not CTRL_C_EVENT)
    to the backend so its own console isn't taken down with it. Windows
    surfaces CTRL_BREAK_EVENT as SIGBREAK — aiohttp only listens for
    SIGINT/SIGTERM and would otherwise hard-exit with
    STATUS_CONTROL_C_EXIT, skipping ``_on_shutdown`` and losing the
    intent.json write. Reraising as SIGINT lets aiohttp's existing
    shutdown chain run unchanged.
    """
    if sys.platform != "win32" or not hasattr(signal, "SIGBREAK"):
        return

    fired = False

    def _on_break(_signum, _frame):  # type: ignore[no-untyped-def]
        # Once-latch (2026-07-30): a second CTRL_BREAK landing after
        # shutdown has begun would raise a KeyboardInterrupt in the
        # middle of aiohttp's cleanup chain, hard-killing the backend
        # (STATUS_CONTROL_C_EXIT) before teardown finished. The first
        # break starts the graceful shutdown; any repeat is ignored —
        # a genuinely wedged shutdown is the supervisor's kill
        # escalation to solve, not a second interrupt's.
        nonlocal fired
        if fired:
            return
        fired = True
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
# How far back the stall report in a dump looks. Wide enough to cover the
# supervisor's whole heartbeat budget, so a dump requested after twelve missed
# probes still describes the block that caused them.
_STALL_REPORT_WINDOW_S = 180.0
# The header line the supervisor reads back out. Declared in
# `supervisor/stack_dump.py`, which both processes import, because a writer and
# a reader in two processes agreeing by eye is how they stop agreeing.


def _stop_request_path() -> Path:
    return runtime_dir() / "stop_request"


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
                # Before the signal, never after. A turn dying on the way down
                # files its own account of why it stopped, and on 2026-08-31 it
                # did so three milliseconds after this line: `failed`, reason
                # "the turn ended without recording how", closed and therefore
                # out of reach of the recovery that exists to tell the person.
                # Saying it here is what lets that record stay open and honest.
                try:
                    from tesseract.orchestrator.turns import note_going_down

                    note_going_down()
                except Exception:  # noqa: BLE001 — never block a stop
                    log.warning("mirror: could not note the shutdown", exc_info=True)
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


def write_stack_dump(output_path: Path, *, header: str) -> None:
    """Write one thread dump, whole or not at all.

    A temp file and a rename, the same shape `_request_backend_stack_dump`
    already uses for the REQUEST, and for a sharper reason on this side. Two
    writers share this file and only one of them is buffered: Python holds the
    header in a block buffer until close, while `faulthandler.dump_traceback`
    goes straight at the file descriptor because it is built to work when the
    interpreter is in a bad way. So opening the final path directly puts a file
    on disk that exists, has stacks in it, and does not carry the header line
    yet.

    That window is the supervisor's whole question. `stack_dump.read_loop_report`
    retries while the file is missing and answers the moment it can read one, so
    a partial file does not make it wait, it makes it say the backend predates
    this record and stop asking. Renaming into place means a reader sees either
    nothing yet or the finished dump, and never the half second in between.
    """
    tmp = output_path.with_name(output_path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            f.write(header)
            faulthandler.dump_traceback(file=f, all_threads=True)
        os.replace(str(tmp), str(output_path))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _watch_stack_dump_requests() -> None:
    """Service supervisor diagnostics requests without touching aiohttp.

    The supervisor cannot inspect another Python process portably. This
    backend-side thread polls for a request file and writes all Python thread
    stacks via faulthandler, which still works when the asyncio loop is blocked
    but the interpreter can schedule this watchdog thread.
    """
    request_dir = runtime_dir() / "diagnostics"
    log_dir = runtime_dir() / "logs" / "supervisor"
    # Made BEFORE it is resolved. On Windows `resolve()` on a path that does
    # not exist yet skips the normalisation it would do for one that does, so
    # a bound resolved before the first dump could never match an output path
    # resolved after it, and a legitimate request would be refused for the
    # life of the watcher. On a fresh install, which is where a dump is most
    # likely to be wanted.
    log_dir.mkdir(parents=True, exist_ok=True)
    log_dir = log_dir.resolve()
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
                        # `install_root()` is the documented read boundary —
                        # the parent of home/, app/ and runtime/. The bound
                        # matters because the request file names its own
                        # output path, so without it a dump of every thread's
                        # stack could be written anywhere the backend can
                        # reach. This previously referenced an undefined
                        # `home`, so EVERY request raised NameError and the
                        # watcher has never produced a dump.
                        # Bounded to the SUPERVISOR LOG DIRECTORY, not to the
                        # install. The request file names its own output path,
                        # and the wider bound let any path inside the install
                        # be overwritten with a thread dump: a config file, the
                        # approvals ledger, anything under the sealed app tree.
                        # Only the supervisor writes these requests today and
                        # it always names this directory, so nothing legitimate
                        # is turned away; what changes is what a writer who
                        # should not be there could reach.
                        if not output_path.is_relative_to(log_dir.resolve()):
                            log.warning(
                                "stack dump output %s is not in %s — writing "
                                "there instead", output_path, log_dir,
                            )
                            output_path = log_dir / f"backend-stack-{pid}.txt"
                    else:
                        output_path = log_dir / f"backend-stack-{pid}.txt"
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    # The sampler's own account, first, because it is the one
                    # thing in this file a person can read. A faulthandler dump
                    # is every thread's stack with no ranking and no way to
                    # tell which of them is the loop; this line says what the
                    # loop thread has actually been sitting in. It is also what
                    # the supervisor lifts back out to name a heartbeat kill,
                    # and the only account of a hang that exists while it is
                    # still happening: the stall record is written by a
                    # coroutine, which a blocked loop cannot run.
                    try:
                        from tesseract.mirror.server.app import loop_stall_report

                        doing = loop_stall_report(_STALL_REPORT_WINDOW_S)
                    except Exception as exc:  # noqa: BLE001 — a dump is still owed
                        doing = f"the sampler could not be read ({type(exc).__name__})"
                    write_stack_dump(
                        output_path,
                        header=(
                            f"backend_pid={pid}\n"
                            f"request_path={path}\n"
                            f"reason={payload.get('reason')}\n"
                            f"{LOOP_WAS_DOING}{doing}\n\n"
                        ),
                    )
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
    unseed_copied_agents()
    unseed_copied_jobs()
    ensure_env_seeded()
    ensure_memory_store_seeded()
    ensure_vault_seeded()
    ensure_workshop_seeded()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Durable rotating file — the console handler above dies with the
    # supervisor's console; crash forensics need a file (logsetup.py).
    from tesseract.logsetup import (
        attach_file_logging,
        redact_credentials_in_logs,
        suppress_proactor_disconnect_noise,
    )

    attach_file_logging("mirror-backend")
    # After both handlers exist — the filter is attached per handler, so
    # anything armed later is not covered.
    redact_credentials_in_logs()
    suppress_proactor_disconnect_noise()
    # After logging is attached, not before: `ensure_alarms_state_migrated()`
    # logs its outcome, and a log call before any handler exists falls
    # through to `logging.lastResort` (bare stderr) — invisible once this
    # process detaches from its parent's console.
    ensure_alarms_state_migrated()
    # Same reasoning, and before anything resolves a voice model path: the
    # weights moved out of the swapped `app/` tree, and without this the
    # first launch after that change would re-download ~2 GB already on disk.
    # Idempotent — a no-op on every launch after the first.
    from tesseract.voice.model_files import migrate_legacy_models

    migrate_legacy_models()
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
