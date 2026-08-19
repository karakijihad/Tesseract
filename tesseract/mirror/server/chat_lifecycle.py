"""Chat lifecycle WS handlers (create/switch/archive/restore/rename) extracted from ws.py."""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from tesseract.mirror.server import chat_store, spawn_wake
from tesseract.mirror.server.envelope import make_envelope
from tesseract.mirror.server.session import (
    ChatInfraNotReady,
    ChatMeta,
    ServerSession,
    new_chat_session,
    send_envelope,
)
from tesseract.brain.session_store import sanitize_history_for_persistence
from tesseract.mirror.server.tts import _cancel_tts_output

log = logging.getLogger(__name__)


def sessions_holding(app: web.Application, chat_id: str) -> list[ServerSession]:
    """Every live session whose registry holds this chat.

    A mutation that only touches disk is undone by the next
    ``persist_session_chats``, which writes each live session's in-memory
    ``chat_meta`` back over the record. That is not a race — it is the writer
    doing its job over a change it was never told about. Both the REST routes
    and the slash commands reach live state through this one function, because
    they had drifted into two half-implementations of it.
    """
    return [
        srv
        for srv in (app.get("server_sessions") or {}).values()
        if chat_id in (getattr(srv, "chat_meta", None) or {})
    ]


def would_orphan_a_session(app: web.Application, chat_id: str) -> bool:
    """True if shelving this chat would leave some session with none open.

    `ServerSession.archive_chat` raises on that, so it is asked BEFORE disk
    moves rather than caught after: archiving on disk while a session keeps the
    chat open is a state the next persist reverses.
    """
    return any(
        chat_id in srv.chat_order and len(srv.chat_order) == 1
        for srv in sessions_holding(app, chat_id)
    )


def chat_is_busy(app: web.Application, chat_id: str) -> bool:
    """True while any live session has a turn running ON THIS CHAT.

    `current_turn_tasks` is keyed by chat_id, so a background chat can be
    mid-turn while another is active. The WS command dispatcher gates mutating
    commands on `has_running_turn`; the REST routes had no equivalent, so a
    delete could land on a chat still being written to and the turn's output
    would go nowhere — `persist_session_chats` only walks `session.chats`.
    """
    for srv in sessions_holding(app, chat_id):
        task = (getattr(srv, "current_turn_tasks", None) or {}).get(chat_id)
        if task is not None and not task.done():
            return True
    return False


def detach_deleted_chat(app: web.Application, chat_id: str) -> None:
    """Drop a deleted chat from every live session, active-safely.

    Popping the registries alone leaves a session whose ``active_chat_id`` and
    ``chat_session`` still point at a chat no longer in ``chats`` — and since
    `persist_session_chats` iterates ``chats``, everything typed there
    afterwards is silently never written. Archiving first is what moves
    ``active`` off it; call `would_orphan_a_session` before this.
    """
    for srv in sessions_holding(app, chat_id):
        if chat_id in srv.chat_order:
            srv.archive_chat(chat_id)
        srv.chats.pop(chat_id, None)
        srv.chat_meta.pop(chat_id, None)


def _detach_outgoing_observer(
    app: web.Application, session: ServerSession, outgoing: Any
) -> None:
    """P4 — drop the observer back-reference on the chat we're leaving, SYNCHRONOUSLY,
    before the switch/archive envelope is sent.

    The subscriber follows ``active_chat_id`` (governance rule #7: the observer is
    session-global, fires on the active chat only). ``_detach_subscriber`` only
    clears whatever ``session.chat_session`` currently points at — the NEW active
    chat post-switch — so the chat we just left keeps its back-reference and, because
    the singleton stays ``is_active`` after re-attach, its next background turn would
    bleed an observation into the new chat. Clearing it here closes that window. The
    slow half (cancel in-flight tasks + re-attach) is deferred to
    ``_spawn_observer_reattach`` so a tab switch doesn't stall. No-op when off."""
    if app.get("observer_state") not in {"armed", "observing"}:
        return
    if app.get("observer_subscriber") is None:
        return
    if outgoing is not None and outgoing is not session.chat_session:
        try:
            outgoing.detach_observer_subscriber()
        except Exception:
            log.exception("observer outgoing-detach failed")


