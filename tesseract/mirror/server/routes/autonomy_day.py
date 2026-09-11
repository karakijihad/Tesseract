"""What the runtime decided and did on its own today, as one reading.

AR-29 item 7: the phone sees the day. Not a second digest written for a
channel, and not a per-surface reader. This is a ROOM, so `autonomy_read`
relays it to Telegram in the same sentences the cockpit draws (ruling 23), and
the operator away from the desk is told exactly what the operator at it is
told. A channel-shaped summary would be the second answer this plan keeps
refusing to build.

**Two rows make one day.** `morning` decides at nine and `workday` carries one
step forward each hour after it, in one conversation. So the question "what did
you do today" is not answerable from either row's run log alone: the morning's
row says what it proposed and the wakes say what each worked, and neither says
what came of the day. The join is here.

**Everything is read, nothing is inferred.** The runs come from the scheduler's
own log, the steps from the agenda records, the money from the same ledger
reader the rows themselves ask. Where a producer will not answer, the row says
so rather than reporting a zero: a day nobody could read and a day where
nothing happened are the two states an operator would act on differently, and
this panel has been wrong about that distinction before.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from aiohttp import web

log = logging.getLogger(__name__)

#: The two rows that make a day. Named here rather than discovered, because a
#: row that stops existing should make this reader say so rather than quietly
#: report a shorter day.
_ROWS = ("morning", "workday")


def _local_day(when: datetime) -> date:
    """The operator's calendar day for an instant, treating a naive one as UTC.

    `morning._local_day`'s rule and its reason: nothing in the agenda load path
    refuses a naive `created_at`, and reading one as local time buckets it into
    the wrong day.
    """
    from datetime import timezone

    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone().date()


def runs_today(day: date | None = None) -> list[dict[str, Any]]:
    """Every `morning` and `workday` run logged today, oldest first.

    Reads the scheduler's own run log rather than counting turns, because a row
    that REFUSED is part of the day and left no turn at all. A day whose
    morning refused for want of a readable ceiling is the most important day
    this room has to be able to describe.
    """
    from tesseract.lib.clock import today as _today
    from tesseract.scheduler.log import iter_runs, outcome_of_row, runs_path

    when = day or _today()
    out: list[dict[str, Any]] = []
    try:
        path = runs_path()
        if not path.is_file():
            return []
        for row in iter_runs(path):
            # `subject` is what the run log calls a row's name, and `ts` its
            # time. Guessing `job`/`at` here matched nothing and reported an
            # empty day rather than failing, which is the shape this module's
            # own docstring warns about.
            if row.get("subject") not in _ROWS:
                continue
            stamp = row.get("ts") or ""
            try:
                fired = datetime.fromisoformat(str(stamp))
            except ValueError:
                continue
            if _local_day(fired) != when:
                continue
            out.append(
                {
                    "row": row.get("subject"),
                    "at": fired,
                    "outcome": outcome_of_row(row).value,
                    # `detail` is what the row said about itself, which for
                    # these two is a written sentence rather than a code.
                    "said": str(row.get("detail") or row.get("summary") or "").strip(),
                }
            )
    except Exception:  # noqa: BLE001 - a log that will not read is not an empty day
        log.warning("the day: the run log could not be read", exc_info=True)
        return []
    return sorted(out, key=lambda r: r["at"])


def steps_today(day: date | None = None) -> dict[str, list[Any]]:
    """The day's steps, split by what became of them.

    `proposed` is every task made today, `closed` those that reached a terminal
    status today, and `working` those taken up and not finished. A step
    proposed on Monday and closed today counts in `closed` and not in
    `proposed`, because the question this room answers is what happened TODAY,
    not what was created.
    """
    from tesseract.lib.clock import today as _today
    from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
    from tesseract.orchestrator.autonomy.models import AgendaSource, AgendaStatus

    when = day or _today()
    proposed: list[Any] = []
    working: list[Any] = []
    try:
        for item in AgendaStore().iter_active():
            if item.source is not AgendaSource.TASK:
                continue
            if _local_day(item.created_at) == when:
                proposed.append(item)
            if item.status is AgendaStatus.RUNNING:
                working.append(item)
    except Exception:  # noqa: BLE001 - see the module docstring
        log.warning("the day: the agenda could not be read", exc_info=True)
        return {"proposed": [], "working": [], "closed": [], "read": False}
    return {
        "proposed": proposed,
        "working": working,
        "closed": _closed_today(when),
        "read": True,
    }


def _closed_today(when: date) -> list[dict[str, Any]]:
    """What finished today, from the history the reaper writes.

    `agenda_history.closed_since` is the one reader of that file and is asked
    rather than re-parsed, for AR-24's reason one subsystem along: a second
    parser is a second place a format change has to reach.
    """
    from datetime import datetime as _dt, time

    from tesseract.orchestrator.autonomy.agenda_history import closed_since

    try:
        midnight = _dt.combine(when, time.min).astimezone()
        return [
            row
            for row in closed_since(midnight, statuses={"done", "failed"})
            if str(row.get("project_id") or "").strip()
        ]
    except Exception:  # noqa: BLE001
        log.warning("the day: what closed could not be read", exc_info=True)
        return []


def money_today() -> dict[str, Any]:
    """What each project the day may work has spent and has left.

    `None` for `spent` is a ledger nobody could read, which the rows themselves
    already refuse on. It is carried here rather than flattened to zero so the
    room can say "could not be read" in the one place an operator would look
    to find out why nothing ran.
    """
    from tesseract.orchestrator.autonomy.morning import (
        room_left,
        spent_today_by_project,
        workable_projects,
    )
    from tesseract.orchestrator.projects.store import ProjectStore

    try:
        projects = workable_projects(ProjectStore().list_projects())
    except Exception:  # noqa: BLE001
        log.warning("the day: the project registry could not be read", exc_info=True)
        return {"spent": None, "projects": [], "read": False}
    spent = spent_today_by_project()
    return {
        "spent": spent,
        "read": spent is not None,
        "projects": [
            {
                "id": project.id,
                "name": project.name,
                "spent_usd": (spent or {}).get(project.id, 0.0),
                "budget_usd": project.budget_usd,
                "left_usd": room_left(project, spent),
            }
            for project in projects
        ],
    }


async def get_day(request: "web.Request") -> "web.Response":
    """`GET /api/autonomy/day` — the room's contents.

    Threaded, all three, because each parses a file that only grows: the run
    log, the agenda directory and the cost ledger. The panel must never hold
    the loop that health, the socket and every inbound turn ride.
    """
    import asyncio

    from tesseract.mirror.server.routes.autonomy_rooms import day_rows
    from tesseract.mirror.server.routes._bands import band_for

    runs, steps, money = await asyncio.gather(
        asyncio.to_thread(runs_today),
        asyncio.to_thread(steps_today),
        asyncio.to_thread(money_today),
        return_exceptions=True,
    )
    band = band_for("day")
    return web.json_response(
        day_rows(
            band(runs, []),
            band(
                steps,
                {"proposed": [], "working": [], "closed": [], "read": False},
            ),
            band(money, {"spent": None, "projects": [], "read": False}),
        )
    )


def register(app: "web.Application") -> None:
    app.router.add_get("/api/autonomy/day", get_day)


__all__ = ["get_day", "money_today", "register", "runs_today", "steps_today"]
