"""Shared dispatcher / trust / autospawn tests — fixtures.

Same log-drift guard pattern as TC-9: capture the real production
``TESSERACT_HOME`` at conftest module-import time so a later
``monkeypatch.setenv`` + ``importlib.reload(tesseract.paths)`` cannot
silently neuter the leak-guard snapshot. The snapshot covers every
substrate the dispatcher touches: ``tars_controller/`` (sessions +
transcripts), ``run/`` (port + token), ``logs/``, ``missions/``
(controller-owned PTY leases), and the trust store at the root.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Iterator

import pytest

from tesseract.paths import TESSERACT_HOME as _REAL_PRODUCTION_HOME


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    import tesseract.paths
    importlib.reload(tesseract.paths)
    return tmp_path


def _production_home() -> Path:
    return _REAL_PRODUCTION_HOME


def _snapshot(*roots: Path) -> set[Path]:
    files: set[Path] = set()
    for root in roots:
        if root.exists():
            files.update(p for p in root.rglob("*") if p.is_file())
    return files


@pytest.fixture(autouse=True)
def _production_substrate_baseline() -> Iterator[None]:
    home = _production_home()
    roots = (
        home / "tars_controller",
        home / "run",
        home / "logs",
        home / "missions",
        home / "trusted_dirs.json",
    )
    before = _snapshot(*roots)
    yield
    after = _snapshot(*roots)
    leaked = after - before
    assert not leaked, (
        f"test polluted production substrate at {home}: {sorted(leaked)}"
    )
