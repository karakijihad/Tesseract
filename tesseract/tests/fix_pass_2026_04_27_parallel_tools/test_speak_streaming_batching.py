"""Speak-mode sentence streaming.

Pins:
- Complete sentence/paragraph segments synthesize as soon as they are
  available.
- Paragraph breaks are preferred boundaries; sentence boundaries are
  the fallback for long single paragraphs.
- Subsequent deltas in the same turn keep flushing at the next boundary.
- The end-of-turn flush synthesizes only the post-streaming tail.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiohttp import web

from tesseract.kernel.adapters.base import ChunkType, StreamChunk
from tesseract.mirror.server import ws as ws_module
from tesseract.mirror.server.stream_parser import _split_speak_segments
from tesseract.mirror.server.tts import _flush_tts_terminator, _maybe_emit_tts_sentences


def _build_app(*, tts_engine=None, voice_state=None, mood=None) -> web.Application:
    app = web.Application()
    app["mood"] = mood
    app["adapter_options"] = SimpleNamespace(tier="api")
    policy = MagicMock()
    policy.resolve_posture.return_value = "auto"
    app["config"] = SimpleNamespace(permissions=policy)
    app["tts_engine"] = tts_engine
    app["voice_state"] = voice_state
    return app


def _build_session(session_id: str = "sess-stream"):
    ws = MagicMock()
    ws.send_json = AsyncMock()
    ws.closed = False
    return SimpleNamespace(
        session_id=session_id,
        ws=ws,
        event_log=MagicMock(append=MagicMock()),
        tool_names_by_call={},
        tts_buffer="",
        tts_sequence=0,
        tts_synth_task=None,
        voice_mode="speak",
    )


def _sent(session) -> list[dict]:
    return [c.args[0] for c in session.ws.send_json.await_args_list]


def _tts_chunks(session) -> list[dict]:
    return [e for e in _sent(session) if e["type"] == "tts_chunk"]


# ── _split_speak_segments ──────────────────────────────────────────


def test_split_speak_drains_paragraph_and_sentences():
    """Splits at every available boundary — sentence inside, paragraph
    between — so each completed segment streams as soon as it's ready."""
    segs, tail = _split_speak_segments(
        "First sentence. Second one.\n\nNew paragraph here."
    )
    assert segs == ["First sentence.", "Second one.", "New paragraph here."]
    assert tail == ""


def test_split_speak_paragraph_only_when_no_sentence_inside():
    """A paragraph break with no internal sentence boundary still flushes
    the paragraph as a single segment."""
    segs, tail = _split_speak_segments("standalone block\n\ntail block")
    assert segs == ["standalone block"]
    assert tail == "tail block"


def test_split_speak_falls_back_to_sentence_within_paragraph():
    segs, tail = _split_speak_segments(
        "First. Second. Third tail"
    )
    assert segs == ["First.", "Second."]
    assert tail == "Third tail"


def test_split_speak_no_boundary_returns_empty():
    segs, tail = _split_speak_segments("no boundaries here yet")
    assert segs == []
    assert tail == "no boundaries here yet"


def test_split_speak_handles_missing_space_after_sentence():
    segs, tail = _split_speak_segments("Checking the place.In this chat")
    assert segs == ["Checking the place."]
    assert tail == "In this chat"


def test_split_speak_drops_whitespace_only_segments():
    """Empty segments (between back-to-back paragraph breaks) are dropped."""
    segs, tail = _split_speak_segments("first\n\n\n\nlast block")
    assert segs == ["first"]
    assert tail == "last block"


# ── streaming-trigger behaviour ────────────────────────────────────


async def test_short_reply_streams_complete_sentence_mid_turn():
    """Reply below `_TTS_STREAMING_TRIGGER_CHARS` synthesizes once at
    `_flush_tts_terminator` — no mid-turn `tts_chunk` envelopes."""
    tts_engine = MagicMock()
    tts_engine.synthesize = AsyncMock(return_value=(b"\x00\x01", "gemini_flash_tts"))
    voice_state = SimpleNamespace(voice_id="Charon", tone_prompt="")
    app = _build_app(tts_engine=tts_engine, voice_state=voice_state)
    session = _build_session()

    await _maybe_emit_tts_sentences(app, session, "Hello there. Short reply.")

    # No mid-turn synth — short replies wait.
    if session.tts_synth_task is not None:
        await session.tts_synth_task
    chunks = _tts_chunks(session)
    assert len(chunks) == 2
    assert tts_engine.synthesize.await_count == 2
    assert [c.args[0] for c in tts_engine.synthesize.await_args_list] == [
        "Hello there.",
        "Short reply.",
    ]
    assert session.tts_buffer == ""

    await _flush_tts_terminator(app, session, succeeded=True)

    # End-of-turn: one audio chunk + one terminator.
    chunks = _tts_chunks(session)
    assert len(chunks) == 3
    assert chunks[-1]["data"]["is_final"] is True
    # Single-shot synth carries the whole reply.
    calls = tts_engine.synthesize.await_args_list
    assert [c.args[0] for c in calls] == ["Hello there.", "Short reply."]


