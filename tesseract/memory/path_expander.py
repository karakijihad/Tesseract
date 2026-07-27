"""Stage D path expansion — 1-hop link following with scored neighbors.

Follows intentional links (first-class, +0.2 boost) and auto_links
(lower confidence) from selected memories. Scores neighbors by cosine
similarity to query. Uses pre-embedded query vector when available.
Caps at 3 neighbors total.
"""

from __future__ import annotations

import logging

import numpy as np

from tesseract.memory.embeddings import EmbeddingIndex
from tesseract.memory.store import MemoryStore

logger = logging.getLogger(__name__)

_COSINE_THRESHOLD = 0.5
_INTENTIONAL_BOOST = 0.2
_MAX_NEIGHBORS = 3


class PathExpander:
    def __init__(self, store: MemoryStore, embeddings: EmbeddingIndex) -> None:
        self._store = store
        self._embeddings = embeddings

    async def expand(
        self,
        selected: list,
        query: str,
        query_vector: np.ndarray | None = None,
        max_neighbors: int = _MAX_NEIGHBORS,
    ) -> list:
        """Follow links 1-hop from selected memories, score and return neighbors."""
        from tesseract.memory.retrieval import RetrievalResult

        if not selected:
            return []

        selected_ids = {r.memory_id for r in selected}

        # Collect neighbor IDs with their link type
        intentional_ids: set[str] = set()
        auto_ids: set[str] = set()

        for result in selected:
            fm_result = self._store.read(result.memory_id, log_access=False)
            if fm_result is None:
                continue
            fm, _ = fm_result
            for link_id in fm.links:
                if link_id not in selected_ids:
                    intentional_ids.add(link_id)
            for link_id in fm.auto_links:
                if link_id not in selected_ids:
                    auto_ids.add(link_id)

        all_neighbor_ids = list(intentional_ids | auto_ids)
        if not all_neighbor_ids:
            return []

        # Score neighbors against query via embeddings
        try:
            if query_vector is not None:
                search_results = self._embeddings.search_by_vector(
                    query_vector,
                    top_k=len(all_neighbor_ids),
                    candidate_ids=all_neighbor_ids,
                )
            else:
                search_results = await self._embeddings.search(
                    query,
                    top_k=len(all_neighbor_ids),
                    candidate_ids=all_neighbor_ids,
                )
        except Exception:
            logger.warning("Path expansion: embeddings failed, skipping D")
            return []

        # Apply scoring boost for intentional links and filter by threshold
        scored: list[tuple[str, float]] = []
        for mem_id, cosine in search_results:
            boosted = cosine + _INTENTIONAL_BOOST if mem_id in intentional_ids else cosine
            if boosted > _COSINE_THRESHOLD:
                scored.append((mem_id, boosted))

        scored.sort(key=lambda x: x[1], reverse=True)
        scored = scored[:max_neighbors]

        # Load full bodies for passing neighbors
        neighbors: list[RetrievalResult] = []
        for mem_id, score in scored:
            read_result = self._store.read(mem_id, log_access=False)
            if read_result is None:
                continue
            fm, body = read_result
            neighbors.append(RetrievalResult(
                memory_id=mem_id,
                title=fm.title,
                body=body,
                score=score,
                mem_type=fm.type,
                provenance=("neighbor",),
                confidence=fm.confidence,
            ))

        return neighbors
