"""MCP verb handlers.

``CALL_VERBS`` maps verb names to their async handlers; the MCP protocol layer
exposes each as a ``tools/call`` tool (``tools.py``). ``activity.watch`` is NOT
here — it names the server→client subscription, served on the GET SSE stream
(``mcp/stream.py``) and gated by the same posture lookup as any verb. It stays
out of the callable-tool set because a subscription has no request/response
shape to call.
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
from tesseract.mirror.server.mcp.verbs.diary import diary_append, feedback_propose
from tesseract.mirror.server.mcp.verbs.memory import (
    memory_forget,
    memory_get,
    memory_promote,
    memory_recall,
    memory_save,
    memory_search,
    memory_update,
)
from tesseract.mirror.server.mcp.verbs.schedule import (
    schedule_create,
    schedule_list,
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
from tesseract.mirror.server.mcp.verbs.workspace import (
    workspace_ask,
    workspace_post,
    workspace_read,
    workspace_reply,
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
    "schedule.list": schedule_list,
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
    # P4 — the shared base. Whoever does the work, the store, the vault and
    # the operator-visible thread all see it. Curation and read-back, not just
    # the write side the earlier phases covered.
    "workspace.post": workspace_post,
    "workspace.reply": workspace_reply,
    # The thread is only shared if it reads both ways: a handoff nobody can
    # read back is a handoff into a log, and a question asked into a thread
    # the asker cannot poll is unanswerable.
    "workspace.read": workspace_read,
    "workspace.ask": workspace_ask,
    "memory.recall": memory_recall,
    "memory.get": memory_get,
    "memory.promote": memory_promote,
    "memory.forget": memory_forget,
    "diary.append": diary_append,
    "feedback.propose": feedback_propose,
}

# The streaming verb — a subscription on the GET SSE stream, not a callable tool.
STREAM_VERB = "activity.watch"

__all__ = [
    "CALL_VERBS",
    "STREAM_VERB",
    "VerbContext",
    "MCPVerbError",
    "MCPPermissionDenied",
]
