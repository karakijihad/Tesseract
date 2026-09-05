"""What each room on the Autonomy panel says about itself, in one sentence.

``GET /api/autonomy/rooms`` is the rail. A rail of eight room names is a menu;
a rail of eight sentences is the overview, and it is what lets the operator see
that Health needs them without opening Health.

**The counting happens here and the phrasing happens in a model.** This module
assembles the numbers from the producers that already hold them, and
``orchestrator/autonomy/panel_lines.py`` turns each room's list into one line
under the rule the watchman already follows: a figure that is not in the input
is dropped and the counts are published instead. The frontend authors nothing,
which is the whole point: a room described in TSX is a room whose description
goes stale on its own.

**A room that costs nothing to read costs nothing to write.** A room with no
facts calls no model and renders no sentence, and a room whose numbers have not
moved is answered from the cache. The ceiling is a `roles.yaml` block named
for the entry, which is what the ledger rebuilds its caps from.

Anonymous-readable, like the other dashboard feeds.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from aiohttp import web

from tesseract.mirror.server.routes._bands import band_for
from tesseract.mirror.server.routes._isotime import parse as _parse_iso

from tesseract.mirror.server.cors import origin_is_allowed, resolve_allowed_origins
from tesseract.mirror.server.routes import autonomy_atlas as atlas_route
from tesseract.mirror.server.routes import autonomy_channels as channels_route
from tesseract.mirror.server.routes import autonomy_health as health_route
from tesseract.mirror.server.routes import autonomy_managed as managed_route
from tesseract.mirror.server.routes import autonomy_memory as memory_route
from tesseract.mirror.server.routes import autonomy_overview as overview_route
from tesseract.mirror.server.routes import autonomy_retention as retention_route
from tesseract.orchestrator.autonomy import journal as operator_journal
from tesseract.orchestrator.autonomy import prune_ledger
from tesseract.orchestrator.autonomy.outbound import (
    CATEGORIES,
    EXEMPT_CATEGORIES,
    read_runtime_mutes,
)
# The module AND the names: the names are what this file calls, and the module
# is what a test patches through, so `write_lines` can be watched where it is
# looked up rather than where it is defined.
from tesseract.orchestrator.autonomy import panel_lines
from tesseract.orchestrator.autonomy.panel_lines import (
    CHAIN,
    ENTRY,
    Room,
    lines_for,
    write_behind,
)
from tesseract.orchestrator.liveness import OperationalState
from tesseract.orchestrator.workers.record import list_active_records
from tesseract.scheduler.role_chain import build_chain_for_chain

log = logging.getLogger(__name__)

# One unwrap for this room's producers, named so a warning says which surface
# went thin. Four rooms had four copies of it.
_band = band_for("rooms")

# Statuses that mean an item is waiting on something outside it.
HELD = frozenset({"blocked", "awaiting_operator"})

# A run that came back in one of these did not do what it said it would.
NOT_WELL = frozenset(
    {
        OperationalState.FAILED.value,
        OperationalState.DEGRADED.value,
        OperationalState.REFUSED.value,
        OperationalState.UNKNOWN.value,
    }
)

# How far back "recently" reaches for the record's own counts.
RECENT_HOURS = 48

# The longest a quoted department sentence may be on its way into the model's
# input. The watchman caps its own summaries at 200 characters, but `said` has
# more than one writer and a cap that lives only upstream is a cap the next
# writer does not know about. Bounded where it is used.
QUOTE_CHARS = 200

# The rooms, in the order the rail draws them. The key is what the frontend
# selects on and what the line cache is keyed under.
ROOM_KEYS = (
    "overview",
    "blocked",
    "health",
    "managed",
    "memory",
    "outcomes",
    "journal",
    "pruned",
    "retention",
    "channels",
    "atlas",
)

# What each room is FOR, which is a different question from what is in it now.
#
# The operator opened the Journal and asked what it showed. A room whose
# purpose has to be explained in a chat has not explained itself, and the
# answer cannot be the written line: that one is about tonight, it changes
# every time the numbers move, and it is written by a model over counts.
#
# One sentence, here rather than in TSX, for the same reason every other word
# on this panel is the backend's: a room described in the view is a room whose
# description goes stale on its own. It says what question the room answers,
# in the language of what a reader can see, and never in the runtime's own
# words. A room added to `ROOM_KEYS` without a line here fails the roster test
# rather than reaching a rail without one.
ROOM_PURPOSE: dict[str, str] = {
    "overview": (
        "What wants you now, what is running, and what ran while you were away."
    ),
    "blocked": (
        "Everything that stopped and is waiting on you to decide, and every "
        "source that was turned off after it went wrong."
    ),
    "health": (
        "Whether the runtime itself is well: what the last look at it found, "
        "what it could not read, and what nothing is watching at all."
    ),
    "managed": (
        "Everything the app runs on its own and everything you added to it, "
        "with what each is for, what it may spend, and when it fires next."
    ),
    "outcomes": (
        "What finished recently and how it went, with anything the last start "
        "had to put right."
    ),
    "journal": (
        "One line for every decision made without you: what was approved, what "
        "was sent out to be done, and what came back."
    ),
    "pruned": (
        "What was dropped before it reached the library, counted by why. Open "
        "it when something you expected to be kept is not there."
    ),
    "retention": (
        "What this machine deletes as it ages, how long each thing is kept "
        "first, and everything under the log trees that nothing has decided "
        "about at all."
    ),
    "channels": (
        "Whether anything the app writes can actually reach you: which kinds "
        "are sent where, what you have muted, and what it last said."
    ),
    "memory": (
        "The library it works from: how much of it there is, what the last "
        "nightly pass changed in it, and whether a question asked now can be "
        "answered."
    ),
    "atlas": (
        "The map of how everything it knows connects: when it was last drawn, "
        "what it could not make sense of, and what it does not cover at all."
    ),
}


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


#: The shared reading, under this module's own name. It was written here
#: byte for byte as well, which is the drift `_isotime` exists to close: five
#: routes each parsing a timestamp their own way is five places a change to
#: how one is read has to land.
_aware = _parse_iso


def overview_facts(
    waiting: list[dict[str, Any]],
    running: list[dict[str, Any]],
    away: list[dict[str, Any]],
    aged: int,
) -> list[str]:
    """Overview: what wants the operator, what is working, what just ran.

    Counted off the room's OWN three bands rather than off the producers
    behind them. A rail sentence assembled from a second reading of the same
    data is a sentence that can disagree with the room it describes, and the
    room is the one the operator can check.
    """
    facts: list[str] = [
        _plural(len(waiting), "thing wants you", "things want you")
        if waiting
        else "nothing needs you",
        _plural(len(running), "thing is working", "things are working")
        if running
        else "nothing is running",
    ]
    if away:
        facts.append(_plural(len(away), "thing ran recently", "things ran recently"))
        badly = [r for r in away if r["state"] in NOT_WELL]
        if badly:
            # Not `_plural`: "of them did not succeed" does not inflect, and
            # passing one string twice reads as an oversight.
            facts.append(f"{len(badly)} of them did not succeed")
    if aged:
        facts.append(
            _plural(
                aged,
                "finished thing is too old to list",
                "finished things are too old to list",
            )
        )
    return facts


def blocked_facts(items: list[Any], pauses: list[Any]) -> list[str]:
    held = [i for i in items if i.status in HELD]
    facts: list[str] = []
    if held:
        facts.append(_plural(len(held), "item is held", "items are held"))
    if pauses:
        facts.append(_plural(len(pauses), "source is paused", "sources are paused"))
    return facts


def health_facts(departments: list[dict[str, Any]], swept_at: str | None) -> list[str]:
    """What Health knows, counted. The bands are the backend's own already, so
    this is a reading of them rather than a second opinion about health."""
    wanting = [d for d in departments if d["band"] == health_route.NEEDS_ACTION]
    gaps = [d for d in departments if d["state"] == OperationalState.UNKNOWN.value]
    unwired = [
        d for d in departments if d["state"] == OperationalState.NOT_INSTRUMENTED.value
    ]
    facts: list[str] = []
    if swept_at is None:
        return ["nothing has looked at the runtime on this machine yet"]
    facts.append(
        _plural(len(wanting), "department needs you", "departments need you")
        if wanting
        else "no department needs you"
    )
    if gaps:
        facts.append(
            _plural(len(gaps), "department could not be read", "departments could not be read")
        )
    if unwired:
        facts.append(
            _plural(len(unwired), "thing has no producer at all", "things have no producer at all")
        )
    for dept in wanting[:3]:
        # What the department itself said, in the words that may leave this
        # machine. Without it the model is asked to explain a fault from its
        # count alone, which is the finding the watchman's own prompt already
        # records; with the operator's copy instead, it quoted a log line back
        # onto the one line the room opens with. `saidToModel` is the
        # watchman's own gate, read rather than decided again here, and a
        # department with nothing safe to forward contributes its name and its
        # state and no more. Bounded because `said` has several writers and a
        # cap that lives only upstream is one the next writer does not know
        # about.
        said = str(dept.get("saidToModel") or "").strip()
        # The sentence alone, because every one of them names its own subject
        # already: `the ollama breaker tripped 1 time and is still open`,
        # `2 janitor sweeps reported errors`. Prefixing the department's name
        # onto it wrote `ERROR from tesseract.brain.boot: 1 error line logged
        # by tesseract.brain.boot`, which is the same fault one layer along.
        facts.append(said[:QUOTE_CHARS] if said else f"{dept['name']} needs you")
    return facts


def memory_facts(
    trees: list[dict[str, Any]],
    last_night: list[dict[str, Any]],
    retrieval: list[dict[str, Any]],
) -> list[str]:
    """The library, counted.

    **Retrieval leads when it is impaired**, for the reason the channels room
    puts a dead bridge first: a library that cannot be searched is a library
    that is not being used, whatever is in it.
    """
    facts: list[str] = []
    impaired = [
        r for r in retrieval
        if r["state"] in {
            OperationalState.DEGRADED.value,
            OperationalState.FAILED.value,
            OperationalState.NOT_INSTRUMENTED.value,
        }
    ]
    for row in impaired[:2]:
        # The producer's own sentence, which is composed from names and
        # counts. Bounded for the same reason the health room bounds its own.
        facts.append(f"{row['name']}: {row['said'][:QUOTE_CHARS]}")
    here = [t for t in trees if t["state"] != OperationalState.NOT_INSTRUMENTED.value]
    for tree in here:
        facts.append(f"{tree['name']} holds {tree['said']}")
    missing = [t for t in trees if t not in here]
    if missing:
        facts.append(
            _plural(len(missing), "part of the library", "parts of the library")
            + " has nothing in it yet"
        )
    if last_night:
        unwell = [s for s in last_night if s["state"] in NOT_WELL]
        facts.append(
            _plural(len(last_night), "step of the last pass", "steps of the last pass")
            + " touched it"
        )
        if unwell:
            facts.append(
                _plural(len(unwell), "of them did not finish", "of them did not finish")
            )
    else:
        facts.append("no nightly pass has touched it on this machine yet")
    return facts


def thrown_away_facts(bands: dict[str, Any]) -> list[str]:
    """What ages, and what nothing has decided about.

    What nobody has decided leads. A tree with a window is working as intended
    however large it is; a tree with none is the thing this room was added to
    surface, and the only one an operator can act on tonight.
    """
    ages = bands.get("ages") or []
    undecided = bands.get("undecided") or []
    kept = bands.get("kept") or []
    facts: list[str] = []
    if undecided:
        facts.append(
            _plural(len(undecided), "tree has no window", "trees have no window")
            + " and nothing has decided about "
            + ("it" if len(undecided) == 1 else "them")
        )
        # The biggest of them, by name and size, because "which one is
        # growing" is the question the band is ordered by.
        facts.append(f"the largest is {undecided[0]['name']} at {undecided[0]['value']}")
    else:
        facts.append("everything under the log trees is aged or kept on purpose")
    failing = [r for r in ages if r["state"] in NOT_WELL]
    for row in failing:
        facts.append(f"{row['name']}: {row['said'][:QUOTE_CHARS]}")
    facts.append(_plural(len(ages), "tree is aged", "trees are aged"))
    facts.append(_plural(len(kept), "is kept on purpose", "are kept on purpose"))
    said = bands.get("lastSweepSaid") or ""
    if said:
        facts.append(said[:QUOTE_CHARS])
    return facts


def atlas_facts(
    rows: list[dict[str, Any]], last_pass: list[dict[str, Any]]
) -> list[str]:
    """The map, counted.

    What is wrong with it leads, for the reason retrieval leads in Memory: a
    map drawn by an older version of the drawing, or one holding a
    contradiction, is a map being read as if it were current. What is simply IN
    it comes after that.
    """
    wrong = [r for r in rows if r["state"] in NOT_WELL]
    # The producer's own sentence, bounded where it is used for the same
    # reason the health room bounds its own.
    facts = [f"{r['name']}: {r['said'][:QUOTE_CHARS]}" for r in wrong]
    facts += [
        f"{r['name']}: {r['value']}"
        for r in rows
        if r not in wrong and r["value"]
    ]
    if last_pass:
        unwell = [s for s in last_pass if s["state"] in NOT_WELL]
        facts.append(
            _plural(len(last_pass), "step drew or checked it", "steps drew or checked it")
        )
        if unwell:
            facts.append(_plural(len(unwell), "of them did not finish", "of them did not finish"))
    else:
        facts.append("no nightly pass has drawn it on this machine yet")
    return facts


def managed_facts(
    rows: list[dict[str, Any]],
    roster: list[dict[str, Any]],
    playbooks: list[dict[str, Any]] | None = None,
) -> list[str]:
    """What the app manages and what the operator added to it.

    Ownership leads because it is the room's axis, and what is turned off comes
    next: a row nobody meant to leave off is the fault this room catches, and
    it is invisible in a total.
    """
    playbooks = playbooks or []
    facts: list[str] = []
    if rows:
        mine = [r for r in rows if r["origin"] == managed_route.USER]
        facts.append(_plural(len(rows), "scheduled job", "scheduled jobs"))
        live = [
            r for r in rows if r["state"] == OperationalState.RUNNING.value
        ]
        if live:
            facts.append(_plural(len(live), "is running now", "are running now"))
        if mine:
            facts.append(_plural(len(mine), "of them is yours", "of them are yours"))
        off = [r for r in rows if not r["enabled"]]
        if off:
            facts.append(_plural(len(off), "is turned off", "are turned off"))
        stopped = [
            r for r in rows if r["state"] == OperationalState.REFUSED.value
        ]
        if stopped:
            facts.append(
                _plural(len(stopped), "stopped itself", "stopped themselves")
            )
        failed = [r for r in rows if r["state"] == OperationalState.FAILED.value]
        if failed:
            # Not `_plural`: "failed last time" does not inflect, and passing
            # one string twice reads as an oversight rather than as a constant.
            facts.append(f"{len(failed)} failed last time")
    if roster:
        waiting = [
            r for r in roster if r["state"] == OperationalState.PENDING.value
        ]
        facts.append(_plural(len(roster), "agent", "agents"))
        if waiting:
            facts.append(
                _plural(len(waiting), "is waiting for you", "are waiting for you")
            )
    if playbooks:
        facts.append(_plural(len(playbooks), "playbook", "playbooks"))
        blocked = [p for p in playbooks if p["cannotRun"]]
        if blocked:
            # Not `_plural`: "cannot run" does not inflect, and passing one
            # string twice reads as an oversight rather than as a constant.
            facts.append(f"{len(blocked)} cannot run")
        retired = [p for p in playbooks if p["status"] == "retired"]
        if retired:
            facts.append(f"{len(retired)} retired")
    return facts


def outcomes_facts(items: list[Any], now: datetime) -> list[str]:
    cutoff = now - timedelta(hours=RECENT_HOURS)
    recent = [i for i in items if (_aware(i.updated_at) or now) >= cutoff]
    done = [i for i in recent if i.status == "done"]
    badly = [i for i in recent if i.status in {"cancelled", "abandoned"}]
    facts: list[str] = []
    if done:
        facts.append(_plural(len(done), "thing finished", "things finished"))
    if badly:
        facts.append(_plural(len(badly), "thing did not finish", "things did not finish"))
    return facts


def outcome_rows(items: list[Any], now: datetime) -> list[dict[str, Any]]:
    """What finished recently, and how it went.

    The same window and the same statuses `outcomes_facts` counts, so the room
    can never say "2 things finished" over a list of three.
    """
    cutoff = now - timedelta(hours=RECENT_HOURS)
    out: list[dict[str, Any]] = []
    for item in items:
        at = _aware(item.updated_at) or now
        if at < cutoff or item.status not in {"done", "cancelled", "abandoned"}:
            continue
        out.append(
            {
                "id": item.id,
                "goal": item.goal,
                "status": item.status,
                "source": str(getattr(item.source, "value", item.source)),
                "at": at.isoformat(),
            }
        )
    out.sort(key=lambda row: row["at"], reverse=True)
    return out


def journal_facts(rows: list[dict[str, Any]]) -> list[str]:
    return [_plural(len(rows), "note", "notes")] if rows else []


def pruned_facts(counts: dict[str, dict[str, int]]) -> list[str]:
    total = sum(sum(stages.values()) for stages in counts.values())
    if not total:
        return []
    facts = [
        _plural(total, "draft was dropped at the door", "drafts were dropped at the door")
    ]
    if len(counts) > 1:
        facts.append(_plural(len(counts), "source", "sources"))
    return facts


def channel_facts(
    muted: dict[str, list[str]],
    doors: list[dict[str, Any]],
    last: dict[str, Any] | None,
) -> list[str]:
    """What can reach the operator, what cannot, and when it last did.

    The three that cannot be muted are counted separately, because a kind that
    is always allowed through and a kind left on are different facts about the
    same panel. **The door leads**, because a bridge that is down makes every
    other fact here beside the point: nothing is muted if nothing is sent.
    """
    facts: list[str] = []
    # On the STATE rather than by subtracting one list from another: two
    # channels reporting the same thing are equal dicts, and `d not in unwired`
    # would drop a second one that was genuinely down.
    unwired = [
        d for d in doors if d["state"] == OperationalState.NOT_INSTRUMENTED.value
    ]
    down = [
        d for d in doors
        if d["state"] not in _CHANNEL_IS_UP
        and d["state"] != OperationalState.NOT_INSTRUMENTED.value
    ]
    if unwired and not down:
        # Nothing is wired at all, which is not a channel that is down. The
        # distinction is the liveness contract's and it is the difference
        # between "fix the bridge" and "there is no bridge".
        facts.append("nothing is wired to reach you away from this screen")
    elif down:
        facts.append(
            _plural(len(down), "channel cannot reach you", "channels cannot reach you")
        )
    elif doors:
        facts.append(
            _plural(len(doors), "channel is connected", "channels are connected")
        )
    off = {c for cats in muted.values() for c in cats}
    silenced = [c for c in CATEGORIES if c in off]
    facts.append(_plural(len(CATEGORIES), "kind it can send you", "kinds it can send you"))
    if silenced:
        facts.append(_plural(len(silenced), "is muted by you", "are muted by you"))
    # Not `_plural`: the count is fixed by `EXEMPT_CATEGORIES` and passing one
    # string twice reads as an oversight rather than as a constant.
    facts.append(f"{len(EXEMPT_CATEGORIES)} cannot be muted at all")
    if last is None:
        facts.append("it has sent you nothing on this machine yet")
    else:
        # The KIND and when, never the message. The text is the operator's own
        # copy on their own screen; the room's line is written by a model.
        facts.append(f"the last thing it sent you was a {last['category']}")
    return facts


# A channel in one of these can carry a message. Anything else and the room
# leads with that, because a bridge that is down makes every other fact about
# what is muted beside the point.
_CHANNEL_IS_UP = frozenset(
    {OperationalState.RUNNING.value, OperationalState.DEGRADED.value}
)


def may_write(request: web.Request) -> bool:
    """Whether THIS request may set the writer going.

    Reading is not gated on this backend and does not need to be: it binds
    loopback and the answer is the operator's own panel. But asking for a line
    to be written costs money and puts the runtime's own status text in front
    of a provider, and any page in any tab can issue a GET to localhost. A
    browser always sends `Origin` cross-site and cannot be made to forge it, so
    the rule the CORS middleware applies to a write is the rule here: an
    allowed origin, or none at all, which is a native client no page can
    impersonate.

    The response is identical either way. A refused caller gets the cached
    lines like anyone else; it simply cannot spend anything.
    """
    allowed = request.app.get("allowed_origins")
    if allowed is None:
        # A test app, or one built before the allowlist existed. The packaged
        # webview's own origins are always in it.
        allowed = resolve_allowed_origins(())
    return origin_is_allowed(request.headers.get("Origin", ""), allowed)


def writer_chain(app: web.Application):
    """What the writer rides, billed to the entry that declares the ceiling.

    An empty chain is not an error: the room falls back to its counted facts,
    which is what it should show while no model can be reached.

    Public, and takes the app rather than the request, because the route is no
    longer the only caller: `autonomy_read` warms the same cache from the same
    ceiling. A second way to build the writer's chain would be a second budget.
    """
    try:
        return build_chain_for_chain(
            CHAIN,
            billing_key=ENTRY,
            log_label="panel lines",
            cost_ledger=app.get("cost_ledger"),
        )
    except Exception:  # noqa: BLE001
        log.exception("rooms route: the writer's chain could not be built")
        return []


@dataclass(frozen=True)
class PanelRead:
    """One reading of the whole panel: what each room counts, and what is in it.

    Two things because two callers need different halves and neither may go
    and fetch its own. The rail draws sentences from `facts`; a reader asked
    "what needs me" has to be able to name the four items rather than repeat
    that there are four. Producing them in one pass is what stops the answer
    to the second question disagreeing with the answer to the first.
    """

    facts: dict[str, list[str]]
    rows: dict[str, dict[str, Any]]


async def gather_facts(app: web.Application, now: datetime) -> dict[str, list[str]]:
    """Every room's numbers. The rail's half of :func:`read_rooms`."""
    return (await read_rooms(app, now)).facts


async def read_rooms(app: web.Application, now: datetime) -> PanelRead:
    """Every room's numbers AND its contents, from the producers that hold them.

    Takes the app, not the request, because the panel is no longer the only
    thing that asks: `autonomy_read` answers "how is autonomy doing" from this
    same join, so that a channel and the cockpit cannot be told two different
    things about one night.

    The rows are the ones the rooms' own routes publish, called rather than
    re-derived. A second serialisation of a held agenda item is a second
    opinion about what "held" means.
    """
    store = overview_route.agenda_store(app)
    (
        items,
        workers,
        journal,
        pruned,
        latest,
        pipeline,
        muted,
        rows,
        roster,
        channel_kinds,
        doors,
        last_sent,
        library,
        night,
        reach,
        drawn,
        drawing,
        thrown,
        plays,
    ) = await asyncio.gather(
        asyncio.to_thread(store.ranked),
        asyncio.to_thread(list_active_records),
        asyncio.to_thread(operator_journal.read_recent, limit=50, days=7),
        asyncio.to_thread(prune_ledger.prune_counts, window_hours=RECENT_HOURS),
        asyncio.to_thread(health_route.read_latest),
        asyncio.to_thread(overview_route.pipeline_payload, now),
        asyncio.to_thread(read_runtime_mutes),
        asyncio.to_thread(managed_route.schedules, app, now),
        asyncio.to_thread(managed_route.agents),
        asyncio.to_thread(channels_route.kinds, app, now),
        asyncio.to_thread(channels_route.door),
        asyncio.to_thread(channels_route.last_message),
        asyncio.to_thread(memory_route.trees),
        asyncio.to_thread(memory_route.last_night),
        asyncio.to_thread(memory_route.retrieval, app),
        asyncio.to_thread(atlas_route.graph),
        asyncio.to_thread(atlas_route.last_pass),
        asyncio.to_thread(retention_route.bands),
        asyncio.to_thread(managed_route.playbooks, app),
        return_exceptions=True,
    )

    items = _band(items, [])
    workers = _band(workers, [])
    journal = _band(journal, [])
    pruned = _band(pruned, {})
    latest = _band(latest, None)
    pipeline = _band(pipeline, {"current": None, "previous": None})
    muted = _band(muted, {})
    rows = _band(rows, [])
    roster = _band(roster, [])
    channel_kinds = _band(channel_kinds, [])
    doors = _band(doors, [])
    last_sent = _band(last_sent, None)
    library = _band(library, [])
    # Both of these answer with their steps AND the sentence for when there
    # are none, so the fallback has to be that pair rather than a bare list:
    # the rail wants the steps and the room wants the sentence.
    night, _night_said = _band(night, ([], ""))
    reach = _band(reach, [])
    drawn = _band(drawn, [])
    drawing, _drawing_said = _band(drawing, ([], ""))
    thrown = _band(thrown, {"ages": [], "kept": [], "undecided": [], "lastSweepSaid": ""})
    plays = _band(plays, [])

    departments = health_route.from_sweep(latest, now)
    swept = (latest or {}).get("observed_at") if latest else None

    pauses = overview_route.paused_sources(app)
    waiting, aged = overview_route.wants_you(pipeline, items, pauses, workers, now)
    running = overview_route.working_now(now)
    away = overview_route.ran_while_away(rows, now)
    held = overview_route.held_items(items)

    return PanelRead(
        facts={
            "overview": overview_facts(waiting, running, away, aged),
            "blocked": blocked_facts(items, pauses),
            "health": health_facts(departments, swept),
            "managed": managed_facts(rows, roster, plays),
            "outcomes": outcomes_facts(items, now),
            "journal": journal_facts(journal),
            "pruned": pruned_facts(pruned),
            "channels": channel_facts(muted, doors, last_sent),
            "memory": memory_facts(library, night, reach),
            "atlas": atlas_facts(drawn, drawing),
            "retention": thrown_away_facts(thrown),
        },
        rows={
            "overview": {
                "wantsYou": waiting,
                "workingNow": running,
                "ranWhileAway": away,
                "aged": aged,
            },
            "blocked": {
                "held": held,
                "paused": overview_route.paused_rows(pauses),
            },
            "health": {"departments": departments, "sweptAt": swept},
            "managed": {"schedules": rows, "agents": roster, "playbooks": plays},
            "outcomes": {"recent": outcome_rows(items, now)},
            "journal": {"notes": journal},
            "pruned": {"counts": pruned},
            "memory": {
                "trees": library,
                "lastNight": night,
                "retrieval": reach,
            },
            "channels": {
                "kinds": channel_kinds,
                "lastMessage": last_sent,
                "adapters": doors,
            },
            "atlas": {
                "graph": drawn,
                "lastPass": drawing,
            },
            "retention": thrown,
        },
    )


async def get_rooms(request: web.Request) -> web.Response:
    """One written line per room, and the facts it was written from.

    **This never waits on a model, and a cross-site caller cannot make it
    spend.** It answers from the cache and asks for the writing to happen
    behind it, for a caller whose origin it recognises, so a room whose numbers just moved shows its
    counts now and its sentence one poll later. Measured 2026-08-25: writing
    six rooms inside the request blocked the event loop for five and a half
    seconds, and a chain that was timing out held it for minutes. Health, the
    socket and every inbound turn ride that loop.
    """
    now = datetime.now(timezone.utc)
    facts = await gather_facts(request.app, now)
    rooms = [Room(key=key, facts=tuple(facts.get(key, ()))) for key in ROOM_KEYS]
    said = await lines_for(rooms, now=now)
    if may_write(request):
        write_behind(rooms, chain=writer_chain(request.app))
    return web.json_response(
        {
            "rooms": [
                {
                    "key": room.key,
                    "said": said.get(room.key, ""),
                    # What the room is for, which does not change and is not
                    # written by a model. `said` is about tonight.
                    "purpose": ROOM_PURPOSE.get(room.key, ""),
                    # What it was written from, under it. Every line on this
                    # panel has to be traceable to the numbers behind it.
                    "facts": list(room.facts),
                }
                for room in rooms
            ],
            "observedAt": now.isoformat(),
        }
    )


def register(app: web.Application) -> None:
    app.router.add_get("/api/autonomy/rooms", get_rooms)


__all__ = [
    "HELD",
    "RECENT_HOURS",
    "atlas_facts",
    "ROOM_KEYS",
    "ROOM_PURPOSE",
    "blocked_facts",
    "channel_facts",
    "NOT_WELL",
    "overview_facts",
    "gather_facts",
    "get_rooms",
    "may_write",
    "health_facts",
    "managed_facts",
    "memory_facts",
    "QUOTE_CHARS",
    "journal_facts",
    "outcomes_facts",
    "pruned_facts",
    "register",
]