def _spawn_observer_reattach(app: web.Application, session: ServerSession) -> None:
    """P4 — re-attach the observer subscriber to the now-active chat in the
    background. ``_attach_observer_subscriber_if_armed`` awaits ``subscriber.detach()``
    (up to ``_DETACH_TIMEOUT_S`` cancelling in-flight observer tasks cleanly, so they
    can't emit against the new chat); running it OFF the WS dispatch path keeps the
    switch responsive — the ``chat_switched`` envelope is already sent by now."""
    if app.get("observer_state") not in {"armed", "observing"}:
        return
    # Lazy: `_spawn_tracked`/`_attach_observer_subscriber_if_armed` still live
    # in ws.py, which re-exports this module's handlers — a module-level
    # import here would cycle with that re-export.
    from tesseract.mirror.server import ws as _ws
    _ws._spawn_tracked(
        app,
        _ws._attach_observer_subscriber_if_armed(app, session),
        f"observer_rewire:{session.session_id}",
    )


def _open_chats_payload(session: ServerSession) -> list[dict[str, str]]:
    """The open-chat list for `session_created` (P3 reload hydration).

    Newest-first (``chat_order`` is insertion-ordered oldest→newest), each with
    its sidebar title from ``chat_meta``. The frontend seeds its tab strip from
    this so tabs survive a page reload.
    """
    return [
        {"chat_id": cid, "title": session.chat_meta[cid].title}
        for cid in reversed(session.chat_order)
        if cid in session.chat_meta
    ]


# mirror-multi-chat P1 inc.3b — chat-lifecycle WS handlers. The chat registry +
# persistence live on ServerSession / chat_store (inc.1/2); these are the thin
# wiring that drives them from the live WS and emits the frontend envelopes.
async def _handle_chat_create(app: web.Application, session: ServerSession, data: dict) -> None:
    try:
        chat_session = new_chat_session(app, session, kind=session.kind)
    except ChatInfraNotReady:
        await send_envelope(session, make_envelope(
            "chat_create_failed", "chat", session.session_id, {"reason": "infra_not_ready"},
        ))
        return
    except Exception:
        # _dispatch has no per-handler guard — a build fault deeper than the
        # boot-race check (config miss, adapter chain) must not break the WS
        # loop. Surface it as a failed envelope, same as the infra-race path.
        log.exception("chat.create: build failed for session %s", session.session_id)
        await send_envelope(session, make_envelope(
            "chat_create_failed", "chat", session.session_id, {"reason": "internal_error"},
        ))
        return
    chat_id = session.create_chat(chat_session)
    # Spawn push Stage 2 — wire idle-wake on the new chat (connect-time install
    # only covered chats open at connect).
    spawn_wake.wire_chat(app, session, chat_id, chat_session)
    meta = session.chat_meta[chat_id]
    # P5 — creating a chat FOCUSES it: switch the backend active chat to the new
    # one so the operator's next `chat_message` (no chat_id → runs on
    # `active_chat_id`, ws.py turn dispatch) and the observer land in the new chat,
    # not the previously-active one. The frontend already makes it active on
    # `chat_created`; this keeps the backend in lock-step. Mirror the switch path:
    # cut the outgoing chat's voice (D8) and re-wire the observer to follow.
    outgoing = session.chat_session
    session.switch_chat(chat_id)
    _cancel_tts_output(session)
    _detach_outgoing_observer(app, session, outgoing)
    # Persist immediately so a freshly-created (empty) chat is in the library
    # even if the connection drops before its first turn.
    try:
        chat_store.persist_session_chats(session)
    except Exception:
        log.exception("chat.create: persist failed for %s", chat_id)
    await send_envelope(session, make_envelope(
        "chat_created", "chat", session.session_id,
        {"chat_id": chat_id, "title": meta.title, "created_at": meta.created_at},
    ))
    _spawn_observer_reattach(app, session)


