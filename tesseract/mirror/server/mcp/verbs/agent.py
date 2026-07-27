"""agent.* MCP verbs (P3).

``assign`` (ASK) hands a task to a fresh controller session via the
``start_controller_session`` kernel tool (returns the session_id). ``status``
(AUTO) and ``review`` (AUTO) read that session's on-disk record / transcript
directly from the SessionRegistry — no IPC, no kernel tool.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from tesseract.kernel.tools.start_controller_session import StartControllerSessionInput
from tesseract.mirror.server.mcp.verbs._base import (
    MCPVerbError,
    VerbContext,
    make_tool_verb,
)
from tesseract.orchestrator.tars_controller.sessions import SessionRegistry

# agent.assign → the start_controller_session kernel tool (fire-and-forget
# dispatch; the session_id it returns is the handle for status/review).
agent_assign = make_tool_verb("start_controller_session", StartControllerSessionInput)

# Cap the transcript replay so a long-running session can't return an
# unbounded body; the newest events are the useful ones.
_MAX_REVIEW_EVENTS = 500


def _require_session(ctx: VerbContext, verb: str):
    session_id = str(ctx.params.get("session_id") or "").strip()
    if not session_id:
        raise MCPVerbError(400, f"{verb} requires 'session_id'")
    try:
        record = SessionRegistry().get_session(session_id)
    except (ValueError, OSError, ValidationError) as exc:
        # ValidationError (Pydantic v2, NOT a ValueError) fires on a corrupt /
        # partially-written record → 400, not an unhandled 500.
        raise MCPVerbError(400, f"invalid session_id: {exc}")
    if record is None:
        raise MCPVerbError(404, f"unknown session: {session_id}")
    return session_id, record


async def agent_status(ctx: VerbContext) -> dict[str, Any]:
    _session_id, record = _require_session(ctx, "agent.status")
    return record.model_dump()


async def agent_review(ctx: VerbContext) -> dict[str, Any]:
    session_id, record = _require_session(ctx, "agent.review")
    path = Path(record.transcript_path)
    if not path.exists():
        return {"session_id": session_id, "event_count": 0, "truncated": False, "events": []}
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    tail = lines[-_MAX_REVIEW_EVENTS:]
    events: list[Any] = []
    for line in tail:
        try:
            events.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            events.append({"_raw": line})
    return {
        "session_id": session_id,
        "event_count": len(lines),
        "truncated": len(lines) > len(tail),
        "events": events,
    }


__all__ = ["agent_assign", "agent_status", "agent_review"]
