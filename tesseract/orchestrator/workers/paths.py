"""Call-time path resolution for the durable worker substrate.

Every helper resolves ``TESSERACT_HOME`` from the environment AT CALL
TIME so tests that ``monkeypatch.setenv("TESSERACT_HOME", tmp_path)``
before invoking the writer get their tmp dir without production logs
ever seeing a test line. Mirrors ``kernel/workspace_changes.py::
workspace_events_dir`` per the project hard rule.
"""

from __future__ import annotations

import os
from pathlib import Path

from tesseract.paths import TESSERACT_HOME


def _home() -> Path:
    override = os.environ.get("TESSERACT_HOME")
    return Path(override).resolve() if override else TESSERACT_HOME


def workers_active_dir() -> Path:
    """``<TESSERACT_HOME>/workers/active/`` — live worker records."""
    return _home() / "workers" / "active"


def workers_archive_dir() -> Path:
    """``<TESSERACT_HOME>/workers/archive/`` — terminal-state records,
    bucketed by ``YYYY-MM`` per the worker-record schema contract."""
    return _home() / "workers" / "archive"


def worker_dir(worker_id: str) -> Path:
    """Per-worker directory under ``active/``. Caller is responsible for
    creating it (``write_record`` does so on first call)."""
    return workers_active_dir() / worker_id


def worktrees_dir() -> Path:
    """``<TESSERACT_HOME>/worktrees/`` — live per-worker git worktrees
    for code-editing workers (AU-12)."""
    return _home() / "worktrees"


def worktrees_archive_dir() -> Path:
    """``<TESSERACT_HOME>/worktrees-archive/`` — finalized worktrees
    awaiting operator review or retention prune (AU-12)."""
    return _home() / "worktrees-archive"
