"""Yesterday-activity pre-fetcher for the daily brief's ``## Yesterday
with you`` section.

The section's underlying agent is ``mission-digest``: the name is a
locked schema, along with the renderer's ``SECTION_ORDER`` and the
workspace-payload key ``yesterday_with_you``. This module reads
:class:`AgendaStore
<tesseract.orchestrator.autonomy.agenda_store.AgendaStore>` records
directly off disk and returns the items whose status transitioned to
``done`` or ``blocked`` inside the window — operator-visible work that
actually happened.

Pure I/O, fail-soft per record. No LLM calls.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

from tesseract.orchestrator.brief._window import (
    DEFAULT_SINCE_HOURS,
    day_window,
    in_window,
    parse_iso,
)

log = logging.getLogger(__name__)

MAX_ITEMS = 20

_TERMINAL_STATUSES_OF_INTEREST = ("done", "blocked")


def collect_yesterday_activity(
    *,
    home: Path,
    target_date: date,
    since_hours: int = DEFAULT_SINCE_HOURS,
) -> dict[str, Any]:
    """Read ``<home>/agenda/`` for items that went DONE or BLOCKED inside
    the window ending at ``target_date`` midnight UTC.

    Anchored on ``target_date`` (not wall-clock ``now``), so a re-render
    or backfill for an earlier date reads that date's window instead of
    today's.

    Walks both ``agenda/active/`` (BLOCKED items stay here — BLOCKED is
    not a terminal status) and ``agenda/archive/**`` (DONE items archive
    immediately per ``AgendaStore.save``). A single malformed or
    non-agenda JSON file (e.g. ``source-pauses.json``) is skipped, not
    fatal.

    ``updated_at`` is bounded on both sides (``cutoff <= updated_at <=
    anchor``) — the upper bound matters when the ``/brief`` REPL tool
    backfills a past date, so an item that transitioned after that
    date's window doesn't leak into a stale digest. Results are
    de-duplicated by ``id`` in case a
    record is caught by the scan in both ``active/`` and ``archive/``
    mid-transition (``AgendaStore._archive`` writes the archive copy
    then unlinks the active one in a separate step).
    """
    cutoff, anchor = day_window(target_date, since_hours)
    agenda_root = home / "agenda"
    items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    if agenda_root.exists():
        for jf in _iter_agenda_json(agenda_root):
            record = _load_json(jf)
            if record is None:
                continue
            status = str(record.get("status") or "").strip().lower()
            if status not in _TERMINAL_STATUSES_OF_INTEREST:
                continue
            updated_at = in_window(
                parse_iso(record.get("updated_at")), cutoff, anchor,
            )
            if updated_at is None:
                # Upper bound matters for backfill/re-render of a past
                # ``target_date`` (the ``/brief`` REPL tool accepts an
                # arbitrary date) — without it, an item that went
                # DONE/BLOCKED after that date's window would leak into
                # a stale day's digest.
                continue
            item_id = str(record.get("id") or "").strip()
            if item_id and item_id in seen_ids:
                # Belt-and-suspenders: an item can theoretically appear
                # under both active/ and archive/ mid-transition
                # (``AgendaStore._archive`` writes the archive copy then
                # unlinks active in a second, non-atomic step). Keep the
                # first occurrence only.
                continue
            if item_id:
                seen_ids.add(item_id)
            items.append(
                {
                    "status": status,
                    "goal": str(record.get("goal") or "").strip(),
                    "blocked_reason": str(record.get("blocked_reason") or "").strip(),
                    "source": str(record.get("source") or "").strip(),
                    "updated_at": updated_at.isoformat(),
                }
            )
    items.sort(key=lambda r: r["updated_at"], reverse=True)
    return {
        "since_hours": since_hours,
        "items": items[:MAX_ITEMS],
    }


def _iter_agenda_json(root: Path):
    for path in root.rglob("*.json"):
        if path.is_file():
            yield path


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        log.info("brief activity: skipping malformed json %s", path.name)
        return None
    return data if isinstance(data, dict) else None


__all__ = [
    "collect_yesterday_activity",
    "MAX_ITEMS",
]
