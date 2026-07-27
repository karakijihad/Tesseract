"""mirror-multi-chat P2 inc.C2 — true parallel text streaming.

Lean migration: only the stream-parser carry state moves to the per-turn
``TurnState`` (TTS stays session-scoped, guarded by the active-turn lock). These
tests pin the per-turn isolation that makes parallel background text safe, the
no-op suppressed-flush invariant, the active-turn-only lock, the per-provider
semaphore, and the gather fan-out conductor primitive. No real model or disk.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tesseract.mirror.server.stream_parser import _split_text_for_surfaces
from tesseract.mirror.server.turn_context import (
    TurnState,
    current_chat_id,
    tts_suppressed,
)


def _plain_session() -> SimpleNamespace:
    # Non-channel session: `_split_text_for_surfaces` only reads chat_session
    # to short-circuit the channel kind; a bare namespace stays on the cockpit
    # tagged-stream path.
    return SimpleNamespace(chat_session=SimpleNamespace(session_kind="cockpit"))


def test_split_text_for_surfaces_isolates_carry_per_turn() -> None:
    """A partial tag held in turn A's carry must not leak into turn B's parse."""
    session = _plain_session()
    ts_a = TurnState()
    ts_b = TurnState()

    # Turn A receives an opening `<answer>` tag split across deltas — the
    # partial `<ans` is held in A's own carry, nothing emitted yet.
    out_a1 = _split_text_for_surfaces(session, ts_a, "<ans")
    assert out_a1 == []
    assert ts_a.stream_status_buffer == "<ans"

    # Turn B parses a complete tag concurrently. It must start fresh — A's
    # carry/state are invisible to it.
    out_b = _split_text_for_surfaces(session, ts_b, "<intent>hi</intent>")
    assert out_b == [("intent", "hi")]
    assert ts_b.stream_status_buffer == ""
    assert ts_b.stream_tag_state == "outside"

    # A completes its tag using ITS carry, untouched by B.
    out_a2 = _split_text_for_surfaces(session, ts_a, "wer>world</answer>")
    assert out_a2 == [("answer", "world")]


def test_untagged_warned_latches_per_turn_not_globally() -> None:
    """The one-warning-per-turn guard lives on each turn's state, so a second
    concurrent turn still gets its own warning rather than being suppressed by
    the first turn's latch."""
    session = _plain_session()
    ts_a = TurnState()
    ts_b = TurnState()

    _split_text_for_surfaces(session, ts_a, "bare untagged text")
    assert ts_a.stream_untagged_warned is True
    assert ts_b.stream_untagged_warned is False


# ── Dynamic voice — suppression follows the LIVE active chat ──────────


def test_tts_suppression_is_dynamic_follows_active_chat() -> None:
    """Voice is dynamic: a turn speaks only while ITS chat is the live active
    chat. Switching the active chat away mid-turn silences it immediately
    (without restarting the turn); switching back un-silences it."""
    session = SimpleNamespace(active_chat_id="A")
    token = current_chat_id.set("A")  # this turn belongs to chat A
    try:
        assert tts_suppressed(session) is False  # A is active → speaks
        session.active_chat_id = "B"  # operator switches away, A still streaming
        assert tts_suppressed(session) is True  # silenced live
        session.active_chat_id = "A"  # switch back
        assert tts_suppressed(session) is False  # speaks again
    finally:
        current_chat_id.reset(token)


def test_tts_not_suppressed_outside_a_turn() -> None:
    session = SimpleNamespace(active_chat_id="A")
    assert tts_suppressed(session) is False  # no current_chat_id → legacy/synthetic, audible


# ── TTS single-writer invariant — suppressed flush must not clobber ───


@pytest.mark.asyncio
async def test_suppressed_flush_leaves_active_tts_state_untouched() -> None:
    """A background (TTS-suppressed) turn ends and runs `_flush_tts_terminator`.
    Because TTS stays session-scoped, that flush MUST NOT reset the buffer /
    sequence / synth task — those belong to the active turn still speaking."""
    from tesseract.mirror.server.tts import _flush_tts_terminator

    session = SimpleNamespace(
        tts_buffer="active turn partial sentence",
        tts_buffer_kind="answer",
        tts_sequence=7,
        tts_synth_task=None,
        voice_mode="speak",
        session_id="sess-inc-c2",
        active_chat_id="active",
    )
    app = {"tts_engine": object()}

    token = current_chat_id.set("bg")  # this turn's chat is NOT the active chat
    try:
        await _flush_tts_terminator(app, session, succeeded=True)
    finally:
        current_chat_id.reset(token)

    # The active turn's in-flight TTS state survives the background turn's flush.
    assert session.tts_buffer == "active turn partial sentence"
    assert session.tts_sequence == 7


# ── tts_chunk routing — audio carries its chat so the UI plays it on the ──
# right chat (only the active chat speaks, but the wire still needs the id) ─


def test_tts_chunk_carries_active_turn_chat_id() -> None:
    from tesseract.mirror.server.envelope import make_tts_chunk
    from tesseract.mirror.server.turn_context import current_chat_id

    token = current_chat_id.set("chat-xyz")
    try:
        env = make_tts_chunk(
            "sess", audio_b64="", provider="piper", sequence=0, is_final=True
        )
    finally:
        current_chat_id.reset(token)
    assert env["chat_id"] == "chat-xyz"


def test_tts_chunk_unrouted_outside_a_turn() -> None:
    from tesseract.mirror.server.envelope import make_tts_chunk

    env = make_tts_chunk(
        "sess", audio_b64="", provider="piper", sequence=0, is_final=True
    )
    # No active turn → session-scoped voice, no chat routing key.
    assert "chat_id" not in env
