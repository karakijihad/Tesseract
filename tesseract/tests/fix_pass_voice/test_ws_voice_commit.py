"""Phase 16 S2: WS voice path — binary PCM accumulator + voice_commit /
voice_cancel envelopes.

Drives `_accumulate_voice_pcm`, `_handle_voice_commit`, and
`_handle_voice_cancel` directly with a fake `STTEngine` and a captured
`send_envelope` to assert the wire-level shape and the dispatch into
`_start_turn`. No aiohttp test client needed — the helpers operate on a
plain `ServerSession` shaped through dataclass kwargs.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from tesseract.mirror.server import ws as ws_module
from tesseract.mirror.server import turn_intake
from tesseract.mirror.server.tts import _synthesize_and_emit_sentence
from tesseract.mirror.server.voice_io import (
    VOICE_PCM_BUFFER_CAP_BYTES,
    _accumulate_voice_pcm,
    _handle_voice_cancel,
    _handle_voice_commit,
    _handle_voice_mode_set,
)
from tesseract.mirror.server.voice_loop import VoiceLoop


class _Session:
    """Minimal ServerSession stand-in. Only the fields the voice helpers
    touch are populated. The dataclass would work too, but the chat-session
    machinery makes it heavier than needed for these unit tests."""

    def __init__(self) -> None:
        self.session_id = "sess-test"
        self.voice_pcm_buffer: bytearray | None = None
        # SC-5 — the voice helpers route `voice_state` through the loop.
        self.voice_loop = VoiceLoop()
        self.voice_mode = "speak"
        self.tts_sequence = 0
        self.tts_buffer = ""
        # G3 — Live-loop fields read by `_handle_voice_cancel` and
        # related paths. Default `None` so the no-live branch is taken.
        self.live_session = None
        self.live_listen_task = None
        self.live_inactivity_task = None
        # G3 — `_handle_voice_cancel` now also cancels the per-sentence
        # synth chain so chunks from a cancelled turn don't bleed onto
        # the wire. None when no synth is in flight.
        self.tts_synth_task = None
        self.current_turn_task = None
        # conversation-layer Task 4.2 (Q2) — `_start_turn`'s in-flight
        # branch keys the FIFO queue off `active_chat_id`/`chat_queues`.
        self.active_chat_id = "chat-1"
        self.chat_queues: dict = {}


@pytest.fixture
def captured_envelopes(monkeypatch):
    captured: list[dict[str, Any]] = []

    async def fake_send(session, envelope):
        if envelope is not None:
            captured.append(envelope)

    monkeypatch.setattr(ws_module, "send_envelope", fake_send)
    monkeypatch.setattr("tesseract.mirror.server.voice_io.send_envelope", fake_send)
    monkeypatch.setattr("tesseract.mirror.server.tts.send_envelope", fake_send)
    # SDD Task 1.2: `_start_turn`'s real (unmocked) mid-turn-inject branch
    # now sends through `turn_intake.send_envelope`, not `ws.send_envelope`.
    monkeypatch.setattr("tesseract.mirror.server.turn_intake.send_envelope", fake_send)
    return captured


def test_accumulate_voice_pcm_lazy_alloc():
    s = _Session()
    _accumulate_voice_pcm(s, b"\x01\x02")
    assert s.voice_pcm_buffer == bytearray(b"\x01\x02")
    _accumulate_voice_pcm(s, b"\x03")
    assert s.voice_pcm_buffer == bytearray(b"\x01\x02\x03")


def test_accumulate_voice_pcm_empty_data_noop():
    s = _Session()
    _accumulate_voice_pcm(s, b"")
    assert s.voice_pcm_buffer is None


def test_accumulate_voice_pcm_trims_overflow():
    s = _Session()
    cap = VOICE_PCM_BUFFER_CAP_BYTES
    _accumulate_voice_pcm(s, b"\x00" * (cap - 4))
    _accumulate_voice_pcm(s, b"\xff" * 8)  # overshoots by 4
    assert len(s.voice_pcm_buffer) == cap
    # Tail must be the most-recent bytes; head was trimmed.
    assert bytes(s.voice_pcm_buffer[-8:]) == b"\xff" * 8


async def test_voice_commit_dispatches_chat_with_transcript(captured_envelopes, monkeypatch):
    s = _Session()
    s.voice_pcm_buffer = bytearray(b"\x00" * 32_000)  # 1 s of 16 kHz mono

    async def fake_transcribe(audio):
        # Mirrors Gemini Flash audio: one final pair.
        yield ("hello world", True)

    fake_engine = SimpleNamespace(transcribe_stream=fake_transcribe)
    app = {"stt_engine": fake_engine}

    start_turn = AsyncMock()
    monkeypatch.setattr(turn_intake, "_start_turn", start_turn)

    await _handle_voice_commit(app, s)

    types = [e["type"] for e in captured_envelopes]
    assert types == ["voice_state", "voice_final", "voice_state"]
    assert captured_envelopes[0]["data"] == {"state": "transcribing"}
    assert captured_envelopes[1]["data"] == {"text": "hello world"}
    assert captured_envelopes[2]["data"] == {"state": "idle"}
    start_turn.assert_awaited_once_with(app, s, {"text": "hello world"})
    # Buffer cleared regardless of text outcome.
    assert s.voice_pcm_buffer is None


async def test_voice_commit_emits_fallback_notice_once(
    captured_envelopes, monkeypatch
):
    """When the STT engine reports a one-shot fallback notice (local STT
    just latched off), `_handle_voice_commit` must emit a single
    `voice_instruction` toast before `voice_final` so the operator sees
    why subsequent voice turns now bill cloud."""
    s = _Session()
    s.voice_pcm_buffer = bytearray(b"\x00" * 32_000)

    async def fake_transcribe(audio):
        yield ("hello", True)

    notices = ["Local STT disabled (boom); falling back to paid Gemini cloud STT until reset in Settings.", ""]
    fake_engine = SimpleNamespace(
        transcribe_stream=fake_transcribe,
        consume_fallback_notice=lambda: notices.pop(0),
    )
    app = {"stt_engine": fake_engine}

    monkeypatch.setattr(turn_intake, "_start_turn", AsyncMock())

    await _handle_voice_commit(app, s)

    types = [e["type"] for e in captured_envelopes]
    assert types == ["voice_state", "voice_instruction", "voice_final", "voice_state"]
    assert "Local STT disabled" in captured_envelopes[1]["data"]["instruction"]
    # No re-notify on a follow-up commit (engine returns "" the second time).
    s.voice_pcm_buffer = bytearray(b"\x00" * 32_000)
    captured_envelopes.clear()
    await _handle_voice_commit(app, s)
    types2 = [e["type"] for e in captured_envelopes]
    assert "voice_instruction" not in types2


async def test_voice_commit_does_not_cancel_running_turn(
    captured_envelopes, monkeypatch
):
    """Voice queueing contract (2026-05-01) — `_handle_voice_commit`
    must NOT cancel the in-flight chat turn or its TTS chain. Voice is
    a soft barge-in: the new transcript routes through `_start_turn`,
    which appends it onto `chat_queues[active_chat_id]` if a turn is
    already running. The drain at the natural turn boundary picks it up. This
    pins the no-cancel-on-commit invariant — the only cancellation
    on the voice path is the explicit operator Stop (`cancel_stream`)."""
    s = _Session()
    s.voice_pcm_buffer = bytearray(b"\x00" * 32_000)

    # A brand-new turn task & TTS synth (representing "the operator
    # already spoke their interrupt; we cancelled at speech-start; here
    # is voice_commit landing"). voice_commit should not touch these.
    async def _spin():
        await asyncio.Event().wait()

    standby_turn = asyncio.create_task(_spin())
    standby_synth = asyncio.create_task(_spin())
    try:
        s.current_turn_task = standby_turn
        s.tts_synth_task = standby_synth
        s.chat_session = SimpleNamespace(
            tool_context=SimpleNamespace(cancel_event=asyncio.Event()),
        )

        async def fake_transcribe(audio):
            yield ("new utterance", True)

        fake_engine = SimpleNamespace(transcribe_stream=fake_transcribe)
        app = {"stt_engine": fake_engine}

        start_turn = AsyncMock()
        monkeypatch.setattr(turn_intake, "_start_turn", start_turn)

        await _handle_voice_commit(app, s)

        # voice_commit MUST NOT cancel — barge_in is the cancel path.
        assert not standby_turn.cancelled()
        assert not standby_synth.cancelled()
        assert not s.chat_session.tool_context.cancel_event.is_set()
        # And it MUST start the new turn for the transcribed text.
        start_turn.assert_awaited_once_with(app, s, {"text": "new utterance"})
    finally:
        standby_turn.cancel()
        standby_synth.cancel()
        for t in (standby_turn, standby_synth):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass


async def test_voice_commit_transcribe_mode_skips_chat_dispatch(
    captured_envelopes, monkeypatch
):
    """Mode C: `mode='transcribe'` emits voice_final but does NOT
    dispatch into the chat path — the frontend routes the text into the
    chat input for review/edit/send."""
    s = _Session()
    s.voice_pcm_buffer = bytearray(b"\x00" * 32_000)

    async def fake_transcribe(audio):
        yield ("draft this email please", True)

    fake_engine = SimpleNamespace(transcribe_stream=fake_transcribe)
    app = {"stt_engine": fake_engine}

    start_turn = AsyncMock()
    monkeypatch.setattr(turn_intake, "_start_turn", start_turn)

    await _handle_voice_commit(app, s, {"mode": "transcribe"})

    types = [e["type"] for e in captured_envelopes]
    assert types == ["voice_state", "voice_final", "voice_state"]
    final = next(e for e in captured_envelopes if e["type"] == "voice_final")
    assert final["data"] == {"text": "draft this email please"}
    start_turn.assert_not_awaited()
    assert s.voice_pcm_buffer is None


async def test_voice_commit_session_mode_transcribe_overrides_msg(
    captured_envelopes, monkeypatch
):
    """`session.voice_mode='transcribe'` must skip chat dispatch even
    when the per-message field is `chat` (legacy / stale client)."""
    s = _Session()
    s.voice_mode = "transcribe"
    s.voice_pcm_buffer = bytearray(b"\x00" * 32_000)

    async def fake_transcribe(audio):
        yield ("note this", True)

    fake_engine = SimpleNamespace(transcribe_stream=fake_transcribe)
    app = {"stt_engine": fake_engine}

    start_turn = AsyncMock()
    monkeypatch.setattr(turn_intake, "_start_turn", start_turn)

    await _handle_voice_commit(app, s, {"mode": "chat"})

    start_turn.assert_not_awaited()


def test_handle_voice_mode_set_validates():
    s = _Session()
    s.voice_mode = "transcribe"
    _handle_voice_mode_set(s, {"mode": "transcribe"})
    assert s.voice_mode == "transcribe"
    _handle_voice_mode_set(s, {"mode": "  SPEAK  "})
    assert s.voice_mode == "speak"
    _handle_voice_mode_set(s, {"mode": "command"})
    assert s.voice_mode == "command"
    # `live` is no longer a valid voice mode — Live was purged
    # 2026-04-27. Bad-envelope inputs must KEEP the current mode
    # rather than reverting to a default; flipping a silent session to
    # speak on a bad envelope would be the worst possible failure
    # (TARS talks when operator opted into silence).
    _handle_voice_mode_set(s, {"mode": "live"})
    assert s.voice_mode == "command"
    _handle_voice_mode_set(s, {"mode": "garbage"})
    assert s.voice_mode == "command"
    _handle_voice_mode_set(s, {})
    assert s.voice_mode == "command"
    _handle_voice_mode_set(s, {"mode": 42})
    assert s.voice_mode == "command"
    s.voice_mode = "transcribe"
    _handle_voice_mode_set(s, {"mode": None})
    assert s.voice_mode == "transcribe"


async def test_voice_commit_unknown_mode_falls_back_to_chat(
    captured_envelopes, monkeypatch
):
    s = _Session()
    s.voice_pcm_buffer = bytearray(b"\x00" * 32_000)

    async def fake_transcribe(audio):
        yield ("hi", True)

    fake_engine = SimpleNamespace(transcribe_stream=fake_transcribe)
    app = {"stt_engine": fake_engine}

    start_turn = AsyncMock()
    monkeypatch.setattr(turn_intake, "_start_turn", start_turn)

    await _handle_voice_commit(app, s, {"mode": "garbage"})

    start_turn.assert_awaited_once_with(app, s, {"text": "hi"})


async def test_voice_commit_empty_buffer_skips_chat(captured_envelopes, monkeypatch):
    s = _Session()  # no buffer at all
    fake_engine = SimpleNamespace(transcribe_stream=AsyncMock())
    app = {"stt_engine": fake_engine}

    start_turn = AsyncMock()
    monkeypatch.setattr(turn_intake, "_start_turn", start_turn)

    await _handle_voice_commit(app, s)

    types = [e["type"] for e in captured_envelopes]
    assert types == ["voice_final", "voice_state"]
    assert captured_envelopes[0]["data"] == {"text": ""}
    assert captured_envelopes[1]["data"] == {"state": "idle"}
    start_turn.assert_not_awaited()


async def test_voice_commit_silence_transcript_skips_chat(captured_envelopes, monkeypatch):
    """Gemini Flash audio sometimes returns whitespace on pure silence;
    we must not dispatch an empty turn into the chat path."""
    s = _Session()
    s.voice_pcm_buffer = bytearray(b"\x00" * 32_000)

    async def fake_transcribe(audio):
        yield ("   ", True)

    fake_engine = SimpleNamespace(transcribe_stream=fake_transcribe)
    app = {"stt_engine": fake_engine}

    start_turn = AsyncMock()
    monkeypatch.setattr(turn_intake, "_start_turn", start_turn)

    await _handle_voice_commit(app, s)

    final = next(e for e in captured_envelopes if e["type"] == "voice_final")
    assert final["data"]["text"] == ""
    start_turn.assert_not_awaited()


async def test_voice_commit_engine_error_resets_to_idle(captured_envelopes, monkeypatch):
    s = _Session()
    s.voice_pcm_buffer = bytearray(b"\x00" * 32_000)

    async def fake_transcribe(audio):
        raise RuntimeError("model boom")
        yield  # pragma: no cover

    fake_engine = SimpleNamespace(transcribe_stream=fake_transcribe)
    app = {"stt_engine": fake_engine}

    start_turn = AsyncMock()
    monkeypatch.setattr(turn_intake, "_start_turn", start_turn)

    await _handle_voice_commit(app, s)

    types = [e["type"] for e in captured_envelopes]
    # transcribing → idle; no voice_final on the error path.
    assert types == ["voice_state", "voice_state"]
    assert captured_envelopes[0]["data"] == {"state": "transcribing"}
    assert captured_envelopes[1]["data"] == {"state": "idle"}
    start_turn.assert_not_awaited()


async def test_voice_commit_no_engine_emits_empty_final(captured_envelopes):
    s = _Session()
    s.voice_pcm_buffer = bytearray(b"\x00" * 8)
    await _handle_voice_commit({}, s)
    types = [e["type"] for e in captured_envelopes]
    assert types == ["voice_final", "voice_state"]
    assert captured_envelopes[0]["data"] == {"text": ""}


async def test_voice_cancel_clears_buffer_and_idles(captured_envelopes):
    s = _Session()
    s.voice_pcm_buffer = bytearray(b"\xff\xff")
    # `_handle_voice_cancel` now accepts `app` so it can also stop a
    # Live loop if one is open; the path with no live_session is a
    # no-op for `app` but still requires the parameter slot.
    app = type("FakeApp", (dict,), {})()
    await _handle_voice_cancel(app, s)
    assert s.voice_pcm_buffer is None
    assert len(captured_envelopes) == 1
    assert captured_envelopes[0]["type"] == "voice_state"
    assert captured_envelopes[0]["data"] == {"state": "idle"}


async def test_voice_barge_in_then_commit_queues_fifo(
    captured_envelopes, monkeypatch
):
    """Voice queueing end-to-end (2026-05-01 → conversation-layer Task 4.2
    Q2): with a chat turn already running, `voice_cancel reason='barge_in'`
    followed by `voice_commit` must (a) not cancel the running turn and
    (b) append the transcript to `chat_queues[active_chat_id]` as a NORMAL
    queued turn (Q2 unifies plain text onto the FIFO queue — mid-turn
    inject is reserved for the future Q3 steer command and stays empty
    here)."""
    s = _Session()
    s.voice_pcm_buffer = bytearray(b"\x00" * 32_000)

    async def _spin():
        await asyncio.Event().wait()

    turn_task = asyncio.create_task(_spin())
    s.current_turn_task = turn_task

    pending_injected: list[dict] = []

    def _enqueue(text: str) -> None:
        text = (text or "").strip()
        if text:
            pending_injected.append({"text": text, "queued_at": "stub"})

    s.chat_session = SimpleNamespace(
        tool_context=SimpleNamespace(cancel_event=asyncio.Event()),
        pending_injected_messages=pending_injected,
        enqueue_user_inject=_enqueue,
    )

    try:
        # Speech-start fires the soft barge-in.
        await _handle_voice_cancel({}, s, {"reason": "barge_in"})
        assert not turn_task.cancelled(), "barge_in must not cancel the running turn"
        assert not s.chat_session.tool_context.cancel_event.is_set()

        # Then voice_commit lands while the turn is still in flight —
        # it must route through _start_turn and land on the FIFO queue,
        # NOT the mid-turn inject queue (Q2 text-only contract).
        async def fake_transcribe(audio):
            yield ("queue me", True)

        fake_engine = SimpleNamespace(transcribe_stream=fake_transcribe)
        app = {"stt_engine": fake_engine}

        await _handle_voice_commit(app, s)

        assert pending_injected == []
        assert [e["text"] for e in s.chat_queues[s.active_chat_id]] == ["queue me"]
        assert not turn_task.cancelled()
    finally:
        turn_task.cancel()
        try:
            await turn_task
        except asyncio.CancelledError:
            pass


async def test_cancel_turn_cancels_turn_and_tts_chain():
    s = _Session()
    s.tts_buffer = "still speaking"

    async def _spin():
        await asyncio.Event().wait()

    synth_task = asyncio.create_task(_spin())
    turn_task = asyncio.create_task(_spin())
    s.tts_synth_task = synth_task
    s.current_turn_task = turn_task
    s.pending_workspace_payloads = []  # _cancel_turn no longer touches this
    s.chat_session = SimpleNamespace(
        tool_context=SimpleNamespace(cancel_event=asyncio.Event()),
        pending_injected_messages=[],
    )

    await ws_module._cancel_turn({}, s)

    assert s.tts_buffer == ""
    assert s.tts_synth_task is None
    assert s.current_turn_task is None
    assert s.chat_session.tool_context.cancel_event.is_set()
    assert synth_task.cancelled()
    assert turn_task.cancelled()


def test_make_voice_state_rejects_unknown():
    from tesseract.mirror.server.envelope import make_voice_state
    with pytest.raises(ValueError, match="voice_state"):
        make_voice_state("s", "garbage")


async def test_synthesize_skipped_when_voice_mode_transcribe(monkeypatch):
    """The TTS synth path must short-circuit when the operator parked
    TARS in `transcribe` mode, regardless of input modality (typed or
    spoken). No engine call, no envelope, no ledger debit."""
    s = _Session()
    s.voice_mode = "transcribe"

    engine_calls: list[str] = []

    class _FakeEngine:
        async def synthesize(self, text, params):
            engine_calls.append(text)
            return (b"\x00", "gemini_flash_tts")

    sent: list[dict] = []

    async def fake_send(_session, env):
        if env is not None:
            sent.append(env)

    monkeypatch.setattr(ws_module, "send_envelope", fake_send)
    monkeypatch.setattr("tesseract.mirror.server.voice_io.send_envelope", fake_send)
    monkeypatch.setattr("tesseract.mirror.server.tts.send_envelope", fake_send)
    # SDD Task 1.2: `_start_turn`'s real (unmocked) mid-turn-inject branch
    # now sends through `turn_intake.send_envelope`, not `ws.send_envelope`.
    monkeypatch.setattr("tesseract.mirror.server.turn_intake.send_envelope", fake_send)
    app = {"tts_engine": _FakeEngine(), "voice_state": None, "mood": None}

    await _synthesize_and_emit_sentence(app, s, "hello there.", is_final=False)

    assert engine_calls == []
    assert sent == []


async def test_synthesize_skipped_when_voice_mode_command(monkeypatch):
    sent = []
    monkeypatch.setattr(ws_module, "send_envelope", lambda s, e: sent.append(e))
    monkeypatch.setattr("tesseract.mirror.server.voice_io.send_envelope", lambda s, e: sent.append(e))
    monkeypatch.setattr("tesseract.mirror.server.tts.send_envelope", lambda s, e: sent.append(e))
    s = _Session()
    s.voice_mode = "command"

    class _FakeEngine:
        async def synthesize(self, text, params):
            raise AssertionError("command mode must not synthesize TTS")

    app = {"tts_engine": _FakeEngine(), "voice_state": None, "mood": None}
    await _synthesize_and_emit_sentence(app, s, "hello", is_final=False)
    assert sent == []


async def test_synthesize_runs_when_voice_mode_speak(monkeypatch):
    s = _Session()
    s.voice_mode = "speak"

    engine_calls: list[str] = []

    class _FakeEngine:
        async def synthesize(self, text, params):
            engine_calls.append(text)
            return (b"\xab\xcd", "gemini_flash_tts")

    sent: list[dict] = []

    async def fake_send(_session, env):
        if env is not None:
            sent.append(env)

    monkeypatch.setattr(ws_module, "send_envelope", fake_send)
    monkeypatch.setattr("tesseract.mirror.server.voice_io.send_envelope", fake_send)
    monkeypatch.setattr("tesseract.mirror.server.tts.send_envelope", fake_send)
    # SDD Task 1.2: `_start_turn`'s real (unmocked) mid-turn-inject branch
    # now sends through `turn_intake.send_envelope`, not `ws.send_envelope`.
    monkeypatch.setattr("tesseract.mirror.server.turn_intake.send_envelope", fake_send)
    app = {"tts_engine": _FakeEngine(), "voice_state": None, "mood": None}

    await _synthesize_and_emit_sentence(app, s, "hello there.", is_final=False)

    assert engine_calls == ["hello there."]
    assert len(sent) == 1
    assert sent[0]["type"] == "tts_chunk"


async def test_tts_provider_failure_emits_voice_instruction_once(monkeypatch):
    """Generic TTS provider failures must be visible to the operator
    while a turn is active. Otherwise Speak mode appears broken with
    no toast or audio. Post-cancel torn-down requests are suppressed
    elsewhere via the `current_turn_task is None` guard."""
    s = _Session()
    s.voice_mode = "speak"
    s.current_turn_task = object()  # active-turn sentinel

    class _FailingEngine:
        async def synthesize(self, text, params):
            raise RuntimeError("provider down")

    sent: list[dict] = []

    async def fake_send(_session, env):
        if env is not None:
            sent.append(env)

    monkeypatch.setattr(ws_module, "send_envelope", fake_send)
    monkeypatch.setattr("tesseract.mirror.server.voice_io.send_envelope", fake_send)
    monkeypatch.setattr("tesseract.mirror.server.tts.send_envelope", fake_send)
    # SDD Task 1.2: `_start_turn`'s real (unmocked) mid-turn-inject branch
    # now sends through `turn_intake.send_envelope`, not `ws.send_envelope`.
    monkeypatch.setattr("tesseract.mirror.server.turn_intake.send_envelope", fake_send)
    app = {"tts_engine": _FailingEngine(), "voice_state": None, "mood": None}

    await _synthesize_and_emit_sentence(app, s, "hello there.", is_final=False)
    await _synthesize_and_emit_sentence(app, s, "second try.", is_final=False)

    instructions = [e for e in sent if e["type"] == "voice_instruction"]
    assert len(instructions) == 1
    instruction = instructions[0]["data"]["instruction"]
    # The toast must surface the exception class + message so the operator
    # sees what actually failed (the generic-toast era was 2026-04-30).
    assert "RuntimeError" in instruction
    assert "provider down" in instruction
    assert not any(e["type"] == "tts_chunk" for e in sent)


def test_make_voice_final_shape():
    from tesseract.mirror.server.envelope import make_voice_final
    env = make_voice_final("sess-1", "hi")
    assert env["type"] == "voice_final"
    assert env["category"] == "voice"
    assert env["session_id"] == "sess-1"
    assert env["data"] == {"text": "hi"}
