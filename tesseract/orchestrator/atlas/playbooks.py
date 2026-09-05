"""A playbook on the map, and the turns it was learned from.

A playbook is a skill that declares the procedure contract
(`brain/skills.py::PLAYBOOK_KEYS`): the shape of problem it answers, the steps
and the tool each one uses, what done looks like, and the turns it came from.
It goes in the compartment for what the machine is made of, beside the stages
and the manifest entries, because it is the same kind of claim a level up: a
thing the runtime does when the shape of a problem calls for it, declared
rather than remembered.

**Its own kind, not a capability.** A capability's record is a module under
the package tree and `locate.py` holds it to that (`.py`, under the source
root). A playbook is a SKILL.md under the home tree, which an update never
touches. One kind reading two roots by convention is the defect the raw-source
comment in `locate.py` already names.

**The edge is `promoted_from`, the word the memory store already uses** for a
record kept out of a run, and it is drawn only when the turn is on the map: a
turn outside the body's window is a fact the playbook's `evidence` field
still states, and the reader shows the field. Nothing here invents a third
spelling for "learned from".

**A retired revision is not drawn.** It stays on disk under
`<name>/history/` as the record a later revision is measured against, and a
map that drew it would be offering a procedure the runtime already judged.

Seeding is by name and trigger: `atlas_query` finds a playbook when the
question contains either, because a playbook is in neither index the query
seeds from. The `trigger` rides as an alias for exactly that reason.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from tesseract.brain.skills import SKILL_FILENAME, load_skills
from tesseract.orchestrator.atlas.builders import Emitter, content_hash, stamp
from tesseract.orchestrator.atlas.config import ReviewWindows
from tesseract.orchestrator.atlas.model import Creator, NodeKind, Provenance
from tesseract.orchestrator.atlas.store import Atlas
from tesseract.orchestrator.atlas.turns import turn_node_id

log = logging.getLogger(__name__)


def playbook_node_id(name: str) -> str:
    return f"playbook:{name}"


def build_playbooks(
    atlas: Atlas,
    skills_dir: Path,
    *,
    now: datetime,
    version: int,
    windows: ReviewWindows,
) -> int:
    """Every playbook under `skills_dir` as a node, joined to its evidence.

    After the turn builder, deliberately: `promoted_from` points at a turn
    node and is drawn only when that node is already in the graph.
    """
    emit = Emitter(atlas, now=now, version=version, windows=windows)
    seen = 0
    for entry in load_skills(skills_dir):
        if not entry.is_playbook or entry.status == "retired":
            continue
        path = skills_dir / entry.dirname / SKILL_FILENAME
        locator = f"{entry.dirname}/{SKILL_FILENAME}"
        node_id = playbook_node_id(entry.name)
        aliases = [entry.name]
        if entry.trigger:
            aliases.append(entry.trigger)
        emit.node(
            node_id,
            NodeKind.PLAYBOOK,
            entry.description,
            locator,
            content=content_hash(path),
            input_stamp=stamp(path),
            aliases=tuple(aliases),
        )
        seen += 1
        for turn_id in entry.evidence:
            turn_node = turn_node_id(turn_id)
            if turn_node not in atlas.nodes:
                continue
            emit.edge(
                node_id,
                turn_node,
                "promoted_from",
                provenance=Provenance.STATED_IN_SOURCE,
                creator=Creator.RULE,
                locator=f"{locator}#evidence",
                asserted_by=entry.name,
            )
    return seen


__all__ = ["build_playbooks", "playbook_node_id"]
