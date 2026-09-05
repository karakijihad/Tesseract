"""Workspace pre-fetcher for the daily brief's ``## Yesterday in TESSERACT``
section.

The section's agent is ``workspace-digest``, whose card says it reads the
workspace event and comment streams "through your existing read tools". The
invoker calls ``adapter.generate(prompt)``, which attaches no tools at all, so
until now the agent was handed ``{"since_hours": 24}`` and nothing else. An
agent given a number and no way to look anything up can only invent, which is
what this phase exists to stop, so the section was rendered empty instead.

This module hands it the stream. Events come off
:meth:`EventStore.list_events` and comments off
:meth:`EventStore.list_all_comments`, both public readers on the class that
owns the file, and the rows are reduced to what the card is allowed to say out
loud: the kind, the title, the summary the producer wrote, the status and who
authored it. **The payload
never leaves this module** — the card forbids quoting raw payloads, file paths
and JSONL fragments, and the surest way to honour that is not to send them.

An event counts for a day if it was WRITTEN in the window or DECIDED in it. A
soul growth proposed on Monday and approved on Tuesday is Tuesday's news: the
approval is the change the operator made, and dropping it because the row is
older is how the most meaningful thing a person did all day goes unmentioned.

Pure I/O, fail-soft per row. No LLM calls.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from tesseract.orchestrator.brief._window import (
    DEFAULT_SINCE_HOURS,
    day_window,
    in_window,
    parse_iso,
)

log = logging.getLogger(__name__)

MAX_EVENTS = 20
MAX_COMMENTS = 20

# How many events are read before the window is applied. The store is
# append-only and `list_events` already collapses to the newest row per id,
# newest first, so a window of one day sits well inside this.
_SCAN_LIMIT = 400


def collect_workspace_activity(
    *,
    event_store: Any,
    target_date: date,
    since_hours: int = DEFAULT_SINCE_HOURS,
) -> dict[str, Any]:
    """The workspace stream for the day ending at ``target_date`` midnight.

    The window and the timestamp reading are :mod:`_window`'s, shared with the
    other two pre-fetchers so a correction to either reaches all three.
    """
    cutoff, anchor = day_window(target_date, since_hours)

    events: list[dict[str, Any]] = []
    try:
        rows = event_store.list_events(limit=_SCAN_LIMIT)
    except Exception:  # noqa: BLE001 — a missing section beats a failed brief
        log.exception("brief workspace: the event stream could not be read")
        rows = []
    for event in rows:
        written = parse_iso(getattr(event, "ts", None))
        decided = parse_iso(getattr(event, "decided_at", None))
        moment = in_window(written, cutoff, anchor) or in_window(decided, cutoff, anchor)
        if moment is None:
            continue
        events.append(
            {
                "kind": str(getattr(event, "kind", "") or ""),
                "title": str(getattr(event, "title", "") or "").strip(),
                "summary": str(getattr(event, "summary", "") or "").strip(),
                "status": str(getattr(event, "status", "") or ""),
                "author": str(getattr(event, "author_display", "") or "").strip(),
                "at": moment.isoformat(),
                # Whether the operator acted on it in this window, which is a
                # different day's news from it having been raised.
                "decided_in_window": in_window(decided, cutoff, anchor) is not None,
            }
        )
    events.sort(key=lambda r: r["at"], reverse=True)

    comments = _commentsin_window(event_store, cutoff, anchor)

    return {
        "since_hours": since_hours,
        "events": events[:MAX_EVENTS],
        "comments": comments[:MAX_COMMENTS],
    }


def _commentsin_window(
    event_store: Any, cutoff: datetime, anchor: datetime,
) -> list[dict[str, Any]]:
    """Every comment written in the window, whoever wrote it.

    Both sides are kept: an operator note and the reply it drew are one item
    to the card, and it cannot see that from the operator's half alone.
    """
    try:
        rows = event_store.list_all_comments(limit=_SCAN_LIMIT)
    except Exception:  # noqa: BLE001 — a missing band beats a failed brief
        log.exception("brief workspace: the comment stream could not be read")
        return []
    out: list[dict[str, Any]] = []
    for comment in rows:
        moment = in_window(parse_iso(getattr(comment, "ts", None)), cutoff, anchor)
        if moment is None:
            continue
        out.append(
            {
                "author": str(getattr(comment, "author", "") or ""),
                "body": str(getattr(comment, "body", "") or "").strip(),
                "at": moment.isoformat(),
            }
        )
    out.sort(key=lambda r: r["at"], reverse=True)
    return out


__all__ = [
    "MAX_COMMENTS",
    "MAX_EVENTS",
    "collect_workspace_activity",
]
