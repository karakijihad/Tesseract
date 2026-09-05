"""The connector the library already writes and the map never read.

Measured against the operator's own Obsidian view of the same store,
2026-09-01:

| | atlas | Obsidian |
| --- | --- | --- |
| how a memory joins a memory | `auto_links` only | tags, wiki links |
| distinct tags read | 0 | 192 |
| files carrying at least one tag | 620 of 647 | 620 of 647 |

620 of 647 records carried a tag and nothing here read one. Every link between
two memories on the map was `similar_to` — 469 of them, all embedding
neighbours.

**A tag is not a stronger claim than an embedding neighbour, and an earlier
draft said it was.** Tags are written through `memory_save` by the assistant,
so they carry the same `inferred_by_model` provenance as `auto_links` and buy
nothing on that axis. What a tag buys is TOPOLOGY. An embedding neighbour is a
pair, so the graph it makes is a scatter of small neighbourhoods; a tag is a
shared label, so it makes a hub — one node many records point at. That is a
different shape, and it is the shape the operator recognises from a graph they
can read.

**Which tags earn a node is a rule, argued from the distribution.** Not every
tag is a hub worth drawing. `sealed` is on 483 of the 620 tagged files and
`topic-summary` on 464: drawn as nodes those two would join almost everything
to almost everything and make the hairball this map opens by refusing.
`workshop` (11), `promoted` (10) and `feedback` (23) are the useful size. See
`FLOOR` and `CEILING_SHARE`.

Deciding needs the whole distribution, so this cannot happen inside the reader
that walks one record at a time. A `Ledger` collects what each record is filed
under; `build_tags` applies the rule once, at the end, over all of it.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from tesseract.orchestrator.atlas.builders import Emitter
from tesseract.orchestrator.atlas.config import ReviewWindows
from tesseract.orchestrator.atlas.model import (
    Creator,
    NodeKind,
    Provenance,
    entity_id,
)
from tesseract.orchestrator.atlas.store import Atlas

#: The fewest records that have to share a label before it is a subject.
#: Two records sharing one is a coincidence; the same word on a third is a
#: pattern the operator's own filing is expressing. Measured over the live
#: store: a floor of 2 keeps 53 tags and a floor of 3 keeps 32, and every one
#: of the 21 it drops is on exactly two records.
FLOOR = 3

#: The most a label may be on before it stops separating anything. As a SHARE
#: of the records that carry any tag, not a fixed count, because the library
#: grows and a number tuned against 620 files means something else against
#: 6,000. At a tenth: `sealed` (78 percent of tagged files) and
#: `topic-summary` (75 percent) are out, and the biggest tag that survives
#: joins 37 records — 6 percent of the corpus, which is a hub a reader can
#: still see the edge of.
CEILING_SHARE = 0.10


def tag_node_id(name: str) -> str:
    """`tag:<slug>` from the literal label, slugged the way an entity is.

    Deliberately the same slug function: `#tool-use` and `#Tool Use` are one
    label filed twice, and the store already treats them as one when it
    matches. Two DIFFERENT words stay two nodes, which is the same rule
    `entity_id` keeps and for the same reason.
    """
    return "tag:" + entity_id(name).removeprefix("entity:")


@dataclass
class Ledger:
    """What each record is filed under, collected while the readers walk.

    Nothing here decides anything. The rule needs the whole distribution and a
    reader sees one record at a time, so the two are separated: this is the
    collection, `build_tags` is the decision.
    """

    #: `record id -> (label, locator)` pairs, in the order they were read.
    filings: list[tuple[str, str, str]] = field(default_factory=list)

    def add(self, record: str, labels, locator: str) -> None:
        for raw in labels or ():
            name = str(raw).strip()
            if name and tag_node_id(name) != "tag:":
                self.filings.append((record, name, locator))

    @property
    def tagged_records(self) -> int:
        return len({record for record, _label, _locator in self.filings})


def earns_a_node(ledger: Ledger) -> dict[str, int]:
    """`{tag id: how many records carry it}` for the labels that earn a node.

    The rule, in one place, so the surface's count and the builder's decision
    cannot be two answers. Returned with the counts because whoever reports on
    this has to be able to say what was left out and how big it was.

    **Counted by the id, never by the spelling.** `#tool-use` and `#Tool Use`
    are one label filed twice, and counting the strings would split a label's
    own total between its spellings so that neither half reached the floor —
    the tag would vanish for being popular in two ways.
    """
    per_label: dict[str, set[str]] = {}
    for record, label, _locator in ledger.filings:
        per_label.setdefault(tag_node_id(label), set()).add(record)
    # The share alone, and NOT `max(FLOOR, share)`, which was tried first and
    # is worse. Raising the ceiling to the floor means that in a store of
    # three tagged records a label on all three earns a node — one hub joining
    # the entire library, which is precisely the hairball the ceiling exists
    # to prevent, arriving at the one size where it is guaranteed.
    #
    # So the two rules cross at 30 tagged records: below that the ceiling is
    # under the floor and no label is a hub yet, which is the right answer
    # rather than a silent failure. A store of five records does not have
    # subjects, it has five records, and every one of them is still found by
    # `memory_search` and still drawn on the map.
    ceiling = ledger.tagged_records * CEILING_SHARE
    return {
        label: len(records)
        for label, records in per_label.items()
        if FLOOR <= len(records) <= ceiling
    }


def build_tags(
    atlas: Atlas,
    ledger: Ledger,
    *,
    now: datetime,
    version: int,
    windows: ReviewWindows,
) -> tuple[int, int]:
    """`(labels drawn, labels left out)`.

    A record filed under a label that earns a node gets an edge to it. The
    provenance is `inferred_by_model` and the creator is the model, the same
    as an embedding neighbour, because the assistant wrote the tag: nothing
    here may present it as something the operator asserted.
    """
    emit = Emitter(atlas, now=now, version=version, windows=windows)
    kept = earns_a_node(ledger)
    every = {tag_node_id(label) for _r, label, _l in ledger.filings}
    if not kept:
        return 0, len(every)

    # Where each label is first seen, so its node cites a file rather than
    # nothing. A label has no record of its own, the same as an entity, and
    # the honest locator is the first record that files something under it.
    # The first SPELLING seen is the node's name for the same reason.
    minted: set[str] = set()
    seen: Counter[str] = Counter()
    for record, label, locator in ledger.filings:
        node_id = tag_node_id(label)
        if node_id not in kept or record not in atlas.nodes:
            continue
        if node_id not in minted:
            minted.add(node_id)
            emit.node(node_id, NodeKind.TAG, label, locator, aliases=(label,))
        emit.edge(
            record,
            node_id,
            "filed_under",
            provenance=Provenance.INFERRED_BY_MODEL,
            creator=Creator.MODEL,
            locator=f"{locator}#tags[{seen[node_id]}]",
            asserted_by="memory_save",
        )
        seen[node_id] += 1

    return len(minted), len(every) - len(minted)


__all__ = [
    "CEILING_SHARE",
    "FLOOR",
    "Ledger",
    "build_tags",
    "earns_a_node",
    "tag_node_id",
]
