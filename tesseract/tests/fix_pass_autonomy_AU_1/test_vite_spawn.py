"""Supervisor-managed Vite dev-server spawn.

Operator-requested: `python -m tesseract.supervisor` should also start
`pnpm run dev` in `tesseract/mirror/`. Vite's lifecycle is tied to the
supervisor — these tests assert the spawn/stop helpers behave correctly
without actually running pnpm (which would tie tests to a JS toolchain).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from tesseract.supervisor import vite


def test_vite_enabled_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUPERVISOR_DEV_VITE", raising=False)
    assert vite._vite_enabled() is True


def test_vite_enabled_off_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ("0", "false", "False", "no", "off"):
        monkeypatch.setenv("SUPERVISOR_DEV_VITE", value)
        assert vite._vite_enabled() is False, f"{value!r} should disable"


def test_start_vite_skips_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUPERVISOR_DEV_VITE", "0")
    assert vite.start_vite(tmp_path) is None


def test_start_vite_skips_when_mirror_dir_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SUPERVISOR_DEV_VITE", raising=False)
    # tmp_path is empty — no tesseract/mirror/package.json
    assert vite.start_vite(tmp_path) is None


def test_start_vite_skips_when_pnpm_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SUPERVISOR_DEV_VITE", raising=False)
    # Pretend the mirror dir exists with a package.json.
    mirror = tmp_path / "tesseract" / "mirror"
    mirror.mkdir(parents=True)
    (mirror / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(vite, "_resolve_pnpm", lambda: None)
    assert vite.start_vite(tmp_path) is None


def test_start_vite_spawns_when_environment_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SUPERVISOR_DEV_VITE", raising=False)
    mirror = tmp_path / "tesseract" / "mirror"
    mirror.mkdir(parents=True)
    (mirror / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(vite, "_resolve_pnpm", lambda: "/fake/pnpm")

    captured: dict[str, Any] = {}

    class _FakeProc:
        pid = 12345
        def poll(self) -> int | None:
            return None

    def fake_popen(cmd: list[str], **kwargs: Any) -> _FakeProc:
        captured["cmd"] = cmd
        captured["cwd"] = kwargs.get("cwd")
        return _FakeProc()

    monkeypatch.setattr(vite.subprocess, "Popen", fake_popen)
    proc = vite.start_vite(tmp_path)
    assert proc is not None
    assert captured["cmd"] == ["/fake/pnpm", "run", "dev"]
    assert captured["cwd"] == str(mirror)


def test_start_vite_failsafe_on_spawn_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spawn raise must NOT propagate — supervisor continues without Vite."""
    monkeypatch.delenv("SUPERVISOR_DEV_VITE", raising=False)
    mirror = tmp_path / "tesseract" / "mirror"
    mirror.mkdir(parents=True)
    (mirror / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(vite, "_resolve_pnpm", lambda: "/fake/pnpm")

    def boom(*a: Any, **kw: Any) -> Any:
        raise OSError("simulated pnpm spawn failure")

    monkeypatch.setattr(vite.subprocess, "Popen", boom)
    assert vite.start_vite(tmp_path) is None


def test_stop_vite_no_op_on_none() -> None:
    """Stopping a None proc must be a quiet no-op (skip happens upstream
    when start_vite returned None)."""
    vite.stop_vite(None)  # must not raise


def test_stop_vite_no_op_when_already_exited() -> None:
    class _DeadProc:
        pid = 9999
        def poll(self) -> int:
            return 0  # non-None → exited

    vite.stop_vite(_DeadProc())  # type: ignore[arg-type] — duck-typed


def test_stop_vite_graceful_then_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Graceful stop signal is sent; if the proc doesn't exit within the
    grace window, escalate to kill."""
    calls: list[str] = []

    class _StubbornProc:
        pid = 4444
        _state = "alive"
        def poll(self) -> int | None:
            return 0 if self._state == "killed" else None
        def wait(self, timeout: float) -> int:
            if self._state == "killed":
                return 0
            raise subprocess.TimeoutExpired(cmd="pnpm", timeout=timeout)
        def terminate(self) -> None:
            calls.append("terminate")
        def kill(self) -> None:
            calls.append("kill")
            self._state = "killed"

    proc = _StubbornProc()
    # Avoid calling the real os.kill on a fake PID.
    monkeypatch.setattr(vite.os, "kill", lambda pid, sig: calls.append(f"os.kill({pid},{sig})"))
    vite.stop_vite(proc, grace_seconds=0.01)  # type: ignore[arg-type]
    assert "kill" in calls
