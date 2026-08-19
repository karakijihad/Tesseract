"""ask_clarification — agent asks the operator a question via the workspace.

AU-19. Posts a ``clarification`` workspace event so the agent can solicit
operator input asynchronously without blocking the current turn. The
operator answers in the event's comment thread; the existing comment-
delivery substrate (``_start_workspace_turn`` / undelivered-comment drain)
surfaces the reply back to the agent on its next turn.

Linkage:
- ``expires_at`` is informational — operator can mark the event
  resolved/rejected/deleted at any time, or let it sit. A sweeper that
  auto-closes stale rows is intentionally out of scope for AU-19.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, ClassVar, Literal, Optional

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.workspace_events import EventStore, WorkspaceEvent
from tesseract.workspace_events.broadcast import broadcast_workspace_event

logger = logging.getLogger(__name__)


_URGENCY_PRIORITY = {
    "low": 3,
    "normal": 5,
    "high": 8,
}


class AskClarificationInput(BaseModel):
    question: str = Field(
        description=(
            "The question to ask the operator (≤1200 chars). Be specific — "
            "'Should I use Tavily or Brave for this search?' beats 'what do you think?'"
        ),
    )
    context: str = Field(
        default="",
        description=(
            "Optional short background (≤2000 chars) explaining what you "
            "are working on and why the question matters. Helps the "
            "operator answer without reconstructing your state."
        ),
    )
    urgency: Literal["low", "normal", "high"] = Field(
        default="normal",
        description=(
            "Maps to priority 3/5/8. Use 'high' only when you cannot make "
            "progress without an answer."
        ),
    )
    expires_in_hours: int = Field(
        default=24,
        ge=1,
        le=168,
        description=(
            "How long the question stays relevant. Stored in payload; "
            "after this window the operator's answer may no longer help. "
            "Max 7 days."
        ),
    )


class AskClarificationTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "asking-without-blocking"
    summary: ClassVar[str] = "Post a question to the operator that can wait for an answer."
    use_when: ClassVar[str] = (
        "Use when you need operator input but can keep making progress on other "
        "work while it waits. The answer lands on a later turn."
    )
    not_when: ClassVar[str] = (
        "the operator is present right now, just ask in the reply you are "
        "already writing; a reply on an existing thread, use `workspace_reply`."
    )

    def __init__(
        self,
        store: EventStore,
        *,
        app_provider: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._store = store
        self._app_provider = app_provider

    @property
    def name(self) -> str:
        return "ask_clarification"

    @property
    def input_schema(self) -> type[BaseModel]:
        return AskClarificationInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: AskClarificationInput = tool_input  # type: ignore[assignment]
        question = inp.question.strip()
        if not question:
            return ToolResult(
                output="ask_clarification requires a non-empty question",
                is_error=True,
            )
        priority = _URGENCY_PRIORITY[inp.urgency]
        expires_at = (
            datetime.now(timezone.utc) + timedelta(hours=inp.expires_in_hours)
        ).isoformat()
        payload: dict[str, Any] = {
            "question": question,
            "context": inp.context.strip(),
            "urgency": inp.urgency,
            "expires_at": expires_at,
            "session_id": context.session_id,
        }
        # One-line preview for the title; full text lives in payload + summary.
        title = question.splitlines()[0][:200]
        summary = question[:1200]
        event = WorkspaceEvent.new(
            kind="clarification",
            source="agent",
            title=title,
            summary=summary,
            payload=payload,
            priority=priority,
            author_id="agent",
            author_display="Agent",
        )
        try:
            self._store.append_event(event)
        except OSError as exc:
            logger.exception("ask_clarification: append failed")
            return ToolResult(
                output=f"ask_clarification failed: {exc}", is_error=True,
            )
        if self._app_provider is not None:
            app = self._app_provider()
            if app is not None:
                await broadcast_workspace_event(app, event)
        return ToolResult(
            output=(
                f"Posted clarification {event.event_id} (urgency={inp.urgency}, "
                f"expires {expires_at})."
            ),
            metadata={
                "event_id": event.event_id,
                "expires_at": expires_at,
                "urgency": inp.urgency,
            },
        )
