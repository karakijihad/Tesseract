"""WS session teardown — cancel in-flight turns, resolve pending asks, drop registry entries."""

from __future__ import annotations

import logging

from aiohttp import web

from tesseract.mirror.server.session_model import ServerSession

log = logging.getLogger(__name__)


def cleanup_session(app: web.Application, session: ServerSession) -> None:
    # Cancel EVERY chat's in-flight turn (active + any background/conductor
    # turns), not just the active chat's. Fires on both legitimate cases
    # (operator closes the tab while the assistant is mid-thought) and regressions
    # (Vite HMR drops the WS on a Python write — see vite.config.ts
    # watch.ignored + globals.css @source). Info-level so a stuck-on-restart
    # pattern is reconstructible from the log without spamming WARNING for
    # normal disconnects.
    running = [t for t in session.current_turn_tasks.values() if t is not None and not t.done()]
    if running:
        log.info(
            "cleanup_session cancelling %d in-flight turn(s) session_id=%s turn_count=%d",
            len(running),
            session.session_id,
            session.turn_count,
        )
        for task in running:
            task.cancel()
    # TTS state is per-turn — cancel each running turn's
    # synth chain via the session map, plus the legacy session field.
    for turn_state in list(session.turn_states_by_chat.values()):
        if turn_state.tts_synth_task and not turn_state.tts_synth_task.done():
            turn_state.tts_synth_task.cancel()
        turn_state.tts_synth_task = None
    if session.tts_synth_task and not session.tts_synth_task.done():
        session.tts_synth_task.cancel()
    session.tts_synth_task = None
    # Resolve any in-flight cost overage futures as deny — without this,
    # the voice-overage background task in ws.py keeps awaiting for up
    # to 300s after the WS is gone, leaking the task and the per-scope
    # in_flight set entry.
    for fut in session.pending_overage_asks.values():
        if not fut.done():
            fut.set_result(False)
    session.pending_overage_asks.clear()
    # Symmetric treatment for tool ASK futures. The turn-task cancel
    # above usually propagates into `ask_fn` and triggers its
    # CancelledError cleanup path, but if the turn already completed
    # while the WS was closing, the future can sit pending with no
    # awaiter. Resolve to deny so the audit picture matches reality
    # (operator left → no further approvals) and the dict can be
    # cleared without leaking futures.
    #
    # trio W4 exception: a PARKED ask is deliberately waiting out the
    # operator's absence — force-denying it here would kill the parked
    # spawn on every tab close/reload, the exact failure ask-instead-of-
    # die exists to prevent. Parked futures stay pending; their entries
    # live in the app-level dict and settle via the approvals surface.
    parked = app.get("parked_asks", {})
    # M13 — a parked entry is keyed by approval_id now, and belongs to a
    # specific session; only skip force-denying this session's own parked asks.
    parked_call_ids = {
        e.call_id for e in parked.values() if e.session_id == session.session_id
    }
    for call_id, fut in session.pending_asks.items():
        if call_id in parked_call_ids:
            continue
        if not fut.done():
            fut.set_result(False)
    session.pending_asks.clear()
    # Drop the voice PCM accumulator before the dict pop so any closure
    # that captured `session` doesn't hold the bytearray alive (Phase
    # 16 S2 reviewer note — defensive hygiene).
    session.voice_pcm_buffer = None
    app["sessions"].pop(session.session_id, None)
    app["server_sessions"].pop(session.session_id, None)
    app["event_logs"].pop(session.session_id, None)
