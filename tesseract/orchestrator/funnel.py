"""The machine's shape, declared: every way in, the one place a turn happens,
and what that turn can reach.

The Autonomy panel draws a map beside whatever you opened, and a map has to be
generated from something. `scheduler/pipeline/diagram.py` answers this for the nightly graph
because a stage graph is already a declaration. Nothing declares the funnel —
which doors exist, that they all arrive at one session, what hangs off it — so
it is declared here, once, and drawn from here. A picture drawn in TSX instead
would be the frontend describing the backend again, which is how the Kernel
rail acquired five signals that render cold forever.

**A written constant, deliberately.** `kernel/tools/taxonomy.py::GROUPS` is the
same kind of artifact and says why: this is the one thing no code can derive.
What keeps it honest is that every claim it makes is checkable — each node
names the site it stands for, and the reach nodes between them account for
every group in the taxonomy, so a tool group added later has nowhere to hide.

**Nothing here claims liveness.** A node says what it IS; whether it is doing
anything right now is the route's answer, off a producer that exists. A node
with no producer renders `not_instrumented` rather than quiet, per
`_shared/liveness-contract.md`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from tesseract.kernel.tools.taxonomy import GROUPS
from tesseract.scheduler.manifest.entry import MIN_SUMMARY_CHARS

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_SITE = re.compile(r"^tesseract/[\w/]+\.py:\w+$")


class FunnelError(ValueError):
    """A declaration that does not hold. Raised at import of this module."""


class Band(str, Enum):
    """Where a node sits on the map. Order is the order it is drawn in.

    Four bands and this file declares nodes for three of them. `RUNS` holds
    the things that fire work without anybody asking, and those are already
    declared — `scheduler/manifest/registry.py` is where they say what they
    are, and a second list of them here would be a second thing to keep in
    step.
    """

    IN = "in"            # a way in
    SESSION = "session"  # the one place a turn happens
    REACHES = "reaches"  # what a turn can reach
    RUNS = "runs"        # what starts itself, declared in the manifest


# The heading over each band. Backend copy, like every other word the floor
# plan renders: a heading written in TSX is a description of this file that
# goes stale the first time a band changes.
BAND_LABELS: dict[Band, str] = {
    Band.IN: "in",
    Band.SESSION: "one session",
    Band.REACHES: "reaches",
    Band.RUNS: "runs on its own",
}


@dataclass(frozen=True)
class FunnelNode:
    name: str
    # What it is, for the operator. The map draws names only — this is what
    # opens under one.
    summary: str
    band: Band
    # Where it is, as `tesseract/path/to.py:Symbol`. A node standing for
    # nothing is the defect this file exists to make impossible, so the site
    # is checked against the tree by a test rather than only by a regex.
    site: str
    # The `taxonomy.py::GROUPS` slugs this node answers for. Reach nodes
    # between them cover every slug, so a group added later must be placed on
    # the map or the check fails.
    groups: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _NAME.match(self.name or ""):
            raise FunnelError(f"funnel node name {self.name!r} must be a lowercase slug")
        if len(self.summary.strip()) < MIN_SUMMARY_CHARS:
            raise FunnelError(
                f"funnel node {self.name!r}: summary is "
                f"{len(self.summary.strip())} characters and the floor is "
                f"{MIN_SUMMARY_CHARS}. The operator reads this, not the code"
            )
        if not _SITE.match(self.site or ""):
            raise FunnelError(
                f"funnel node {self.name!r} names no site. Give it as "
                "`tesseract/path/to.py:Symbol` — a node on the map has to be "
                "something somebody can open"
            )
        unknown = [slug for slug in self.groups if slug not in GROUPS]
        if unknown:
            raise FunnelError(
                f"funnel node {self.name!r} claims tool groups that do not exist: "
                f"{unknown}"
            )


@dataclass(frozen=True)
class FunnelEdge:
    source: str
    target: str
    # What passes along it. Every edge carries something: an arrow with
    # nothing on it is a claim about ordering, and the funnel has no ordering
    # to claim.
    carries: str


NODES: tuple[FunnelNode, ...] = (
    FunnelNode(
        name="cockpit",
        summary="the app's own window, where a turn is typed or spoken",
        band=Band.IN,
        site="tesseract/mirror/server/session_factory.py:create_server_session",
    ),
    FunnelNode(
        name="channel",
        summary="a message arriving from somewhere else, such as Telegram",
        band=Band.IN,
        site="tesseract/mirror/server/channel_turn.py:_start_channel_turn",
    ),
    FunnelNode(
        name="terminal",
        summary="a shell in the app, and whatever is driven from inside it",
        band=Band.IN,
        site="tesseract/mirror/server/pty_manager.py:PTYManager",
    ),
    FunnelNode(
        name="schedule",
        summary="work that starts itself, on the clock or on an event",
        band=Band.IN,
        site="tesseract/scheduler/engine.py:_tick_loop",
    ),
    FunnelNode(
        name="session",
        summary="one conversation with the assistant, whichever door it came through",
        band=Band.SESSION,
        site="tesseract/brain/chat.py:ChatSession",
    ),
    FunnelNode(
        name="memory",
        summary="what it remembers, and what it is reminded of on a turn",
        band=Band.REACHES,
        site="tesseract/memory/store.py:MemoryStore",
        groups=("remembering",),
    ),
    FunnelNode(
        name="vault",
        summary="the research library it reads from and files into",
        band=Band.REACHES,
        site="tesseract/kernel/tools/vault_query.py:VaultQueryTool",
        groups=("research-library",),
    ),
    FunnelNode(
        name="web",
        summary="searching the web, and driving a page when reading one is not enough",
        band=Band.REACHES,
        site="tesseract/kernel/tools/web_search.py:WebSearchTool",
        groups=("searching-the-web", "driving-a-web-page"),
    ),
    FunnelNode(
        name="observer",
        summary="looking at the screen for itself, when it is allowed to",
        band=Band.REACHES,
        site="tesseract/kernel/tools/screen_look.py:ScreenLookTool",
        groups=("looking-for-yourself",),
    ),
    FunnelNode(
        name="tools",
        summary="everything else a turn can do: files, commands, delegates, reaching you",
        band=Band.REACHES,
        site="tesseract/brain/tools.py:ToolRegistry",
        groups=(
            "files-on-disk",
            "showing-the-operator",
            "handing-work-off",
            "long-running-collaborators",
            "tracking-spawned-work",
            "running-commands",
            "reaching-the-operator",
            "asking-without-blocking",
            # Proposing, taking up and closing a task the operator accepted.
            # A task is worked in a turn, so it reaches through the same door.
            "owed-work",
            "being-present",
            "time",
            "projects",
            # Seeing which accounts it has and asking for one it lacks. Here
            # rather than on a node of its own because nothing spends one
            # yet: a node would draw a reach the runtime does not have.
            "your-own-accounts",
            "extending-yourself",
            "checking-your-state",
            # Reaching what this turn is not carrying: a tool whose schema is
            # not in the payload, or a playbook the prompt named without its
            # steps. Both are the same act through the same door, and neither
            # is a reach of its own: what comes back is what the turn was
            # already allowed to use.
            "finding-a-tool",
            "finding-a-playbook",
        ),
    ),
    FunnelNode(
        name="atlas",
        summary="the map of who and what, built from what memory already holds",
        band=Band.REACHES,
        site="tesseract/orchestrator/atlas/store.py:Atlas",
    ),
)


EDGES: tuple[FunnelEdge, ...] = (
    FunnelEdge(source="cockpit", target="session", carries="a turn"),
    FunnelEdge(source="channel", target="session", carries="a turn"),
    FunnelEdge(source="terminal", target="session", carries="a turn"),
    FunnelEdge(source="schedule", target="session", carries="work it starts itself"),
    FunnelEdge(source="session", target="memory", carries="what it recalls and keeps"),
    FunnelEdge(source="session", target="vault", carries="what it reads and files"),
    FunnelEdge(source="session", target="web", carries="what it looks up"),
    FunnelEdge(source="session", target="observer", carries="what it looks at"),
    FunnelEdge(source="session", target="tools", carries="everything else it does"),
    FunnelEdge(source="memory", target="atlas", carries="entities and links"),
)


def _validate() -> None:
    """Raised at import, because a map that lies is worse than no map."""
    names = [n.name for n in NODES]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise FunnelError(f"funnel nodes declared twice: {duplicates}")

    declared_here = [n.name for n in NODES if n.band is Band.RUNS]
    if declared_here:
        raise FunnelError(
            f"{declared_here} are declared here in the `runs` band, which the "
            "manifest owns. Declare them in scheduler/manifest/registry.py"
        )

    unnamed = [band.value for band in Band if band not in BAND_LABELS]
    if unnamed:
        raise FunnelError(
            f"these bands have no heading: {unnamed}. A band the map cannot "
            "name is one it would draw over an empty word"
        )

    known = set(names)
    for edge in EDGES:
        if edge.source == edge.target:
            raise FunnelError(
                f"funnel edge {edge.source!r} points at itself, which says nothing"
            )
        for end, node in (("source", edge.source), ("target", edge.target)):
            if node not in known:
                raise FunnelError(
                    f"funnel edge {edge.source} -> {edge.target} names {end} "
                    f"{node!r}, which is not a declared node"
                )

    # A node nothing reaches and that reaches nothing is on the map for no
    # stated reason, which is the same defect as an undeclared edge read from
    # the other end.
    touched = {e.source for e in EDGES} | {e.target for e in EDGES}
    orphans = sorted(set(names) - touched)
    if orphans:
        raise FunnelError(
            f"funnel nodes with no declared edge: {orphans}. Say what reaches "
            "them, or what they reach"
        )

    sessions = [n.name for n in NODES if n.band is Band.SESSION]
    if len(sessions) != 1:
        raise FunnelError(
            f"the funnel has one session and this declares {sessions}. Every door "
            "arriving at one place is the thing the picture exists to show"
        )
    only = sessions[0]
    for node in NODES:
        if node.band is Band.IN:
            if not any(e.source == node.name and e.target == only for e in EDGES):
                raise FunnelError(
                    f"funnel door {node.name!r} does not reach {only!r}. A door that "
                    "arrives somewhere else is a second funnel"
                )

    claimed: dict[str, str] = {}
    for node in NODES:
        for slug in node.groups:
            if slug in claimed:
                raise FunnelError(
                    f"tool group {slug!r} is claimed by both {claimed[slug]!r} and "
                    f"{node.name!r} — the map would draw it twice"
                )
            claimed[slug] = node.name
    unplaced = sorted(set(GROUPS) - set(claimed))
    if unplaced:
        raise FunnelError(
            f"these tool groups are on no node of the map: {unplaced}. Put each "
            "one on a reach node, so a group nobody placed cannot go unseen"
        )


_validate()


def nodes_of(band: Band) -> tuple[FunnelNode, ...]:
    return tuple(n for n in NODES if n.band is band)


def node(name: str) -> FunnelNode | None:
    return next((n for n in NODES if n.name == name), None)


__all__ = [
    "BAND_LABELS",
    "Band",
    "EDGES",
    "FunnelEdge",
    "FunnelError",
    "FunnelNode",
    "NODES",
    "node",
    "nodes_of",
]
