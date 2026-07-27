"""Pydantic schema for ``providers.yaml``.

Strict at the top-level provider blocks; permissive at per-model entries
because model dicts genuinely carry heterogeneous fields across
providers (kind, base_url_override, mix, synthesis_presets, ...). The
goal is drift detection on STRUCTURE — adding a new top-level provider
block or renaming a connection key — not on per-model fields where
providers themselves drift weekly.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Permissive(BaseModel):
    model_config = ConfigDict(extra="allow")


class Availability(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_consecutive_failures: int = 3


class ChainPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transient_retries: int = 2
    transient_backoff_ms: int = 250
    cooldown_max_failures: int = 1
    cooldown_seconds: int = 60


class CostTracking(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    warning_at_pct: float = 0.75
    log_file: str = "logs/cost-tracking.jsonl"


class ProviderEntry(_Permissive):
    """A single provider connection block. ``models`` is the structured
    surface that emits MO-10-2 proposals; everything else is connection-
    level glue.
    """

    enabled: bool = True
    models: dict[str, dict[str, Any]] = Field(default_factory=dict)


class TierBlock(_Permissive):
    """A tier block (``api`` / ``cli`` / ``local``). Provider keys map to
    :class:`ProviderEntry` blocks; ``enabled`` is the tier toggle.
    """

    enabled: bool = True


class ProvidersConfig(BaseModel):
    """Top-level shape of ``providers.yaml``.

    Top-level keys are pinned (``extra='forbid'``). Tier and provider
    bodies allow extra keys so the heterogeneous per-model fields land
    cleanly.
    """

    model_config = ConfigDict(extra="forbid")

    availability: Availability = Field(default_factory=Availability)
    chain: ChainPolicy = Field(default_factory=ChainPolicy)
    cost_tracking: CostTracking = Field(default_factory=CostTracking)
    api: dict[str, Any] = Field(default_factory=dict)
    cli: dict[str, Any] = Field(default_factory=dict)
    local: dict[str, Any] = Field(default_factory=dict)
