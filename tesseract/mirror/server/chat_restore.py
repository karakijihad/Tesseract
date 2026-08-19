"""Persisted-chat rehydration — rebuild a session's open-chat registry from disk on reconnect."""

from __future__ import annotations

import logging

from aiohttp import web

from tesseract.brain.chat import ChatSession
from tesseract.mirror.server.session_model import MAX_OPEN_CHATS, ChatMeta, ServerSession

log = logging.getLogger(__name__)


def _restore_persisted_chats(app: web.Application, session: ServerSession) -> None:
    """P3 reload hydration — replace the single seeded chat with the persisted
    open (non-archived) chats so the tab strip survives a page reload.

    ``chat_store`` is session-agnostic, so the open set is the global non-archived
    library, capped at ``MAX_OPEN_CHATS`` newest (D5). Each chat is rebuilt as a
    live ``ChatSession`` carrying its persisted history; the newest is made
    active. First run (empty library) keeps the fresh ``__post_init__`` seed.
    Builds into locals and assigns atomically — a mid-rebuild failure leaves the
    session untouched.

    Day-rollover (operator request 2026-07-05): before listing, chats last
    touched on a prior local calendar day are auto-archived via
    ``chat_store.archive_stale_open_chats`` — so a connection made on a new
    day either restores only today's still-open chats, or (if none) falls
    through to the ``if not rows`` branch and keeps the blank fresh seed,
    same as the never-used-Mirror-yet case. The archived chat is untouched
    on disk otherwise — reachable via chat.restore / GET
    /api/chats?include_archived=1.
    """
    from tesseract.mirror.server import chat_store
    # Lazy: `session_factory.py` imports `_restore_persisted_chats` from this
    # module at top level (`create_server_session` calls it after seeding the
    # first chat) — a module-level import of `new_chat_session` here would
    # cycle back into a module still mid-import.
    from tesseract.mirror.server.session_factory import new_chat_session

    chat_store.archive_stale_open_chats()
    rows = chat_store.list_chats()[:MAX_OPEN_CHATS]  # newest-first, non-archived
    if not rows:
        return
    chats: dict[str, ChatSession] = {}
    chat_meta: dict[str, ChatMeta] = {}
    chat_order: list[str] = []
    for row in reversed(rows):  # oldest-first → matches create_chat append order
        record = chat_store.load_chat(row["chat_id"])
        if record is None:
            log.debug("chat restore: skipping missing record %s", row["chat_id"])
            continue
        cs = new_chat_session(app, session, kind=session.kind)
        cs.history = list(record.history)
        # P6 Task 3 §G5 — a spawn started under `record.session_id` (the
        # PRIOR session/process) has no surviving asyncio.Task now; mark any
        # orphan `[spawn_lost]` before the chat is handed back to the operator.
        cs.mark_vanished_spawns(record.session_id)
        # M4-p2 — AFTER the above sweep (so only genuinely-dead spawns were
        # dropped): re-associate any still-live or dead-window-completed
        # spawn this chat owned with THIS reconnect's (session, cs), so its
        # completion (or already-completed result) is observable here
        # instead of notifying the orphaned prior ChatSession.
        from tesseract.mirror.server.spawn_ownership import rebind_chat
        rebind_chat(app, session, cs, record.chat_id)
        # AFTER the rebind, so a dead-window completion it just folded in by
        # hand is skipped rather than delivered twice. What is left here is the
        # cross-restart case the in-memory ownership index cannot see: a spawn
        # that finished under the PREVIOUS process and was never read.
        cs.replay_undelivered_completions(record.chat_id)
        chats[record.chat_id] = cs
        chat_meta[record.chat_id] = ChatMeta(
            chat_id=record.chat_id,
            title=record.title,
            created_at=record.created_at,
            started_at=record.started_at,
            archived=False,
            turn_count=record.turn_count,
            model=record.model,
        )
        chat_order.append(record.chat_id)
    if not chats:
        return
    session.chats = chats
    session.chat_meta = chat_meta
    session.chat_order = chat_order
    session.active_chat_id = chat_order[-1]  # newest is active
    session.chat_session = chats[session.active_chat_id]
