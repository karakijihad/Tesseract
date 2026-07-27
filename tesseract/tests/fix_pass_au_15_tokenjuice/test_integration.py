"""AU-15: end-to-end integration through brain.tools.execute_tool.

Verifies that TokenJuice fires on a real tool result via execute_tool
and that the test harness keeps audit writes out of the production
logs tree.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from tesseract.brain.tools import ToolRegistry, execute_tool, reset_tokenjuice_cache
from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult


class _NoiseInput(BaseModel):
    command: str = "git status -s"


class _BigGitStatusTool(Tool):
    default_posture = "auto"
    risk_class = "autonomous"

    @property
    def name(self) -> str:
        return "bash_exec"

    @property
    def description(self) -> str:
        return "test stub mimicking bash_exec"

    @property
    def input_schema(self) -> type[BaseModel]:
        return _NoiseInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        # Return a long git-status-shaped output so the builtin rule fires.
        lines = ["On branch main"]
        lines += [f"  modified: file{i}.py" for i in range(80)]
        lines += ["  (use \"git add ...\" to stage)"]
        return ToolResult(output="\n".join(lines))


@pytest.fixture(autouse=True)
def _tokenjuice_home_isolation(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    reset_tokenjuice_cache()
    yield
    reset_tokenjuice_cache()


def _run(coro):
    return asyncio.run(coro)


def test_execute_tool_compresses_long_git_status_output(tmp_path: Path):
    registry = ToolRegistry()
    registry.register(_BigGitStatusTool())
    context = ToolContext()

    result = _run(
        execute_tool(
            registry,
            "bash_exec",
            {"command": "git status -s"},
            context,
        )
    )

    assert not result.is_error
    # 81 lines + header should compress with head_tail(40, 10) → far fewer.
    line_count = result.output.count("\n") + 1
    assert line_count < 60, f"expected compression, got {line_count} lines"
    assert "lines elided" in result.output

    # Audit landed in monkeypatched HOME, not the real logs tree.
    audit_path = tmp_path / "logs" / "tokenjuice" / "audit.jsonl"
    assert audit_path.exists()


def test_execute_tool_passthrough_for_short_output(tmp_path: Path):
    class _ShortTool(Tool):
        default_posture = "auto"
        risk_class = "autonomous"

        @property
        def name(self) -> str:
            return "bash_exec"

        @property
        def description(self) -> str:
            return "short"

        @property
        def input_schema(self) -> type[BaseModel]:
            return _NoiseInput

        async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
            return ToolResult(output="On branch main\nnothing to commit")

    registry = ToolRegistry()
    registry.register(_ShortTool())
    context = ToolContext()

    result = _run(
        execute_tool(
            registry,
            "bash_exec",
            {"command": "git status -s"},
            context,
        )
    )
    # Below 800-char passthrough threshold.
    assert "nothing to commit" in result.output


def test_execute_tool_unrelated_tool_passthrough(tmp_path: Path):
    class _UnrelatedTool(Tool):
        default_posture = "auto"
        risk_class = "autonomous"

        @property
        def name(self) -> str:
            return "no_matching_rule_tool"

        @property
        def description(self) -> str:
            return "unrelated"

        @property
        def input_schema(self) -> type[BaseModel]:
            return _NoiseInput

        async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
            return ToolResult(output="x" * 5000)

    registry = ToolRegistry()
    registry.register(_UnrelatedTool())
    context = ToolContext()

    result = _run(
        execute_tool(
            registry,
            "no_matching_rule_tool",
            {"command": "ignored"},
            context,
        )
    )
    assert result.output == "x" * 5000  # no rule → passthrough
