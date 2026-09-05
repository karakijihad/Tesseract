"""channel_notify — agent-initiated outbound text on an external channel.

Distinct from ``channel_send_*`` (which is the in-session reply path):
``channel_notify`` is for the assistant to *start* a conversation. The operator may
be away from the keyboard; the assistant pings them on Telegram with a finished
thought, a result, an alert, or a check-in. Lands in the operator's
Telegram thread with no inbound message required.

Two modes:

1. ``chat_ref`` provided → send to that specific chat_id only (e.g. a
   group chat the assistant already knows).
2. ``chat_ref`` omitted → fan to everyone the adapter calls an operator
   (:func:`notify_operators`, the one shared rule, so the tier filter and
   the blocked / pending semantics are the same ones the runtime's own
   notifications use).

``default_posture="auto"`` — the assistant choosing to ping the operator is
part of the autonomy story. The tool itself enforces a hard per-call length cap
as a guardrail.

**What this tool is NOT under.** It does not go through `OutboundNotifier`, so
the operator's mute list and the per-hour cap do not reach it: both are keyed
by notification category, and a sentence the assistant chose to write has none.
The docstring here used to say caps and categories "sit in the outbound
notifier", which read as though they applied. What bounds this tool today is
the roster, the length cap, and the assistant's own judgement about when a tap
on the shoulder is warranted. Whether it should also answer to a category the
operator can mute is an open question in `phase-AR-13`.
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
            "The message body. Be human: what you would say if you walked up "
            "and tapped the operator on the shoulder. Plain prose, and light "
            "markdown if you want emphasis. Each channel renders it its own "
            "way, so do not write a channel's markup yourself."
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
            "fan to every operator-tier chat in the adapter allowlist. "
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


def _as_message(text: str):
    """The assistant's own sentence, as a message a channel can render.

    Two things it settles, and both branches of the tool need them.

    **It renders.** `send_text` takes text a caller has already marked up, so
    handing it raw meant `**bold**` arrived as asterisks when addressed to one
    chat and as bold when sent to all of them: one sentence reading two ways
    depending on which argument was passed.

    **It is a payload, not a ping.** A renderer holds an ordinary body to 512
    characters, which is right for "one thing happened, go and look" and wrong
    for the thing itself. This tool accepts 4000 and the fan-out branch was
    silently delivering the first 512 of them, with the rest unrecoverable.
    The body IS what the operator asked to be told, so nothing here cuts it and
    the channel splits it into as many messages as it takes.
    """
    from tesseract.orchestrator.autonomy.message import Message

    return Message(body=text, payload=True)


class ChannelNotifyTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "reaching-the-operator"
    summary: ClassVar[str] = "Start a conversation with the operator on an external channel."
    use_when: ClassVar[str] = (
        "Use when the operator may be away from the Mirror and you have something "
        "they should see now: a finished thought, a result, an alert, a check-in. "
        "Pass `chat_ref` to target a specific chat, or omit it to fan to every "
        "operator-tier chat in the allowlist. Be selective, because this is a tap on the "
        "shoulder, not a stream."
    )
    not_when: ClassVar[str] = (
        "the operator is already in the Mirror. Answer them there with an ordinary "
        "reply or a surface card, not a channel message."
    )
    depends_on: ClassVar[str] = ""

    @property
    def name(self) -> str:
        return "channel_notify"

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
        # Measured the way the delivery layer measures it. `len` counts code
        # points and Telegram counts utf-16 code units, so 4000 emoji passed a
        # cap named after Telegram's limit and arrived as 8000 across three
        # messages. Same defect the chunker carried, one layer up.
        from tesseract.integrations.telegram.chunker import text_width

        width = text_width(text)
        if width > MAX_TEXT_CHARS:
            return ToolResult(
                output=(
                    f"channel_notify: text is {width} characters as the "
                    f"channel counts them, cap is {MAX_TEXT_CHARS}. Trim or "
                    "use workspace_post for long-form notes."
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
            # The roster is the authority on both branches, and it is the SAME
            # rule on both: this tool is the runtime speaking unasked, and
            # `_outbound.OPERATOR_TIER` already says who hears that. A friend
            # on a channel gets replies to what they said and nothing else.
            #
            # Addressing one chat used to skip the roster entirely, so any id
            # that reached this tool was written to whether the channel had
            # ever accepted it or not, at a posture that asks nobody first.
            # Checking only that the chat was ACCEPTED closed that but left the
            # tier behind, which let an unasked message reach a friend.
            #
            # Fails closed: a roster that cannot be read is not one that said
            # yes.
            from tesseract.integrations._outbound import operators_of

            roster = getattr(adapter, "list_users", None)
            try:
                known = set(operators_of(roster())) if roster is not None else set()
            except Exception:
                logger.exception("channel_notify: could not read who is on %s", inp.channel)
                known = set()
            if str(inp.chat_ref) not in known:
                return ToolResult(
                    output=(
                        f"channel_notify: chat {inp.chat_ref} is not one of "
                        f"'{inp.channel}'s operators, so nothing was sent. "
                        "The runtime speaks unasked only to them. Leave "
                        "`chat_ref` out to reach all of them, and reply to a "
                        "friend in the turn they wrote to you in."
                    ),
                    is_error=True,
                )
            from tesseract.integrations._render import render_for

            try:
                await send_text(
                    chat_ref=inp.chat_ref,
                    text=render_for(adapter, _as_message(text)),
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

        # Fan to everyone the adapter calls an operator. This used to reach
        # into `integrations/telegram/` for the rule and dig the allowlist out
        # of a private `_state` holder to feed it, which is a kernel tool
        # knowing the shape of one channel's insides. Both are the adapter's
        # own business now, behind `list_users()`.
        from tesseract.integrations._outbound import notify_operators

        try:
            result = await notify_operators(adapter, _as_message(text))
        except Exception as exc:
            logger.exception("channel_notify: fan-out failed")
            return ToolResult(
                output=f"channel_notify failed: {exc}", is_error=True,
            )
        if result.get("reason") in {"no_send_text", "no_roster"}:
            return ToolResult(
                output=(
                    f"channel_notify: adapter '{inp.channel}' cannot say who "
                    "its operators are; pass chat_ref explicitly"
                ),
                is_error=True,
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
