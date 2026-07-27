import threading
from pathlib import Path

import pytest

from tesseract.supervisor.stop_watcher import (
    StopRequestWatcher,
    supervisor_stop_request_path,
)


def test_path_is_under_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    p = supervisor_stop_request_path(tmp_path)
    assert p == tmp_path / "runtime" / "supervisor_stop_request"


def test_check_once_no_file_returns_false(tmp_path):
    watcher = StopRequestWatcher(tmp_path, on_stop=lambda: None)
    assert watcher._check_once() is False


def test_check_once_consumes_file_and_fires_callback(tmp_path):
    fired = threading.Event()
    watcher = StopRequestWatcher(tmp_path, on_stop=fired.set)
    req = supervisor_stop_request_path(tmp_path)
    req.parent.mkdir(parents=True, exist_ok=True)
    req.write_text("stop\n", encoding="utf-8")

    assert watcher._check_once() is True
    assert fired.is_set()
    assert not req.exists()  # consumed so a respawn starts clean


def test_start_then_stop_is_clean(tmp_path):
    watcher = StopRequestWatcher(tmp_path, on_stop=lambda: None, poll_interval_s=0.01)
    watcher.start()
    watcher.stop()  # must not hang
