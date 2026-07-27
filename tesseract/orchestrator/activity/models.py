"""AS-1 — Unified Activity Registry data model.

An ``ActivityRecord`` is a derived, in-memory projection of ONE running
unit of TARS's work — a headless delegate, a named lane, a controller
session, and (AS-3) routines. It is NOT a new persistent
store: the canonical truth lives in each substrate's own on-disk files
(``lane.json``, ``named-lanes/*.json``, ``tars_controller/sessions/*.json``).
The registry indexes them so the Mirror can reflect everything running in
one place — backend is the source of truth, the Mirror only reflects.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel

# The substrate a record projects from. ``mcp_session`` is the kept-open MCP
# client connection (one per connected client) — the "who's in the chair"
# record; action verbs it drives spawn their own kinds parented to it
# (Doclog 2026-06-30 §MCP ActivityKind = mcp_session).
ActivityKind = Literal[
    "delegate", "lane", "controller_session", "routine",
    "autonomy", "mcp_session",
]

# Lifecycle state, normalized across substrates. ``input_required`` (trio
# W4) = running-but-parked on an operator question (ask-instead-of-die);
# non-terminal, projects from `SpawnHandle.input_required`.
ActivityState = Literal[
    "spawning", "running", "idle", "input_required",
    "done", "failed", "cancelled", "closed",
]

# Does the work survive a brain/backend restart? Lanes + controller sessions
# are disk-durable (re-indexed on boot); delegates die with the process.
DurabilityClass = Literal["ephemeral", "persistent"]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ActivityRecord:
    """One running unit of work. Frozen — a state change replaces the record
    (``dataclasses.replace``) so snapshots are trivially consistent."""

    activity_id: str  # stable, kind-scoped: "delegate:<id>" / "lane:<id>" / "session:<id>"
    kind: ActivityKind
    label: str  # human-readable (lane name, tool name, session title)
    state: ActivityState
    durability: DurabilityClass
    provider: str | None = None  # "claude" | "codex" | None
    parent_turn_id: str | None = None  # the chat turn that spawned a delegate
    parent_session_id: str | None = None  # owning controller session, if any
    transcript_ref: str | None = None  # path under TESSERACT_HOME (relative)
    goal: str | None = None  # the intent the unit was launched with (delegate task, …)
    result: str | None = None  # terminal outcome summary, set on a terminal transition
    started_at: str = ""
    updated_at: str = ""


class ActivityRecordOut(BaseModel):
    """Wire model (REST + WS). No ``asyncio.Task`` / runtime handles cross
    the boundary — the registry only ever exposes this projection."""

    activity_id: str
    kind: str
    label: str
    state: str
    durability: str
    provider: str | None = None
    parent_turn_id: str | None = None
    parent_session_id: str | None = None
    transcript_ref: str | None = None
    goal: str | None = None
    result: str | None = None
    started_at: str
    updated_at: str

    @classmethod
    def from_record(cls, r: ActivityRecord) -> "ActivityRecordOut":
        return cls(
            activity_id=r.activity_id,
            kind=r.kind,
            label=r.label,
            state=r.state,
            durability=r.durability,
            provider=r.provider,
            parent_turn_id=r.parent_turn_id,
            parent_session_id=r.parent_session_id,
            transcript_ref=r.transcript_ref,
            goal=r.goal,
            result=r.result,
            started_at=r.started_at,
            updated_at=r.updated_at,
        )


__all__ = [
    "ActivityKind",
    "ActivityState",
    "DurabilityClass",
    "ActivityRecord",
    "ActivityRecordOut",
    "utc_now_iso",
]
