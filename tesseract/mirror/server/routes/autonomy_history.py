"""Whether the runtime is getting worse, which is usually the question.

``GET /api/autonomy/history`` answers the one thing every other feed on this
panel cannot. A diagnostic room that shows only the current value can say the
night took seventy minutes; it cannot say that it took forty for a fortnight
and then did that twice. The second is what an operator acts on.

**Six series in two groups, and each one is read from a record the machine
already keeps.** Nothing here is a new thing written to disk, and nothing
parses prose for a number a producer could have declared. `SERIES` is the one
declaration and the one aggregator: Health's three and Cost's three come out
of the same pass, in the same envelope, so a second chart never grows a second
reader. Health's three:

* **Errors a day** counts the ERROR and CRITICAL lines the backend logged, from
  the same files and by the same test `autonomy_health.runtime_tail` uses for
  the last day of them. That reader shows four lines; this one counts every
  one, bucketed by the day the line says it happened.
* **How long the night took** is the `consolidate` row's own duration out of
  the scheduler's run log, which records one row per run with the milliseconds
  it took.
* **Crashes** counts the supervisor deciding one. `daemon.py` already tells a
  crash from an operator quitting and writes which it was; this counts the
  first and never the second, so a clean shutdown is not a crash on a chart.

Cost's three are **what it spent**, **model calls** and **tokens sent**, each a
day, read off the cost ledger's own rows through `brain/cost/spent.py`, and
under them **what each task cost**, which the same rows answer because every
row written under a turn names the turn and the task it was working.

**Every series says how far back it can actually see.** The records have
different windows: two are aged by `retention.yaml`, the supervisor's own log
is bounded by size instead, so a busy fortnight can roll the start of that one
off, and the ledger is read from its end. A chart whose left edge is "no errors" when it means "no log" is
worse than no chart, so `reaches` is how many of the fourteen days the record
covers and the room says so when it is short.

**A series whose record is missing renders `not_instrumented`.** The phase's
rule, applied to a plot: a department with no camera says so and never draws a
flat line at zero.

The walk is off the loop and held, like the retention room's: a few megabytes
of log for the errors series, and a fortnight of them does not change between
two polls of a room.

Anonymous-readable, like the other dashboard feeds.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from aiohttp import web

from tesseract import paths
from tesseract.brain.cost import spent
from tesseract.mirror.server.routes._bands import band_for
from tesseract.mirror.server.routes._isotime import iso as _iso
from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.liveness import OperationalState, label_of

log = logging.getLogger(__name__)

_band = band_for("history")

#: Two weeks, which is the span ruling 13 settled on: long enough that a
#: weekly rhythm is visible and short enough that fourteen bars are readable
#: without a scale.
DAYS = 14

#: How long one reading stands. The errors series opens a few megabytes of log,
#: and a fortnight of history does not move between two polls of a room.
HELD_FOR = 120.0

#: How much of one log file to read, from the END of it. The backend writes one
#: file per launch and they are small; the ceiling is here so a boot loop that
#: wrote a gigabyte costs this reader a bounded amount rather than the room.
#:
#: **From the end, and that is the whole point.** A chart of the last fortnight
#: read from the START of an oversized file would show the oldest lines in it
#: and drop everything since, which is the opposite of what it is for.
MAX_LOG_BYTES = 8 * 1024 * 1024


def _tail(path: Path, limit: int | None = None) -> list[str]:
    """The last `limit` bytes of a file, as whole lines.

    The first line after a seek is almost always cut in half, so it is dropped:
    a half line has no stamp this can read and would be counted on no day.

    The ceiling is resolved HERE rather than as a default argument, because a
    default is bound once when this module is imported and the constant then
    stops being the thing that decides. It read as tunable and was not.
    """
    ceiling = MAX_LOG_BYTES if limit is None else limit
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > ceiling:
                handle.seek(size - ceiling)
                handle.readline()
            raw = handle.read()
    except OSError:
        return []
    return raw.decode("utf-8", errors="replace").splitlines()


#: The supervisor's own word for a backend that died rather than being stopped.
#: It computes this already, in `supervisor/daemon.py`, and writes it on the
#: line below; nothing here decides what a crash is.
CRASH_LINE = re.compile(r"supervisor: backend exited \(.*?decision=crash\b")


def _days_ending(today: date) -> list[date]:
    """The fourteen days the chart covers, oldest first."""
    return [today - timedelta(days=n) for n in range(DAYS - 1, -1, -1)]


def _line_stamp(line: str) -> datetime | None:
    """The clock at the head of a `logging` line, in the host's own offset.

    `autonomy_health` reads these the same way and says why at length: the
    comma is `logging`'s millisecond separator, and the stamp is local because
    `logsetup.py` sets no UTC converter.
    """
    from tesseract.mirror.server.routes.autonomy_health import _line_stamp as stamp_of

    return stamp_of(line)


def _bucket(days: list[date], stamps: Iterable[datetime | None]) -> list[int]:
    """Count stamps into `days`. Anything outside the window is dropped."""
    index = {day: n for n, day in enumerate(days)}
    counts = [0] * len(days)
    for stamp in stamps:
        if stamp is None:
            continue
        at = index.get(stamp.astimezone().date())
        if at is not None:
            counts[at] += 1
    return counts


# ---- the three records -------------------------------------------------


def _error_stamps(days: list[date]) -> tuple[list[datetime | None], datetime | None]:
    """Every logged error in the window, and the oldest line seen at all.

    The second is what says how far back this can see. The backend writes one
    file per launch and `retention.yaml` ages the set, so the answer is a fact
    about this machine rather than a constant.
    """
    directory = paths.log_dir("backend")
    if not directory.is_dir():
        return [], None
    floor = datetime.combine(days[0], datetime.min.time()).astimezone()
    found: list[datetime | None] = []
    oldest: datetime | None = None
    for path in sorted(directory.glob("*.log")):
        try:
            # Its last write is before the window, so every line in it is too.
            if datetime.fromtimestamp(path.stat().st_mtime).astimezone() < floor:
                continue
        except OSError:
            continue
        for line in _tail(path):
            stamp = _line_stamp(line)
            if stamp is not None and (oldest is None or stamp < oldest):
                oldest = stamp
            if " ERROR " in line or " CRITICAL " in line:
                found.append(stamp)
    return found, oldest


def _night_minutes(days: list[date]) -> tuple[list[float], datetime | None]:
    """How long the nightly row took, per day, and the oldest run recorded.

    A day the row did not run at all is a zero, and the sentence under the
    chart is what says which of the two a zero is. Two runs in one day take the
    longest, because the question is how long the night takes and not what the
    average of a retry was.
    """
    from tesseract.scheduler.log import default_log_dir

    path = default_log_dir() / "runs.jsonl"
    index = {day: n for n, day in enumerate(days)}
    minutes = [0.0] * len(days)
    oldest: datetime | None = None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return minutes, None
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        fired = row.get("fired_at")
        try:
            stamp = datetime.fromisoformat(str(fired))
        except ValueError:
            continue
        if oldest is None or stamp < oldest:
            oldest = stamp
        if row.get("job_name") != NIGHTLY_ROW:
            continue
        at = index.get(stamp.astimezone().date())
        if at is None:
            continue
        took = float(row.get("duration_ms") or 0.0) / 60_000.0
        minutes[at] = max(minutes[at], round(took, 1))
    return minutes, oldest


#: The row that IS the night. Named here rather than guessed from the longest
#: run: which row does the nightly work is a fact about `schedule.yaml`, and a
#: chart that silently followed whatever ran longest would change its subject
#: the first time something else did.
NIGHTLY_ROW = "consolidate"


def _crash_stamps() -> tuple[list[datetime | None], datetime | None]:
    """When the supervisor decided the backend had crashed, and its oldest line.

    Its log is bounded by size rather than by a window, which is why the oldest
    line matters more here than anywhere else: a busy fortnight rolls the start
    of this series off, and a chart cannot tell that from a quiet one.
    """
    path = paths.runtime_logs_root() / "supervisor.log"
    found: list[datetime | None] = []
    oldest: datetime | None = None
    for line in _tail(path):
        stamp = _line_stamp(line)
        if stamp is not None and (oldest is None or stamp < oldest):
            oldest = stamp
        if CRASH_LINE.search(line):
            found.append(stamp)
    return found, oldest


# ---- what the room is handed -------------------------------------------


def _reaches(days: list[date], oldest: datetime | None) -> int:
    """How many of the fourteen days the record actually covers.

    Zero means nothing was found to read at all, which the room renders as no
    producer rather than as a fortnight of calm.
    """
    if oldest is None:
        return 0
    seen = oldest.astimezone().date()
    return sum(1 for day in days if day >= seen)


@dataclass
class Reading:
    """What one pass over the records has in hand, so six series over two
    files parse each file once, and the three cost series bucket it once."""

    days: list[date]
    ledger: list[spent.Row]
    _by_day: spent.DaySeries | None = None

    @property
    def by_day(self) -> spent.DaySeries:
        if self._by_day is None:
            self._by_day = spent.per_day(self.ledger, self.days)
        return self._by_day


@dataclass(frozen=True)
class SeriesSpec:
    """One chart, declared. `read` returns the values over the days and the
    oldest stamp its record holds, which is what says how far back it sees."""

    key: str
    group: str
    title: str
    headline: str
    unit: str
    why: str
    tone: str
    read: Callable[[Reading], tuple[list[Any], datetime | None]]


def _read_errors(reading: Reading) -> tuple[list[Any], datetime | None]:
    found, oldest = _error_stamps(reading.days)
    return _bucket(reading.days, found), oldest


def _read_night(reading: Reading) -> tuple[list[Any], datetime | None]:
    return _night_minutes(reading.days)


def _read_crashes(reading: Reading) -> tuple[list[Any], datetime | None]:
    found, oldest = _crash_stamps()
    return _bucket(reading.days, found), oldest


def _read_spend(reading: Reading) -> tuple[list[Any], datetime | None]:
    return reading.by_day.spend_usd, spent.oldest(reading.ledger)


def _read_calls(reading: Reading) -> tuple[list[Any], datetime | None]:
    return reading.by_day.calls, spent.oldest(reading.ledger)


def _read_tokens(reading: Reading) -> tuple[list[Any], datetime | None]:
    return reading.by_day.tokens_sent, spent.oldest(reading.ledger)


#: The two bands the room draws the series under, in order. A series names
#: one, and the room draws a band per group with the title written here.
GROUPS: tuple[tuple[str, str], ...] = (
    ("health", "Over the last two weeks"),
    ("cost", "What it spent, over the same two weeks"),
)

#: Every series the panel can draw, and the ONE place a new one is added. The
#: cost series read the ledger through `brain/cost/spent.py`; a second reader
#: of that file, or a second envelope for a chart, is the fork this exists to
#: stop, and `tests/autonomy_rewrite_AR_24` fails on either.
SERIES: tuple[SeriesSpec, ...] = (
    SeriesSpec(
        "errors",
        "health",
        "Errors",
        "Errors a day",
        "",
        "Every error the runtime wrote down, counted by the day it "
        "happened. What you want from it is the shape: one or two on "
        "most days is the background, and a day that stands up is "
        "usually one fault repeating rather than many.",
        "warn",
        _read_errors,
    ),
    SeriesSpec(
        "night",
        "health",
        "The night",
        "How long the night took",
        "m",
        "The nightly pass, start to last step. A day at zero is a "
        "night it did not run. What is worth looking at is a step up "
        "that stays up, which usually means something it now retries "
        "rather than something it now does.",
        "calm",
        _read_night,
    ),
    SeriesSpec(
        "crashes",
        "health",
        "Crashes",
        "Times it stopped and had to be restarted",
        "",
        "A backend that died and was restarted for you. Being stopped "
        "on purpose does not count here, so anything on this chart is "
        "something that went wrong. The record behind it is kept by "
        "size rather than by days, so a busy fortnight can push the "
        "left of it out of reach.",
        "bad",
        _read_crashes,
    ),
    SeriesSpec(
        "spend",
        "cost",
        "Spend",
        "What it spent a day",
        " USD",
        "Every paid model call, summed by the day the ledger filed it under. "
        "Calls on a subscription or on this machine cost nothing here and "
        "still count on the other two charts.",
        "calm",
        _read_spend,
    ),
    SeriesSpec(
        "calls",
        "cost",
        "Calls",
        "Model calls a day",
        "",
        "How many times a model was asked anything, whoever asked: a turn, "
        "a scheduled row, a boot probe. A step up that stays up is usually "
        "a row that now runs more often.",
        "calm",
        _read_calls,
    ),
    SeriesSpec(
        "tokens",
        "cost",
        "Sent",
        "Tokens sent a day",
        "",
        "Everything sent to a model in a day, cached or not. This is what "
        "the size of the working set and the length of a conversation "
        "change, so it moves before the spend does.",
        "calm",
        _read_tokens,
    ),
)


def _series(spec: SeriesSpec, values: list[Any], reaches: int, days: list[date]) -> dict[str, Any]:
    state = (
        OperationalState.NOT_INSTRUMENTED if reaches == 0 else OperationalState.IDLE
    )
    # Both sentences are composed HERE and not on the surface. The room draws
    # bars and a readout and writes none of the words, which is the rule every
    # other room on this panel follows; a frontend that assembles one of these
    # out of `reaches` and a length is the frontend describing the backend
    # again, which is the defect the card contract exists to stop.
    cannot = ""
    short = ""
    if reaches == 0:
        cannot = (
            f"{spec.headline} cannot be drawn, because nothing on this machine has "
            f"kept the record it would come from. {spec.why}"
        )
    elif reaches < len(values):
        short = (
            f"The record behind this reaches back {reaches} of {len(values)} "
            "days, so anything to the left of that is missing rather than zero."
        )
    return {
        "cannotSay": cannot,
        "shortSay": short,
        "key": spec.key,
        "group": spec.group,
        # What the tab says. Short, because it sits beside others.
        "title": spec.title,
        # What the chart is OF, over it.
        "headline": spec.headline,
        "unit": spec.unit,
        # What it is for and what its shape means. The room prints this and
        # writes nothing of its own.
        "why": spec.why,
        # Which of the panel's meanings the bars carry. Never a colour.
        "tone": spec.tone,
        "values": values,
        "days": [day.isoformat() for day in days],
        "reaches": reaches,
        "state": state.value,
        "label": label_of(state),
    }


# ---- what each task cost -----------------------------------------------

#: How many tasks the card lists. Most recently paid for first; the ledger
#: keeps the rest.
TASKS_SHOWN = 20


def _tier_phrase(by_tier: dict[str, int]) -> str:
    if len(by_tier) == 1:
        (tier,) = by_tier
        return f"all on {tier}"
    parts = [f"{count} on {tier}" for tier, count in by_tier.items()]
    return ", ".join(parts[:-1]) + f" and {parts[-1]}"


def _task_sentence(task: spent.TaskSpend, record_gone: bool) -> str:
    calls = "1 model call" if task.calls == 1 else f"{task.calls} model calls"
    turns = "1 turn" if task.turns == 1 else f"{task.turns} turns"
    said = (
        f"{calls}, {_tier_phrase(task.calls_by_tier)}; "
        f"{task.tokens_sent:,} tokens sent over {turns}."
    )
    if record_gone:
        said += " The task's own record is gone; its spend is still on the ledger."
    return said


def _task_lines(tasks: list[spent.TaskSpend], agenda: AgendaStore | None) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for task in tasks[:TASKS_SHOWN]:
        item = None
        if agenda is not None:
            try:
                item = agenda.get(task.task_id)
            except Exception:  # noqa: BLE001 - a bad record must not empty the card
                item = None
        lines.append(
            {
                "key": f"task:{task.task_id}",
                "state": OperationalState.IDLE.value,
                "label": item.status.value.replace("_", " ") if item else "record gone",
                "name": item.goal if item else task.task_id,
                "said": _task_sentence(task, item is None),
                "value": f"{task.cost_usd:.3f} USD",
                "when": _iso(task.last),
            }
        )
    return lines


def _measure_lines() -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for measure in spent.MEASURES:
        state = OperationalState.IDLE if measure.measured else OperationalState.NOT_INSTRUMENTED
        lines.append(
            {
                "key": f"measure:{measure.key}",
                "state": state.value,
                "label": "measured" if measure.measured else label_of(state),
                "name": measure.title,
                "said": measure.why,
            }
        )
    return lines


def _ledger_rows(ledger_path: Path | None) -> list[spent.Row]:
    """The ledger's rows with each turn's task written back onto the turn's
    earlier rows. Empty when no ledger was handed in or the file is not there,
    which the series render as no producer."""
    if ledger_path is None:
        return []
    return spent.attributed(spent.parse(_tail(ledger_path)))


def role_days(
    ledger_path: Path | None, *, since: date
) -> dict[str, dict[date, float]]:
    """What each role spent, a day at a time, from `since` onwards.

    **Here rather than in the caller, because there is one reader of this
    ledger.** The series above answer what the machine spent; this answers
    what each role spent, which is the question a ceiling is judged against.
    Both come off `_ledger_rows`, so a change to how a row is read or how far
    back the file is tailed reaches both, and `runtime_tuning` never opens the
    ledger itself.
    """
    out: dict[str, dict[date, float]] = {}
    for row in _ledger_rows(ledger_path):
        if row.local_date < since or not row.role:
            continue
        out.setdefault(row.role, {})
        out[row.role][row.local_date] = (
            out[row.role].get(row.local_date, 0.0) + row.cost_usd
        )
    return out


def series(
    now: datetime | None = None,
    ledger_path: Path | None = None,
    agenda: AgendaStore | None = None,
) -> dict[str, Any]:
    """Every series, each with what it can see, and what each task cost."""
    today = (now or datetime.now(timezone.utc)).astimezone().date()
    days = _days_ending(today)
    reading = Reading(days=days, ledger=_ledger_rows(ledger_path))

    drawn: list[dict[str, Any]] = []
    for spec in SERIES:
        values, oldest = spec.read(reading)
        drawn.append(_series(spec, values, _reaches(days, oldest), days))

    return {
        "days": DAYS,
        "groups": [{"key": key, "title": title} for key, title in GROUPS],
        "series": drawn,
        "spent": {
            "title": "What each task cost",
            "empty": (
                "No model call has been attributed to a task yet. A turn that "
                "takes one up writes the task on every call it makes from now on."
            ),
            "tasks": _task_lines(spent.per_task(reading.ledger), agenda),
            "measuresTitle": "What this card can measure",
            "measures": _measure_lines(),
        },
        "observedAt": _iso(datetime.now(timezone.utc)),
    }


#: One reading: when it was taken, which ledger it read, and what it found.
#: The ledger is one per process in the app, so the path is part of the key
#: only so that two readers of different ledgers inside one window cannot be
#: handed each other's fortnight.
_cache: tuple[float, Path | None, dict[str, Any]] | None = None
_lock = threading.Lock()


def read(
    now: float | None = None,
    clock: Callable[[], datetime] | None = None,
    ledger_path: Path | None = None,
    agenda: AgendaStore | None = None,
) -> dict[str, Any]:
    """The series, read once and held."""
    global _cache

    at = now if now is not None else time.monotonic()

    def held(entry: tuple[float, Path | None, dict[str, Any]] | None) -> bool:
        return entry is not None and entry[1] == ledger_path and at - entry[0] < HELD_FOR

    cached = _cache
    if held(cached):
        return cached[2]
    with _lock:
        cached = _cache
        if held(cached):
            return cached[2]
        payload = series(clock() if clock else None, ledger_path, agenda)
        _cache = (at, ledger_path, payload)
        return payload


async def get_history(request: web.Request) -> web.Response:
    """Two weeks of the numbers Health and Cost are asked about. Off the loop."""
    import asyncio

    ledger = request.app.get("cost_ledger")
    ledger_path = getattr(ledger, "log_path", None)
    agenda = request.app.get("agenda_store") or AgendaStore()
    (read_back,) = await asyncio.gather(
        asyncio.to_thread(read, None, None, ledger_path, agenda), return_exceptions=True
    )
    return web.json_response(
        _band(
            read_back,
            {"days": DAYS, "groups": [], "series": [], "spent": None, "observedAt": None},
        )
    )


def register(app: web.Application) -> None:
    app.router.add_get("/api/autonomy/history", get_history)


__all__ = [
    "CRASH_LINE",
    "DAYS",
    "GROUPS",
    "HELD_FOR",
    "NIGHTLY_ROW",
    "SERIES",
    "TASKS_SHOWN",
    "SeriesSpec",
    "get_history",
    "read",
    "register",
    "series",
]
