"""TC-4 controller-daemon test fixtures.

Every test routes writes to a tmp ``TESSERACT_HOME``. The autouse guard
snapshots the production substrate dirs AND the production logs tree
before each test, then asserts no new files appear in either (per the
CLAUDE.md hard rule).

Audit-2 m-1: the previous ``_home()`` helper read ``TESSERACT_HOME``
from the live environment, which under ``monkeypatch.setenv`` resolves
to the per-test tmp tree. Snapshotting that path before+after the same
test is a vacuous check — the diff is always empty because the writes
also land there. TC-9 captures the real production home at conftest
import time (before any monkeypatch / module reload); we mirror that.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Iterator

import pytest

# Capture the real production home at conftest import time, BEFORE any
# test monkeypatches `TESSERACT_HOME` or reloads `tesseract.paths`. A
# function-level lookup would re-read the (already-reloaded) module
# attribute and snapshot the tmp_path instead of the real tree, silently
# disabling the leak guard.
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
    before = _snapshot(controller_root, chats_root, run_root, logs_root)
    yield
    after = _snapshot(controller_root, chats_root, run_root, logs_root)
    leaked = after - before
    assert not leaked, (
        f"test polluted production substrate at {home}: {sorted(leaked)}"
    )
