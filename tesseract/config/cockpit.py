"""TARS Cockpit config accessors (``cockpit.yaml``).

Single source of truth for the trio's named lanes (CV-1). The Mirror lane
bridge (``routes/lanes.py``) reads the trio definition from here, so a
model upgrade is a one-line YAML edit rather than a drifting source literal.
"""

from __future__ import annotations

from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from tesseract.paths import CONFIG_DIR

_COCKPIT_YAML = CONFIG_DIR / "cockpit.yaml"


class TrioRelayConfig(BaseModel):
    """Validated trio verify-relay tunables. ``StrictBool`` rejects a quoted
    ``"false"`` (which ``bool(...)`` coerced to True); ``ge=1`` rejects a zero
    or negative round cap. Config is authoritative — malformed values raise a
    ``ValidationError`` rather than silently defaulting (audit M8)."""

    model_config = ConfigDict(extra="forbid")

    max_rounds: int = Field(ge=1)
    verify_by_default: StrictBool


def _resolve_role_model(model_role: str) -> str:
    """Resolve a roles.yaml role name to its concrete catalog model (N1 —
    roles.yaml is the single source of truth). Raises loudly (ConfigError /
    KeyError) on a missing/inactive role — config is authoritative, no silent
    defaults (matches the rest of this module). A resolved model is required by
    the lane-spawn path (`/api/lanes/named/ensure`), so a drift MUST surface,
    not silently omit the model."""
    from tesseract.config.loader import load_config

    bundle = load_config()
    role = bundle.role(model_role)  # raises ConfigError if missing
    if role.primary is None:
        raise KeyError(
            f"trio lane model_role {model_role!r} has no primary in roles.yaml"
        )
    # role.primary is already a ResolvedRef (load_config resolves it).
    return role.primary.model.model


def load_trio_lanes() -> list[dict[str, Any]]:
    """Return the trio's named-lane definitions. Each lane's concrete model is
    derived from its ``model_role`` via roles.yaml (N1 — no hardcoded model id).
    Raises loudly (ConfigError / KeyError / OSError) on a missing or malformed
    cockpit/roles config — no silent defaults."""
    raw = yaml.safe_load(_COCKPIT_YAML.read_text(encoding="utf-8")) or {}
    lanes = list(raw["trio"]["lanes"])
    for lane in lanes:
        role = lane.get("model_role")
        if role and not lane.get("model"):
            lane["model"] = _resolve_role_model(role)
    return lanes


def load_trio_relay() -> dict[str, Any]:
    """Trio verify-relay tunables: ``{"max_rounds": int, "verify_by_default":
    bool}``. Raises loudly (KeyError / OSError) on a missing or malformed
    config — no silent defaults (mirrors load_trio_lanes convention)."""
    raw = yaml.safe_load(_COCKPIT_YAML.read_text(encoding="utf-8")) or {}
    relay = raw["trio"]["relay"]
    validated = TrioRelayConfig.model_validate(relay)
    return validated.model_dump()


def load_conductor_relay() -> tuple[float, float]:
    """(poll_s, timeout_s) for the conductor's send-and-await relay.

    Raises loudly (KeyError / OSError) on a missing or malformed config —
    no silent defaults (mirrors load_trio_lanes convention)."""
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


def trio_lane(name: str) -> dict[str, Any]:
    """Return one trio lane definition by name, or raise KeyError."""
    for lane in load_trio_lanes():
        if lane.get("name") == name:
            return lane
    raise KeyError(f"trio lane {name!r} not defined in cockpit.yaml")
