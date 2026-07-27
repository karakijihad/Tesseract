"""Deterministic fake backend for AU-1 supervisor tests.

Reads ``$FAKE_BACKEND_MODE`` and behaves accordingly:

* ``operator_quit`` — write intent.json {operator_quit, source: ui_button}
  then exit 0. Models a clean Mirror UI shutdown.
* ``crash`` — exit non-zero without writing any intent file. Models a
  hard crash (segfault, SIGKILL, missing intent).
* ``restart_upgrade`` — write intent.json {restart_upgrade,
  continuation_id: $FAKE_CONTINUATION_ID} then exit 0. Models the
  UpgradeManager (AU-9) path. Also reads
  ``$TESSERACT_RESUME_CONTINUATION`` and writes the value to
  ``$FAKE_BACKEND_RESUME_OUT`` so the test can assert the env var was
  passed on respawn.

Reads ``$FAKE_BACKEND_HOME`` to know where ``runtime/intent.json``
should land — tests pass a tmp dir to keep the real TESSERACT_HOME
untouched.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def _write_intent(home: Path, payload: dict) -> None:
    runtime_dir = home / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    path = runtime_dir / "intent.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _bump_spawn_counter(home: Path) -> int:
    """Append-only spawn ledger so tests can assert the supervisor
    actually respawned N times rather than inferring from exit codes."""
    counter_path = home / "spawn_counter.txt"
    n = 0
    if counter_path.exists():
        try:
            n = int(counter_path.read_text(encoding="utf-8").strip() or "0")
        except ValueError:
            n = 0
    n += 1
    counter_path.write_text(str(n), encoding="utf-8")
    return n


def main() -> int:
    mode = os.environ.get("FAKE_BACKEND_MODE", "crash")
    home = Path(os.environ["FAKE_BACKEND_HOME"])
    spawn_n = _bump_spawn_counter(home)

    # Record the resume continuation env to a per-spawn file so the
    # test can assert spawn #1 had no env and spawn #2 carried the id.
    resume = os.environ.get("TESSERACT_RESUME_CONTINUATION")
    resume_out_base = os.environ.get("FAKE_BACKEND_RESUME_OUT")
    if resume_out_base:
        base = Path(resume_out_base)
        per_spawn = base.with_name(f"{base.stem}.{spawn_n}{base.suffix}")
        per_spawn.write_text(resume or "", encoding="utf-8")
        # Also keep the last-spawn rollup at the original path for
        # tests that only need the final value.
        base.write_text(resume or "", encoding="utf-8")

    # Tiny startup pause so the supervisor's wait() actually enters the
    # poll loop before we exit — surfaces races faster.
    time.sleep(0.1)

    if mode == "operator_quit":
        _write_intent(home, {
            "intent": "operator_quit",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "ui_button",
            "backend_pid": os.getpid(),
            "reason": "fake_backend operator_quit",
        })
        return 0

    if mode == "restart_upgrade":
        continuation_id = os.environ.get("FAKE_CONTINUATION_ID", "ag-test-default")
        _write_intent(home, {
            "intent": "restart_upgrade",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "upgrade_manager",
            "continuation_id": continuation_id,
            "backend_pid": os.getpid(),
        })
        return 0

    if mode == "crash":
        # No intent file. Exit nonzero to model a hard crash.
        return 11

    if mode == "heartbeat_then_silent":
        # Models the real bug: backend binds /api/health, answers OK a
        # few times, then goes silent (event loop blocked, process hung,
        # etc.). When the supervisor's heartbeat thread gives up and
        # sends CTRL_BREAK_EVENT, our signal handler writes
        # intent=operator_quit — exactly what the real Mirror backend
        # does, because the OS signal is indistinguishable from operator
        # Ctrl-C. The supervisor MUST override this and respawn anyway.
        import http.server
        import signal
        import threading

        port = int(os.environ["FAKE_BACKEND_PORT"])
        answer_limit = int(os.environ.get("FAKE_BACKEND_ANSWER_LIMIT", "1"))
        answered = [0]

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 — stdlib API
                if self.path == "/api/health" and answered[0] < answer_limit:
                    answered[0] += 1
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"status":"ok"}')
                else:
                    # Past the answer limit → simulate a hung event loop
                    # by closing the connection without responding.
                    self.send_response(503)
                    self.end_headers()

            def log_message(self, *_args, **_kwargs) -> None:  # noqa: N802
                return None

        server = http.server.HTTPServer(("127.0.0.1", port), _Handler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        # Mimic the real backend: on SIGBREAK/SIGINT, write operator_quit
        # (because we can't tell who sent the signal) and exit 0.
        stop_event = threading.Event()

        def _on_signal(_signum, _frame) -> None:
            _write_intent(home, {
                "intent": "operator_quit",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": "fake_backend_signal",
                "backend_pid": os.getpid(),
                "reason": "supervisor sent signal (fake backend treats as operator)",
            })
            stop_event.set()

        signal.signal(signal.SIGINT, _on_signal)
        if sys.platform == "win32" and hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, _on_signal)  # type: ignore[attr-defined]

        # Block until signaled or until a watchdog timeout — never run
        # away if the supervisor never sends the kill.
        deadline = time.monotonic() + 30.0
        while not stop_event.is_set() and time.monotonic() < deadline:
            time.sleep(0.05)
        return 0

    if mode == "operator_quit_then_done":
        # After writing operator_quit, set the mode marker file so the
        # next spawn (which won't happen — this is the assertion!) would
        # be detectable.
        marker = home / "second_spawn_marker.txt"
        if marker.exists():
            marker.write_text("BUG: respawned after operator_quit", encoding="utf-8")
        else:
            marker.write_text("first_spawn", encoding="utf-8")
        _write_intent(home, {
            "intent": "operator_quit",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "ui_button",
            "backend_pid": os.getpid(),
        })
        return 0

    raise SystemExit(f"unknown FAKE_BACKEND_MODE={mode!r}")


if __name__ == "__main__":
    sys.exit(main())
