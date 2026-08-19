"""atlas.yaml accessor. Config is authoritative — a missing key raises.

The builder version deliberately does NOT live here. It is the code's
statement about how the derived layer was produced, and a version an operator
can edit is a version that can be pinned below the builder that is actually
running — which is exactly the state invariant 7 exists to prevent.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class ReviewWindows(BaseModel):
    """How long an assertion of each provenance class stands before it wants
    looking at again. `0` means it does not age."""

    model_config = ConfigDict(extra="forbid")

    operator_asserted: int = Field(ge=0)
    stated_in_source: int = Field(ge=0)
    inferred_by_model: int = Field(ge=0)


class Relink(BaseModel):
    """How much of the orphan backlog one pass repairs."""

    model_config = ConfigDict(extra="forbid")

    max_per_run: int = Field(ge=1)


class AtlasConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_after_days: ReviewWindows
    relink: Relink


def load_atlas_config(path: Path | None = None) -> AtlasConfig:
    from tesseract.paths import config_dir

    target = path or (config_dir() / "atlas.yaml")
    raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    return AtlasConfig.model_validate(raw)


__all__ = ["AtlasConfig", "Relink", "ReviewWindows", "load_atlas_config"]
