"""Leaving a conversation behind, once, wherever the turn came from.

Reflection before a fresh start had three implementations. The cockpit's
`/reset` snapshots the conversation and reflects in the background under
`REFLECTION_PROMPT`, landing a `reflection_proposal` in the workspace inbox. A
channel's `/clear` ran a synthetic FOREGROUND turn against a second,
hand-written prompt and landed nothing anywhere the operator could read later.
And `session_ops.do_reset` / `reset_with_reflection` were a third, written and
never called.

So the REFLECTION lives here and is the same for everyone.

What does NOT live here, because each surface is genuinely right about its own:

- **Persisting.** The cockpit archives the outgoing transcript into the drawer.
  A channel drops its durable record, so the next message does not restore the
  thing that was just cleared. Neither is the other's bug.
- **The ending.** The cockpit reaches an empty conversation by opening a new
  chat beside the archived one; a channel has no chat to switch to and wipes in
  place. A transport difference, not a behaviour one.

The caller persists, calls this, then ends the conversation the way its surface
can.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from tesseract.brain.session_ops import reflect_in_background

log = logging.getLogger(__name__)

#: How many saves the inbox summary lists by name before it counts the rest.
_BULLET_CAP = 6
_SUMMARY_CHARS = 1200


def _deliver_package(
    chat_session: Any,
    outcome: str,
    label: str,
    saves: list[dict[str, Any]],
) -> str:
    """The continuity package, put in front of the conversation that carries on.

    Only for a CONTINUE, and only once this reflection has written its
    checkpoint, which is why it lives here rather than at the boundary: the
    boundary returned before the model turn that produces the record even
    started.

    Read back from the record rather than handed a copy of it, which is the
    rule the record itself is built on. Returns the text so a caller can send
    it; empty means there was nothing to say, which is what a conversation
    that was never about a piece of work leaves behind.
    """
    if outcome != "continue":
        return ""
    from tesseract.brain import continuity
    from tesseract.orchestrator import checkpoints

    chat_id = str(getattr(getattr(chat_session, "tool_context", None), "chat_id", "") or "")
    if not chat_id:
        log.info("continuity: %s has no durable id, so nothing carries over", label)
        return ""
    text = continuity.package_for(checkpoints.latest_for_chat(chat_id), saves)
    if not text:
        log.info("continuity: the boundary at %s said nothing about the work", label)
        return ""
    try:
        chat_session.note_continuity(text)
    except Exception:
        log.exception("continuity: could not put the package in front of %s", label)
        return ""
    return text


def _persist(session: Any, label: str) -> None:
    """Write the package to disk with the conversation it was put in front of.

    The boundary persisted an EMPTY history on its way past, because at that
    moment the package did not exist. Left to the periodic autosave, a reload
    inside its interval shows a thread the boundary cleared and nothing saying
    why. It is one write and it lands with the record already open.
    """
    try:
        from tesseract.mirror.server import chat_store

        chat_store.persist_session_chats(session)
    except Exception:
        log.exception("continuity: the package was not written to disk for %s", label)


def reflect_callbacks(
    app: Any,
    session: Any,
    label: str,
    *,
    chat_session: Any = None,
    outcome: str = "",
    deliver: Any = None,
) -> tuple:
    """Build `(on_complete, on_error)` for `reflect_in_background`.

    `on_complete` writes a `reflection_proposal` event carrying the actual save
    list, one bullet per `memory_save` / `diary_append` / `soul_growth_propose`
    observed, so the operator sees WHAT was saved and not just a count. The
    `mission_reflection_proposal` kind is historical-records-only — its producer
    was removed with the mission engine, and the kind stays defined so old
    workspace events still deserialize.

    `on_error` writes the same kind at `priority=7`, so a reflection that failed
    rises above ambient inbox noise instead of being buried in a log.
    """

    async def on_complete(saves: list[dict[str, Any]], reason: str) -> None:
        try:
            from tesseract.workspace_events.broadcast import broadcast_workspace_event
            from tesseract.workspace_events.events import WorkspaceEvent

            store = app.get("workspace_event_store")
            if store is None:
                log.warning("reflect proposal skipped — no workspace_event_store on app")
                return
            count = len(saves)
            if count:
                bullet_lines: list[str] = []
                for s in saves[:_BULLET_CAP]:
                    tool = s.get("tool", "?")
                    title = s.get("title") or s.get("snippet") or "(no title)"
                    status = s.get("status") or ""
                    status_tag = (
                        f" [{status}]"
                        if status and status not in {"saved", "completed"}
                        else ""
                    )
                    bullet_lines.append(f"- [{tool}]{status_tag} {title}")
                if count > _BULLET_CAP:
                    bullet_lines.append(f"- … and {count - _BULLET_CAP} more")
                summary = (
                    f"Reflection complete: {count} write{'s' if count != 1 else ''}. "
                    "Expand for paths and content.\n" + "\n".join(bullet_lines)
                )[:_SUMMARY_CHARS]
            else:
                summary = "Reflection complete: nothing load-bearing to save."
            event = WorkspaceEvent(
                event_id=_event_id("evt_refl_session", session),
                ts=datetime.now(timezone.utc).isoformat(),
                kind="reflection_proposal",
                source="agent",
                title=f"Session reflection ({label})",
                summary=summary,
                payload={
                    "session_id": session.session_id,
                    "saves_count": count,
                    "saves": saves,
                    "reason": reason,
                    "label": label,
                },
            )
            store.append_event(event)
            await broadcast_workspace_event(app, event)
        except Exception:
            log.exception(
                "reflect_in_background on_complete: emit proposal failed (%s)", label
            )
        # Outside the try above on purpose. The package is what the person is
        # left with, and an inbox that would not take the proposal must not
        # also cost them the only thing telling them what happened to their
        # conversation.
        if chat_session is not None:
            text = _deliver_package(chat_session, outcome, label, saves)
            if text:
                await asyncio.to_thread(_persist, session, label)
                if deliver is not None:
                    try:
                        await deliver(text)
                    except Exception:
                        log.exception("continuity: could not tell %s about it", label)

    async def on_error(exc: BaseException, reason: str) -> None:
        try:
            from tesseract.workspace_events.broadcast import broadcast_workspace_event
            from tesseract.workspace_events.events import WorkspaceEvent

            store = app.get("workspace_event_store")
            if store is None:
                return
            event = WorkspaceEvent(
                event_id=_event_id("evt_refl_err", session),
                ts=datetime.now(timezone.utc).isoformat(),
                kind="reflection_proposal",
                source="agent",
                title=f"Session reflection failed ({label})",
                summary=f"Reflection raised {type(exc).__name__}: {exc}"[:_SUMMARY_CHARS],
                priority=7,
                payload={
                    "session_id": session.session_id,
                    "reason": reason,
                    "label": label,
                    "error_type": type(exc).__name__,
                },
            )
            store.append_event(event)
            await broadcast_workspace_event(app, event)
        except Exception:
            log.exception(
                "reflect_in_background on_error: emit proposal failed (%s)", label
            )

    return on_complete, on_error


def _event_id(prefix: str, session: Any) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    return f"{prefix}_{str(getattr(session, 'session_id', '') or '')[:16]}_{stamp}"


def hand_off(
    app: Any,
    session: Any,
    chat_session: Any,
    *,
    reason: str,
    label: str,
    trigger: str = "",
    outcome: str = "",
    refused: str = "",
    deliver: Any = None,
) -> bool:
    """Reflect on a snapshot of `chat_session`, in the background.

    Returns whether reflection actually started. `False` means the history was
    too short to be worth a model turn, or a prior reflect is still in flight —
    both of which `reflect_in_background` decides, and neither of which is a
    reason for the caller not to end the conversation.

    `trigger` and `outcome` describe the boundary that led here and are recorded
    on the checkpoint the reflection turn writes. They default to empty because
    the operator's `/reset`, `/clear` and `/reflect` also reach this function,
    and none of those is a boundary the runtime or the turn decided: a
    checkpoint claiming otherwise would put a decision in the record that
    nobody made.

    Background, on a snapshot, for the reason `session_ops` gives: reflection is
    a model turn and must never hold the operator's foreground. The live session
    can be wiped or switched away from in parallel; the clone owns its own copy
    of the history.
    """
    on_complete, on_error = reflect_callbacks(
        app, session, label,
        chat_session=chat_session, outcome=outcome, deliver=deliver,
    )
    return (
        reflect_in_background(
            chat_session,
            reason=reason,
            on_complete=on_complete,
            on_error=on_error,
            trigger=trigger,
            outcome=outcome,
            refused=refused,
        )
        is not None
    )


__all__ = ["hand_off", "reflect_callbacks"]
