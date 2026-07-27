"""Approve-dispatch tests — isolate every write to tmp.

Locks down two leak surfaces before the workspace route handlers run:

- ``TESSERACT_HOME`` → ``tmp_path`` so ``record_ask`` writes to
  ``<tmp>/logs/approvals.jsonl`` and ``apply_change`` resolves SOUL.md
  from the tmp tree (we also monkeypatch ``ws_routes.ROOT`` to ``tmp_path``
  so target validation finds the seeded file).
- Production substrate snapshot (mirrors the dispatcher conftest) —
  asserts the test left nothing behind under the real
  ``TESSERACT_HOME/logs`` tree.
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


def _snapshot(*roots: Path) -> set[Path]:
    files: set[Path] = set()
    for root in roots:
        if root.exists():
            files.update(p for p in root.rglob("*") if p.is_file())
    return files


@pytest.fixture(autouse=True)
def _production_substrate_baseline() -> Iterator[None]:
    home = _REAL_PRODUCTION_HOME
    roots = (
        home / "logs",
        home / "memory-store",
        home / "workspace",
    )
    before = _snapshot(*roots)
    yield
    after = _snapshot(*roots)
    leaked = after - before
    assert not leaked, (
        f"test polluted production substrate at {home}: {sorted(leaked)}"
    )
