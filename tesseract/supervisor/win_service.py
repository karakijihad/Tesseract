"""Windows service shim wrapping ``tesseract.supervisor``.

AU-13. Lets the operator register the supervisor with the Service Control
Manager so it auto-starts at boot. Stop semantics match every other clean
shutdown route in the system: write ``intent.json {operator_quit, source:
service_control}`` first, signal the supervisor process group with
``CTRL_BREAK_EVENT``, then wait. ``operator_quit`` is absolute — the
service will not respawn the supervisor on its own; the SCM stops the
service cleanly with exit zero.

Crashes are handled by the supervisor itself (its crash-storm breaker +
backoff schedule are untouched). The service framework only sees the
supervisor process — when it exits, the service exits with the same code.

Imports are platform-guarded so importing this module on POSIX raises
loudly rather than ImportError-ing at module load. Both the service
class and the installer live behind ``if sys.platform == "win32"``
guards so the file can sit in the supervisor package without breaking
Linux test runs.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from tesseract.paths import TESSERACT_HOME
from tesseract.supervisor.intent import (
    IntentFile,
    intent_path,
    now_utc,
    runtime_dir,
    write_atomic,
)


SERVICE_NAME = "TesseractSupervisor"
SERVICE_DISPLAY_NAME = "Tesseract Supervisor"
SERVICE_DESCRIPTION = (
    "Out-of-process supervisor for the TESSERACT Mirror backend. "
    "Honors operator_quit absolutely — no respawn after operator-initiated stop."
)

# Grace window the service waits between writing intent + signal and
# force-terminating a wedged supervisor process. Sized 15s above the
# supervisor's own _GRACEFUL_STOP_GRACE_S (30s in daemon.py) so the
# supervisor's full backend-drain budget can elapse before we cut it
# off — without the headroom, the two 30s windows compete and the
# backend gets `terminate()`d mid-teardown.
_STOP_GRACE_S = 45.0

log = logging.getLogger(__name__)


def _read_supervisor_pid(home: Path) -> int | None:
    path = runtime_dir(home) / "supervisor.pid"
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _write_operator_quit_intent(home: Path, reason: str) -> None:
    record = IntentFile(
        intent="operator_quit",
        timestamp=now_utc(),
        # Reuses the existing ``cli_tool`` source — the service is functionally
        # a CLI invocation under the SCM. Reason field disambiguates in the
        # supervisor audit log.
        source="cli_tool",
        reason=reason,
    )
    write_atomic(intent_path(home), record)


def _signal_ctrl_break(pid: int) -> bool:
    """Send CTRL_BREAK_EVENT; fall back to SIGTERM. Returns True if the
    signal was delivered at all (even if the fallback fired)."""
    try:
        os.kill(pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
        return True
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
            return True
        except OSError:
            return False


if sys.platform == "win32":
    import servicemanager  # type: ignore[import-not-found]
    import win32event  # type: ignore[import-not-found]
    import win32service  # type: ignore[import-not-found]
    import win32serviceutil  # type: ignore[import-not-found]

    class TesseractSupervisorService(win32serviceutil.ServiceFramework):
        """Windows service wrapper around ``python -m tesseract.supervisor``."""

        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY_NAME
        _svc_description_ = SERVICE_DESCRIPTION

        def __init__(self, args: list[str]) -> None:
            super().__init__(args)
            self._stop_event = win32event.CreateEvent(None, 0, 0, None)
            self._proc: subprocess.Popen | None = None

        def SvcStop(self) -> None:  # noqa: N802 — pywin32 contract
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            home = TESSERACT_HOME
            try:
                _write_operator_quit_intent(home, reason="windows service stop")
            except Exception:
                log.exception("win_service: failed to write operator_quit intent")
            pid = _read_supervisor_pid(home)
            if pid is not None:
                _signal_ctrl_break(pid)
            elif self._proc is not None and self._proc.poll() is None:
                # No PID file but a live subprocess — the supervisor is
                # still starting up. Terminate it directly so the SCM sees
                # the service stop within its window.
                self._proc.terminate()
            else:
                # Stop arrived before the supervisor subprocess was ever
                # spawned. Intent is on disk; the SetEvent below wakes
                # _run_supervisor's wait loop, which will exit before it
                # touches self._proc thanks to the null-guard there.
                log.warning(
                    "win_service: SvcStop fired before supervisor spawn "
                    "(pid file absent, subprocess unborn) — intent.json written, "
                    "nothing to signal"
                )
            win32event.SetEvent(self._stop_event)

        def SvcDoRun(self) -> None:  # noqa: N802 — pywin32 contract
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, ""),
            )
            self._run_supervisor()

        def _run_supervisor(self) -> None:
            # Spawn the supervisor as a child so its existing signal +
            # intent routing stays unchanged. CREATE_NEW_PROCESS_GROUP
            # lets the service handler deliver CTRL_BREAK_EVENT in SvcStop.
            creationflags = 0
            if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
                creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP
            env = os.environ.copy()
            # Service sessions have no console window — backend must
            # stay headless. The supervisor reads this env at spawn
            # time (see ``__main__.separate_console``).
            env["SUPERVISOR_HEADLESS"] = "1"
            self._proc = subprocess.Popen(
                [sys.executable, "-m", "tesseract.supervisor"],
                cwd=str(Path(__file__).resolve().parents[2]),
                env=env,
                creationflags=creationflags,
            )

            # Block until either the SCM signals stop or the supervisor
            # exits on its own (crash storm latched, max respawns, etc.).
            while True:
                rc = win32event.WaitForSingleObject(self._stop_event, 1000)
                if rc == win32event.WAIT_OBJECT_0:
                    break
                if self._proc.poll() is not None:
                    log.info(
                        "win_service: supervisor exited rc=%s; ending service",
                        self._proc.returncode,
                    )
                    break

            # Stop signalled. Give the supervisor _STOP_GRACE_S to exit
            # cleanly, then terminate. The supervisor itself enforces a
            # 30s graceful stop on the backend; we sit 15s above so the
            # full drain budget gets to run.
            if self._proc is None:
                return
            deadline = time.monotonic() + _STOP_GRACE_S
            while self._proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.5)
            if self._proc.poll() is None:
                log.warning(
                    "win_service: supervisor did not exit within %.0fs; terminating",
                    _STOP_GRACE_S,
                )
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    self._proc.kill()

else:  # pragma: no cover — non-Windows import path
    TesseractSupervisorService = None  # type: ignore[assignment,misc]


def main(argv: list[str] | None = None) -> int:
    if sys.platform != "win32":
        print(
            "win_service: Windows-only. Use `python -m tesseract.supervisor` on this platform.",
            file=sys.stderr,
        )
        return 2
    import win32serviceutil  # type: ignore[import-not-found]
    # HandleCommandLine consumes argv directly; pass-through.
    win32serviceutil.HandleCommandLine(TesseractSupervisorService, argv=argv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
