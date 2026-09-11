"""What was already said, so it is not said again.

Stage 4 is the only stage with memory. Every other one reads the record and
decides; this one has to know what the operator was told an hour ago, because
the whole question it answers is whether a fault is new.

One small JSON file beside the watchman's other artifacts, keyed by a
finding's identity: `<source>/<kind>/<subject>`. It holds when the fault was
first reported and when it was last seen, and nothing else — the record on
disk is where a fault's evidence lives, and duplicating it here would give the
report two sources of truth about the same failure.

**A fault that disappears is a recovery, and a recovery is worth one line.**
That is why entries survive a tick in which their fault did not appear: the
next tick is where the store notices it is gone and says so, once.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# How long a recovered fault stays in the file after it is announced. Long
# enough that a fault flapping across two ticks is recognised as the same one
# returning rather than as a new arrival, short enough that the file is a
# record of now and not a history.
FORGET_AFTER = timedelta(days=2)

# How long a standing fault WITH a declared remedy stays quiet between
# mentions. Beside `FORGET_AFTER` because the two are one setting really: what
# this file is for is how often the operator hears about something that has
# not changed. A day, because the complaint this stage exists to answer was a
# channel repeating itself every hour, and because the thing being re-announced
# is what the remedy has managed since yesterday, which is a day's worth of
# news or none.
REANNOUNCE_AFTER = timedelta(days=1)


@dataclass(frozen=True)
class Standing:
    """One fault the operator has already been told about."""

    key: str
    first_reported: datetime
    last_seen: datetime
    # Set when the fault stopped appearing, cleared if it comes back. A
    # recovery is announced on the tick that sets it and never again.
    recovered_at: datetime | None = None
    announced_recovery: bool = False
    # Set when the fault was spoken a SECOND time because nobody was working on
    # it. Once, not every tick: the point is that the silence was wrong, not
    # that the fault is loud. Cleared the moment someone takes it on, so a
    # fault dropped again is heard again.
    announced_unowned: bool = False
    # When this fault was last put in front of the operator, including the tick
    # it first appeared on. A fault with a declared remedy is announced again
    # once `REANNOUNCE_AFTER` has passed; one without keeps the flag above,
    # which says once and then never. Both fields, because they answer
    # different questions and collapsing them would make a remedy's daily line
    # depend on whether anyone owns the fault.
    last_announced: datetime | None = None
    summary: str = ""


def store_path() -> Path:
    from tesseract.paths import home_dir

    return home_dir() / "autonomy" / "watchman-standing.json"


def key_for(finding) -> str:
    """The identity a fault keeps across ticks.

    Not the summary: it carries counts, and a fault reported `×2` this hour
    and `×5` the next is one standing fault, not two.
    """
    return f"{finding.source}/{finding.kind}/{finding.subject}"


def load() -> dict[str, Standing]:
    """Never raises. An unreadable store means every fault reads as new, which
    is noisy for one tick and is the only safe direction: the alternative is a
    parse error silencing a real fault."""
    path = store_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("watchman: the standing-fault store is unreadable; treating all as new")
        return {}
    out: dict[str, Standing] = {}
    for key, entry in (raw or {}).items():
        first = _parse(entry.get("first_reported"))
        last = _parse(entry.get("last_seen"))
        if first is None or last is None:
            continue
        out[str(key)] = Standing(
            key=str(key),
            first_reported=first,
            last_seen=last,
            recovered_at=_parse(entry.get("recovered_at")),
            announced_recovery=bool(entry.get("announced_recovery")),
            announced_unowned=bool(entry.get("announced_unowned")),
            # Absent in a store written before remedies existed. `None` reads
            # as never announced, so the first tick after an upgrade says the
            # remedy line once and the clock starts from there.
            last_announced=_parse(entry.get("last_announced")),
            summary=str(entry.get("summary") or ""),
        )
    return out


def save(entries: dict[str, Standing], *, now: datetime) -> Path:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    keep = {
        key: entry
        for key, entry in entries.items()
        if entry.recovered_at is None or now - entry.recovered_at < FORGET_AFTER
    }
    payload = {
        key: {
            "first_reported": entry.first_reported.isoformat(),
            "last_seen": entry.last_seen.isoformat(),
            "recovered_at": entry.recovered_at.isoformat() if entry.recovered_at else None,
            "announced_recovery": entry.announced_recovery,
            "announced_unowned": entry.announced_unowned,
            "last_announced": (
                entry.last_announced.isoformat() if entry.last_announced else None
            ),
            "summary": entry.summary,
        }
        for key, entry in keep.items()
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return path


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


__all__ = [
    "FORGET_AFTER",
    "REANNOUNCE_AFTER",
    "Standing",
    "key_for",
    "load",
    "save",
    "store_path",
]
