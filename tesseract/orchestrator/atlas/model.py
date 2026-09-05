"""What a node and an edge are, and what they may not be without.

The atlas exists because `related_slugs` does not: a plain derived link whose
origin is lost cannot answer "why is this connected?", which means a caller
cannot decline to trust it either. So every edge here carries its origin, its
locator, when it was asserted and by which builder — and the dataclass refuses
to be constructed without them, because a field that is optional is a field
that will be empty on the rows that matter.

Identity is adopted, never invented. A memory already has an id assigned at
write time and a wiki page already has a slug; the atlas reuses both rather
than recovering identity from normalised text at build time. The one node kind
with no write-time identity is the entity, and the rule there is the strict
one: two spellings are two nodes. Nothing in this module merges them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from hashlib import sha1
from typing import Any


class Provenance(str, Enum):
    """Where an edge came from, in precedence order.

    Operator-authored anchors outrank anything derived because they encode
    intent rather than centrality — a hub the operator declared is not the
    same claim as a hub a degree count found.
    """

    OPERATOR_ASSERTED = "operator_asserted"
    STATED_IN_SOURCE = "stated_in_source"
    INFERRED_BY_MODEL = "inferred_by_model"


PRECEDENCE: tuple[Provenance, ...] = (
    Provenance.OPERATOR_ASSERTED,
    Provenance.STATED_IN_SOURCE,
    Provenance.INFERRED_BY_MODEL,
)


# What each provenance class MEANS, for anything a person reads. Beside the
# enum for the reason `KIND_LABELS` and `REGION_LABELS` are: a surface naming
# these itself would be a second vocabulary, and the difference between what a
# record STATES and what a model concluded is the whole reason the class
# exists.
PROVENANCE_LABELS: dict[Provenance, str] = {
    Provenance.OPERATOR_ASSERTED: "you said so",
    Provenance.STATED_IN_SOURCE: "the record says so itself",
    Provenance.INFERRED_BY_MODEL: "the assistant worked it out when the map was drawn",
}


# What each KIND OF LINK says, for anything a person reads. Beside the others
# for the same reason: a surface offering `similar_to` as something to filter
# by is offering a machine word as a control.
LINK_LABELS: dict[str, str] = {
    "links_to": "the record points at it",
    "similar_to": "they read alike",
    "mentions": "the record names it",
    "derived_from": "the record came from it",
    "same_as": "two names for one thing",
    "related_to": "the record calls it related",
    "promoted_from": "kept out of something it did",
    "filed_by": "filed by something it did",
    "filed_under": "the record is filed under it",
    "written_by": "written by something it did",
    "ran": "one run of that",
    "reads_from": "it uses what that one makes",
    "part_of": "it is part of that",
    "runs_after": "it runs after that one",
}


# Which links say that one thing CAUSED another, and which only say that two
# things resemble or accompany each other. Every type above is in exactly one
# of these two sets and a test over the live builders enforces it, because the
# whole reason the atlas is not `related_slugs` is that a caller can tell the
# two claims apart. Measured 2026-09-01 before this existed: 520 of 664 links
# on the live graph were symmetric and 144 causal, and nothing in the code
# said which was which — the ratio had to be worked out by hand from the type
# names, which is exactly the reading a surface cannot do.
#
# A builder inventing a third spelling of a claim already here is the failure
# this set exists to catch: `filed_by` and `written_by` are two different
# things a run leaves behind, and a `produced` beside them would be a third
# name for one of them.
CAUSAL_LINKS: frozenset[str] = frozenset({
    "derived_from",
    "promoted_from",
    "filed_by",
    "written_by",
    "ran",
    "reads_from",
})

#: The rest: a link that says two records go together without either making
#: the other. A declared ordering belongs here rather than above — it says
#: which goes first, never that the first produced the second.
ASSOCIATIVE_LINKS: frozenset[str] = frozenset({
    "links_to",
    "similar_to",
    "mentions",
    "same_as",
    "related_to",
    "filed_under",
    "part_of",
    "runs_after",
})


def is_causal(link_type: str) -> bool:
    """Whether this link says one record caused the other.

    Raises on a type nobody classified, and that is the point: an edge type is
    a free string so a graph on disk may carry one this code has never seen,
    but a BUILDER adding one has to say which claim it is making. The caller
    that walks causality is the one that must never guess.
    """
    if link_type in CAUSAL_LINKS:
        return True
    if link_type in ASSOCIATIVE_LINKS:
        return False
    raise ValueError(
        f"link type {link_type!r} is in neither `CAUSAL_LINKS` nor "
        "`ASSOCIATIVE_LINKS`. Put it in one in the same change that writes "
        "it: a link nobody classified is a link the trace cannot draw and "
        "cannot refuse to draw either."
    )


def label_of_link(link_type: str) -> str:
    """What a kind of link says, in words. Falls back to the raw type, which
    is the one place in this module a fallback is right: an edge's type is a
    free string rather than an enum, so a graph on disk can carry a type this
    version of the code has never heard of, and a surface that raised would
    refuse to draw the whole picture over one word it could not name. Printing
    the type is honest; inventing a phrase for it is not. What keeps the table
    complete is a test that reads the types the builders write."""
    return LINK_LABELS.get(link_type, link_type)


def label_of_provenance(provenance: Provenance) -> str:
    """Where a connection came from, in words. Raises on one nobody named:
    a surface that fell back to `inferred_by_model` would print the enum and
    call it an explanation."""
    try:
        return PROVENANCE_LABELS[provenance]
    except KeyError:
        raise ValueError(
            f"provenance {provenance.value!r} has no label. Add it to "
            "`PROVENANCE_LABELS` in the same change that adds the class."
        ) from None


class Creator(str, Enum):
    """Who made the edge — as distinct from where the claim came from. A rule
    reading a field a model wrote is `rule` creating an `inferred_by_model`
    edge, and both halves are worth keeping."""

    OPERATOR = "operator"
    MODEL = "model"
    RULE = "rule"


class NodeKind(str, Enum):
    MEMORY = "memory"
    WIKI_PAGE = "wiki_page"
    RAW_SOURCE = "raw_source"
    ENTITY = "entity"
    RUN = "run"
    FEEDBACK = "feedback"
    TREE = "tree"
    TAG = "tag"
    AGENDA = "agenda"
    CAPABILITY = "capability"
    PLAYBOOK = "playbook"


class Region(str, Enum):
    """Which compartment of the one brain a node belongs to.

    The operator's model, and it is the right one: the runtime is the daily
    signal a body sends, the memory store is what it learns from living, the
    vault is what it keeps for good, and the code is what it is made of. What
    makes that a brain rather than four piles is that they are intertwined, so
    a region has to be a fact ON the node before an edge between two of them
    can be a different claim from an edge inside one.

    All four have nodes. `MADE_OF` was declared for two months with nothing
    mapped to it, so the surface promised four compartments and drew three;
    what fills it is the runtime's own declaration of what runs on its own —
    the manifest entries and the pipeline stages — which is the honest answer
    to "what is it made of" and is the one part of the code that already says
    what it is for in the operator's language.
    """

    LEARNED = "learned"
    KEPT = "kept"
    BODY = "body"
    MADE_OF = "made_of"


# What each KIND is, in the words a person reads. Here rather than on a
# surface for the reason `REGION_LABELS` is here: the panel's rule is that the
# words about the machine are the backend's, and a legend that named these
# itself would be a second vocabulary the moment a kind was renamed.
KIND_LABELS: dict[NodeKind, str] = {
    NodeKind.MEMORY: "a memory",
    NodeKind.WIKI_PAGE: "a page it wrote",
    NodeKind.RAW_SOURCE: "a file it was given",
    NodeKind.ENTITY: "a person or a thing",
    NodeKind.RUN: "something it did",
    NodeKind.FEEDBACK: "something it found wrong",
    NodeKind.TREE: "what it gathered under one heading",
    NodeKind.TAG: "a label it files things under",
    NodeKind.AGENDA: "something it decided to do",
    NodeKind.CAPABILITY: "a part of what it is made of",
    NodeKind.PLAYBOOK: "a way it learned to do something",
}


REGION_LABELS: dict[Region, str] = {
    Region.LEARNED: "what it learns",
    Region.KEPT: "what it keeps for good",
    Region.BODY: "what its body did",
    Region.MADE_OF: "what it is made of",
}


# What a kind IS decides where it lives, and this is the only place that
# decides it. A kind missing from this table cannot be built, which is what
# makes "no node defaults to a region" true by construction rather than by
# anyone remembering.
REGION_OF: dict[NodeKind, Region] = {
    NodeKind.MEMORY: Region.LEARNED,
    NodeKind.TREE: Region.LEARNED,
    NodeKind.TAG: Region.LEARNED,
    NodeKind.WIKI_PAGE: Region.KEPT,
    NodeKind.RAW_SOURCE: Region.KEPT,
    NodeKind.ENTITY: Region.KEPT,
    NodeKind.RUN: Region.BODY,
    NodeKind.FEEDBACK: Region.BODY,
    NodeKind.AGENDA: Region.BODY,
    NodeKind.CAPABILITY: Region.MADE_OF,
    # A procedure that worked, written down. It is part of what the machine
    # is made of the way a stage is: something it does when the shape of a
    # problem calls for it, declared rather than remembered.
    NodeKind.PLAYBOOK: Region.MADE_OF,
}


class UnknownRegion(ValueError):
    """A node kind nothing has placed in a compartment.

    Raised at construction, so a builder that adds a kind and forgets its
    region fails the build rather than shipping a graph the surface cannot
    draw. Defaulting would put the new thing quietly in the wrong half of the
    brain, which nobody would notice until the picture was wrong.
    """


def region_of(kind: NodeKind) -> Region:
    try:
        return REGION_OF[kind]
    except KeyError:
        raise UnknownRegion(
            f"node kind {kind.value!r} belongs to no region. Add it to "
            "`REGION_OF` in the same change that adds the kind: a node whose "
            "compartment nobody chose is a node the graph cannot place."
        ) from None


def label_of(region: Region) -> str:
    """The operator's own phrase for a compartment, for anything a person reads."""
    return REGION_LABELS[region]


