"""GET /api/conscience/drift — latest drift report + a history window.
GET /api/conscience/tool-usage — which tools get used, over a window.
GET /api/conscience/playbook-usage — which playbooks get used, and how they went.
GET /api/conscience/payload — what a turn costs before the first message.
GET /api/conscience/cache — what the prompt cache did, per turn.

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

`payload` assembles one turn's system prompt for EVERY surface and reports
each one's composition — every section, its size, its tier, and what the
budget did to it. It answers the question the drop WARNING could not: a turn
sheds sections and the operator's only trace of it was a log line. All
surfaces in one response because the reading worth having is the difference
between them; a channel carries its surface contract on top of the same
assembly and sheds further down the same list for it.

Both of these are READOUTS and nothing more, but the thing they read is now
reachable: the working set moved out of `brain/boot.py` into
`working_set.yaml::core`, which the operator owns after an install. The control
beside these numbers is still owed, so the dial is a file rather than a
screen.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, timedelta
from pathlib import Path

from aiohttp import web

from tesseract.brain import tool_usage as tool_usage_mod
from tesseract.brain.playbook_set import CARRIED_FILENAME
from tesseract.brain import prompt_payload
from tesseract.integrations._channels_config import load_channels_config
from tesseract.paths import log_dir

log = logging.getLogger(__name__)


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


async def tool_heatmap(request: web.Request) -> web.Response:
    """GET /api/conscience/tool-heatmap — the ledger as a tool-by-day grid.

    A second shape over `tool-usage`'s rows, not a second source. The window
    total cannot show that a tool was used hard for three days and then never
    again, which is the shape that says a habit changed and a demotion is safe.

    Deliberately a grid rather than the 3D surface the ask suggested: a
    tool-by-day matrix is exactly what the ledger holds, and a rotatable plot is
    a shape you have to fly around before it says anything.
    """
    registry = request.app.get("tool_registry")
    roster = sorted(registry.tools) if registry is not None else []

    try:
        days = int(request.query.get("days") or 30)
    except ValueError:
        days = 30
    if not 0 < days <= 365:
        days = 30

    return await asyncio.to_thread(
        lambda: web.json_response({
            **tool_usage_mod.by_day(days, roster),
            "roster": registry is not None,
        })
    )


async def playbook_usage(request: web.Request) -> web.Response:
    """GET /api/conscience/playbook-usage — used how often, worked how often.

    The tool half of this surface can answer only the first question: the
    ledger records that a tool was called and nothing about how the call went.
    A playbook can answer both, because the usage log carries the revision that
    was read and the session it was read in, and AR-19's turn records say how
    that turn ended. `brain/playbook_reuse.py` owns the join and this route
    calls it rather than repeating it, for the reason the cost ledger has one
    reader: a second arithmetic over the same rows is a second answer waiting
    to disagree.

    **Every playbook appears, including the ones with nothing.** A playbook
    carried on every turn and never once read is exactly what this panel is
    for, and a reader that returns only what the log holds cannot show one.

    `unjoined` and `ungraded` are reported rather than folded into either
    column. A load with no closed turn around it is neither a success nor a
    failure, and counting it as one would let a fortnight of open turns read
    as a procedure that stopped working. A load in a turn that closed its task
    on the assistant's own sentence is the same refusal one step further in:
    the turn is there, and its outcome is the model grading itself.

    The rows themselves come from `brain/playbook_record.py`, which the tool
    that answers this from a channel calls too. A panel that built its own
    rows would be the per-surface reader ruling 23 refuses.
    """
    from tesseract.brain.playbook_record import (
        DEFAULT_WINDOW_DAYS as PLAYBOOK_WINDOW_DAYS,
        TURN_SCOPE_NOTE,
        records,
    )
    from tesseract.brain.playbook_set import carried_path
    from tesseract.paths import workspace_dir

    try:
        days = int(request.query.get("days") or PLAYBOOK_WINDOW_DAYS)
    except ValueError:
        days = PLAYBOOK_WINDOW_DAYS
    if not 0 < days <= 365:
        days = PLAYBOOK_WINDOW_DAYS

    def _read() -> dict[str, object]:
        rows = records(workspace_dir() / "skills", window_days=days)
        return {
            "days": days,
            "playbooks": rows,
            "carried_count": sum(1 for r in rows if r["carried"]),
            "total": len(rows),
            "path": str(carried_path()),
            "turn_scope_note": TURN_SCOPE_NOTE,
        }

    # Off the loop: the usage log is read whole and the turn tree is walked a
    # day directory at a time.
    return await asyncio.to_thread(lambda: web.json_response(_read()))


async def working_set(request: web.Request) -> web.Response:
    """GET /api/conscience/working-set — every tool, and whether it rides.

    One response rather than "the list" plus "the roster": the decision this
    panel exists for is which of the tools you never call are being loaded
    anyway, and that is an intersection. Splitting it into two fetches would
    only add a way for them to disagree.

    Each row carries the tool's own `group` and `summary` — the same strings
    the assistant is given and the same ones the config file is annotated with,
    so the panel cannot become a third opinion about what a tool does.
    """
    from tesseract.config.working_set import FLOOR, config_path, load_core_tool_names
    from tesseract.kernel.tools.taxonomy import GROUPS, heading_for

    registry = request.app.get("tool_registry")
    if registry is None:
        return web.json_response(
            {"error": "the tool registry is not up yet"}, status=503
        )

    try:
        core = load_core_tool_names()
    except (OSError, ValueError, KeyError) as exc:
        # Naming the file is the whole of the remedy here: the operator can
        # open it, and a panel that says "failed to load" cannot be acted on.
        return web.json_response(
            {"error": f"{config_path()}: {exc}"}, status=500
        )

    # One helper, not a copy of the union rule. Written out three times, the
    # copies disagreed: a promotion the POST had just applied read back here as
    # switched off.
    from tesseract.kernel.home_tools import effective_core_names, promoted_path

    core = effective_core_names(registry)

    counts = {
        str(row["tool"]): int(row["calls"])
        for row in tool_usage_mod.rollup(30, sorted(registry.tools))
    }
    rows = [
        {
            "tool": name,
            "group": type(tool).group,
            "group_label": heading_for(type(tool).group),
            "summary": type(tool).summary,
            "core": name in core,
            "calls_30d": counts.get(name, 0),
            "locked": name in FLOOR,
            "origin": getattr(tool, "origin", "shipped"),
        }
        for name, tool in sorted(registry.tools.items())
    ]
    return web.json_response({
        "tools": rows,
        "groups": [{"slug": slug, "label": label} for slug, label in GROUPS.items()],
        "core_count": sum(1 for r in rows if r["core"]),
        "total": len(rows),
        "locked": sorted(FLOOR),
        "path": str(config_path()),
        # Custom tools are written to their own file, so a panel that names
        # only the yaml sends an operator editing one to the wrong place.
        "custom_path": str(promoted_path()),
    })


async def set_working_set(request: web.Request) -> web.Response:
    """POST /api/conscience/working-set — carry a tool every turn, or stop.

    Body: `{"tool": "<name>", "core": true|false}`.

    Writes through `generate_working_set` rather than editing the yaml in
    place, so the groupings and the descriptions beside every name are rebuilt
    from the registry on each save. A round-trip edit would preserve the
    annotation a previous release wrote next to a tool whose summary has since
    changed, which is the drift the generated file exists to prevent.

    The change is live on the next turn: `_apply_tool_tiers` re-marks the
    registry here rather than waiting for the config watcher's debounce, for
    the same reason the identity route reloads itself.
    """
    from tesseract.brain.boot import _apply_tool_tiers, core_tool_names
    from tesseract.config.working_set import FLOOR, load_core_tool_names
    from tesseract.scripts.generate_working_set import render, _registry_facts

    registry = request.app.get("tool_registry")
    if registry is None:
        return web.json_response(
            {"error": "the tool registry is not up yet"}, status=503
        )

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be an object"}, status=400)

    name = str(body.get("tool") or "").strip()
    wanted = body.get("core")
    if not name:
        return web.json_response({"error": "send a 'tool' name"}, status=400)
    if not isinstance(wanted, bool):
        return web.json_response({"error": "'core' must be true or false"}, status=400)
    if name not in registry.tools:
        return web.json_response(
            {"error": f"no tool is called {name!r}"}, status=400
        )
    if name in FLOOR and not wanted:
        # Refused rather than silently restored by the loader on the next read:
        # a switch that flips back on its own is worse than one that says no.
        return web.json_response(
            {
                "error": f"{name} is how the assistant reaches everything not on "
                "the list. Without it, it can only reach what is."
            },
            status=400,
        )

    # A custom tool is promoted in its own file. `working_set.yaml` is
    # regenerated by `generate_working_set` and it ships, so a machine-local
    # name written there is destroyed by the next generator run or handed to
    # strangers as a tool that does not exist on their machine. The caller
    # does not need to know: one endpoint, one request shape, the split is
    # here.
    is_custom = getattr(registry.tools[name], "origin", "shipped") == "custom"

    if is_custom:
        from tesseract.kernel.home_tools import promoted_names, write_promoted

        chosen_custom = set(promoted_names())
        chosen_custom.add(name) if wanted else chosen_custom.discard(name)

        def _write() -> None:
            write_promoted(chosen_custom)

    else:
        try:
            chosen = set(load_core_tool_names())
        except (OSError, ValueError, KeyError) as exc:
            return web.json_response({"error": str(exc)}, status=500)
        chosen.add(name) if wanted else chosen.discard(name)

        def _write() -> None:
            from tesseract.config.working_set import config_path

            config_path().write_text(
                render(sorted(chosen), _registry_facts()), encoding="utf-8"
            )

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        log.exception("working set: could not save %s -> core=%s", name, wanted)
        return web.json_response({"error": f"saved nothing: {exc}"}, status=500)

    # Separate, because the write already landed. `_apply_tool_tiers` raises
    # when `working_set.yaml::core` names a tool that is not registered, which
    # a stale hand-edit can cause, and it would then fire on every save
    # afterwards. Reporting "saved nothing" there was a lie about a file that
    # had just changed on disk.
    try:
        core_tool_names(refresh=True)
        _apply_tool_tiers(registry)
    except Exception as exc:
        log.exception("working set: saved %s but could not apply it", name)
        return web.json_response(
            {
                "error": (
                    f"Saved, but it does not take effect until the next "
                    f"restart: {exc}"
                )
            },
            status=500,
        )

    # Same helper the GET uses, so the count the panel is handed matches the
    # rows it will draw on the next read. Counting the raw yaml could also
    # include a conditionally-registered tool that is not in this boot's
    # registry, inflating the total past the rows that exist.
    from tesseract.kernel.home_tools import effective_core_names

    return web.json_response({
        "tool": name,
        "core": wanted,
        "core_count": len(effective_core_names(registry)),
    })


def _surfaces() -> list[str]:
    """The surfaces the payload can be measured for, cockpit first.

    Read from `channels.yaml` rather than listed here: adding a `whatsapp:`
    block is meant to work without code, and a picker that names the channels
    a developer remembered is a second roster.
    """
    try:
        known = load_channels_config().known_channels()
    except Exception:
        known = []
    return ["cockpit", *known]


async def payload(request: web.Request) -> web.Response:
    """Every surface's payload, composed, in one response.

    All of them rather than one at a time: the reading worth having is the
    DIFFERENCE — the cockpit and a channel run the same assembly and shed
    different amounts of it, and a picker makes the operator hold one number
    in their head while they fetch the other. Same reasoning as `tool-usage`'s
    two windows.

    Assembling reads the workspace and the memory store — the same work a turn
    does, and well over the 50ms that would hold the event loop — so each runs
    in a thread, and they run together. `return_exceptions=True`: one surface
    failing to compose must not blank the others, so it is dropped from the
    reading and logged rather than taking the response down.
    """
    registry = request.app.get("tool_registry")
    provider = (lambda: registry) if registry is not None else None

    names = _surfaces()
    composed = await asyncio.gather(
        *(
            asyncio.to_thread(
                prompt_payload.payload_breakdown,
                channel_name=None if name == "cockpit" else name,
                tool_registry_provider=provider,
            )
            for name in names
        ),
        return_exceptions=True,
    )

    readings = []
    for name, result in zip(names, composed):
        if isinstance(result, BaseException):
            log.exception("payload: %s did not compose", name, exc_info=result)
            continue
        readings.append(result)

    # `ceiling` is not repeated here — every reading carries its own, and one
    # more copy is one more thing that can disagree with them.
    return web.json_response({
        "readings": readings,
        "chars_per_token": prompt_payload.CHARS_PER_TOKEN,
    })


async def what_it_did(request: web.Request) -> web.Response:
    """GET /api/conscience/day — what the runtime did on a day, or a span.

    `on` and `through` are `YYYY-MM-DD`; both absent means the newest day that
    has records. That default matters: opening on today would draw an empty
    panel on a morning where nothing has happened yet, and an empty day and a
    day nobody kept records for read identically to somebody looking at it.

    The reader is shared with the tool that answers the same question on a
    channel. This route holds no arithmetic of its own for the reason
    `playbook-usage` holds none: a second computation over the same rows is a
    second answer waiting to disagree.
    """
    from tesseract.orchestrator.turns import day as day_reader

    on = _parse_day(request.query.get("on"))
    through = _parse_day(request.query.get("through"))

    def _read() -> dict[str, object]:
        anchor = on or day_reader.latest_day_with_records()
        if anchor is None:
            # Nothing has ever been recorded. Say that, rather than drawing
            # today as a quiet day, which is a different thing entirely.
            return {
                **day_reader.DayReport(days=(), turns=(), calls=(), tools=()).as_json(),
                "anchor": None,
                "available": [],
                "maxSpanDays": day_reader.MAX_SPAN_DAYS,
            }
        report = day_reader.day(anchor, through)
        from tesseract.orchestrator.turns.manifest import TurnManifestStore

        return {
            **report.as_json(),
            "anchor": anchor.isoformat(),
            # Which days the picker may reach at all, so it can disable an
            # arrow rather than offer a day it will then draw empty.
            "available": [
                d.isoformat() for d in TurnManifestStore().days_with_records()
            ],
            "maxSpanDays": day_reader.MAX_SPAN_DAYS,
        }

    # Off the loop: this walks a day directory at a time and reads every
    # record in it, which is the same shape `playbook-usage` moves for.
    return await asyncio.to_thread(lambda: web.json_response(_read()))


#: How many of the window's turns the panel lists, per section. The ranked
#: reading is "which turns stopped reusing their prefix", and that is a short
#: list by nature: the ones worth acting on sit far above the rest, and a page
#: of every turn is the shape nobody scrolls.
WORST_TURNS = 25

#: How many of the newest turns ride alongside the ranking. A well-cached turn
#: is by definition nowhere near the top of a list ranked by what it re-read,
#: so ranking alone answered "what should I fix" and could not answer "what did
#: the turn I just took do". Both are asked of this panel, and the second is
#: what a live reading after a settings change needs.
LATEST_TURNS = 12


async def cache(request: web.Request) -> web.Response:
    """GET /api/conscience/cache — what the prompt cache did, per turn.

    Answers two: what the turns just taken did, newest first, and which turns
    stopped reusing their prefix. The second is ranked by uncached input
    tokens, which is a measured number the ledger holds, rather than by
    dollars-of-waste, which needs a counterfactual price for a request nobody
    made.

    Both, because a turn that cached well is at the bottom of the ranking by
    construction, so a ranking on its own cannot say whether the change just
    made cost anything. That is the reading every phase from here needs, and
    it is the one that was missing when it was first asked for.

    The window is calendar days on the operator's clock, inclusive of today,
    the way `tool-usage` counts them.

    Off the loop: the ledger is one growing file read whole, which is the shape
    `playbook-usage` and `day` already move for.
    """
    from tesseract.brain.cost import cache_report
    from tesseract.brain.cost.ledger import configured_log_path

    try:
        days = int(request.query.get("days") or 7)
    except ValueError:
        days = 7
    if not 0 < days <= 365:
        days = 7

    def _read() -> dict[str, object]:
        path = configured_log_path()
        if path is None or not path.exists():
            # No ledger yet is not an error and not a zero: a panel drawing a
            # 0% hit rate here would be reporting a total miss on a runtime
            # that has never made a call.
            return {
                "days": days, "summary": None,
                "latest": [], "turns": [], "ledger": False,
            }
        rows = cache_report.parse(path.read_text(encoding="utf-8").splitlines())
        windowed = cache_report.within(rows, days, today=date.today())
        # `by_turn` is newest first already, so `latest` is a slice and the
        # ranking is the one that sorts.
        turns = cache_report.by_turn(windowed)
        worst = sorted(turns, key=lambda t: t.uncached_tokens, reverse=True)
        return {
            "days": days,
            "summary": cache_report.summarise(turns),
            "latest": [cache_report.as_row(t) for t in turns[:LATEST_TURNS]],
            "turns": [cache_report.as_row(t) for t in worst[:WORST_TURNS]],
            "ledger": True,
        }

    return await asyncio.to_thread(lambda: web.json_response(_read()))
