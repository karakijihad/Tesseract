"""The panel stops asking. One producer says what changed.

Every room on the Autonomy panel was REST-fed and polled, so a state changed on
screen because the panel asked again rather than because the runtime said so.
That is not a drawing problem: nothing broadcast a node's state at all, so no
surface could react to one changing, and three of AR-8's criteria waited on the
same missing producer.

**One producer, and it derives nothing of its own.** It calls the same
functions the two routes call — `autonomy_map.entry_states` for what fires
work, `autonomy_map.in_flight_states` for the run that is open now, and
`autonomy_health.read_departments` for what is well. A second derivation of
"how is that doing" would be a second answer, and the one on the socket would
be the one nobody checked.

**Two cadences, because the two halves cost differently.** The open run is one
file, and it is the half that changes while somebody is watching: a step taking
its turn, and a step going quiet for longer than it declared. Everything else
means reading the scheduler's whole run log, which is the largest log the app
keeps, so it runs on the slower cadence and in a thread.

**The slow pass always publishes, and publishes everything.** A panel that has
only ever been told about changes cannot tell "nothing changed" from "I stopped
being told", and it cannot recover from a dropped event either. A full snapshot
on a known cadence answers both: a gap in the sequence is detectable, and it
heals by itself at the next one.

Nothing is published while nobody is connected. The feed exists to keep an open
panel current, and a machine with no cockpit open should not be reading files
for an audience that is not there.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from tesseract.config.runtime_limits import (
    default_runtime_config_path,
    load_liveness_sweep_seconds,
    load_liveness_tick_seconds,
)
from tesseract.orchestrator.background_event_bus import get_background_bus
from tesseract.orchestrator.panel_refresh import CHANNEL

log = logging.getLogger(__name__)

#: The one event this module publishes. Named for what happened: a thing the
#: panel draws is in a different state from the one it was in.
EVENT = "liveness_changed"

#: What each kind of thing is called in the key namespace the panel reads. A
#: department and a node can share a name, so the scope is part of the key
#: rather than something the receiver has to infer.
NODE = "node"
DEPARTMENT = "department"


def _keyed(scope: str, states: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {f"{scope}:{name}": liveness for name, liveness in states.items()}


async def in_flight(now: datetime) -> dict[str, dict[str, Any]]:
    """The entry whose run is open. One file, off the loop.

    Only the run in flight, because it is the only thing whose state can move
    without a record being written: a step taking its turn, and a step going
    quiet for longer than it declared. Everything else changes when something
    is written down, and the sweep below is what reads those.
    """
    from tesseract.mirror.server.routes.autonomy_map import in_flight_states

    return _keyed(NODE, await asyncio.to_thread(in_flight_states, now))


async def everything(app: web.Application, now: datetime) -> dict[str, dict[str, Any]]:
    """Every state the panel can be told about, in one pass."""
    from tesseract.mirror.server.routes.autonomy_health import (
        department_states,
        read_departments,
    )
    from tesseract.mirror.server.routes.autonomy_map import entry_states

    entries, (departments, _sweep, _tail) = await asyncio.gather(
        asyncio.to_thread(entry_states, now),
        read_departments(app, now, with_tail=False),
    )
    return {
        **_keyed(NODE, entries),
        **_keyed(DEPARTMENT, department_states(departments)),
    }


class LivenessFeed:
    """What has been said, so that only changes are said again."""

    def __init__(self, sweep_seconds: float = 0.0) -> None:
        self._last: dict[str, dict[str, Any]] = {}
        self._seq = 0
        self._sweep_seconds = sweep_seconds

    def _publish(self, states: dict[str, dict[str, Any]], *, full: bool) -> None:
        self._seq += 1
        get_background_bus().publish(
            EVENT,
            {
                "kind": EVENT,
                "channel": CHANNEL,
                "session_id": "",
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "data": {
                    "seq": self._seq,
                    "full": full,
                    "states": states,
                    # How often the whole of it is said, so a panel can tell
                    # "nothing changed" from "I have stopped being told". The
                    # runtime's own number rather than one the panel guesses:
                    # a deadline invented in the view is a second answer to a
                    # cadence this file owns.
                    "sweepSeconds": self._sweep_seconds,
                },
            },
        )

    def sweep(self, states: dict[str, dict[str, Any]]) -> None:
        """Say everything, whether or not it changed.

        Unconditional on purpose. This is the message a panel reads as "the
        runtime is still here and this is the whole of it", and one withheld
        because nothing moved is indistinguishable from one that never came.
        """
        # A copy, because what is published is handed to the bus and what is
        # kept is mutated by the next `changes`. Sharing one dict between the
        # two rewrites a message that has already been sent.
        self._last = dict(states)
        self._publish(states, full=True)

    def changes(self, states: dict[str, dict[str, Any]]) -> None:
        """Say what is different from the last thing said about it.

        Only what is IN the pass. A key that has gone is not a change this can
        see, because the absence of a state says nothing about what the state
        now is: the caller that notices one has gone re-reads it instead.
        """
        changed = {
            key: liveness
            for key, liveness in states.items()
            if self._last.get(key) != liveness
        }
        if not changed:
            return
        self._last.update(changed)
        self._publish(changed, full=False)

    def forget(self) -> None:
        """Drop what was said. The next listener is told everything again."""
        self._last = {}


async def feed_loop(app: web.Application) -> None:
    """Publish state changes for as long as a panel is open to receive them."""
    config = default_runtime_config_path()
    tick = load_liveness_tick_seconds(config)
    sweep_every = load_liveness_sweep_seconds(config)
    feed = LivenessFeed(sweep_seconds=sweep_every)
    since_sweep = sweep_every
    #: The entries that had a run open at the last look. A key that leaves
    #: this set is a run that finished, which is a change nothing else reports.
    open_now: set[str] = set()
    while True:
        try:
            await asyncio.sleep(tick)
            since_sweep += tick
            if get_background_bus().subscriber_count == 0:
                # Nobody to tell. What was said no longer stands for what the
                # next panel to connect has been told, so the slate is cleared
                # rather than left to make its first message a diff against a
                # conversation it was not part of.
                feed.forget()
                since_sweep = sweep_every
                open_now = set()
                continue
            now = datetime.now(timezone.utc)
            if since_sweep >= sweep_every:
                since_sweep = 0.0
                feed.sweep(await everything(app, now))
                # What is open, taken again after the sweep rather than left
                # to the next tick. A run that started between the last tick
                # and the sweep, was reported open BY the sweep and then
                # finished, was in no set anything could compare against, so
                # nothing said it had ended.
                open_now = set(await in_flight(now))
                continue
            live = await in_flight(now)
            ended = open_now - set(live)
            if ended:
                # A run finished. The fast pass cannot say so, because what it
                # reports is what is open and a finished run is simply absent
                # from it, so the panel went on drawing one as working until
                # the next sweep. The closed record is written by the time the
                # manifest is gone, so this reads everything once: a run
                # ending is rare, and one extra pass is cheaper than a panel
                # claiming work that is over.
                feed.changes(await everything(app, now))
            else:
                feed.changes(live)
            # AFTER the publish, never before: a read that raises on the tick a
            # run closed would otherwise have already dropped the entry from
            # the set that notices, and the closure would be lost until the
            # next sweep. Recording it last is what makes the next tick retry.
            open_now = set(live)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a feed that fails takes itself off the board
            log.exception("liveness feed: a pass failed; the panel falls back to its own reads")


__all__ = [
    "DEPARTMENT",
    "EVENT",
    "LivenessFeed",
    "NODE",
    "everything",
    "feed_loop",
    "in_flight",
]