def label_of_kind(kind: NodeKind) -> str:
    """What a record IS, in words. Raises on a kind nobody named, for the same
    reason `region_of` does: a legend that falls back to the enum name prints
    `wiki_page` at somebody and calls it a label."""
    try:
        return KIND_LABELS[kind]
    except KeyError:
        raise ValueError(
            f"node kind {kind.value!r} has no label. Add it to `KIND_LABELS` in "
            "the same change that adds the kind: a surface cannot name a record "
            "the model has not named."
        ) from None


#: The longest a record's name may be. A name is what the map draws under a
#: dot, what the Inspector's "what it touches" prints once per neighbour, what
#: a tool result lists and what a channel reply reads out — so it is a fact
#: about the record and not a display choice, and clamping it on one surface
#: would leave it long on the other four.
#:
#: 80 is measured, not chosen. On the live graph of 2026-09-01 a run's name
#: was 9 characters, a wiki page's 6, a raw source's 13, an entity's 11 and a
#: memory's 46 — and a filed defect's was a MEDIAN of 98 and a maximum of 200,
#: because the builder used the log line the defect was about as its name. One
#: kind out of six was named with a paragraph, and it was the kind the
#: operator opened first: five links from one defect rendered five paragraphs
#: where five names belonged.
#:
#: Enforced here rather than in each builder so a builder arriving later
#: cannot reintroduce it quietly. `builders.Emitter.node` is the sanctioned
#: path and shortens before constructing; a `Node(...)` built by hand with a
#: paragraph raises, which is what a test pins.
MAX_TITLE_CHARS = 80


