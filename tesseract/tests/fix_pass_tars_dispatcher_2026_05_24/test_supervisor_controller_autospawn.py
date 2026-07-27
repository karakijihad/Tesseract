"""Supervisor → controller-daemon auto-spawn.

The Supervisor was carrying the ``controller_daemon_enabled`` field +
``_spawn_controller_daemon`` method for a while, but ``run()`` never
called the spawn. After the 2026-05-24 rollout the supervisor's
``__main__.py`` enables it by default and ``run()`` calls
``_spawn_controller_daemon``.

These tests verify the wiring directly without booting a real backend
(``backend_cmd`` points at a no-op sleeper).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from tesseract.supervisor.daemon import Supervisor


def test_main_enables_controller_daemon_when_env_not_set(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``python -m tesseract.supervisor`` constructs a Supervisor with
    controller_daemon_enabled=True unless the operator sets
    SUPERVISOR_DISABLE_CONTROLLER=1."""
    monkeypatch.delenv("SUPERVISOR_DISABLE_CONTROLLER", raising=False)
    # Mimic __main__'s constructor call.
    monkeypatch.setenv("TESSERACT_HOME", str(isolated_home))

    enabled = (
        __import__("os").environ.get("SUPERVISOR_DISABLE_CONTROLLER") != "1"
    )
    sup = Supervisor(
        tesseract_home=isolated_home,
        controller_daemon_enabled=enabled,
    )
    assert sup.controller_daemon_enabled is True


def test_main_disables_controller_daemon_when_env_set(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SUPERVISOR_DISABLE_CONTROLLER", "1")
    enabled = (
        __import__("os").environ.get("SUPERVISOR_DISABLE_CONTROLLER") != "1"
    )
    sup = Supervisor(
        tesseract_home=isolated_home,
        controller_daemon_enabled=enabled,
    )
    assert sup.controller_daemon_enabled is False


def test_stop_all_daemons_idempotent(isolated_home: Path) -> None:
    """``_stop_all_daemons`` calls both teardowns and tolerates either
    one not having been started."""
    sup = Supervisor(tesseract_home=isolated_home)
    # Neither daemon was spawned; the call must not raise.
    sup._stop_all_daemons()
    sup._stop_all_daemons()


def test_run_spawns_controller_when_enabled(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: when ``run()`` starts with controller_daemon_enabled,
    the controller spawn method fires exactly once.
    """
    sup = Supervisor(
        tesseract_home=isolated_home,
        controller_daemon_enabled=True,
        # Use a backend that exits immediately so run() terminates fast.
        backend_cmd=[sys.executable, "-c", "import sys; sys.exit(0)"],
        max_respawns=1,
        heartbeat_enabled=False,
    )
    calls = {"spawn": 0, "watchdog": 0}
    monkeypatch.setattr(
        sup,
        "_spawn_controller_daemon",
        lambda: calls.__setitem__("spawn", calls["spawn"] + 1),
    )
    monkeypatch.setattr(
        sup,
        "_start_controller_daemon_watchdog",
        lambda: calls.__setitem__("watchdog", calls["watchdog"] + 1),
    )
    # No real watchdog to join in teardown.
    monkeypatch.setattr(sup, "_stop_controller_daemon", lambda: None)

    sup.run()

    assert calls["spawn"] == 1, "controller daemon must be spawned once on enable"
    assert calls["watchdog"] == 1, "watchdog must start when spawn succeeds"


def test_run_does_not_spawn_controller_when_disabled(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sup = Supervisor(
        tesseract_home=isolated_home,
        controller_daemon_enabled=False,
        backend_cmd=[sys.executable, "-c", "import sys; sys.exit(0)"],
        max_respawns=1,
        heartbeat_enabled=False,
    )
    calls = {"spawn": 0}
    monkeypatch.setattr(
        sup,
        "_spawn_controller_daemon",
        lambda: calls.__setitem__("spawn", calls["spawn"] + 1),
    )
    sup.run()
    assert calls["spawn"] == 0


def test_spawn_failure_does_not_crash_supervisor(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Controller-spawn raising must not take the supervisor down —
    `tars` CLI's self-bootstrap is the safety net."""
    sup = Supervisor(
        tesseract_home=isolated_home,
        controller_daemon_enabled=True,
        backend_cmd=[sys.executable, "-c", "import sys; sys.exit(0)"],
        max_respawns=1,
        heartbeat_enabled=False,
    )

    def _boom() -> None:
        raise RuntimeError("simulated spawn failure")

    monkeypatch.setattr(sup, "_spawn_controller_daemon", _boom)
    monkeypatch.setattr(sup, "_stop_controller_daemon", lambda: None)
    monkeypatch.setattr(
        sup, "_start_controller_daemon_watchdog", lambda: None
    )

    # Should return cleanly (the backend exits and the run loop ends)
    # despite the spawn raising.
    sup.run()
