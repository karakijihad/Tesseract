"""P15 audit-3 follow-up: set_mood tool result triggers an immediate
entity_signals envelope.

`_handle_chunk` in `mirror/server/ws.py` watches for TOOL_RESULT chunks
whose call_id maps to a prior TOOL_CALL_END for `set_mood`, and fires an
extra `entity_signals` envelope so the orb reflects the new mood without
waiting for the 2-second pump cycle. This test pins that contract — it is
load-bearing for the body-of-TARS feel.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiohttp import web

from tesseract.kernel.adapters.base import ChunkType, StreamChunk
from tesseract.kernel.state import ToolCall
from tesseract.mirror.server import ws as ws_module


def _build_app(*, mood_intensity: float = 0.5, mood_valence: float = 0.0) -> web.Application:
    app = web.Application()
    app["mood"] = SimpleNamespace(intensity=mood_intensity, valence=mood_valence)
    app["adapter_options"] = SimpleNamespace(tier="api")
    policy = MagicMock()
    # "auto" emits a tool_auto envelope on TOOL_CALL_END but doesn't block
    # — exactly what set_mood resolves to in the live config.
    policy.resolve_posture.return_value = "auto"
    app["config"] = SimpleNamespace(permissions=policy)
    return app


def _build_session(session_id: str = "sess-1"):
    ws = MagicMock()
    ws.send_json = AsyncMock()
    ws.closed = False
    return SimpleNamespace(
        session_id=session_id,
        ws=ws,
        event_log=MagicMock(append=MagicMock()),
        tool_names_by_call={},
    )


def _sent_types(session) -> list[str]:
    return [c.args[0]["type"] for c in session.ws.send_json.await_args_list]


async def test_set_mood_tool_result_emits_entity_signals():
    app = _build_app(mood_intensity=0.7, mood_valence=-0.2)
    session = _build_session()

    # TOOL_CALL_END registers the call_id → tool_name mapping.
    end_chunk = StreamChunk(
        type=ChunkType.TOOL_CALL_END,
        tool_call=ToolCall(id="call-1", name="set_mood", input={"intensity": 0.7, "valence": -0.2}),
        tool_call_id="call-1",
    )
    await ws_module._handle_chunk(app, session, end_chunk)
    assert session.tool_names_by_call.get("call-1") == "set_mood"

    # TOOL_RESULT for the same call_id → entity_signals envelope.
    result_chunk = StreamChunk(
        type=ChunkType.TOOL_RESULT,
        tool_call_id="call-1",
        text="ok",
    )
    await ws_module._handle_chunk(app, session, result_chunk)

    types_sent = _sent_types(session)
    assert "stream_tool_result" in types_sent
    assert "entity_signals" in types_sent

    es_payload = next(
        c.args[0] for c in session.ws.send_json.await_args_list
        if c.args[0]["type"] == "entity_signals"
    )
    assert es_payload["data"]["mood_intensity"] == 0.7
    assert es_payload["data"]["mood_valence"] == -0.2
    # call_id must be drained from the lookup so it can't double-fire.
    assert "call-1" not in session.tool_names_by_call


async def test_non_set_mood_tool_result_does_not_emit_entity_signals():
    app = _build_app()
    session = _build_session()

    end_chunk = StreamChunk(
        type=ChunkType.TOOL_CALL_END,
        tool_call=ToolCall(id="call-2", name="memory_search", input={"query": "x"}),
        tool_call_id="call-2",
    )
    await ws_module._handle_chunk(app, session, end_chunk)

    result_chunk = StreamChunk(
        type=ChunkType.TOOL_RESULT,
        tool_call_id="call-2",
        text="hits: []",
    )
    await ws_module._handle_chunk(app, session, result_chunk)

    types_sent = _sent_types(session)
    assert "stream_tool_result" in types_sent
    assert "entity_signals" not in types_sent


async def test_set_mood_with_no_mood_subsystem_is_safe():
    """A misconfigured app (no `mood` registered) must not crash the
    chunk-dispatch path — `_emit_entity_signals` short-circuits."""
    app = _build_app()
    app["mood"] = None
    session = _build_session()

    await ws_module._handle_chunk(
        app,
        session,
        StreamChunk(
            type=ChunkType.TOOL_CALL_END,
            tool_call=ToolCall(id="call-3", name="set_mood", input={"intensity": 0.5, "valence": 0.0}),
            tool_call_id="call-3",
        ),
    )
    await ws_module._handle_chunk(
        app,
        session,
        StreamChunk(type=ChunkType.TOOL_RESULT, tool_call_id="call-3", text="ok"),
    )

    types_sent = _sent_types(session)
    assert "entity_signals" not in types_sent
