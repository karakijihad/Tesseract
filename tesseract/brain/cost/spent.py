"""What the ledger says a piece of work cost, read back off its own rows.

Pure functions over the lines of `cost-tracking.jsonl`. Nothing here opens a
file or knows where one is: the caller reads the lines, and the one aggregator
in `mirror/server/routes/autonomy_history.py` turns what comes back into the
series the panel draws. Keeping the parse here and the chart there is what
lets a second consumer read spend without a second reader of the row format.

**A row is attributed to a task through its turn.** The turn is bound before
the first model call and the task part-way through, when the model takes it
up, so the rows a turn wrote BEFORE that carry the turn and not the task. Every
row after carries both, and a turn that took up a task always writes at least
one more row (the model answers after the tool result), so `attributed` can
hand the task back to the turn's earlier rows off the ledger alone. No join to
the turn record, which is what makes the three questions on the card one read.

**A measure with no producer says so.** `MEASURES` is the closed set the card
draws; adding one means adding it here, in the aggregator and on the card in
the same pass, and one with `measured=False` renders as no producer and never
as zero.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable, Iterable


@dataclass(frozen=True)
class Row:
    """One model call as the ledger recorded it. `tokens_sent` is everything
    the call sent: fresh input, cached input and cache writes alike."""

    ts: datetime
    local_date: date
    role: str
    model: str
    tier: str
    turn_id: str
    task_id: str
    cost_usd: float
    tokens_sent: int


def parse(lines: Iterable[str]) -> list[Row]:
    """Rows out of ledger lines. A line that is not one is skipped, and a row
    written before the ledger named turns reads as nobody's turn.

    **The whole row is built inside the guard.** The dates were guarded and
    the numbers were not, so a `cost_usd` or `input_tokens` that was not a
    number raised out of a parser whose contract is that a bad line is
    skipped, and took down every reader of the ledger with it. One malformed
    line in a file that only grows must cost its own row and nothing else.
    """
    rows: list[Row] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            if not isinstance(raw, dict):
                continue
            ts = datetime.fromisoformat(str(raw["ts"]).replace("Z", "+00:00"))
            day = date.fromisoformat(str(raw["local_date"]))
            row = Row(
                ts=ts,
                local_date=day,
                role=str(raw.get("role") or ""),
                model=str(raw.get("model") or ""),
                tier=str(raw.get("tier") or ""),
                turn_id=str(raw.get("turn_id") or ""),
                task_id=str(raw.get("task_id") or ""),
                cost_usd=float(raw.get("cost_usd") or 0.0),
                # `input_tokens` ALREADY includes the cached ones — that is
                # what `ledger.py::_compute_usd` relies on when it prices
                # `max(0, input_tokens - cached_tokens)` at the uncached rate.
                # Adding them back double-counted every cached token, which at
                # a measured 90% hit rate reported nearly twice the true
                # figure. Cache creation is a separate charge and is real.
                tokens_sent=int(raw.get("input_tokens") or 0)
                + int(raw.get("cache_creation_tokens") or 0),
            )
        except (ValueError, KeyError, TypeError):
            continue
        rows.append(row)
    return rows


def attributed(rows: list[Row]) -> list[Row]:
    """The same rows, with a turn's task written onto every row of that turn.

    A row outside any turn is left exactly as it was: there is no turn to
    borrow a task from, and inventing one is the defect this phase closes.
    """
    task_of: dict[str, str] = {}
    for row in rows:
        if row.turn_id and row.task_id:
            task_of.setdefault(row.turn_id, row.task_id)
    out: list[Row] = []
    for row in rows:
        task = task_of.get(row.turn_id, "") if row.turn_id else ""
        if task and task != row.task_id:
            row = Row(**{**row.__dict__, "task_id": task})
        out.append(row)
    return out


def oldest(rows: list[Row]) -> datetime | None:
    """The earliest row seen, which is how far back the ledger reaches."""
    return min((row.ts for row in rows), default=None)


@dataclass(frozen=True)
class DaySeries:
    spend_usd: list[float]
    calls: list[int]
    tokens_sent: list[int]


def per_day(rows: list[Row], days: list[date]) -> DaySeries:
    """Three daily series over `days`, bucketed by the day the ledger itself
    filed the row under, which is the operator's calendar day and the same one
    the ledger's daily total rolls on."""
    index = {day: n for n, day in enumerate(days)}
    spend = [0.0] * len(days)
    calls = [0] * len(days)
    tokens = [0] * len(days)
    for row in rows:
        at = index.get(row.local_date)
        if at is None:
            continue
        spend[at] += row.cost_usd
        calls[at] += 1
        tokens[at] += row.tokens_sent
    return DaySeries(
        spend_usd=[round(value, 3) for value in spend],
        calls=calls,
        tokens_sent=tokens,
    )


