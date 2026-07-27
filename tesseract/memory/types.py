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

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MemoryType(str, Enum):
    USER = "user"
    FEEDBACK = "feedback"
    PROJECT = "project"
    REFERENCE = "reference"
    # Runtime self-observation: conscience drift events, drift reflections,
    # and any other heartbeat-authored notes about TARS's own behavior.
    # Distinct from PROJECT so operator-curated project notes don't blur
    # with auto-written runtime telemetry.
    CONSCIENCE = "conscience"


class Stability(str, Enum):
    ACTIVE = "active"
    STABLE = "stable"
    ARCHIVED = "archived"


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

    @field_validator("id")
    @classmethod
    def id_must_start_with_mem(cls, v: str) -> str:
        if not v.startswith("mem_"):
            raise ValueError("Memory ID must start with 'mem_'")
        return v

    @field_validator("created_at", "updated_at", "expiry_at")
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
        for field in ("source_path", "source_url", "source_type", "slug"):
            if not d.get(field):
                d.pop(field, None)
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

    CR-1 follow-up (M3): ``work_history`` carries non-authoritative
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
    # CR-1 M3 — non-authoritative chunks merged in when the caller asks
    # for work-history. Each entry is a `WorkHit` (avoids circular import
    # via `list`).
    work_history: list = field(default_factory=list)
