"""activity.* MCP verbs.

``activity.list`` returns the current registry snapshot. ``activity.watch`` is
the (deferred) server→client streaming surface, not a callable tool.
``activity.cancel`` (ASK) stops a running unit of work, dispatching to the
substrate by kind.
"""

from __future__ import annotations

from typing import Any

from tesseract.kernel.tools.lane_close import LaneCloseInput
from tesseract.mirror.server.mcp.verbs._base import (
    MCPVerbError,
    VerbContext,
    run_kernel_tool,
)
from tesseract.orchestrator.activity import get_activity_registry


async def activity_list(ctx: VerbContext) -> list[dict[str, Any]]:
    snapshot = get_activity_registry().snapshot()
    return [r.model_dump() for r in snapshot]


async def activity_cancel(ctx: VerbContext) -> str:
    activity_id = str(ctx.params.get("activity_id") or "").strip()
    if not activity_id:
        raise MCPVerbError(400, "activity.cancel requires 'activity_id'")
    record = get_activity_registry().get(activity_id)
    if record is None:
        raise MCPVerbError(404, f"unknown activity: {activity_id}")

    if record.kind == "delegate":
        return await _cancel_delegate(ctx, activity_id)
    if record.kind == "lane":
        lane_id = activity_id.split(":", 1)[1]
        await run_kernel_tool(
            ctx,
            "lane_close",
            LaneCloseInput(lane_id=lane_id, reason="mcp_activity_cancel"),
            ask_fn=ctx.ask_fn,
        )
        return f"closed lane {lane_id}"
    if record.kind == "mcp_session":
        server = ctx.app.get("mcp_server")
        if server is None or not server.cancel_session(activity_id):
            raise MCPVerbError(404, f"mcp session not connected: {activity_id}")
        return f"closed mcp session {activity_id}"

    # session / routine / autonomy have no single-unit cancel reachable from
    # here — each is driven by its own dedicated verb or route.
    raise MCPVerbError(
        400,
        f"activity.cancel does not support kind '{record.kind}' — use its "
        f"dedicated verb or route instead.",
    )


async def _cancel_delegate(ctx: VerbContext, activity_id: str) -> str:
    """Cancel a background delegate spawn — find its owning session's
    SpawnRegistry (the same iteration the app-shutdown path uses) and cancel."""
    handle_id = activity_id.split(":", 1)[1]
    for sess in (ctx.app.get("server_sessions") or {}).values():
        spawns = getattr(getattr(sess, "chat_session", None), "spawns", None)
        if spawns is None:
            continue
        if await spawns.cancel(handle_id):
            return f"cancelled delegate {handle_id}"
    raise MCPVerbError(404, f"delegate not found in any active session: {handle_id}")


__all__ = ["activity_list", "activity_cancel"]
