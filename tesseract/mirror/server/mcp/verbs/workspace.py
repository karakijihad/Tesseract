"""workspace.* MCP verbs — the operator-visible thread.

The hub could already write memory and the vault, so a connected CLI's
*conclusions* survived a session. What did not survive was the record of the
work itself: the thread the operator reads is where a handoff is legible, and
until now nothing but the assistant could post to it.

``read`` closes the other half. A client could post and reply into a thread it
could not read back, which made every question it asked unanswerable — the
comment-drain substrate that returns an operator's reply belongs to the assistant's turn
loop, and an MCP caller has no turn for it to land on. ``ask`` is exposed
because ``read`` now exists to be polled: without it the tool's own promise,
"the reply lands on your next turn", is simply false here.

The thread is deliberately unscoped, like memory and the vault: it is the
shared base, not per-client storage. Lane ownership is a different question,
answered on the lane.
"""

from __future__ import annotations

from typing import Any

from tesseract.kernel.tools.ask_clarification import AskClarificationInput
from tesseract.kernel.tools.workspace_post import WorkspacePostInput
from tesseract.kernel.tools.workspace_reply import WorkspaceReplyInput
from tesseract.mirror.server.mcp.verbs._base import (
    MCPVerbError,
    VerbContext,
    make_tool_verb,
)
from tesseract.paths import home_logs_root
from tesseract.workspace_events import EventStore

# Mirrors ``EventStore.list_events``'s own page size. A remote caller asking
# for the whole thread at once is the case worth bounding; a client that wants
# more pages by filtering on kind or status.
_MAX_EVENTS = 200

workspace_post = make_tool_verb("workspace_post", WorkspacePostInput)
workspace_reply = make_tool_verb("workspace_reply", WorkspaceReplyInput)
workspace_ask = make_tool_verb("ask_clarification", AskClarificationInput)


def _event_row(event: Any, comments: list[Any]) -> dict[str, Any]:
    row = event.to_dict()
    row["comments"] = [c.to_dict() for c in comments]
    return row


async def workspace_read(ctx: VerbContext) -> list[dict[str, Any]]:
    """Read the workspace thread.

    With ``event_id`` this is the thread view — one event and every comment on
    it, which is how a caller collects the answer to a question it asked. With
    no ``event_id`` it is the list view: events only, each carrying
    ``comment_count`` so a caller knows which threads are worth fetching rather
    than reading every body to find out."""
    store = EventStore(home_logs_root())
    event_id = str(ctx.params.get("event_id") or "").strip()
    if event_id:
        event = store.get_event(event_id)
        if event is None:
            raise MCPVerbError(404, f"unknown workspace event: {event_id}")
        return [_event_row(event, store.list_comments(event_id))]

    kinds = ctx.params.get("kinds")
    if kinds is not None and not isinstance(kinds, list):
        raise MCPVerbError(400, "workspace.read 'kinds' must be a list of event kinds")
    # Checked for the same reason `kinds` is, and more sharply: a bad `kinds`
    # 400s, but an unchecked `status` compares a str field against whatever was
    # passed and filters every row out. On a surface whose whole purpose is to
    # be polled for an answer, an empty list reads as "not answered yet", so a
    # malformed request would look exactly like a patient one.
    status = ctx.params.get("status") or None
    if status is not None and not isinstance(status, str):
        raise MCPVerbError(400, "workspace.read 'status' must be a string")
    try:
        limit = int(ctx.params.get("limit") or _MAX_EVENTS)
    except (TypeError, ValueError):
        raise MCPVerbError(400, "workspace.read 'limit' must be an integer")
    events = store.list_events(
        kinds=tuple(kinds) if kinds else None,
        status=status,
        limit=max(1, min(limit, _MAX_EVENTS)),
    )
    rows = []
    for event in events:
        row = _event_row(event, [])
        row["comment_count"] = len(store.list_comments(event.event_id))
        rows.append(row)
    return rows


__all__ = ["workspace_post", "workspace_reply", "workspace_read", "workspace_ask"]
