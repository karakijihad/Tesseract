"""chat_initiate — the assistant speaks first in the Mirror chat tab.

A turn normally starts with the operator typing into the chat tab. This
tool inverts that: the assistant pushes an entity-role message envelope to every
open Mirror WS session so the chat tab renders a agent-initiated turn
without an inbound operator message.

Use cases:
- A long-running job the assistant started finished — alert the operator.
- A delegate worker returned with a result worth a one-liner.
- The assistant noticed something in conscience drift and wants to flag it now
  rather than waiting for the next chat turn.

Distinct from ``workspace_post`` (Slack-style inbox feed) and
``channel_notify`` (Telegram outbound). ``chat_initiate`` lights up the
Mirror's chat tab specifically — the place the operator already watches
agent-side prose. AU-10 will layer rate-cap categories on top; for now
this is a primitive without backoff.

``default_posture="auto"`` matches sibling outbound tools.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import (
    PermissionResult,
    Tool,
    ToolContext,
    ToolResult,
)

logger = logging.getLogger(__name__)

MAX_TEXT_CHARS = 4000


class ChatInitiateInput(BaseModel):
    text: str = Field(
        description=(
            "What to say. One short paragraph; long-form belongs in "
            "workspace_post. Markdown-light is fine — the chat tab "
            "renders it like any assistant message."
        ),
    )
    reason: Literal["alert", "nudge", "checkin", "result"] = Field(
        default="nudge",
        description=(
            "Why are you speaking first? alert: something the operator "
            "needs to see; nudge: gentle prompt; checkin: status report; "
            "result: a delegate / scheduled job finished. Surfaces in the "
            "tool transcript so the operator can filter."
        ),
    )


class ChatInitiateTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "being-present"
    summary: ClassVar[str] = "Starts a new chat turn unprompted, without an inbound operator message."
    use_when: ClassVar[str] = (
        "Use for alerts, nudges, check-ins, or delegate/job results the operator should see now. "
        "Be sparing — the chat tab is for live conversation, not a feed."
    )
    not_when: ClassVar[str] = (
        "replying to what the operator just said, which needs no tool; a durable inbox item, "
        "`workspace_post`; a push to an external chat channel, `channel_notify`."
    )

    def __init__(self, app_provider: Optional[Callable[[], Any]] = None) -> None:
        """``app_provider`` resolves the Mirror ``web.Application`` at call
        time (closure pattern; matches scheduler_provider). REPL / unit
        tests pass ``None`` → the tool refuses with a clear error rather
        than crashing.
        """
        self._app_provider = app_provider

    @property
    def name(self) -> str:
        return "chat_initiate"

    @property
    def input_schema(self) -> type[BaseModel]:
        return ChatInitiateInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return False

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input if isinstance(tool_input, ChatInitiateInput)
            else ChatInitiateInput(**tool_input.model_dump())
        )
        text = (inp.text or "").strip()
        if not text:
            return ToolResult(output="chat_initiate: `text` is empty", is_error=True)
        if len(text) > MAX_TEXT_CHARS:
            return ToolResult(
                output=(
                    f"chat_initiate: text is {len(text)} chars, cap is "
                    f"{MAX_TEXT_CHARS}. Trim or use workspace_post."
                ),
                is_error=True,
            )

        app = self._app_provider() if self._app_provider is not None else None
        if app is None:
            return ToolResult(
                output=(
                    "chat_initiate: no Mirror app context (running headless "
                    "or test). Use workspace_post or channel_notify instead."
                ),
                is_error=True,
            )

        sessions = app.get("server_sessions") or {} if hasattr(app, "get") else {}
        if not sessions:
            return ToolResult(
                output=(
                    "chat_initiate: no Mirror chat tab open. Nothing to "
                    "push to — try channel_notify (Telegram) or "
                    "workspace_post (durable feed)."
                ),
                is_error=True,
            )

        # Late import — same fail-soft pattern as workspace_events.broadcast.
        try:
            from tesseract.mirror.server.envelope import make_envelope
            from tesseract.mirror.server.session import send_envelope
        except Exception:
            logger.exception("chat_initiate: mirror helpers import failed")
            return ToolResult(
                output="chat_initiate: Mirror envelope helpers unavailable",
                is_error=True,
            )

        payload = {"text": text, "reason": inp.reason}
        sent = 0
        for sess in list(sessions.values()):
            env = make_envelope(
                "chat_assistant_initiated",
                "entity",
                getattr(sess, "session_id", ""),
                payload,
            )
            try:
                await send_envelope(sess, env)
                sent += 1
            except Exception:
                logger.exception(
                    "chat_initiate: send_envelope failed for %s",
                    getattr(sess, "session_id", "?"),
                )

        return ToolResult(
            output=f"chat_initiate pushed to {sent} session(s) (reason={inp.reason})",
            metadata={"sessions": sent, "reason": inp.reason},
        )


__all__ = ["ChatInitiateTool", "ChatInitiateInput", "MAX_TEXT_CHARS"]
