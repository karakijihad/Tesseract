"""TC-9 parity test fixtures.

The parity suite touches every substrate written by TC-1..TC-8:
``tars_controller/``, ``sessions/chats``, ``run``, ``logs``, ``missions``,
``operator_journal``, and ``agenda``. The autouse leak guard snapshots
each root against the *real* production tree (the static import-time
``TESSERACT_HOME``) so a test that forgets to redirect a writer surfaces
as a leak rather than as silent pollution.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Iterator

import pytest

# Capture the real production home at conftest import time, BEFORE any
# test monkeypatches `TESSERACT_HOME` or reloads `tesseract.paths`. A
# function-level `from tesseract.paths import TESSERACT_HOME` would
# re-read the (already-reloaded) module attribute and snapshot the
# tmp_path instead of the real tree, silently disabling the leak guard.
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
        home / "sessions" / "chats",
        home / "run",
        home / "logs",
        home / "missions",
        home / "operator_journal",
        home / "agenda",
    )
    before = _snapshot(*roots)
    yield
    after = _snapshot(*roots)
    leaked = after - before
    assert not leaked, (
        f"test polluted production substrate at {home}: {sorted(leaked)}"
    )
