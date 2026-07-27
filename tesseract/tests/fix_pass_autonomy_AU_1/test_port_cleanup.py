"""Supervisor port-release on exit.

Operator-requested 2026-05-18: when the supervisor exits, free ports
8000 (Mirror backend) and 1420 (Vite dev) so zombie listeners don't
break the next start. Tests stub the platform-specific PID lookup +
kill helpers so the suite stays portable + fast.
"""

from __future__ import annotations

from typing import Any

import pytest

from tesseract.supervisor import port_cleanup


def test_resolve_ports_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUPERVISOR_RELEASE_PORTS", raising=False)
    assert port_cleanup._resolve_ports() == port_cleanup.DEFAULT_PORTS


def test_resolve_ports_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPERVISOR_RELEASE_PORTS", "5000, 6000 ,7000")
    assert port_cleanup._resolve_ports() == (5000, 6000, 7000)


def test_resolve_ports_env_ignores_non_int(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUPERVISOR_RELEASE_PORTS", "8000,not-a-port,1420")
    assert port_cleanup._resolve_ports() == (8000, 1420)


def test_resolve_ports_empty_override_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUPERVISOR_RELEASE_PORTS", "  ,  ")
    assert port_cleanup._resolve_ports() == port_cleanup.DEFAULT_PORTS


def test_release_ports_kills_each_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stub the PID lookup + kill helpers and assert every PID is killed."""
    monkeypatch.setattr(port_cleanup.sys, "platform", "linux", raising=False)
    pids_by_port = {8000: {1111, 2222}, 1420: {3333}}
    killed: list[tuple[int, int]] = []

    def fake_posix_pids(port: int) -> set[int]:
        return set(pids_by_port.get(port, set()))

    def fake_posix_kill(pid: int) -> bool:
        killed.append(("posix", pid))  # type: ignore[arg-type]
        return True

    monkeypatch.setattr(port_cleanup, "_posix_pids_on_port", fake_posix_pids)
    monkeypatch.setattr(port_cleanup, "_posix_kill", fake_posix_kill)

    summary = port_cleanup.release_ports((8000, 1420))
    assert sorted(summary[8000]) == [1111, 2222]
    assert summary[1420] == [3333]
    assert sorted(p for _, p in killed) == [1111, 2222, 3333]


def test_release_ports_skips_self_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refuse to taskkill the supervisor's own PID if it ever appears in
    the netstat output (paranoia for future bind tests)."""
    monkeypatch.setattr(port_cleanup.sys, "platform", "linux", raising=False)
    self_pid = port_cleanup.os.getpid()

    monkeypatch.setattr(
        port_cleanup, "_posix_pids_on_port", lambda port: {self_pid, 9999},
    )
    killed: list[int] = []
    monkeypatch.setattr(
        port_cleanup, "_posix_kill", lambda pid: (killed.append(pid) or True),
    )

    summary = port_cleanup.release_ports((8000,))
    assert summary[8000] == [9999]
    assert killed == [9999]


def test_release_ports_returns_empty_when_nothing_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(port_cleanup.sys, "platform", "linux", raising=False)
    monkeypatch.setattr(port_cleanup, "_posix_pids_on_port", lambda port: set())
    summary = port_cleanup.release_ports((8000, 1420))
    assert summary == {8000: [], 1420: []}


def test_release_ports_dispatches_windows_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When sys.platform == 'win32', release_ports must call the windows
    helpers (netstat + taskkill) not the POSIX ones."""
    monkeypatch.setattr(port_cleanup.sys, "platform", "win32", raising=False)
    calls: list[str] = []

    def fake_win_pids(port: int) -> set[int]:
        calls.append(f"win_pids:{port}")
        return {12345}

    def fake_win_kill(pid: int) -> bool:
        calls.append(f"win_kill:{pid}")
        return True

    def fail_posix(*a: Any, **kw: Any) -> Any:  # noqa: ARG001
        raise AssertionError("POSIX helpers must not run on win32")

    monkeypatch.setattr(port_cleanup, "_windows_pids_on_port", fake_win_pids)
    monkeypatch.setattr(port_cleanup, "_windows_kill", fake_win_kill)
    monkeypatch.setattr(port_cleanup, "_posix_pids_on_port", fail_posix)
    monkeypatch.setattr(port_cleanup, "_posix_kill", fail_posix)

    summary = port_cleanup.release_ports((8000,))
    assert summary[8000] == [12345]
    assert calls == ["win_pids:8000", "win_kill:12345"]


def test_windows_netstat_parser_extracts_listening_pids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Smoke-test the netstat parser regex against a realistic capture."""
    sample = (
        "Active Connections\r\n"
        "  Proto  Local Address          Foreign Address        State           PID\r\n"
        "  TCP    127.0.0.1:8000         0.0.0.0:0              LISTENING       38972\r\n"
        "  TCP    127.0.0.1:1420         0.0.0.0:0              LISTENING       54321\r\n"
        "  TCP    127.0.0.1:8000         127.0.0.1:52613        ESTABLISHED     38972\r\n"
        "  TCP    127.0.0.1:9999         0.0.0.0:0              LISTENING       11111\r\n"
    )

    class _Completed:
        stdout = sample
        stderr = ""
        returncode = 0

    monkeypatch.setattr(
        port_cleanup.subprocess, "run", lambda *a, **kw: _Completed(),
    )
    assert port_cleanup._windows_pids_on_port(8000) == {38972}
    assert port_cleanup._windows_pids_on_port(1420) == {54321}
    assert port_cleanup._windows_pids_on_port(9999) == {11111}
    assert port_cleanup._windows_pids_on_port(7777) == set()
