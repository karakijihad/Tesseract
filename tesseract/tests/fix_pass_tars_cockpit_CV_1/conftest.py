"""Fixtures for CV-1 — Claude/Codex live-lane cards on canvas.

Tests isolate ``TESSERACT_HOME`` to ``tmp_path`` (no production state) and
drive the Mirror lane bridge with a stub ``ControllerClient`` injected via
``APP_FACTORY_KEY`` so the route logic is exercised without a real daemon.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path
