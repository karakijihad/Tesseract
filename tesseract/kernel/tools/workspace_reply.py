"""workspace_reply — the assistant replies to an operator comment thread.

When the operator leaves a comment on a Workspace event, the comment is
injected on the assistant's next turn as ``[workspace_comment_on_<event_id>]``.
The assistant calls this tool to attach a reply back to the thread; the Mirror
renders it under the operator's comment.

No reasoning fragility: the tool takes ``event_id`` and ``comment_id``
directly so we never have to pattern-match the assistant's prose for which thread
the reply belongs to.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.workspace_events import EventStore, WorkspaceComment

logger = logging.getLogger(__name__)


class WorkspaceReplyInput(BaseModel):
    event_id: str = Field(
        description="The Workspace event id (e.g. evt_a1b2c3d4e5f6).",
    )
    comment_id: str = Field(
        description=(
            "The operator comment id you are replying to (e.g. cmt_…). "
            "Came in via the [workspace_comment_on_…] injection on this turn."
        ),
    )
    body: str = Field(
        description="Your reply to the operator (≤4000 chars).",
    )


class WorkspaceReplyTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"
    def __init__(self, store: EventStore) -> None:
        """Writes the reply comment to disk (durable). Broadcasting is
        the caller's responsibility: `dispatch_workspace_reply` calls
        `broadcast_comment_appended` directly; Mirror-session paths use
        ws.py's `_broadcast_workspace_reply` hook on TOOL_RESULT."""
        self._store = store

    @property
    def name(self) -> str:
        return "workspace_reply"

    @property
    def description(self) -> str:
        return (
            "Reply to an operator comment on a Workspace event. Use this "
            "when you see [workspace_comment_on_<event_id>] in your context "
            "and the operator's comment expects an answer. The reply renders "
            "under the comment in the Workspace Inbox thread."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return WorkspaceReplyInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input
            if isinstance(tool_input, WorkspaceReplyInput)
            else WorkspaceReplyInput(**tool_input.model_dump())
        )

        event_id = (inp.event_id or "").strip()
        comment_id = (inp.comment_id or "").strip()
        body = (inp.body or "").strip()
        if not event_id or not comment_id or not body:
            return ToolResult(
                output="workspace_reply requires `event_id`, `comment_id`, and `body`.",
                is_error=True,
            )

        if self._store.get_event(event_id) is None:
            return ToolResult(
                output=f"Workspace event {event_id} not found.",
                is_error=True,
            )

        reply = WorkspaceComment.new(
            event_id=event_id,
            author="agent",
            body=body,
            reply_to=comment_id,
        )
        try:
            self._store.append_comment(reply)
        except OSError as exc:
            logger.exception("workspace_reply: append failed")
            return ToolResult(
                output=f"workspace_reply failed: {exc}", is_error=True,
            )
        return ToolResult(
            output=f"Reply attached to {event_id} (cmt {reply.comment_id}).",
            metadata={"event_id": event_id, "comment_id": reply.comment_id},
        )
