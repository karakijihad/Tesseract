"""Codex-fix i2 / M1 / M2 — concurrent turn-state isolation regression.

Two simulated synthetic turns interleave TOOL_CALL_END / TOOL_RESULT /
workspace_reply chunks on the same ServerSession. Verifies:

* Per-turn ``TurnState`` (M1) keeps tool_names_by_call, workspace_reply_succeeded,
  turn_tool_count, deep_focus_latched, pending_happy_saves isolated across
  turns — one turn can't pop another turn's pending tool name or consume
  the other's reply-success flag.
* Workspace reply broadcast (M2) uses the exact ``comment_id`` from
  ``chunk.raw["metadata"]`` — each turn broadcasts ITS reply, not a
  "latest TARS comment" scan.

The test drives ``_handle_chunk`` directly under two concurrent
ContextVar-scoped ``TurnState`` instances, mirroring what
``_run_turn`` would do for two synthetic turns running in parallel.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from tesseract.kernel.adapters.base import ChunkType, StreamChunk
from tesseract.kernel.state import ToolCall


def _toolcall(name: str, call_id: str, *, input_: dict | None = None) -> ToolCall:
    return ToolCall(id=call_id, name=name, input=input_ or {})


def _tool_call_end(name: str, call_id: str, **input_: Any) -> StreamChunk:
    return StreamChunk(
        type=ChunkType.TOOL_CALL_END,
        tool_call=_toolcall(name, call_id, input_=input_ or None),
        tool_call_id=call_id,
    )


def _tool_result(call_id: str, *, metadata: dict | None = None, text: str = "ok") -> StreamChunk:
    raw: dict[str, Any] = {}
    if metadata is not None:
        raw["metadata"] = dict(metadata)
    return StreamChunk(
        type=ChunkType.TOOL_RESULT,
        text=text,
        tool_call_id=call_id,
        raw=raw,
    )


class _StubWS:
    closed = True  # closed → send_envelope short-circuits


def _make_session() -> SimpleNamespace:
    return SimpleNamespace(
        session_id="sid",
        ws=_StubWS(),
        event_log=[],
        ws_send_lock=asyncio.Lock(),
        workspace_origin=None,
        # Legacy fields — must remain untouched by the new path.
        tool_names_by_call={},
        pending_happy_saves=set(),
        turn_tool_count=0,
        deep_focus_latched=False,
        workspace_reply_succeeded=False,
        chat_session=SimpleNamespace(
            tool_context=SimpleNamespace(todos=[]),
        ),
    )


@pytest.fixture
def app_with_store(monkeypatch):
    """App with a stubbed workspace event store + capture of broadcasts."""
    broadcasts: list[Any] = []

    class _Store:
        def get_comment(self, comment_id: str):
            # Return a stub WorkspaceComment-ish object with the id.
            return SimpleNamespace(
                comment_id=comment_id,
                event_id=f"evt_for_{comment_id}",
                author="tars",
                body="reply body",
                ts="2026-05-23T00:00:00Z",
                reply_to=None,
                delivered_to_tars=False,
                to_dict=lambda: {"comment_id": comment_id},
            )

        def _read_comments(self):
            raise AssertionError("legacy latest-comment scan must not be used")

    async def fake_broadcast_comment_appended(app, comment):
        broadcasts.append(comment.comment_id)

    monkeypatch.setattr(
        "tesseract.workspace_events.broadcast.broadcast_comment_appended",
        fake_broadcast_comment_appended,
    )
    return {"workspace_event_store": _Store()}, broadcasts


@pytest.mark.asyncio
async def test_concurrent_turns_keep_tool_attribution_isolated(
    monkeypatch, app_with_store,
):
    """Two synthetic turns each register their own workspace_reply call
    on the SAME ServerSession. Each turn's TOOL_RESULT must pop ITS OWN
    entry — the other turn's pending registration must survive.
    """
    from tesseract.mirror.server import chunk_handler as chunk_handler_mod
    from tesseract.mirror.server import ws as ws_mod
    from tesseract.mirror.server.turn_context import TurnState, current_turn_state

    # Don't go through the orb / posture pipeline. `_handle_chunk` resolves
    # these via chunk_handler's own module globals (SDD Task 1.3), so the
    # patch target is chunk_handler, not the ws.py re-export.
    monkeypatch.setattr(chunk_handler_mod, "_set_orb_state", AsyncMock())
    monkeypatch.setattr(chunk_handler_mod, "_emit_posture_event", AsyncMock())

    app, _broadcasts = app_with_store
    session = _make_session()

    async def turn_a(state: TurnState, gate_after_register: asyncio.Event,
                    gate_for_result: asyncio.Event) -> None:
        token = current_turn_state.set(state)
        try:
            await ws_mod._handle_chunk(
                app, session, _tool_call_end("workspace_reply", "call-A"),
            )
            gate_after_register.set()
            await gate_for_result.wait()
            # By now turn B has registered AND completed its own call.
            # Our call-A entry must still be present.
            assert "call-A" in state.tool_names_by_call
            await ws_mod._handle_chunk(
                app, session,
                _tool_result("call-A", metadata={"event_id": "evt_A", "comment_id": "cmt_A"}),
            )
        finally:
            current_turn_state.reset(token)

    async def turn_b(state: TurnState, wait_for_a_register: asyncio.Event,
                     release_a_result: asyncio.Event) -> None:
        token = current_turn_state.set(state)
        try:
            await wait_for_a_register.wait()
            await ws_mod._handle_chunk(
                app, session, _tool_call_end("workspace_reply", "call-B"),
            )
            await ws_mod._handle_chunk(
                app, session,
                _tool_result("call-B", metadata={"event_id": "evt_B", "comment_id": "cmt_B"}),
            )
            release_a_result.set()
        finally:
            current_turn_state.reset(token)

    state_a, state_b = TurnState(), TurnState()
    gate1, gate2 = asyncio.Event(), asyncio.Event()

    await asyncio.gather(
        turn_a(state_a, gate1, gate2),
        turn_b(state_b, gate1, gate2),
    )

    # Each turn's own success flag set; the OTHER turn didn't touch it.
    assert state_a.workspace_reply_succeeded is True
    assert state_b.workspace_reply_succeeded is True
    # Both turns popped their own entries; no leak into the other turn.
    assert "call-A" not in state_a.tool_names_by_call
    assert "call-B" not in state_a.tool_names_by_call
    assert "call-A" not in state_b.tool_names_by_call
    assert "call-B" not in state_b.tool_names_by_call
    # Legacy session field untouched by the new path.
    assert session.tool_names_by_call == {}
    assert session.workspace_reply_succeeded is False


@pytest.mark.asyncio
async def test_concurrent_reply_broadcasts_use_exact_comment_id(
    monkeypatch, app_with_store,
):
    """Codex-fix M2 regression — broadcast must use chunk.raw["metadata"]
    comment_id, NOT a "latest TARS comment" scan that races across turns.
    """
    from tesseract.mirror.server import chunk_handler as chunk_handler_mod
    from tesseract.mirror.server import ws as ws_mod
    from tesseract.mirror.server.turn_context import TurnState, current_turn_state

    monkeypatch.setattr(chunk_handler_mod, "_set_orb_state", AsyncMock())
    monkeypatch.setattr(chunk_handler_mod, "_emit_posture_event", AsyncMock())

    app, broadcasts = app_with_store
    session = _make_session()

    async def run_turn(state: TurnState, call_id: str, comment_id: str) -> None:
        token = current_turn_state.set(state)
        try:
            await ws_mod._handle_chunk(
                app, session, _tool_call_end("workspace_reply", call_id),
            )
            # Yield to the loop so the two turns actually interleave at
            # the await between register and result.
            await asyncio.sleep(0)
            await ws_mod._handle_chunk(
                app, session,
                _tool_result(
                    call_id,
                    metadata={"event_id": f"evt_{comment_id}", "comment_id": comment_id},
                ),
            )
        finally:
            current_turn_state.reset(token)

    await asyncio.gather(
        run_turn(TurnState(), "call-A", "cmt_A"),
        run_turn(TurnState(), "call-B", "cmt_B"),
    )

    # Both expected comments broadcast — order doesn't matter, but each
    # turn's exact id must be present.
    assert "cmt_A" in broadcasts
    assert "cmt_B" in broadcasts
    # No accidental "latest TARS" stampede that would broadcast the same
    # id twice.
    assert broadcasts.count("cmt_A") == 1
    assert broadcasts.count("cmt_B") == 1


@pytest.mark.asyncio
async def test_missing_reply_metadata_does_not_fallback_scan(
    monkeypatch, app_with_store,
):
    """Missing comment_id metadata should skip live broadcast, not guess."""
    from tesseract.mirror.server import chunk_handler as chunk_handler_mod
    from tesseract.mirror.server import ws as ws_mod
    from tesseract.mirror.server.turn_context import TurnState, current_turn_state

    monkeypatch.setattr(chunk_handler_mod, "_set_orb_state", AsyncMock())
    monkeypatch.setattr(chunk_handler_mod, "_emit_posture_event", AsyncMock())

    app, broadcasts = app_with_store
    session = _make_session()
    state = TurnState()

    token = current_turn_state.set(state)
    try:
        await ws_mod._handle_chunk(
            app, session, _tool_call_end("workspace_reply", "call-missing"),
        )
        await ws_mod._handle_chunk(
            app, session, _tool_result("call-missing", metadata=None),
        )
    finally:
        current_turn_state.reset(token)

    assert state.workspace_reply_succeeded is True
    assert broadcasts == []
