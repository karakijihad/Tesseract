"""vault_search tool — search vault contents via hybrid BM25 + vector search."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.memory.embeddings import EmbeddingIndex
from tesseract.memory.fts_index import FTSIndex
from tesseract.memory.vault_manager import VaultManager

if TYPE_CHECKING:
    from tesseract.brain.boot import VaultConfig


class VaultSearchInput(BaseModel):
    query: str = Field(description="Search query across vault documents")
    top_k: int | None = Field(default=None, ge=1, le=20, description="Maximum results to return (defaults to vault.yaml search.default_top_k)")
    category: str | None = Field(default=None, description="Filter to a vault category (research, articles, data, uploads, snapshots, media)")


class VaultSearchTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"
    # Audit-3 M9 — see VaultQueryTool.untrusted_source.
    untrusted_source: ClassVar[bool] = True

    def __init__(
        self,
        embeddings: EmbeddingIndex | None,
        fts_index: FTSIndex | None,
        vault_manager: VaultManager,
        vault_cfg: "VaultConfig",
        reranker: object | None = None,
    ) -> None:
        self._embeddings = embeddings
        self._fts_index = fts_index
        self._manager = vault_manager
        self._rrf_k = vault_cfg.search_rrf_k
        self._default_top_k = vault_cfg.search_default_top_k
        self._reranker = reranker

    @property
    def name(self) -> str:
        return "vault_search"

    @property
    def description(self) -> str:
        return "Search raw source material in the vault (PDFs, articles, data files, web snapshots). Returns matching chunks with source paths."

    @property
    def input_schema(self) -> type[BaseModel]:
        return VaultSearchInput

    def is_read_only(self) -> bool:
        return True

    def is_concurrency_safe(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = tool_input if isinstance(tool_input, VaultSearchInput) else VaultSearchInput(**tool_input.model_dump())

        if not inp.query.strip():
            return ToolResult(output="Empty query.", is_error=True)

        top_k = inp.top_k if inp.top_k is not None else self._default_top_k
        scores: dict[str, float] = {}

        # BM25 search via FTS5 — prefix-filtered SQL-side so memory rows in
        # the shared table cannot crowd the fetch window.
        if self._fts_index is not None:
            fts_results = self._fts_index.search(
                inp.query, limit=top_k * 3, require_prefix="vault:"
            )
            rank = 0
            for mem_id, _score in fts_results:
                if not mem_id.startswith("vault:"):
                    continue
                if inp.category and not self._matches_category(mem_id, inp.category):
                    continue
                scores[mem_id] = scores.get(mem_id, 0.0) + 1.0 / (self._rrf_k + rank)
                rank += 1

        # Vector search via FAISS — only when embeddings are online.
        try:
            if self._embeddings is None:
                raise RuntimeError("embeddings offline")
            vec = await self._embeddings.embed_text(inp.query)
            if vec is not None:
                import numpy as np
                import faiss  # type: ignore[import-untyped]

                arr = np.array([vec], dtype=np.float32)
                faiss.normalize_L2(arr)
                vector_results = self._embeddings.search_by_vector(
                    arr[0], top_k=top_k * 3, require_prefix="vault:",
                )
                rank = 0
                for mem_id, _score in vector_results:
                    if not mem_id.startswith("vault:"):
                        continue
                    if inp.category and not self._matches_category(mem_id, inp.category):
                        continue
                    scores[mem_id] = scores.get(mem_id, 0.0) + 1.0 / (self._rrf_k + rank)
                    rank += 1
        except Exception:
            pass  # vector search is best-effort

        if not scores:
            return ToolResult(output="No vault results found.")

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        # Cross-encoder precision pass over the fused pool. Best-effort:
        # any failure (or missing model) keeps the RRF order. Chunks whose
        # text is unavailable keep their RRF position after the scored ones.
        reranked = False
        if self._reranker is not None:
            pool = [
                (cid, text)
                for cid, _s in ranked
                if (text := self._get_chunk_text(cid))
            ]
            try:
                scored = await self._reranker.rerank(inp.query, pool)
            except Exception:
                scored = None
            if scored:
                scored_ids = {cid for cid, _s in scored}
                ranked = scored + [(c, s) for c, s in ranked if c not in scored_ids]
                reranked = True

        ranked = ranked[:top_k]

        # Format results
        header = f"Found {len(ranked)} vault result(s)"
        parts: list[str] = [f"{header} (cross-encoder reranked):\n" if reranked else f"{header}:\n"]
        for chunk_id, score in ranked:
            # Parse vault_rel_path from chunk_id: "vault:{path}:chunk_{N}"
            stripped = chunk_id.removeprefix("vault:")
            path_part = stripped.rsplit(":chunk_", 1)[0] if ":chunk_" in stripped else stripped

            # Try to get chunk text from FTS
            chunk_text = self._get_chunk_text(chunk_id)
            excerpt = chunk_text[:500] + "..." if len(chunk_text) > 500 else chunk_text

            parts.append(f"### {path_part}")
            parts.append(f"Score: {score:.4f}")
            if excerpt:
                parts.append(excerpt)
            parts.append("")

        return ToolResult(output="\n".join(parts))

    @staticmethod
    def _matches_category(chunk_id: str, category: str) -> bool:
        """Check if a vault chunk ID belongs to the given category."""
        stripped = chunk_id.removeprefix("vault:")
        return stripped.startswith(f"{category}/")

    def _get_chunk_text(self, chunk_id: str) -> str:
        """Retrieve chunk text from FTS body column."""
        if self._fts_index is None:
            return ""
        return self._fts_index.get_body(chunk_id) or ""
