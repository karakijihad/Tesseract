"""What the runtime is made of, which was a compartment with nothing in it.

`Region.MADE_OF` was declared when regions were, and no kind was ever mapped
to it. So the surface's own hint said "the four halves of one brain", three
chips rendered, and `report.NOT_INDEXED` carried the line "the code and config
it is made of — the manifest declares it; nothing draws it" for as long as the
region existed. The operator's answer to that was to fill it.

**What fills it is the runtime's own declaration of what runs on its own.**
Not every Python file: a module is a fact about a checkout and a thousand of
them would be a picture of a repository rather than of a machine. Two registries
already say, in the operator's language, what this thing does when nobody is
asking it to:

* `scheduler/manifest/registry.py` — every schedule row, service, trigger and
  on-demand entry, each with a summary, what would be lost without it, what it
  costs and which chains it rides. 29 of them. `entry.py`'s own docstring says
  a declaration here has the shape a tool's does, a level up.
* `scheduler/pipeline/registry.py` — the stages the two rows run, each with a
  summary and its declared `reads`, `writes` and `after`. 25 of them.

Between them they are what the machine IS, and their declared edges are real
causality rather than resemblance: a stage that reads what another writes is
downstream of it, and the ordering is data rather than a clock minute.

**And they are what makes the body legible.** 3,538 run nodes named a job and
linked to almost nothing; 65 percent of run rows name something declared here,
so `ran` joins two thirds of the body to the part of the machine it is a run
of. The other nine names are the autonomy kernel's own loops, which are in
neither registry, and `report.NOT_INDEXED` says so.
"""

from __future__ import annotations

import logging
from datetime import datetime

from tesseract.orchestrator.atlas.builders import Emitter
from tesseract.orchestrator.atlas.config import ReviewWindows
from tesseract.orchestrator.atlas.model import (
    Creator,
    NodeKind,
    Provenance,
)
from tesseract.orchestrator.atlas.store import Atlas

log = logging.getLogger(__name__)

#: Where the manifest's own declarations live, for an entry that names no loop
#: of its own. A row is found through `schedule.yaml` and has no site, so this
#: is the honest answer to "where is this written down".
_MANIFEST_FILE = "scheduler/manifest/registry.py"
_PIPELINE_FILE = "scheduler/pipeline/registry.py"


def capability_node_id(name: str) -> str:
    """`capability:<name>` — the name the ledger bills to and the panel keys
    on, adopted rather than invented. A manifest entry and a pipeline stage
    cannot collide: `registry._by_name` refuses a duplicate entry name, and
    `checks.py` refuses a stage that shadows one."""
    return f"capability:{name}"


def build_made_of(
    atlas: Atlas,
    *,
    now: datetime,
    version: int,
    windows: ReviewWindows,
) -> int:
    """Everything that runs on its own, and the declared edges between them.

    Imported at the call rather than at the top, the same as the watchman and
    the scheduler are: nothing that merely imports the atlas should drag the
    whole pipeline registry in with it.

    Fail-soft. A registry that will not import is a fault worth logging and
    not a reason to leave the operator with no map at all.
    """
    try:
        from tesseract.scheduler.manifest.registry import ENTRIES
        from tesseract.scheduler.pipeline import stages as _stages  # noqa: F401
        from tesseract.scheduler.pipeline.registry import rows
    except Exception:  # noqa: BLE001 — a registry is not a reason to lose the map
        log.warning("atlas: what the runtime is made of could not be read")
        return 0

    emit = Emitter(atlas, now=now, version=version, windows=windows)
    seen = 0
    for entry in ENTRIES:
        emit.node(
            capability_node_id(entry.name),
            NodeKind.CAPABILITY,
            entry.summary,
            _site_of(entry),
            aliases=(entry.name,),
        )
        seen += 1

    #: Which stage writes each artifact, so a stage that READS one can point
    #: at the stage that makes it. Built over every row, because a row may
    #: consume what another row produces and `Row.imports` is where that is
    #: declared: an artifact's producer is a question about the system.
    writes: dict[str, str] = {}
    for row in rows():
        for stage in row.stages:
            for artifact in stage.writes:
                writes[artifact] = stage.name

    for row in rows():
        for stage in row.stages:
            node_id = capability_node_id(stage.name)
            emit.node(
                node_id,
                NodeKind.CAPABILITY,
                stage.summary or stage.name,
                _stage_site(stage),
                aliases=(stage.name,),
            )
            seen += 1
            # A stage runs because its row does, so what it spends is the
            # row's spend and the ledger bills it there. That is a real
            # containment and the map should show it.
            emit.edge(
                node_id,
                capability_node_id(row.name),
                "part_of",
                provenance=Provenance.STATED_IN_SOURCE,
                creator=Creator.RULE,
                locator=f"{_PIPELINE_FILE}#{row.name}",
            )
            for artifact in stage.reads:
                producer = writes.get(artifact)
                if producer is None or producer == stage.name:
                    continue
                # A data dependency: this stage uses what that one made. The
                # one genuinely causal edge in this module, and the reason
                # `after` below is not one.
                emit.edge(
                    node_id,
                    capability_node_id(producer),
                    "reads_from",
                    provenance=Provenance.STATED_IN_SOURCE,
                    creator=Creator.RULE,
                    locator=f"{_PIPELINE_FILE}#{stage.name}/reads/{artifact}",
                )
            for other in stage.after:
                if other == stage.name:
                    continue
                # Ordering without dependency, which is what `after` MEANS:
                # run after that one, but do not wait on its result. It says
                # which goes first and never that the first produced the
                # second, so it is not causality.
                emit.edge(
                    node_id,
                    capability_node_id(other),
                    "runs_after",
                    provenance=Provenance.STATED_IN_SOURCE,
                    creator=Creator.RULE,
                    locator=f"{_PIPELINE_FILE}#{stage.name}/after/{other}",
                )
    return seen


def _site_of(entry) -> str:
    """Where an entry is written down, as a path under the package.

    A service names its own loop as `tesseract/path/to.py:function`, which is
    a better answer than the registry: a service IS a loop and the loop is
    what somebody wants to open. Everything else is found through the
    registry, which is where its declaration lives.
    """
    site = (entry.site or "").strip()
    if not site:
        return _MANIFEST_FILE
    return site.split(":", 1)[0].removeprefix("tesseract/")


def _stage_site(stage) -> str:
    """The module the stage's body is defined in.

    Off the function rather than off a table, so a stage that moves file takes
    its own locator with it. A `Stage` declares no path of its own and a hand
    written map of 25 names to 25 modules would be wrong within a release.
    """
    module = getattr(stage.body, "__module__", "") or ""
    if not module.startswith("tesseract."):
        return _PIPELINE_FILE
    return module.removeprefix("tesseract.").replace(".", "/") + ".py"


__all__ = ["build_made_of", "capability_node_id"]
