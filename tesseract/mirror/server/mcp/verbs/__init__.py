"""MCP verb handlers.

``CALL_VERBS`` maps verb names to their async handlers; the MCP protocol layer
exposes each as a ``tools/call`` tool (``tools.py``). ``activity.watch`` is NOT
here — it names the (currently deferred) server→client streaming surface, kept
out of the callable-tool set.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from tesseract.mirror.server.mcp.verbs._base import (
    MCPPermissionDenied,
    MCPVerbError,
    VerbContext,
)
from tesseract.mirror.server.mcp.verbs.activity import activity_cancel, activity_list
from tesseract.mirror.server.mcp.verbs.agent import (
    agent_assign,
    agent_review,
    agent_status,
)
from tesseract.mirror.server.mcp.verbs.budget import (
    budget_pause_source,
    budget_set_cap,
    budget_status,
)
from tesseract.mirror.server.mcp.verbs.lane import (
    lane_close,
    lane_ensure,
    lane_read,
    lane_send,
    lane_turn,
)
from tesseract.mirror.server.mcp.verbs.memory import (
    memory_save,
    memory_search,
    memory_update,
)
from tesseract.mirror.server.mcp.verbs.schedule import (
    schedule_create,
    schedule_remove,
    schedule_run,
    schedule_update,
)
from tesseract.mirror.server.mcp.verbs.surface import (
    surface_close,
    surface_open,
    surface_focus,
    surface_spawn,
    surface_update,
)
from tesseract.mirror.server.mcp.verbs.vault import (
    vault_ingest,
    vault_query,
    vault_search,
)

CallHandler = Callable[[VerbContext], Awaitable[Any]]

CALL_VERBS: dict[str, CallHandler] = {
    # P2 — read-only
    "activity.list": activity_list,
    # P3 s3 — cancel (ASK)
    "activity.cancel": activity_cancel,
    "memory.search": memory_search,
    "vault.search": vault_search,
    "vault.query": vault_query,
    # P3 s1 — write (kernel-tool-backed, ASK floor)
    "memory.save": memory_save,
    "memory.update": memory_update,
    "vault.ingest": vault_ingest,
    # P3 s2 — lane / schedule / surface (kernel-tool-backed)
    "lane.ensure": lane_ensure,
    "lane.send": lane_send,
    "lane.turn": lane_turn,
    "lane.read": lane_read,
    "lane.close": lane_close,
    "schedule.create": schedule_create,
    "schedule.update": schedule_update,
    "schedule.run": schedule_run,
    "schedule.remove": schedule_remove,
    "surface.open": surface_open,
    "surface.spawn": surface_spawn,
    "surface.update": surface_update,
    "surface.focus": surface_focus,
    "surface.close": surface_close,
    # P3 s3 — budget (non-tool; direct CostLedger)
    "budget.status": budget_status,
    "budget.set_cap": budget_set_cap,
    "budget.pause_source": budget_pause_source,
    # P3 s3 — agent (assign via controller session; status/review read the registry)
    "agent.assign": agent_assign,
    "agent.status": agent_status,
    "agent.review": agent_review,
}

# The streaming verb — not a callable tool (server→client push is deferred).
STREAM_VERB = "activity.watch"

__all__ = [
    "CALL_VERBS",
    "STREAM_VERB",
    "VerbContext",
    "MCPVerbError",
    "MCPPermissionDenied",
]
