"""How a playbook did, per revision, over a window.

The usage log says a playbook was read, and which revision. The turn records
say how the turn it was read in ended. The join between them is a function
that already exists: a turn id is minted from the session id
(`turns/manifest.py::turn_session_id`), and a load row carries that session
id and the moment it was read. So a load is joined to the closed turn of the
same session whose span contains that moment, and the turn's own closing
outcome is the playbook's.

**Measured against the version, not the name.** The point of a revision is
that a later one which measures worse can be told apart from the one it
replaced, and returned to. Two revisions of one playbook are two rows here.

**Nothing here invents a number.** A load with no closed turn to join to is
counted as unjoined and is not a success or a failure. A revision with no
loads has no `trouble` to compare, and `worse_than` says so with `None`
rather than a ratio over nothing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from tesseract.orchestrator.turns.manifest import read_step_name, turn_session_id

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Reuse:
    """One revision's record over the window."""

    version: str
    loads: int = 0
    #: Turns it was read in that closed `succeeded`.
    succeeded: int = 0
    #: Turns it was read in that closed any other way.
    failed: int = 0
    #: Loads with no closed turn of that session around them: an open turn,
    #: a turn outside the window, or a read from somewhere that records no
    #: turn. Neither a success nor a failure.
    unjoined: int = 0
    corrections: int = 0
    #: Reads of the same revision after the first inside one turn. The
    #: assistant going back to the procedure mid-task is the cheapest sign
    #: that the procedure did not carry it through.
    retries: int = 0

    @property
    def trouble(self) -> float | None:
        """Failed turns and corrections over loads, or None with no loads."""
        if not self.loads:
            return None
        return (self.failed + self.corrections) / self.loads

    def as_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "loads": self.loads,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "unjoined": self.unjoined,
            "corrections": self.corrections,
            "retries": self.retries,
            "trouble": self.trouble,
        }


@dataclass(frozen=True)
class ClosedTurn:
    session_id: str
    started_at: datetime
    completed_at: datetime
    succeeded: bool


