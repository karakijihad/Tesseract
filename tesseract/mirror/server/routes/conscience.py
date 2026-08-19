"""GET /api/conscience/drift — latest drift report + a history window.
GET /api/conscience/tool-usage — which tools get used, over a window.

Scrapes `home/logs/conscience/drift-YYYY-MM-DD.jsonl`, one file per day, and
returns the most recent report line plus one report per day across the window
the caller asked for. Responds 200 with `{"report": null, "history": []}` when
no reports have been written yet — the frontend renders a heartbeat-disabled
empty state rather than an error.

The window defaults to the last 30 days and narrows with `?from=&to=`
(`YYYY-MM-DD`, inclusive both ends). `available` reports every date actually on
disk, so the picker offers the days that exist rather than a calendar of empty
ones — what is stored locally is the whole of what can be shown, and a date
range that silently returns nothing is indistinguishable from a broken panel.

`tool-usage` reads `logs/usage/tools.jsonl` and joins the LIVE registry, so a
tool nobody has called is a row of zero rather than a missing row. The zeroes
are the half worth reading: they are the demotion candidates, and a panel that
only lists what was used cannot show one. Ranked by distinct sessions, never by
raw calls — one loop calling a tool four hundred times is one session's worth
of evidence.

It is a READOUT and nothing more. `_CORE_TOOL_NAMES` is a frozenset in
`brain/boot.py`, inside the tree that becomes the sealed `app/` on an install,
so an installed operator can read this and change nothing about it. IS-11 moves
the set into config; until then this is honest about being ours. There is
deliberately no control beside it — a knob that only turns in dev is worse than
no knob.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path

from aiohttp import web

from tesseract.brain import tool_usage as tool_usage_mod
from tesseract.paths import log_dir


def _drift_dir() -> Path:
    """Conscience drift follows the operator, so it lives under `home/logs`.
    Call-time: an import-time constant freezes the path."""
    return log_dir("conscience")

# The window when the caller names neither end. Also the span the picker
# opens on, so the two cannot disagree.
DEFAULT_WINDOW_DAYS = 30

_DRIFT_NAME = re.compile(r"^drift-(\d{4}-\d{2}-\d{2})\.jsonl$")


def _parse_day(raw: str | None) -> date | None:
    """`YYYY-MM-DD` or nothing. A malformed value is treated as absent rather
    than as an error: the bound is a filter, and refusing the whole request
    over one unparseable end would blank a panel that has data to show."""
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _dated_files(drift_dir: Path) -> list[tuple[date, Path]]:
    """Every drift file whose name carries a date, oldest first. A file that
    does not match the pattern is skipped rather than sorted in blind — the
    date is read from the NAME because that is what the picker filters on."""
    found: list[tuple[date, Path]] = []
    if not drift_dir.exists():
        return found
    for path in drift_dir.glob("drift-*.jsonl"):
        match = _DRIFT_NAME.match(path.name)
        if not match:
            continue
        try:
            found.append((date.fromisoformat(match.group(1)), path))
        except ValueError:
            continue
    found.sort(key=lambda pair: pair[0])
    return found


async def drift(request: web.Request) -> web.Response:
    dated = _dated_files(_drift_dir())
    available = [day.isoformat() for day, _ in dated]
    if not dated:
        return web.json_response({"report": None, "history": [], "available": []})

    # The latest report is the newest file's last line, whatever window was
    # asked for. It is the CURRENT state of drift, not a member of the range —
    # narrowing the history to last week must not also claim last week's
    # summary is what the signals read right now.
    latest = _load_last_report(dated[-1][1])

    to_day = _parse_day(request.query.get("to")) or dated[-1][0]
    from_day = _parse_day(request.query.get("from")) or (
        to_day - timedelta(days=DEFAULT_WINDOW_DAYS - 1)
    )
    if from_day > to_day:
        from_day, to_day = to_day, from_day

    history = [
        report
        for day, path in dated
        if from_day <= day <= to_day
        for report in (_load_last_report(path),)
        if report
    ]
    return web.json_response({
        "report": latest,
        "history": history,
        "available": available,
        "from": from_day.isoformat(),
        "to": to_day.isoformat(),
    })


def _load_last_report(path: Path) -> dict | None:
    last: dict | None = None
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                last = json.loads(raw)
            except json.JSONDecodeError:
                continue
    return last


# The windows the panel offers. Two, because the question the operator asked
# was "the past one week, two weeks" and a third would be a picker nobody
# needs; `?days=` still accepts any of them so a later surface is not blocked.
USAGE_WINDOWS: tuple[int, ...] = (7, 14)


async def tool_usage(request: web.Request) -> web.Response:
    """Windowed tool usage, with every registered tool present.

    Returns one series per window rather than making the client ask twice: the
    two are read from the same file in one pass and the panel shows them side
    by side, so two round trips would only add a way for them to disagree.

    Without a registry the ledger's own keys are all there is, and the response
    says so — `roster: false` — because a panel that cannot tell "nobody has
    called this" from "this does not exist" is showing the operator a different
    fact than the one it claims.
    """
    registry = request.app.get("tool_registry")
    roster = sorted(registry.tools) if registry is not None else []

    requested = request.query.get("days")
    windows: tuple[int, ...] = USAGE_WINDOWS
    if requested:
        try:
            asked = int(requested)
        except ValueError:
            asked = 0
        # A bad or absurd value falls back to the default pair rather than
        # erroring: the parameter narrows a readout, and refusing the whole
        # request over it would blank a panel that has data to show.
        if 0 < asked <= 365:
            windows = (asked,)

    return web.json_response({
        "windows": [
            {"days": days, "tools": tool_usage_mod.rollup(days, roster)}
            for days in windows
        ],
        "roster": registry is not None,
        "total_tools": len(roster),
    })
