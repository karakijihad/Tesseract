"""Who the runtime's own words go to on a channel.

This was Telegram's to decide until AR-13 section 2, because the one fan-out
helper lived in `integrations/telegram/brief_push.py` and three callers reached
in for it: the notifier in `orchestrator/`, the `channel_notify` kernel tool,
and the brief's own delivery. A rule that was never that adapter's, imported
from inside it by everything that needed it.

The rule is small and says nothing about Telegram: send to the people this
channel calls operators, skip anyone pending or blocked, and let one
recipient's failure decide nothing for the rest. `ChannelAdapter.list_users()`
already reports a tier and a state per person, so the rule is written against
the protocol. A second channel gets it by implementing the protocol, not by
being named here.

What stays behind the adapter boundary is what is genuinely a channel's: its
API, its allowlist file, its message limits, and how it renders a message.

Three properties this module has to hold at once:

1. **The same people as before.** An id that is allowed AND blocked is skipped,
   which the old helper checked explicitly, and an id with no tier recorded
   counts as an operator, which is what the bridge's own projection does.
   Getting either wrong changes who hears from the runtime.
2. **One failure is one failure.** Recipients are sent to together and every
   result is inspected, so one unreachable chat costs the others nothing.
3. **`skipped` is a real count.** The brief's delivery decides whether to try
   its last-resort path on it, so a filtered recipient has to be reported as
   filtered and not as an absence.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Iterable, Sequence

from tesseract.orchestrator.autonomy.message import Message

log = logging.getLogger(__name__)

#: The tier that hears from the runtime unasked. A friend on a channel gets
#: replies to what they said and nothing else.
OPERATOR_TIER = "operator"

SendText = Callable[..., Awaitable[Any]]
Sender = Callable[[Message], Awaitable[dict[str, Any]]]


class PartialDelivery(Exception):
    """Some of a message reached this recipient and the rest did not.

    Raised by an adapter's `send_text`, and channel-neutral for the same
    reason the rest of this module is: a long message is split by whatever
    carries it, so any channel can half deliver one.

    It is an error, because the recipient did not get what was sent. It is
    also not nothing, because they got some of it, and a record that says
    "nothing was sent" is as wrong as one that says the whole thing was.
    `fan_out` counts it in both columns for exactly that reason.
    """

    def __init__(self, message: str, *, delivered: int, total: int) -> None:
        super().__init__(message)
        self.delivered = delivered
        self.total = total


def allowed_refs(users: Iterable[Any]) -> set[str]:
    """Every id this channel currently accepts messages for.

    An id that appears more than once with different states is not one of
    them: `list_users` reports the allowlist, the pending set and the blocked
    set as separate rows, one person can be in two of them, and a person in
    two of them is not settled.

    This is the answer to "may the runtime write to this chat at all", which
    is a different question from "does this chat hear from the runtime
    unasked". A tool addressing one person asks the first; a broadcast asks
    the second, below.
    """
    listed = list(users)
    withheld = {
        str(user.user_id) for user in listed
        if getattr(user, "state", "") != "allowed"
    }
    return {
        str(user.user_id) for user in listed
        if getattr(user, "state", "") == "allowed"
        and str(user.user_id) not in withheld
    }


def operators_of(users: Iterable[Any]) -> list[str]:
    """The operator ids in `users`, in the order the adapter listed them."""
    listed = list(users)
    allowed = allowed_refs(listed)
    out: list[str] = []
    for user in listed:
        ref = str(user.user_id)
        if ref not in allowed or ref in out:
            continue
        if getattr(user, "tier", OPERATOR_TIER) != OPERATOR_TIER:
            continue
        out.append(ref)
    return out


async def fan_out(
    send_text: SendText,
    recipients: Sequence[str],
    text: str,
    *,
    link_preview: bool = False,
) -> dict[str, Any]:
    """Send `text` to each recipient at once, isolating per-recipient failure.

    `link_preview` is off by default. This is the lane the runtime speaks on
    unasked, and Telegram expands the first URL in a message into a card: a
    brief that mentioned a story arrived under a picture of it, chosen by
    whichever link happened to come first. A link the operator asked for goes
    out through the chat lane, which keeps its preview.
    """
    if not text:
        return {"sent": 0, "skipped": 0, "errors": 0, "reason": "empty_text"}
    if not recipients:
        # A reason, like every other way of reaching nobody has. Without one
        # the caller reads an empty result as a successful send and records
        # the channel as having carried the message.
        return {"sent": 0, "skipped": 0, "errors": 0, "reason": "no_operators"}

    async def one(ref: str) -> None:
        await send_text(
            chat_ref=ref, text=text, disable_web_page_preview=not link_preview,
        )

    outcomes = await asyncio.gather(
        *(one(ref) for ref in recipients), return_exceptions=True,
    )
    sent = 0
    errors = 0
    partial = 0
    for ref, outcome in zip(recipients, outcomes):
        if isinstance(outcome, BaseException):
            log.exception("outbound: send failed for %s", ref, exc_info=outcome)
            errors += 1
            if isinstance(outcome, PartialDelivery):
                partial += 1
        else:
            sent += 1
    result: dict[str, Any] = {"sent": sent, "skipped": 0, "errors": errors}
    if partial:
        # Named, not folded into either count. A caller deciding whether to
        # keep a record of what the operator was told needs to know that
        # something reached them, and a caller reporting success needs to know
        # it was not all of it.
        result["partial"] = partial
        result["reason"] = "partial_delivery"
    return result


def reached_anyone(result: Any) -> bool:
    """Did this channel put any of the message in front of a person?

    Three fan-outs ask this: the notifier, the daily brief and the offline
    backstop. All three used to ask it as `if sent:`, and when a fourth
    answer appeared, a recipient who received part of a long message, only
    one of them learned it. The other two went on recording that nothing had
    been sent to an operator holding half of it.

    A whole delivery and a partial one are both "something arrived". Whether
    it was all of it is a different question, and `sent` is still the number
    that answers it.
    """
    if not isinstance(result, dict):
        return False
    return bool(result.get("sent") or result.get("partial"))


def _sender_for(adapter: Any, message: Message, send_text: SendText) -> SendText:
    """`send_text`, or the adapter's own way of sending answers with it.

    Asked for by `getattr`, like `render_message` and for the same reason: a
    channel that cannot draw an answer is a channel that speaks plainly, never
    a boot failure. It keeps `send_text`'s signature so `fan_out` is unchanged
    and one failing recipient still costs the others nothing.

    **A message whose actions could not be drawn is not a message with a
    control missing.** Every action is also a reply spelled out in the text,
    which is why this can fall back at all.
    """
    if not message.actions:
        return send_text
    send_answerable = getattr(adapter, "send_answerable", None)
    if send_answerable is None:
        return send_text

    async def send(**kwargs: Any) -> Any:
        return await send_answerable(actions=message.actions, **kwargs)

    return send


async def notify_operators(
    adapter: Any, message: Message | str, *, link_preview: bool = False,
) -> dict[str, Any]:
    """Fan `message` to every operator on `adapter`, as that channel reads it.

    A plain string is accepted and sent as the whole body, for a caller whose
    words are already finished: `channel_notify` is the assistant writing its
    own sentence, and there is no kind to compose.

    An adapter with no `send_text` is reported rather than raised: whether a
    registered channel can be spoken to is answered here, at the moment
    something is being said.
    """
    from tesseract.integrations._render import render_for

    if isinstance(message, str):
        message = Message(body=message)
    send_text = getattr(adapter, "send_text", None)
    if send_text is None:
        return {
            "sent": 0, "skipped": 0, "errors": 0, "reason": "no_send_text",
        }
    try:
        users = list(adapter.list_users())
    except Exception:
        log.exception("outbound: could not read who is on %s", getattr(adapter, "name", "?"))
        return {"sent": 0, "skipped": 0, "errors": 1, "reason": "no_roster"}

    targets = operators_of(users)
    text = render_for(adapter, message)
    result = await fan_out(
        _sender_for(adapter, message, send_text),
        targets,
        text,
        link_preview=link_preview,
    )
    allowed = sum(1 for user in users if getattr(user, "state", "") == "allowed")
    result["skipped"] = max(0, allowed - len(targets))
    return result


def operator_sender(channel: str) -> Sender:
    """A one-argument sender for `channel`, resolved when it is called.

    Late resolution is what lets the routing table name a channel whose
    adapter is not built yet, or is switched off: the table is the operator's
    statement of intent, and whether anything can carry it is answered at the
    moment of sending.
    """

    async def send(message: Message) -> dict[str, Any]:
        from tesseract.integrations import get_channel

        adapter = get_channel(channel)
        if adapter is None:
            return {"sent": 0, "skipped": 0, "errors": 0, "reason": "no_adapter"}
        return await notify_operators(adapter, message)

    return send


__all__ = [
    "OPERATOR_TIER",
    "PartialDelivery",
    "Sender",
    "allowed_refs",
    "fan_out",
    "notify_operators",
    "operator_sender",
    "operators_of",
    "reached_anyone",
]
