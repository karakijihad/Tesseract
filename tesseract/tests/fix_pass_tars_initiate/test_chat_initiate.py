"""chat_initiate — TARS speaks first in the Mirror chat tab."""

from __future__ import annotations

from typing import Any

import pytest

from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.chat_initiate import (
    MAX_TEXT_CHARS,
    ChatInitiateInput,
    ChatInitiateTool,
)


class _StubWS:
    closed = False

    async def send_json(self, payload: dict[str, Any]) -> None:
        self._last = payload


class _StubSession:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.event_log: list[dict[str, Any]] = []
        self.ws = _StubWS()


@pytest.mark.asyncio
async def test_chat_initiate_pushes_to_all_sessions() -> None:
    sessions = {"s1": _StubSession("s1"), "s2": _StubSession("s2")}
    app = {"server_sessions": sessions}

    tool = ChatInitiateTool(app_provider=lambda: app)
    result = await tool.run(
        ChatInitiateInput(text="job finished", reason="result"),
        ToolContext(),
    )

    assert not result.is_error
    assert result.metadata == {"sessions": 2, "reason": "result"}
    for sess in sessions.values():
        assert len(sess.event_log) == 1
        env = sess.event_log[0]
        assert env["type"] == "chat_assistant_initiated"
        assert env["category"] == "entity"
        assert env["data"] == {"text": "job finished", "reason": "result"}


@pytest.mark.asyncio
async def test_chat_initiate_no_sessions_returns_error() -> None:
    app = {"server_sessions": {}}
    tool = ChatInitiateTool(app_provider=lambda: app)
    result = await tool.run(
        ChatInitiateInput(text="alert", reason="alert"),
        ToolContext(),
    )
    assert result.is_error
    assert "no Mirror chat tab open" in result.output


@pytest.mark.asyncio
async def test_chat_initiate_no_app_provider_returns_error() -> None:
    tool = ChatInitiateTool(app_provider=None)
    result = await tool.run(
        ChatInitiateInput(text="alert"),
        ToolContext(),
    )
    assert result.is_error
    assert "headless" in result.output or "test" in result.output


@pytest.mark.asyncio
async def test_chat_initiate_empty_text_rejected() -> None:
    app = {"server_sessions": {"s1": _StubSession("s1")}}
    tool = ChatInitiateTool(app_provider=lambda: app)
    result = await tool.run(
        ChatInitiateInput(text="   ", reason="nudge"),
        ToolContext(),
    )
    assert result.is_error
    assert "empty" in result.output


@pytest.mark.asyncio
async def test_chat_initiate_text_cap_enforced() -> None:
    app = {"server_sessions": {"s1": _StubSession("s1")}}
    tool = ChatInitiateTool(app_provider=lambda: app)
    result = await tool.run(
        ChatInitiateInput(text="x" * (MAX_TEXT_CHARS + 1)),
        ToolContext(),
    )
    assert result.is_error
    assert "cap is" in result.output


@pytest.mark.asyncio
async def test_chat_initiate_default_posture_auto() -> None:
    assert ChatInitiateTool.default_posture == "auto"
