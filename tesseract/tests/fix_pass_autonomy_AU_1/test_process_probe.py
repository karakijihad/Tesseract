"""Cross-platform PID liveness probe.

Regression test for the Windows-specific bug where ``os.kill(pid, 0)``
returned ``WinError 87 (parameter is incorrect)`` against a perfectly
healthy process and the previous probe misclassified it as dead — the
Mirror Runtime panel then flagged the live supervisor as "STALE PID
FILE". Fix: route the Windows path through ``OpenProcess`` +
``GetExitCodeProcess``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from tesseract.supervisor.process_probe import pid_alive


def test_pid_alive_for_current_process() -> None:
    """The interpreter running the test is, by definition, alive."""
    assert pid_alive(os.getpid()) is True


def test_pid_alive_rejects_none() -> None:
    assert pid_alive(None) is False


def test_pid_alive_rejects_non_positive() -> None:
    assert pid_alive(0) is False
    assert pid_alive(-1) is False


def test_pid_alive_for_dead_pid_returns_false() -> None:
    """A very-high PID is overwhelmingly likely to be unused on any OS;
    the probe must return False rather than raise."""
    # 2**31 - 1 — bigger than any realistic PID on Windows or POSIX.
    assert pid_alive(2_147_483_647) is False


def test_pid_alive_detects_subprocess_alive_then_dead() -> None:
    """End-to-end: spawn a short-lived sleeper, assert alive while it
    runs, assert dead after it exits. This is the canonical regression
    against the original Windows WinError 87 false-negative."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(2.0)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Give the OS a moment to schedule the child.
        time.sleep(0.2)
        assert pid_alive(proc.pid) is True, (
            "live subprocess must be detected as alive — "
            "regression against Windows WinError 87 misclassification"
        )
    finally:
        proc.kill()
        proc.wait(timeout=5.0)
    # After death the probe must report False (without raising).
    # Brief wait so the OS reaps the process record.
    time.sleep(0.2)
    assert pid_alive(proc.pid) is False


def test_pid_alive_failsafe_on_unexpected_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe must never propagate exceptions — any unexpected error
    becomes 'not alive' so callers (recovery, runtime status badge,
    supervisor stale-pid detection) keep functioning."""
    from tesseract.supervisor import process_probe

    def _raise(_: int) -> bool:
        raise RuntimeError("simulated probe failure")

    if sys.platform == "win32":
        monkeypatch.setattr(process_probe, "_alive_windows", _raise)
    else:
        monkeypatch.setattr(process_probe, "_alive_posix", _raise)
    assert pid_alive(os.getpid()) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific probe")
def test_windows_probe_uses_openprocess_not_os_kill() -> None:
    """Sanity: on Windows the probe must route through the OpenProcess
    helper; os.kill(pid, 0) misclassifies live processes as dead."""
    from tesseract.supervisor import process_probe

    # The Windows branch exists and is named.
    assert hasattr(process_probe, "_alive_windows")
    # Direct call — the live current process must be reported alive.
    assert process_probe._alive_windows(os.getpid()) is True
