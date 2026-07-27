"""Per-session voice input handling.

Extracted from ``ws.py`` 2026-05-23 (codex audit m2 follow-up). Owns the
PCM accumulator and the ``voice_mode_set`` / ``voice_commit`` /
``voice_cancel`` envelope handlers. Routes successful chat-mode
transcripts back through ``_start_turn`` in ``turn_intake.py`` via a
lazy import to avoid a hard circular dependency.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from aiohttp import web

from tesseract.brain.cost import BudgetExhausted
from tesseract.mirror.server.envelope import (
    make_voice_final,
    make_voice_instruction,
    make_voice_state,
)
from tesseract.mirror.server.session import send_envelope

if TYPE_CHECKING:
    from tesseract.mirror.server.session import ServerSession

log = logging.getLogger(__name__)

# Phase 16 S2 — per-session PCM cap. 5min of 16 kHz mono 16-bit ≈ 9.6MB.
# Frames beyond this trim from the head so the buffer can't grow unbounded
# while keeping the most recent speech intact.
VOICE_PCM_BUFFER_CAP_BYTES = 9_600_000


async def _emit_voice_state(session: "ServerSession", wire: str | None) -> None:
    """Send a ``voice_state`` envelope when the loop produced a wire value
    (``None`` means no state change — skip the send)."""
    if wire is not None:
        await send_envelope(session, make_voice_state(session.session_id, wire))


async def note_voice_audio(session: "ServerSession", *, turn_active: bool) -> None:
    """SC-5 — drive the voice loop on an inbound PCM frame (binary WS path).
    Emits ``listening`` exactly once per utterance (idle → listening),
    turn-gated so ambient mic audio during TARS's reply can't flip the mic
    UI out of ``speaking_back``."""
    await _emit_voice_state(session, session.voice_loop.note_audio(turn_active=turn_active))


def _handle_voice_mode_set(session: "ServerSession", data: dict) -> None:
    """Update the per-session voice mode. Frontend sends this on HUD
    pill toggle and on (re)connect so the server gates TTS synthesis
    even for typed messages. On malformed input, KEEP the current mode
    rather than reverting to a default — flipping silent → speak on a
    bad envelope would be the worst possible failure mode (TARS speaks
    when the operator opted into silence)."""
    raw = (data or {}).get("mode")
    mode = raw.strip().lower() if isinstance(raw, str) else session.voice_mode
    if mode not in ("transcribe", "command", "speak"):
        mode = session.voice_mode
    if mode != session.voice_mode:
        log.info("voice_mode: %s -> %s for %s", session.voice_mode, mode, session.session_id)
    session.voice_mode = mode


def _accumulate_voice_pcm(session: "ServerSession", data: bytes) -> None:
    """Append a binary PCM frame to the per-session accumulator. Trims the
    head when the buffer would exceed ``VOICE_PCM_BUFFER_CAP_BYTES``."""
    if not data:
        return
    buf = session.voice_pcm_buffer
    if buf is None:
        buf = bytearray()
        session.voice_pcm_buffer = buf
    buf.extend(data)
    overflow = len(buf) - VOICE_PCM_BUFFER_CAP_BYTES
    if overflow > 0:
        del buf[:overflow]


async def _handle_voice_commit(
    app: web.Application,
    session: "ServerSession",
    data: dict | None = None,
) -> None:
    """Drain the per-session PCM buffer through ``STTEngine``; emit
    ``voice_state`` + ``voice_final`` envelopes. Behaviour branches on
    the optional ``mode`` field of the incoming envelope:

    - ``mode="chat"`` (default): on non-empty text dispatch into the
      typed-chat path so chat_brain runs with no new wiring.
    - ``mode="transcribe"``: emit only ``voice_final``; the frontend
      routes the text into the chat input for the operator to
      review/edit/send.

    STT is local Whisper primary with a Gemini Flash audio fallback
    (``roles.yaml::voice.stt``). A ``BudgetExhausted`` is surfaced as a
    ``voice_instruction`` so the operator sees the cause and the orb
    returns to idle without a partial-state.

    SC-5 — the ``voice_state`` transitions (transcribing → idle) flow
    through ``session.voice_loop`` so the speech-in half of the loop has a
    single explicit definition.
    """
    raw_mode = (data or {}).get("mode")
    msg_mode = raw_mode.strip().lower() if isinstance(raw_mode, str) else None
    session_mode = (session.voice_mode or "speak").strip().lower()
    if session_mode == "transcribe":
        mode = "transcribe"
    elif session_mode == "command":
        mode = "chat"
    elif msg_mode in ("chat", "transcribe"):
        mode = msg_mode
    else:
        mode = "chat"
    audio = bytes(session.voice_pcm_buffer or b"")
    session.voice_pcm_buffer = None
    log.info("voice_commit: %d bytes (~%.2fs) mode=%s", len(audio), len(audio) / 32_000, mode)

    engine = app.get("stt_engine")
    if engine is None:
        log.warning("voice_commit: stt_engine unavailable — emitting empty final")
        await send_envelope(session, make_voice_final(session.session_id, ""))
        await _emit_voice_state(session, session.voice_loop.finish())
        return

    if not audio:
        await send_envelope(session, make_voice_final(session.session_id, ""))
        await _emit_voice_state(session, session.voice_loop.finish())
        return

    await _emit_voice_state(session, session.voice_loop.begin_transcribe())

    text = ""
    try:
        async for chunk_text, _is_final in engine.transcribe_stream(audio):
            if chunk_text:
                text = chunk_text  # cloud provider yields one final pair
    except BudgetExhausted as exc:
        # Cost UX overhaul (2026-04-27): STT preflight routes through the
        # same overage-ask card the TTS path uses. First STT cap hit
        # aborts this transcript (no buffered audio to retry against
        # once approved), but the operator sees the confirm card and
        # subsequent voice turns proceed silently after approval.
        log.warning("voice_commit: budget exhausted (%s)", exc)
        from tesseract.mirror.server.tts import _emit_voice_overage_ask
        await _emit_voice_overage_ask(app, session, exc)
        await send_envelope(session, make_voice_final(session.session_id, ""))
        await _emit_voice_state(session, session.voice_loop.finish())
        return
    except Exception:
        log.exception("voice_commit: STT failed")
        await _emit_voice_state(session, session.voice_loop.finish())
        return

    text = (text or "").strip()
    notice = ""
    if hasattr(engine, "consume_fallback_notice"):
        try:
            notice = engine.consume_fallback_notice()
        except Exception:
            log.exception("voice_commit: consume_fallback_notice failed")
    if notice:
        await send_envelope(
            session,
            make_voice_instruction(session.session_id, instruction=notice),
        )
    await send_envelope(session, make_voice_final(session.session_id, text))
    await _emit_voice_state(session, session.voice_loop.finish())
    if text and mode == "chat":
        # Voice follows the same queueing contract as typed input
        # (2026-05-01). `_start_turn` appends a fresh transcript onto
        # `chat_queues[active_chat_id]` when a turn is already running and
        # `_run_turn`'s finally clause drains the queue at the next
        # gap — so the operator can speak while TARS is working
        # without aborting the live turn. SDD Task 1.2: `_start_turn`
        # now lives in `turn_intake.py`. Lazy import: avoids a hard
        # circular dependency at module-load time.
        from tesseract.mirror.server.turn_intake import _start_turn
        await _start_turn(app, session, {"text": text})


async def _handle_voice_cancel(
    app: web.Application,
    session: "ServerSession",
    data: dict | None = None,
) -> None:
    """Operator-side cancel. Two flavors based on ``reason``:

    - ``reason='barge_in'``: speech-start barge-in. Cancels TTS
      playback only (frontend already cancelled local audio;
      server-side TTS chain is dropped here). The chat brain turn
      keeps running so compute already in flight is not wasted; the
      new transcript arrives via ``voice_commit`` and queues onto
      ``chat_queues[active_chat_id]`` like a typed follow-up.

    - default (no reason / operator stop): full teardown — drop PCM
      buffer and return to idle. The HUD Stop button uses
      ``cancel_stream`` (not this) when it wants to abort the chat
      turn.

    The frontend already cancelled local audio playback in both cases;
    we just make sure the server side is consistent.
    """
    del app  # reserved for future hooks; keeps signature stable
    reason = (data or {}).get("reason") if isinstance(data, dict) else None
    is_barge_in = reason == "barge_in"

    # parallel-tars P6: TTS state is per-turn — sweep every running turn's
    # state (plus the legacy session fields) since barge-in / stop must
    # silence whichever chat is audible.
    states = list(getattr(session, "turn_states_by_chat", {}).values())
    states.append(session)
    for state in states:
        state.tts_buffer = ""
        synth_task = state.tts_synth_task
        state.tts_synth_task = None
        if synth_task is not None and not synth_task.done():
            synth_task.cancel()
            try:
                await synth_task
            except (asyncio.CancelledError, Exception):
                pass

    if is_barge_in:
        # The operator is speaking over TARS — the input half is live again.
        # The chat turn keeps running; the new transcript queues via the
        # next voice_commit onto `chat_queues[active_chat_id]`.
        await _emit_voice_state(session, session.voice_loop.barge_in())
        return

    session.voice_pcm_buffer = None
    await _emit_voice_state(session, session.voice_loop.cancel())
