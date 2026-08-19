"""workspace_post — the assistant posts a self-initiated note into the Workspace.

Used when the assistant wants to surface something to the operator without
blocking the chat: an observation worth noting, a decision it made
autonomously, an heads-up. Lands in the Workspace Inbox as a
``agent_post`` event.

The Layer B/C scheduler jobs do NOT use this tool — they write events
directly via ``EventStore.append_event`` from job code. This tool is the
LLM-side surface so the assistant can post during a normal turn.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, ClassVar, Literal, Optional

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.workspace_events import EventStore, WorkspaceEvent
from tesseract.workspace_events.broadcast import broadcast_workspace_event

logger = logging.getLogger(__name__)


class WorkspacePostInput(BaseModel):
    title: str = Field(
        description="One-line headline (≤200 chars). What is this note about?",
    )
    summary: str = Field(
        description=(
            "1-3 sentences (≤1200 chars) — the body the operator reads. "
            "Be specific. 'Noticed memory_search returns slower since "
            "yesterday's reindex' beats 'memory thing is slow'."
        ),
    )
    priority: int = Field(
        default=5, ge=1, le=10,
        description=(
            "1-10. 5 = default. ≥8 only when the operator should look "
            "before continuing. Don't inflate."
        ),
    )
    kind: Literal["agent_post", "nudge"] = Field(
        default="agent_post",
        description=(
            "agent_post: 'leaving a note' (default). "
            "nudge: 'please look at this' — operator-attention request."
        ),
    )


class WorkspacePostTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "asking-without-blocking"
    summary: ClassVar[str] = "Open a new, self-initiated note in the operator's Workspace Inbox."
    use_when: ClassVar[str] = (
        "Use to surface something without interrupting the chat thread. Replies "
        "land as `[workspace_comment_on_<event_id>]` on a later turn."
    )
    not_when: ClassVar[str] = (
        "a reply inside a thread that already exists, use `workspace_reply`; an "
        "ambient or scheduler-job signal, routed through the autonomy bus instead."
    )

    def __init__(
        self,
        store: EventStore,
        *,
        app_provider: Optional[Callable[[], Any]] = None,
    ) -> None:
        """``app_provider`` resolves the Mirror app at call time so the
        post triggers a live WS broadcast to every open Workspace tab.
        Unset (REPL / tests) → write-only; the next inbox fetch still
        picks the event up from disk."""
        self._store = store
        self._app_provider = app_provider

    @property
    def name(self) -> str:
        return "workspace_post"

    @property
    def input_schema(self) -> type[BaseModel]:
        return WorkspacePostInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input
            if isinstance(tool_input, WorkspacePostInput)
            else WorkspacePostInput(**tool_input.model_dump())
        )

        title = (inp.title or "").strip()
        summary = (inp.summary or "").strip()
        if not title or not summary:
            return ToolResult(
                output="workspace_post requires both `title` and `summary`.",
                is_error=True,
            )

        event = WorkspaceEvent.new(
            kind=inp.kind,
            source="agent",
            title=title,
            summary=summary,
            payload={"session_id": context.session_id},
            priority=inp.priority,
        )
        try:
            self._store.append_event(event)
        except OSError as exc:
            logger.exception("workspace_post: append failed")
            return ToolResult(
                output=f"workspace_post failed: {exc}", is_error=True,
            )
        # Live broadcast so open Mirror Workspace tabs re-render without
        # waiting for the next manual refresh. Fail-soft — broadcast.py
        # never raises; a failure here must not lose the on-disk event.
        if self._app_provider is not None:
            app = self._app_provider()
            if app is not None:
                await broadcast_workspace_event(app, event)
        return ToolResult(
            output=f"Posted to Workspace as {event.event_id}.",
            metadata={"event_id": event.event_id},
        )