def shorten(title: str) -> str:
    """A name cut to the ceiling, on a word boundary where there is one.

    Used by the one construction path so every builder gets the ceiling for
    free. The cut is marked, because a name silently truncated reads as a name
    somebody wrote badly rather than as one the map shortened, and the whole
    record is one click away either way.
    """
    text = " ".join(title.split())
    if len(text) <= MAX_TITLE_CHARS:
        return text
    room = MAX_TITLE_CHARS - 1
    cut = text[:room]
    space = cut.rfind(" ")
    # Only fall back to a word boundary that leaves most of the room used:
    # a title whose first word is 70 characters would otherwise be cut to
    # nothing at all.
    if space > room // 2:
        cut = cut[:space]
    return cut.rstrip() + "…"


_SLUG = re.compile(r"[^a-z0-9]+")


def entity_id(name: str) -> str:
    """`entity:<slug>` from the literal string, and no further.

    Deliberately not a similarity function. A personal store accumulates
    near-synonyms — "this machine", "the dev box", "the operator's PC" — that
    no string metric resolves, and a fuzzy merge at 0.9 already erased real
    distinctions in this repo once. Two spellings stay two nodes until
    something outside the builder says otherwise.
    """
    slug = _SLUG.sub("-", name.strip().casefold()).strip("-")
    return f"entity:{slug}"


