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


class Query(BaseModel):
    """How wide a question casts before the walk begins.

    Both fields carry defaults, unlike the rest of this file. `review_after_days`
    and `relink` decide what the nightly builder writes, so a missing one is a
    fault worth raising on; these two only decide how broad one answer is. An
    operator who deleted the block keeps it deleted (`migrate_config_keys` will
    not put back what they removed), and taking their boot down over the width
    of a search result is not a trade this makes.
    """

    model_config = ConfigDict(extra="forbid")

    max_content_seeds: int = Field(default=10, ge=1)
    min_score_ratio: float = Field(default=0.5, ge=0.0, le=1.0)


class Body(BaseModel):
    """How far back the graph draws what the machine did.

    One dial, deliberately. The run log grows for as long as the machine runs,
    so something has to bound it, and a window is what a person means by
    recently. A second dial capping the row count would be the one actually
    doing the bounding while this one appeared to, which is a shape this repo
    has already paid for once.

    Carries a default for the reason :class:`Query` gives: it decides how much
    of the body is drawn rather than what is stored, and taking an operator's
    boot down over that is not a trade this makes.
    """

    model_config = ConfigDict(extra="forbid")

    runs_within_days: int = Field(default=30, ge=1)


class Surface(BaseModel):
    """How much of the graph the picture draws at once.

    One ceiling. The library grows without limit and a canvas does not, so
    something has to bound the picture, and the honest bound is a number the
    surface can print beside what it drew. It decides what is DRAWN and never
    what is stored, so it carries a default for the reason :class:`Query` and
    :class:`Body` do.
    """

    model_config = ConfigDict(extra="forbid")

    max_nodes_drawn: int = Field(default=600, ge=1)


class AtlasConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_after_days: ReviewWindows
    relink: Relink
    query: Query = Query()
    body: Body = Body()
    surface: Surface = Surface()


def load_atlas_config(path: Path | None = None) -> AtlasConfig:
    from tesseract.paths import config_dir

    target = path or (config_dir() / "atlas.yaml")
    raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    return AtlasConfig.model_validate(raw)


__all__ = [
    "AtlasConfig",
    "Body",
    "Query",
    "Relink",
    "ReviewWindows",
    "Surface",
    "load_atlas_config",
]
