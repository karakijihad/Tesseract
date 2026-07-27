"""Audit 2026-07-12 — `reflect_in_background` must not share the live
session's ToolContext by reference.

`ChatSession.__post_init__` assigns `tool_context.spawns` and
`tool_context.enabled_extended_tools` onto whatever context it's given, so a
by-reference share let the background-reflect clone silently replace the
live session's spawn registry and extended-tool set with fresh empty ones
(new spawn registrations + tool_search enablements lost). The fix copies
the context (`copy.copy`, `agent_factory.py` idiom).
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

import pytest

from tesseract.brain import session_ops
from tesseract.brain.chat import ChatSession
from tesseract.kernel.adapters.base import (
    AdapterOptions,
    ChunkType,
    ModelAdapter,
    StreamChunk,
)
from tesseract.kernel.tools.base import ToolContext


class _FakeAdapter(ModelAdapter):
    async def check_available(self) -> bool:
        return True

    def count_tokens(self, text: str) -> int:
        return len(text) // 4

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: AdapterOptions | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        yield StreamChunk(type=ChunkType.TEXT, text="ok")
        yield StreamChunk(type=ChunkType.STOP)


def _session(tmp_path) -> ChatSession:
    cs = ChatSession(
        adapter=_FakeAdapter(),
        system_prompt="",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(model="fake", provider="fake", context_window=100_000),
        tool_context=ToolContext(
            workspace_root=str(tmp_path), session_id="sess-reflect-iso"
        ),
    )
    for i in range(session_ops.MIN_HISTORY_FOR_REFLECTION):
        role = "user" if i % 2 == 0 else "assistant"
        cs.history.append({"role": role, "content": f"turn {i}"})
    return cs


@pytest.mark.asyncio
async def test_reflect_clone_does_not_clobber_live_tool_context(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    session = _session(tmp_path)
    live_spawns = session.tool_context.spawns
    live_enabled = session.tool_context.enabled_extended_tools
    assert live_spawns is session.spawns  # __post_init__ wiring, sanity

    async def _fake_reflect(clone, reason):
        return []

    monkeypatch.setattr(session_ops, "reflect_on_session", _fake_reflect)

    task = session_ops.reflect_in_background(session, "test")
    assert task is not None
    await task

    # The live session's context still points at ITS objects — the clone's
    # __post_init__ mutated only the copied context.
    assert session.tool_context.spawns is live_spawns
    assert session.tool_context.enabled_extended_tools is live_enabled