@dataclass(frozen=True)
class Node:
    """One thing the atlas knows about.

    `input_stamp` is what makes an incremental pass honest: `<mtime_ns>:<size>`
    of the file this node was derived from. Unchanged stamp AND unchanged
    builder version is the only case where re-derivation may be skipped.
    """

    id: str
    kind: NodeKind
    title: str
    locator: str
    builder_version: int
    first_seen: datetime
    updated_at: datetime
    content_hash: str = ""
    input_stamp: str = ""
    aliases: tuple[str, ...] = ()
    # Derived from `kind` at construction, never passed in. Three separate
    # places rebuild a node field by field (`Atlas.add_node`, the builder's
    # first-seen carry, and the builders themselves), and a field each of them
    # had to remember to copy is a field one of them would eventually drop.
    # Deriving it means every copy of a node carries the right region for free,
    # and a stored one can never disagree with the kind beside it.
    region: Region = field(init=False, default=Region.LEARNED)

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("a node needs an id")
        if not self.locator.strip():
            raise ValueError(f"node {self.id!r} has no locator")
        if len(self.title) > MAX_TITLE_CHARS:
            raise ValueError(
                f"node {self.id!r} is named with {len(self.title)} characters "
                f"and the ceiling is {MAX_TITLE_CHARS}. A name says what the "
                "record IS; the line it was found in is the record's body. "
                "Shorten it in the builder, or pass it through "
                "`builders.Emitter.node`, which shortens for you."
            )
        object.__setattr__(self, "region", region_of(self.kind))

    def with_first_seen(self, first_seen: datetime) -> "Node":
        """The same node, keeping the date the graph first saw it.

        Two callers copy a node forward: the store, when a build re-derives one
        it already holds, and the pass, when it carries a date across a version
        bump. Both used to spell the field list out, and a field each of them
        had to remember is a field one of them would eventually drop. This is
        the one place that list lives.
        """
        return replace(self, first_seen=first_seen)

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "region": self.region.value,
            "title": self.title,
            "locator": self.locator,
            "builder_version": self.builder_version,
            "first_seen": self.first_seen.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "content_hash": self.content_hash,
            "input_stamp": self.input_stamp,
            "aliases": list(self.aliases),
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Node":
        """A stored `region` is not read back, deliberately.

        It is written so the file answers for itself and the surface can group
        without recomputing, and it is re-derived on load so a graph from
        before regions existed, or one somebody hand-edited, cannot carry a
        region its kind disagrees with. One rule, in one place, both ways.
        """
        return cls(
            id=str(raw["id"]),
            kind=NodeKind(raw["kind"]),
            # Shortened on the way in as well as on the way out. A graph
            # written before the ceiling existed is still a graph, and
            # refusing to load one would mean the pass that would fix it
            # could not read what it was replacing.
            title=shorten(str(raw.get("title") or "")),
            locator=str(raw["locator"]),
            builder_version=int(raw["builder_version"]),
            first_seen=datetime.fromisoformat(raw["first_seen"]),
            updated_at=datetime.fromisoformat(raw["updated_at"]),
            content_hash=str(raw.get("content_hash") or ""),
            input_stamp=str(raw.get("input_stamp") or ""),
            aliases=tuple(raw.get("aliases") or ()),
        )


