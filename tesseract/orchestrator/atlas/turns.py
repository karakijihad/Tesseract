"""A conversation turn on the map, and the task it worked.

The body compartment drew what the scheduler ran and nothing a person asked
for: the turn records AR-19 writes under `runtime/turns/` were read by recovery
and by nobody else. And the one edge the agenda has waited for, which run acted
on which decision, is written on exactly those records now: a turn that takes
up a task carries `task_id`, so the join is a field and not a guess.

What is a node and what is a field: a closed turn is a node, kind `run`, titled
by the door it came through. Its steps, its outcome and how long it took are
the record's own fields, which the reader shows. An open turn is not drawn: it
is running or waiting for the next boot to close it, and either way it has no
end to date it by. The edge is `ran`, the word the body already uses for "one
run of that": a turn is one run of the task, and a third spelling for the same
claim is what `model.CAUSAL_LINKS` exists to refuse.

Bounded by the body's own dial, `runs_within_days`, and on both sides against
`now` by each record's own end, the way the run builder is: the day directory
is only the cheap first filter, because a rebuild asked to prove an older pass
has to see exactly the turns that pass saw, and a turn closing later on the
same day would otherwise read as drift.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from tesseract.orchestrator.atlas.agenda import agenda_node_id
from tesseract.orchestrator.atlas.builders import Emitter, content_hash, stamp
from tesseract.orchestrator.atlas.config import ReviewWindows
from tesseract.orchestrator.atlas.model import Creator, NodeKind, Provenance
from tesseract.orchestrator.atlas.store import Atlas
from tesseract.orchestrator.turns.manifest import turn_label

log = logging.getLogger(__name__)

#: The label half of a turn's locator. `locate.py` recognises it and reads the
#: file from `turns_root()`, which is what decides where the tree is.
LOCATOR_PREFIX = "runtime/turns/"


def turn_node_id(turn_id: str) -> str:
    return f"turn:{turn_id}"


def _day_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and p.name != "open")


def _day_of(path: Path) -> date | None:
    try:
        return date.fromisoformat(path.name)
    except ValueError:
        return None


def _moment(raw: dict) -> datetime | None:
    """When the turn ended, or when it began if the record never says."""
    for key in ("completed_at", "started_at"):
        value = raw.get(key)
        if not isinstance(value, str) or not value:
            continue
        try:
            when = datetime.fromisoformat(value)
        except ValueError:
            continue
        return when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    return None


def build_turns(
    atlas: Atlas,
    root: Path,
    *,
    now: datetime,
    version: int,
    windows: ReviewWindows,
    within_days: int,
) -> int:
    """How many closed turns reached the map.

    After the agenda builder, because the one edge here points AT a task and is
    drawn only when that task is in the graph. A turn naming a task the graph
    does not hold is a fact about the window, not an edge.
    """
    emit = Emitter(atlas, now=now, version=version, windows=windows)
    cutoff = now - timedelta(days=within_days)
    first = cutoff.date()
    last = now.date()
    seen = 0
    for day_dir in _day_dirs(root):
        day = _day_of(day_dir)
        if day is None or day < first or day > last:
            continue
        for path in sorted(day_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.warning("atlas: unreadable turn record at %s", path)
                continue
            turn_id = str(raw.get("run_id") or "").strip()
            if not turn_id:
                continue
            when = _moment(raw)
            if when is None or when < cutoff or when > now:
                continue
            entry = str(raw.get("entry") or "")
            locator = f"{LOCATOR_PREFIX}{day_dir.name}/{path.name}"
            node_id = turn_node_id(turn_id)
            emit.node(
                node_id,
                NodeKind.RUN,
                turn_label(entry),
                locator,
                content=content_hash(path),
                input_stamp=stamp(path),
                aliases=(turn_id,),
            )
            seen += 1
            task_id = str(raw.get("task_id") or "").strip()
            if not task_id:
                continue
            task_node = agenda_node_id(task_id)
            if task_node not in atlas.nodes:
                continue
            emit.edge(
                node_id,
                task_node,
                "ran",
                provenance=Provenance.STATED_IN_SOURCE,
                creator=Creator.RULE,
                locator=f"{locator}#task_id",
                asserted_by=entry or "turn",
            )
    return seen


__all__ = ["LOCATOR_PREFIX", "build_turns", "turn_node_id"]
