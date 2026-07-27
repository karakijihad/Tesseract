"""set_state TOOL_RESULT triggers an `entity_state_set` envelope.

Mirrors the set_mood/entity_signals contract pinned in
fix_pass_2026_04_25/test_set_mood_emits_entity_signals.py — the orb
must reflect TARS's discrete state choice immediately, not on the
next pump cycle.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiohttp import web

from tesseract.kernel.adapters.base import ChunkType, StreamChunk
from tesseract.kernel.state import ToolCall
from tesseract.kernel.tools.set_state import EntityAffect
from tesseract.mirror.server import ws as ws_module


def _build_app(state: str = "idle") -> web.Application:
    app = web.Application()
    affect = EntityAffect()
    affect.set(state)
    app["entity_affect"] = affect
    policy = MagicMock()
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


async def test_set_state_tool_result_emits_entity_state_set():
    app = _build_app(state="happy")
    session = _build_session()

    end_chunk = StreamChunk(
        type=ChunkType.TOOL_CALL_END,
        tool_call=ToolCall(id="call-1", name="set_state", input={"state": "happy"}),
        tool_call_id="call-1",
    )
    await ws_module._handle_chunk(app, session, end_chunk)
    assert session.tool_names_by_call.get("call-1") == "set_state"

    result_chunk = StreamChunk(
        type=ChunkType.TOOL_RESULT,
        tool_call_id="call-1",
        text="state set: happy",
    )
    await ws_module._handle_chunk(app, session, result_chunk)

    types_sent = _sent_types(session)
    assert "stream_tool_result" in types_sent
    assert "entity_state_set" in types_sent

    payload = next(
        c.args[0] for c in session.ws.send_json.await_args_list
        if c.args[0]["type"] == "entity_state_set"
    )
    assert payload["category"] == "entity"
    assert payload["data"]["state"] == "happy"
    assert "call-1" not in session.tool_names_by_call


async def test_non_set_state_tool_result_does_not_emit_envelope():
    app = _build_app()
    session = _build_session()

    end_chunk = StreamChunk(
        type=ChunkType.TOOL_CALL_END,
        tool_call=ToolCall(id="call-2", name="memory_search", input={"query": "x"}),
        tool_call_id="call-2",
    )
    await ws_module._handle_chunk(app, session, end_chunk)
    await ws_module._handle_chunk(
        app, session,
        StreamChunk(type=ChunkType.TOOL_RESULT, tool_call_id="call-2", text="hits: []"),
    )

    assert "entity_state_set" not in _sent_types(session)


async def test_set_state_error_result_does_not_emit_envelope():
    """Rejected `set_state` (e.g. TARS passed `'thinking'` or a typo) must
    NOT emit an `entity_state_set` envelope — the holder isn't mutated on
    error, so re-asserting the prior value would be a phantom write that
    the orb reads as a successful state set."""
    app = _build_app(state="happy")
    session = _build_session()

    end_chunk = StreamChunk(
        type=ChunkType.TOOL_CALL_END,
        tool_call=ToolCall(id="call-err", name="set_state", input={"state": "thinking"}),
        tool_call_id="call-err",
    )
    await ws_module._handle_chunk(app, session, end_chunk)
    # `chunk.error` non-empty signals is_error=True from ToolResult.
    err_chunk = StreamChunk(
        type=ChunkType.TOOL_RESULT,
        tool_call_id="call-err",
        text="set_state: unknown or non-settable state",
        error="set_state: unknown or non-settable state",
    )
    await ws_module._handle_chunk(app, session, err_chunk)

    assert "entity_state_set" not in _sent_types(session)


async def test_set_state_without_affect_subsystem_is_safe():
    """Misconfigured app (no `entity_affect`) must not crash chunk dispatch
    — `_emit_entity_state_from_affect` short-circuits like the mood/voice
    siblings."""
    app = _build_app()
    app["entity_affect"] = None
    session = _build_session()

    await ws_module._handle_chunk(
        app, session,
        StreamChunk(
            type=ChunkType.TOOL_CALL_END,
            tool_call=ToolCall(id="call-3", name="set_state", input={"state": "happy"}),
            tool_call_id="call-3",
        ),
    )
    await ws_module._handle_chunk(
        app, session,
        StreamChunk(type=ChunkType.TOOL_RESULT, tool_call_id="call-3", text="ok"),
    )

    assert "entity_state_set" not in _sent_types(session)
