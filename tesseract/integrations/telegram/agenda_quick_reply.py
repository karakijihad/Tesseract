"""Inbound quick-reply parser for agenda items.

Telegram operators reply ``<agenda_id>:<verb>`` to approve / deny /
snooze an agenda item without opening Mirror. Verbs:

* ``approve`` — fulfil every unfulfilled approval gate. Once all gates
  on an ``awaiting_operator`` item are fulfilled, the route layer
  transitions the item back to ``PROPOSED`` so the kernel admits on the
  next tick.
* ``deny`` / ``cancel`` — transition to ``CANCELLED`` with
  ``reason="operator_telegram_deny"``.
* ``snooze`` — bump ``operator_priority`` down by two steps (clamped to
  the schema's ``-2`` floor). Mirrors the dashboard's SNOOZE button.

The parser is intentionally narrow: it only matches strings that look
exactly like ``ag-YYYY-MM-DD-HHMM-<slug>:<verb>`` (case-insensitive
verb). Any other text falls through to the existing chat path so a
casual operator message that happens to contain a colon stays
conversational.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

log = logging.getLogger(__name__)


QuickReplyVerb = Literal["approve", "deny", "cancel", "snooze"]

#: A verb, in one character, for somewhere with no room for the word.
#: Telegram caps a button's callback data at 64 bytes and an agenda id is up
#: to 59 of them (`mint_agenda_id` truncates the slug at 40), so the verb gets
#: one. Every verb starts with a different letter and this asserts it rather
#: than assuming: two verbs sharing an initial would silently make one of them
#: unreachable from a button.
VERB_LETTERS: dict[str, QuickReplyVerb] = {
    "a": "approve",
    "d": "deny",
    "c": "cancel",
    "s": "snooze",
}


def letter_for(verb: str) -> str:
    """The one character that stands for `verb`, or `""` if none does."""
    for letter, named in VERB_LETTERS.items():
        if named == verb:
            return letter
    return ""

# ``ag-YYYY-MM-DD-HHMM-<slug>`` per ``mint_agenda_id``.
_AGENDA_ID = re.compile(
    r"^(ag-\d{4}-\d{2}-\d{2}-\d{4}-[a-z0-9-]+):(approve|deny|cancel|snooze)$",
    re.IGNORECASE,
)

SNOOZE_PRIORITY_FLOOR = -2
SNOOZE_PRIORITY_DELTA = 2


@dataclass(frozen=True)
class QuickReply:
    agenda_id: str
    verb: QuickReplyVerb


def parse_quick_reply(text: str) -> QuickReply | None:
    """Return a typed reply struct or ``None`` if ``text`` is not a quick reply."""
    if not text:
        return None
    match = _AGENDA_ID.match(text.strip())
    if match is None:
        return None
    return QuickReply(
        agenda_id=match.group(1),
        verb=match.group(2).lower(),  # type: ignore[arg-type]
    )


def looks_like_quick_reply(text: str) -> bool:
    """Cheap pre-check used by the bridge before importing the store."""
    if not text:
        return False
    stripped = text.strip()
    if not stripped.lower().startswith("ag-") or ":" not in stripped:
        return False
    return _AGENDA_ID.match(stripped) is not None


async def _record(reply: QuickReply, *, actor: str, allowed: bool) -> None:
    """Put the decision in `approvals.jsonl`, where every other one is.

    Here rather than at either caller, so a decision made by typing and the
    same decision made by tapping a button land as the same row. Two writers
    would be two answers to "did the operator approve this", and the ledger's
    whole value is that there is one.

    Only approve and deny are recorded. A snooze grants nothing and refuses
    nothing, it moves the item down the queue, and the ledger's `result`
    vocabulary has no word for that; inventing one would make the count of
    approvals stop meaning approvals.
    """
    from tesseract.permissions.approval_log import record_ask

    await record_ask(
        session_id=actor,
        call_id=reply.agenda_id,
        tool_name=f"agenda:{reply.verb}",
        input_summary={"agenda_id": reply.agenda_id, "verb": reply.verb},
        posture_source="agenda_decision",
        result="allow_once" if allowed else "deny",
        actor="operator",
    )


async def apply_quick_reply(
    reply: QuickReply,
    *,
    store: Any,
    actor: str = "telegram_operator",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply ``reply`` against the live :class:`AgendaStore`. Returns a
    structured result the bridge feeds into its operator reply body.

    Imports are local so the parser module stays cheap to import in
    tests that only exercise pattern matching."""
    from tesseract.orchestrator.autonomy.models import AgendaStatus

    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    item = store.get(reply.agenda_id)
    if item is None:
        return {"ok": False, "reason": "not_found", "agenda_id": reply.agenda_id}
    if item.is_terminal():
        return {
            "ok": False,
            "reason": "already_terminal",
            "agenda_id": reply.agenda_id,
            "status": item.status.value,
        }

    if reply.verb == "approve":
        fulfilled = 0
        for gate in item.approvals_required:
            if not gate.fulfilled:
                gate.fulfilled = True
                gate.fulfilled_at = moment
                gate.fulfilled_by = actor
                fulfilled += 1
        item.updated_at = moment
        store.save(item)
        await _record(reply, actor=actor, allowed=True)
        return {
            "ok": True,
            "verb": "approve",
            "agenda_id": reply.agenda_id,
            "fulfilled_count": fulfilled,
            "goal": item.goal,
        }

    if reply.verb in {"deny", "cancel"}:
        store.transition(
            item,
            AgendaStatus.CANCELLED,
            reason="operator_telegram_deny",
            by="operator",
        )
        await _record(reply, actor=actor, allowed=False)
        return {
            "ok": True,
            "verb": reply.verb,
            "agenda_id": reply.agenda_id,
            "status": item.status.value,
            "goal": item.goal,
        }

    if reply.verb == "snooze":
        new_priority = max(
            SNOOZE_PRIORITY_FLOOR, item.operator_priority - SNOOZE_PRIORITY_DELTA
        )
        noop = new_priority == item.operator_priority
        if not noop:
            item.operator_priority = new_priority
            item.updated_at = moment
            store.save(item)
        return {
            "ok": True,
            "verb": "snooze",
            "agenda_id": reply.agenda_id,
            "operator_priority": item.operator_priority,
            "noop": noop,
            "goal": item.goal,
        }

    return {"ok": False, "reason": "unknown_verb", "agenda_id": reply.agenda_id}


