"""Audit C1 / M6 regression — when a tool resolves to ASK and no `ask_fn`
is wired (headless / programmatic / unattended), the executor must DENY
non-read-only tools instead of silently auto-allowing.

Before 2026-04-29, `execute_tool` logged "no ask_fn wired, auto-allowing"
and ran the tool. That defeated the ASK contract for write tools and let
`headless` mode `agent_create: auto` (and any future ASK posture without
an attached approval channel) execute without any operator review.

Read-only tools still fall through to allow because they are inert from
the operator's perspective.
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


class _NopInput(BaseModel):
    pass


class _AlwaysAskWriteTool(Tool):
    """Mimics a write-side tool that always asks (e.g. agent_create)."""

    @property
    def name(self) -> str:
        return "_test_write_ask"

    @property
    def description(self) -> str:
        return "test"

    @property
    def input_schema(self) -> type[BaseModel]:
        return _NopInput

    def is_read_only(self) -> bool:
        return False

    def is_concurrency_safe(self) -> bool:
        return False

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.ASK

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        return ToolResult(output="ran")


class _AlwaysAskReadTool(Tool):
    @property
    def name(self) -> str:
        return "_test_read_ask"

    @property
    def description(self) -> str:
        return "test"

    @property
    def input_schema(self) -> type[BaseModel]:
        return _NopInput

    def is_read_only(self) -> bool:
        return True

    def is_concurrency_safe(self) -> bool:
        return True

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.ASK

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        return ToolResult(output="ran")


def test_write_tool_denied_when_no_ask_fn(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(_AlwaysAskWriteTool())
    result = asyncio.run(execute_tool(
        registry=registry,
        tool_name="_test_write_ask",
        tool_input={},
        context=ToolContext(workspace_root=str(tmp_path)),
        ask_fn=None,
    ))
    assert result.is_error
    assert result.denied_hard
    assert "approval channel" in result.deny_reason or "approval" in result.output


def test_read_tool_allowed_when_no_ask_fn(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(_AlwaysAskReadTool())
    result = asyncio.run(execute_tool(
        registry=registry,
        tool_name="_test_read_ask",
        tool_input={},
        context=ToolContext(workspace_root=str(tmp_path)),
        ask_fn=None,
    ))
    assert not result.is_error
    assert result.output == "ran"


def test_write_tool_allowed_when_ask_fn_approves(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(_AlwaysAskWriteTool())

    async def ask_yes(tool, validated, context):
        return True

    result = asyncio.run(execute_tool(
        registry=registry,
        tool_name="_test_write_ask",
        tool_input={},
        context=ToolContext(workspace_root=str(tmp_path)),
        ask_fn=ask_yes,
    ))
    assert not result.is_error
    assert result.output == "ran"


def test_write_tool_declined_returns_clear_message(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(_AlwaysAskWriteTool())

    async def ask_no(tool, validated, context):
        return False

    result = asyncio.run(execute_tool(
        registry=registry,
        tool_name="_test_write_ask",
        tool_input={},
        context=ToolContext(workspace_root=str(tmp_path)),
        ask_fn=ask_no,
    ))
    assert result.is_error
    assert "declined" in result.output.lower()
