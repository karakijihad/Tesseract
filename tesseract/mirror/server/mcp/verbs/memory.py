"""memory.* MCP verbs.

``memory.search`` (read), ``memory.save`` / ``memory.update`` (write, ASK
floor) each map to their kernel tool, run through the full permission pipeline.
"""

from __future__ import annotations

from pydantic import ValidationError

from tesseract.kernel.tools.memory_save import MemorySaveInput
from tesseract.kernel.tools.memory_search import MemorySearchInput
from tesseract.kernel.tools.memory_update import MemoryUpdateInput
from tesseract.mirror.server.mcp.verbs._base import (
    MCPVerbError,
    VerbContext,
    run_kernel_tool,
)


async def memory_search(ctx: VerbContext) -> str:
    query = str(ctx.params.get("query") or "").strip()
    if not query:
        raise MCPVerbError(400, "memory.search requires a non-empty 'query'")
    tool_input = MemorySearchInput(
        query=query,
        type_filter=ctx.params.get("type_filter"),
        scope=ctx.params.get("scope"),
        entity=ctx.params.get("entity"),
        source_slug=ctx.params.get("source_slug"),
        since=ctx.params.get("since"),
    )
    return await run_kernel_tool(ctx, "memory_search", tool_input, ask_fn=ctx.ask_fn)


def _validation_summary(exc: ValidationError) -> str:
    """Turn a Pydantic ``ValidationError`` into a terse required-fields
    summary — a client guessing at param shapes (no advertised schema, or
    a schema it ignored) needs the missing-field names up front, not
    pydantic's multi-line per-error dump."""
    missing = [".".join(str(p) for p in e["loc"]) for e in exc.errors() if e["type"] == "missing"]
    if missing:
        return f"missing required field(s): {', '.join(missing)}"
    return str(exc)


async def memory_save(ctx: VerbContext) -> str:
    try:
        tool_input = MemorySaveInput.model_validate(ctx.params)
    except (TypeError, ValueError, ValidationError) as exc:
        detail = _validation_summary(exc) if isinstance(exc, ValidationError) else str(exc)
        raise MCPVerbError(400, f"memory.save invalid params: {detail}")
    return await run_kernel_tool(ctx, "memory_save", tool_input, ask_fn=ctx.ask_fn)


async def memory_update(ctx: VerbContext) -> str:
    try:
        tool_input = MemoryUpdateInput.model_validate(ctx.params)
    except (TypeError, ValueError, ValidationError) as exc:
        detail = _validation_summary(exc) if isinstance(exc, ValidationError) else str(exc)
        raise MCPVerbError(400, f"memory.update invalid params: {detail}")
    return await run_kernel_tool(ctx, "memory_update", tool_input, ask_fn=ctx.ask_fn)


# tools/list schema source (tesseract/mirror/server/mcp/tools.py::_input_schema) —
# without this, memory.save/update fell back to a vague
# `{"additionalProperties": true}` curated schema (P7 live-gate finding: a
# lane client guessed at param shapes and got error_400 three times).
memory_save.mcp_input_model = MemorySaveInput
memory_update.mcp_input_model = MemoryUpdateInput


__all__ = ["memory_search", "memory_save", "memory_update"]
