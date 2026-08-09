"""TTS streaming pipeline.

Extracted from ``ws.py`` 2026-05-23 (codex audit m2 follow-up). Owns the
per-sentence chained synth, the end-of-turn terminator flush, the
voice-overage ask card, and the provider-failure toast. Reaches back
into ``ws.py`` only for two side-effecting helpers (``_spawn_tracked``
for tracked tasks and ``_emit_cost_state`` for the post-unlock HUD
refresh) via lazy imports to avoid a hard circular dependency.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import uuid
from dataclasses import replace
from typing import TYPE_CHECKING

from aiohttp import web

from tesseract.brain.cost import BudgetExhausted
from tesseract.mirror.server.envelope import (
    make_envelope,
    make_tts_chunk,
    make_voice_instruction,
)
from tesseract.mirror.server.session import send_envelope
from tesseract.mirror.server.stream_parser import (
    _split_sentences,
    _split_speak_segments,
)
from tesseract.mirror.server.turn_context import get_turn_state, tts_suppressed
from tesseract.mirror.server.voice_modes import (
    SILENT_VOICE_MODES,
    normalize_voice_mode,
)

if TYPE_CHECKING:
    from tesseract.mirror.server.session import ServerSession

log = logging.getLogger(__name__)

# Soft cap on a single Gemini TTS request — if the accumulated reply
# exceeds this, fall back to per-sentence synthesis to stay well below
# the provider's input ceiling and keep request latency bounded.
_TTS_SINGLE_SHOT_CHAR_CAP = 3000


def _preset_for_kind(kind: str) -> str:
    """Map a surface label to a synthesis preset.

    ``spoken`` carries reply content, so it takes the ``answer`` voicing —
    inheriting the clipped ``intent`` preset just because the line is short
    would make the spoken reply sound like a status announcement.
    """
    return "answer" if kind == "spoken" else kind


def _tts_state(session: "ServerSession"):
    """Resolve the owner of the 6 tts_* fields for the running task.

    inside a turn (streaming emit, chained synth tasks,
    the finally-flush) the per-turn ``TurnState`` owns TTS state — two
    concurrently-streaming chats can't clobber each other. Outside a turn
    (legacy tests driving the emit helpers directly) fall back to the
    session's same-named transitional fields.
    """
    return get_turn_state() or session


def _cancel_tts_output(session: "ServerSession") -> None:
    """Kill in-flight synth + drop buffered tails for EVERY running turn.

    Callers are out-of-turn paths (chat switch, restore, Stop button, WS
    cleanup) where the ContextVar is unset, so the running turns' states
    are reached via ``session.turn_states_by_chat``; the session's legacy
    fields are swept too for any pre-migration path.
    """
    states: list[object] = list(
        getattr(session, "turn_states_by_chat", {}).values()
    )
    states.append(session)
    for state in states:
        state.tts_buffer = ""
        synth_task = state.tts_synth_task
        state.tts_synth_task = None
        if synth_task is not None and not synth_task.done():
            synth_task.cancel()
    # The spoken latch is deliberately NOT swept off the per-turn states
    # above: a chat switch cancels audio but does not end the turn, and
    # `tts_suppressed` un-suppresses that same turn if the operator switches
    # back — clearing the latch here would let the rest of an already-muted
    # answer speak aloud, which is the exact outcome the contract exists to
    # prevent. Each turn clears its own latch at its terminator flush. Only
    # the session-level fallback needs sweeping, because that object outlives
    # turns and a cancelled turn may never reach a flush to clear it.
    session.tts_spoken_seen = False


async def _maybe_emit_tts_sentences(
    app: web.Application,
    session: "ServerSession",
    delta: str,
    *,
    kind: str = "answer",
) -> None:
    """Accumulate assistant text and synthesize every complete segment.

    Only natural assistant text reaches this path. Tool-call and
    tool-result envelopes bypass it, while short action narration and
    final answer sentences both speak in source order. ``kind`` is the
    ``<intent>``/``<spoken>``/``<answer>`` label from
    ``_split_text_for_surfaces`` and rides through to the provider as a
    preset hint (Piper picks a different voicing per kind).

    ``spoken`` is the abbreviated spoken form of a long reply. Seeing one
    latches the turn: every later ``answer`` delta still streams to screen
    but is muted from audio, so the operator hears the short version and
    reads the long one. The contract emits spoken BEFORE answer, so the
    latch is always set in time — no lookahead, no buffering, no added
    speech latency. A reply with no spoken tag is spoken verbatim exactly
    as before; a missing tag is never a failure.
    """
    state = _tts_state(session)
    # Latch BEFORE any gate below. The latch records what the model emitted,
    # not what was audible: `tts_suppressed` is dynamic, so a turn whose
    # `<spoken>` streamed while its chat sat in the background would
    # otherwise never latch, and the answer would speak in full the moment
    # the operator switched back to it mid-turn.
    if kind == "spoken":
        state.tts_spoken_seen = True
    engine = app.get("tts_engine")
    if engine is None:
        return
    mode = normalize_voice_mode(getattr(session, "voice_mode", None))
    # mirror-multi-chat inc.C — a background (non-active) chat turn streams text
    # to its slice but stays silent (D8).
    if tts_suppressed(session) or mode in SILENT_VOICE_MODES:
        return
    if kind == "answer" and getattr(state, "tts_spoken_seen", False):
        return
    # Pin the buffer kind from whichever delta opens a fresh buffer
    # segment. If a long `<intent>` text spans multiple deltas without
    # a sentence boundary, a follow-up `<answer>` delta would otherwise
    # overwrite the kind mid-fragment and the terminator-flush path
    # would synthesize the buffered intent tail with the answer voicing.
    if not (state.tts_buffer or ""):
        state.tts_buffer_kind = kind
    state.tts_buffer = (state.tts_buffer or "") + delta

    segments, tail = _split_speak_segments(state.tts_buffer)
    if not segments:
        return

    state.tts_buffer = tail
    from tesseract.mirror.server.ws import _spawn_tracked
    for segment in segments:
        prior = state.tts_synth_task
        state.tts_synth_task = _spawn_tracked(
            app,
            _chained_tts_synth(app, session, segment, prior, kind=kind),
            name=f"tts-synth:{session.session_id}",
        )


async def _chained_tts_synth(
    app: web.Application,
    session: "ServerSession",
    sentence: str,
    prior: asyncio.Task | None,
    *,
    kind: str = "answer",
) -> None:
    """Synthesize concurrently, emit serially.

    Synth is a network round-trip to Gemini Flash TTS (~200-600 ms per
    sentence). The previous "await prior, then synthesize, then emit"
    chain serialised those round-trips, so a 4-sentence reply spent
    ~4× the synth latency before the operator heard the second chunk
    — the "skipping texts" feel the operator reported.

    The fix kicks off this sentence's synth task BEFORE awaiting the
    prior chain link, so all sentences in the same delta batch synth
    in parallel. Emission still serialises (we don't send the chunk
    envelope until after ``prior`` completes) so the wire order
    matches source order and the TtsPlayer's linear timeline doesn't
    jumble. Cost is unchanged — same number of synth requests, same
    character count billed.

    Errors in the prior task don't block the current emit; a failed
    sentence shouldn't stall the whole reply.
    """
    audio_task = asyncio.create_task(_synthesize_sentence_audio(app, session, sentence, kind=kind))
    if prior is not None:
        try:
            await prior
        except (asyncio.CancelledError, Exception):
            pass
    try:
        result = await audio_task
    except asyncio.CancelledError:
        if not audio_task.done():
            audio_task.cancel()
        raise
    except BudgetExhausted as exc:
        log.info("tts budget exhausted for %s — emitting overage ask", session.session_id)
        await _emit_voice_overage_ask(app, session, exc)
        return
    except Exception as exc:
        msg = str(exc)
        if ("500" in msg or "503" in msg) and not _tts_state(session).tts_failure_notified:
            log.info("tts transient error for %s — retrying once: %s", session.session_id, msg[:120])
            await asyncio.sleep(0.4)
            try:
                result = await asyncio.create_task(_synthesize_sentence_audio(app, session, sentence, kind=kind))
            except asyncio.CancelledError:
                raise
            except BudgetExhausted as bexc:
                await _emit_voice_overage_ask(app, session, bexc)
                return
            except Exception as retry_exc:
                log.exception("tts synthesize retry failed for %s — dropping sentence", session.session_id)
                await _emit_tts_failure_instruction(session, retry_exc)
                return
        else:
            log.exception("tts synthesize failed for %s — dropping sentence", session.session_id)
            await _emit_tts_failure_instruction(session, exc)
            return
    if result is None:
        return
    audio_bytes, provider = result
    await _emit_tts_chunk(session, audio_bytes, provider, is_final=False)


async def _emit_voice_overage_ask(
    app: web.Application,
    session: "ServerSession",
    exc: BudgetExhausted,
) -> None:
    """Cost UX overhaul — when voice TTS hits its cap, drop the current
    sentence (silent skip) but ALSO ask the operator to unlock the day.

    Non-blocking: we don't await the response inside synth (would stall
    the staircase). Instead we spawn a background task that awaits the
    operator's reply; on Yes we call ``ledger.unlock_overage`` and
    future sentences synth normally. On No / timeout, voice stays silent
    until midnight.

    De-dupes against existing in-flight asks for the same scope so the
    operator doesn't get spammed with cards while the assistant tries to
    speak each subsequent sentence.
    """
    ledger = app.get("cost_ledger")
    if ledger is None:
        await send_envelope(session, make_voice_instruction(
            session.session_id,
            instruction="Voice budget reached for today; the assistant is silent until midnight.",
        ))
        return
    scope_key = exc.scope_key()
    if ledger.is_overage_unlocked(scope_key):
        return
    in_flight: set[str] = getattr(session, "_voice_overage_inflight", set())
    if scope_key in in_flight:
        return
    in_flight.add(scope_key)
    setattr(session, "_voice_overage_inflight", in_flight)

    call_id = uuid.uuid4().hex[:12]
    fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    session.pending_overage_asks[call_id] = fut

    scope_label = exc.role
    if exc.scope == "global":
        scope_label = "global daily budget"
    await send_envelope(session, make_envelope(
        "cost_overage_ask",
        "cost",
        session.session_id,
        {
            "call_id": call_id,
            "scope_key": scope_key,
            "scope_label": scope_label,
            "spent_usd": exc.spent_usd,
            "cap_usd": exc.cap_usd,
        },
    ))
    await send_envelope(session, make_voice_instruction(
        session.session_id,
        instruction="Voice budget reached. Approve in the chat to keep speaking today.",
    ))

    async def _await_response() -> None:
        try:
            approved = await asyncio.wait_for(fut, timeout=300.0)
        except asyncio.TimeoutError:
            approved = False
        finally:
            session.pending_overage_asks.pop(call_id, None)
            in_flight.discard(scope_key)
        if approved:
            ledger.unlock_overage(scope_key)
            from tesseract.mirror.server.ws import _emit_cost_state
            await _emit_cost_state(app, session)

    from tesseract.mirror.server.ws import _spawn_tracked
    _spawn_tracked(app, _await_response(), f"overage_ask:{scope_key}")


async def _emit_tts_failure_instruction(
    session: "ServerSession",
    exc: BaseException | None = None,
) -> None:
    """Surface provider failures once per turn instead of letting Speak
    mode fail silently. When ``exc`` is supplied the toast carries the
    exception type+message so the operator sees what actually broke;
    the ``log.exception`` already covered by the caller still hits the
    pulse via the log forwarder, but the toast is a faster signal.
    Budget failures use the overage UX. Suppressed when the turn was
    already cancelled — a torn-down HTTP request raising mid-flight
    isn't a real provider failure.
    """
    state = _tts_state(session)
    if getattr(state, "tts_failure_notified", False):
        return
    if session.current_turn_task is None:
        return
    state.tts_failure_notified = True
    if exc is None:
        instruction = "TTS provider failed; the assistant cannot speak this reply. Check pulse → errors."
    else:
        detail = str(exc)
        if len(detail) > 220:
            detail = detail[:217] + "..."
        instruction = f"TTS failed ({type(exc).__name__}): {detail}"
    await send_envelope(session, make_voice_instruction(
        session.session_id,
        instruction=instruction,
    ))


async def _synthesize_sentence_audio(
    app: web.Application,
    session: "ServerSession",
    sentence: str,
    *,
    kind: str = "answer",
) -> tuple[bytes, str] | None:
    """Pure synth — returns ``(audio_bytes, provider)`` or ``None`` when
    transcribe-mode gates the call. Split out from emission so multiple
    sentences can synth concurrently while the chained-emit step still
    fires ``tts_chunk`` envelopes in source order. Raises
    ``BudgetExhausted`` on cap; caller surfaces via ``voice_instruction``.
    ``kind`` selects the per-surface synthesis preset the provider was
    configured with (intent vs answer).
    """
    from tesseract.voice.text_for_speech import to_spoken_text

    spoken = to_spoken_text(sentence)
    if not spoken:
        # Sentence was pure markdown noise (e.g. a fenced code block) —
        # skip synth entirely. Both callers already treat None as a
        # transcribe-mode short-circuit, so the wire stays clean.
        return None
    engine = app["tts_engine"]
    audio_bytes, provider = await engine.synthesize(
        spoken, preset=_preset_for_kind(kind),
    )
    return audio_bytes, provider


async def _emit_tts_chunk(
    session: "ServerSession",
    audio_bytes: bytes,
    provider: str,
    *,
    is_final: bool,
) -> None:
    """Assign the next per-turn sequence number and emit a ``tts_chunk``
    envelope. Sequence assignment must happen at emission time (not
    synth time) so concurrent synth tasks that finish out-of-order
    still emit in the order their chained-emit step fires.
    """
    state = _tts_state(session)
    seq = state.tts_sequence
    state.tts_sequence += 1
    audio_b64 = base64.b64encode(audio_bytes).decode("ascii") if audio_bytes else ""
    await send_envelope(session, make_tts_chunk(
        session.session_id,
        audio_b64=audio_b64,
        provider=provider,
        sequence=seq,
        is_final=is_final,
    ))


async def _synthesize_and_emit_sentence(
    app: web.Application,
    session: "ServerSession",
    sentence: str,
    *,
    is_final: bool,
    kind: str = "answer",
) -> None:
    """Synthesize-then-emit, single-shot. Used by ``_flush_tts_terminator``
    for end-of-turn synth (the whole reply in ``speak`` mode, or each
    sentence when the reply exceeds the single-shot char cap).
    """
    if tts_suppressed(session) or normalize_voice_mode(
        getattr(session, "voice_mode", None)
    ) in SILENT_VOICE_MODES:
        return

    try:
        result = await _synthesize_sentence_audio(app, session, sentence, kind=kind)
    except BudgetExhausted as exc:
        log.info("tts budget exhausted for %s — emitting overage ask", session.session_id)
        await _emit_voice_overage_ask(app, session, exc)
        return
    except Exception as exc:
        log.exception("tts synthesize failed for %s — dropping sentence", session.session_id)
        await _emit_tts_failure_instruction(session, exc)
        return

    if result is None:
        return
    audio_bytes, provider = result
    await _emit_tts_chunk(session, audio_bytes, provider, is_final=is_final)


async def _flush_tts_terminator(
    app: web.Application,
    session: "ServerSession",
    *,
    succeeded: bool,
) -> None:
    """End-of-turn TTS flush. On success, synthesize any tail still in the
    buffer (a final partial sentence with no trailing punctuation) as
    the last audio chunk, then emit a terminator with ``is_final=True``
    so the frontend can flip out of ``speaking_back``. On cancel/error,
    only the terminator fires — partial audio is dropped.
    """
    state = _tts_state(session)
    engine = app.get("tts_engine")
    if engine is None:
        # Voice subsystem is off; no consumers to notify.
        state.tts_buffer = ""
        state.tts_sequence = 0
        state.tts_spoken_seen = False
        return
    # TTS state is per-turn now, so a suppressed
    # (background-chat) turn flushing its OWN state can't clobber the
    # active turn's audio. It still exits early — it never accumulated
    # anything (the emit gate returns early) and must not emit the
    # is_final terminator over the active chat's stream.
    if tts_suppressed(session):
        # This IS the turn's own flush, so its latch dies with it even though
        # nothing was audible — the emit path now latches before the
        # suppression gate, so a background turn can reach here holding one.
        state.tts_spoken_seen = False
        return
    if normalize_voice_mode(getattr(session, "voice_mode", None)) in SILENT_VOICE_MODES:
        state.tts_buffer = ""
        state.tts_sequence = 0
        state.tts_spoken_seen = False
        state.tts_synth_task = None
        return

    full_text = (state.tts_buffer or "").strip()
    state.tts_buffer = ""

    # Drain any in-flight synth task from the speak-mode streaming
    # staircase. Awaiting the latest chained-synth task waits for the
    # whole chain (each link awaits its predecessor before emitting).
    prior = state.tts_synth_task
    state.tts_synth_task = None
    if prior is not None:
        try:
            await prior
        except (asyncio.CancelledError, Exception):
            pass

    if succeeded and full_text:
        kind = getattr(state, "tts_buffer_kind", "answer") or "answer"
        if len(full_text) <= _TTS_SINGLE_SHOT_CHAR_CAP:
            await _synthesize_and_emit_sentence(app, session, full_text, is_final=False, kind=kind)
        else:
            sentences, tail = _split_sentences(full_text + " ")
            if tail.strip():
                sentences.append(tail.strip())
            for sentence in sentences:
                await _synthesize_and_emit_sentence(app, session, sentence, is_final=False, kind=kind)
    seq = state.tts_sequence
    state.tts_sequence = 0
    # Clear the spoken latch at the turn boundary. Production turns get a
    # fresh `TurnState` so this is structural there, but the legacy
    # session-level fallback in `_tts_state` persists across turns — left
    # set, one `<spoken>` reply would mute every answer that followed it.
    state.tts_spoken_seen = False
    await send_envelope(session, make_tts_chunk(
        session.session_id,
        audio_b64="",
        provider="",
        sequence=seq,
        is_final=True,
    ))
