"""atlas.* MCP verbs.

``atlas.query`` (read) maps to the `atlas_query` kernel tool and runs through
the full permission pipeline, exactly as the in-process caller does. That is
the whole point of the verb being this thin: an MCP client and an agent inside
the runtime ask one implementation, so the same question cannot come back two
ways.
"""

from __future__ import annotations

from pydantic import ValidationError

from tesseract.kernel.tools.atlas_query import AtlasQueryInput
from tesseract.mirror.server.mcp.verbs._base import (
    MCPVerbError,
    VerbContext,
    run_kernel_tool,
)


async def atlas_query(ctx: VerbContext) -> str:
    query = str(ctx.params.get("query") or "").strip()
    start_at = ctx.params.get("start_at") or []
    if not query and not start_at:
        raise MCPVerbError(
            400, "atlas.query requires a 'query' or a non-empty 'start_at'"
        )
    try:
        tool_input = AtlasQueryInput.model_validate(ctx.params)
    except (TypeError, ValueError, ValidationError) as exc:
        raise MCPVerbError(400, f"atlas.query invalid params: {exc}")
    return await run_kernel_tool(ctx, "atlas_query", tool_input, ask_fn=ctx.ask_fn)


# tools/list schema source (mcp/tools.py::_input_schema). Without the model
# attached the verb advertises `additionalProperties: true` and a client has to
# guess at `trust`, `kinds` and `depth`, which are the three params that decide
# what comes back.
atlas_query.mcp_input_model = AtlasQueryInput

__all__ = ["atlas_query"]
