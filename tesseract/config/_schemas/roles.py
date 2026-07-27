"""Pydantic schema for ``roles.yaml``.

Strict at the top-level role/voice/embeddings blocks; permissive at the
per-role body because roles carry diverse override knobs
(``compact_threshold``, ``reasoning_effort_override``,
``daily_budget_usd``, etc).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Permissive(BaseModel):
    model_config = ConfigDict(extra="allow")


class Embeddings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    primary: str


class RoleBody(_Permissive):
    """A single cognition role. ``primary`` is required; ``fallbacks`` is
    optional. Everything else (overrides, budgets, notes) flows through
    as extras.
    """

    mode: str = "active"
    primary: str | None = None
    fallbacks: list[str] = Field(default_factory=list)
    notes: str | None = None


class VoiceLane(_Permissive):
    mode: str = "active"
    primary: str
    fallbacks: list[str] = Field(default_factory=list)
    settings: dict[str, Any] = Field(default_factory=dict)


class VoiceBlock(_Permissive):
    default_voice_id: str | None = None
    default_tone_prompt: str | None = None
    stt: VoiceLane | None = None
    tts: VoiceLane | None = None


class RolesConfig(BaseModel):
    """Top-level shape of ``roles.yaml``."""

    model_config = ConfigDict(extra="forbid")

    embeddings: Embeddings
    roles: dict[str, RoleBody] = Field(default_factory=dict)
    voice: VoiceBlock | None = None
