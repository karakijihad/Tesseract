"""The part of the memory store the map never reached.

Measured 2026-09-01: the store held 647 markdown files and the atlas drew 119
of them. The other 528 were not filtered out by a ceiling or by a lack of
links — `MemoryStore.list_frontmatter` walks five subdirectories
(`RECORD_SUBDIRS`) and returns only files carrying a recognised `type:`, and
none of the derived tree layer, the daily notes or the diary is one of those.
So the map was a map of a fifth of the store, and the surface said nothing
about the other four fifths.

**This is a second reader, not a widening of the first.** `list_frontmatter`
means "every typed memory record" and three other callers depend on it meaning
that. What the atlas needs is a different question — every file in the store a
person reads — so it asks it here.

**What is drawn, and what is not, is a decision and it is written down.**

* `trees/**` — 483 files. The derived tree layer: a topic tree gathers every
  leaf sealed under one subject, a source tree gathers them by where they came
  from, a global digest by the day. These are the records Obsidian's graph view
  colour-groups, which is the picture the operator holds this map against.
* `diary/` — 10 files. The assistant's own first-person record. It carries no
  frontmatter at all, so it arrives with no links: that is a fact about the
  diary and not a fault here, and `ATLAS.md` reports them among the orphans.
* `daily/` and `daily/briefs/` — 30 files, deliberately NOT drawn. They are a
  timeline rather than things to look up, and a daily note already reaches the
  graph as a `source:` node wherever a memory cites one, so drawing them here
  would put the same file on the map twice under two ids. `report.NOT_INDEXED`
  says so out loud.

A tree's own heading is its name, because that is what the writer called it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from tesseract.orchestrator.atlas.builders import Emitter, content_hash, stamp
from tesseract.orchestrator.atlas.config import ReviewWindows
from tesseract.orchestrator.atlas.model import (
    Creator,
    NodeKind,
    Provenance,
    entity_id,
)
from tesseract.orchestrator.atlas.store import Atlas

log = logging.getLogger(__name__)

#: Where in the store each of these lives, relative to its root. A directory
#: and not a glob over everything: the whole point of this module is that what
#: reaches the map is chosen per kind rather than swept up, and a `rglob("*")`
#: over the store would silently start drawing whatever lands in it next.
TREES_DIR = "trees"
DIARY_DIR = "diary"

#: The frontmatter field naming what a tree is ABOUT, per parent tree. A topic
#: tree names an entity, a source tree names where its leaves came from; a
#: global digest names a date, which is not a thing to point at, so it has no
#: row and draws no edge.
_SUBJECT_FIELD: dict[str, str] = {
    "topic": "entity",
    "source": "source",
}


def tree_node_id(relative: str) -> str:
    """`tree:<parent>/<name>` — the path inside `trees/`, without the suffix.

    Identity adopted from where the writer put the file, which is the only
    identity these have: a tree carries no id of its own, and the path IS what
    the leaf router keys on when it appends to one.
    """
    return "tree:" + relative.removesuffix(".md")


def diary_node_id(relative: str) -> str:
    """`diary:<name>` — a diary entry is named by its day, which is its file."""
    return "diary:" + Path(relative).stem


@dataclass(frozen=True)
class Subject:
    """What one tree is about, and where the tree says so.

    Held rather than drawn, because whether it becomes an edge depends on
    something no single builder knows yet. See `link_subjects`.
    """

    tree: str
    name: str
    locator: str


def build_trees(
    atlas: Atlas,
    store_dir: Path,
    *,
    now: datetime,
    version: int,
    windows: ReviewWindows,
    reuse: dict[str, tuple[str, str]] | None = None,
    ledger: Any = None,
) -> tuple[int, int, tuple[Subject, ...]]:
    """`(trees, diary entries, what the trees are about)`.

    Fail-soft on an unreadable tree, like the run log and the evidence tree: a
    store with no derived layer is the state of a fresh install, and it must
    not be the thing that leaves the operator with no atlas.

    `ledger` is a `tags.Ledger`, filled for the same reason `build_memory`
    fills one: a tree carries tags like a memory does, and which labels earn a
    node is decided once over the whole distribution rather than here.
    """
    emit = Emitter(atlas, now=now, version=version, windows=windows)
    cached = reuse or {}
    trees, subjects = _build_trees(emit, store_dir, cached, ledger)
    diary = _build_diary(emit, store_dir, cached)
    return trees, diary, subjects


def _build_trees(
    emit: Emitter, store_dir: Path, cached: dict, ledger: Any = None
) -> tuple[int, tuple[Subject, ...]]:
    root = store_dir / TREES_DIR
    seen = 0
    subjects: list[Subject] = []
    for path in _files(root):
        relative = path.relative_to(root).as_posix()
        node_id = tree_node_id(relative)
        fields, heading = _read(path)
        parent = str(fields.get("parent_tree") or "").strip()
        field_name = _SUBJECT_FIELD.get(parent, "")
        subject = str(fields.get(field_name) or "").strip() if field_name else ""
        emit.node(
            node_id,
            NodeKind.TREE,
            heading or relative,
            f"{TREES_DIR}/{relative}",
            content=_hash(path, node_id, cached),
            input_stamp=stamp(path),
            # The subject is an alias whether or not it becomes an edge, so
            # asking the atlas about `snake` finds the tree that gathers
            # everything filed under it. Retrieval matches an alias the same
            # way it matches a title.
            aliases=(subject,) if subject else (),
        )
        seen += 1
        if ledger is not None:
            ledger.add(node_id, fields.get("tags"), f"{TREES_DIR}/{relative}")
        if subject and entity_id(subject) != "entity:":
            subjects.append(
                Subject(
                    tree=node_id,
                    name=subject,
                    locator=f"{TREES_DIR}/{relative}#{field_name}",
                )
            )
    return seen, tuple(subjects)


def link_subjects(
    atlas: Atlas,
    subjects: tuple[Subject, ...],
    *,
    now: datetime,
    version: int,
    windows: ReviewWindows,
) -> int:
    """A tree points at what it is about, WHEN the library already knows it.

    **The condition is the whole of this function**, and it is measured. Every
    one of the 464 topic trees names a subject and 10 of those subjects were
    already in the graph. Minting the other 454 would have added 454 nodes
    joined to exactly one record each: a connector that connects nothing, and
    9 percent of the graph made of it. An entity node exists to join records,
    so one that joins a single record is not an entity the map learnt, it is
    the tree's own name written twice.

    So this runs after every builder that mints an entity, and draws the edge
    only where one exists. It is the rule `build_runs` already keeps for the
    same reason — a record naming something the graph does not hold is a fact
    about the store rather than an edge — and it is why this is a second pass
    instead of a line inside the first.
    """
    emit = Emitter(atlas, now=now, version=version, windows=windows)
    drawn = 0
    for subject in subjects:
        target = entity_id(subject.name)
        if target not in atlas.nodes or subject.tree not in atlas.nodes:
            continue
        # Stated by the record: the file says what it is about in its own
        # frontmatter, so no model concluded it.
        emit.edge(
            subject.tree,
            target,
            "mentions",
            provenance=Provenance.STATED_IN_SOURCE,
            creator=Creator.RULE,
            locator=subject.locator,
        )
        drawn += 1
    return drawn


def _build_diary(emit: Emitter, store_dir: Path, cached: dict) -> int:
    root = store_dir / DIARY_DIR
    seen = 0
    for path in _files(root):
        relative = path.relative_to(root).as_posix()
        node_id = diary_node_id(relative)
        _, heading = _read(path)
        emit.node(
            node_id,
            NodeKind.MEMORY,
            heading or path.stem,
            f"{DIARY_DIR}/{relative}",
            content=_hash(path, node_id, cached),
            input_stamp=stamp(path),
        )
        seen += 1
    return seen


def _files(root: Path) -> list[Path]:
    try:
        return sorted(root.rglob("*.md"))
    except OSError:
        log.warning("atlas: %s could not be read; none of it is drawn", root.name)
        return []


def _read(path: Path) -> tuple[dict, str]:
    """`(frontmatter, first heading)` off one read.

    A file with no frontmatter is not an error here — the diary has none at
    all — so the fields come back empty and the heading still names the
    record.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        log.warning("atlas: a record under the store's derived layer is unreadable")
        return {}, ""
    fields: dict = {}
    body = raw
    if raw.startswith("---"):
        parts = raw.split("\n---", 2)
        if len(parts) >= 2:
            try:
                parsed = yaml.safe_load(parts[0][3:])
            except yaml.YAMLError:
                parsed = None
            if isinstance(parsed, dict):
                fields = parsed
            body = parts[1]
    for line in body.splitlines():
        if line.startswith("# "):
            return fields, line[2:].strip()
    return fields, ""


def _hash(path: Path, node_id: str, cached: dict) -> str:
    """The file's hash, reusing the last pass's when the stamp has not moved.

    Same bargain the vault and evidence builders make, and it matters more
    here: this is 493 files, and re-hashing all of them every night to learn
    that a topic tree nobody touched is unchanged is the one avoidable cost in
    this module.
    """
    raw_stamp = stamp(path)
    known = cached.get(node_id)
    if raw_stamp and known and known[0] == raw_stamp:
        return known[1]
    return content_hash(path)


__all__ = [
    "DIARY_DIR",
    "TREES_DIR",
    "Subject",
    "build_trees",
    "diary_node_id",
    "link_subjects",
    "tree_node_id",
]
