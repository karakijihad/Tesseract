"""Persisted-chat rehydration — rebuild a session's open-chat registry from disk on reconnect."""

from __future__ import annotations

import logging

from aiohttp import web

from tesseract.brain.chat import ChatSession
from tesseract.mirror.server.session_model import (
    MAX_OPEN_CHATS,
    ChatMeta,
    ServerSession,
    stamp_chat_id,
)

log = logging.getLogger(__name__)


def _restore_persisted_chats(app: web.Application, session: ServerSession) -> None:
    """P3 reload hydration — bring the persisted open (non-archived) chats back
    into the session so a page reload does not lose them.

    ``chat_store`` is session-agnostic, so the open set is the global non-archived
    library, capped at ``MAX_OPEN_CHATS`` newest (D5). Each chat is rebuilt as a
    live ``ChatSession`` carrying its persisted history.
    Builds into locals and assigns atomically — a mid-rebuild failure leaves the
    session untouched.

    **The seed stays, and the seed is what is active.** This used to make the
    newest restored chat active and drop the ``__post_init__`` seed entirely,
    which put the operator inside yesterday's conversation without telling
    them: ``session_created`` carries each chat's id and title and no history,
    so the transcript rendered empty while the backend held the whole thread,
    and the next message was appended to it. Opening a window now gives a
    blank conversation, which is what every other surface means by opening
    one, and the restored chats are reachable from the rail (operator ruling,
    2026-08-25). The seed is re-registered under a fresh id, which runs the
    open-chat cap, so the set still totals ``MAX_OPEN_CHATS``.

    **No day-rollover archive.** This used to call
    ``chat_store.archive_stale_open_chats()`` with the default ``keep_days=0``,
    so every chat not touched today was shelved before the list was read
    (operator request, 2026-07-05: a new day should open on a blank chat).
    The outcome was right and the mechanism was not: yesterday's conversation
    was archived, and getting it back was a restore, which is not what the
    word means to anyone. IS-18 reverses it on the operator's own instruction.

    The window that files a conversation away is `retention.yaml::sessions`,
    where it has always been and has always been a week. That sweep flips
    ``archived`` in place on a chat quiet for `keep_days`, which is the same
    flag this list reads, so nothing else changes.
    """
    from tesseract.mirror.server import chat_store
    # Lazy: `session_factory.py` imports `_restore_persisted_chats` from this
    # module at top level (`create_server_session` calls it after seeding the
    # first chat) — a module-level import of `new_chat_session` here would
    # cycle back into a module still mid-import.
    from tesseract.mirror.server.session_factory import new_chat_session

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
        # A chat rebuilt here is registered by replacing `session.chats`
        # wholesale below, which is the one route into the live set that does
        # not run through `create_chat` or `reopen_chat`. Without this the
        # restored chat never learns its own id, and anything the assistant
        # then leaves on the canvas records no owner: a press in it reaches
        # nobody, silently, which is exactly how it failed the first time.
        stamp_chat_id(cs, record.chat_id)
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
    seed = session.chat_session
    session.chats = chats
    session.chat_meta = chat_meta
    session.chat_order = chat_order
    # The blank chat you land in. ``create_chat`` mints its id, which is what
    # keeps this to one path: reusing the ``__post_init__`` id would collide
    # with a restored chat's whenever the two matched, and the seed would
    # silently replace a conversation. The ChatSession object itself IS
    # reused, because it is already built with this connection's ask gate,
    # sink and status closures. Registering it also runs the open-chat cap,
    # so the set still totals ``MAX_OPEN_CHATS``.
    session.active_chat_id = session.create_chat(seed)
    session.chat_session = seed
