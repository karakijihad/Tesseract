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


class Reranker(BaseModel):
    model_config = ConfigDict(extra="forbid")
    primary: str


class RoleBody(_Permissive):
    """A single cognition role. Everything not named here (overrides,
    budgets, notes) flows through as extras.

    A role names its chain one of two ways: `primary` (+ optional
    `fallbacks`) written out, or `chain` naming an entry in the top-level
    ``chains:`` block. Exactly one — the loader refuses both, because a
    role that says two different things about the same slot has no
    answer, only a precedence rule nobody would remember.
    """

    mode: str = "active"
    primary: str | None = None
    fallbacks: list[str] = Field(default_factory=list)
    chain: str | None = None
    notes: str | None = None


class VoiceLane(_Permissive):
    mode: str = "active"
    primary: str
    fallbacks: list[str] = Field(default_factory=list)
    settings: dict[str, Any] = Field(default_factory=dict)


class VoiceBlock(_Permissive):
    stt: VoiceLane | None = None
    tts: VoiceLane | None = None


class RolesConfig(BaseModel):
    """Top-level shape of ``roles.yaml``."""

    model_config = ConfigDict(extra="forbid")

    embeddings: Embeddings
    reranker: Reranker | None = None
    chains: dict[str, list[str]] = Field(default_factory=dict)
    roles: dict[str, RoleBody] = Field(default_factory=dict)
    voice: VoiceBlock | None = None
