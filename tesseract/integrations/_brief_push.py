"""Pushing the daily brief to whichever channels it is routed to.

This lived in `integrations/telegram/brief_push.py`, which made the brief's
delivery a Telegram feature: finding the payload, deciding it was worth
sending, and knowing who to send it to were all inside one adapter, and a
second channel would have needed its own copy of all three.

Only one of those is a channel's: how the brief READS on it. The brief is
composed into a `Message` like every other kind the runtime sends, and the
channel it is going to answers `render_message`, or gets plain sentences. One
hook, and it is the same hook a notification uses.

Where it goes is `config/routing.yaml::routes.daily_brief`, the one table every
outbound kind is routed by. The brief had its own switch under
`channels.yaml::telegram.brief_push` until AR-13 section 3.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Callable

from tesseract.orchestrator.autonomy.message import Message

log = logging.getLogger(__name__)

BRIEF_KIND = "daily_brief"


class BriefPushSubscriber:
    """Listens for ``daily_brief_ready`` and pushes the summary out.

    ``adapters`` maps a channel name to the thing that sends it, defaulting
    to the channel registry.
    """

    def __init__(
        self,
        *,
        event_store: Any,
        adapters: dict[str, Any] | None = None,
        routing_getter: Callable[[], Any | None] | None = None,
    ) -> None:
        self._event_store = event_store
        self._adapters = dict(adapters or {})
        self._routing_getter = routing_getter

    def _destinations(self) -> tuple[str, ...]:
        routing = self._routing_getter() if self._routing_getter else None
        if routing is None:
            from tesseract.orchestrator.autonomy.outbound_routing import (
                load_outbound_routing,
            )

            try:
                routing = load_outbound_routing()
            except Exception:
                log.exception("brief_push: could not read where the brief is routed")
                return ()
        return tuple(routing.destinations(BRIEF_KIND))

    def _adapter_for(self, channel: str) -> Any | None:
        override = self._adapters.get(channel)
        if override is not None:
            return override
        from tesseract.integrations import get_channel

        return get_channel(channel)

    def _brief_payload(self, date: str | None = None) -> dict[str, Any] | None:
        """The payload for `date`, or the newest one when no date is asked for.

        The date matters. An operator who renders an older day through
        `/api/brief/refresh` makes that the newest `daily_brief` event, and a
        lookup that only ever took the newest would then push last Tuesday's
        brief to the phone on the next delivery, including the nightly one,
        which asks for a specific date and used to be ignored.
        """
        if self._event_store is None:
            return None
        try:
            events = self._event_store.list_events(kinds=(BRIEF_KIND,), limit=10)
        except Exception:
            log.exception("brief_push: list_events failed")
            return None
        wanted = (date or "").strip()
        for ev in events:
            payload = ev.payload or {}
            if not isinstance(payload, dict) or not payload.get("sections"):
                continue
            if wanted and str(payload.get("date") or "").strip() != wanted:
                continue
            return payload
        return None

    async def _push_to(self, channel: str, message: Message) -> dict[str, Any]:
        adapter = self._adapter_for(channel)
        if adapter is None:
            return {"sent": 0, "skipped": 0, "errors": 0, "reason": "no_adapter"}
        from tesseract.integrations._outbound import notify_operators

        return await notify_operators(adapter, message)

    async def handle(self, date: str | None = None) -> dict[str, Any]:
        targets = self._destinations()
        if not targets:
            return {"sent": 0, "skipped": 0, "errors": 0, "reason": "disabled"}
        payload = self._brief_payload(date)
        if payload is None:
            return {"sent": 0, "skipped": 0, "errors": 0, "reason": "no_payload"}
        message = compose_brief(payload)
        if not (message.sections or message.bullets):
            return {"sent": 0, "skipped": 0, "errors": 0, "reason": "empty_text"}

        from tesseract.integrations._outbound import reached_anyone

        outcomes = await asyncio.gather(
            *(self._push_to(channel, message) for channel in targets),
            return_exceptions=True,
        )
        sent = 0
        skipped = 0
        errors = 0
        reasons: list[str] = []
        delivered: list[str] = []
        for channel, outcome in zip(targets, outcomes):
            if isinstance(outcome, BaseException):
                log.exception(
                    "brief_push: sending to %s raised", channel, exc_info=outcome,
                )
                errors += 1
                reasons.append("send_failed")
                continue
            sent += int(outcome.get("sent") or 0)
            skipped += int(outcome.get("skipped") or 0)
            errors += int(outcome.get("errors") or 0)
            # `reached_anyone`, not `sent`, because a brief that arrived in
            # part is a brief the operator has some of. Asking it as `if sent`
            # here meant the panel said nothing had been sent to someone
            # reading half of it.
            if reached_anyone(outcome):
                delivered.append(channel)
            reason = str(outcome.get("reason") or "")
            if reason and reason not in reasons:
                reasons.append(reason)
        if delivered:
            # The panel's record of what the runtime last said. This path
            # fans out itself rather than going through `OutboundNotifier`,
            # which is the only other caller, so the brief reached the
            # operator every morning and the Channels room said nothing had
            # been sent.
            from tesseract.orchestrator.autonomy.message import render_plain
            from tesseract.orchestrator.autonomy.outbound import record_sent

            record_sent("daily_brief", render_plain(message), delivered)
        result: dict[str, Any] = {"sent": sent, "skipped": skipped, "errors": errors}
        if reasons:
            result["reason"] = ", ".join(reasons)
        return result




# -- What the brief says, chosen for a phone -------------------------------
#
# The workspace ``daily_brief`` event carries the whole brief. This picks the
# part worth waking someone for: the opening sentences of each prose section
# and the first few vault entries, saying how many it left behind.
#
# All of that is CONTENT, so none of it is a channel's. It used to return
# Telegram-HTML from inside `integrations/telegram/`, which meant a second
# channel needed its own copy of the CHOOSING as well as its own markup.


# Each prose section gets its opening sentences and no character clip. A
# section too long to fit is a section with too much in it, which is the
# renderer's problem to fix rather than this one's to hide.
SECTION_SENTENCES = 2
MAX_VAULT_LINES = 5

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

#: The prose sections, in reading order, and what each is called.
SECTIONS: tuple[tuple[str, str], ...] = (
    ("Yesterday in TESSERACT", "yesterday_in_tesseract"),
    ("With you", "yesterday_with_you"),
    ("What I learned", "what_i_learned"),
)


def _first_sentences(text: str, *, count: int = SECTION_SENTENCES) -> str:
    """The first ``count`` whole sentences, whitespace collapsed."""
    if not text:
        return ""
    raw = " ".join(str(text).strip().split())
    if not raw:
        return ""
    parts = _SENTENCE_SPLIT_RE.split(raw)
    if count and len(parts) > count:
        return " ".join(parts[:count]).strip()
    return raw


def _vault_lines(entries: Any) -> list[str]:
    """One line per vault entry, up to ``MAX_VAULT_LINES``.

    It used to be the first entry alone, so a night that filed four pages
    reported one and said nothing about the rest.
    """
    if not isinstance(entries, list):
        return []
    out: list[str] = []
    for entry in entries:
        if len(out) >= MAX_VAULT_LINES:
            break
        if isinstance(entry, dict):
            line = str(entry.get("title") or entry.get("body") or "").strip()
        elif isinstance(entry, str):
            line = entry.strip()
        else:
            line = ""
        if line:
            out.append(" ".join(line.split()))
    return out


def compose_brief(workspace_payload: dict[str, Any] | None) -> Message:
    """The brief as a message, or an empty one when there is nothing to say.

    An empty section drops, label and body both. Nothing beyond the title
    drops the title too, so the operator never gets a payload-shaped "empty
    brief" ping.
    """
    if not isinstance(workspace_payload, dict):
        return Message()

    sections = workspace_payload.get("sections") or {}
    if not isinstance(sections, dict) or not sections:
        return Message()

    date = str(workspace_payload.get("date") or "").strip()
    blocks: list[tuple[str, str]] = []
    for label, key in SECTIONS:
        body = _first_sentences(str(sections.get(key) or ""))
        if body:
            blocks.append((label, body))

    vault_all = sections.get("vault")
    vault = _vault_lines(vault_all)
    held_back = (len(vault_all) if isinstance(vault_all, list) else 0) - len(vault)
    if held_back > 0:
        # Say what was left out. A list that stops at five and does not
        # mention it reads as a night that filed five pages.
        vault = [*vault, f"and {held_back} more, in the brief"]

    if not blocks and not vault:
        return Message()

    return Message(
        title=f"TESSERACT {date}".strip(),
        sections=tuple(blocks),
        bullets=tuple(vault),
        bullets_label="Vault" if vault else "",
    )


__all__ = [
    "BRIEF_KIND",
    "MAX_VAULT_LINES",
    "SECTIONS",
    "SECTION_SENTENCES",
    "BriefPushSubscriber",
    "compose_brief",
]
