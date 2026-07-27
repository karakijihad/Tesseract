"""Stage 10 — headless quarantine-write carve-out in `permissions/decide.py`.

A non-read-only tool whose CLASS declares `headless_quarantine_write = True`
may proceed when its ASK finds no ask_fn wired (unattended context). The
operator gate moves from write-time to activation-time — safe only because
such tools write exclusively to quarantine (agents/pending/). Everything
else about the no-ask_fn contract stays: undeclared tools deny, attended
sessions still ASK, and the flag is honored from the class only (an
instance attribute must not count — a compromised call path could set one).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel

from tesseract.brain.tools import ToolRegistry, execute_tool
from tesseract.kernel.tools.base import (
    PermissionResult,
    Tool,
    ToolContext,
    ToolResult,
)


class _NoInput(BaseModel):
    pass


class _AskingWriteTool(Tool):
    """Non-read-only tool that ASKs unconditionally — agent_create shape."""

    default_posture = "ask"
    risk_class = "propose"

    def __init__(self) -> None:
        self.ran = False

    @property
    def name(self) -> str:
        return "_test_asking_write"

    @property
    def description(self) -> str:
        return "test"

    @property
    def input_schema(self) -> type[BaseModel]:
        return _NoInput

    def is_read_only(self) -> bool:
        return False

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.ASK

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        self.ran = True
        return ToolResult(output="ok")


class _CarveoutTool(_AskingWriteTool):
    headless_quarantine_write = True

    @property
    def name(self) -> str:
        return "_test_carveout_write"


def _execute(tool: Tool, ask_fn=None) -> ToolResult:
    registry = ToolRegistry()
    registry.register(tool)
    return asyncio.run(execute_tool(
        registry=registry,
        tool_name=tool.name,
        tool_input={},
        context=ToolContext(workspace_root="."),
        ask_fn=ask_fn,
        policy=None,
    ))


def test_flagged_tool_runs_headless(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    tool = _CarveoutTool()
    result = _execute(tool)
    assert not result.is_error
    assert tool.ran


def test_unflagged_tool_still_denies_headless(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    tool = _AskingWriteTool()
    result = _execute(tool)
    assert result.is_error
    assert result.denied_hard
    assert not tool.ran


def test_flag_ignored_when_ask_fn_wired(tmp_path: Path, monkeypatch) -> None:
    """Attended sessions still go through the operator — carve-out is
    strictly the no-ask_fn branch."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    tool = _CarveoutTool()
    asked: list[str] = []

    async def deny(t, validated, context):
        asked.append(t.name)
        return False

    result = _execute(tool, ask_fn=deny)
    assert asked == [tool.name]
    assert result.is_error
    assert not tool.ran


def test_instance_flag_does_not_count(tmp_path: Path, monkeypatch) -> None:
    """Only the CLASS attribute (kernel-owned source) is honored."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    tool = _AskingWriteTool()
    tool.headless_quarantine_write = True  # instance-level — must be ignored
    result = _execute(tool)
    assert result.is_error
    assert not tool.ran
