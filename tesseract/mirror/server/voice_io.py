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
import time
from typing import TYPE_CHECKING

from aiohttp import web

from tesseract.brain.cost import BudgetExhausted
from tesseract.mirror.server.envelope import (
    make_voice_discarded,
    make_voice_final,
    make_voice_instruction,
    make_voice_state,
    make_voice_woken,
)
from tesseract.mirror.server.session import send_envelope
from tesseract.mirror.server.voice_modes import (
    VOICE_MODES,
    destination_for,
    normalize_voice_mode,
)
from tesseract.mirror.server.wake_word import reset_wake_stream, wake_verdict

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


async def note_voice_audio(
    app: web.Application,
    session: "ServerSession",
    data: bytes,
    *,
    turn_active: bool,
) -> None:
    """Everything one inbound PCM frame causes, in the order it must happen.

    Three things, and the ordering between the first two is load-bearing: the
    frame is buffered for transcription, then offered to the wake decoder, and
    only then does the voice loop advance.

    **The wake decoder runs here rather than at commit.** It is a streaming
    decoder, and using it on the finished buffer meant nothing could know
    whether an utterance had woken the assistant until the operator stopped
    talking — so a minute of speech could be refused a minute after the phrase
    that should have started it, with no way to tell early. Deciding per frame
    costs 3.46 ms for the 100 ms frames the capture path sends (measured), and
    the operator learns mid-sentence.

    SC-5: emits ``listening`` exactly once per utterance (idle → listening),
    turn-gated so ambient mic audio during the assistant's reply can't flip the
    mic UI out of ``speaking_back``.
    """
    _accumulate_voice_pcm(session, data)

    from tesseract.mirror.server.wake_word import note_wake_audio

    if note_wake_audio(app, session, data):
        # The edge, not the state: this is true once per utterance, and it is
        # the moment the operator may keep talking knowing they were heard.
        #
        # Logged as well as sent, because the discard line alone cannot be
        # counted. A refusal carries no transcript by design, so a log with
        # only refusals in it cannot distinguish the gate missing a phrase
        # from the operator never saying one — and those are the two numbers
        # the wake word is judged on. The pair makes the log self-scoring:
        # every gated utterance leaves exactly one of these two lines.
        log.info(
            "voice_commit: wake word heard %.2fs into the utterance",
            len(session.voice_pcm_buffer or b"") / 32_000,
        )
        await send_envelope(session, make_voice_woken(session.session_id))

    await _emit_voice_state(session, session.voice_loop.note_audio(turn_active=turn_active))


