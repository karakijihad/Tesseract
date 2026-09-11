"""Turn execution — runs a single model turn end-to-end (chat and synthetic
workspace turns), the conductor fan-out primitives (`send_and_await_turn`,
`run_turns_concurrently`), and post-turn housekeeping (auto-compact, stats).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from tesseract.mirror.server import chat_store

from aiohttp import web

from tesseract.brain import context_report
from tesseract.kernel.adapters.base import ChunkType
from tesseract.paths import TESSERACT_HOME
from tesseract.mirror.server.after_turn import after_turn
from tesseract.mirror.server.chunk_handler import _handle_chunk
from tesseract.mirror.server.envelope import make_envelope
from tesseract.mirror.server.session import ServerSession, send_envelope
from tesseract.mirror.server.tts import _flush_tts_terminator
from tesseract.mirror.server.turn_context import (
    TurnState,
    current_chat_id,
    current_turn_id,
    current_turn_state,
    current_workspace_origin,
)
from tesseract.mirror.server.uploads import _chat_content_for_model

log = logging.getLogger(__name__)


async def _run_turn(
    app: web.Application,
    session: ServerSession,
    text: str,
    attachments: list[dict[str, Any]] | None = None,
    *,
    workspace_origin: dict[str, Any] | None = None,
    chat_session: Any = None,
    chat_id: str | None = None,
    outcome: dict[str, Any] | None = None,
    runtime_origin: str | None = None,
) -> None:
    # Lazy: `_emit_entity_signals` and `_preprocess_audio_attachments` still
    # live in ws.py. A module-level import here would cycle with ws.py's
    # re-export of this module's public names. `_handle_chunk` moved to
    # chunk_handler.py (SDD Task 1.3) and is imported at module level above —
    # chunk_handler doesn't depend on ws.py or turn_runner.py, so no cycle.
    from tesseract.mirror.server import ws as _ws
    # Synthetic workspace turns pass a forked ChatSession so they
    # don't mutate the canonical chat history. Chat turns leave
    # `chat_session=None` and the canonical session is used.
    is_synthetic = chat_session is not None
    # Resolve which chat this turn drives. A chat
    # turn runs against that chat's ChatSession and tags its envelopes with the
    # id; a NON-active (conductor/background) chat streams text to its slice
    # but stays silent (D8 — TTS suppressed). Synthetic workspace turns keep
    # the canonical session and their existing session-scoped behavior.
    cid = None if is_synthetic else (chat_id or session.active_chat_id or None)
    if chat_session is not None:
        cs = chat_session
    elif cid is not None and cid in session.chats:
        cs = session.chats[cid]
    else:
        cs = session.chat_session
    session.turn_count += 1
    session.last_turn_at = datetime.now(timezone.utc)
    turn = session.turn_count
    # workspace_origin is not a session attribute (single-slot) but
    # explicit parameter + task-local ContextVar so concurrent chat +
    # synthetic turns don't overwrite each other. Backward-compatible
    # fallback for callers still relying on the legacy session attr.
    if workspace_origin is None:
        workspace_origin = getattr(session, "workspace_origin", None)
    # Synthetic turns get a `syn:<event_id>:<short>` discriminator so
    # dispatch.ts can route their envelopes around the chat conversation
    # store. Chat turns stay on bare uuid hex.
    if workspace_origin and workspace_origin.get("event_id"):
        turn_id = f"syn:{workspace_origin['event_id']}:{uuid.uuid4().hex[:8]}"
    else:
        turn_id = uuid.uuid4().hex
    # TTS state now lives on the per-turn TurnState
    # (constructed fresh below), so no session-level reset is needed — a
    # stale tail from a cancelled prior turn dies with that turn's state,
    # and two concurrently-streaming chats each own their buffer/sequence/
    # synth chain. Voice params pin lazily on the turn's first synth.
    # inc.C2: stream-parser carry state moved off the session onto the per-turn
    # TurnState (constructed fresh below), so background chats can stream text
    # in parallel without clobbering each other's partial-tag carry. No session
    # reset needed — each turn's TurnState starts clean.
    # Codex-fix M1 (2026-05-23): per-turn mutable state lives on a fresh
    # TurnState bound to a ContextVar — concurrent synthetic turns each
    # see their own. The legacy `session.tool_names_by_call`/
    # `workspace_reply_succeeded`/etc. fields remain as transitional
    # fallbacks (untouched by the new path).
    stream_ok = False
    turn_cancelled = False
    # `stream_ok` only says the generator ran to exhaustion, and exhaustion is
    # not success: `ChatSession.send` yields an ERROR chunk and then plainly
    # returns when the adapter-error breaker trips or the budget refuses the
    # turn. Two different questions hang off that, and they have different
    # answers on a turn that errored and then recovered:
    #
    #   `saw_error`     — did the model definitely READ what this turn carried?
    #                     No, if any ERROR was emitted: the one-shot injection
    #                     is cleared after the first request is built
    #                     (`chat.py::send`), so the retry that recovers no
    #                     longer contains the completion block. Committing on
    #                     that STOP would claim a result nobody was ever shown.
    #   `last_terminal` — did this turn END badly? Only if the final terminal
    #                     chunk was the ERROR. A recovered turn ended fine, and
    #                     telling the wake breaker otherwise would trip it on
    #                     turns that worked.
    saw_error = False
    last_terminal: ChunkType | None = None
    # Everything from the ContextVar `.set()` calls onward lives inside the
    # `try` so the `finally` ALWAYS runs — it emits `loop_end` and resets the
    # four ContextVars. Previously the pre-stream block (sets + loop_start +
    # `_preprocess_audio_attachments`) sat before the `try`; a preprocess or
    # send failure there skipped the reset and hung the cockpit (no loop_end).
    # The four `.set()` calls are the first statements with no `await` between
    # them, so their tokens are always bound by the time `finally` runs.
    try:
        turn_state = TurnState()
        turn_state_token = current_turn_state.set(turn_state)
        wo_token = current_workspace_origin.set(workspace_origin)
        turn_id_token = current_turn_id.set(turn_id)
        # mirror-multi-chat P2 — tag this turn's envelopes with the chat they
        # belong to so the frontend routes them to that chat's slice. inc.C2: TTS
        # suppression is now derived LIVE from this cid vs session.active_chat_id
        # (see turn_context.tts_suppressed) — no latched flag — so the voice follows
        # the active chat the moment the operator switches (D8).
        resolved_cid = cid if cid is not None else (session.active_chat_id or None)
        chat_id_token = current_chat_id.set(resolved_cid)
        # expose this CHAT turn's TurnState to out-of-turn
        # cancel paths (chat switch, barge-in, Stop, WS cleanup). Synthetic
        # workspace turns are excluded: they never emit TTS and may run
        # concurrently with the chat turn on the same chat_id, so registering
        # them would clobber the chat turn's entry.
        turn_state_key: str | None = None
        if workspace_origin is None:
            turn_state_key = resolved_cid or ""
            session.turn_states_by_chat[turn_state_key] = turn_state
        view_snapshot = session.pending_view_snapshot
        session.pending_view_snapshot = None
        loop_start_payload: dict[str, Any] = {"turn": turn}
        if workspace_origin:
            loop_start_payload["workspace_origin"] = dict(workspace_origin)
        await send_envelope(session, make_envelope("loop_start", "loop", session.session_id, loop_start_payload))
        await _ws._emit_entity_signals(app, session)
        await send_envelope(session, make_envelope(
            "stream_start", "loop", session.session_id, {"turn_id": turn_id},
        ))
        # Workstream M2 (Codex 2026-05-06): set by `_handle_chunk` when
        # the assistant's `workspace_reply` returns success during this turn. The
        # finally block uses it to commit / rollback the deferred delivery
        # flags that `chat.py::_drain_pending_suggestions` stashed.
        # Codex-fix M1 (2026-05-23): now lives on the per-turn TurnState
        # (turn_state.workspace_reply_succeeded). Session attribute below is
        # only reset for cleanup of any legacy reader that survives the
        # migration.
        session.workspace_reply_succeeded = False
        # Audio attachments → Whisper transcripts before chat_brain sees them.
        # No-op when no audio is attached.
        text, attachments = await _ws._preprocess_audio_attachments(
            app, session, text, attachments or [],
        )
        # Claim the pending voice commit for THIS turn, and empty the session
        # slot so nothing else can. Only a real chat turn may claim it —
        # synthetic workspace turns are excluded the same way they are excluded
        # from `turn_states_by_chat` above, since they produce no speech.
        # Everything before this boundary is ours (STT, queueing, retrieval);
        # everything after it, until the first speakable sentence, is prompt
        # assembly plus the provider round trip.
        # Restricted to a real, foreground chat turn: synthetic workspace turns
        # are excluded the same way `turn_states_by_chat` excludes them, and a
        # background chat turn is excluded because `tts_suppressed` will stop it
        # ever producing audio — it could only take the timestamp away from the
        # turn that will.
        if (
            workspace_origin is None
            and resolved_cid == (session.active_chat_id or None)
            and getattr(session, "voice_commit_at", None) is not None
        ):
            turn_state.voice_commit_at = session.voice_commit_at
            turn_state.voice_turn_started_at = time.monotonic()
            session.voice_commit_at = None
        async for chunk in cs.send(
            await _chat_content_for_model(text, attachments),
            transient=workspace_origin is not None,
            workspace_origin=workspace_origin,
            view_snapshot=view_snapshot,
            runtime_origin=runtime_origin,
            # The same hook the turn boundary uses below, so a turn that
            # outgrows the ceiling on its own folds and continues instead of
            # being held under it by the emergency guard. Not for synthetic
            # turns: their forked session is dropped when the turn ends.
            # Mid-turn: fold only. The boundary clears the conversation in
            # place, and the conversation being cleared would be the one this
            # very loop is speaking.
            boundary_when_needed=(
                None if workspace_origin is not None
                else lambda: _maybe_auto_compact(app, session, cs, mid_turn=True)
            ),
            # A conversation nobody has written down cannot be restored, and a
            # window restores what is on disk, so a crash in a chat's first
            # minute used to take the whole conversation with it and leave
            # recovery holding an id it could not wake.
            turn_opened=lambda: _record_the_conversation_exists(session, chat_id),
        ):
            await _handle_chunk(app, session, chunk)
            if chunk.type in (ChunkType.STOP, ChunkType.ERROR):
                last_terminal = chunk.type
                if chunk.type is ChunkType.ERROR:
                    saw_error = True
        stream_ok = True
    except asyncio.CancelledError:
        turn_cancelled = True
        await send_envelope(session, make_envelope(
            "stream_error", "loop", session.session_id, {"message": "cancelled"},
        ))
    except Exception as exc:
        log.exception("turn %d failed for session %s", turn, session.session_id)
        await send_envelope(session, make_envelope(
            "stream_error", "loop", session.session_id,
            {"message": str(exc), "reason": str(exc)},
        ))
    finally:
        # Fix pass 1 (idle-wake-design.md §G1) — observe the actual turn
        # outcome even though this function swallows Exception/CancelledError
        # into a `stream_error` envelope above rather than re-raising. Callers
        # that need to know whether the turn actually succeeded (e.g. the
        # spawn-wake breaker) pass `outcome`; existing callers omit it.
        # Fix pass 2 (2026-07-06) — `cancelled` distinguishes an operator-
        # cancelled turn from a genuine swallowed failure so the spawn-wake
        # breaker can treat a Stop-button cancel as neutral, not a failure.
        # `stream_ok` alone would report a turn whose adapter-error breaker
        # tripped as a success: it yields an ERROR chunk and then plainly
        # returns, exhausting the generator without raising. That matters
        # twice over now. The spawn-delivery gate below rolls such a turn back,
        # which re-queues the note — and `spawn_wake._wake_turn`'s straggler
        # check re-schedules a wake whenever a completion is still pending. If
        # the wake breaker also counted the failed turn as a success it would
        # never trip, and a persistently failing adapter would wake the chat
        # forever. One signal for both, so they cannot disagree.
        # `ended_clean` is the turn-level outcome (the wake breaker, and the
        # auto-compact below); `turn_committed` is the stricter delivery
        # question. They differ by exactly one case: the recovered turn.
        ended_clean = stream_ok and last_terminal is not ChunkType.ERROR
        turn_committed = stream_ok and not saw_error
        if outcome is not None:
            outcome["ok"] = ended_clean
            outcome["cancelled"] = turn_cancelled
            outcome["committed"] = turn_committed
            # Whether the runtime declined to BEGIN this turn, as opposed to
            # running it and failing: a spending cap answers here. Guarded on
            # `stream_ok` because the session's field is only written by
            # `ChatSession.send`, so without that guard a turn that blew up
            # before the stream started would report the PREVIOUS turn's
            # refusal and a real failure would go uncounted.
            from tesseract.orchestrator.outcome import RunOutcome

            outcome["refused"] = bool(
                stream_ok
                and getattr(cs, "last_turn_outcome", None) is RunOutcome.REFUSED
            )
        loop_end_payload: dict[str, Any] = {"turn": turn, "tokens_used": 0}
        if workspace_origin:
            loop_end_payload["workspace_origin"] = dict(workspace_origin)
        await send_envelope(session, make_envelope(
            "loop_end", "loop", session.session_id, loop_end_payload,
        ))
        await _ws._emit_entity_signals(app, session)
        # Auto-decay mood to neutral. Mood is per-turn — the assistant calls set_mood
        # for the current turn and it resets here, so it can't bleed into the
        # next prompt. Voice is decoupled from mood (synthesis reads per-surface
        # presets from roles.yaml), so this only affects the orb on the next turn.
        mood = app.get("mood")
        if mood is not None and hasattr(mood, "reset"):
            mood.reset()
        # Drain any TOOL_CALL_END entries whose TOOL_RESULT never landed
        # (cancelled mid-tool, adapter error). Codex-fix M1 (2026-05-23):
        # the turn-local TurnState gets dropped with the turn so per-turn
        # tool name attribution is naturally scoped — no shared-state
        # clear that could wipe another concurrent turn's pending entries.
        await _flush_tts_terminator(app, session, succeeded=stream_ok)
        # drop this turn's TurnState registration.
        # Identity-guarded so a successor turn's entry (same chat) is never
        # popped by a stale finally.
        if (
            turn_state_key is not None
            and session.turn_states_by_chat.get(turn_state_key) is turn_state
        ):
            session.turn_states_by_chat.pop(turn_state_key, None)
        # workspace_origin lives on the ContextVar (set at turn
        # start, reset below). The session attribute is the legacy fallback;
        # clear it for any code path that may still read it pre-migration.
        if hasattr(session, "workspace_origin"):
            session.workspace_origin = None
        current_turn_state.reset(turn_state_token)
        current_workspace_origin.reset(wo_token)
        current_turn_id.reset(turn_id_token)
        current_chat_id.reset(chat_id_token)
        # The spawn-delivery commit gate, on EVERY turn rather than only
        # synthetic ones: a completion is drained into iteration 0 of whatever
        # turn runs next, so the turn that read it is the turn that owes it a
        # commit. A clean stream means the model actually saw the block;
        # anything else puts the notes back at the front of the queue and
        # leaves the durable record outstanding, so the result is redelivered
        # rather than lost. No-op for a chat that drained nothing.
        try:
            if turn_committed:
                cs.confirm_spawn_delivery()
            else:
                cs.rollback_spawn_delivery()
        except Exception:
            log.exception("spawn delivery commit/rollback failed")
        # M2 commit gate: only mark workspace items delivered when the
        # synthetic turn produced a successful workspace_reply. Any
        # other outcome (cancel, adapter error, model ignored the
        # directive, no-reply) rolls back so the next drain re-includes
        # the same comments / posts and the operator's intent is not
        # silently lost.
        if workspace_origin is not None:
            try:
                if stream_ok and turn_state.workspace_reply_succeeded:
                    cs.confirm_workspace_delivery()
                else:
                    cs.rollback_workspace_delivery()
            except Exception:
                log.exception("workspace delivery commit/rollback failed")
            # Clear the `thinking…` indicator on every synthetic-turn end —
            # reply landed, rolled back, or cancelled. Never raise.
            try:
                from tesseract.workspace_events.broadcast import (
                    broadcast_thread_pending,
                )
                await broadcast_thread_pending(
                    app,
                    event_id=str(workspace_origin.get("event_id") or ""),
                    comment_id=str(workspace_origin.get("comment_id") or ""),
                    state="cleared",
                )
            except Exception:
                log.exception("workspace thread_pending(cleared) broadcast failed")
        session.workspace_reply_succeeded = False
    # Post-finally work is scoped to chat turns. Synthetic turns
    # use an ephemeral forked ChatSession that gets dropped on completion,
    # so compaction is pointless; emit_stats/turn_task/drain all belong
    # to the canonical chat lane. The synthetic spawn site cleans up its
    # own entry in `session.synthetic_turn_tasks`.
    if is_synthetic:
        return
    if ended_clean:
        await _maybe_auto_compact(app, session, cs)
    # `cid`, not the contextvar. `session_stats` is not a turn-scoped type,
    # so an unstamped envelope reaches the reader as the ACTIVE chat's
    # numbers (`stores/dispatch/session.ts`), and a background turn's floor
    # then draws itself under the name of whatever is on screen.
    await emit_stats(app, session, cs, cid)
    # Free THIS chat's task slot (active or background) so a background
    # conductor turn's completion releases its own slot, not the active one.
    # Guarded on identity, the way `send_and_await_turn` below already does
    # it: the slot may belong to a different turn by now (a drained follow-up
    # claims it while this one is still finishing), and an unconditional pop
    # would untrack the LIVE turn. Every busy check and every later stop reads
    # that slot.
    session.release_turn_slot(cid, asyncio.current_task())
    # Drain on EVERY way a turn can end: it finished, it crashed, the
    # operator stopped it. The FIFO queue must not strand remaining entries
    # because one turn errored, and it must not strand them because the
    # operator stopped the turn in front of them either — a queued message is
    # a whole next turn addressed to the session, and stopping this turn is
    # not a decision about that one (`stop.py`, property 1). A cancel used to
    # skip this, on the reasoning that `_cancel_turn` had already emptied the
    # queue; it no longer does, and skipping now would leave the operator
    # with a message the runtime had quietly decided not to answer.
    # Voice does NOT queue here: spoken follow-ups interrupt at
    # speech-start, never tail this path. Only the ACTIVE chat
    # drains its FIFO queue — it's the sole chat the operator queues into;
    # background conductor turns never populate the queue.
    #
    # Unless the SESSION ended, which is a different thing from the turn
    # ending and the one case where the queue does not survive. A teardown
    # cancels turns exactly the way a stop does, and it does not clear the
    # queue, so this tail would pop a follow-up and start a full turn against
    # a session already dropped from every registry: real model calls,
    # streaming to a closed socket, and unreachable by any later stop. Found
    # in review, not by a test, because the drain was made unconditional
    # against the stop and never checked against the other caller.
    if getattr(session, "torn_down", False):
        return
    # The drain itself (FIFO pop + stranded-inject fallback + re-entry into
    # `_start_turn`) lives in `turn_intake.drain_next`. Lazy import:
    # `turn_intake` is not needed until end-of-turn, and keeping this
    # direction lazy matches the established ws.py/turn_runner.py convention.
    from tesseract.mirror.server import turn_intake
    if cid == session.active_chat_id:
        await turn_intake.drain_next(app, session, cid)
    elif cid is not None:
        # A steer inject can strand on a BACKGROUND chat too (a steer
        # landed on it, lost the race against ITS turn ending) and
        # `drain_next`'s fallback only ever reaches the focused chat,
        # so this turn's own end must rescue its own chat_id
        # regardless of focus.
        await turn_intake.drain_stranded_background(app, session, cid)


def _resolve_chat_provider(app: web.Application, session: ServerSession, chat_id: str | None) -> str:
    """Provider name backing ``chat_id`` — the per-provider semaphore key.

    All chats currently share ``app["adapter_entry"]`` (one chat_brain role);
    reading it here keeps the key correct now and future-proof for the planned
    per-chat model override. Falls back to ``"default"`` so a stub app (tests,
    pre-boot) still gets a stable single bucket.
    """
    del session, chat_id  # reserved for per-chat model override (D3, deferred)
    entry = app.get("adapter_entry") if hasattr(app, "get") else None
    return getattr(entry, "provider", None) or "default"


def _chat_turn_provider_slot(
    app: web.Application, session: ServerSession, chat_id: str | None
):
    """Acquire a per-provider concurrency slot for a chat turn (inc.C2).

    Bounds how many turns stream against one provider at once so parallel
    background chats can't collide on its rate limit. Returns a ``nullcontext``
    when the app has no semaphore registry (test stubs / pre-boot) so unit tests
    that pass a bare ``app`` are unaffected; production boot populates
    ``app["chat_turn_semaphores"]`` + ``app["max_concurrent_chat_turns_per_provider"]``.
    """
    sems = app.get("chat_turn_semaphores") if hasattr(app, "get") else None
    if sems is None:
        return contextlib.nullcontext()
    provider = _resolve_chat_provider(app, session, chat_id)
    sem = sems.get(provider)
    if sem is None:
        cap = app["max_concurrent_chat_turns_per_provider"]
        sem = asyncio.Semaphore(cap)
        sems[provider] = sem
    return sem


async def _record_the_conversation_exists(session: Any, chat_id: str) -> None:
    """Put a chat on disk the first time it has anything in it.

    Off the loop, because it writes a file, and cheap after the first turn:
    `persist_first_turn` returns at once when a record is already there, which
    is every turn but one.
    """
    await asyncio.to_thread(chat_store.persist_first_turn, session, chat_id)


async def _run_chat_turn(
    app: web.Application,
    session: ServerSession,
    text: str,
    attachments: list[dict[str, Any]] | None = None,
    *,
    chat_id: str | None = None,
    outcome: dict[str, Any] | None = None,
    runtime_origin: str | None = None,
) -> None:
    """Run a chat (non-synthetic) turn.

    The stream lock is ACTIVE-TURN-ONLY. The stream-parser carry state lives
    on the per-turn ``TurnState`` and the suppressed-turn TTS flush is a
    no-op, so a background (non-active) chat can stream text in parallel
    without clobbering anything and takes no lock. Only the active chat takes
    ``turn_stream_lock``: that keeps voice single, because the active chat
    owns TTS and a second active-chat send waits for the first to finish, so
    audio never overlaps. `_run_turn`'s end-of-turn drain re-spawns AFTER the
    slot is freed and only *spawns* (never awaits) the follow-up, so a queued
    message can't deadlock behind the turn that drained it. Synthetic
    workspace turns do NOT use this wrapper: they call `_run_turn` directly
    and keep their own concurrency."""
    is_background = chat_id is not None and chat_id != session.active_chat_id
    async with _chat_turn_provider_slot(app, session, chat_id):
        if is_background:
            await _run_turn(
                app, session, text, attachments,
                chat_id=chat_id, outcome=outcome, runtime_origin=runtime_origin,
            )
        else:
            async with session.turn_stream_lock:
                await _run_turn(
                    app, session, text, attachments,
                    chat_id=chat_id, outcome=outcome, runtime_origin=runtime_origin,
                )


async def send_and_await_turn(
    app: web.Application,
    session: ServerSession,
    chat_id: str,
    text: str,
    attachments: list[dict[str, Any]] | None = None,
    *,
    runtime_origin: str | None = None,
) -> None:
    """Conductor relay primitive — fire a turn on ``chat_id`` and await its
    completion (the turn's ``loop_end``). Yields the event loop while the turn
    streams, so other chats stay responsive. inc.C2: a background ``chat_id``
    runs lock-free (parallel text) and stays silent (D8); the active chat takes
    the stream lock so its voice stays single.

    ``runtime_origin`` marks a turn nobody typed, so the transcript draws it as
    the runtime rather than putting the operator's name on it
    (``brain/chat.py::RUNTIME_ORIGINS``). The conductor omits it: it relays
    what the operator asked for."""
    # Lazy: `_spawn_tracked` still lives in ws.py; see the note in `_run_turn`.
    from tesseract.mirror.server import ws as _ws
    task = _ws._spawn_tracked(
        app,
        _run_chat_turn(
            app, session, text, attachments,
            chat_id=chat_id, runtime_origin=runtime_origin,
        ),
        f"chat_turn:{session.session_id}:{chat_id}",
    )
    session.current_turn_tasks[chat_id] = task
    try:
        await task
    finally:
        # `_run_turn` already pops this slot on completion; only clear it here
        # if it still points at OUR task (a drained follow-up may have replaced
        # it for the active chat).
        if session.current_turn_tasks.get(chat_id) is task:
            session.current_turn_tasks.pop(chat_id, None)


async def run_turns_concurrently(
    app: web.Application,
    session: ServerSession,
    items: list[tuple[str, str]] | list[tuple[str, str, list[dict[str, Any]] | None]],
) -> list[Any]:
    """Conductor fan-out — fire ``send_and_await_turn`` for every ``(chat_id,
    text[, attachments])`` item concurrently and await them all.

    Returns the per-item results in order; a turn that raises lands as its
    Exception in that slot (``return_exceptions=True``) so one chat failing
    never aborts the others — parallel by default, with failure isolation.
    The per-provider semaphore in ``_run_chat_turn`` still bounds how many of
    these actually stream against one provider at a time.
    """
    coros = []
    for item in items:
        chat_id, text = item[0], item[1]
        attachments = item[2] if len(item) > 2 else None
        coros.append(send_and_await_turn(app, session, chat_id, text, attachments))
    return await asyncio.gather(*coros, return_exceptions=True)


def _chat_id_of(session: ServerSession, cs: Any) -> str | None:
    """Which chat this ChatSession is, by identity rather than by assumption."""
    for candidate_id, candidate in (getattr(session, "chats", None) or {}).items():
        if candidate is cs:
            return candidate_id
    return getattr(session, "active_chat_id", "") or None


async def _maybe_auto_compact(
    app: web.Application, session: ServerSession, cs: Any = None,
    *, mid_turn: bool = False,
) -> None:
    """The cockpit's delivery, bound to the shared after-turn hook.

    Bound the chat that actually ran. A background conductor turn runs against
    a non-active ChatSession; default to the active chat for legacy callers
    that do not pass one.

    The one envelope is all this surface adds. Whether a boundary is due, the
    tally, and the `[boundary]` log entry are the runtime's, and they live in
    `after_turn` so a channel gets the same ones.
    """
    target = cs if cs is not None else session.chat_session
    # The chat that ran, by identity. A `chat_id` argument beside it would be
    # a second answer to the same question, free to disagree with the first.
    stamp = _chat_id_of(session, target)

    async def ending(reflect) -> bool:
        # The cockpit's answer to a boundary the agent reached: copy what was
        # said into its own archived record, then clear this conversation and
        # keep the thread. NOT `start_fresh_chat`, which archives the record
        # and opens a new chat and is still what the operator's own `/reset`
        # does: a boundary is not the operator asking to move on, and a new
        # chat appearing mid-work is a surprise the work did not ask for.
        # `stamp` is the chat that actually ran, which for a background turn is
        # not the one on screen, and `reflect` fires inside once the transcript
        # is safely written.
        from tesseract.mirror.server.commands import consolidate_in_place

        return await consolidate_in_place(
            app, session, chat_id=stamp, on_persisted=reflect
        )

    async def announce(text: str) -> None:
        # The cockpit had no `announce` at all, so every sentence the boundary
        # produces — the reset notice, and the continuity package, which IS
        # what the person is told — reached a phone and never a screen. That is
        # the fork the one-funnel rule is written against, and it was not a
        # transport limit: this surface carries a line of text more easily than
        # any other. `note` is a message the transcript draws as the runtime
        # rather than as the operator, the way a reloaded conversation already
        # draws one out of history.
        await send_envelope(session, make_envelope(
            "session_note", "session", session.session_id,
            {"text": text, "mark": "boundary"},
            chat_id=stamp,
        ))

    async def carry_on(text: str) -> None:
        # The turn a `continue` promised. Its body is the continuity package,
        # stamped so the transcript draws it as the runtime's: nobody typed
        # this, and the package reaching history through the turn is what
        # keeps it out of the record twice.
        #
        # `send_and_await_turn` rather than a path of its own. It is the
        # primitive this surface already has for a turn on a named chat, it
        # registers the turn where the Stop button reads, and awaiting it here
        # is safe because `handoff` spawned this whole call rather than
        # awaiting it.
        if stamp is None:
            log.info("no chat to carry the work on for session %s", session.session_id)
            return
        running = session.current_turn_tasks.get(stamp)
        if running is not None and not running.done():
            # The operator spoke while the reflection was still in flight.
            # Their turn owns the slot the Stop button reads and
            # `send_and_await_turn` claims that slot unconditionally, so the
            # carried turn waits for theirs to unwind rather than taking it
            # out of reach. `asyncio.wait` rather than awaiting the task: a
            # turn they stopped raises `CancelledError` into whoever awaits
            # it, and their stop is not a decision about this turn.
            await asyncio.wait({running})
        if getattr(session, "torn_down", False):
            # Asked again, because the wait above can be as long as a turn.
            log.info("the session ended before %s could carry the work on", stamp)
            return
        await send_and_await_turn(app, session, stamp, text, runtime_origin="carry_on")

    await after_turn(
        target, app=app, session=session,
        ending=ending, announce=announce, carry_on=carry_on,
        mid_turn=mid_turn,
    )


async def emit_stats(
    app: web.Application,
    session: ServerSession,
    cs: Any = None,
    chat_id: str | None = None,
) -> None:
    """Send this conversation's measured shape.

    `chat_id` is passed explicitly by the callers that run outside a turn, on
    connect and on a chat switch, where the turn contextvar holds nothing. The
    stamp is what lets the reader tell whose numbers these are, and leaving it
    to be inferred made a switch deliver the chat you just left.
    """
    # `.get`, not a subscript. Stats are telemetry, and this now runs on
    # connect and on a chat switch as well as after a turn — seams that exist
    # before the adapter is wired, and on which a KeyError would take down the
    # connection rather than skip a number.
    if app.get("adapter_options") is None:
        return
    # Stats for the chat that ran (a background turn targets its own
    # chat); default to the active chat for the slash-command call site.
    if cs is None:
        cs = session.chat_session
    # One measurement, shared with `context_read`. A panel that measures its
    # own copy draws a different fold from the one the runtime enforces, and
    # the assistant answering the same question from a second source is that
    # defect with a longer reach.
    try:
        report = context_report.gather(cs)
    except Exception:
        # Telemetry. A conversation that cannot be counted skips its envelope
        # rather than sending zeros the HUD would draw as an empty bar.
        log.exception("context report failed for %s", session.session_id)
        return
    if "compact_threshold_tokens" not in report:
        # `gather` guards each measurement on its own, so a report can come
        # back without the ceiling the bar is drawn against. Same call as
        # above: skip the envelope rather than send a zero the HUD would draw
        # as a conversation with all its room left.
        log.warning(
            "context report for %s carries no ceiling; stats skipped",
            session.session_id,
        )
        return
    await send_envelope(session, make_envelope(
        "session_stats", "session", session.session_id,
        {
            "tokens": report["tokens"],
            "system_tokens": report["system_tokens"],
            "turns": report["turns"],
            "compact_threshold_tokens": report["compact_threshold_tokens"],
            "compact_threshold_ratio": report["compact_threshold_ratio"],
            **{
                key: report[key]
                for key in (
                    "context_window",
                    "boundary_trigger_tokens",
                    # What the trigger is actually compared against. Without it
                    # the HUD divided the WHOLE payload by a conversation-only
                    # ceiling and drew the bar over-full, while `context_read`
                    # answered the same question correctly for the same
                    # conversation at the same moment.
                    "conversation_tokens",
                )
                if key in report
            },
        },
        chat_id=chat_id,
    ))
