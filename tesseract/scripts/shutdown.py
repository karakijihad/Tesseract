"""``python -m tesseract.scripts.shutdown`` — operator clean-stop CLI.

Reads ``<TESSERACT_HOME>/runtime/supervisor.pid``, writes an
``intent.json {operator_quit, source: cli_tool}`` so the supervisor's
post-exit routing honors operator intent, and sends SIGTERM (POSIX) or
CTRL_BREAK_EVENT (Windows) to the supervisor process. The supervisor's
own signal handler then propagates the stop to the backend.

Distinct from sending Ctrl-C in the supervisor's terminal — useful
when the supervisor was launched from a service shim or detached
shell. Same result either way (supervisor exits zero, no respawn).
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path

from tesseract.paths import TESSERACT_HOME
from tesseract.supervisor.intent import (
    IntentFile,
    intent_path,
    now_utc,
    runtime_dir,
    write_atomic,
)


def _read_pid(home: Path) -> int | None:
    path = runtime_dir(home) / "supervisor.pid"
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _signal_supervisor(pid: int) -> None:
    if sys.platform == "win32":
        # CTRL_BREAK_EVENT requires the target to be in the same
        # process group OR the same console as the sender. The
        # operator CLI is normally launched from the operator's own
        # terminal — sufficient for the common case. If it isn't
        # (e.g. service shim), fall back to a generic terminate via
        # `os.kill(pid, signal.SIGTERM)` which Python translates to
        # TerminateProcess on Windows; that's a hard kill but the
        # intent.json we just wrote still routes the supervisor's
        # post-exit code path on the NEXT start.
        try:
            os.kill(pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
            return
        except OSError:
            os.kill(pid, signal.SIGTERM)
            return
    os.kill(pid, signal.SIGTERM)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tesseract.scripts.shutdown",
        description=(
            "Ask the running supervisor to stop. Writes operator_quit "
            "intent and signals the supervisor process."
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

    # Write intent FIRST so the file is on disk before the signal
    # reaches the supervisor's handler — the supervisor reads it after
    # backend exit, so an early signal that finds no intent would
    # route as crash and respawn.
    record = IntentFile(
        intent="operator_quit",
        timestamp=now_utc(),
        source="cli_tool",
        reason=args.reason,
    )
    write_atomic(intent_path(home), record)
    print(f"shutdown: wrote operator_quit intent → {intent_path(home)}")

    try:
        _signal_supervisor(pid)
    except ProcessLookupError:
        print(f"shutdown: pid {pid} no longer alive — supervisor may have already exited", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"shutdown: signal to pid {pid} failed: {exc}", file=sys.stderr)
        return 1

    print(f"shutdown: signaled supervisor (pid={pid})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
