"""channel_notify — TARS-initiated outbound text on an external channel.

Distinct from ``channel_send_*`` (which is the in-session reply path):
``channel_notify`` is for TARS to *start* a conversation. The operator may
be away from the keyboard; TARS pings them on Telegram with a finished
thought, a result, an alert, or a check-in. Lands in the operator's
Telegram thread with no inbound message required.

Two modes:

1. ``chat_ref`` provided → send to that specific chat_id only (e.g. a
   group chat TARS already knows).
2. ``chat_ref`` omitted → fan to every operator-tier chat_id in the
   adapter's allowlist (reuses :func:`send_to_operators` from the daily-
   brief push so the tier filter / blocked / pending semantics match).

``default_posture="auto"`` — TARS choosing to ping the operator is part
of the autonomy story (AU-10 will layer rate caps + categories on top).
The tool itself enforces a hard per-call length cap as a guardrail.
"""

from __future__ import annotations

import logging
from typing import Optional

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import (
    PermissionResult,
    Tool,
    ToolContext,
    ToolResult,
)

logger = logging.getLogger(__name__)

# Hard cap matches Telegram's per-message limit; longer payloads are
# chunked by the bridge's send_text but a runaway prompt should fail
# loudly instead of fanning out 30 messages.
MAX_TEXT_CHARS = 4000


class ChannelNotifyInput(BaseModel):
    text: str = Field(
        description=(
            "The message body. Be human — what you'd say if you walked up "
            "and tapped the operator on the shoulder. Plain prose; "
            "Telegram-HTML is also accepted (the bridge handles fallback)."
        ),
    )
    channel: str = Field(
        default="telegram",
        description="Channel slug (telegram today; future WhatsApp/Signal/...).",
    )
    chat_ref: Optional[str] = Field(
        default=None,
        description=(
            "Specific chat to ping (Telegram chat_id as string). Omit to "
            "fan to every operator-tier chat in the adapter allowlist — "
            "this is the standard 'notify the operator' mode."
        ),
    )
    reply_to_message_id: Optional[int] = Field(
        default=None,
        description=(
            "Optional Telegram message_id to quote-reply to. Only used "
            "when ``chat_ref`` is set."
        ),
    )


class ChannelNotifyTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"
    @property
    def name(self) -> str:
        return "channel_notify"

    @property
    def description(self) -> str:
        return (
            "Start a conversation with the operator on an external channel "
            "(Telegram by default). Use when you have something they should "
            "see right now and they may be away from the Mirror. Pass "
            "`chat_ref` to target a specific chat, or omit it to fan to "
            "every operator-tier chat in the allowlist. Be selective — "
            "this is a tap on the shoulder, not a stream."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return ChannelNotifyInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return False

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input if isinstance(tool_input, ChannelNotifyInput)
            else ChannelNotifyInput(**tool_input.model_dump())
        )
        text = (inp.text or "").strip()
        if not text:
            return ToolResult(
                output="channel_notify: `text` is empty",
                is_error=True,
            )
        if len(text) > MAX_TEXT_CHARS:
            return ToolResult(
                output=(
                    f"channel_notify: text is {len(text)} chars, cap is "
                    f"{MAX_TEXT_CHARS}. Trim or use workspace_post for "
                    "long-form notes."
                ),
                is_error=True,
            )

        from tesseract.integrations import get_channel
        adapter = get_channel(inp.channel)
        if adapter is None:
            return ToolResult(
                output=(
                    f"channel_notify: channel '{inp.channel}' is not "
                    "registered (adapter not built or disabled in "
                    "channels.yaml)"
                ),
                is_error=True,
            )

        send_text = getattr(adapter, "send_text", None)
        if send_text is None:
            return ToolResult(
                output=(
                    f"channel_notify: adapter for '{inp.channel}' does "
                    "not expose send_text"
                ),
                is_error=True,
            )

        if inp.chat_ref:
            try:
                await send_text(
                    chat_ref=inp.chat_ref,
                    text=text,
                    reply_to_message_id=inp.reply_to_message_id,
                )
            except Exception as exc:
                logger.exception("channel_notify: send_text failed")
                return ToolResult(
                    output=f"channel_notify failed: {exc}", is_error=True,
                )
            return ToolResult(
                output=f"notified {inp.channel}:{inp.chat_ref} ({len(text)} chars)",
            )

        # Fan to operator-tier allowlist — reuses the daily-brief helper
        # so tier / blocked / pending semantics stay consistent.
        # TelegramBridge keeps allowlist + user_tier on a private _state
        # holder; surface either the top-level attrs (test fakes) or the
        # _state proxies (real bridge) without forcing every adapter
        # implementation to expose them as protocol members.
        try:
            allowlist = getattr(adapter, "allowlist", None)
            user_tier = getattr(adapter, "user_tier", None)
            state = getattr(adapter, "_state", None)
            if allowlist is None and state is not None:
                allowlist = getattr(state, "allowlist", None)
            if user_tier is None and state is not None:
                poll_state = getattr(state, "poll_state", None)
                user_tier = getattr(poll_state, "user_tier", None) if poll_state is not None else None
            if allowlist is None:
                return ToolResult(
                    output=(
                        f"channel_notify: adapter '{inp.channel}' has no "
                        "allowlist; pass chat_ref explicitly"
                    ),
                    is_error=True,
                )
            from tesseract.integrations.telegram.brief_push import send_to_operators
            result = await send_to_operators(
                text,
                bridge=adapter,
                allowlist=allowlist,
                user_tier=user_tier if isinstance(user_tier, dict) else None,
            )
        except Exception as exc:
            logger.exception("channel_notify: fan-out failed")
            return ToolResult(
                output=f"channel_notify failed: {exc}", is_error=True,
            )

        sent = result.get("sent", 0)
        skipped = result.get("skipped", 0)
        errors = result.get("errors", 0)
        return ToolResult(
            output=(
                f"notified {inp.channel} operators: sent={sent} "
                f"skipped={skipped} errors={errors}"
            ),
            metadata=result,
        )


__all__ = ["ChannelNotifyTool", "ChannelNotifyInput", "MAX_TEXT_CHARS"]
