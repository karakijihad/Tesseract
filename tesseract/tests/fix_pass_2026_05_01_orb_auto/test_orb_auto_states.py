"""Auto-wired discretionary orb states.

Three runtime-driven flips alongside TARS's own `set_state` calls:

- `deep_focus` latches once when a turn fires `_DEEP_FOCUS_TOOL_THRESHOLD`
  tool calls back-to-back.
- `happy` flips on a successful `memory_save` whose input had
  `importance >= _HAPPY_IMPORTANCE`.
- `dreaming` flips on entry to `DreamCycleJob.run` and restores the
  prior state on exit (covered indirectly here via the EntityAffect
  contract — the scheduler test is in fix_pass_consolidation_2026_04_29).

These are *runtime* flips. They mutate the same `EntityAffect` holder
TARS's `set_state` writes to so the next signals pump and any explicit
`set_state` see the latest value.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
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
        turn_tool_count=0,
        deep_focus_latched=False,
        pending_happy_saves=set(),
    )


def _sent_types(session) -> list[str]:
    return [c.args[0]["type"] for c in session.ws.send_json.await_args_list]


def _payloads(session, type_: str) -> list[dict]:
    return [
        c.args[0] for c in session.ws.send_json.await_args_list
        if c.args[0]["type"] == type_
    ]


@pytest.mark.asyncio
async def test_deep_focus_latches_after_threshold_tool_calls():
    app = _build_app()
    session = _build_session()

    threshold = ws_module._DEEP_FOCUS_TOOL_THRESHOLD
    for i in range(threshold):
        end_chunk = StreamChunk(
            type=ChunkType.TOOL_CALL_END,
            tool_call=ToolCall(id=f"call-{i}", name="memory_search", input={}),
            tool_call_id=f"call-{i}",
        )
        await ws_module._handle_chunk(app, session, end_chunk)

    assert session.deep_focus_latched is True
    assert app["entity_affect"].state == "deep_focus"
    state_payloads = _payloads(session, "entity_state_set")
    assert state_payloads, "expected an entity_state_set envelope"
    assert state_payloads[-1]["data"]["state"] == "deep_focus"


@pytest.mark.asyncio
async def test_deep_focus_does_not_re_latch_within_same_turn():
    app = _build_app()
    session = _build_session()

    threshold = ws_module._DEEP_FOCUS_TOOL_THRESHOLD
    for i in range(threshold + 3):
        end_chunk = StreamChunk(
            type=ChunkType.TOOL_CALL_END,
            tool_call=ToolCall(id=f"call-{i}", name="file_read", input={}),
            tool_call_id=f"call-{i}",
        )
        await ws_module._handle_chunk(app, session, end_chunk)

    flips = [p for p in _payloads(session, "entity_state_set")
             if p["data"]["state"] == "deep_focus"]
    assert len(flips) == 1, "deep_focus must latch once per turn, not once per tool call"


@pytest.mark.asyncio
async def test_happy_on_high_importance_memory_save():
    app = _build_app()
    session = _build_session()

    end_chunk = StreamChunk(
        type=ChunkType.TOOL_CALL_END,
        tool_call=ToolCall(
            id="save-1",
            name="memory_save",
            input={"type": "user", "title": "x", "content": "y", "importance": 9},
        ),
        tool_call_id="save-1",
    )
    await ws_module._handle_chunk(app, session, end_chunk)
    assert "save-1" in session.pending_happy_saves

    result_chunk = StreamChunk(
        type=ChunkType.TOOL_RESULT,
        tool_call_id="save-1",
        text="Memory saved: mem-123 (x)",
    )
    await ws_module._handle_chunk(app, session, result_chunk)

    assert app["entity_affect"].state == "happy"
    states = [p["data"]["state"] for p in _payloads(session, "entity_state_set")]
    assert "happy" in states


@pytest.mark.asyncio
async def test_low_importance_memory_save_does_not_flip_happy():
    app = _build_app()
    session = _build_session()

    end_chunk = StreamChunk(
        type=ChunkType.TOOL_CALL_END,
        tool_call=ToolCall(
            id="save-low",
            name="memory_save",
            input={"type": "user", "title": "x", "content": "y", "importance": 5},
        ),
        tool_call_id="save-low",
    )
    await ws_module._handle_chunk(app, session, end_chunk)
    assert "save-low" not in session.pending_happy_saves

    result_chunk = StreamChunk(
        type=ChunkType.TOOL_RESULT,
        tool_call_id="save-low",
        text="Memory saved: mem-456 (x)",
    )
    await ws_module._handle_chunk(app, session, result_chunk)

    assert app["entity_affect"].state == "idle"


@pytest.mark.asyncio
async def test_failed_memory_save_does_not_flip_happy():
    app = _build_app()
    session = _build_session()

    end_chunk = StreamChunk(
        type=ChunkType.TOOL_CALL_END,
        tool_call=ToolCall(
            id="save-fail",
            name="memory_save",
            input={"type": "user", "title": "x", "content": "y", "importance": 9},
        ),
        tool_call_id="save-fail",
    )
    await ws_module._handle_chunk(app, session, end_chunk)

    result_chunk = StreamChunk(
        type=ChunkType.TOOL_RESULT,
        tool_call_id="save-fail",
        text="Memory blocked: dedupe",
        error="dedupe",
    )
    await ws_module._handle_chunk(app, session, result_chunk)

    assert app["entity_affect"].state == "idle"


@pytest.mark.asyncio
async def test_dream_cycle_flips_to_dreaming_and_restores():
    """`DreamCycleJob.run` flips the orb to `dreaming` for the duration
    of the cycle and restores the prior state on exit."""
    from tesseract.scheduler.tasks.dream_cycle import DreamCycleJob
    from tesseract.scheduler.types import JobContext

    affect = EntityAffect()
    affect.set("idle")

    captured: list[str] = []

    class FakeEngine:
        def run_cycle(self):
            captured.append(affect.state)
            return ["mem-1", "mem-2"]

    bundle = SimpleNamespace(dreaming=FakeEngine())
    app = SimpleNamespace(get=lambda key, default=None: {
        "memory_bundle": bundle,
        "entity_affect": affect,
        "server_sessions": {},
    }.get(key, default))

    ctx = JobContext(
        job_name="dream_cycle",
        run_id="run-1",
        app=app,
    )

    job = DreamCycleJob()
    result = await job.run(ctx)

    assert result.ok is True
    assert captured == ["dreaming"], "engine.run_cycle must observe state=dreaming"
    assert affect.state == "idle", "prior state must be restored after cycle"
