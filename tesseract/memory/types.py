"""Memory types and frontmatter schema.

Defines the 7 memory types, stability states, and the Pydantic model
for YAML frontmatter that lives at the top of every memory file.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


#: Last-resort guard on `MemoryFrontmatter.summary`, not a style rule.
#:
#: A summary ends where its first paragraph ends, so its length follows the
#: thought rather than a number. This ceiling exists only because a body can be
#: one unbroken blob, and `list_all` parses every frontmatter on every
#: retrieval; a summary that is a whole document would be paid for on each one.
SUMMARY_HARD_CEILING = 1_000


def lead_paragraph(text: str) -> str:
    """The memory's own opening paragraph, collapsed to one line.

    The natural end of a summary, and why the ceiling above rarely applies. A
    writer who puts the fact first gets the fact; the rest of the body stays in
    the file, one `memory_get` away.
    """
    # A leading markdown heading is the record's title again, not its content,
    # so a body that opens with one would summarise to its own name.
    blocks = [b for b in (text or "").strip().split("\n\n") if b.strip()]
    for block in blocks:
        without_heading = "\n".join(
            line for line in block.splitlines() if not line.lstrip().startswith("#")
        ).strip()
        if without_heading:
            return summarize(without_heading, SUMMARY_HARD_CEILING)
    return ""


def summarize(text: str, limit: int) -> str:
    """One line, at most `limit` chars, cut on a word boundary and marked.

    Every caller that shortens a summary goes through here, so a shortened
    summary always looks shortened. Without the mark a cut sentence is
    indistinguishable from a rule that genuinely ends there.
    """
    one_line = " ".join(text.split())
    if len(one_line) <= limit:
        return one_line
    # The mark is part of the budget: a caller that asked for `limit` chars
    # gets at most `limit`, so a render can be laid out against a fixed width.
    if limit <= 1:
        return "…"[:limit]
    head = one_line[: limit - 1]
    cut = head.rfind(" ")
    if cut > 0:
        head = head[:cut]
    return head.rstrip(" ,;:.-") + "…"


class MemoryType(str, Enum):
    USER = "user"
    FEEDBACK = "feedback"
    PROJECT = "project"
    REFERENCE = "reference"
    # Runtime self-observation: conscience drift events, drift reflections,
    # and any other heartbeat-authored notes about the assistant's own behavior.
    # Distinct from PROJECT so operator-curated project notes don't blur
    # with auto-written runtime telemetry.
    CONSCIENCE = "conscience"


class Stability(str, Enum):
    ACTIVE = "active"
    STABLE = "stable"
    ARCHIVED = "archived"


class SourceTier(str, Enum):
    """How far a record stands from material nobody in this runtime wrote.

    Ranked, and the order is the whole point: ``SOURCE`` outranks ``DERIVED``
    so a model's paraphrase of a paper cannot answer in place of the paper.

    The invariants this carries, and every later change is checked against
    ALL of them rather than against whichever one prompted it:

    1. Closed. A record's tier is one of three; an unrecognised
       ``source_type`` resolves to ``DERIVED``, the direction that cannot
       promote model output by accident.
    2. Never inferred from text. The tier comes from the declared table
       below, keyed on what the writer already says it is writing.
    3. Ranked in retrieval as a weight, not applied as a filter. A `derived`
       record still answers when nothing better exists; it just never ties
       with its own source.
    4. Cheap to read. `list_frontmatter` parses every record on every query,
       so the tier is resolved once at parse and never recomputed.
    5. Degrades. A record whose tier cannot be resolved is still readable and
       still retrievable, because a record that vanishes from retrieval is a
       worse failure than one that ranks low.

    ``RECALLED`` is not below ``DERIVED`` on any measurement. It is placed
    there because the work-history block already renders below promoted
    memory and this preserves that ordering rather than inventing one. OT-8
    is where it gets a number.
    """

    #: Operator-supplied or externally fetched. Nothing in this runtime wrote
    #: it, so nothing in this runtime can regenerate it, so it is never deleted.
    SOURCE = "source"
    #: Written by a model from something else already in the store.
    DERIVED = "derived"
    #: A conversation transcript or work-history artifact. Nobody rewrote it,
    #: and nobody authored it either.
    RECALLED = "recalled"


#: The one place a `source_type` becomes a tier.
#:
#: Every writer of `source_type` in the tree is named here. A value absent
#: from this table resolves to `DERIVED` — a new writer that forgot to
#: declare itself is far more likely to be a pass over the store than a new
#: kind of raw material, and that is the direction that fails safe.
_TIER_BY_SOURCE_TYPE: dict[str, SourceTier] = {
    # Model-written passes over records that already existed.
    "consolidation": SourceTier.DERIVED,
    "daily_brief": SourceTier.DERIVED,
    # First-hand records of something that happened. The runtime observing
    # its own drift is not summarising a document; it is the only account of
    # that transition there will ever be.
    "conscience_heartbeat": SourceTier.SOURCE,
    # Same class, and no longer written by anything: it is in the live store
    # from a job that has since gone. A table built only from the writers
    # alive in the code today would have let the fallback demote 13 first-hand
    # observations to `derived`, which is why the migration reports an
    # unrecognised value rather than quietly resolving it.
    "autonomy_heartbeat": SourceTier.SOURCE,
    # A decision recorded with the plan file it was taken against. First-hand,
    # not a summary of the plan.
    "research": SourceTier.SOURCE,
    # `memory_save` / `memory_update`'s operator-facing vocabulary.
    "chat": SourceTier.SOURCE,
    "upload": SourceTier.SOURCE,
    "paper": SourceTier.SOURCE,
    "article": SourceTier.SOURCE,
    "data": SourceTier.SOURCE,
    "snapshot": SourceTier.SOURCE,
    "imagination": SourceTier.SOURCE,
    "observation": SourceTier.SOURCE,
    # Unset. Operator-written records predate the field entirely and the
    # store is mostly these; treating them as derived would demote the
    # operator's own facts to make room for nothing.
    "": SourceTier.SOURCE,
}

#: `capture/reflect.py` stamps the conversation's own source, which is
#: `mirror` or `channel:<name>`. Both are transcripts.
_RECALLED_SOURCE_PREFIXES = ("mirror", "channel:")

#: `vault_ingest` and `vault_raw_watch` stamp the file's own extension. Raw
#: bytes the operator put in the vault, which is the one store this runtime
#: never rewrites.
#:
#: Spelled out rather than imported from `vault_manager.EXTENSION_MAP`, which
#: imports this module. A test asserts the two stay in step, so a newly
#: ingestable file type cannot quietly become `derived` by being forgotten
#: here.
_VAULT_SUFFIXES: frozenset[str] = frozenset(
    {
        "pdf", "md", "txt", "csv", "json", "tsv", "xlsx", "docx", "pptx",
        "png", "jpg", "jpeg", "gif", "svg", "mp3", "wav", "mp4",
    }
)


def tier_for(source_type: str) -> SourceTier:
    """The tier a `source_type` resolves to. The only derivation of it."""
    value = (source_type or "").strip()
    if value in _TIER_BY_SOURCE_TYPE:
        return _TIER_BY_SOURCE_TYPE[value]
    if value.startswith(_RECALLED_SOURCE_PREFIXES):
        return SourceTier.RECALLED
    if value in _VAULT_SUFFIXES:
        return SourceTier.SOURCE
    return SourceTier.DERIVED


class MemoryFrontmatter(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    type: MemoryType
    title: str
    summary: str = ""
    created_at: datetime
    updated_at: datetime | None = None
    importance: int = Field(default=5, ge=1, le=10)
    tags: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    auto_links: list[str] = Field(default_factory=list)
    source_session: str = ""
    source_path: str = ""
    source_url: str = ""
    source_type: str = ""
    stability: Stability = Stability.ACTIVE

    # How far this record stands from material nobody here wrote, and what it
    # was written from. Absent on every record written before the field
    # existed, so it resolves from `source_type` at parse time and is stamped
    # on the next write — a lazy migration rather than a pass over the store,
    # because the file on disk is the truth and rewriting the store to add a
    # field nobody has read yet is a large edit for no reading.
    source_tier: SourceTier | None = None
    # Locators, never prose: memory ids or store-relative paths. The atlas
    # refuses an edge without one and this is the same rule a level down.
    derived_from: list[str] = Field(default_factory=list)
    # One more than the deepest thing in `derived_from`. Stamped by
    # `MemoryStore.write`, which is the only place that can see the sources,
    # so a caller cannot declare itself shallow.
    derivation_depth: int = Field(default=0, ge=0)

    # Belief-state fields (spec.md §1, §"Memory record shape", 2026-04-29).
    # `slug` is the canonical exact-match key for decisions (e.g. "voice_default").
    # Empty when the memory is not a slug-keyed decision. Must be unique across
    # the store when set (enforced by memory_save).
    slug: str = ""
    # Confidence the operator (or save path) attaches to the fact, 0.0-1.0.
    # Default 1.0 preserves prior behavior for existing memories on read.
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    # Optional review/expiry. When in the past, retrieval drops the memory
    # from the prefilter (soft-delete; the file stays on disk for audit).
    expiry_at: datetime | None = None
    # When the conversation this memory was learned from was deleted. Stamped
    # by the record's own owner at the moment of the delete — `chat_store
    # .delete_chat` reaches the recap by a derived id — never by a sweep
    # guessing which memories have lost their source. The memory is untouched
    # otherwise: what was learned outlives the conversation that taught it,
    # and this only records that there is no transcript left to re-read.
    source_deleted_at: datetime | None = None

    @field_validator("id")
    @classmethod
    def id_must_start_with_mem(cls, v: str) -> str:
        if not v.startswith("mem_"):
            raise ValueError("Memory ID must start with 'mem_'")
        return v

    @field_validator("created_at", "updated_at", "expiry_at", "source_deleted_at")
    @classmethod
    def _tz_aware(cls, v: datetime | None) -> datetime | None:
        # Naive timestamps (offset-less ISO strings in older/foreign
        # frontmatter) crashed every aware-datetime compare downstream —
        # retrieval stage-A `now - updated_at` 500'd memory.search over MCP
        # and the librarian's recency window (trio W0 audit D6, 2026-07-09).
        # Normalize at the parse boundary: naive is interpreted as UTC.
        if v is not None and v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v

    @field_validator("source_tier", mode="before")
    @classmethod
    def _tier_degrades(cls, v):
        """An unreadable tier ranks low; it never hides the record.

        `list_frontmatter` catches a parse error by skipping the file, so a
        raise here would delete a memory from retrieval on a typo. Ranking it
        as `derived` is the safe direction and keeps it answerable.
        """
        if v is None or isinstance(v, SourceTier):
            return v
        try:
            return SourceTier(str(v))
        except ValueError:
            return SourceTier.DERIVED

    @model_validator(mode="after")
    def _resolve_tier(self) -> MemoryFrontmatter:
        if self.source_tier is None:
            object.__setattr__(self, "source_tier", tier_for(self.source_type))
        return self

    @field_validator("slug")
    @classmethod
    def slug_format(cls, v: str) -> str:
        if not v:
            return v
        if not re.match(r"^[a-z0-9][a-z0-9_]*$", v):
            raise ValueError(
                "slug must be lowercase letters/digits/underscore, starting with letter/digit"
            )
        return v

    def to_yaml_dict(self) -> dict:
        d = self.model_dump()
        d["type"] = self.type.value
        d["stability"] = self.stability.value
        d["created_at"] = self.created_at.isoformat()
        if self.updated_at:
            d["updated_at"] = self.updated_at.isoformat()
        else:
            d.pop("updated_at", None)
        if self.expiry_at:
            d["expiry_at"] = self.expiry_at.isoformat()
        else:
            d.pop("expiry_at", None)
        # Dropped when unset rather than written as a null, so the field
        # appears only on the memories it is true of — every other record
        # round-trips byte-identical.
        if self.source_deleted_at:
            d["source_deleted_at"] = self.source_deleted_at.isoformat()
        else:
            d.pop("source_deleted_at", None)
        for field in ("source_path", "source_url", "source_type", "slug"):
            if not d.get(field):
                d.pop(field, None)
        # The tier is always written once a record passes through here: it is
        # the fact retrieval ranks on, and leaving it implicit would mean
        # re-deriving it from `source_type` on every parse forever. This is
        # the lazy migration, and it is why a record written before the field
        # existed comes back with one extra line.
        d["source_tier"] = (self.source_tier or tier_for(self.source_type)).value
        if not d.get("derived_from"):
            d.pop("derived_from", None)
        if not d.get("derivation_depth"):
            d.pop("derivation_depth", None)
        # Confidence defaults to 1.0; only persist when a non-default value is
        # set so older memories round-trip unchanged.
        if d.get("confidence") == 1.0:
            d.pop("confidence", None)
        return d

    @classmethod
    def from_yaml_dict(cls, d: dict) -> MemoryFrontmatter:
        return cls(**d)

    @staticmethod
    def generate_id() -> str:
        return f"mem_{secrets.token_hex(4)}"


@dataclass(frozen=True)
class RetrievalPacket:
    """Return type for retrieve(). Wraps results + optional synthesis.

    ``work_history`` carries non-authoritative
    session + workshop chunks when the caller passed
    ``include_work_history=True``. These are NEVER folded into
    ``results`` (which is reserved for promoted memory). Formatters
    render them under a separate, trust-labeled block so the operator
    and the model can tell the difference at a glance.
    """

    results: list  # list[RetrievalResult] — avoids circular import
    synthesis: str | None = None
    confidence: float = 0.0
    stages_run: tuple[str, ...] = ()
    d_contributions: int = 0
    daily_notes: str = ""
    # When stage 0 returns an exact slug match, the rest of the pipeline is
    # short-circuited. Surfaced so the caller (memory_search tool) can flag
    # the high-trust hit explicitly.
    short_circuited: bool = False
    # Non-authoritative chunks merged in when the caller asks
    # for work-history. Each entry is a `WorkHit` (avoids circular import
    # via `list`).
    work_history: list = field(default_factory=list)
