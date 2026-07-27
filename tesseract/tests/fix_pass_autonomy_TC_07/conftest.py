"""TC-7 follow-up mapper test fixtures.

Mirrors the TC-1 pattern: every test writes to a tmp `TESSERACT_HOME`;
the autouse guard snapshots production-substrate roots (journal +
agenda + logs) at fixture-resolved paths and asserts no leakage.
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


def _home() -> Path:
    from tesseract.paths import TESSERACT_HOME as _default_home
    override = os.environ.get("TESSERACT_HOME")
    return Path(override).resolve() if override else _default_home


def _snapshot(*roots: Path) -> set[Path]:
    files: set[Path] = set()
    for root in roots:
        if root.exists():
            files.update(p for p in root.rglob("*") if p.is_file())
    return files


@pytest.fixture(autouse=True)
def _production_substrate_baseline() -> Iterator[None]:
    home = _home()
    journal_root = home / "operator_journal"
    agenda_root = home / "agenda"
    logs_root = home / "logs"
    before = _snapshot(journal_root, agenda_root, logs_root)
    yield
    after = _snapshot(journal_root, agenda_root, logs_root)
    leaked = after - before
    assert not leaked, (
        f"test polluted production substrate at {home}: {sorted(leaked)}"
    )
