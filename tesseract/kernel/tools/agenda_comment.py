"""agenda_comment — TARS replies to an operator comment on an agenda item.

When an operator leaves a comment on an agenda item's thread, the
controller is dispatched with a directive to answer via this tool
rather than returning prose. Mirrors ``workspace_reply``: the write is
durable the moment this tool returns; the backend
(``agenda_reply.dispatch_agenda_reply``) then detects the newly-written
comment and broadcasts it for live UI update. No reasoning fragility —
the tool takes ``item_id`` directly so we never pattern-match TARS's
prose for which item the reply belongs to.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.orchestrator.autonomy.agenda_comments import append_comment
from tesseract.orchestrator.autonomy.agenda_store import AgendaStore

logger = logging.getLogger(__name__)

_AGENT_BY = "tars"


class AgendaCommentInput(BaseModel):
    item_id: str = Field(
        description="The agenda item id you are replying to (e.g. agd_…).",
    )
    body: str = Field(
        description="Your reply to the operator (≤4000 chars).",
    )


class AgendaCommentTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    def __init__(self, store: AgendaStore) -> None:
        """Writes the reply comment to disk (durable). Broadcasting is the
        caller's responsibility: `dispatch_agenda_reply` calls
        `broadcast_agenda_comment_event` directly after detecting the
        newly-written comment."""
        self._store = store

    @property
    def name(self) -> str:
        return "agenda_comment"

    @property
    def description(self) -> str:
        return (
            "Reply to an operator comment on an agenda item. Use this when "
            "directed to answer a comment thread on an agenda item — the "
            "reply renders in the item's comment thread, not the chat panel."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return AgendaCommentInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input
            if isinstance(tool_input, AgendaCommentInput)
            else AgendaCommentInput(**tool_input.model_dump())
        )

        item_id = (inp.item_id or "").strip()
        body = (inp.body or "").strip()
        if not item_id or not body:
            return ToolResult(
                output="agenda_comment requires `item_id` and `body`.",
                is_error=True,
            )

        if self._store.get(item_id) is None:
            return ToolResult(
                output=f"Agenda item {item_id} not found.",
                is_error=True,
            )

        try:
            comment = append_comment(item_id, role="agent", by=_AGENT_BY, body=body)
        except (OSError, ValueError) as exc:
            logger.exception("agenda_comment: append failed")
            return ToolResult(
                output=f"agenda_comment failed: {exc}", is_error=True,
            )
        return ToolResult(
            output=f"Reply attached to {item_id} (comment {comment.id}).",
            metadata={"item_id": item_id, "comment_id": comment.id},
        )
