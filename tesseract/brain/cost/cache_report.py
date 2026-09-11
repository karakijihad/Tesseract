"""What the prompt cache did, read back off the ledger's own rows.

Pure functions over the lines of `cost-tracking.jsonl`, the way `spent.py` is:
nothing here opens a file or knows where one is. The caller reads the lines and
the route turns what comes back into a panel.

**Why this is not part of `spent.py`.** That module answers what a piece of
work cost and is read by the autonomy history card. This answers where the
prefix broke. The rows are the same rows; the questions do not share a shape,
and widening `spent.Row` to carry cache columns would make every consumer of
the spend series parse fields it never reads.

**A turn is the unit, not a call.** A tool loop is one turn and up to eighty
model calls, and the second call of a turn reads almost everything the first
one wrote. Ranking calls would put a turn's own tool traffic at the top of a
"worst" list forever, which says nothing anyone can act on. Ranking turns says
which conversation stopped reusing its prefix.

**Absent is not zero, and the distinction is the whole instrument.** The ledger
writes `null` for a class the provider did not report and a number for one it
reported as zero (`ledger.TokenUsage`). A hit rate averaged over calls where
nothing was reported is a made-up number that looks measured, so reported and
unreported calls are counted separately and the second count travels with the
first. A window where most calls are unreported is a broken adapter, not a cold
cache, and the panel has to be able to say which.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable


@dataclass(frozen=True)
class CallRow:
    """One model call, as far as the cache is concerned."""

    ts: datetime
    local_date: date
    role: str
    model: str
    turn_id: str
    input_tokens: int
    #: `None` means the provider reported nothing, which is not the same fact
    #: as a reported zero and must never be averaged as one.
    cached_tokens: int | None
    written_tokens: int | None
    cost_usd: float

    @property
    def reported(self) -> bool:
        return self.cached_tokens is not None

    @property
    def uncached_tokens(self) -> int:
        return max(0, self.input_tokens - (self.cached_tokens or 0))


@dataclass(frozen=True)
class TurnCache:
    """One turn's model calls, summed."""

    turn_id: str
    started_at: datetime
    local_date: date
    role: str
    model: str
    calls: int
    reported_calls: int
    input_tokens: int
    cached_tokens: int
    uncached_tokens: int
    written_tokens: int
    cost_usd: float

    @property
    def hit_rate(self) -> float | None:
        """Cached share of input, or `None` when no call in the turn reported.

        Computed over the turn's whole input rather than over reported calls
        only: a turn that reported on three of four calls has a real
        denominator and hiding the fourth would flatter it.
        """
        if not self.reported_calls or self.input_tokens <= 0:
            return None
        return self.cached_tokens / self.input_tokens


def parse(lines: Iterable[str]) -> list[CallRow]:
    """Rows out of ledger lines. A line that is not one is skipped.

    Built inside the guard for `spent.parse`'s reason: a `cost_usd` or
    `input_tokens` that is not a number must cost its own row and not take
    down every reader of the ledger with it.
    """
    rows: list[CallRow] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            raw: dict[str, Any] = json.loads(line)
            rows.append(
                CallRow(
                    ts=datetime.fromisoformat(str(raw["ts"]).replace("Z", "+00:00")),
                    local_date=date.fromisoformat(str(raw["local_date"])),
                    role=str(raw.get("role") or ""),
                    model=str(raw.get("model") or ""),
                    turn_id=str(raw.get("turn_id") or ""),
                    input_tokens=int(raw.get("input_tokens") or 0),
                    cached_tokens=_opt_int(raw.get("cached_tokens")),
                    written_tokens=_opt_int(raw.get("cache_creation_tokens")),
                    cost_usd=float(raw.get("cost_usd") or 0.0),
                )
            )
        except (ValueError, TypeError, KeyError):
            continue
    return rows


def _opt_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def by_turn(rows: Iterable[CallRow]) -> list[TurnCache]:
    """Every turn, newest first.

    Calls with no `turn_id` are a background role writing outside any turn.
    They are kept, grouped under one id per role and model so a scheduled job
    that never reuses a prefix is visible, rather than being folded into the
    conversation's rows or dropped for having no turn.
    """
    grouped: dict[str, list[CallRow]] = defaultdict(list)
    for row in rows:
        key = row.turn_id or f"(no turn) {row.role}/{row.model}"
        grouped[key].append(row)

    turns: list[TurnCache] = []
    for key, calls in grouped.items():
        calls.sort(key=lambda r: r.ts)
        first = calls[0]
        turns.append(
            TurnCache(
                turn_id=key,
                started_at=first.ts,
                local_date=first.local_date,
                role=first.role,
                model=first.model,
                calls=len(calls),
                reported_calls=sum(1 for c in calls if c.reported),
                input_tokens=sum(c.input_tokens for c in calls),
                cached_tokens=sum(c.cached_tokens or 0 for c in calls),
                uncached_tokens=sum(c.uncached_tokens for c in calls),
                written_tokens=sum(c.written_tokens or 0 for c in calls),
                cost_usd=sum(c.cost_usd for c in calls),
            )
        )
    turns.sort(key=lambda t: t.started_at, reverse=True)
    return turns


def within(rows: Iterable[CallRow], days: int, *, today: date) -> list[CallRow]:
    """The window, by the operator's calendar days, inclusive of today."""
    floor = today - timedelta(days=days - 1)
    return [r for r in rows if r.local_date >= floor]


def summarise(turns: list[TurnCache]) -> dict[str, Any]:
    """The window as one reading.

    `hit_rate` is `None` rather than 0.0 when nothing reported, so a panel
    cannot draw a full miss on an adapter that simply said nothing.
    """
    calls = sum(t.calls for t in turns)
    reported = sum(t.reported_calls for t in turns)
    input_tokens = sum(t.input_tokens for t in turns)
    cached = sum(t.cached_tokens for t in turns)
    return {
        "turns": len(turns),
        "calls": calls,
        "reported_calls": reported,
        "unreported_calls": calls - reported,
        "input_tokens": input_tokens,
        "cached_tokens": cached,
        "uncached_tokens": sum(t.uncached_tokens for t in turns),
        "written_tokens": sum(t.written_tokens for t in turns),
        "cost_usd": round(sum(t.cost_usd for t in turns), 6),
        "hit_rate": (cached / input_tokens) if reported and input_tokens else None,
    }


def as_row(turn: TurnCache) -> dict[str, Any]:
    return {
        "turn_id": turn.turn_id,
        "started_at": turn.started_at.isoformat(),
        "local_date": turn.local_date.isoformat(),
        "role": turn.role,
        "model": turn.model,
        "calls": turn.calls,
        "reported_calls": turn.reported_calls,
        "input_tokens": turn.input_tokens,
        "cached_tokens": turn.cached_tokens,
        "uncached_tokens": turn.uncached_tokens,
        "written_tokens": turn.written_tokens,
        "cost_usd": round(turn.cost_usd, 6),
        "hit_rate": turn.hit_rate,
    }


__all__ = [
    "CallRow",
    "TurnCache",
    "as_row",
    "by_turn",
    "parse",
    "summarise",
    "within",
]
