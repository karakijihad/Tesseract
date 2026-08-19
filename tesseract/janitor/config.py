"""janitor.yaml accessor. Config is authoritative — missing or malformed
keys raise (KeyError / ValidationError), no silent defaults.

`archive_retention_days` and `log_prune` used to live here; both are rows in
`config/retention.yaml` now. `extra="forbid"` means an install still carrying
them fails to load rather than reading them and doing nothing — which is what
a silently-ignored retention window would be.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from tesseract.paths import CONFIG_DIR

_JANITOR_YAML = CONFIG_DIR / "janitor.yaml"


class Fingerprint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    pattern: str  # regex searched against the full command line


class JanitorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    process_fingerprints: list[Fingerprint]
    scratch_dir_globs: list[str]
    stale_session_grace_hours: int = Field(ge=1)
    claimed_heartbeat_max_age_s: int = Field(ge=1)


def load_janitor_config(path: Path | None = None) -> JanitorConfig:
    raw = yaml.safe_load((path or _JANITOR_YAML).read_text(encoding="utf-8"))
    return JanitorConfig.model_validate(raw)