@dataclass(frozen=True)
class TaskSpend:
    task_id: str
    cost_usd: float
    calls: int
    calls_by_tier: dict[str, int] = field(default_factory=dict)
    tokens_sent: int = 0
    turns: int = 0
    first: datetime | None = None
    last: datetime | None = None


def per_task(rows: list[Row]) -> list[TaskSpend]:
    """What each task cost, most recently paid for first. Only rows that name
    a task after `attributed` count; a turn that worked no task is not one."""
    cost: dict[str, float] = defaultdict(float)
    calls: dict[str, int] = defaultdict(int)
    by_tier: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    tokens: dict[str, int] = defaultdict(int)
    turns: dict[str, set[str]] = defaultdict(set)
    first: dict[str, datetime] = {}
    last: dict[str, datetime] = {}
    for row in rows:
        if not row.task_id:
            continue
        key = row.task_id
        cost[key] += row.cost_usd
        calls[key] += 1
        by_tier[key][row.tier or "unknown"] += 1
        tokens[key] += row.tokens_sent
        turns[key].add(row.turn_id)
        first[key] = min(first.get(key, row.ts), row.ts)
        last[key] = max(last.get(key, row.ts), row.ts)
    out = [
        TaskSpend(
            task_id=key,
            cost_usd=round(cost[key], 6),
            calls=calls[key],
            calls_by_tier=dict(sorted(by_tier[key].items())),
            tokens_sent=tokens[key],
            turns=len(turns[key]),
            first=first[key],
            last=last[key],
        )
        for key in cost
    ]
    out.sort(key=lambda task: task.last or datetime.min, reverse=True)
    return out


def per_project(rows: list[Row], project_of: Callable[[str], str]) -> dict[str, float]:
    """What each project has spent, over whatever window `rows` covers.

    The ledger has no project on it and should not grow one: a row already
    carries the task it was paid for, and a task already carries its project
    (`AgendaItem.project_id`, AR-27's). Recording it a third time would be a
    third place for the same fact to be wrong in.

    `project_of` resolves a task id to a project id and answers `""` for a task
    that names none, or one whose record is gone because the reaper took it.
    Both mean the same thing here and neither is an error: money spent on work
    that belongs to no project is money no project's budget bounds. The
    morning's own proposing turn is exactly that, and counting it against a
    project would charge one of them for a decision about all of them.

    It is called once per distinct task rather than once per row, because a
    task with forty calls should not cost forty store reads.

    **It is not guarded.** A resolver that raises takes this down with it,
    deliberately: this function is pure and cannot tell a store that is broken
    from a store that is empty, and answering "nothing was spent" for a
    registry it could not read would hand a caller a budget that looks
    untouched. Failing closed is the caller's to do, where the store is.
    """
    by_task: dict[str, float] = defaultdict(float)
    for row in rows:
        if row.task_id:
            by_task[row.task_id] += row.cost_usd
    out: dict[str, float] = defaultdict(float)
    for task_id, cost in by_task.items():
        project = project_of(task_id)
        if project:
            out[project] += cost
    return {project: round(cost, 6) for project, cost in out.items()}


@dataclass(frozen=True)
class Measure:
    key: str
    title: str
    why: str
    measured: bool


#: The closed set of measures the Cost card may claim. One with no producer is
#: listed so the card can say so; it is never computed from a rate card.
MEASURES: tuple[Measure, ...] = (
    Measure(
        "cost_per_task",
        "What a task cost",
        "Every model call made while a task was being worked, summed off the ledger.",
        measured=True,
    ),
    Measure(
        "calls_per_task_by_tier",
        "How many calls a task took, and on which tier",
        "The calls behind that cost, split by whether the model was paid per "
        "token, covered by a subscription, or run on this machine.",
        measured=True,
    ),
    Measure(
        "tokens_per_task",
        "How much a task sent",
        "Every token sent to a model while the task was being worked, cached "
        "or not, which is what the size of the working set changes.",
        measured=True,
    ),
    Measure(
        "correction_rate",
        "How often a task's work had to be corrected",
        "Nothing on this machine records a correction yet, so this cannot be "
        "measured. It will be a number once something does, and it is not a "
        "zero until then.",
        measured=False,
    ),
)


__all__ = [
    "MEASURES",
    "DaySeries",
    "Measure",
    "Row",
    "TaskSpend",
    "attributed",
    "oldest",
    "parse",
    "per_day",
    "per_task",
]
