"""What the runtime did on a day, read back from its own records.

Everything here already existed on disk and nothing rendered it. A turn writes
one record per turn with a row per step, each carrying an outcome, a reason, a
duration and the door the turn came through; a receipt says what a step left
behind. Three things read those records before this: the atlas builder, boot
recovery, and playbook extraction. No route served them and no surface drew
them, so the honest answer to "what did it do yesterday" was to open a
directory of JSON.

**One reader, three callers.** The panel's route, the range route, and the tool
that answers the same question in a chat window or on a phone. That is the
same-eyes ruling and it is a requirement rather than tidiness: a reader only
the cockpit can reach is a reader that does not exist when the operator is away
from the desk, and building a second, channel-shaped digest is the fork the
funnel rule exists to prevent.

**It reads and never writes.** A panel that repaired what it was reading would
be a surface with an opinion about the record, and the record is evidence.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from tesseract.kernel.tools.receipt import Receipt
from tesseract.lib.clock import to_local
from tesseract.orchestrator.outcome import RunOutcome, worst_of
from tesseract.orchestrator.turns import receipts as receipts_log
from tesseract.orchestrator.turns.manifest import (
    TurnManifestStore,
    door_name,
    read_step_name,
    turn_label,
)
from tesseract.scheduler.pipeline.manifest import RunManifest

log = logging.getLogger(__name__)

#: How many days one call may span. A window is a reading aid, not an export,
#: and a reader that walks a year of directories inside a request is a stall
#: the operator experiences as the app hanging.
MAX_SPAN_DAYS = 31

#: Step kinds the RUNTIME writes about its own filing rather than about work.
#: The same set `what_it_reached` walks past, and for the same reason: a reader
#: taking the last row would otherwise describe our bookkeeping.
_BOOKKEEPING = frozenset({"turn"})


@dataclass(frozen=True)
class ToolCall:
    """One tool call, as its own turn recorded it."""

    at: datetime
    tool: str
    outcome: RunOutcome
    reason: str
    ms: float
    turn: str
    door: str
    #: What it left behind, when it left anything. `None` covers three
    #: different cases and the OUTCOME is what tells them apart: the tool
    #: cannot leave marks, it had nothing to leave this time, or it owed one
    #: and did not produce it, which is `unverified`.
    receipt: Receipt | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "tool": self.tool,
            "outcome": self.outcome.value,
            "reason": self.reason,
            "ms": round(self.ms, 1),
            "turn": self.turn,
            "door": self.door,
            "receipt": self.receipt.to_dict() if self.receipt else None,
        }


@dataclass(frozen=True)
class ToolDay:
    """One tool's whole day: how often, how it went, what it left."""

    tool: str
    calls: int
    by_outcome: dict[str, int]
    receipts: int

    @property
    def clean(self) -> bool:
        return self.by_outcome.get(RunOutcome.SUCCEEDED.value, 0) == self.calls

    def as_json(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "calls": self.calls,
            "byOutcome": dict(self.by_outcome),
            "receipts": self.receipts,
        }


@dataclass(frozen=True)
class TurnRow:
    """One turn, and the door somebody reached it through.

    The door is the one thing a per-tool view cannot show, and it is the
    difference between a failure the operator watched happen and one that
    happened on their phone while they were asleep.
    """

    turn: str
    at: datetime
    entry: str
    label: str
    tools: tuple[str, ...]
    outcome: RunOutcome
    task_id: str = ""
    task_outcome: str = ""
    task_verified_by: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "turn": self.turn,
            "at": self.at.isoformat(),
            "entry": self.entry,
            "label": self.label,
            "door": door_name(self.entry),
            "tools": list(self.tools),
            "outcome": self.outcome.value,
            "taskId": self.task_id,
            "taskOutcome": self.task_outcome,
            "taskVerifiedBy": self.task_verified_by,
        }


@dataclass(frozen=True)
class DayReport:
    """Everything the three surfaces need, from one pass over the day."""

    days: tuple[date, ...]
    turns: tuple[TurnRow, ...]
    calls: tuple[ToolCall, ...]
    tools: tuple[ToolDay, ...]
    #: Turns whose record could not be read at all. Reported rather than
    #: dropped: "nothing happened" and "this reader could not tell you" are
    #: different answers, and a panel that cannot separate them is the defect
    #: the whole result vocabulary exists to prevent.
    unreadable: int = 0
    by_door: dict[str, int] = field(default_factory=dict)

    @property
    def trouble(self) -> tuple[ToolCall, ...]:
        """Every call that was not a clean success, in the order it happened.

        `unverified` is in here. It is not a failure, and a panel that showed
        only failures would be silent about the calls nobody can check, which
        are the ones this whole phase was built to surface.
        """
        return tuple(c for c in self.calls if c.outcome is not RunOutcome.SUCCEEDED)

    def as_json(self) -> dict[str, Any]:
        counts = Counter(c.outcome.value for c in self.calls)
        return {
            "days": [d.isoformat() for d in self.days],
            "turns": [t.as_json() for t in self.turns],
            "calls": [c.as_json() for c in self.calls],
            "tools": [t.as_json() for t in self.tools],
            "trouble": [c.as_json() for c in self.trouble],
            "byOutcome": dict(counts),
            # Keyed by the raw entry so a caller can still group on it,
            # and carrying the words beside it so no surface has to invent
            # them. A panel printing `channel:telegram` is printing a slug.
            "byDoor": [
                {"entry": entry, "name": door_name(entry), "turns": n}
                for entry, n in sorted(self.by_door.items())
            ],
            "turnCount": len(self.turns),
            "callCount": len(self.calls),
            "unreadable": self.unreadable,
        }


