"""Fixtures for the 2026-07-14 lane fire-and-queue fix pass.

Every test isolates ``TESSERACT_HOME`` so `lanes/` writes land under
``tmp_path`` (CLAUDE.md hard rule: tests never touch production runtime
state)."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path
