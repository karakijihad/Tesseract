"""What the operator has looked at and is content to leave.

The Health room shows what the runtime can see, and some of what it can see is
true, understood and not going to be acted on. Until now there was nothing to
say so with: a row stayed in Needs action until the condition itself changed,
so a fault the operator had read, understood and decided to live with went on
demanding attention every time they opened the panel. A panel that keeps asking
about something already answered teaches them to stop reading it, which is the
failure the whole room exists to prevent.

**This is not the same question as `judge/standing.py`, and they must not be
merged.** Standing asks *have we told them yet*, and exists to stop one fault
being announced twice. This asks *did they say it is fine*. Being told is not
accepting, and a store that conflated the two would quietly mark every fault
the operator was ever notified about as handled.

They do share an identity, deliberately: `standing.key_for` is what decides
whether two sweeps saw the same fault, and an acknowledgement keyed any other
way would drift from it the first time a subject was renamed.

Three rules, and each one is a way this could have gone wrong:

1. **An acknowledged finding is still drawn.** It moves to Operating and says
   it was marked as seen, with the date. It never disappears, because a room
   that goes quiet says nothing is wrong, and the operator asked to stop being
   alarmed rather than to stop being told.
2. **It comes back if it gets worse.** The acknowledgement records the severity
   it was given at, so a spare that was slow and is now refusing is a new
   claim and returns to Needs action on its own.
3. **It is spent when the fault clears.** The condition ending is what ends the
   acknowledgement, so the same fault arriving next month is new again and is
   not silently pre-accepted by a decision made about a different incident.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tesseract.lib.log_envelope import BAD, INFO, WARN

log = logging.getLogger(__name__)

#: Worst last. Comparing positions is how "has it got worse" is answered
#: without a second vocabulary for severity.
_RANK: dict[str, int] = {INFO: 0, WARN: 1, BAD: 2}


@dataclass(frozen=True)
class Acknowledged:
    """One finding the operator has read and left alone."""

    key: str
    at: datetime
    #: What it was rated when they looked. A finding that has since got worse
    #: is a different claim and this is what notices.
    severity: str
    #: Their own words, where they gave any. The room shows it, because "I know,
    #: the key is being replaced on Friday" is worth more to the next reader
    #: than the fact that somebody clicked something.
    note: str = ""

    def covers(self, severity: str) -> bool:
        """Does this acknowledgement still answer a finding rated `severity`?"""
        return _RANK.get(severity, 0) <= _RANK.get(self.severity, 0)

    def cleared(self, severity: str) -> bool:
        """Has the fault they accepted actually ENDED?

        Rule 3 for a row that stays on the panel while it is well, which is
        every collector row. A finding answers rule 3 by disappearing; a
        collector has no way to disappear, so it has to be asked.

        Both halves are needed and each one alone is a real defect:

        - It has to be well NOW. Less broken is not the condition ending. A row
          accepted while it was failing and now merely degraded is the same
          standing fault, and spending the acceptance there asks the operator
          again about the thing they just answered.
        - It has to have been UNWELL when they looked. Otherwise an acceptance
          taken on a row that is already well is spent by the very state it was
          taken in. That is not theoretical: a fact nothing produces is rated
          INFO and most collector rows sit there for months, so the row would
          be unacceptable from every surface.

        `covers` is the same recorded severity read the other way, which is why
        the two live together. Worse than it was asks again, well after being
        unwell is over, and anything between is the fault still standing.
        """
        return _RANK.get(severity, 0) == 0 < _RANK.get(self.severity, 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "severity": self.severity,
            "note": self.note,
        }


def store_path() -> Path:
    """Beside the standing store, and resolved at call time for the same
    reason every other path here is: an import-time constant freezes the
    location before a relocated home is known."""
    from tesseract.paths import home_dir

    return home_dir() / "autonomy" / "watchman-acknowledged.json"


def load() -> dict[str, Acknowledged]:
    """Never raises. An unreadable store means nothing is acknowledged, which
    shows the operator too much rather than too little."""
    path = store_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        log.warning("acknowledged: unreadable store at %s", path)
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Acknowledged] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        at = _parse(value.get("at"))
        if at is None:
            continue
        out[str(key)] = Acknowledged(
            key=str(key),
            at=at,
            severity=str(value.get("severity") or BAD),
            note=str(value.get("note") or ""),
        )
    return out


def save(entries: dict[str, Acknowledged]) -> Path:
    path = store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({k: v.to_dict() for k, v in entries.items()}, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError:
        log.warning("acknowledged: could not write the store at %s", path)
    return path


def acknowledge(key: str, *, severity: str, note: str = "", now: datetime | None = None) -> Acknowledged:
    """Record that the operator has seen `key` and is leaving it."""
    entry = Acknowledged(
        key=key,
        at=(now or datetime.now(timezone.utc)),
        severity=severity,
        note=note.strip(),
    )
    entries = load()
    entries[key] = entry
    save(entries)
    return entry


def forget(key: str) -> bool:
    """Undo one acknowledgement. Returns whether there was one to undo."""
    entries = load()
    if entries.pop(key, None) is None:
        return False
    save(entries)
    return True


def prune(live_keys: set[str]) -> None:
    """Drop acknowledgements whose fault is no longer being found.

    The condition ending is what ends the acknowledgement. Without this a
    decision about today's incident would silently pre-accept the same fault
    arriving next month, which is a different incident and deserves to be
    looked at.
    """
    entries = load()
    keep = {key: value for key, value in entries.items() if key in live_keys}
    if len(keep) != len(entries):
        save(keep)


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


__all__ = [
    "Acknowledged",
    "acknowledge",
    "forget",
    "load",
    "prune",
    "save",
    "store_path",
]
