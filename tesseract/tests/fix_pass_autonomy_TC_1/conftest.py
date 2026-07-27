"""TC-1 operator-journal test fixtures.

Every test under this directory writes to a tmp ``TESSERACT_HOME``.
The autouse guard resolves the actual journal directory from the
fixture-time environment so it tracks the same path the writer uses —
default ``<TESSERACT_DIR>/operator_journal/`` when ``TESSERACT_HOME``
is unset, and the monkeypatched tmp path otherwise.
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


def _journal_root() -> Path:
    from tesseract.paths import TESSERACT_HOME as _default_home
    override = os.environ.get("TESSERACT_HOME")
    home = Path(override).resolve() if override else _default_home
    return home / "operator_journal"


@pytest.fixture(autouse=True)
def _production_journal_baseline() -> Iterator[None]:
    """Snapshot the live journal directory and ensure the test left no
    new files behind. Resolves the path at fixture time so it follows
    ``TESSERACT_HOME`` overrides — guards against a test that forgets
    ``isolated_home`` and writes to the source-tree default location."""
    root = _journal_root()
    before: set[Path] = set()
    if root.exists():
        before = {p for p in root.rglob("*") if p.is_file()}
    yield
    after: set[Path] = set()
    if root.exists():
        after = {p for p in root.rglob("*") if p.is_file()}
    leaked = after - before
    assert not leaked, (
        f"test polluted production journal tree at {root}: {sorted(leaked)}"
    )
