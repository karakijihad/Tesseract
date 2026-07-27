"""Fixtures for the X-1 (tars-cockpit) stale-IPC fix.

Isolates ``TESSERACT_HOME`` for every test so dispatcher / controller-
session-handle writes land under ``tmp_path``, never under the production
``tesseract/logs/**`` tree (CLAUDE.md hard rule).
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_tesseract_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
