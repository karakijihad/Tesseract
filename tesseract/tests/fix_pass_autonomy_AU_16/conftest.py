"""AU-16 shared fixtures.

Every test runs with ``TESSERACT_HOME=tmp_path`` so leaf / buffer / seal
files land inside the per-test sandbox per the project hard rule on
log + state isolation.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    import tesseract.paths
    importlib.reload(tesseract.paths)
    return tmp_path
