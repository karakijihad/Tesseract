"""StartControllerSessionTool — chat-side fire-and-forget wrapper.

The chat brain calls this tool when a task is heavy enough to warrant
a controller session the operator can later attach to. The tool MUST:

* call the dispatcher with ``origin="mirror"`` and
  ``wait_for_completion=False`` (chat doesn't block on the reply),
* return the session_id in both the output text and the metadata so
  the WS layer can write a ``child_transcript_ref`` envelope.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.start_controller_session import (
    StartControllerSessionInput,
    StartControllerSessionTool,
)
from tesseract.orchestrator.tars_controller.dispatcher import (
    DispatchResult,
    DispatcherError,
)


def _ctx() -> ToolContext:
    return ToolContext(workspace_root=".", session_id="s", current_call_id="c")


def test_class_attributes(isolated_home: Path) -> None:
    tool = StartControllerSessionTool()
    assert tool.default_posture == "ask"
    assert tool.risk_class == "propose"
    assert tool.name == "start_controller_session"


@pytest.mark.asyncio
async def test_tool_forwards_to_dispatcher_fire_and_forget(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def _fake_dispatch(prompt: str, **kwargs: Any) -> DispatchResult:
        captured["prompt"] = prompt
        captured.update(kwargs)
        return DispatchResult(
            session_id="sess-fire-forget",
            metadata={"detached": True},
        )

    from tesseract.kernel.tools import start_controller_session as tool_mod
    monkeypatch.setattr(tool_mod, "dispatch_to_controller", _fake_dispatch)

    tool = StartControllerSessionTool()
    result = await tool.run(
        StartControllerSessionInput(
            task="long refactor of auth middleware",
            title="auth refactor",
        ),
        _ctx(),
    )

    # Chat hand-off semantics — origin=mirror, fire-and-forget.
    assert captured["origin"] == "mirror"
    assert captured["wait_for_completion"] is False
    assert captured["title"] == "auth refactor"
    assert captured["mode"] == "chat"

    assert result.is_error is False
    # Output names the session id so the operator can grep/cmd+f.
    assert "sess-fire-forget" in result.output
    assert "tars --session sess-fire-forget" in result.output
    assert result.metadata is not None
    assert result.metadata["session_id"] == "sess-fire-forget"
    assert result.metadata.get("detached") is True


def test_pty_mission_mode_rejected() -> None:
    """TARS-drives-PTY was retired (locked decision) — ``pty_mission`` is
    no longer an accepted session mode."""
    with pytest.raises(ValidationError):
        StartControllerSessionInput(task="x", mode="pty_mission")


@pytest.mark.asyncio
async def test_tool_dispatcher_error_surfaces_as_error_result(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_dispatch(prompt: str, **kwargs: Any) -> DispatchResult:
        raise DispatcherError("port unreachable")

    from tesseract.kernel.tools import start_controller_session as tool_mod
    monkeypatch.setattr(tool_mod, "dispatch_to_controller", _fake_dispatch)

    tool = StartControllerSessionTool()
    result = await tool.run(
        StartControllerSessionInput(task="x"),
        _ctx(),
    )
    assert result.is_error is True
    assert "port unreachable" in result.output