def _handle_voice_mode_set(session: "ServerSession", data: dict) -> None:
    """Update the per-session voice mode. Frontend sends this on HUD
    pill toggle and on (re)connect so the server gates TTS synthesis
    even for typed messages. On malformed input, KEEP the current mode
    rather than reverting to a default — flipping silent → speak on a
    bad envelope would be the worst possible failure mode (the assistant speaks
    when the operator opted into silence)."""
    raw = (data or {}).get("mode")
    mode = raw.strip().lower() if isinstance(raw, str) else session.voice_mode
    if mode not in VOICE_MODES:
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
      review/edit/send. The session's ``terminal`` mode resolves here
      too — same server contract, different frontend destination.

    AS-2 — in ``chat`` mode the transcript passes the wake-word gate
    before it becomes a turn. A refusal emits ``voice_discarded``
    *instead of* ``voice_final``, because a final in a dispatching mode
    is what puts the operator's words on screen as a chat bubble; a
    discarded utterance must leave no bubble waiting for a reply that
    will never come.

    STT is local Whisper primary with a Gemini Flash audio fallback
    (``roles.yaml::voice.stt``). A ``BudgetExhausted`` is surfaced as a
    ``voice_instruction`` so the operator sees the cause and the orb
    returns to idle without a partial-state.

    SC-5 — the ``voice_state`` transitions (transcribing → idle) flow
    through ``session.voice_loop`` so the speech-in half of the loop has a
    single explicit definition.
    """
    session_mode = normalize_voice_mode(session.voice_mode)
    # Resolved once, before the transcription await, and carried on every
    # exit — including the empty ones, since a final without it would fall
    # back to the frontend value the field exists to stop trusting.
    #
    # `mode` is derived FROM `destination`, not computed beside it. They
    # were once resolved from different sources — the session for one, the
    # envelope's `mode` field for the other — and could therefore disagree:
    # a `speak` session receiving a stale `transcribe` payload emitted a
    # chat-destination final while skipping the dispatch, so the operator
    # saw their words become a chat bubble that the assistant never answered.
    destination = destination_for(session_mode)
    mode = "chat" if destination == "chat" else "transcribe"
    raw_mode = (data or {}).get("mode")
    msg_mode = raw_mode.strip().lower() if isinstance(raw_mode, str) else None
    if msg_mode is not None and msg_mode != mode:
        # The session is authoritative — it is set on mount, on every mode
        # change and on reconnect. A disagreeing payload is a stale client,
        # and honouring it is what let the two halves diverge.
        # Both values named explicitly: `msg_mode` is a routing hint and
        # `session_mode` is a mic mode, so printing them side by side
        # without the resolution in between reads as a mismatch between
        # two things that were never the same kind.
        log.info(
            "voice_commit: envelope says mode=%s, but session mode=%s "
            "resolves to %s — using the session",
            msg_mode,
            session_mode,
            mode,
        )
    audio = bytes(session.voice_pcm_buffer or b"")
    session.voice_pcm_buffer = None
    # The decoder stream describes the same utterance as that buffer, so the
    # two are released together. The chat branch below reads the verdict off
    # the session first; the silent modes never gate, and a stream left behind
    # by one of them would carry decoder state into the next utterance — a
    # phrase said once could then wake twice.
    if mode != "chat":
        reset_wake_stream(session)
    log.info("voice_commit: %d bytes (~%.2fs) mode=%s", len(audio), len(audio) / 32_000, mode)
    # End of speech. Held locally and only published to the session once a
    # chat turn is actually dispatched — see the bottom of this function. Most
    # of the paths below never start a turn (no engine, no audio, STT failure,
    # wake-word rejection, transcribe mode, empty transcript), and publishing
    # here would leave a timestamp standing that the next unrelated turn would
    # pick up and report as its own.
    commit_at = time.monotonic()

    engine = app.get("stt_engine")
    if engine is None:
        log.warning("voice_commit: stt_engine unavailable — emitting empty final")
        await send_envelope(
            session,
            make_voice_final(session.session_id, "", destination=destination),
        )
        await _emit_voice_state(session, session.voice_loop.finish())
        return

    if not audio:
        await send_envelope(
            session,
            make_voice_final(session.session_id, "", destination=destination),
        )
        await _emit_voice_state(session, session.voice_loop.finish())
        return

    await _emit_voice_state(session, session.voice_loop.begin_transcribe())

    # THE GATE RUNS BEFORE TRANSCRIPTION, and that ordering is the point.
    # The old gate matched text, so it had to transcribe first; this one
    # decides from audio and needs no transcript, which means speech that did
    # not address the assistant never reaches an STT engine at all — including
    # the cloud fallback `roles.yaml::voice.stt.fallbacks` ships by default,
    # where a local-Whisper outage would otherwise have sent ambient room audio
    # to a provider. It also stops paying transcription latency and cost on
    # every utterance the gate was going to throw away.
    if mode == "chat":
        # Read, not decided. The live feed already decided while the operator
        # was speaking — which is what let them be told mid-sentence — so this
        # is the same verdict they have already seen, not a second opinion.
        decision = wake_verdict(app, session)
        reset_wake_stream(session)
        if not decision.matched:
            audio_seconds = len(audio) / 32_000
            log.info(
                "voice_commit: wake-word gate discarded utterance "
                "(%.2fs of audio, not transcribed)",
                audio_seconds,
            )
            await send_envelope(
                session,
                make_voice_discarded(session.session_id, audio_seconds=audio_seconds),
            )
            await _emit_voice_state(session, session.voice_loop.finish())
            return

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
        await send_envelope(
            session,
            make_voice_final(session.session_id, "", destination=destination),
        )
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

    await send_envelope(
        session,
        make_voice_final(session.session_id, text, destination=destination),
    )
    await _emit_voice_state(session, session.voice_loop.finish())
    if text and mode == "chat":
        # Voice follows the same queueing contract as typed input
        # (2026-05-01). `_start_turn` appends a fresh transcript onto
        # `chat_queues[active_chat_id]` when a turn is already running and
        # `_run_turn`'s finally clause drains the queue at the next
        # gap — so the operator can speak while the assistant is working
        # without aborting the live turn. SDD Task 1.2: `_start_turn`
        # now lives in `turn_intake.py`. Lazy import: avoids a hard
        # circular dependency at module-load time.
        from tesseract.mirror.server.turn_intake import _start_turn
        # End of speech rides WITH the payload, the way `view_snapshot` does,
        # and `_start_turn` re-carries it through the queue on drain. A
        # session-wide slot could be claimed by whatever turn happened to start
        # next — a typed message already queued ahead of this transcript drains
        # first — and would outlive a payload that `_start_turn` rejects on
        # queue overflow.
        await _start_turn(
            app, session, {"text": text, "voice_commit_at": commit_at},
        )


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

    # TTS state is per-turn — sweep every running turn's
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
        # The operator is speaking over the assistant — the input half is live again.
        # The chat turn keeps running; the new transcript queues via the
        # next voice_commit onto `chat_queues[active_chat_id]`.
        await _emit_voice_state(session, session.voice_loop.barge_in())
        return

    session.voice_pcm_buffer = None
    reset_wake_stream(session)
    # A cancelled voice turn never reaches its first audio chunk, so an
    # unclaimed commit timestamp would survive and be reported against whatever
    # turn came next — including a typed one.
    session.voice_commit_at = None
    await _emit_voice_state(session, session.voice_loop.cancel())