def closed_turns(root: Path, *, since: datetime, until: datetime) -> list[ClosedTurn]:
    """Every closed turn record whose end falls in the window.

    Reads the day directories the window can touch and nothing else; `open/`
    is skipped because an open turn has not ended and cannot be joined to.
    """
    if not root.is_dir():
        return []
    found: list[ClosedTurn] = []
    for day_dir in sorted(p for p in root.iterdir() if p.is_dir() and p.name != "open"):
        try:
            day = datetime.fromisoformat(day_dir.name).date()
        except ValueError:
            continue
        if day < since.date() or day > until.date():
            continue
        for path in day_dir.glob("*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.warning("playbook reuse: unreadable turn record at %s", path)
                continue
            turn = _closed_turn(raw)
            if turn is None or turn.completed_at < since or turn.completed_at > until:
                continue
            found.append(turn)
    return found


def _closed_turn(raw: dict[str, Any]) -> ClosedTurn | None:
    turn_id = str(raw.get("run_id") or "")
    started = _moment(raw.get("started_at"))
    ended = _moment(raw.get("completed_at"))
    if not turn_id or started is None or ended is None:
        return None
    outcome = ""
    for row in raw.get("stages") or []:
        kind, name = read_step_name(str(row.get("stage") or ""))
        if kind == "turn" and name == "end":
            outcome = str(row.get("outcome") or "")
    if not outcome:
        return None
    return ClosedTurn(
        session_id=turn_session_id(turn_id),
        started_at=started,
        completed_at=ended,
        succeeded=outcome == "succeeded",
    )


def turn_record_around(session_id: str, when: datetime, *, root: Path | None = None) -> dict[str, Any] | None:
    """The closed turn record of `session_id` whose span holds `when`, raw.

    Read off the day directory of `when` and its neighbours, because a turn
    that started before midnight closes after it.
    """
    from tesseract.orchestrator.turns import turns_root as live_turns_root

    base = root if root is not None else live_turns_root()
    if not base.is_dir():
        return None
    for offset in (0, -1, 1):
        day_dir = base / (when + timedelta(days=offset)).date().isoformat()
        if not day_dir.is_dir():
            continue
        for path in day_dir.glob("*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            turn_id = str(raw.get("run_id") or "")
            if not turn_id or turn_session_id(turn_id) != session_id:
                continue
            started, ended = _moment(raw.get("started_at")), _moment(raw.get("completed_at"))
            if started is None or ended is None:
                continue
            if started <= when <= ended:
                return raw
    return None


def _moment(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def reuse_by_version(
    name: str,
    *,
    rows: Iterable[dict[str, Any]],
    turns: Iterable[ClosedTurn],
    since: datetime,
) -> dict[str, Reuse]:
    """The record of every revision of `name` read since `since`."""
    by_session: dict[str, list[ClosedTurn]] = {}
    for turn in turns:
        by_session.setdefault(turn.session_id, []).append(turn)

    counts: dict[str, dict[str, int]] = {}
    seen_in_turn: set[tuple[str, str, datetime]] = set()
    for row in rows:
        if row.get("skill") != name:
            continue
        version = str(row.get("version") or "")
        if not version:
            continue
        when = _moment(row.get("ts"))
        if when is None or when < since:
            continue
        tally = counts.setdefault(
            version,
            {"loads": 0, "succeeded": 0, "failed": 0, "unjoined": 0,
             "corrections": 0, "retries": 0},
        )
        if row.get("outcome") == "correction":
            tally["corrections"] += 1
            continue
        tally["loads"] += 1
        turn = _turn_around(by_session.get(str(row.get("session_id") or ""), ()), when)
        if turn is None:
            tally["unjoined"] += 1
            continue
        key = (version, turn.session_id, turn.started_at)
        if key in seen_in_turn:
            tally["retries"] += 1
        seen_in_turn.add(key)
        tally["succeeded" if turn.succeeded else "failed"] += 1

    return {version: Reuse(version=version, **tally) for version, tally in counts.items()}


def _turn_around(turns: Iterable[ClosedTurn], when: datetime) -> ClosedTurn | None:
    for turn in turns:
        if turn.started_at <= when <= turn.completed_at:
            return turn
    return None


def worse_than(live: Reuse | None, before: Reuse | None) -> bool | None:
    """Whether `live` measured worse than `before`. None when either has
    nothing measured, because a comparison over nothing is not a finding."""
    if live is None or before is None:
        return None
    if live.trouble is None or before.trouble is None:
        return None
    return live.trouble > before.trouble


def measure_all(
    names: Iterable[str],
    *,
    window_days: int,
    now: datetime | None = None,
    usage_rows: Iterable[dict[str, Any]] | None = None,
    turns_root: Path | None = None,
) -> dict[str, dict[str, Reuse]]:
    """`measure` for several playbooks, over ONE pass of the log and the turns.

    Same arithmetic, read once. `measure` reads the whole usage log and walks
    the turn tree per name, which is right for the one-playbook question the
    refinement job asks and wrong for a panel asking about every playbook at
    once: six of them is six walks of the same fortnight, and the walk is the
    expensive half.

    A name with nothing measured comes back as an empty mapping rather than
    being dropped. That is the interesting half here for the same reason a
    zero row is in `tool_usage.rollup`: a playbook carried on every turn and
    never once read is what the panel exists to show, and a reader that
    returns only what the log holds cannot show one.
    """
    from tesseract.brain.skill_usage import read_usage
    from tesseract.orchestrator.turns import turns_root as live_turns_root

    until = now or datetime.now(timezone.utc)
    since = until - timedelta(days=window_days)
    rows = list(read_usage() if usage_rows is None else usage_rows)
    root = turns_root if turns_root is not None else live_turns_root()
    turns = closed_turns(root, since=since, until=until)
    return {
        name: reuse_by_version(name, rows=rows, turns=turns, since=since)
        for name in names
    }


def measure(
    name: str,
    *,
    window_days: int,
    now: datetime | None = None,
    usage_rows: Iterable[dict[str, Any]] | None = None,
    turns_root: Path | None = None,
) -> dict[str, Reuse]:
    """`reuse_by_version` over the live usage log and turn tree, for one name.

    One name is the degenerate case of several, so this delegates rather than
    repeating the window arithmetic and the two reads. It had its own copy of
    both, which is two places for a change to `since`/`until` to be made in
    and one place for it to be forgotten.
    """
    return measure_all(
        [name],
        window_days=window_days,
        now=now,
        usage_rows=usage_rows,
        turns_root=turns_root,
    ).get(name, {})


__all__ = [
    "turn_record_around",
    "ClosedTurn",
    "Reuse",
    "closed_turns",
    "measure",
    "measure_all",
    "reuse_by_version",
    "worse_than",
]