async def _handle_chat_switch(app: web.Application, session: ServerSession, data: dict) -> None:
    chat_id = data.get("chat_id")
    if not isinstance(chat_id, str):
        return
    outgoing = session.chat_session
    try:
        session.switch_chat(chat_id)
    except KeyError:
        await send_envelope(session, make_envelope(
            "chat_switch_failed", "chat", session.session_id,
            {"chat_id": chat_id, "reason": "unknown_chat"},
        ))
        return
    # inc.C2 dynamic voice — cut the chat we're leaving. `tts_suppressed` is now
    # live, so the previously-active turn (if still streaming) goes silent the
    # moment active_chat_id changes; cancelling its in-flight synth here stops
    # the already-queued sentences from trailing on after the switch. The
    # frontend's own audio stop + speaking_back clear ride with the P3 switch UI.
    _cancel_tts_output(session)
    # P4 — the observer follows the active chat. Clear the outgoing chat NOW (so a
    # background turn on it can't bleed into the new chat), then re-attach in the
    # background AFTER the switch UI is sent — the re-attach's task-cancel can take
    # up to ~2s and must not stall the tab switch.
    _detach_outgoing_observer(app, session, outgoing)
    meta = session.chat_meta[chat_id]
    # Sanitize (drop attachment bytes) so a chat that processed a vision/tool
    # attachment doesn't push multi-MB base64 into the switch frame; matches
    # the persisted/reloaded form the frontend already renders.
    history = sanitize_history_for_persistence(session.chat_session.history)
    await send_envelope(session, make_envelope(
        "chat_switched", "chat", session.session_id,
        {"chat_id": chat_id, "title": meta.title, "history": history},
    ))
    _spawn_observer_reattach(app, session)


async def _handle_chat_archive(app: web.Application, session: ServerSession, data: dict) -> None:
    chat_id = data.get("chat_id")
    if not isinstance(chat_id, str):
        return
    outgoing = session.chat_session
    try:
        session.archive_chat(chat_id)
    except KeyError:
        await send_envelope(session, make_envelope(
            "chat_archive_failed", "chat", session.session_id,
            {"chat_id": chat_id, "reason": "unknown_chat"},
        ))
        return
    except ValueError:
        await send_envelope(session, make_envelope(
            "chat_archive_failed", "chat", session.session_id,
            {"chat_id": chat_id, "reason": "last_open_chat"},
        ))
        return
    # P4 — archiving the active chat switches active away (archive_chat → switch_chat);
    # the observer follows. No-op when a background chat was archived. Sync detach
    # before the envelope, background re-attach after (see _handle_chat_switch).
    active_changed = session.chat_session is not outgoing
    if active_changed:
        _detach_outgoing_observer(app, session, outgoing)
    try:
        chat_store.persist_session_chats(session)
    except Exception:
        log.exception("chat.archive: persist failed for %s", chat_id)
    # Archiving the active chat switches active away → report the new active
    # so the frontend can follow without a separate round-trip.
    await send_envelope(session, make_envelope(
        "chat_archived", "chat", session.session_id,
        {"chat_id": chat_id, "active_chat_id": session.active_chat_id},
    ))
    if active_changed:
        _spawn_observer_reattach(app, session)


def _restore_failed(session: ServerSession, chat_id: object, reason: str) -> dict:
    return make_envelope(
        "chat_restore_failed", "chat", session.session_id,
        {"chat_id": chat_id, "reason": reason},
    )


