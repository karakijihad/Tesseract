"""Fixtures for Y-3 — views-as-canvases migration.

Isolate ``TESSERACT_HOME`` to ``tmp_path`` so the surface store's canvas-state
writes + the mission series root land under the test scratch dir (CLAUDE.md
hard rule: tests MUST NOT write to production runtime state). Reset the
surface store + background bus singletons around each test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tesseract.orchestrator.background_event_bus import reset_background_bus
from tesseract.orchestrator.surfaces.store import reset_surface_store


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``TESSERACT_HOME`` to ``tmp_path`` (resolved at call time by
    the persistence + series layers, so no module-attr patching needed)."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _reset_singletons():
    reset_surface_store()
    reset_background_bus()
    yield
    reset_surface_store()
    reset_background_bus()