async def test_long_reply_with_paragraph_streams_at_paragraph_boundary():
    """Once a paragraph break appears past the trigger, the segment
    before it streams immediately. The tail synthesizes at end-of-turn."""
    tts_engine = MagicMock()
    tts_engine.synthesize = AsyncMock(return_value=(b"\x10", "gemini_flash_tts"))
    voice_state = SimpleNamespace(voice_id="Charon", tone_prompt="")
    app = _build_app(tts_engine=tts_engine, voice_state=voice_state)
    session = _build_session()

    # >600 chars, ends with a paragraph break that should fire streaming.
    head = "x" * 605
    await _maybe_emit_tts_sentences(app, session, f"{head}\n\n")
    # Drain the streaming staircase task so its emit fires.
    if session.tts_synth_task is not None:
        await session.tts_synth_task

    chunks = _tts_chunks(session)
    assert len(chunks) == 1, "first paragraph segment should have streamed"
    assert chunks[0]["data"]["is_final"] is False
    assert tts_engine.synthesize.await_args_list[0].args[0] == head

    # More text arrives; the tail goes out at end-of-turn.
    await _maybe_emit_tts_sentences(app, session, "Closing line.")
    await _flush_tts_terminator(app, session, succeeded=True)

    chunks = _tts_chunks(session)
    # 1 streamed paragraph + 1 tail audio + 1 terminator = 3.
    assert len(chunks) == 3
    assert chunks[-1]["data"]["is_final"] is True
    # Tail synthesized exactly the closing line — `\n\n` already advanced
    # the cursor past the boundary, so it's not part of the tail.
    tail_call = tts_engine.synthesize.await_args_list[1]
    assert tail_call.args[0] == "Closing line."


async def test_long_reply_no_paragraph_streams_at_sentence_boundary():
    """Long reply without paragraph breaks still streams once the trigger
    is crossed — sentence boundary is the fallback."""
    tts_engine = MagicMock()
    tts_engine.synthesize = AsyncMock(return_value=(b"\x20", "gemini_flash_tts"))
    voice_state = SimpleNamespace(voice_id="Charon", tone_prompt="")
    app = _build_app(tts_engine=tts_engine, voice_state=voice_state)
    session = _build_session()

    long_sentence = "x" * 605  # cross _TTS_STREAMING_TRIGGER_CHARS (600)
    await _maybe_emit_tts_sentences(app, session, f"{long_sentence}. tail")
    if session.tts_synth_task is not None:
        await session.tts_synth_task

    chunks = _tts_chunks(session)
    assert len(chunks) == 1
    assert tts_engine.synthesize.await_args_list[0].args[0] == f"{long_sentence}."
    # The fragment after the period stays in the buffer for the tail.
    assert session.tts_buffer == "tail"


async def test_streaming_continues_for_subsequent_deltas():
    """Once streaming kicks in, every subsequent boundary fires a chunk —
    no need to re-cross the trigger."""
    tts_engine = MagicMock()
    tts_engine.synthesize = AsyncMock(return_value=(b"\x30", "gemini_flash_tts"))
    voice_state = SimpleNamespace(voice_id="Charon", tone_prompt="")
    app = _build_app(tts_engine=tts_engine, voice_state=voice_state)
    session = _build_session()

    # First delta crosses threshold and ends with a sentence.
    head = "y" * 605
    await _maybe_emit_tts_sentences(app, session, f"{head}. ")
    if session.tts_synth_task is not None:
        await session.tts_synth_task

    # Now the streaming flag is implicitly on (synth task chained).
    # A short follow-up sentence should still flush — well under trigger,
    # but streaming-mode keeps draining boundaries.
    await _maybe_emit_tts_sentences(app, session, "Short one. ")
    if session.tts_synth_task is not None:
        await session.tts_synth_task

    chunks = _tts_chunks(session)
    assert len(chunks) == 2
    synth_inputs = [c.args[0] for c in tts_engine.synthesize.await_args_list]
    assert synth_inputs == [f"{head}.", "Short one."]


