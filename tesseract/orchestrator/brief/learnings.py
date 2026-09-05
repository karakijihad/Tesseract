"""Consolidator pre-fetcher for the daily brief's ``## What I learned`` section.

The section's agent is ``memory-digest``, whose card says it reads "the dreaming
consolidator's output" and explicitly must NOT read raw memory-store writes,
because every turn writes and that stream would drown the brief. Until now the
agent was handed ``{"since_hours": 24}`` and no tools, so it could only invent,
and the renderer rendered the section empty instead.

**What the consolidator leaves behind is a list of promoted ids.**
``DreamCycleJob`` scores the recall log, lifts the winners into ``MEMORY.md``
and writes a ``nudge`` workspace event carrying
``payload={"promoted": [...], "promoted_count": N}``. That list IS the
distillation the card describes: the cycle already decided which of the day's
memories earned a place, so restating them is not summarising the raw stream.

Two things a later reader should know before reaching for a different source.
The pipeline's stage row does not carry a job's payload, so the promoted ids do
NOT survive into ``runs.jsonl``: the only thing there is ``reason`` reading
``promoted=3``, and parsing a stage's free text is what this phase and AR-8
both refuse. And the nudge is written only when the cycle runs with a Mirror
app attached, so a CLI-only scheduler run promotes silently and this section is
empty for that day, which is correct rather than invented.

Pure I/O, fail-soft per record. No LLM calls.
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

MAX_LEARNINGS = 15

_SCAN_LIMIT = 400


def collect_consolidated_learnings(
    *,
    event_store: Any,
    memory_store: Any,
    target_date: date,
    since_hours: int = DEFAULT_SINCE_HOURS,
) -> dict[str, Any]:
    """What the overnight pass promoted in the day ending at ``target_date``.

    The window and the timestamp reading are :mod:`_window`'s, shared with the
    other two pre-fetchers.
    """
    cutoff, anchor = day_window(target_date, since_hours)

    promoted = _promoted_ids(event_store, cutoff, anchor)
    learnings: list[dict[str, Any]] = []
    for memory_id in promoted:
        record = _read_memory(memory_store, memory_id)
        if record is None:
            continue
        learnings.append(record)
        if len(learnings) >= MAX_LEARNINGS:
            break

    return {"since_hours": since_hours, "learnings": learnings}


def _promoted_ids(event_store: Any, cutoff: datetime, anchor: datetime) -> list[str]:
    """The ids the cycle lifted, newest run first, de-duplicated.

    Found by the shape of the payload rather than by reading the title, so a
    wording change to the nudge cannot silently empty this section.
    """
    try:
        rows = event_store.list_events(kinds=("nudge",), limit=_SCAN_LIMIT)
    except Exception:  # noqa: BLE001 — a missing section beats a failed brief
        log.exception("brief learnings: the event stream could not be read")
        return []
    out: list[str] = []
    seen: set[str] = set()
    for event in rows:
        if str(getattr(event, "source", "") or "") != "orchestrator":
            continue
        if in_window(parse_iso(getattr(event, "ts", None)), cutoff, anchor) is None:
            continue
        payload = getattr(event, "payload", None)
        if not isinstance(payload, dict):
            continue
        ids = payload.get("promoted")
        if not isinstance(ids, list):
            continue
        for raw in ids:
            memory_id = str(raw or "").strip()
            if memory_id and memory_id not in seen:
                seen.add(memory_id)
                out.append(memory_id)
    return out


def _read_memory(memory_store: Any, memory_id: str) -> dict[str, Any] | None:
    """One promoted memory, reduced to what the card may restate.

    ``log_access=False`` because reading a memory to describe it is not a
    recall, and counting it as one would feed the very score that decided it
    was worth promoting.
    """
    if memory_store is None:
        return None
    try:
        result = memory_store.read(memory_id, log_access=False)
    except Exception:  # noqa: BLE001 — one memory, not the section
        log.exception("brief learnings: %s could not be read", memory_id)
        return None
    if result is None:
        return None
    frontmatter, _body = result
    title = str(getattr(frontmatter, "title", "") or "").strip()
    summary = str(getattr(frontmatter, "summary", "") or "").strip()
    if not title and not summary:
        return None
    kind = getattr(frontmatter, "type", None)
    return {
        "title": title,
        "summary": summary,
        "kind": str(getattr(kind, "value", kind) or ""),
        "tags": [str(t) for t in (getattr(frontmatter, "tags", None) or [])],
    }


__all__ = [
    "MAX_LEARNINGS",
    "collect_consolidated_learnings",
]
