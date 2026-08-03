"""Pydantic v2 models matching `_shared/lane-contract.md` v1.

Every shape on the wire — `LaneEvent`, `LaneStatus`, `Lane`, `LaneSnapshot`,
`LaneSendResult` — is defined here exactly once. The contract enforces
`assistant_text` and `tool_result` as distinct `LaneEventKind` values —
conflating them, or dropping one in favor of the other, silently erases
tool-call visibility on the wire."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ._common import utc_now_iso


LaneKind = Literal["claude", "codex"]
LaneMode = Literal["headless"]


LaneEventKind = Literal[
    "turn_started",
    "assistant_text",
    "assistant_text_partial",
    "tool_use",
    "tool_result",
    "turn_ended",
    "permission_request",
    "status_change",
    "error",
    "closed",
]


LaneLifecycle = Literal[
    "spawning",
    "ready",
    "busy",
    "idle",
    "closing",
    "closed",
    "error",
]


class LaneEvent(BaseModel):
    """One entry in `events.jsonl`. ``cursor`` is the byte offset of the
    LINE START in the events.jsonl file as observed AT READ TIME — writers
    do not stamp it (the cursor is supplied by `read_events_since`).

    The writer stamps `at_utc` on append; callers should not pre-populate
    it. Payload is kind-specific; consumers narrow on `kind`.

    ``extra="ignore"`` upholds the contract's additive-without-version-bump
    guarantee — newer writers may append fields a Session-A reader doesn't
    know about; we MUST not crash on read."""

    model_config = ConfigDict(extra="ignore")

    lane_id: str
    kind: LaneEventKind
    payload: dict[str, Any] = Field(default_factory=dict)
    at_utc: str = Field(default_factory=utc_now_iso)
    # Cursor is set by the reader, not the writer. Default empty string so
    # `events.jsonl` lines are byte-stable regardless of cursor stamp.
    cursor: str = ""


class LaneStatus(BaseModel):
    """Fast read-only status probe shape returned by `lane.status`."""

    # extra="ignore" (was "forbid"): the IPC proxy reconstructs this from
    # controller wire dicts via model_validate. If the controller ever adds a
    # field the Mirror model doesn't declare (version-skew / split-deploy),
    # forbid would raise ValidationError and degrade the tool to an error.
    # Ignore is forward-compatible — matches Lane/LaneEvent/NamedLaneRecord.
    model_config = ConfigDict(extra="ignore")

    alive: bool
    busy: bool
    queue_depth: int = 0
    last_activity_utc: str = Field(default_factory=utc_now_iso)
    current_turn_id: str | None = None
    end_of_turn_at_utc: str | None = None
    lifecycle: LaneLifecycle = "spawning"


class Lane(BaseModel):
    """The persisted `lane.json` shape. The on-disk record is the
    authoritative description of the lane; the manager reconstructs
    in-memory state from this on `attach` after a brain restart.

    ``extra="ignore"`` — Session B may add fields (mode-specific config,
    e.g. PTY size); a Session-A reader must continue to load older AND
    newer records without raising."""

    model_config = ConfigDict(extra="ignore")

    lane_id: str
    kind: LaneKind
    mode: LaneMode
    model: str
    working_dir: str
    env: dict[str, str] = Field(default_factory=dict)
    opened_at_utc: str = Field(default_factory=utc_now_iso)
    lifecycle: LaneLifecycle = "spawning"
    # Threaded by the headless transport across turns so a re-spawn
    # (Codex per-turn, Claude post-daemon-restart) can `--resume` the
    # on-disk session state instead of starting fresh.
    cli_session_id: str | None = None
    # Opens the CLI with its own read-only sandbox. Persisted rather than
    # derived at spawn time: the adapter is rebuilt from this record after a
    # restart, and a reviewer lane that came back writeable would be a silent
    # audit-boundary failure.
    read_only: bool = False
    closed_at_utc: str | None = None
    close_reason: str | None = None


class LaneSnapshot(BaseModel):
    """`lane.attach` return shape — full state for a re-attaching client."""

    # extra="ignore" (was "forbid") — forward-compat with controller
    # version-skew; see LaneStatus.
    model_config = ConfigDict(extra="ignore")

    lane: Lane
    status: LaneStatus
    recent_events: list[LaneEvent] = Field(default_factory=list)
    next_cursor: str


class LaneSendResult(BaseModel):
    """`lane.send` return shape."""

    # extra="ignore" (was "forbid") — forward-compat with controller
    # version-skew; see LaneStatus.
    model_config = ConfigDict(extra="ignore")

    accepted: bool
    queue_depth: int
    reason: str | None = None


__all__ = [
    "Lane",
    "LaneEvent",
    "LaneEventKind",
    "LaneKind",
    "LaneLifecycle",
    "LaneMode",
    "LaneSendResult",
    "LaneSnapshot",
    "LaneStatus",
]
