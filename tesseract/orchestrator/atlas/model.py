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
from dataclasses import dataclass, field
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

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("a node needs an id")
        if not self.locator.strip():
            raise ValueError(f"node {self.id!r} has no locator")

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
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
        return cls(
            id=str(raw["id"]),
            kind=NodeKind(raw["kind"]),
            title=str(raw.get("title") or ""),
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
    "PRECEDENCE",
    "Conflict",
    "Creator",
    "Edge",
    "Node",
    "NodeKind",
    "Provenance",
    "entity_id",
]
