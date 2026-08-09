"""Turn execution — runs a single model turn end-to-end (chat and synthetic
workspace turns), the conductor fan-out primitives (`send_and_await_turn`,
`run_turns_concurrently`), and post-turn housekeeping (auto-compact, stats).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from tesseract.brain.session_ops import auto_compact_if_needed
from tesseract.kernel.adapters.base import ChunkType
from tesseract.memory.log_notes import append_log_entry
from tesseract.paths import TESSERACT_HOME, log_dir
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
) -> None:
    # Lazy: `_emit_entity_signals` and `_preprocess_audio_attachments` still
    # live in ws.py. A module-level import here would cycle with ws.py's
    # re-export of this module's public names. `_handle_chunk` moved to
    # chunk_handler.py (SDD Task 1.3) and is imported at module level above —
    # chunk_handler doesn't depend on ws.py or turn_runner.py, so no cycle.
    from tesseract.mirror.server import ws as _ws
    # WP-2: synthetic workspace turns pass a forked ChatSession so they
    # don't mutate the canonical chat history. Chat turns leave
    # `chat_session=None` and the canonical session is used.
    is_synthetic = chat_session is not None
    # mirror-multi-chat P2 inc.C — resolve which chat this turn drives. A chat
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
    # WP-2: workspace_origin moved from session attribute (single-slot) to
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
        async for chunk in cs.send(
            await _chat_content_for_model(text, attachments),
            transient=workspace_origin is not None,
            workspace_origin=workspace_origin,
            view_snapshot=view_snapshot,
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
        # WP-2: workspace_origin now lives on the ContextVar (set at turn
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
    # WP-2: post-finally work is scoped to chat turns. Synthetic turns
    # use an ephemeral forked ChatSession that gets dropped on completion,
    # so compaction is pointless; emit_stats/turn_task/drain all belong
    # to the canonical chat lane. The synthetic spawn site cleans up its
    # own entry in `session.synthetic_turn_tasks`.
    if is_synthetic:
        return
    if ended_clean:
        await _maybe_auto_compact(app, session, cs)
    await emit_stats(app, session, cs)
    # Free THIS chat's task slot (active or background) so a background
    # conductor turn's completion releases its own slot, not the active one.
    if cid is not None:
        session.current_turn_tasks.pop(cid, None)
    else:
        session.current_turn_task = None
    # Task 4.2 (Q2) — drain on every natural completion, INCLUDING a
    # genuine crash (uncaught exception): the FIFO queue must not strand
    # remaining entries just because one turn errored. Only an explicit
    # CANCEL skips the drain — `_cancel_turn` / `_handle_voice_cancel`
    # already cleared the whole queue for this chat before cancelling the
    # task, so there's nothing left to drain; running it anyway would be a
    # no-op at best. (Audit-3 finding #2 originally gated this on
    # `stream_ok`, which also skipped drain on a plain crash — that
    # silently stranded the queue once it became FIFO instead of
    # single-slot, so the gate is now `not turn_cancelled`.)
    # Voice does NOT queue here: spoken follow-ups interrupt at
    # speech-start, never tail this path. inc.C: only the ACTIVE chat
    # drains its FIFO queue — it's the sole chat the operator queues into;
    # background conductor turns never populate the queue.
    if not turn_cancelled:
        # SDD Task 1.2 / Task 4.2: the drain itself (FIFO pop + stranded-
        # inject fallback + re-entry into `_start_turn`) lives in
        # `turn_intake.drain_next`. Lazy import: `turn_intake` is not
        # needed until end-of-turn, and keeping this direction lazy
        # matches the established ws.py/turn_runner.py cross-module
        # convention.
        from tesseract.mirror.server import turn_intake
        if cid == session.active_chat_id:
            await turn_intake.drain_next(app, session, cid)
        elif cid is not None:
            # Review fix-pass Finding 1: a Q3-steer inject can strand on a
            # BACKGROUND chat too (a steer landed on it, lost the race
            # against ITS turn ending) — `drain_next`'s fallback only ever
            # reaches the focused chat, so this turn's own end must rescue
            # its own chat_id regardless of focus.
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


async def _run_chat_turn(
    app: web.Application,
    session: ServerSession,
    text: str,
    attachments: list[dict[str, Any]] | None = None,
    *,
    chat_id: str | None = None,
    outcome: dict[str, Any] | None = None,
) -> None:
    """Run a chat (non-synthetic) turn.

    mirror-multi-chat P2 inc.C2 — the stream lock is now ACTIVE-TURN-ONLY.
    inc.C2 migrated the stream-parser carry state to the per-turn ``TurnState``
    and made the suppressed-turn TTS flush a no-op, so a background (non-active)
    chat can stream text in parallel without clobbering anything — it takes no
    lock. Only the active chat takes ``turn_stream_lock``: that keeps voice
    single (D8 — the active chat owns TTS; a second active-chat send waits for
    the first to finish, so audio never overlaps). `_run_turn`'s end-of-turn
    drain re-spawns AFTER the slot is freed and only *spawns* (never awaits) the
    follow-up, so a queued message can't deadlock behind the turn that drained
    it. Synthetic workspace turns do NOT use this wrapper (they call `_run_turn`
    directly and keep their existing WP-2 concurrency)."""
    is_background = chat_id is not None and chat_id != session.active_chat_id
    async with _chat_turn_provider_slot(app, session, chat_id):
        if is_background:
            await _run_turn(app, session, text, attachments, chat_id=chat_id, outcome=outcome)
        else:
            async with session.turn_stream_lock:
                await _run_turn(app, session, text, attachments, chat_id=chat_id, outcome=outcome)


async def send_and_await_turn(
    app: web.Application,
    session: ServerSession,
    chat_id: str,
    text: str,
    attachments: list[dict[str, Any]] | None = None,
) -> None:
    """Conductor relay primitive — fire a turn on ``chat_id`` and await its
    completion (the turn's ``loop_end``). Yields the event loop while the turn
    streams, so other chats stay responsive. inc.C2: a background ``chat_id``
    runs lock-free (parallel text) and stays silent (D8); the active chat takes
    the stream lock so its voice stays single."""
    # Lazy: `_spawn_tracked` still lives in ws.py; see the note in `_run_turn`.
    from tesseract.mirror.server import ws as _ws
    task = _ws._spawn_tracked(
        app,
        _run_chat_turn(app, session, text, attachments, chat_id=chat_id),
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
    never aborts the others (CLAUDE.md: parallel by default, failure isolation).
    The per-provider semaphore in ``_run_chat_turn`` still bounds how many of
    these actually stream against one provider at a time.
    """
    coros = []
    for item in items:
        chat_id, text = item[0], item[1]
        attachments = item[2] if len(item) > 2 else None
        coros.append(send_and_await_turn(app, session, chat_id, text, attachments))
    return await asyncio.gather(*coros, return_exceptions=True)


async def _maybe_auto_compact(
    app: web.Application, session: ServerSession, cs: Any = None
) -> None:
    # inc.C — compact the chat that actually ran. A background conductor turn
    # runs against a non-active ChatSession; default to the active chat for
    # legacy callers that don't pass one.
    target = cs if cs is not None else session.chat_session
    try:
        result = await auto_compact_if_needed(target)
    except Exception:
        log.exception("auto-compact failed for %s", session.session_id)
        return
    if result is None:
        return
    before, after = result
    await send_envelope(session, make_envelope(
        "compaction_done", "loop", session.session_id,
        {"before_tokens": before, "after_tokens": after},
    ))
    session.compact_count += 1
    try:
        now = datetime.now(timezone.utc)
        ratio_pct = round((1 - after / before) * 100, 1) if before else 0.0
        append_log_entry(
            header=f"## [auto_compact] Compaction {now.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            body=(
                f"Auto-compact fired (session={session.session_id[:8]}, turn={session.turn_count}).\n"
                f"Tokens before: {before}  |  Tokens after: {after}  |  Ratio: {ratio_pct}%"
            ),
            log_dir=log_dir("sessions"),
            date=now,
        )
    except Exception:
        log.exception("logs/sessions [auto_compact] append failed for %s", session.session_id)
    await send_envelope(session, make_envelope(
        "session_compact", "session", session.session_id,
        {"tokens_before": before, "tokens_after": after, "trigger": "auto"},
    ))


async def emit_stats(
    app: web.Application, session: ServerSession, cs: Any = None
) -> None:
    if app["adapter_options"] is None:
        return
    # inc.C — stats for the chat that ran (a background turn targets its own
    # chat); default to the active chat for the slash-command call site.
    if cs is None:
        cs = session.chat_session
    try:
        tokens = cs.token_estimate()
    except Exception:
        log.exception("token_estimate failed for %s", session.session_id)
        return
    system_tokens = 0
    if cs.system_prompt:
        try:
            system_tokens = cs.adapter.count_tokens(
                [{"role": "system", "content": cs.system_prompt}]
            )
        except Exception:
            log.exception("system token count failed for %s", session.session_id)
    context_window = cs.options.context_window
    await send_envelope(session, make_envelope(
        "session_stats", "session", session.session_id,
        {
            "tokens": tokens,
            "system_tokens": system_tokens,
            "turns": len(cs.history) // 2,
            "compact_threshold_tokens": int(context_window * cs.compact_threshold),
            "compact_threshold_ratio": cs.compact_threshold,
        },
    ))
