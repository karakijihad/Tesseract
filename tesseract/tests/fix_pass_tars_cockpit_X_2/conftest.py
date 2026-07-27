"""Fixtures for the X-2 (tars-cockpit) controller-daemon-default-on phase.

Per CLAUDE.md hard rule on tests/logs, every test isolates ``TESSERACT_HOME``
so Supervisor / SessionRegistry / route writes land under ``tmp_path``
instead of the production tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_tesseract_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
