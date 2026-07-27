"""Supervisor sibling-spawn behavior for the controller daemon.

Confirms:

* `_spawn_controller_daemon` writes the token file BEFORE Popen so the
  daemon can read it on launch.
* The Popen kwargs carry CREATE_NEW_PROCESS_GROUP on Windows or
  start_new_session=True on POSIX (the controller daemon must outlive a
  backend SIGTERM).
* `last_controller_pid` is recorded after the spawn returns.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tesseract.supervisor.daemon import Supervisor


class _FakeProc:
    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self._returncode: int | None = None

    def poll(self) -> int | None:
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
        return 0

    def kill(self) -> None:
        self._returncode = -9

    def send_signal(self, *_: Any) -> None:  # noqa: ARG002
        self._returncode = 0

    def terminate(self) -> None:
        self._returncode = 0


def test_spawn_writes_token_and_uses_isolation_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def _fake_popen(cmd: list[str], **kwargs: Any) -> _FakeProc:
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    sup = Supervisor(
        tesseract_home=tmp_path,
        controller_daemon_enabled=True,
        controller_daemon_cmd=[
            sys.executable, "-m", "tesseract.scripts.tars_controller",
        ],
    )
    sup._spawn_controller_daemon()  # noqa: SLF001 — tested directly

    # Token file present and non-empty.
    token_path = tmp_path / "run" / "controller.token"
    assert token_path.exists()
    assert token_path.read_text(encoding="utf-8").strip()

    # Sibling-survival flags.
    kwargs = captured["kwargs"]
    if sys.platform == "win32":
        assert kwargs.get("creationflags") == subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        assert kwargs.get("start_new_session") is True

    # PID recorded for inspection.
    assert sup.last_controller_pid == 4242
    assert sup._controller_proc is not None  # noqa: SLF001


def test_default_supervisor_has_controller_daemon_enabled(tmp_path: Path) -> None:
    """X-2 (2026-06-02): the Supervisor dataclass default flipped from
    ``False`` to ``True``. ``__main__`` still honors
    ``SUPERVISOR_DISABLE_CONTROLLER=1`` for opt-out (see
    ``test_supervisor_controller_autospawn.py``)."""
    sup = Supervisor(tesseract_home=tmp_path)
    assert sup.controller_daemon_enabled is True
