"""AU-1 S2 — supervisor --status reports stale pid file (kill-switch §Tests #10).

Covers the operator-visible signal that the supervisor was hard-killed
(SIGKILL / Task Manager / OS reboot) — pid file remains on disk but
the process is gone.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


def test_status_alive_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """A pid file pointing at THIS process reports alive."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "supervisor.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")

    import importlib
    import tesseract.paths
    importlib.reload(tesseract.paths)
    from tesseract.supervisor import __main__ as supervisor_main
    importlib.reload(supervisor_main)

    rc = supervisor_main.main(["--status"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "alive" in captured.out
    assert str(os.getpid()) in captured.out


def test_status_stale_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """A pid file pointing at a definitely-dead pid reports stale."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    # PID 0 is never a real process (POSIX) and is treated as not-alive
    # by both branches of _pid_alive on Windows + POSIX.
    (runtime_dir / "supervisor.pid").write_text("0\n", encoding="utf-8")

    import importlib
    import tesseract.paths
    importlib.reload(tesseract.paths)
    from tesseract.supervisor import __main__ as supervisor_main
    importlib.reload(supervisor_main)

    rc = supervisor_main.main(["--status"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "stale" in captured.out


def test_status_no_pid_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """No pid file → 'not running' message, exit 0."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    import importlib
    import tesseract.paths
    importlib.reload(tesseract.paths)
    from tesseract.supervisor import __main__ as supervisor_main
    importlib.reload(supervisor_main)

    rc = supervisor_main.main(["--status"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "not running" in captured.out
