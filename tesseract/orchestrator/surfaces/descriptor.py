"""Surface descriptor — the canonical JSON contract (v1).

Mirrors ``_shared/surface-protocol.md §Descriptor schema``. The frontend
``canvas/protocol/types.ts`` is the TypeScript twin; keep the two in sync.

Schema versioning is forward-incompatible by design: a descriptor whose
``schema_version`` is not 1 is rejected with a clear error. Additive changes
(new optional fields, new ``type`` values, new verbs) do NOT bump the
version — only breaking changes do, and those require a v2 with v1
back-compat in the renderers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = 1

SurfaceMode = Literal["embedded", "external", "canvas", "background"]


class SurfacePosition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float
    y: float


class SurfaceSize(BaseModel):
    model_config = ConfigDict(extra="forbid")
    w: float
    h: float


class BoundSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    id: str


class SurfaceDescriptor(BaseModel):
    """A single canvas surface. Position/size/z/locked are canvas-owned;
    ``props`` is renderer-owned payload (file root, url, code text, …)."""

    # ``extra="ignore"`` so a persisted file written by a later additive
    # build (new optional fields) still loads on an older binary.
    model_config = ConfigDict(extra="ignore")

    schema_version: int = SCHEMA_VERSION
    id: str
    type: str
    view: str
    position: SurfacePosition
    size: SurfaceSize
    title: str | None = None
    mode: SurfaceMode = "embedded"
    z: int = 0
    locked: bool = False
    props: dict[str, Any] = Field(default_factory=dict)
    bound_session: BoundSession | None = None
    created_at_utc: str
    updated_at_utc: str

    @field_validator("schema_version")
    @classmethod
    def _guard_version(cls, v: int) -> int:
        if v != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported surface schema_version {v!r}: this build speaks "
                f"v{SCHEMA_VERSION} only (forward-incompatible by design)"
            )
        return v


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
