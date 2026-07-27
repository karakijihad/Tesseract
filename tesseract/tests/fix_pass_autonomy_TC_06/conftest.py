"""TC-6 tars TUI client test fixtures.

Same `TESSERACT_HOME` redirect + autouse production-substrate snapshot
pattern as TC-2/TC-4/TC-5.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Iterator

import pytest


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    import tesseract.paths
    importlib.reload(tesseract.paths)
    return tmp_path


def _production_home() -> Path:
    """Resolve the REAL production substrate root, ignoring any
    ``TESSERACT_HOME`` override the test may have set. The leak
    guard's job is preventing pollution of the real tree, regardless
    of where the test's `isolated_home` fixture redirected writes to.
    """
    from tesseract.paths import TESSERACT_HOME as _default_home
    return _default_home


def _snapshot(*roots: Path) -> set[Path]:
    files: set[Path] = set()
    for root in roots:
        if root.exists():
            files.update(p for p in root.rglob("*") if p.is_file())
    return files


@pytest.fixture(autouse=True)
def _production_substrate_baseline() -> Iterator[None]:
    """Snapshot the REAL production tree (not a tmp redirect) and
    assert zero new files post-test. A test that forgets
    ``isolated_home`` and writes to the source-tree default is
    detected here regardless of fixture-ordering subtleties."""
    home = _production_home()
    controller_root = home / "tars_controller"
    chats_root = home / "sessions" / "chats"
    run_root = home / "run"
    logs_root = home / "logs"
    before = _snapshot(controller_root, chats_root, run_root, logs_root)
    yield
    after = _snapshot(controller_root, chats_root, run_root, logs_root)
    leaked = after - before
    assert not leaked, (
        f"test polluted production substrate at {home}: {sorted(leaked)}"
    )
