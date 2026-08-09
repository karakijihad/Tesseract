"""the assistant Cockpit config accessors (``cockpit.yaml``).

UI-facing configuration plus the tier-0 verification gate's tunables. The
standing coder/auditor seating that used to live here is gone: delegation
picks its worker through ``roles.yaml`` per call, so there is no seating to
keep in a second place.
"""

from __future__ import annotations

from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from tesseract.paths import CONFIG_DIR

_COCKPIT_YAML = CONFIG_DIR / "cockpit.yaml"


class VerifyConfig(BaseModel):
    """Validated tier-0 verification-gate tunables.

    ``step_timeout_s`` is bounded at 600 because that is ``BashInput.timeout``'s
    own ceiling — a larger value here would be silently rejected by pydantic
    one layer down, at gate-run time rather than at config load.
    """

    model_config = ConfigDict(extra="forbid")

    step_timeout_s: float = Field(gt=0, le=600)
    output_head_lines: int = Field(ge=0)
    output_tail_lines: int = Field(ge=0)
    # Backstop after the line-based elision: head/tail count lines, so one
    # multi-megabyte line survives them whole.
    output_max_chars: int = Field(gt=0)


def load_verify_config() -> VerifyConfig:
    """Tier-0 verification-gate tunables from ``verify``.

    Raises loudly (KeyError / OSError / ValidationError) on a missing or
    malformed config — no silent defaults. The gate would otherwise pick its own
    output cap, and an uncapped test log is precisely the payload it exists to
    bound.
    """
    raw = yaml.safe_load(_COCKPIT_YAML.read_text(encoding="utf-8")) or {}
    return VerifyConfig.model_validate(raw["verify"])


def load_conductor_relay() -> tuple[float, float]:
    """(poll_s, timeout_s) for the conductor's send-and-await relay.

    Raises loudly (KeyError / OSError) on a missing or malformed config —
    no silent defaults."""
    raw = yaml.safe_load(_COCKPIT_YAML.read_text(encoding="utf-8")) or {}
    relay = raw["conductor"]
    return float(relay["relay_poll_s"]), float(relay["relay_timeout_s"])


def load_lane_ack_timeout_s() -> float:
    """IPC accept-ack ceiling for lane.* verbs (`ControllerClient._lane_call`).

    Bounds only the daemon's "queued" acknowledgment — never the turn
    itself, which is awaited via the `turn_ended` event stream. Raises
    loudly (KeyError / OSError) on a missing or malformed config — no
    silent defaults (mirrors load_conductor_relay convention)."""
    raw = yaml.safe_load(_COCKPIT_YAML.read_text(encoding="utf-8")) or {}
    return float(raw["conductor"]["lane_ack_timeout_s"])


def load_conductor_reply_cap() -> int:
    """Max chars for a `lane_turn` reply before truncation.

    Raises loudly (KeyError / OSError) on a missing or malformed config —
    no silent defaults (mirrors load_conductor_relay convention)."""
    raw = yaml.safe_load(_COCKPIT_YAML.read_text(encoding="utf-8")) or {}
    relay = raw["conductor"]
    return int(relay["reply_cap_chars"])


def load_activity_rebuild_window_hours() -> float:
    """Recency window for re-registering controller sessions at boot-time
    activity rebuild.

    Raises loudly (KeyError / OSError) on a missing or malformed config —
    no silent defaults (mirrors load_conductor_relay convention)."""
    raw = yaml.safe_load(_COCKPIT_YAML.read_text(encoding="utf-8")) or {}
    return float(raw["activity"]["rebuild_session_window_hours"])
