"""Write-time auto-linking via embedding similarity.

Discovers related memories when a memory is saved, populates bidirectional
auto_links. auto_links are derived metadata — can be rebuilt from FAISS
at any time via rebuild_auto_links().

Selection layers (audit 2026-05-01):
  1. cosine threshold (`_COSINE_THRESHOLD`) — coarse semantic gate
  2. weak-cosine reject — pairs in [_COSINE_THRESHOLD, _COSINE_STRONG)
     must share at least one entity OR tag, else dropped
  3. score boosts — entity overlap (+_ENTITY_BOOST), same MemoryType
     (+_SAME_TYPE_BOOST). Boosts re-rank within the candidate set; raw
     scores still gate admission.
  4. union re-rank on incremental updates — when a memory already has
     5 links and a new candidate arrives, the union is re-scored against
     the source memory's vector before clipping. Keeps the freshest, most-
     similar 5; older weaker links can be displaced.

Failure surface (M4): `auto_link()` returns `AutoLinkResult` with a status
and reason instead of swallowing silently. `memory_save` logs degraded
runs to events/writes.jsonl and tags the tool result.
"""

from __future__ import annotations

import logging
from typing import Literal, NamedTuple

from tesseract.memory.embeddings import EmbeddingIndex
from tesseract.memory.related_block import RelatedItem, replace_related_block
from tesseract.memory.store import MemoryStore

logger = logging.getLogger(__name__)

_COSINE_THRESHOLD = 0.6
# Pairs at or above this are accepted on cosine alone. Below this, we require
# entity OR tag overlap to admit the pair (M2 weak-cosine reject).
_COSINE_STRONG = 0.75
_MAX_AUTO_LINKS = 5
# Score boosts re-rank inside the candidate set without changing the cosine
# admission gate. Small enough that strong cosine still dominates.
_ENTITY_BOOST = 0.05
_SAME_TYPE_BOOST = 0.02


class AutoLinkResult(NamedTuple):
    """Outcome of an auto-link pass.

    `status="ok"` means neighbors were selected and persisted.
    `status="skipped"` means no edges were written; `reason` says why so
    callers can surface degraded runs (M4).
    """
    status: Literal["ok", "skipped"]
    reason: str
    linked: list[str]


