"""Yesterday-activity pre-fetcher for the daily brief's ``## Yesterday
with you`` section.

The mission engine was deleted (P4 prune wave 1); the section's
underlying agent (``mission-digest``, name unchanged — the renderer's
``SECTION_ORDER`` and the workspace-payload key ``yesterday_with_you``
are a locked schema) now reports real autonomy work instead. This
module reads the still-live :class:`AgendaStore
<tesseract.orchestrator.autonomy.agenda_store.AgendaStore>` records
directly off disk and returns the items whose status transitioned to
``done`` or ``blocked`` inside the window — the same "operator-visible
work that actually happened" the deleted mission registry used to
supply.

Pure I/O, mirrors :mod:`tesseract.orchestrator.brief.ecosystem`'s
fail-soft per-record scan. No LLM calls.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_SINCE_HOURS = 24
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

    Anchored on ``target_date`` (not wall-clock ``now``) — same
    convention as :func:`~tesseract.orchestrator.brief.ecosystem.collect_ecosystem_inputs`,
    so a re-render or backfill for an earlier date reads that date's
    window instead of today's.

    Walks both ``agenda/active/`` (BLOCKED items stay here — BLOCKED is
    not a terminal status) and ``agenda/archive/**`` (DONE items archive
    immediately per ``AgendaStore.save``). A single malformed or
    non-agenda JSON file (e.g. ``source-pauses.json``) is skipped, not
    fatal — mirrors ``collect_ecosystem_inputs``'s per-record fail-soft
    contract.

    ``updated_at`` is bounded on both sides (``cutoff <= updated_at <=
    anchor``) — the upper bound matters when the ``/brief`` REPL tool
    backfills a past date, so an item that transitioned after that
    date's window doesn't leak into a stale digest (mirrors
    ``ecosystem.py::_read_provider_watch``'s ``entry_date >
    target_date`` guard). Results are de-duplicated by ``id`` in case a
    record is caught by the scan in both ``active/`` and ``archive/``
    mid-transition (``AgendaStore._archive`` writes the archive copy
    then unlinks the active one in a separate step).
    """
    anchor = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
    cutoff = anchor - timedelta(hours=since_hours)
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
            updated_at = _parse_iso(record.get("updated_at"))
            if updated_at is None or updated_at < cutoff or updated_at > anchor:
                # Upper bound matters for backfill/re-render of a past
                # ``target_date`` (the ``/brief`` REPL tool accepts an
                # arbitrary date) — without it, an item that went
                # DONE/BLOCKED after that date's window would leak into
                # a stale day's digest. Mirrors
                # ``ecosystem.py::_read_provider_watch``'s
                # ``entry_date > target_date`` guard.
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


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


__all__ = [
    "collect_yesterday_activity",
    "DEFAULT_SINCE_HOURS",
    "MAX_ITEMS",
]
