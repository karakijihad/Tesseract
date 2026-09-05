"""``python -m tesseract.scripts.shutdown`` -- operator clean-stop CLI.

Reads ``<TESSERACT_HOME>/runtime/supervisor.pid``, writes an
``intent.json {operator_quit, source: cli_tool}`` so the supervisor's
post-exit routing honors operator intent, writes the supervisor's
stop-request file, and waits for the process to actually go.

**Why a file and not a console signal.** This used to send
``CTRL_BREAK_EVENT`` via ``os.kill``, which on Windows is
``GenerateConsoleCtrlEvent`` -- it reaches a process group attached to the
CALLER's console and nothing else. Run from a second terminal, from a
detached shell, or from a service shim, it fails, and the fallback was
``os.kill(pid, SIGTERM)``, which Python implements on Windows as
``TerminateProcess``: a hard kill that runs no handler and no ``finally``.
Measured 2026-08-29. The supervisor vanished with no line in its own log,
its ``_stop_all_daemons`` never ran, ``stop_vite`` never ran, the port
release in ``supervisor/__main__.py`` never ran, and the backend and the
Vite dev server were left holding 8000 and 1420 until they were reaped by
hand. The thing whose job is to stop everything stopped only itself.

The stop-request file is the door the Tauri app already uses and it works
from any console, so there is one path now rather than one that works and
one that lies. The supervisor's watcher picks it up within a second and
routes to the same ``request_stop`` a Ctrl-C in its terminal would.

Distinct from sending Ctrl-C in the supervisor's terminal only in reach:
same result either way (supervisor exits zero, no respawn).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from tesseract.paths import TESSERACT_HOME
from tesseract.supervisor.daemon import CLEAN_STOP_CEILING_S
from tesseract.supervisor.intent import (
    IntentFile,
    clear_intent,
    intent_path,
    now_utc,
    runtime_dir,
    write_atomic,
)
from tesseract.supervisor.port_cleanup import DEFAULT_PORTS
from tesseract.supervisor.process_probe import pid_alive
from tesseract.supervisor.stop_watcher import supervisor_stop_request_path

# How long a clean stop is allowed to take before we stop calling it one.
# Taken from the supervisor rather than restated here: it is the sum of every
# bounded wait between the request and the process going, and it is made of
# constants that live in `daemon.py`. A number copied to this side would drift
# below the real ceiling the moment one of them moved, and this CLI would
# start reporting a slow stop as a failed one.
_STOP_DEADLINE_S = CLEAN_STOP_CEILING_S
_POLL_INTERVAL_S = 0.5


def _read_pid(home: Path) -> int | None:
    path = runtime_dir(home) / "supervisor.pid"
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _wait_for_exit(pid: int, deadline_s: float) -> bool:
    """Poll until the supervisor process is gone. True if it went."""
    deadline = time.monotonic() + deadline_s
    while True:
        if not pid_alive(pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_INTERVAL_S)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tesseract.scripts.shutdown",
        description=(
            "Ask the running supervisor to stop. Writes operator_quit "
            "intent, asks the supervisor to stop, and waits for it."
        ),
    )
    parser.add_argument(
        "--reason",
        default="operator-initiated shutdown",
        help="Free-text reason recorded in intent.json.",
    )
    args = parser.parse_args(argv)

    home = TESSERACT_HOME
    pid = _read_pid(home)
    if pid is None:
        print("shutdown: no supervisor running (no pid file)", file=sys.stderr)
        return 1
    if not pid_alive(pid):
        print(
            f"shutdown: supervisor pid {pid} is not running. Nothing was "
            f"changed. The pid file is left over from a hard kill and the "
            f"next start replaces it.",
            file=sys.stderr,
        )
        return 1

    # Write intent FIRST so the file is on disk before the supervisor's
    # watcher fires -- the supervisor reads it after backend exit, so a
    # request that arrives ahead of the intent would route as crash and
    # respawn.
    record = IntentFile(
        intent="operator_quit",
        timestamp=now_utc(),
        source="cli_tool",
        reason=args.reason,
    )
    intent_file = intent_path(home)
    write_atomic(intent_file, record)
    # Plain ASCII, and the reason is not taste. A Windows console is cp1252,
    # this print raised UnicodeEncodeError on the arrow that used to be here,
    # and the raise landed BETWEEN writing the intent and asking for the stop:
    # the supervisor was left running with an operator_quit file on disk, so
    # the next backend exit of any kind would have been read as the operator
    # asking to stop and nothing would have respawned. Measured 2026-08-29.
    print(f"shutdown: wrote operator_quit intent to {intent_file}")

    stop_file = supervisor_stop_request_path(home)
    try:
        stop_file.write_text("stop\n", encoding="utf-8")
    except OSError as exc:
        # Nothing was asked, so nothing may be left armed: an operator_quit
        # sitting on disk beside a live supervisor turns its next crash into
        # a silent stop.
        clear_intent(intent_file)
        print(
            f"shutdown: could not write the stop request at {stop_file}: "
            f"{exc}. The supervisor is still running and nothing was changed.",
            file=sys.stderr,
        )
        return 1

    print(
        f"shutdown: asked supervisor (pid={pid}) to stop, waiting up to "
        f"{int(_STOP_DEADLINE_S)} seconds"
    )
    if _wait_for_exit(pid, _STOP_DEADLINE_S):
        # `StopRequestWatcher._run` returns for good on the first request it
        # reads, so a second stop typed while the first was still working
        # writes a file nothing will ever consume: it would survive to the
        # next boot and stop THAT supervisor a second after it started. The
        # process is gone, so whatever is left here is ours to clear.
        stop_file.unlink(missing_ok=True)
        print(f"shutdown: supervisor stopped (pid={pid})")
        return 0

    # The watcher deletes the file the moment it reads it, so its absence
    # is proof the request was heard and its presence is proof it was not.
    # Two different things to tell the operator, and only one of them
    # leaves state to clean up.
    if not stop_file.exists():
        print(
            f"shutdown: supervisor (pid={pid}) took the stop request but is "
            f"still running after {int(_STOP_DEADLINE_S)} seconds. It is "
            f"most likely still stopping the backend. Check with "
            f"`python -m tesseract.supervisor --status`.",
            file=sys.stderr,
        )
        return 1

    stop_file.unlink(missing_ok=True)
    clear_intent(intent_file)
    ports = ", ".join(str(port) for port in DEFAULT_PORTS)
    print(
        f"shutdown: supervisor (pid={pid}) never read the stop request. It "
        f"is not stopping, and the app, the backend and the dev server are "
        f"all still running. The request and the quit intent were removed so "
        f"neither is left to catch the next start. End process {pid} "
        f"yourself, then free ports {ports} before starting again.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
