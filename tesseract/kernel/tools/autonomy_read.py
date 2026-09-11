"""autonomy_read — what the Autonomy panel says, to whoever asked.

The panel knew eight rooms' worth of the runtime's own state and nothing could
ask it. `panel_lines.py` and the four `/api/autonomy/*` routes are reachable
only by the frontend, so "how is autonomy doing" could not be answered by the
assistant on ANY surface, the cockpit's own chat included. `cockpit_show` is a
window mover: asked from a phone it correctly says it has no window and tells
the operator to go and open it themselves, which is the whole fork in one
sentence.

Ruling 23, operator, 2026-08-25: *"If I request to show me autonomy, request to
show me information, I should receive these informations. I know I can enter
via TeamViewer, but I need to have eyes via channels too."* A question the
assistant can answer at the desk it answers from a phone, and a remote-desktop
session is not an answer, it is the workaround that proves the surface cannot
do it.

**So this is a reader, not a channel feature.** It assembles nothing: it calls
`autonomy_rooms.gather_facts`, which is the same join the route calls, and
`panel_lines.lines_for`, which is the same cache the panel reads. There is no
Telegram shape here, no per-surface digest, and no fourth place deciding what a
room contains. Whatever the panel would say, this says, in the same words.

**On a cold cache it waits briefly, where the panel does not.** The route answers from
the cache and writes behind itself, because a panel is left open and picks the
sentence up on its next poll. A person who asked a question is not left open:
they read the reply and put the phone down, so writing behind them means they
never see it. Measured live on 2026-08-25, the first ask after a restart came
back as counted facts and the sentences landed ten seconds later, addressed to
nobody.

So a read whose rooms are all cached returns at once, and a read that is
missing any of them waits for the writer, bounded by the PRIMARY ref's own
`timeout_seconds` rather than the chain walked to the end. That distinction is
not cosmetic: a chain walked to the end can be minutes, and a chat is
serialised behind its own lock, so the bot would go silent for four minutes to
save one sentence. Whatever has landed when the wait ends is returned and the
rest keep their counted facts: the writer caches per room, so a wait that runs
out still leaves everything it finished. The ceiling is the `panel_writer`
manifest entry's, the same one the panel spends against.

`default_posture="auto"` — it reads local state the operator owns. Opening the
panel costs no approval; asking what it says costs none either.

**On a cold cache it does spend**, and the docstring used to say it made no
outbound call of its own, which was true only of the warm path. Writing the
sentences is a provider call on the panel writer's chain, bounded by that
chain's own ceiling and by the wait below, and not by a count. That is the
same trade `image_generate` and `vault_query` make at the same posture, and
it is written down here rather than left for a reader to discover in `run`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable, ClassVar, Literal, Optional

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)

#: The rooms, named the way the panel's own route names them. Restated rather
#: than imported because the input schema has to be static for the model to
#: read it, and a name the model can pick is worth more than a `str` it can
#: mistype. `ROOM_KEYS` stays the source of truth and a test pins this against
#: it, because the last thing that let a stale panel name through a `Literal`
#: threw during render.
RoomKey = Literal[
    "overview",
    "day",
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
]


#: How many rows of one band a detailed read lists before it says how many
#: more there are. A room with forty held items is a room whose answer nobody
#: reads, on a phone least of all, and a list silently cut is worse than a
#: short one that says it was cut.
DETAIL_ROWS = 8


class AutonomyReadInput(BaseModel):
    detail: bool = Field(
        default=False,
        description=(
            "Return what is IN the rooms, not only what each one says. Use it "
            "the moment the operator asks which things, rather than how many: "
            "'what needs me', 'which one failed', 'what is running'. Name a "
            "room with it to get only that room's contents."
        ),
    )
    room: Optional[RoomKey] = Field(
        default=None,
        description=(
            "One room to read. Leave it out for all of them, which is the "
            "answer to 'how is autonomy doing': overview (what wants you, what "
            "is working, what ran while you were away), blocked, health, "
            "managed, memory, outcomes, journal, pruned, retention "
            "(what the machine deletes as it ages), channels, atlas."
        ),
    )


class AutonomyReadTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "checking-your-state"
    summary: ClassVar[str] = (
        "Read what the Autonomy panel says, in the same sentences it shows."
    )
    use_when: ClassVar[str] = (
        "Use whenever you are asked how autonomy is doing, what is running, "
        "what is waiting on the operator, or what one room of that panel "
        "says. Asked what the observer flagged, what was decided without "
        "them, or what came of a recommendation: that is the journal room, "
        "and nothing else in the tree can answer it. Answer from this rather "
        "than sending them to the panel: on a channel they cannot open it. "
        "Relay its lines as they are, room name in front of each. Ask again "
        "with detail:true when they want which and not how many, or the "
        "words a note was written in."
    )
    not_when: ClassVar[str] = (
        "to change what is on the operator's screen, which is `cockpit_show`; "
        "for this machine's hardware and provider health (GPU, disk, "
        "breakers, model files), which is `system_diagnose`. Asked for "
        "\"health\" with no other clue, both are worth reading: this one says "
        "which departments of the runtime need the operator, that one says "
        "whether the machine under them is well."
    )
    depends_on: ClassVar[str] = ""
    receipt_kind: ClassVar[str] = "none"
    recovery_behaviour: ClassVar[str] = "read_only"

    def __init__(self, app_provider: Optional[Callable[[], Any]] = None) -> None:
        self._app_provider = app_provider

    @property
    def name(self) -> str:
        return "autonomy_read"

    @property
    def input_schema(self) -> type[BaseModel]:
        return AutonomyReadInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        del context
        inp = (
            tool_input if isinstance(tool_input, AutonomyReadInput)
            else AutonomyReadInput(**tool_input.model_dump())
        )

        app = self._app_provider() if self._app_provider is not None else None
        if app is None:
            # The producers behind the panel live in the backend process. Say
            # which question went unanswered rather than returning an empty
            # panel, which would read as "nothing is happening".
            return ToolResult(
                output=(
                    "autonomy_read: this runtime has no backend attached, so "
                    "the panel's readers are not reachable from here. Nothing "
                    "is known about autonomy's state, which is not the same as "
                    "nothing happening."
                ),
                is_error=True,
            )

        from tesseract.mirror.server.routes import autonomy_rooms as rooms_route
        from tesseract.orchestrator.autonomy import panel_lines

        now = datetime.now(timezone.utc)
        read = await rooms_route.read_rooms(app, now)

        keys = (inp.room,) if inp.room else rooms_route.ROOM_KEYS
        rooms = [
            panel_lines.Room(key=key, facts=tuple(read.facts.get(key, ())))
            for key in keys
        ]
        said = await panel_lines.lines_for(rooms, now=now)

        if panel_lines.stale(rooms, await asyncio.to_thread(panel_lines.read_cache)):
            said = await self._wait_for_the_writer(
                rooms, chain=rooms_route.writer_chain(app), now=now
            )

        return ToolResult(
            output=(
                _as_detail(rooms, said, read.rows)
                if inp.detail
                else _as_text(rooms, said)
            ),
            metadata={
                "rooms": [
                    {
                        "key": room.key,
                        "said": said.get(room.key, ""),
                        "facts": list(room.facts),
                        **({"rows": read.rows.get(room.key, {})} if inp.detail else {}),
                    }
                    for room in rooms
                ],
                "observedAt": now.isoformat(),
            },
        )


    async def _wait_for_the_writer(
        self,
        rooms: list[Any],
        *,
        chain: Any,
        now: datetime,
    ) -> dict[str, str]:
        """Give the sentences a moment to land, then read the cache again.

        The panel can afford to write behind itself because it is still on
        screen when the sentence arrives. A question cannot: the operator reads
        the reply and puts the phone down, so a line written ten seconds later
        is written to nobody. That is not a hypothetical, it is what the first
        live ask did.

        Bounded by `first_answer_budget`: the PRIMARY ref's own
        `timeout_seconds`, not the chain walked to the end. The difference is
        the whole point. Walking a chain to the end is every entry's timeout,
        and its middle ref was measured timing out at its full 120 on every
        call, so the honest worst case for a room is four minutes. A Telegram
        chat is serialised behind its own lock, so four minutes is the bot
        going silent, which is a worse failure than a plain sentence.

        Whatever landed is returned and the rest keep their counted facts.
        `write_lines` caches per room, so an expired wait keeps everything it
        finished and the next ask is cheaper for it.
        """
        from tesseract.orchestrator.autonomy import panel_lines

        if not chain:
            # No chain, no sentences to wait for. The counted facts already in
            # hand are the right answer and are what the panel shows too.
            return await panel_lines.lines_for(rooms, now=now)

        # Through `write_behind`, never around it. It owns the one-at-a-time
        # guard, and two chains writing the same rooms at once is the shape
        # that guard exists to stop. If the panel already has one running, this
        # waits on THAT one rather than starting a rival.
        panel_lines.write_behind(rooms, chain=chain)
        task = panel_lines.in_flight()
        if task is None:
            # It finished between starting and asking, or refused to start.
            # Either way there is nothing to wait for and the cache is current.
            return await panel_lines.lines_for(rooms, now=now)

        grace = panel_lines.first_answer_budget(chain)
        try:
            # `shield`, so an expired wait does not cancel the write itself.
            # The rooms it goes on to finish are cached, and the next ask has
            # them; cancelling here would throw away work already paid for.
            await asyncio.wait_for(asyncio.shield(task), timeout=grace)
        except asyncio.TimeoutError:
            logger.info(
                "autonomy_read: the writer had not finished in %.0fs, so the "
                "rooms it did not reach answer with their counts",
                grace,
            )
        except Exception:  # noqa: BLE001 — a line may fail; an answer may not
            logger.exception("autonomy_read: the writer failed; answering with counts")
        return await panel_lines.lines_for(rooms, now=now)


def _as_text(rooms: list[Any], said: dict[str, str]) -> str:
    """The rooms as lines, quiet ones left out.

    A room with nothing in it says nothing on the panel, and a list padded with
    "nothing here" eight times is a worse answer than a short one. If every
    room is quiet that is itself the answer, and it is said in words.
    """
    lines = [
        f"{room.key}: {said.get(room.key, '')}"
        for room in rooms
        if said.get(room.key, "")
    ]
    if not lines:
        return "Nothing in any room of the autonomy panel: nothing is running, waiting, or recently finished."
    return "\n".join(lines)


def _as_detail(
    rooms: list[Any], said: dict[str, str], rows: dict[str, dict[str, Any]]
) -> str:
    """Each room's line, then the things inside it.

    The line stays on top because it is the panel's own sentence and the rule
    everywhere else is to relay it. Under it go the rows, named by the band
    they belong to, because "4 held items" and the four items are different
    answers and the operator asked for the second one.
    """
    out: list[str] = []
    for room in rooms:
        line = said.get(room.key, "")
        band = rows.get(room.key) or {}
        listed = [
            f"  {label}: {_one_line(entry)}"
            for label, entries in band.items()
            if isinstance(entries, list) and entries
            for entry in entries[:DETAIL_ROWS]
        ]
        if not line and not listed:
            continue
        out.append(f"{room.key}: {line}" if line else f"{room.key}:")
        out.extend(listed)
        for label, entries in band.items():
            if isinstance(entries, list) and len(entries) > DETAIL_ROWS:
                out.append(
                    f"  ({len(entries) - DETAIL_ROWS} more under {label}, "
                    f"ask for that room on its own)"
                )
    if not out:
        return "Nothing in any room of the autonomy panel: nothing is running, waiting, or recently finished."
    return "\n".join(out)


def _one_line(entry: Any) -> str:
    """One row as one line, from whichever of the panel's names it carries.

    The rooms' routes do not share a row shape, and inventing one here would be
    a third vocabulary for a thing the panel already names twice. So this reads
    the fields that exist and says what it found, rather than requiring any.
    """
    if not isinstance(entry, dict):
        return str(entry)
    # `saidToModel` FIRST, and `said` only after it. A row carrying both is a
    # row whose producer decided its two readers get different text: `said` is
    # the operator's copy and can hold a log line, a provider's own words or
    # the string of an exception this runtime was handed, while `saidToModel`
    # is the half composed here. `routes/autonomy_health.py` states that as
    # invariant 5, and this reader used to ignore it, because `said` sat first
    # and is always truthy. The tool is `auto`, so nothing asked first.
    #
    # Dropping `said` entirely was tried and is wrong. Most rows that carry it
    # carry an agenda item's own goal, and naming those IS what this reader is
    # for: it exists because "8 things want your attention" was answered and
    # the operator asked which ones. A row that keeps only its name answers
    # that with `held: self_reflection` where it used to say what the thing
    # actually was.
    #
    # So the two producers that can put HANDED text in `said` declare a
    # `saidToModel` beside it, the way `department()` always has, and this
    # reads the safe one when a row offers it.
    title = next(
        (
            str(entry[key])
            for key in ("saidToModel", "said", "goal", "name", "title", "subject", "source")
            if entry.get(key)
        ),
        "",
    )
    # `reason` is deliberately not in this list. No producer reaching this
    # reader sets one today, because the two that build a `reason` from
    # `outcome_reason` serve their own routes, but that is a door standing
    # open rather than a door that is shut.
    trail = [
        str(entry[key])
        for key in ("state", "status", "value", "at", "took")
        if entry.get(key) and str(entry[key]) != title
    ]
    return f"{title} ({', '.join(trail)})" if trail else title or str(entry)


__all__ = ["AutonomyReadTool", "AutonomyReadInput", "RoomKey"]