def span(on: date, through: date | None = None) -> tuple[date, ...]:
    """The days a request covers, oldest first, capped and never reversed.

    A reversed pair is read as the range the caller meant rather than refused:
    the two ends of a date picker are the same two numbers whichever way round
    they arrive, and refusing would be a validation error where an obvious
    reading exists.
    """
    end = through or on
    first, last = (on, end) if on <= end else (end, on)
    days = (last - first).days + 1
    if days > MAX_SPAN_DAYS:
        first = last - timedelta(days=MAX_SPAN_DAYS - 1)
    return tuple(
        first + timedelta(days=offset) for offset in range((last - first).days + 1)
    )


def day(on: date, through: date | None = None) -> DayReport:
    """What the runtime did, from the records it already keeps.

    Reads whole day directories, so it is IO bound and belongs off the event
    loop. Every caller wraps it: a route in `asyncio.to_thread`, the tool in
    the same, which is what `playbook_usage` already does for the same reason.
    """
    days = span(on, through)
    store = TurnManifestStore()

    calls: list[ToolCall] = []
    turns: list[TurnRow] = []
    doors: Counter[str] = Counter()
    unreadable = 0

    for stamped in days:
        marks = receipts_log.read_day(stamped)
        for path in store.closed_on(stamped):
            try:
                manifest = RunManifest.from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (OSError, ValueError, KeyError):
                # One bad record costs its own row and not the day's. This is
                # rendered to a person, and a panel that refuses to draw
                # because of one corrupt file tells them nothing at all.
                log.warning("day reader: unreadable turn record at %s", path)
                unreadable += 1
                continue
            turns.append(_turn_row(manifest, marks, calls))
            doors[manifest.entry or "unknown"] += 1

    calls.sort(key=lambda c: c.at)
    turns.sort(key=lambda t: t.at)
    return DayReport(
        days=days,
        turns=tuple(turns),
        calls=tuple(calls),
        tools=_by_tool(calls),
        unreadable=unreadable,
        by_door=dict(doors),
    )


def _turn_row(
    manifest: RunManifest,
    marks: dict[tuple[str, str], Receipt],
    into: list[ToolCall],
) -> TurnRow:
    """One turn's row, appending its tool calls to `into` as it goes.

    Both in one pass because both walk the same rows, and walking a day twice
    to build two shapes of the same fact is how the two come to disagree about
    how many calls there were.
    """
    tools: list[str] = []
    step_outcomes: list[RunOutcome] = []
    for row in manifest.rows:
        kind, name = read_step_name(row.stage)
        if not kind or kind in _BOOKKEEPING:
            continue
        step_outcomes.append(row.outcome)
        if kind != "tool":
            continue
        tools.append(name)
        into.append(
            ToolCall(
                at=row.started_at or manifest.started_at,
                tool=name,
                outcome=row.outcome,
                reason=row.reason,
                ms=row.duration_ms,
                turn=manifest.run_id,
                door=manifest.entry,
                receipt=marks.get((manifest.run_id, row.stage)),
            )
        )
    return TurnRow(
        turn=manifest.run_id,
        at=manifest.started_at,
        entry=manifest.entry,
        label=turn_label(manifest.entry),
        tools=tuple(dict.fromkeys(tools)),
        outcome=worst_of(step_outcomes),
        task_id=manifest.task_id,
        task_outcome=manifest.task_outcome,
        task_verified_by=manifest.task_verification_by,
    )


def _by_tool(calls: list[ToolCall]) -> tuple[ToolDay, ...]:
    """The same calls grouped by tool, busiest first.

    Ties break on the name so two runs over one day produce the same order.
    A panel whose rows move between refreshes reads as though the numbers
    moved.
    """
    grouped: dict[str, list[ToolCall]] = {}
    for call in calls:
        grouped.setdefault(call.tool, []).append(call)
    rows = [
        ToolDay(
            tool=tool,
            calls=len(mine),
            by_outcome=dict(Counter(c.outcome.value for c in mine)),
            receipts=sum(
                1 for c in mine if c.receipt and c.receipt.points_at_something
            ),
        )
        for tool, mine in grouped.items()
    ]
    rows.sort(key=lambda r: (-r.calls, r.tool))
    return tuple(rows)


def latest_day_with_records() -> date | None:
    """The newest day a record exists for, which is what a picker opens on.

    `None` when nothing has been recorded yet, and the caller says so rather
    than defaulting to today and drawing an empty day as though it were a quiet
    one.
    """
    found = TurnManifestStore().days_with_records()
    return found[-1] if found else None


def today_local() -> date:
    """The operator's today. One clock, per the timezone rule."""
    return to_local(datetime.now()).date()


__all__ = [
    "DayReport",
    "MAX_SPAN_DAYS",
    "ToolCall",
    "ToolDay",
    "TurnRow",
    "day",
    "latest_day_with_records",
    "span",
    "today_local",
]
