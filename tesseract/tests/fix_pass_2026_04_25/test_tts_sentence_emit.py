"""Phase 16 S3 (post-G2 reshape): sentence-boundary TTS emission +
voice_instruction wiring.

Pins:
- `_split_sentences` peels at `[.!?]\\s` boundaries, drops empties, retains tail.
- `_handle_chunk` forwards TEXT deltas through `_maybe_emit_tts_sentences` and
  emits one `tts_chunk` envelope per completed sentence in order.
- `_synthesize_sentence_audio` snapshots `voice_state.voice_id` into
  `VoiceParams` and threads `kind` (intent/answer) as the `preset` for the
  engine. Style/character is config-only (`roles.yaml synthesis_presets`)
  after 2026-05-04 — the mood object no longer feeds voice synthesis,
  only the orb.
- `_handle_chunk` for a `set_voice` TOOL_RESULT emits a `voice_instruction`
  envelope sourced from the post-state of `app["voice_state"]` carrying
  `voice_id` only (style is config-only post 2026-05-04).
- `make_voice_state` accepts `"speaking_back"` (S3 contract) and rejects junk.
- `make_tts_chunk` shape: type, category, data fields.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from tesseract.kernel.adapters.base import ChunkType, StreamChunk
from tesseract.kernel.state import ToolCall
from tesseract.mirror.server import ws as ws_module
from tesseract.mirror.server.envelope import (
    make_tts_chunk,
    make_voice_instruction,
    make_voice_state,
)
from tesseract.mirror.server.stream_parser import _split_sentences
from tesseract.mirror.server.tts import _flush_tts_terminator


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


def _build_session(session_id: str = "sess-tts"):
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


async def _drain_synth(app, session) -> None:
    """End-of-turn flush. Post-2026-04-26 the synthesizer is deferred
    until the stream completes so all text is read in one go (matches
    operator's mental model — text first, then voice). Tests must call
    this after pushing chunks for synth/emit assertions to populate."""
    await _flush_tts_terminator(app, session, succeeded=True)


def _sent(session) -> list[dict]:
    return [c.args[0] for c in session.ws.send_json.await_args_list]


# ── _split_sentences ───────────────────────────────────────────


def test_split_sentences_basic():
    # The regex `([\.!\?])(\s+|(?=[A-Z])|$)` flushes a final sentence
    # ending in punctuation even without trailing whitespace, so an
    # input like "...I'm fine!" drains entirely with no tail.
    sentences, tail = _split_sentences("Hello world. How are you? I'm fine!")
    assert sentences == ["Hello world.", "How are you?", "I'm fine!"]
    assert tail == ""


def test_split_sentences_trailing_space_drains_last():
    sentences, tail = _split_sentences("First. Second. ")
    assert sentences == ["First.", "Second."]
    assert tail == ""


def test_split_sentences_no_boundary_below_force_flush_keeps_tail():
    sentences, tail = _split_sentences("partial fragment with no end")
    assert sentences == []
    assert tail == "partial fragment with no end"


def test_split_sentences_force_flush_when_no_boundary():
    long = "a " * 150  # 300 chars, no punctuation
    sentences, tail = _split_sentences(long)
    assert sentences, "expected force-flush at the soft cap"
    assert sentences[0] in long


# ── _handle_chunk → tts_chunk emission ─────────────────────────


async def test_handle_chunk_text_emits_tts_chunk_per_sentence():
    tts_engine = MagicMock()
    tts_engine.synthesize = AsyncMock(return_value=(b"\x00\x01", "gemini_flash_tts"))
    voice_state = SimpleNamespace(voice_id="Charon", tone_prompt="A British voice.")
    mood = SimpleNamespace(intensity=0.5, valence=0.0)

    app = _build_app(tts_engine=tts_engine, voice_state=voice_state, mood=mood)
    session = _build_session()

    await ws_module._handle_chunk(
        app,
        session,
        StreamChunk(type=ChunkType.TEXT, text="Hello there. And t"),
    )
    await ws_module._handle_chunk(
        app,
        session,
        StreamChunk(type=ChunkType.TEXT, text="hen this. "),
    )
    await _drain_synth(app, session)

    sent = _sent(session)
    tts_envs = [e for e in sent if e["type"] == "tts_chunk"]
    # Per-sentence streaming synth (parallel synth, serial emit — see
    # `_chained_tts_synth`): each completed sentence fires its own
    # tts_chunk during streaming, then `_flush_tts_terminator` emits a
    # final empty-payload chunk with is_final=True. Two sentences peeled
    # from "Hello there. And then this." → 2 audio chunks + 1 terminator.
    assert len(tts_envs) == 3
    assert [e["data"]["sequence"] for e in tts_envs] == [0, 1, 2]
    assert [e["data"]["is_final"] for e in tts_envs] == [False, False, True]
    assert tts_envs[0]["data"]["provider"] == "gemini_flash_tts"
    assert tts_envs[1]["data"]["provider"] == "gemini_flash_tts"
    assert tts_envs[0]["data"]["audio_b64"] == "AAE="
    assert tts_envs[1]["data"]["audio_b64"] == "AAE="

    # Each sentence is synthesized independently. Voice is decoupled from
    # MoodState (2026-05-01) and tone is config-only via roles.yaml
    # synthesis_presets (2026-05-04) — VoiceParams carries voice_id +
    # preset, no per-turn tone_prompt.
    calls = tts_engine.synthesize.await_args_list
    assert [c.args[0] for c in calls] == ["Hello there.", "And then this."]
    for c in calls:
        params = c.args[1]
        assert params.voice_id == "Charon"
        assert params.preset == "answer"


async def test_handle_chunk_text_with_no_engine_is_noop():
    app = _build_app(tts_engine=None)
    session = _build_session()
    await ws_module._handle_chunk(
        app,
        session,
        StreamChunk(type=ChunkType.TEXT, text="Hello world. "),
    )
    sent = _sent(session)
    assert any(e["type"] == "stream_text" for e in sent)
    assert not any(e["type"] == "tts_chunk" for e in sent)


async def test_handle_chunk_emits_voice_instruction_on_budget_exhausted():
    """When the engine raises BudgetExhausted, the WS handler must emit a
    `voice_instruction` toast (no fallback target after G2)."""
    from tesseract.brain.cost import BudgetExhausted

    tts_engine = MagicMock()
    tts_engine.synthesize = AsyncMock(side_effect=BudgetExhausted(
        scope="voice", role="voice_tts", spent_usd=0.21, cap_usd=0.20,
    ))
    voice_state = SimpleNamespace(voice_id="Charon", tone_prompt="")
    app = _build_app(tts_engine=tts_engine, voice_state=voice_state)
    session = _build_session()

    await ws_module._handle_chunk(
        app,
        session,
        StreamChunk(type=ChunkType.TEXT, text="Sentence one. "),
    )
    await _drain_synth(app, session)

    sent = _sent(session)
    instructions = [e for e in sent if e["type"] == "voice_instruction"]
    assert len(instructions) == 1
    assert "budget" in instructions[0]["data"]["instruction"].lower()
    # No AUDIO chunk emitted on budget — only the terminator (empty
    # audio_b64, is_final=True) so the frontend leaves speaking_back.
    audio_chunks = [
        e for e in sent
        if e["type"] == "tts_chunk" and e["data"].get("audio_b64")
    ]
    assert audio_chunks == []


# ── set_voice TOOL_RESULT → voice_instruction ─────────────────


async def test_set_voice_tool_result_emits_voice_instruction():
    voice_state = SimpleNamespace(voice_id="Algieba", tone_prompt="Speak gently.")
    app = _build_app(voice_state=voice_state)
    session = _build_session()

    await ws_module._handle_chunk(
        app,
        session,
        StreamChunk(
            type=ChunkType.TOOL_CALL_END,
            tool_call=ToolCall(id="call-v", name="set_voice", input={}),
            tool_call_id="call-v",
        ),
    )
    await ws_module._handle_chunk(
        app,
        session,
        StreamChunk(type=ChunkType.TOOL_RESULT, tool_call_id="call-v", text="ok"),
    )

    sent = _sent(session)
    types = [e["type"] for e in sent]
    assert "voice_instruction" in types
    vi = next(e for e in sent if e["type"] == "voice_instruction")
    assert vi["data"]["voice_id"] == "Algieba"
    # Style/character is config-only post 2026-05-04 — no tone_prompt
    # leaks into the voice_instruction wire.
    assert "tone_prompt" not in vi["data"]
    assert "rate" not in vi["data"]
    assert "pitch" not in vi["data"]


async def test_non_set_voice_result_does_not_emit_voice_instruction():
    app = _build_app(voice_state=SimpleNamespace(voice_id="Charon", tone_prompt=""))
    session = _build_session()
    await ws_module._handle_chunk(
        app,
        session,
        StreamChunk(
            type=ChunkType.TOOL_CALL_END,
            tool_call=ToolCall(id="call-z", name="memory_search", input={}),
            tool_call_id="call-z",
        ),
    )
    await ws_module._handle_chunk(
        app,
        session,
        StreamChunk(type=ChunkType.TOOL_RESULT, tool_call_id="call-z", text="hits"),
    )
    types = [e["type"] for e in _sent(session)]
    assert "voice_instruction" not in types


# ── envelope factories ─────────────────────────────────────────


def test_make_voice_state_accepts_speaking_back():
    env = make_voice_state("s1", "speaking_back")
    assert env["type"] == "voice_state"
    assert env["category"] == "voice"
    assert env["data"] == {"state": "speaking_back"}


def test_make_voice_state_rejects_unknown():
    with pytest.raises(ValueError):
        make_voice_state("s1", "nope")


def test_make_tts_chunk_shape():
    env = make_tts_chunk(
        "s1", audio_b64="AAA=", provider="gemini_flash_tts", sequence=3, is_final=True,
    )
    assert env["type"] == "tts_chunk"
    assert env["category"] == "voice"
    assert env["data"] == {
        "audio_b64": "AAA=",
        "provider": "gemini_flash_tts",
        "sequence": 3,
        "is_final": True,
    }


def test_make_voice_instruction_omits_unset_fields():
    env = make_voice_instruction("s1", instruction="calmer")
    assert env["data"] == {"instruction": "calmer"}


def test_make_voice_instruction_carries_voice_id_only():
    """Style/character is config-only after 2026-05-04 — the envelope
    factory no longer accepts a tone_prompt. voice_id is the only knob."""
    env = make_voice_instruction("s1", voice_id="Charon")
    assert env["data"] == {"voice_id": "Charon"}
