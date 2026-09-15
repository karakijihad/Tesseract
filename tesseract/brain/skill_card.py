"""File the `skill_approval` card a drafted skill is proposed on, and settle it.

`skill_door.py` re-exports both names.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from tesseract.workspace_events import EventStore, WorkspaceEvent
from tesseract.workspace_events.broadcast import broadcast_workspace_event

logger = logging.getLogger(__name__)


async def file_card(
    event_store: Optional[EventStore],
    app_provider: Optional[Callable[[], Any]],
    name: str,
    draft: Any,
    rendered: str,
    origin: str,
    *,
    card_title: Optional[str],
    card_summary: Optional[str],
    card_extra: Optional[dict[str, Any]],
    required: bool,
) -> tuple[str, str, Optional[str]]:
    """File the `skill_approval` card. Returns (event_id, card_note, error).
    `error` is set only when `required` and the card could not be filed —
    the caller takes the draft back in that case, because nothing else would
    ever surface it. Every other case is best-effort: the write already
    landed and a missing card is a warning, not a reason to lose the file."""
    if event_store is None:
        if required:
            return "", "", (
                "no workspace inbox is configured, so the draft was taken "
                "back rather than left where nothing could ever find it"
            )
        return "", "", None

    payload: dict[str, Any] = {
        "name": name,
        "description": draft.description,
        "rationale": draft.rationale,
        "proposer": draft.proposer,
        "origin": origin,
        "rendered_markdown": rendered,
    }
    if card_extra:
        payload.update(card_extra)
    event = WorkspaceEvent.new(
        kind="skill_approval",
        source="agent",
        title=card_title or f"Skill proposal: {name}",
        summary=card_summary if card_summary is not None else draft.rationale,
        payload=payload,
    )
    try:
        event_store.append_event(event)
    except Exception:
        logger.exception("skill_door: proposal event failed for %s", name)
        if required:
            return "", "", (
                "the inbox could not take the card, so the draft was taken "
                "back to be written again next pass"
            )
        return "", (
            "\nWARNING: the proposal card could not be filed in the "
            "Workspace Inbox. Post a workspace_post note so the "
            "operator knows this skill is pending."
        ), None

    try:
        if app_provider is not None:
            app = app_provider()
            if app is not None:
                await broadcast_workspace_event(app, event)
    except Exception:
        logger.warning("skill_door: card broadcast failed for %s", name, exc_info=True)

    return event.event_id, f"\nProposal card filed in the Workspace Inbox ({event.event_id}).", None


def settle_card_approved(event_store: Optional[EventStore], name: str, event_id: str) -> None:
    """Mark a just-filed proposal card approved, so the inbox does not offer
    a decision that has already been taken."""
    if event_store is None or not event_id:
        return
    try:
        event_store.update_event_status(
            event_id, "approved",
            reason="promoted on its own: creating a skill needs no approval in this mode",
        )
    except Exception:
        logger.warning("skill_door: could not settle the card for %s", name, exc_info=True)
