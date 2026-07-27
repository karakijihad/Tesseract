"""Operator journal — append-only per-day JSONL log of approval / dispatch /
outcome / advice-only / follow-up-draft events.

Files land under ``<TESSERACT_HOME>/operator_journal/YYYY-MM-DD.jsonl``.
Path resolves at every ``append`` call so ``monkeypatch.setenv("TESSERACT_HOME",
tmp_path)`` works without re-importing the module.

``append`` is best-effort: a write error is logged and swallowed — journal
availability must never block an agenda transition or worker dispatch.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tesseract.paths import TESSERACT_HOME

log = logging.getLogger(__name__)

JOURNAL_EVENT_TYPES = (
    "approval",
    "dispatch",
    "outcome",
    "advice_only",
    "follow_up_draft",
)


def journal_dir() -> Path:
    """Resolve ``<TESSERACT_HOME>/operator_journal/`` at call time."""
    override = os.environ.get("TESSERACT_HOME")
    home = Path(override).resolve() if override else TESSERACT_HOME
    return home / "operator_journal"


def journal_path(day: datetime | None = None) -> Path:
    """Per-day JSONL path. ``day`` defaults to now (UTC)."""
    when = day or datetime.now(timezone.utc)
    return journal_dir() / f"{when.strftime('%Y-%m-%d')}.jsonl"


def _normalize(payload: dict[str, Any]) -> dict[str, Any]:
    """Ensure all schema fields are present; unknown keys pass through."""
    out: dict[str, Any] = dict(payload)
    out.setdefault("agenda_item_id", None)
    out.setdefault("worker_id", None)
    out.setdefault("summary", None)
    out.setdefault("artifacts", None)
    out.setdefault("follow_up_draft_id", None)
    return out


class JournalWriter:
    """Append-only writer for the operator journal.

    No constructor arguments — ``TESSERACT_HOME`` resolves at every call.
    Use the module-level ``append()`` convenience wrapper in production;
    instantiate directly in tests.
    """

    def append(self, event_type: str, payload: dict[str, Any]) -> None:
        """Append one row to today's journal file.

        Unknown ``event_type`` values are still written (forward compat)
        but logged at WARNING so typos surface.
        """
        if event_type not in JOURNAL_EVENT_TYPES:
            log.warning("operator_journal: unknown event_type %r", event_type)
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            **_normalize(payload),
        }
        path = journal_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(row, default=str) + "\n"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            log.exception("operator_journal: append failed for %s", event_type)


_default = JournalWriter()


def append(event_type: str, payload: dict[str, Any]) -> None:
    """Module-level convenience binding to the default writer."""
    _default.append(event_type, payload)


def read_recent(limit: int = 50, *, days: int = 7) -> list[dict[str, Any]]:
    """Return up to ``limit`` rows from the most recent ``days`` files,
    newest-first. Malformed lines are logged and skipped.
    """
    out: list[dict[str, Any]] = []
    root = journal_dir()
    if not root.exists():
        return out
    files = sorted(
        (p for p in root.iterdir() if p.suffix == ".jsonl"),
        reverse=True,
    )
    for path in files[:days]:
        rows: list[dict[str, Any]] = []
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    log.warning(
                        "operator_journal: skip malformed line in %s", path.name
                    )
        except OSError:
            log.exception("operator_journal: read failed for %s", path)
            continue
        out.extend(reversed(rows))
        if len(out) >= limit:
            break
    return out[:limit]