def format_reply_body(result: dict[str, Any]) -> str:
    """Telegram-HTML one-liner the bridge sends back so the operator sees
    confirmation in the same thread."""
    if not result.get("ok"):
        reason = result.get("reason", "error")
        ag = result.get("agenda_id", "")
        return f"<b>Quick reply</b> · <code>{ag}</code> · {reason}"
    verb = result.get("verb", "")
    ag = result.get("agenda_id", "")
    goal = str(result.get("goal") or "")
    suffix = ""
    if verb == "approve":
        suffix = " · gates fulfilled"
    elif verb == "snooze":
        suffix = (
            f" · priority {result.get('operator_priority')}"
            + (" (already at floor)" if result.get("noop") else "")
        )
    elif verb in {"deny", "cancel"}:
        suffix = " · cancelled"
    line = f"<b>{verb.title()}</b> · <code>{ag}</code>"
    if goal:
        clipped = goal if len(goal) <= 80 else goal[:79] + "…"
        line += f" · {clipped}"
    return line + suffix


__all__ = [
    "QuickReply",
    "QuickReplyVerb",
    "VERB_LETTERS",
    "letter_for",
    "SNOOZE_PRIORITY_DELTA",
    "SNOOZE_PRIORITY_FLOOR",
    "apply_quick_reply",
    "format_reply_body",
    "looks_like_quick_reply",
    "parse_quick_reply",
]
