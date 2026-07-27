"""DelegateTarsControllerTool — autonomy-runner-facing wrapper.

Verifies that the tool:
* declares the right posture + risk class (operator_gate / ask),
* forwards ``task`` + ``title`` through the dispatcher with
  ``origin="autonomy"`` + ``mode="autonomy"`` + ``wait_for_completion=True``,
* surfaces dispatcher errors as ``is_error=True`` ToolResults rather
  than raising into the kernel runner,
* maps timed-out / cancelled / no-assistant-text dispatcher states
  onto the right ToolResult shape (``timed_out``, ``is_error``,
  metadata).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.delegate_tars_controller import (
    DelegateTarsControllerInput,
    DelegateTarsControllerTool,
)
from tesseract.orchestrator.tars_controller import dispatcher as dispatcher_mod
from tesseract.orchestrator.tars_controller.dispatcher import (
    DispatchResult,
    DispatcherError,
)


def _ctx() -> ToolContext:
    return ToolContext(workspace_root=".", session_id="s", current_call_id="c")


def test_class_attributes_match_audit_2_taxonomy(isolated_home: Path) -> None:
    tool = DelegateTarsControllerTool()
    assert tool.default_posture == "ask"
    assert tool.risk_class == "operator_gate"
    assert tool.name == "delegate_tars_controller"


@pytest.mark.asyncio
async def test_tool_forwards_to_dispatcher_with_autonomy_origin(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def _fake_dispatch(prompt: str, **kwargs: Any) -> DispatchResult:
        captured["prompt"] = prompt
        captured.update(kwargs)
        return DispatchResult(
            session_id="sess-1",
            assistant_text="orchestrated reply",
            saw_assistant_text=True,
        )

    monkeypatch.setattr(
        dispatcher_mod, "dispatch_to_controller", _fake_dispatch
    )
    # The tool imports the symbol at module load — patch THERE too.
    from tesseract.kernel.tools import delegate_tars_controller as tool_mod
    monkeypatch.setattr(
        tool_mod, "dispatch_to_controller", _fake_dispatch
    )

    tool = DelegateTarsControllerTool()
    result = await tool.run(
        DelegateTarsControllerInput(
            task="patch the auth middleware",
            title="auth-fix",
            idle_timeout_seconds=42.0,
        ),
        _ctx(),
    )

    assert captured["prompt"] == "patch the auth middleware"
    assert captured["origin"] == "autonomy"
    assert captured["mode"] == "autonomy"
    assert captured["title"] == "auth-fix"
    assert captured["wait_for_completion"] is True
    assert captured["idle_timeout_seconds"] == 42.0

    assert result.is_error is False
    assert result.output == "orchestrated reply"
    assert result.metadata is not None
    assert result.metadata["session_id"] == "sess-1"


@pytest.mark.asyncio
async def test_tool_maps_timed_out_dispatch_to_timed_out_result(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_dispatch(prompt: str, **kwargs: Any) -> DispatchResult:
        return DispatchResult(
            session_id="sess-2",
            assistant_text="",
            saw_assistant_text=False,
            timed_out=True,
        )

    from tesseract.kernel.tools import delegate_tars_controller as tool_mod
    monkeypatch.setattr(tool_mod, "dispatch_to_controller", _fake_dispatch)

    tool = DelegateTarsControllerTool()
    result = await tool.run(
        DelegateTarsControllerInput(task="x", idle_timeout_seconds=10.0),
        _ctx(),
    )

    assert result.is_error is True
    assert result.timed_out is True
    assert result.metadata is not None
    assert result.metadata.get("timed_out") is True
    assert result.metadata["session_id"] == "sess-2"


@pytest.mark.asyncio
async def test_tool_maps_dispatcher_error_to_error_result(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_dispatch(prompt: str, **kwargs: Any) -> DispatchResult:
        raise DispatcherError("daemon unreachable")

    from tesseract.kernel.tools import delegate_tars_controller as tool_mod
    monkeypatch.setattr(tool_mod, "dispatch_to_controller", _fake_dispatch)

    tool = DelegateTarsControllerTool()
    result = await tool.run(
        DelegateTarsControllerInput(task="x"),
        _ctx(),
    )

    assert result.is_error is True
    assert "daemon unreachable" in result.output
    assert result.metadata is not None
    assert result.metadata["reason"] == "dispatcher_error"


@pytest.mark.asyncio
async def test_tool_no_assistant_text_surfaces_as_error(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_dispatch(prompt: str, **kwargs: Any) -> DispatchResult:
        return DispatchResult(
            session_id="sess-3",
            assistant_text="",
            saw_assistant_text=False,
            timed_out=False,
            cancelled=False,
        )

    from tesseract.kernel.tools import delegate_tars_controller as tool_mod
    monkeypatch.setattr(tool_mod, "dispatch_to_controller", _fake_dispatch)

    tool = DelegateTarsControllerTool()
    result = await tool.run(
        DelegateTarsControllerInput(task="x"),
        _ctx(),
    )

    assert result.is_error is True
    assert "no assistant_text" in result.output
