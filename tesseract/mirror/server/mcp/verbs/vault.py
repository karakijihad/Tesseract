"""vault.* MCP verbs.

``vault.search`` (read, BM25+vector), ``vault.query`` (read, wiki synthesis),
``vault.ingest`` (write, ASK floor) each map to their kernel tool and run
through the full permission pipeline.
"""

from __future__ import annotations

from pydantic import ValidationError

from tesseract.kernel.tools.vault_ingest import VaultIngestInput
from tesseract.kernel.tools.vault_query import VaultQueryInput
from tesseract.kernel.tools.vault_search import VaultSearchInput
from tesseract.mirror.server.mcp.verbs._base import (
    MCPVerbError,
    VerbContext,
    run_kernel_tool,
)


async def vault_search(ctx: VerbContext) -> str:
    query = str(ctx.params.get("query") or "").strip()
    if not query:
        raise MCPVerbError(400, "vault.search requires a non-empty 'query'")
    tool_input = VaultSearchInput(
        query=query,
        top_k=ctx.params.get("top_k"),
        category=ctx.params.get("category"),
    )
    return await run_kernel_tool(ctx, "vault_search", tool_input, ask_fn=ctx.ask_fn)


async def vault_query(ctx: VerbContext) -> str:
    query = str(ctx.params.get("query") or "").strip()
    if not query:
        raise MCPVerbError(400, "vault.query requires a non-empty 'query'")
    tool_input = VaultQueryInput(
        query=query,
        topic_filter=ctx.params.get("topic_filter"),
    )
    return await run_kernel_tool(ctx, "vault_query", tool_input, ask_fn=ctx.ask_fn)


async def vault_ingest(ctx: VerbContext) -> str:
    source_path = str(ctx.params.get("source_path") or "").strip()
    if not source_path:
        raise MCPVerbError(400, "vault.ingest requires a non-empty 'source_path'")
    try:
        tool_input = VaultIngestInput.model_validate(ctx.params)
    except (TypeError, ValueError, ValidationError) as exc:
        raise MCPVerbError(400, f"vault.ingest invalid params: {exc}")
    return await run_kernel_tool(ctx, "vault_ingest", tool_input, ask_fn=ctx.ask_fn)


# tools/list schema source (mcp/tools.py::_input_schema) — same fix as
# memory.save/update (P7 live gate): without the model attached, vault.ingest
# advertised a vague `additionalProperties: true` schema and clients guessed
# at param shapes (trio W1 schema curation).
vault_ingest.mcp_input_model = VaultIngestInput

__all__ = ["vault_search", "vault_query", "vault_ingest"]
