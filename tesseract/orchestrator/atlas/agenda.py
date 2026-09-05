"""What the machine decided to do, beside what it did and what it learnt.

The atlas joined four trees and one was missing: `agenda/` holds what wants the
operator, what is paused, what was decided and what it cost, and no builder
read any of it. So the map could answer what ran and what it filed, and could
not answer why it ran or what it was for — a run was an effect with no cause,
which is the same hole AR-9e names from the other side.

**And nothing declared the gap**, so the map was quietly missing the agenda
rather than openly.

What is a node and what is a field:

* An item is a node. It has a goal, a file, a status and a history.
* What it is SCORED is a field. `priority_score` and its components are a
  number about one item, not a thing to find, and the record shows them.
* `spend/` is not drawn. It is a running total, and what it answers is an
  amount over a window rather than a thing to point at. It is evidence an item
  may cite.

**How far back is the body's own dial and not a second one.** An item is drawn
when it moved inside `runs_within_days`, the same window that decides how much
of the run log is drawn. Two dials would mean whichever was smaller did the
bounding while the other appeared to, which is a shape this repo has paid for
once.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tesseract.orchestrator.atlas.builders import Emitter, content_hash, stamp
from tesseract.orchestrator.atlas.config import ReviewWindows
from tesseract.orchestrator.atlas.model import NodeKind
from tesseract.orchestrator.atlas.store import Atlas

log = logging.getLogger(__name__)

#: Where live items and finished ones sit, relative to the agenda root. Named
#: rather than swept, for the reason `trees.py` names its two: an `rglob` over
#: the root would start drawing `spend/` and `source-pauses.json` the moment
#: either changed shape.
ACTIVE_DIR = "active"
ARCHIVE_DIR = "archive"


def agenda_node_id(item_id: str) -> str:
    """`agenda:<id>` — the id the store minted, adopted rather than invented.

    An agenda id is already unique, already portable and already what the
    index, the panel and the reply thread key on.
    """
    return f"agenda:{item_id}"


def build_agenda(
    atlas: Atlas,
    root: Path,
    *,
    now: datetime,
    version: int,
    windows: ReviewWindows,
    within_days: int,
    reuse: dict[str, tuple[str, str]] | None = None,
) -> int:
    """How many items reached the map.

    **No edge is drawn out of an item toward a scheduled run, and that is a
    finding rather than an omission.** The edge that does exist is drawn by
    `turns.py`: a conversation turn that takes up a task records its id, and
    the turn points at the item. No scheduled run records which item it acted on.
    The only producer that ever came close recorded the SLUG it published, and
    `mint_agenda_id` truncates that slug at 40 characters when it builds the
    id, so one of the two surviving rows names a slug no id carries. That
    producer has since been removed from the runtime, so the field is
    archaeology and not a live join. Recovering it would mean reproducing a
    truncation the agenda store owns inside a reader, which is the same defect
    the evidence stamp was made one constant to avoid.

    When a producer records the item ID, this becomes one row in the run
    builder's own table. Until then `report.NOT_INDEXED` says the map cannot
    answer which run carried out a decision.

    Fail-soft on an unreadable tree, like every other builder that reads
    runtime state: a machine that has never proposed anything is the ordinary
    case on a fresh install.
    """
    emit = Emitter(atlas, now=now, version=version, windows=windows)
    cached = reuse or {}
    cutoff = now - timedelta(days=within_days)
    seen = 0

    for path in _files(root):
        item = _read(path)
        if item is None:
            continue
        moved = _moment(item.get("updated_at")) or _moment(item.get("created_at"))
        # Bounded on both sides, the same as the run log: a rebuild asked to
        # reproduce an older pass has to see the agenda that pass saw, or
        # every item decided since reads as drift.
        if moved is None or moved < cutoff or moved > now:
            continue
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            continue
        node_id = agenda_node_id(item_id)
        raw_stamp = stamp(path)
        known = cached.get(node_id)
        emit.node(
            node_id,
            NodeKind.AGENDA,
            # The goal, which is what the item IS. It runs long — a median of
            # 123 characters on this machine — and `Emitter.node` shortens it
            # to the ceiling; the whole goal, the rationale and every status
            # it passed through are the record's own fields, which the panel
            # renders.
            str(item.get("goal") or item_id),
            f"{_relative(path, root)}",
            content=(
                known[1]
                if raw_stamp and known and known[0] == raw_stamp
                else content_hash(path)
            ),
            input_stamp=raw_stamp,
            aliases=(item_id,),
        )
        seen += 1
    return seen


def _files(root: Path) -> list[Path]:
    out: list[Path] = []
    for directory in (root / ACTIVE_DIR, root / ARCHIVE_DIR):
        try:
            out.extend(sorted(directory.rglob("*.json")))
        except OSError:
            log.warning("atlas: %s could not be read; it is not drawn", directory.name)
    return out


def _read(path: Path) -> dict | None:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("atlas: one agenda item could not be read")
        return None
    return parsed if isinstance(parsed, dict) else None


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _moment(value) -> datetime | None:
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
    "ACTIVE_DIR",
    "ARCHIVE_DIR",
    "agenda_node_id",
    "build_agenda",
]
