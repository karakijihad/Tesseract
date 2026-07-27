"""Turn intake — WS entry points that start/cancel a turn, plus the
end-of-turn drain of the per-chat FIFO queue (conversation-layer Task 4.2,
Q2). A follow-up arriving while a turn is in flight for that chat queues as
a NORMAL turn (not a mid-turn inject) and is dispatched FIFO as each prior
queued turn completes.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timezone

from aiohttp import web

from tesseract.mirror.server.envelope import (
    make_envelope,
    make_queue_overflow,
    make_queued_message,
    make_steer_rejected,
    make_steered,
)
from tesseract.mirror.server.session import ServerSession, send_envelope
from tesseract.mirror.server.tts import _cancel_tts_output
from tesseract.mirror.server.uploads import _validated_attachments

log = logging.getLogger(__name__)

MAX_USER_TEXT_CHARS = 32_000


async def _start_turn(app: web.Application, session: ServerSession, data: dict) -> None:
    # Lazy: `_spawn_tracked` lives in ws.py and `_run_chat_turn` in
    # turn_runner.py. Module-level imports here would cycle with ws.py's
    # re-export of this module's `_start_turn`/`_cancel_turn`.
    from tesseract.mirror.server import ws as _ws
    from tesseract.mirror.server.turn_runner import _run_chat_turn

    text = (data.get("text") or "").strip()
    attachments = _validated_attachments(app, session, data.get("attachments"))
    # MP-2: per-turn view-context snapshot from the Mirror. Stash on the
    # session — `_run_turn` consumes and clears before chat_session.send.
    snapshot = data.get("view_snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else None
    session.pending_view_snapshot = snapshot
    if attachments is None:
        await send_envelope(session, make_envelope(
            "stream_error", "loop", session.session_id,
            {"message": "invalid attachment payload"},
        ))
        return
    if not text and not attachments:
        return
    if len(text) > MAX_USER_TEXT_CHARS:
        await send_envelope(session, make_envelope(
            "stream_error", "loop", session.session_id,
            {"message": f"text exceeds {MAX_USER_TEXT_CHARS} chars"},
        ))
        return
    if session.current_turn_task and not session.current_turn_task.done():
        # conversation-layer Task 4.2 (Q2): a follow-up arriving mid-turn is
        # a NORMAL turn, queued FIFO behind the active one — not a mid-turn
        # inject. `enqueue_user_inject`/`pending_injected_messages` stay in
        # place for the future Q3 steer command; this is no longer their
        # default entry point (never silently dropped or coalesced — every
        # arrival either queues or is loudly rejected as overflow below).
        cid = session.active_chat_id
        queue = session.chat_queues.setdefault(cid, deque())
        from tesseract.config.runtime_limits import (
            default_runtime_config_path,
            load_chat_queue_max,
        )
        cap = load_chat_queue_max(default_runtime_config_path())
        if len(queue) >= cap:
            log.warning(
                "session %s chat %s queue full (max %d) — dropping follow-up",
                session.session_id, cid, cap,
            )
            await send_envelope(session, make_queue_overflow(
                session.session_id, text=text, queue_size=len(queue),
            ))
            return
        queued_at = datetime.now(timezone.utc).isoformat()
        # Carry the snapshot with the queued payload so the drain (which
        # calls `_start_turn(app, session, pending)` recursively) rehydrates
        # it and turn-B doesn't lose its view context.
        queue.append({
            "text": text,
            "attachments": attachments,
            "view_snapshot": snapshot,
            "queued_at": queued_at,
        })
        await send_envelope(session, make_queued_message(
            session.session_id,
            text=text,
            queued_at=queued_at,
            queue_size=len(queue),
            position=len(queue),
        ))
        return
    session.current_turn_task = _ws._spawn_tracked(
        app,
        _run_chat_turn(app, session, text, attachments, chat_id=session.active_chat_id),
        f"chat_turn:{session.session_id}",
    )


async def handle_steer(app: web.Application, session: ServerSession, data: dict) -> None:
    """conversation-layer Task 5.1 (Q3) — redirect a running turn without
    cancelling it. `data = {chat_id, text}`. If a turn is ACTIVE for
    `chat_id`, `text` is folded into that turn via `ChatSession.
    enqueue_user_inject` (picked up at the next tool boundary) and a
    `steered` envelope confirms it landed. No active turn for `chat_id` —
    nothing to redirect — degrades to a normal `_start_turn` send so the
    text is never silently dropped, PROVIDED `chat_id` is the focused chat:
    `_start_turn` has no way to target an arbitrary chat (it always sends on
    `session.active_chat_id`), so a steer for an inactive BACKGROUND chat
    with no active turn is dropped instead of silently misdelivering the
    text into whatever chat happens to be focused — loudly: logged AND a
    client-visible `steer_rejected` envelope (house convention, cf.
    `make_queue_overflow` — drops are never silent).

    SAFETY (review blocker): this function never touches `session.
    pending_asks` — a steer must NEVER resolve a pending ASK. ASK futures
    are a wholly separate mechanism (`_resolve_ask` in ws.py, keyed by
    `call_id`) from the per-chat inject queue touched here, so there is no
    shared state to accidentally settle; an ASK left open before a steer
    stays open after it.

    Race note: the active-check and the `enqueue_user_inject` call below
    are both synchronous with no `await` between them, so no other
    coroutine can run the turn to completion in between — the check is
    atomic w.r.t. the event loop. If the turn's tool loop has already
    exited its polling window by the time this steer lands (task not yet
    `done()` but no further boundary will run), the entry is not lost:
    `_run_turn`'s finally-tail rescues any leftover `pending_injected_
    messages` for the chat whose turn just ended — via `drain_next` for the
    focused chat (unchanged), or `drain_stranded_background` (below) for a
    background chat, since `drain_next`'s fallback re-enters through
    `_start_turn`, which only ever targets the focused chat.

    Review fix-pass (Task 5.2): the FOCUSED-chat degrade branch below (no
    active turn → plain `_start_turn`) used to emit no envelope at all, so
    the frontend's optimistic `steered: true` bubble (rendered at send
    time, before this handler even runs) never got reconciled — a normal
    turn permanently wore a "redirected" pill. Now emits `make_steered(...,
    applied=False)` before starting the turn so the client can clear the
    flag on that bubble.
    """
    chat_id = data.get("chat_id") or session.active_chat_id
    text = (data.get("text") or "").strip()
    if not text:
        return
    task = session.current_turn_tasks.get(chat_id)
    if task and not task.done():
        chat_session = session.chats.get(chat_id, session.chat_session)
        chat_session.enqueue_user_inject(text)
        await send_envelope(session, make_steered(
            session.session_id, chat_id=chat_id, text=text,
        ))
        return
    if chat_id != session.active_chat_id:
        reason = (
            f"no active turn for background chat {chat_id} (focused chat "
            f"is {session.active_chat_id}) — dropped rather than "
            "misrouted to the focused chat"
        )
        log.warning("steer: %s", reason)
        await send_envelope(session, make_steer_rejected(
            session.session_id, chat_id=chat_id, text=text, reason=reason,
        ))
        return
    await send_envelope(session, make_steered(
        session.session_id, chat_id=chat_id, text=text, applied=False,
    ))
    await _start_turn(app, session, data)


async def _cancel_turn(app: web.Application, session: ServerSession) -> None:
    _cancel_tts_output(session)
    # Audit-3 #2/#4 — explicit cancel (operator stop button, voice
    # speech-start barge-in) drops the ENTIRE queued backlog too, not just
    # one entry (Task 4.2 / Q2: clear-all). The operator's live intent is
    # whatever comes next (the new voice utterance, or nothing); draining
    # stale queued turns after a cancel would surprise the operator with
    # replies they no longer wanted. Popping `chat_queues[active_chat_id]`
    # drops the whole deque (Task 4.5 retired the `pending_user_payload`
    # back-compat setter that used to do this). The same contract extends
    # to mid-turn injected messages (Q3 steer) — they share the "queued
    # during active turn" semantics.
    session.chat_queues.pop(session.active_chat_id, None)
    session.chat_session.pending_injected_messages = []
    # WP-2: cancel ONLY affects the chat turn. Workspace synthetic turns
    # (in `synthetic_turn_tasks`) and their queued payloads live on
    # independent threads — the operator hitting Stop on the chat panel
    # is not a signal to abandon every open workspace comment thread.
    # If the operator wants to cancel a specific thread's reply, they
    # dismiss the card directly. Cross-thread workspace queue stays
    # intact; in-flight synthetic turns continue.
    task = session.current_turn_task
    if task and not task.done():
        session.chat_session.tool_context.cancel_event.set()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        session.current_turn_task = None


async def drain_next(app: web.Application, session: ServerSession, chat_id: str | None) -> None:
    """End-of-turn FIFO drain for ``chat_id`` (conversation-layer Task 4.2,
    Q2). Pops the OLDEST queued payload and re-enters `_start_turn`, which
    spawns it as a fresh turn — no turn is in flight at this point, since
    this only runs from `_run_turn`'s post-turn tail. Only ONE entry is
    popped per call; any remaining entries stay queued for the next turn's
    completion to drain (that's what keeps the dispatch strictly FIFO,
    never running two queued turns at once).
    """
    cs = session.chats[chat_id] if chat_id in session.chats else session.chat_session
    # M1 (Codex 2026-05-06): operator typed/voice has its own queue.
    # Workspace synthetic turns are owned by the `synthetic_turn_tasks`
    # lane via the controller path — the chat-lane drain MUST NOT touch
    # `pending_workspace_payloads`, otherwise both paths race and a
    # payload can spawn twice. Chat-lane drain here only handles
    # operator typed text/voice.
    queue = session.chat_queues.get(chat_id)
    pending = queue.popleft() if queue else None
    if queue is not None and not queue:
        session.chat_queues.pop(chat_id, None)
    if pending is None and cs.pending_injected_messages:
        # Stranded Q3-steer injects (the explicit mid-turn inject path —
        # Q2 no longer routes plain text here by default): the turn ended
        # before a tool boundary ever drained them. Surface as a fresh turn
        # now. Each entry was already shown in chat as a queued_message
        # envelope, so the operator already saw it land — this just makes
        # the model actually respond to it.
        stranded = cs.pending_injected_messages
        cs.pending_injected_messages = []
        joined = "\n".join(e["text"] for e in stranded if e.get("text"))
        if joined:
            pending = {
                "text": joined,
                "attachments": [],
                "view_snapshot": None,
            }
    if pending:
        await _start_turn(app, session, pending)


async def drain_stranded_background(
    app: web.Application, session: ServerSession, chat_id: str,
) -> None:
    """Background-chat counterpart to `drain_next`'s stranded-inject fallback
    (review fix-pass, Finding 1). Background (non-focused) chats never
    populate `chat_queues` — that FIFO queue is only ever filled for the
    active chat (see `_start_turn`) — so there is no queued follow-up to pop
    here, only a possible Q3-steer inject that lost the race against this
    chat's own turn ending (the same race `drain_next` covers for the
    focused chat).

    `drain_next`'s fallback re-enters via `_start_turn`, which always
    targets `session.active_chat_id` — wrong for a background chat, so this
    spawns `_run_chat_turn` directly for `chat_id` instead (the same pattern
    `spawn_wake.schedule_wake` uses to drive a background turn), fire-and-
    forget like the active-chat drain's respawn (never awaited to
    completion — `_run_turn`'s tail must return promptly).
    """
    from tesseract.mirror.server import ws as _ws
    from tesseract.mirror.server.turn_runner import _run_chat_turn

    cs = session.chats.get(chat_id, session.chat_session)
    if not cs.pending_injected_messages:
        return
    stranded = cs.pending_injected_messages
    cs.pending_injected_messages = []
    joined = "\n".join(e["text"] for e in stranded if e.get("text"))
    if not joined:
        return
    task = _ws._spawn_tracked(
        app,
        _run_chat_turn(app, session, joined, chat_id=chat_id),
        f"chat_turn:{session.session_id}:{chat_id}",
    )
    session.current_turn_tasks[chat_id] = task
