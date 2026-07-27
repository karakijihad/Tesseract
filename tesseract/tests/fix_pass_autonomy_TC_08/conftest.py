"""TC-8 controller-owned PTY child-worker test fixtures.

Same `TESSERACT_HOME` redirect + autouse production-substrate snapshot
pattern as TC-6. Adds ``missions`` to the snapshot roots because the
runner writes lease files into ``<TESSERACT_HOME>/missions/``.

Audit-2 m-1: ``_production_home()`` previously re-imported
``tesseract.paths.TESSERACT_HOME`` inside the function body. After a
test reloads ``tesseract.paths`` against the tmp tree, that lookup
returns the tmp path and the leak guard silently snapshots the same
directory the writes land in. Capture the real production home at
conftest import time (TC-9 pattern) so the guard cannot be subverted.
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
    controller_root = home / "tars_controller"
    chats_root = home / "sessions" / "chats"
    run_root = home / "run"
    logs_root = home / "logs"
    missions_root = home / "missions"
    before = _snapshot(
        controller_root, chats_root, run_root, logs_root, missions_root,
    )
    yield
    after = _snapshot(
        controller_root, chats_root, run_root, logs_root, missions_root,
    )
    leaked = after - before
    assert not leaked, (
        f"test polluted production substrate at {home}: {sorted(leaked)}"
    )
