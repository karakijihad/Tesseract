"""TC-3 codex-daemon-parity test fixtures.

Catches the CLAUDE.md hard rule (tests must never write to
``tesseract/logs/**``) by snapshotting the production logs tree at the
fixture-time-resolved path so a missing ``TESSERACT_HOME`` patch is
flagged loudly.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest


def _logs_root() -> Path:
    from tesseract.paths import TESSERACT_HOME as _default_home
    override = os.environ.get("TESSERACT_HOME")
    home = Path(override).resolve() if override else _default_home
    return home / "logs"


@pytest.fixture(autouse=True)
def _production_logs_baseline() -> Iterator[None]:
    root = _logs_root()
    before: set[Path] = set()
    if root.exists():
        before = {p for p in root.rglob("*") if p.is_file()}
    yield
    after: set[Path] = set()
    if root.exists():
        after = {p for p in root.rglob("*") if p.is_file()}
    leaked = after - before
    assert not leaked, (
        f"test polluted production logs tree at {root}: {sorted(leaked)}"
    )