class AutoLinker:
    def __init__(self, store: MemoryStore, embeddings: EmbeddingIndex) -> None:
        self._store = store
        self._embeddings = embeddings

    async def auto_link(self, memory_id: str, body: str) -> AutoLinkResult:
        """Discover neighbors for a newly written memory."""
        if self._embeddings is None:
            return AutoLinkResult("skipped", "embeddings_unavailable", [])

        src = self._store.read(memory_id, log_access=False)
        if src is None:
            return AutoLinkResult("skipped", "source_not_found", [])
        src_fm, _ = src

        try:
            vec = self._embeddings.get_vector(memory_id)
            if vec is not None:
                results = self._embeddings.search_by_vector(
                    vec, top_k=_MAX_AUTO_LINKS * 3 + 1,
                )
            else:
                results = await self._embeddings.search(
                    body, top_k=_MAX_AUTO_LINKS * 3 + 1,
                )
        except Exception:
            logger.warning("Auto-linker: embeddings failed, skipping for %s", memory_id)
            return AutoLinkResult("skipped", "embeddings_failed", [])

        if not results:
            # Either the index is empty or embed_text returned None upstream
            # (Ollama down). Either way: no neighbors to link, surface it.
            return AutoLinkResult("skipped", "no_results", [])

        src_entities = {e.lower() for e in src_fm.entities}
        src_tags = {t.lower() for t in src_fm.tags}

        scored: list[tuple[str, float]] = []
        for nbr_id, score in results:
            if nbr_id == memory_id or score < _COSINE_THRESHOLD:
                continue
            nbr = self._store.read(nbr_id, log_access=False)
            if nbr is None:
                # FAISS knew about an ID the store doesn't — stale index.
                continue
            nbr_fm, _ = nbr

            entity_overlap = bool(src_entities & {e.lower() for e in nbr_fm.entities})
            tag_overlap = bool(src_tags & {t.lower() for t in nbr_fm.tags})
            same_type = src_fm.type == nbr_fm.type

            if score < _COSINE_STRONG and not (entity_overlap or tag_overlap):
                # Weak cosine without any structural support: noisy cross-link.
                continue

            adjusted = score
            if entity_overlap:
                adjusted += _ENTITY_BOOST
            if same_type:
                adjusted += _SAME_TYPE_BOOST
            scored.append((nbr_id, adjusted))

        scored.sort(key=lambda p: p[1], reverse=True)
        neighbor_ids = [mid for mid, _ in scored[:_MAX_AUTO_LINKS]]

        if not neighbor_ids:
            return AutoLinkResult("skipped", "no_neighbors", [])

        if not self._add_auto_links(memory_id, neighbor_ids):
            return AutoLinkResult("skipped", "persist_failed", [])
        for nbr_id in neighbor_ids:
            self._add_auto_links(nbr_id, [memory_id])

        return AutoLinkResult("ok", "", neighbor_ids)

    def _add_auto_links(self, memory_id: str, new_links: list[str]) -> bool:
        """Merge `new_links` into the memory's auto_links, re-rank against
        the memory's own vector when over the cap (M1), and refresh the body
        block with resolved titles (M3). Returns False when the write-back
        for this memory failed, so the caller can surface a degraded run.
        """
        read_result = self._store.read(memory_id, log_access=False)
        if read_result is None:
            return False

        fm, body = read_result

        # Preserve order, dedupe, drop self-links.
        union = list(dict.fromkeys(list(fm.auto_links) + list(new_links)))
        union = [lid for lid in union if lid != memory_id]
        if not union:
            return True

        if len(union) > _MAX_AUTO_LINKS and self._embeddings is not None:
            # Re-rank the union against this memory's vector. Older links
            # don't get a free pass — if a new candidate is more similar,
            # it displaces them.
            vec = self._embeddings.get_vector(memory_id)
            if vec is not None:
                scored = self._embeddings.search_by_vector(
                    vec, top_k=_MAX_AUTO_LINKS, candidate_ids=union,
                )
                ranked = [mid for mid, _ in scored]
                # Anything FAISS didn't return (e.g. neighbor not embedded
                # yet) keeps insertion-order slots so we don't silently drop
                # a freshly-saved-but-unembedded neighbor.
                for lid in union:
                    if lid not in ranked and len(ranked) < _MAX_AUTO_LINKS:
                        ranked.append(lid)
                updated_auto_links = ranked[:_MAX_AUTO_LINKS]
            else:
                updated_auto_links = union[:_MAX_AUTO_LINKS]
        else:
            updated_auto_links = union[:_MAX_AUTO_LINKS]

        if list(fm.auto_links) == updated_auto_links:
            # No-op write avoidance: prevents churning files when the same
            # neighbor list lands twice (e.g. bidirectional update where the
            # neighbor's link is already present).
            return True

        # Resolve titles for the body block. Frontmatter stays bare IDs.
        items: list[RelatedItem] = []
        for lid in updated_auto_links:
            nbr = self._store.read(lid, log_access=False)
            title = nbr[0].title if nbr is not None else ""
            items.append((lid, title))

        # Pydantic model_copy preserves every field including belief-state
        # additions (slug/confidence/expiry_at). Reconstructing the model by
        # hand is brittle — every new field becomes a silent data-loss site
        # at this call (reviewer finding #2, 2026-04-29).
        updated_fm = fm.model_copy(update={"auto_links": updated_auto_links})
        new_body = replace_related_block(body, items)

        try:
            if not self._store.write(updated_fm, new_body):
                logger.warning("Auto-linker: write blocked/failed for %s", memory_id)
                return False
        except Exception:
            logger.warning("Auto-linker: failed to update %s, continuing", memory_id)
            return False
        return True
    async def rebuild_auto_links(self) -> int:
        """Recompute all auto_links from scratch via FAISS. Returns count updated."""
        all_fms = self._store.list_all()
        count = 0

        for fm in all_fms:
            if fm.auto_links:
                read_result = self._store.read(fm.id, log_access=False)
                if read_result is None:
                    continue
                _, body = read_result
                cleared_fm = fm.model_copy(update={"auto_links": []})
                self._store.write(cleared_fm, replace_related_block(body, []))

        for fm in all_fms:
            read_result = self._store.read(fm.id)
            if read_result is None:
                continue
            _, body = read_result
            result = await self.auto_link(fm.id, body)
            if result.status == "ok" and result.linked:
                count += 1

        logger.info("Rebuilt auto_links for %d memories", count)
        return count
