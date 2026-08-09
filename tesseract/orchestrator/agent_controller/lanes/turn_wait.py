"""The wait loop's bookkeeping, shared by the in-process manager and the
IPC proxy.

Both waiters read the same event stream and must reach the same verdict, so
the rule that decides "is this event mine, and does it end my turn" lives
here once. Keeping it out of both loops is what stops them drifting back
into two notions of completion — which is how they came to stop at the
first `turn_ended` they saw.

Two signals, deliberately separated on `TurnPoll`:

- ``events`` are only the polled turn's own.
- ``lane_active`` says whether the LANE emitted anything at all, mine or
  not. That is what extends the silence deadline: a turn queued behind a
  long-running sibling has no events of its own for as long as the sibling
  runs, and bounding it on its own silence abandons a healthy queued turn.
  Over IPC the daemon filters before the wire, so without this flag the
  remote waiter could not tell a busy lane from a dead one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from .models import LaneEvent, TurnOutcome


class TurnPoll(BaseModel):
    """One turn-scoped read of a lane's event stream.

    The unit both wait loops advance on, and the `lane_turn_read` wire
    shape. `known=False` means the lane never issued this turn — the caller
    must fail closed rather than fall back to "whichever turn ends first"."""

    model_config = ConfigDict(extra="ignore")

    known: bool = True
    lane_active: bool = False
    events: list[LaneEvent] = Field(default_factory=list)
    cursor: str = ""
    completed: bool = False
    is_error: bool = False
    error: str | None = None


@dataclass
class TurnAccumulator:
    """Folds a sequence of `TurnPoll`s down to one turn's outcome."""

    lane_id: str
    turn_id: str
    cursor: str = ""
    events: list[LaneEvent] = field(default_factory=list)
    completed: bool = False
    is_error: bool = False
    error: str | None = None

    def absorb(self, poll: TurnPoll) -> bool:
        """Take one poll. Returns whether the lane showed activity, which is
        what the caller uses to extend its silence deadline."""
        self.cursor = poll.cursor
        self.events.extend(poll.events)
        if poll.completed:
            self.completed = True
            self.is_error = poll.is_error
            self.error = poll.error
        return poll.lane_active

    @property
    def reply_text(self) -> str:
        return "\n\n".join(
            str(event.payload.get("text", "")).strip()
            for event in self.events
            if event.kind == "assistant_text" and event.payload.get("text")
        )

    def outcome(self) -> TurnOutcome:
        return TurnOutcome(
            lane_id=self.lane_id,
            turn_id=self.turn_id,
            completed=self.completed,
            is_error=self.is_error,
            events=list(self.events),
            cursor=self.cursor,
            reply_text=self.reply_text,
            error=self.error,
        )


def scope_to_turn(
    turn_id: str, events: list[LaneEvent], next_cursor: str
) -> TurnPoll:
    """Reduce one raw lane read to the polled turn's share of it.

    The single implementation of "is this event mine": payload `turn_id`
    equality, never position in the stream."""
    mine: list[LaneEvent] = []
    completed = False
    is_error = False
    error: str | None = None
    for event in events:
        if event.payload.get("turn_id") != turn_id:
            continue
        mine.append(event)
        if event.kind == "turn_ended":
            completed = True
            is_error = bool(event.payload.get("is_error"))
            raw_error = event.payload.get("error")
            error = str(raw_error) if raw_error else None
    return TurnPoll(
        known=True,
        lane_active=bool(events),
        events=mine,
        cursor=next_cursor,
        completed=completed,
        is_error=is_error,
        error=error,
    )


__all__ = ["TurnAccumulator", "TurnPoll", "scope_to_turn"]