@dataclass(frozen=True)
class Edge:
    """One reified relationship.

    The id is derived from what the edge IS — subject, object, type, locator —
    so re-deriving the same relationship replaces its row instead of adding a
    second one. Everything else about it may change between builds; that it is
    the same edge may not.
    """

    subject: str
    object: str
    type: str
    provenance: Provenance
    creator: Creator
    locator: str
    asserted_at: datetime
    builder_version: int
    # `None` means the producer recorded no confidence, which is the honest
    # answer for most of what exists today: an `auto_links` list keeps the
    # winning ids and throws the cosine score away. Defaulting to 1.0 would
    # have printed certainty the store never claimed.
    confidence: float | None = None
    # Invariant 5: provenance says who asserted, not when it stopped being
    # true. `None` means the assertion does not age — reserved for what the
    # operator wrote and for what a record literally states about itself.
    review_after_days: int | None = None
    # The model or rule that produced the underlying claim, where one exists.
    # `vault_librarian` wrote most of the wiki's relationships; that is worth
    # recording even though its prompt version is not recoverable.
    asserted_by: str = ""
    superseded_by: str = ""
    id: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        for name in ("subject", "object", "type"):
            if not getattr(self, name).strip():
                raise ValueError(f"an edge needs a {name}")
        if not self.locator.strip():
            raise ValueError(
                f"edge {self.subject}->{self.object} has no locator; an edge "
                "that cannot point at what supports it cannot be checked"
            )
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"edge {self.subject}->{self.object}: bad confidence")
        if not self.id:
            object.__setattr__(self, "id", self._derive_id())

    def _derive_id(self) -> str:
        key = "\x1f".join((self.subject, self.object, self.type, self.locator))
        return "edge_" + sha1(key.encode("utf-8")).hexdigest()[:16]

    @property
    def ages(self) -> bool:
        return self.review_after_days is not None

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "subject": self.subject,
            "object": self.object,
            "type": self.type,
            "provenance": self.provenance.value,
            "creator": self.creator.value,
            "locator": self.locator,
            "asserted_at": self.asserted_at.isoformat(),
            "builder_version": self.builder_version,
            "confidence": self.confidence,
            "review_after_days": self.review_after_days,
            "asserted_by": self.asserted_by,
            "superseded_by": self.superseded_by,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Edge":
        return cls(
            id=str(raw.get("id") or ""),
            subject=str(raw["subject"]),
            object=str(raw["object"]),
            type=str(raw["type"]),
            provenance=Provenance(raw["provenance"]),
            creator=Creator(raw["creator"]),
            locator=str(raw["locator"]),
            asserted_at=datetime.fromisoformat(raw["asserted_at"]),
            builder_version=int(raw["builder_version"]),
            confidence=(
                None if raw.get("confidence") is None else float(raw["confidence"])
            ),
            review_after_days=(
                None if raw.get("review_after_days") is None
                else int(raw["review_after_days"])
            ),
            asserted_by=str(raw.get("asserted_by") or ""),
            superseded_by=str(raw.get("superseded_by") or ""),
        )


@dataclass(frozen=True)
class Conflict:
    """Two records that cannot both be right, recorded rather than resolved.

    Invariant 4: contradiction is a record, not a merge. Over months
    unattended nobody is present at the moment two things disagree — if it is
    not written down then, it is gone, and the graph quietly keeps whichever
    one the last pass happened to see.

    A conflict names its subjects and cites the locator of each. It never
    picks a winner: that is the operator's, and until they do, both stand.
    """

    kind: str
    subjects: tuple[str, ...]
    detail: str
    locators: tuple[str, ...]
    detected_at: datetime
    builder_version: int
    id: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        if len(self.subjects) < 2:
            raise ValueError(
                f"conflict {self.kind!r} names {len(self.subjects)} subject(s); "
                "a disagreement needs two sides"
            )
        if not self.detail.strip():
            raise ValueError(f"conflict {self.kind!r} has no readable detail")
        if not self.id:
            key = "\x1f".join((self.kind, *sorted(self.subjects)))
            object.__setattr__(
                self, "id", "conflict_" + sha1(key.encode("utf-8")).hexdigest()[:16]
            )

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "subjects": list(self.subjects),
            "detail": self.detail,
            "locators": list(self.locators),
            "detected_at": self.detected_at.isoformat(),
            "builder_version": self.builder_version,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Conflict":
        return cls(
            id=str(raw.get("id") or ""),
            kind=str(raw["kind"]),
            subjects=tuple(raw["subjects"]),
            detail=str(raw["detail"]),
            locators=tuple(raw.get("locators") or ()),
            detected_at=datetime.fromisoformat(raw["detected_at"]),
            builder_version=int(raw["builder_version"]),
        )


__all__ = [
    "ASSOCIATIVE_LINKS",
    "CAUSAL_LINKS",
    "KIND_LABELS",
    "LINK_LABELS",
    "MAX_TITLE_CHARS",
    "PRECEDENCE",
    "PROVENANCE_LABELS",
    "REGION_LABELS",
    "REGION_OF",
    "Conflict",
    "Creator",
    "Edge",
    "Node",
    "NodeKind",
    "Provenance",
    "Region",
    "UnknownRegion",
    "entity_id",
    "is_causal",
    "label_of",
    "label_of_kind",
    "label_of_link",
    "label_of_provenance",
    "region_of",
    "shorten",
]
