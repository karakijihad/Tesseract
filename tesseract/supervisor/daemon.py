"""Supervisor daemon — spawns the Mirror backend, watches it, routes
on exit per the intent file.

Sync loop with ``time.sleep``; the supervisor is small enough that
asyncio buys nothing. Heartbeat polls happen on a separate thread
(stdlib only — no event-loop dependency so the supervisor stays
independent of the backend's runtime).

AU-1 Session 1 ships:
- spawn + signal + heartbeat
- ``operator_quit`` routing (exit zero, no respawn)
- ``crash`` routing (respawn with exponential backoff)
- ``restart_upgrade`` routing (respawn with ``TESSERACT_RESUME_CONTINUATION``)

Crash-storm circuit breaker + ``--force`` clear + UI shutdown route
land in Session 2.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from tesseract.supervisor.breaker import CrashStormBreaker
from tesseract.supervisor.console_capture import (
    ConsoleWriter,
    popen_capture_kwargs,
    start_drain,
)
from tesseract.supervisor.intent import (
    IntentFile,
    intent_path,
    clear_intent,
    read_with_staleness_check,
    runtime_dir,
)
from tesseract.supervisor.stop_watcher import StopRequestWatcher

log = logging.getLogger(__name__)


# Crash-backoff schedule from kill-switch-protocol.md: 5s, 30s, 120s,
# capped at 300s for subsequent retries.
_CRASH_BACKOFF_S = (5.0, 30.0, 120.0, 300.0)

# Heartbeat: poll every 10s. Three consecutive misses are a soft incident
# only: record diagnostics and keep watching. A backend restart is reserved
# for a sustained outage at the hard limit. This keeps transient event-loop
# stalls from forcing a full Mirror reconnect cycle while still recovering
# from a genuinely wedged backend.
_HEARTBEAT_INTERVAL_S = 10.0
_HEARTBEAT_TIMEOUT_S = 5.0
_HEARTBEAT_SOFT_FAILURES = 3
_HEARTBEAT_MAX_FAILURES = 12
# Boot grace — connection-refused misses during the first N seconds
# after spawn do NOT count toward the failure budget. The async
# on_startup refactor (3b230c1) made the listener bind in <1s in
# theory, but cold Python imports on Windows (antivirus + .pyc
# compile + the route-module import tree at the top of app.py)
# still push first-bind out to 20-40s on real installs. Without
# the grace the supervisor SIGKILLs a backend that is booting
# correctly. The grace expires the moment the listener answers
# once, so "came up then went silent" still counts immediately.
_HEARTBEAT_BOOT_GRACE_S = 120.0

# Grace window after SIGTERM before SIGKILL — both for backend stop
# and for the supervisor's own SIGINT-during-grace second-Ctrl-C path.
_GRACEFUL_STOP_GRACE_S = 30.0

# Time we'll wait for the backend's intent.json to land on disk after
# the process exits — the file may still be flushing.
_INTENT_FLUSH_GRACE_S = 2.0


@dataclass
class HeartbeatProbe:
    ok: bool
    latency_ms: float
    status: int | None = None
    error_type: str | None = None
    error: str | None = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "ok": self.ok,
            "latency_ms": round(self.latency_ms, 3),
        }
        if self.status is not None:
            payload["status"] = self.status
        if self.error_type:
            payload["error_type"] = self.error_type
        if self.error:
            payload["error"] = self.error[:500]
        return payload


@dataclass
class BackendProcess:
    """One backend invocation. ``proc`` is the live Popen; ``started_at``
    is the UTC time we recorded the spawn so we can detect stale intent
    files written by a prior run.
    """

    proc: subprocess.Popen
    started_at: datetime
    continuation_id: str | None = None
    health_failures: int = 0
    heartbeat_incident_reported: bool = False
    last_probe: HeartbeatProbe | None = None
    # Set by ``_heartbeat_loop`` when it gives up on the backend and
    # terminates it. The OS signal we deliver (CTRL_BREAK_EVENT on
    # Windows, SIGTERM on POSIX) is indistinguishable from an operator
    # Ctrl-C at the backend, so the backend writes ``intent=operator_quit``
    # in either case. Without this flag the main loop would honor that
    # intent and exit zero — defeating the whole point of the supervisor.
    # When set, ``_classify`` routes the exit as ``crash`` regardless of
    # intent so the respawn + backoff path runs.
    heartbeat_killed: bool = False
    # Set by ``_terminate_backend`` the moment it delivers the stop
    # signal. Makes termination idempotent: at quit, BOTH the stop-watcher
    # thread (``request_stop``) and the main loop (``_wait_for_exit``)
    # used to call ``_terminate_backend``, delivering a second
    # CTRL_BREAK ~1s into the backend's graceful shutdown — which raised
    # a KeyboardInterrupt mid-cleanup and hard-killed it
    # (STATUS_CONTROL_C_EXIT, observed live 2026-07-30). The second
    # caller now only waits.
    stop_signalled: bool = False


@dataclass
class Supervisor:
    """The supervisor's whole state. Construct with paths + port +
    backend module reference; call :meth:`run` to enter the main loop.

    Test fixtures inject ``tesseract_home`` to point at a tmp dir so
    intent files / crash markers don't leak into the operator's real
    runtime tree.
    """

    tesseract_home: Path
    health_url: str = "http://127.0.0.1:8000/api/health"
    mirror_module: str = "tesseract.mirror.server"
    # Test hook — when set, overrides the default ``[sys.executable, "-m",
    # mirror_module]`` so a unit test can spawn a deterministic fake
    # backend instead of the real Mirror.
    backend_cmd: list[str] | None = None
    extra_env: dict[str, str] = field(default_factory=dict)
    # Skip /api/health polling. Tests against a fake backend don't run
    # an HTTP listener; production always polls.
    heartbeat_enabled: bool = True
    # When True on Windows, spawn the backend with CREATE_NEW_CONSOLE so
    # the operator sees two visible terminal windows (supervisor's own
    # + the backend's own). Tests pass False to keep test runs quiet.
    separate_console: bool = False
    # Maximum total respawns this Supervisor instance is willing to do.
    # Set to a small int in tests so test 2 (crash auto-restarts) doesn't
    # loop forever if the test subprocess keeps crashing.
    max_respawns: int = 100
    # TC-4 + cockpit X-2 (2026-06-02): controller-daemon sibling defaults
    # ON. Operator opt-out: set ``SUPERVISOR_DISABLE_CONTROLLER=1`` (honored at the
    # ``__main__`` boot site so the constructor stays a pure dataclass).
    controller_daemon_enabled: bool = True
    controller_daemon_cmd: list[str] | None = None
    max_controller_daemon_respawns: int = 10

    _stop_event: threading.Event = field(default_factory=threading.Event, init=False)
    _current: BackendProcess | None = field(default=None, init=False)
    # Console capture (2026-07-29) — one rotating writer per child name,
    # shared across respawns. ``None`` until first spawn; stays ``None``
    # when capture setup fails (diagnostics are fail-soft).
    _console_writers: dict[str, ConsoleWriter | None] = field(
        default_factory=dict, init=False
    )
    _heartbeat_thread: threading.Thread | None = field(default=None, init=False)
    _crash_count: int = field(default=0, init=False)
    _shutdown_intent: str | None = field(default=None, init=False)
    _breaker: CrashStormBreaker | None = field(default=None, init=False)
    # TC-4 — controller daemon sibling lifecycle state.
    _controller_proc: subprocess.Popen | None = field(default=None, init=False)
    last_controller_pid: int | None = field(default=None, init=False)
    _controller_respawn_count: int = field(default=0, init=False)
    _controller_watchdog_thread: threading.Thread | None = field(
        default=None, init=False
    )
    _controller_stop_event: threading.Event = field(
        default_factory=threading.Event, init=False
    )
    _stop_watcher: StopRequestWatcher | None = field(default=None, init=False)

    # -- public surface ----------------------------------------------------

    def run(self) -> int:
        """Main loop. Returns the supervisor exit code.

        0 = operator_quit honored, 1 = max_respawns reached, 2 =
        crash-storm latched, other = fatal error.
        """
        self._install_signal_handlers()
        self._stop_watcher = StopRequestWatcher(
            self.tesseract_home,
            on_stop=lambda: self.request_stop(source="supervisor_signal"),
        )
        self._stop_watcher.start()
        log.info("supervisor: starting (home=%s)", self.tesseract_home)
        if self._breaker is None:
            self._breaker = CrashStormBreaker(tesseract_home=self.tesseract_home)
        # Janitor claim first (a detached supervisor is an orphan by design),
        # then the boot sweep (Docs/Plan/janitor/PLAN.md): reap fingerprinted
        # orphans / scratch / stale sessions from the previous run BEFORE
        # spawning fresh daemons. Best-effort — a janitor failure must never
        # block or crash boot.
        try:
            from tesseract.janitor import run_sweep
            from tesseract.janitor.pidfile import write_pidfile

            write_pidfile("supervisor")
            report = run_sweep(dry_run=False)
            log.info("supervisor: boot janitor sweep — %s", report.summary())
        except Exception:  # noqa: BLE001
            log.exception("supervisor: boot janitor sweep failed — continuing")
        # TC-4 dispatcher rollout (2026-05-24): controller daemon is now
        # part of the standard supervisor stack so `tars` in any terminal
        # attaches to it without manual `python -m tesseract.scripts.tars_controller`.
        # Failure must NOT crash the supervisor — the controller daemon
        # is an independent failure surface. The dispatcher's
        # `ensure_daemon_running` is a safety net: if the supervisor's
        # spawn failed, the first
        # `tars` invocation spawns one itself.
        if self.controller_daemon_enabled:
            try:
                self._spawn_controller_daemon()
                self._start_controller_daemon_watchdog()
            except Exception:  # noqa: BLE001
                log.exception(
                    "supervisor: controller daemon failed to start — "
                    "continuing without it (tars CLI will self-bootstrap)"
                )
                self._controller_proc = None
        respawns = 0
        while not self._stop_event.is_set():
            try:
                backend = self._spawn_backend(
                    continuation_id=self._pop_continuation_id(),
                )
            except Exception:  # noqa: BLE001
                # Popen itself failed (rare — bad cmd, env too long, OS
                # refused to fork). Treat as a crash so the breaker can
                # latch if it keeps happening, then back off and retry.
                log.exception("supervisor: backend spawn failed — backoff and retry")
                self._crash_count += 1
                respawns += 1
                try:
                    if self._breaker.record_crash(exit_code=-1):
                        log.error("supervisor: crash storm latched on spawn failure — exiting 2")
                        self._stop_all_daemons()
                        return 2
                except Exception:  # noqa: BLE001
                    log.exception("supervisor: crash-breaker raised — continuing")
                self._sleep_backoff(self._crash_count)
                if respawns >= self.max_respawns:
                    log.warning("supervisor: max_respawns reached, exiting")
                    self._stop_all_daemons()
                    return 1
                continue
            self._current = backend
            try:
                self._start_heartbeat(backend)
                exit_code = self._wait_for_exit(backend)
                self._stop_heartbeat()
                # Brief grace so the intent file's tmp-rename finishes.
                time.sleep(_INTENT_FLUSH_GRACE_S)
                intent = self._read_intent(backend)
                decision = self._classify(
                    intent, exit_code, heartbeat_killed=backend.heartbeat_killed,
                )
                # A stop WE initiated is planned by definition — the intent
                # file is corroboration, not the deciding vote. Depending on
                # it alone misclassified every update-stop and quit as a
                # crash (observed live 2026-07-30: intent written by the
                # backend yet read as None), sending quits through crash
                # backoff until the shell force-killed the supervisor.
                # Heartbeat kills keep their crash routing: that terminate
                # is a recovery action, not a plan.
                if (
                    decision == "crash"
                    and backend.stop_signalled
                    and not backend.heartbeat_killed
                ):
                    log.info(
                        "supervisor: exit followed our own stop signal — "
                        "classifying as operator_quit (intent file said %s)",
                        getattr(intent, "intent", None),
                    )
                    decision = "operator_quit"
                # Capture the continuation id IMMEDIATELY after classify
                # so a downstream raise (clear_intent, log formatting, etc.)
                # in the catch-all path below can't lose the resume token
                # across the bounce.
                if decision == "restart_upgrade" and intent is not None:
                    self._pending_continuation_id = intent.continuation_id
                log.info(
                    "supervisor: backend exited (code=%s, intent=%s, decision=%s, heartbeat_killed=%s)",
                    exit_code, getattr(intent, "intent", None), decision,
                    backend.heartbeat_killed,
                )
                self._current = None
                try:
                    clear_intent(intent_path(self.tesseract_home))
                except Exception:  # noqa: BLE001
                    log.exception("supervisor: clear_intent raised — ignoring (will overwrite next spawn)")
                if decision == "operator_quit":
                    self._shutdown_intent = "operator_quit"
                    log.info("supervisor: operator_quit honored")
                    try:
                        from tesseract.orchestrator.tars_controller.shutdown import (
                            teardown_all_controller_sessions,
                        )
                        from tesseract.orchestrator.tars_controller.sessions import SessionRegistry

                        _reg = SessionRegistry()
                        teardown_all_controller_sessions(
                            list_fn=lambda: _reg.list_sessions(status="active"),
                            delete_fn=_reg.delete_session,
                        )
                    except Exception:  # noqa: BLE001
                        log.exception("supervisor: teardown_all_controller_sessions raised — continuing shutdown")
                    self._stop_all_daemons()
                    return 0
                if decision == "restart_upgrade":
                    respawns += 1
                else:  # crash
                    self._crash_count += 1
                    respawns += 1
                    # Inline the backend's last console lines so one file
                    # (supervisor.log) carries the whole crash story — the
                    # tail is what a remote "it just says exited code=1"
                    # report can never reconstruct otherwise.
                    writer = self._console_writers.get("backend")
                    if writer is not None and writer.tail:
                        log.error(
                            "supervisor: backend crash output (last %d console lines):\n%s",
                            len(writer.tail), writer.tail_text(),
                        )
                    # Record into the rolling crash window. Three crashes
                    # in CRASH_WINDOW_SECONDS → latch + exit 2; the operator
                    # has to clear the marker before the next supervisor
                    # start succeeds.
                    try:
                        if self._breaker.record_crash(exit_code=exit_code):
                            log.error("supervisor: crash storm latched — exiting 2")
                            self._stop_all_daemons()
                            return 2
                    except Exception:  # noqa: BLE001
                        log.exception("supervisor: crash-breaker raised — continuing")
                    self._sleep_backoff(self._crash_count)
            except Exception:  # noqa: BLE001
                # Catch-all so a single bad iteration (corrupted intent
                # JSON, transient filesystem error, etc.) cannot crash
                # the supervisor — its job is to keep running.
                log.exception("supervisor: iteration raised — recovering and continuing")
                self._current = None
                try:
                    self._stop_heartbeat()
                except Exception:  # noqa: BLE001
                    log.debug("supervisor: stop_heartbeat raised in recovery path", exc_info=True)
                respawns += 1
                # Short fixed pause instead of crash backoff — we don't
                # know if the backend actually crashed.
                time.sleep(1.0)
            if respawns >= self.max_respawns:
                log.warning("supervisor: max_respawns reached, exiting")
                # SU-3b chunk 12: tear down siblings on any exit path.
                self._stop_all_daemons()
                return 1
        # Normal stop (operator_quit, signal). Tear down siblings too.
        self._stop_all_daemons()
        return 0

    def request_stop(self, *, source: str = "supervisor_signal") -> None:
        """Called by signal handlers (Ctrl-C in the supervisor terminal).

        Marks the loop for exit and signals the backend so its own
        clean-shutdown path writes ``intent.json`` with the matching
        source.
        """
        log.info("supervisor: stop requested (source=%s)", source)
        self._stop_event.set()
        if self._current is not None:
            self._terminate_backend(self._current)

    # -- TC-4: controller daemon sibling --------------------------------

    def _spawn_controller_daemon(self) -> None:
        """Spawn the TARS controller daemon as a sibling process.

        Token-on-disk before ``Popen`` so the daemon can read it on launch
        (Contract #10); CREATE_NEW_PROCESS_GROUP on Windows / start_new_session
        on POSIX so the daemon survives a backend SIGTERM and a Ctrl-C
        delivered to the supervisor's terminal does not cascade into
        the controller.
        """
        # Lazy import so the supervisor's hot startup path doesn't pull
        # the controller package's pydantic chain until controller mode
        # is actually enabled.
        from tesseract.orchestrator.tars_controller import auth as _ctrl_auth

        token = _ctrl_auth.mint_token()
        env = os.environ.copy()
        env.update(self.extra_env)
        env["TESSERACT_HOME"] = str(self.tesseract_home)

        prior_home = os.environ.get("TESSERACT_HOME")
        os.environ["TESSERACT_HOME"] = str(self.tesseract_home)
        try:
            _ctrl_auth.write_token(token)
        finally:
            if prior_home is None:
                os.environ.pop("TESSERACT_HOME", None)
            else:
                os.environ["TESSERACT_HOME"] = prior_home

        cmd = list(self.controller_daemon_cmd) if self.controller_daemon_cmd else [
            sys.executable, "-m", "tesseract.scripts.tars_controller",
        ]
        log.info("supervisor: spawning controller daemon cmd=%s", cmd)
        kwargs: dict = {
            "env": env,
            "stdout": None,
            "stderr": None,
            "stdin": subprocess.DEVNULL,
        }
        controller_console = (
            None if self.separate_console else self._console_writer("tars-controller")
        )
        if controller_console is not None:
            kwargs.update(popen_capture_kwargs())
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(cmd, **kwargs)  # noqa: S603
        if controller_console is not None:
            start_drain(proc, controller_console, "tars-controller")
        self._controller_proc = proc
        self.last_controller_pid = proc.pid

    def _stop_controller_daemon(self) -> None:
        """Idempotent teardown for the controller daemon."""
        self._controller_stop_event.set()
        proc = self._controller_proc
        if proc is not None and proc.poll() is None:
            try:
                if sys.platform == "win32":
                    proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
                else:
                    proc.terminate()
            except Exception:  # noqa: BLE001
                log.debug("supervisor: controller daemon terminate raised", exc_info=True)
            try:
                proc.wait(timeout=_GRACEFUL_STOP_GRACE_S)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    log.debug("supervisor: controller daemon kill raised", exc_info=True)
        self._controller_proc = None
        t = self._controller_watchdog_thread
        if t is not None and t.is_alive():
            t.join(timeout=3.0)
        self._controller_watchdog_thread = None

    def _stop_all_daemons(self) -> None:
        """Idempotent teardown for every sibling daemon the supervisor
        owns. Single helper so every exit path tears the controller
        daemon down consistently."""
        try:
            self._stop_controller_daemon()
        except Exception:  # noqa: BLE001
            log.debug(
                "supervisor: controller daemon teardown raised", exc_info=True
            )
        if self._stop_watcher is not None:
            self._stop_watcher.stop()

    def _start_controller_daemon_watchdog(self) -> None:
        """Respawn the controller if it dies during the supervisor's run.

        Independent failure domain from the backend's crash-storm
        breaker — controller flakiness must not nuke the operator's
        session.
        """
        self._controller_stop_event.clear()

        def _loop() -> None:
            while not self._controller_stop_event.is_set():
                proc = self._controller_proc
                if proc is None:
                    return
                rc = proc.poll()
                if rc is not None:
                    if self._controller_stop_event.is_set():
                        return  # operator-initiated stop
                    if (
                        self._controller_respawn_count
                        >= self.max_controller_daemon_respawns
                    ):
                        log.error(
                            "supervisor: controller daemon exceeded respawn "
                            "budget (%d) — giving up",
                            self.max_controller_daemon_respawns,
                        )
                        return
                    self._controller_respawn_count += 1
                    log.warning(
                        "supervisor: controller daemon exited (code=%s) — "
                        "respawn %d/%d",
                        rc,
                        self._controller_respawn_count,
                        self.max_controller_daemon_respawns,
                    )
                    try:
                        self._spawn_controller_daemon()
                    except Exception:  # noqa: BLE001
                        log.exception(
                            "supervisor: controller daemon respawn failed"
                        )
                        return
                self._controller_stop_event.wait(timeout=2.0)

        t = threading.Thread(
            target=_loop, name="tars-controller-watchdog", daemon=True
        )
        self._controller_watchdog_thread = t
        t.start()

    # -- spawn / signal ----------------------------------------------------

    _pending_continuation_id: str | None = None

    def _pop_continuation_id(self) -> str | None:
        cid = self._pending_continuation_id
        self._pending_continuation_id = None
        return cid

    def _console_writer(self, name: str) -> ConsoleWriter | None:
        """Lazily create (once, cached across respawns) the rotating
        console log for a named child. Fail-soft: a capture that cannot
        be set up must never block a spawn — the child just runs with
        discarded stdio like it always did before 2026-07-29.
        """
        if name not in self._console_writers:
            try:
                self._console_writers[name] = ConsoleWriter(self.tesseract_home, name)
            except Exception:  # noqa: BLE001
                log.exception(
                    "supervisor: console capture unavailable for %s — continuing without it",
                    name,
                )
                self._console_writers[name] = None
        return self._console_writers[name]

    def _spawn_backend(self, *, continuation_id: str | None) -> BackendProcess:
        env = os.environ.copy()
        env.update(self.extra_env)
        if continuation_id:
            env["TESSERACT_RESUME_CONTINUATION"] = continuation_id
        kwargs: dict = {
            "env": env,
            "stdout": None,
            "stderr": None,
            "stdin": subprocess.DEVNULL,
        }
        # Console capture (2026-07-29): in the packaged app the inherited
        # streams go nowhere, so an import-time traceback was invisible —
        # the pywinpty crash storm shipped undiagnosable. Merge stderr
        # into a drained pipe unless the operator asked for a visible
        # console window (separate_console keeps the old inherit).
        backend_console = None if self.separate_console else self._console_writer("backend")
        if backend_console is not None:
            kwargs.update(popen_capture_kwargs())
        if sys.platform == "win32":
            # Separate process group so we can deliver CTRL_BREAK_EVENT
            # to the backend without taking down the supervisor's own
            # console. The actual signal is sent in _terminate_backend
            # via os.kill(pid, signal.CTRL_BREAK_EVENT) — note that
            # subprocess.Popen.terminate() on Windows is TerminateProcess
            # (hard kill, no chance to write intent.json), NOT a console
            # ctrl event, which is why we don't use it.
            flags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            if self.separate_console:
                # CREATE_NEW_CONSOLE gives the backend its own visible
                # window so the operator can watch both processes
                # simultaneously. Stdio is reset to that console's
                # streams — don't pipe stdin/stdout to DEVNULL when
                # this flag is on, or the backend's prints disappear.
                flags |= subprocess.CREATE_NEW_CONSOLE  # type: ignore[attr-defined]
                kwargs["stdout"] = None
                kwargs["stderr"] = None
                kwargs["stdin"] = None
            kwargs["creationflags"] = flags
        else:
            kwargs["start_new_session"] = True
        cmd = list(self.backend_cmd) if self.backend_cmd else [
            sys.executable, "-m", self.mirror_module,
        ]
        log.info("supervisor: spawning backend cmd=%s continuation=%s", cmd, continuation_id)
        proc = subprocess.Popen(cmd, **kwargs)  # noqa: S603
        if backend_console is not None:
            start_drain(proc, backend_console, "backend")
        return BackendProcess(
            proc=proc,
            started_at=datetime.now(timezone.utc),
            continuation_id=continuation_id,
        )

    def _terminate_backend(self, backend: BackendProcess) -> None:
        """Graceful stop. Two paths on Windows depending on whether the
        backend lives in a shared or separate console:

        * Shared console (default tests + ``SUPERVISOR_HEADLESS=1``) —
          deliver ``CTRL_BREAK_EVENT`` via ``os.kill``. Fastest path; the
          backend's SIGBREAK→SIGINT bridge unblocks aiohttp's shutdown.
        * Separate console (production ``tesseract-start.bat``) — write
          ``<TESSERACT_HOME>/runtime/stop_request``. The backend's stop-
          request watcher picks it up within ~1s and synthesizes SIGINT.
          ``GenerateConsoleCtrlEvent`` requires shared-console
          attachment, so the ctrl-event path silently fails across the
          console boundary.

        POSIX uses ``proc.terminate()`` (SIGTERM) — aiohttp's signal
        handler runs on_shutdown there. After ``_GRACEFUL_STOP_GRACE_S``
        without exit, escalate to SIGKILL / TerminateProcess.
        """
        proc = backend.proc
        if proc.poll() is not None:
            return
        if not backend.stop_signalled:
            backend.stop_signalled = True
            try:
                if sys.platform == "win32":
                    if self.separate_console:
                        stop_path = runtime_dir(self.tesseract_home) / "stop_request"
                        stop_path.write_text("stop\n", encoding="utf-8")
                    else:
                        os.kill(proc.pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
                else:
                    proc.terminate()
            except OSError:
                log.exception("supervisor: graceful stop signal raised")
        deadline = time.monotonic() + _GRACEFUL_STOP_GRACE_S
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.2)
        if proc.poll() is None:
            log.warning("supervisor: backend ignored SIGTERM, escalating to SIGKILL")
            try:
                proc.kill()
            except OSError:
                log.exception("supervisor: kill() raised")
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                log.error("supervisor: backend did not die after SIGKILL")

    def _wait_for_exit(self, backend: BackendProcess) -> int:
        """Block until backend exits OR supervisor was asked to stop."""
        while not self._stop_event.is_set():
            try:
                return backend.proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                continue
        # Stop requested mid-flight — signal the backend and wait.
        self._terminate_backend(backend)
        try:
            return backend.proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            return -1

    # -- heartbeat ---------------------------------------------------------

    def _start_heartbeat(self, backend: BackendProcess) -> None:
        if not self.heartbeat_enabled:
            return
        backend.health_failures = 0
        thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(backend,),
            name="supervisor-heartbeat",
            daemon=True,
        )
        thread.start()
        self._heartbeat_thread = thread

    def _stop_heartbeat(self) -> None:
        # The thread exits naturally when ``backend.proc.poll()`` returns
        # non-None or the stop event fires. Join with a short timeout so
        # a hung HTTP read doesn't block the main loop.
        thread = self._heartbeat_thread
        if thread is not None:
            thread.join(timeout=_HEARTBEAT_TIMEOUT_S + 1.0)
        self._heartbeat_thread = None

    def _heartbeat_loop(self, backend: BackendProcess) -> None:
        spawn_time = time.monotonic()
        listener_seen = False
        while not self._stop_event.is_set() and backend.proc.poll() is None:
            probe = self._probe_health()
            backend.last_probe = probe
            if probe.ok:
                backend.health_failures = 0
                backend.heartbeat_incident_reported = False
                listener_seen = True
            else:
                elapsed = time.monotonic() - spawn_time
                in_boot_grace = not listener_seen and elapsed < _HEARTBEAT_BOOT_GRACE_S
                if in_boot_grace:
                    log.debug(
                        "supervisor: heartbeat miss during boot grace (t+%.1fs / %.0fs)",
                        elapsed, _HEARTBEAT_BOOT_GRACE_S,
                    )
                else:
                    backend.health_failures += 1
                    log.debug(
                        "supervisor: heartbeat miss %d/%d hard limit (%s: %s, %.1fms)",
                        backend.health_failures,
                        _HEARTBEAT_MAX_FAILURES,
                        probe.error_type or f"status={probe.status}",
                        probe.error or "non-200 health response",
                        probe.latency_ms,
                    )
                    if (
                        backend.health_failures >= _HEARTBEAT_SOFT_FAILURES
                        and not backend.heartbeat_incident_reported
                    ):
                        backend.heartbeat_incident_reported = True
                        self._record_heartbeat_incident(backend, elapsed)
                    if backend.health_failures >= _HEARTBEAT_MAX_FAILURES:
                        log.warning(
                            "supervisor: backend missed %d heartbeats, restarting backend",
                            _HEARTBEAT_MAX_FAILURES,
                        )
                        # Mark BEFORE delivering the signal — the backend
                        # will write intent.json on the way down, and
                        # ``_classify`` reads this flag to override the
                        # backend's ``operator_quit`` self-label.
                        backend.heartbeat_killed = True
                        self._terminate_backend(backend)
                        return
            # Sleep in small slices so stop_event cancels promptly.
            slept = 0.0
            while slept < _HEARTBEAT_INTERVAL_S and not self._stop_event.is_set():
                time.sleep(0.5)
                slept += 0.5
                if backend.proc.poll() is not None:
                    return

    def _probe_health(self) -> HeartbeatProbe:
        # Catch BaseException-minus-Exception (KeyboardInterrupt, SystemExit)
        # is intentionally NOT swallowed — those reach the signal handler.
        # Everything else (URLError, OSError, TimeoutError, plus any
        # surprise from urllib internals like RemoteDisconnected, partial
        # response, http.client errors) must return False rather than
        # killing the heartbeat thread silently — a dead heartbeat means
        # a hung backend that never gets respawned.
        started = time.monotonic()
        try:
            with urllib.request.urlopen(  # noqa: S310 — localhost only, no untrusted scheme
                self.health_url, timeout=_HEARTBEAT_TIMEOUT_S,
            ) as resp:
                latency_ms = (time.monotonic() - started) * 1000.0
                return HeartbeatProbe(
                    ok=resp.status == 200,
                    status=resp.status,
                    latency_ms=latency_ms,
                    error=None if resp.status == 200 else "non-200 health response",
                )
        except urllib.error.HTTPError as exc:
            latency_ms = (time.monotonic() - started) * 1000.0
            return HeartbeatProbe(
                ok=False,
                status=exc.code,
                latency_ms=latency_ms,
                error_type=type(exc).__name__,
                error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001
            latency_ms = (time.monotonic() - started) * 1000.0
            return HeartbeatProbe(
                ok=False,
                latency_ms=latency_ms,
                error_type=type(exc).__name__,
                error=str(exc),
            )

    def _record_heartbeat_incident(
        self,
        backend: BackendProcess,
        elapsed_seconds: float,
    ) -> None:
        """Soft heartbeat incident: record diagnostics without killing backend."""
        probe = backend.last_probe
        stack_request = self._request_backend_stack_dump(backend)
        log.warning(
            "supervisor: backend missed %d heartbeats; recording soft incident "
            "(pid=%s, elapsed=%.1fs, hard_limit=%d, last_probe=%s)",
            backend.health_failures,
            backend.proc.pid,
            elapsed_seconds,
            _HEARTBEAT_MAX_FAILURES,
            probe.to_payload() if probe is not None else None,
        )
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": "heartbeat_soft_failure",
            "backend_pid": backend.proc.pid,
            "health_url": self.health_url,
            "consecutive_failures": backend.health_failures,
            "soft_limit": _HEARTBEAT_SOFT_FAILURES,
            "hard_limit": _HEARTBEAT_MAX_FAILURES,
            "elapsed_seconds": round(elapsed_seconds, 3),
        }
        if probe is not None:
            payload["last_probe"] = probe.to_payload()
        if stack_request is not None:
            payload["stack_dump"] = stack_request
        try:
            log_dir = self.tesseract_home / "logs" / "supervisor"
            log_dir.mkdir(parents=True, exist_ok=True)
            with (log_dir / "heartbeat-incidents.jsonl").open(
                "a", encoding="utf-8",
            ) as f:
                f.write(json.dumps(payload, sort_keys=True) + "\n")
        except Exception:  # noqa: BLE001
            log.exception("supervisor: heartbeat incident write failed")

    def _request_backend_stack_dump(
        self,
        backend: BackendProcess,
    ) -> dict[str, str] | None:
        """Ask the backend watchdog to dump all Python thread stacks."""
        try:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            log_dir = self.tesseract_home / "logs" / "supervisor"
            request_dir = runtime_dir(self.tesseract_home) / "diagnostics"
            log_dir.mkdir(parents=True, exist_ok=True)
            request_dir.mkdir(parents=True, exist_ok=True)
            output_path = log_dir / f"backend-stack-{backend.proc.pid}-{stamp}.txt"
            request_path = request_dir / f"stack-dump-{backend.proc.pid}-{stamp}.json"
            request = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "backend_pid": backend.proc.pid,
                "output_path": str(output_path),
                "reason": "heartbeat_soft_failure",
            }
            tmp_path = request_path.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(request, sort_keys=True), encoding="utf-8")
            tmp_path.replace(request_path)
            return {
                "request_path": str(request_path),
                "output_path": str(output_path),
            }
        except Exception:  # noqa: BLE001
            log.exception("supervisor: backend stack-dump request failed")
            return None

    # -- intent routing ----------------------------------------------------

    def _read_intent(self, backend: BackendProcess) -> IntentFile | None:
        return read_with_staleness_check(
            intent_path(self.tesseract_home),
            backend_started_at=backend.started_at,
            backend_pid=backend.proc.pid,
        )

    def _classify(
        self,
        intent: IntentFile | None,
        exit_code: int,
        *,
        heartbeat_killed: bool = False,
    ) -> str:
        """Map (intent, exit_code, heartbeat_killed) → routing decision.

        Absent intent OR stale intent OR explicit ``crash`` → ``crash``.
        ``operator_quit`` is honored unconditionally — even a non-zero
        exit code combined with operator_quit is still operator intent.

        ``heartbeat_killed=True`` overrides every intent label and forces
        ``crash``. The backend can't tell our termination signal apart
        from an operator Ctrl-C, so it will write ``operator_quit`` on
        the way down regardless. Without this override the supervisor
        would honor its own kill as an operator quit and exit zero —
        instead of respawning, which is its entire purpose.
        """
        if heartbeat_killed:
            return "crash"
        if intent is None:
            return "crash"
        if intent.intent == "operator_quit":
            return "operator_quit"
        if intent.intent == "restart_upgrade":
            return "restart_upgrade"
        return "crash"

    # -- backoff -----------------------------------------------------------

    def _sleep_backoff(self, crash_n: int) -> None:
        idx = min(crash_n - 1, len(_CRASH_BACKOFF_S) - 1)
        delay = _CRASH_BACKOFF_S[max(idx, 0)]
        log.info("supervisor: backoff %.0fs before respawn", delay)
        slept = 0.0
        while slept < delay and not self._stop_event.is_set():
            time.sleep(0.5)
            slept += 0.5

    # -- signals -----------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        # SIGINT (Ctrl-C) + SIGTERM route to a clean stop. On Windows,
        # CTRL_BREAK_EVENT generates SIGBREAK (NOT SIGINT) — the
        # operator pressing Ctrl-Break in the supervisor terminal, or
        # a parent process delivering CTRL_BREAK_EVENT to our group,
        # both arrive as SIGBREAK. Without an explicit handler Python
        # hard-exits with STATUS_CONTROL_C_EXIT and the backend is
        # orphaned.
        def _handler(signum, _frame):  # type: ignore[no-untyped-def]
            self.request_stop(source="supervisor_signal")
        try:
            signal.signal(signal.SIGINT, _handler)
            signal.signal(signal.SIGTERM, _handler)
            if sys.platform == "win32" and hasattr(signal, "SIGBREAK"):
                signal.signal(signal.SIGBREAK, _handler)  # type: ignore[attr-defined]
        except (ValueError, OSError):
            # Tests construct Supervisor instances inside threads — the
            # signal module raises ValueError there. Tests drive the
            # stop event directly.
            log.debug("supervisor: signal handlers skipped (likely test context)")


def write_pid_file(tesseract_home: Path) -> Path:
    """Write our PID to ``<TESSERACT_HOME>/runtime/supervisor.pid``.

    Operator CLI tools (`shutdown.py`, `clear_crash_storm.py`) read this
    to send signals. Caller is responsible for unlinking on exit.
    """
    path = runtime_dir(tesseract_home) / "supervisor.pid"
    path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    return path


def clear_pid_file(tesseract_home: Path) -> None:
    path = runtime_dir(tesseract_home) / "supervisor.pid"
    try:
        path.unlink()
    except FileNotFoundError:
        pass


__all__ = ["Supervisor", "BackendProcess", "write_pid_file", "clear_pid_file"]