async def _handle_chat_restore(app: web.Application, session: ServerSession, data: dict) -> None:
    """P5 — un-archive a chat back into the open set and focus it.

    Two cases: a chat archived THIS session is still in `session.chats` (re-add in
    place); a chat archived in a PRIOR session is gone from the live registry and is
    rebuilt from its persisted record. Focusing it mirrors the switch path (TTS cut +
    observer re-wire). Restoring an already-open chat is a `not_archived` contract
    error (mirror of the REST restore guard)."""
    chat_id = data.get("chat_id")
    if not isinstance(chat_id, str):
        return
    if chat_id in session.chat_order:
        await send_envelope(session, _restore_failed(session, chat_id, "not_archived"))
        return
    outgoing = session.chat_session
    if chat_id in session.chats:
        session.reopen_chat(chat_id)
    else:
        record = chat_store.load_chat(chat_id)
        if record is None:
            await send_envelope(session, _restore_failed(session, chat_id, "unknown_chat"))
            return
        if not record.archived:
            await send_envelope(session, _restore_failed(session, chat_id, "not_archived"))
            return
        try:
            cs = new_chat_session(app, session, kind=session.kind)
        except ChatInfraNotReady:
            await send_envelope(session, _restore_failed(session, chat_id, "infra_not_ready"))
            return
        except Exception:
            log.exception("chat.restore: build failed for %s", chat_id)
            await send_envelope(session, _restore_failed(session, chat_id, "internal_error"))
            return
        cs.history = list(record.history)
        # P6 Task 3 §G5 — same rebuild-from-a-prior-record scenario as
        # `session.py::_restore_persisted_chats`: a spawn started under
        # `record.session_id` has no surviving asyncio.Task in this fresh
        # ChatSession. Mark any orphan `[spawn_lost]` before reopening.
        cs.mark_vanished_spawns(record.session_id)
        meta = ChatMeta(
            chat_id=record.chat_id, title=record.title,
            created_at=record.created_at, started_at=record.started_at,
            archived=False, turn_count=record.turn_count, model=record.model,
        )
        session.reopen_chat(chat_id, chat_session=cs, meta=meta)
        # Spawn push Stage 2 — a chat rebuilt from a prior-session record is a
        # fresh ChatSession with the bare floor notifier; wire idle-wake so a
        # background spawn dispatched after restore can wake it. The in-place
        # reopen branch above keeps its connect-time wiring (archiving doesn't
        # rebuild the ChatSession).
        spawn_wake.wire_chat(app, session, chat_id, cs)
        # M4-p2 review follow-up (3.2) — same re-association `chat_restore.
        # _restore_persisted_chats` performs after its own rebuild: without
        # this, a spawn owned by this chat before it was archived keeps
        # notifying the orphaned pre-archive ChatSession forever.
        from tesseract.mirror.server.spawn_ownership import rebind_chat
        rebind_chat(app, session, cs, chat_id)
        # Same ordering rule as `chat_restore._restore_persisted_chats`: the
        # rebind's dead-window delivery goes first, then anything recorded
        # under a previous process that nothing in memory remembers.
        cs.replay_undelivered_completions(chat_id)
    # Best-effort like the persist below — the in-memory restore already
    # succeeded; a disk-write failure here must not propagate through _dispatch
    # and drop the WS. Worst case the chat re-archives on the next reload.
    try:
        chat_store.set_archived(chat_id, False)
    except Exception:
        log.exception("chat.restore: un-archive on disk failed for %s", chat_id)
    _cancel_tts_output(session)
    _detach_outgoing_observer(app, session, outgoing)
    try:
        chat_store.persist_session_chats(session)
    except Exception:
        log.exception("chat.restore: persist failed for %s", chat_id)
    meta = session.chat_meta[chat_id]
    history = sanitize_history_for_persistence(session.chat_session.history)
    await send_envelope(session, make_envelope(
        "chat_restored", "chat", session.session_id,
        {"chat_id": chat_id, "title": meta.title, "history": history,
         "active_chat_id": session.active_chat_id},
    ))
    _spawn_observer_reattach(app, session)


async def _handle_chat_rename(app: web.Application, session: ServerSession, data: dict) -> None:
    # P3 — rename over WS (not the session-agnostic REST route) so the live
    # `chat_meta` title and the persisted record stay in lock-step: a REST-only
    # rename would be reverted by the next `persist_session_chats` autosave,
    # which writes the in-memory `chat_meta` back to disk.
    chat_id = data.get("chat_id")
    title = (data.get("title") or "").strip()
    if not isinstance(chat_id, str) or chat_id not in session.chat_meta:
        await send_envelope(session, make_envelope(
            "chat_rename_failed", "chat", session.session_id,
            {"chat_id": chat_id, "reason": "unknown_chat"},
        ))
        return
    if not title:
        await send_envelope(session, make_envelope(
            "chat_rename_failed", "chat", session.session_id,
            {"chat_id": chat_id, "reason": "empty_title"},
        ))
        return
    session.chat_meta[chat_id].title = title
    try:
        chat_store.persist_session_chats(session)
    except Exception:
        log.exception("chat.rename: persist failed for %s", chat_id)
    await send_envelope(session, make_envelope(
        "chat_renamed", "chat", session.session_id,
        {"chat_id": chat_id, "title": title},
    ))


def _handle_observer_pane_ack(app: web.Application, msg: dict) -> None:
    pane_id = msg.get("paneId") or msg.get("pane_id")
    granted = bool(msg.get("granted"))
    if not isinstance(pane_id, str) or not pane_id:
        log.debug("ws: observer_pane_ack missing paneId — ignoring")
        return
    pty = app.get("pty_manager")
    if pty is None:
        return
    if granted:
        pty.grant_consent(pane_id)
        # Granting transitions the global observer state into observing —
        # matches the frontend store, which sets state=observing on grant.
        if app.get("observer_state") in {"armed", "observing"}:
            app["observer_state"] = "observing"
    else:
        pty.revoke_consent(pane_id)

