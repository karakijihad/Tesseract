"""orb_visibility tool — broadcast + fail-soft behavior (mirrors
test_chat_initiate.py's stub-session pattern)."""

from __future__ import annotations

from typing import Any

import pytest

from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.orb_visibility import (
    OrbVisibilityInput,
    OrbVisibilityTool,
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
async def test_orb_visibility_pushes_to_all_sessions() -> None:
    sessions = {"s1": _StubSession("s1"), "s2": _StubSession("s2")}
    app = {"server_sessions": sessions}

    tool = OrbVisibilityTool(app_provider=lambda: app)
    result = await tool.run(OrbVisibilityInput(visible=False), ToolContext())

    assert not result.is_error
    assert result.metadata == {"sessions": 2, "visible": False}
    for sess in sessions.values():
        assert len(sess.event_log) == 1
        env = sess.event_log[0]
        assert env["type"] == "orb_visibility"
        assert env["category"] == "entity"
        assert env["data"] == {"visible": False}


@pytest.mark.asyncio
async def test_orb_visibility_no_sessions_returns_error() -> None:
    app = {"server_sessions": {}}
    tool = OrbVisibilityTool(app_provider=lambda: app)
    result = await tool.run(OrbVisibilityInput(visible=True), ToolContext())
    assert result.is_error
    assert "no Mirror window open" in result.output


@pytest.mark.asyncio
async def test_orb_visibility_no_app_provider_returns_error() -> None:
    tool = OrbVisibilityTool(app_provider=None)
    result = await tool.run(OrbVisibilityInput(visible=True), ToolContext())
    assert result.is_error
    assert "headless" in result.output or "test" in result.output


def test_orb_visibility_default_posture_auto() -> None:
    assert OrbVisibilityTool.default_posture == "auto"
