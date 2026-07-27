"""parallel-tars P6 — per-chat (per-turn) TTS state.

Two concurrent turns, each with its own TurnState bound via the
`current_turn_state` ContextVar, must accumulate / sequence / flush TTS
independently — the deferred "Per-chat TTS state" race. Also covers the
legacy fallback (no TurnState → session fields) and the out-of-turn
cancel sweep through `turn_states_by_chat`.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tesseract.mirror.server.tts import _cancel_tts_output, _tts_state
from tesseract.mirror.server.turn_context import TurnState, current_turn_state


def _fake_session() -> SimpleNamespace:
    """Duck-typed ServerSession carrying only the fields the TTS state
    layer touches (legacy fallbacks + the per-turn state map)."""
    return SimpleNamespace(
        tts_buffer="",
        tts_buffer_kind="answer",
        tts_sequence=0,
        tts_synth_task=None,
        tts_voice_params=None,
        tts_failure_notified=False,
        turn_states_by_chat={},
    )


@pytest.mark.asyncio
async def test_concurrent_turns_own_independent_tts_state():
    session = _fake_session()

    async def _turn(chat_id: str, text: str) -> TurnState:
        state = TurnState()
        token = current_turn_state.set(state)
        try:
            resolved = _tts_state(session)
            assert resolved is state
            resolved.tts_buffer += text
            resolved.tts_sequence += 1
            await asyncio.sleep(0.01)
            # The other turn ran meanwhile — our state must be untouched.
            assert resolved.tts_buffer == text
            assert resolved.tts_sequence == 1
            return resolved
        finally:
            current_turn_state.reset(token)

    state_a, state_b = await asyncio.gather(
        _turn("chat-a", "hello from A. "),
        _turn("chat-b", "hello from B. "),
    )
    assert state_a is not state_b
    # Session legacy fields never touched by the new path.
    assert session.tts_buffer == ""
    assert session.tts_sequence == 0


def test_no_turn_state_falls_back_to_session():
    session = _fake_session()
    assert current_turn_state.get() is None
    assert _tts_state(session) is session


@pytest.mark.asyncio
async def test_flush_of_one_turn_leaves_other_buffer_intact():
    """Turn A's REAL end-of-turn flush must not clear turn B's still-
    accumulating buffer. M10: exercise the actual ``_flush_tts_terminator``
    (voice off → engine None → it resets only the flushing turn's per-turn
    state) instead of hand-clearing the fields."""
    from tesseract.mirror.server.tts import _flush_tts_terminator

    session = _fake_session()
    state_a, state_b = TurnState(), TurnState()
    session.turn_states_by_chat = {"a": state_a, "b": state_b}

    state_a.tts_buffer = "turn A tail"
    state_a.tts_sequence = 2
    state_b.tts_buffer = "turn B partial"
    state_b.tts_sequence = 3

    # Flush turn A through the real code path (its state bound via the ctxvar).
    app = {"tts_engine": None}  # voice subsystem off → flush just resets state
    token = current_turn_state.set(state_a)
    try:
        await _flush_tts_terminator(app, session, succeeded=True)
    finally:
        current_turn_state.reset(token)

    assert state_a.tts_buffer == ""
    assert state_a.tts_sequence == 0
    assert state_b.tts_buffer == "turn B partial"
    assert state_b.tts_sequence == 3


@pytest.mark.asyncio
async def test_cancel_sweeps_all_running_turn_states():
    session = _fake_session()
    state_a, state_b = TurnState(), TurnState()
    session.turn_states_by_chat = {"a": state_a, "b": state_b}

    async def _pending() -> None:
        await asyncio.Event().wait()

    state_a.tts_buffer = "queued A"
    state_a.tts_synth_task = asyncio.create_task(_pending())
    state_b.tts_buffer = "queued B"
    state_b.tts_synth_task = asyncio.create_task(_pending())
    session.tts_buffer = "legacy"

    _cancel_tts_output(session)
    await asyncio.sleep(0)  # let cancellations propagate

    for state in (state_a, state_b, session):
        assert state.tts_buffer == ""
        assert state.tts_synth_task is None
