"""TC-2 shared-session substrate test fixtures.

Every test in this directory routes writes to a tmp ``TESSERACT_HOME``.
The autouse guard resolves the production substrate dirs from the
fixture-time environment so a test that forgets ``isolated_home`` is
flagged loudly instead of silently writing into the repo.
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
    """Snapshot the substrate dirs + the production logs tree before each
    test and assert no new files appear under either. Catches both:

    - the TC-1 mistake of guarding the wrong path (resolved here at
      fixture time, so a TESSERACT_HOME override moves the guard with it)
    - the CLAUDE.md hard rule that tests must never write to
      ``tesseract/logs/**``.
    """
    home = _home()
    controller_root = home / "tars_controller"
    chats_root = home / "sessions" / "chats"
    logs_root = home / "logs"
    before = _snapshot(controller_root, chats_root, logs_root)
    yield
    after = _snapshot(controller_root, chats_root, logs_root)
    leaked = after - before
    assert not leaked, (
        f"test polluted production substrate at {home}: {sorted(leaked)}"
    )
