"""Shared verb-dispatch primitives.

Every MCP verb resolves through the same governed path as an in-process tool
call: the kernel-tool-backed verbs (memory/vault) run through
``permissions.decide.evaluate`` (security floor → path validation → operator
policy) before ``tool.run``, so no MCP path bypasses a gate (HANDOFF §2.4).
Pure-registry verbs (activity) carry no kernel tool; their posture is governed
by the ``mcp.yaml`` verb allowlist at the dispatcher.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from aiohttp import web
from pydantic import BaseModel, ValidationError

from tesseract.config.mcp import MCPClient
from tesseract.kernel.tools.base import ToolContext
from tesseract.permissions import decide


@dataclass
class VerbContext:
    """Per-call context handed to a verb handler."""

    app: web.Application
    params: dict[str, Any]
    client: MCPClient
    session_activity_id: str
    # Operator-approval callback for the tool's own permissions.yaml posture
    # (independent of the MCP verb floor the dispatcher already gated). None →
    # decide.evaluate applies its no-ask_fn rule (read-only auto-allow / write
    # deny). Set by the dispatcher.
    ask_fn: Any = None


class MCPVerbError(Exception):
    """A verb failed with a specific HTTP status (non-permission)."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class MCPPermissionDenied(Exception):
    """A verb was denied by the permission stack → 403."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def run_kernel_tool(
    ctx: VerbContext,
    tool_name: str,
    tool_input: BaseModel,
    *,
    ask_fn=None,
) -> str:
    """Run a kernel tool through the full permission pipeline.

    ``ask_fn`` is forwarded to ``decide.evaluate`` — it enforces the tool's own
    ``permissions.yaml`` posture (which is independent of the MCP verb floor the
    dispatcher already gated). For AUTO tool-backed verbs it never fires; when a
    tool's own policy is ASK it is the operator gate. The absolute security +
    path-validation layers run regardless of ``ask_fn``.

    Raises :class:`MCPPermissionDenied` on a policy/security denial and
    :class:`MCPVerbError` when the tool is unavailable or returns an error.
    Returns the tool's text output on success.
    """
    registry = ctx.app.get("tool_registry")
    if registry is None or tool_name not in getattr(registry, "tools", {}):
        raise MCPVerbError(503, f"{tool_name} unavailable (tool registry not ready)")
    tool = registry.tools[tool_name]
    policy = ctx.app["config"].permissions
    context = _mcp_tool_context(ctx, ask_fn)
    # A tool that dispatches to another tool (`open` → os_launch) reads the
    # policy off the context. This path calls `decide.evaluate` directly rather
    # than going through `brain.tools.execute_tool`, so it must do that sync
    # itself — without it the nested call proceeds at PASSTHROUGH and skips the
    # posture the operator configured.
    context.policy = policy
    raw = tool_input.model_dump()
    denial = await decide.evaluate(tool, tool_input, raw, context, ask_fn=ask_fn, policy=policy)
    if denial is not None:
        raise MCPPermissionDenied(denial.deny_reason or denial.output)
    result = await tool.run(tool_input, context)
    if result.is_error:
        raise MCPVerbError(400, result.output)
    return result.output


def _mcp_tool_context(ctx: VerbContext, ask_fn: Any) -> ToolContext:
    """Build the ToolContext for an MCP-driven kernel-tool call, wiring the
    substrate providers off ``ctx.app`` exactly as the chat path does
    (``session.py::_build_chat_session``) so lane/schedule verbs reach
    their live managers. Lane managers are the controller IPC proxies (fresh
    per call — no ``app`` slot), matching the chat wiring."""
    from tesseract.orchestrator.tars_controller.lanes.ipc_proxy import (
        IpcLaneManager,
        IpcNamedLaneManager,
    )

    app = ctx.app
    return ToolContext(
        workspace_root=str(app.get("repo_root") or "."),
        session_id=ctx.session_activity_id,
        current_call_id=f"mcp-{uuid.uuid4().hex}",
        ask_fn=ask_fn,
        scheduler_provider=lambda: app.get("scheduler"),
        tool_registry_provider=lambda: app.get("tool_registry"),
        lane_manager_provider=IpcLaneManager,
        named_lane_manager_provider=IpcNamedLaneManager,
    )


def make_tool_verb(
    tool_name: str, input_cls: type[BaseModel]
) -> Callable[[VerbContext], Awaitable[str]]:
    """Build a verb handler that validates ``ctx.params`` into ``input_cls`` and
    runs ``tool_name`` through the permission pipeline. Bad params → 400 (Pydantic
    v2 ``ValidationError`` is not a ``ValueError``, so it is caught explicitly)."""

    async def _handler(ctx: VerbContext) -> str:
        try:
            tool_input = input_cls.model_validate(ctx.params)
        except (TypeError, ValueError, ValidationError) as exc:
            raise MCPVerbError(400, f"{tool_name} invalid params: {exc}")
        return await run_kernel_tool(ctx, tool_name, tool_input, ask_fn=ctx.ask_fn)

    _handler.__name__ = f"verb_{tool_name}"
    # Carried for MCP tools/list — the accurate inputSchema source (tools.py).
    _handler.mcp_input_model = input_cls
    return _handler


__all__ = [
    "VerbContext",
    "MCPVerbError",
    "MCPPermissionDenied",
    "run_kernel_tool",
    "make_tool_verb",
]
