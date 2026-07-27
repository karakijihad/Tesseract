"""Coverage for ``tesseract.scripts.slash_dispatch.run_slash``.

Builds a minimal :class:`ToolRegistry` with one fake tool and verifies the
operator-slash invocation pipeline: validation, DENY-blocking, ASK-bypass,
exception handling, suggestion fallback for unknown names.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from tesseract.brain.tools import ToolRegistry
from tesseract.kernel.tools.base import (
    PermissionResult,
    Tool,
    ToolContext,
    ToolResult,
)
from tesseract.scripts.slash_dispatch import _OPERATOR_TOKEN, run_slash


class _EchoInput(BaseModel):
    text: str


class _EchoTool(Tool):
    default_posture = "auto"

    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "echoes the input"

    @property
    def input_schema(self) -> type[BaseModel]:
        return _EchoInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: _EchoInput = tool_input  # type: ignore[assignment]
        return ToolResult(
            output=f"echo: {inp.text}",
            metadata={"posture_source": context.posture_source},
        )


class _DenyTool(_EchoTool):
    @property
    def name(self) -> str:
        return "deny_me"

    def check_permissions(self, tool_input, context) -> PermissionResult:
        return PermissionResult.DENY


class _ExplodingTool(_EchoTool):
    @property
    def name(self) -> str:
        return "boom"

    async def run(self, tool_input, context) -> ToolResult:
        raise RuntimeError("kaboom")


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(_EchoTool())
    reg.register(_DenyTool())
    reg.register(_ExplodingTool())
    return reg


@pytest.fixture
def ctx() -> ToolContext:
    return ToolContext(workspace_root=".", session_id="test-session")


async def _slash(*args, **kwargs):
    """Test-side wrapper that injects the operator caller token."""
    return await run_slash(*args, caller_token=_OPERATOR_TOKEN, **kwargs)


@pytest.mark.asyncio
async def test_happy_path_kv(registry, ctx):
    out = await _slash(registry, "echo", {"text": "hi"}, [], ctx)
    assert "echo: hi" in out
    # Posture-source audit hint is set on slash invocations.
    assert "operator-slash" in out


@pytest.mark.asyncio
async def test_happy_path_positional(registry, ctx):
    out = await _slash(registry, "echo", {}, ["hello", "world"], ctx)
    assert "echo: hello world" in out


@pytest.mark.asyncio
async def test_unknown_tool_returns_suggestions(registry, ctx):
    out = await _slash(registry, "echos", {}, [], ctx)
    assert "unknown command" in out
    assert "echo" in out  # suggestion should hit the closest match


@pytest.mark.asyncio
async def test_unknown_tool_no_close_match(registry, ctx):
    out = await _slash(registry, "totallyunrelated", {}, [], ctx)
    assert "unknown command" in out
    assert "/help" in out


@pytest.mark.asyncio
async def test_deny_blocks(registry, ctx):
    out = await _slash(registry, "deny_me", {"text": "hi"}, [], ctx)
    assert "denied" in out


@pytest.mark.asyncio
async def test_invalid_args_shows_usage(registry, ctx):
    out = await _slash(registry, "echo", {"ghost": "x"}, [], ctx)
    assert "invalid args" in out
    assert "/echo" in out


@pytest.mark.asyncio
async def test_tool_exception_caught(registry, ctx):
    out = await _slash(registry, "boom", {"text": "hi"}, [], ctx)
    assert "tool error" in out
    assert "kaboom" in out


# Path-validation gate: operator slash MUST NOT bypass kernel-lockdown
# workspace boundary checks for write tools (e.g. file_write).
class _FileWriteInput(BaseModel):
    file_path: str
    content: str


class _FakeFileWrite(Tool):
    """Stand-in for `file_write` — only exists to verify the path-gate fires."""

    default_posture = "ask"

    @property
    def name(self) -> str:
        return "file_write"  # decide.py:50 _WRITE_PATH_TOOLS uses this exact name

    @property
    def description(self) -> str:
        return "fake file write"

    @property
    def input_schema(self) -> type[BaseModel]:
        return _FileWriteInput

    async def run(self, tool_input, context) -> ToolResult:
        return ToolResult(output=f"wrote {tool_input.file_path}")


@pytest.mark.asyncio
async def test_path_validation_blocks_workspace_escape(tmp_path):
    reg = ToolRegistry()
    reg.register(_FakeFileWrite())
    ctx = ToolContext(workspace_root=str(tmp_path), session_id="t")
    out = await _slash(
        reg, "file_write",
        {"file_path": "../../etc/passwd", "content": "x"}, [], ctx,
    )
    assert "denied" in out.lower()
    assert "path validation" in out.lower()


@pytest.mark.asyncio
async def test_path_validation_allows_inside_workspace(tmp_path):
    reg = ToolRegistry()
    reg.register(_FakeFileWrite())
    ctx = ToolContext(workspace_root=str(tmp_path), session_id="t")
    target = tmp_path / "ok.txt"
    out = await _slash(
        reg, "file_write",
        {"file_path": str(target), "content": "x"}, [], ctx,
    )
    assert "wrote" in out


# Operator-only sentinel guard: TARS (any non-operator caller) cannot
# reach run_slash because run_slash bypasses permissions.yaml policy
# posture. The check is identity-based (`is _OPERATOR_TOKEN`); a forged
# `object()` must not satisfy it.
@pytest.mark.asyncio
async def test_run_slash_rejects_missing_caller_token(registry, ctx):
    with pytest.raises(RuntimeError, match="operator-only"):
        await run_slash(registry, "echo", {"text": "hi"}, [], ctx)


@pytest.mark.asyncio
async def test_run_slash_rejects_forged_caller_token(registry, ctx):
    with pytest.raises(RuntimeError, match="operator-only"):
        await run_slash(
            registry, "echo", {"text": "hi"}, [], ctx,
            caller_token=object(),
        )