async def test_streaming_chains_synth_tasks_serially_in_emit_order():
    """Multiple segments in one delta synth concurrently but emit in
    source order via the chained-synth pattern."""
    # Synth holds the call until released, so we can pin the chain.
    pending = [asyncio.Event(), asyncio.Event()]
    call_idx = {"i": 0}

    async def fake_synth(text, params):
        idx = call_idx["i"]
        call_idx["i"] += 1
        await pending[idx].wait()
        return (b"\x40", "gemini_flash_tts")

    tts_engine = MagicMock()
    tts_engine.synthesize = AsyncMock(side_effect=fake_synth)
    voice_state = SimpleNamespace(voice_id="Charon", tone_prompt="")
    app = _build_app(tts_engine=tts_engine, voice_state=voice_state)
    session = _build_session()

    head = "z" * 605
    # Two paragraph segments in the same delta.
    await _maybe_emit_tts_sentences(
        app, session, f"{head}\n\nSecond paragraph.\n\n"
    )

    # Both segments should have spawned synth tasks; nothing emitted yet.
    await asyncio.sleep(0)
    assert tts_engine.synthesize.await_count == 0  # synths still blocked
    assert _tts_chunks(session) == []

    # Release the SECOND synth first; emit must still wait for the first.
    pending[1].set()
    await asyncio.sleep(0)
    assert _tts_chunks(session) == []

    # Release the first; both should drain in source order.
    pending[0].set()
    if session.tts_synth_task is not None:
        await session.tts_synth_task

    chunks = _tts_chunks(session)
    assert len(chunks) == 2
    assert chunks[0]["data"]["sequence"] == 0
    assert chunks[1]["data"]["sequence"] == 1


async def test_transcribe_mode_never_streams():
    """Transcribe mode short-circuits before any synth, regardless of length."""
    tts_engine = MagicMock()
    tts_engine.synthesize = AsyncMock(return_value=(b"\x00", "gemini_flash_tts"))
    voice_state = SimpleNamespace(voice_id="Charon", tone_prompt="")
    app = _build_app(tts_engine=tts_engine, voice_state=voice_state)
    session = _build_session()
    session.voice_mode = "transcribe"

    long = "a" * 600
    await _maybe_emit_tts_sentences(app, session, f"{long}.\n\nNext.")

    assert session.tts_buffer == ""  # transcribe never even buffers
    assert session.tts_synth_task is None
    assert _tts_chunks(session) == []
    assert tts_engine.synthesize.await_count == 0


async def test_handle_chunk_streams_long_reply_via_text_chunks():
    """Verify `_handle_chunk` correctly drives the streaming staircase
    when long text comes in over multiple TEXT chunks."""
    tts_engine = MagicMock()
    tts_engine.synthesize = AsyncMock(return_value=(b"\x50", "gemini_flash_tts"))
    voice_state = SimpleNamespace(voice_id="Charon", tone_prompt="")
    app = _build_app(tts_engine=tts_engine, voice_state=voice_state)
    session = _build_session()

    head = "w" * 605
    await ws_module._handle_chunk(app, session, StreamChunk(type=ChunkType.TEXT, text=head))
    # Buffer below boundary — nothing flushes yet.
    await ws_module._handle_chunk(app, session, StreamChunk(type=ChunkType.TEXT, text=". "))
    if session.tts_synth_task is not None:
        await session.tts_synth_task
    # First sentence streams.
    assert tts_engine.synthesize.await_count == 1

    await ws_module._handle_chunk(app, session, StreamChunk(type=ChunkType.TEXT, text="Tail."))
    await _flush_tts_terminator(app, session, succeeded=True)

    # 1 streamed + 1 tail = 2 synths, plus terminator.
    assert tts_engine.synthesize.await_count == 2
    chunks = _tts_chunks(session)
    assert chunks[-1]["data"]["is_final"] is True


# NOTE: the regex-heuristic stream classifier was replaced 2026-04-29 with
# a tag state machine over `<intent>...</intent>` / `<answer>...</answer>`.
# Coverage for the new parser lives in
# `tests/fix_pass_2026_04_29_tagged_stream/test_tagged_stream_parser.py`.
# The five `test_stream_text_*` cases that used to live here asserted the
# old regex behavior (raw prose → classified by leading verb) and are now
# obsolete — they tested an interface the model is no longer asked to use.
