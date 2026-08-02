"""Call-time path resolution for the AgendaStore.

Mirrors ``orchestrator/workers/paths.py`` — every helper resolves
``TESSERACT_HOME`` at CALL TIME so tests that
``monkeypatch.setenv("TESSERACT_HOME", tmp_path)`` route writes into
their tmp dir. Production agenda state never leaks into tests; test
fixtures never leak into ``tesseract/`` or ``<live TESSERACT_HOME>/``.
"""

from __future__ import annotations

import os
from pathlib import Path

from tesseract.paths import TESSERACT_HOME, log_dir


def _home() -> Path:
    override = os.environ.get("TESSERACT_HOME")
    return Path(override).resolve() if override else TESSERACT_HOME


def agenda_root() -> Path:
    """``<TESSERACT_HOME>/agenda/`` — the agenda store root."""
    return _home() / "agenda"


def agenda_active_dir() -> Path:
    """``<TESSERACT_HOME>/agenda/active/`` — live items."""
    return agenda_root() / "active"


def agenda_archive_dir() -> Path:
    """``<TESSERACT_HOME>/agenda/archive/`` — callers append the ``YYYY-MM`` bucket."""
    return agenda_root() / "archive"


def agenda_index_path() -> Path:
    """Append-only event log: every status transition lands here."""
    return agenda_root() / "index.jsonl"


def agenda_item_path(item_id: str) -> Path:
    return agenda_active_dir() / f"{item_id}.json"


def agenda_archive_path(item_id: str, month: str) -> Path:
    return agenda_archive_dir() / month / f"{item_id}.json"


def agenda_comments_dir() -> Path:
    """``<TESSERACT_HOME>/agenda/comments/`` — operator-facing per-item
    discussion threads. One JSONL file per agenda item; append-only;
    gitignored (operator-private)."""
    return agenda_root() / "comments"


def agenda_comments_path(item_id: str) -> Path:
    return agenda_comments_dir() / f"{item_id}.jsonl"


def source_pauses_path() -> Path:
    """Single durable state file for AU-6 governor source pauses.
    Survives restart so a pause cannot be cleared by reboot."""
    return agenda_root() / "source-pauses.json"


def governor_log_path() -> Path:
    """Append-only event log: every pause / unpause / detector trigger."""
    return log_dir("governor") / "pauses.jsonl"
