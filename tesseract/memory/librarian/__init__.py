"""Librarian: periodic consolidation pass over the memory store.

Invoked manually via Mirror `/reflect` (which calls `run_pass()` alongside
`reflect_on_session()`). Will run on a schedule once the schedule/alarm
subsystem lands — priority-raised but separate workstream.

Responsibilities:
  1. Scan `daily/*.md` (raw capture) files older than today, parse markdown
     sections, and promote each promotable section into the appropriate
     canonical subdir via `store.write()`. Dedupe-check against embeddings
     before write; WhatNotToSave inside `store.write()` catches trivial /
     request-echo / turn-summary bodies.
  2. Refresh `memory-store/MEMORY.md` — the top-level curated synthesis.
     Lists top-20 most-important entries by frontmatter.importance + the
     most recent 10 promotions (created in the last 7 days).

Today's daily file is skipped — it's still being written to. Repeat passes
on older files are idempotent via two guards: (a) a stable `source_path`
anchor (`daily/YYYY-MM-DD.md#section-title`) checked against already-
promoted memories before the embedding dedupe runs, and (b) when
embeddings are online, cosine-similarity dedupe in `memory.dedupe`. The
source-path guard keeps repeat passes idempotent even when Ollama is
offline (audit-2 finding #2).

The librarian never writes outside `memory-store/`. No workspace edits, no
source-tree edits.

This package splits the consolidation agent into focused stages:
  - `constants` — tunables + routing tables
  - `utils`     — pure helpers (slugging, clipping, parsing)
  - `promotion` — `PromotionMixin`: daily-note → canonical-store promotion
  - `distillation` — `DistillationMixin`: SOUL Growth candidate distillation
  - `summary`   — `SummaryMixin`: MEMORY.md index rendering
The `Librarian` class composes the three mixins and owns `run_pass`.
"""

from __future__ import annotations

import asyncio
import logging

from tesseract.kernel.adapters.base import AdapterOptions, ModelAdapter
from tesseract.memory.embeddings import EmbeddingIndex
from tesseract.memory.librarian.constants import (
    MEMORY_INDEX_FILE,
    PENDING_GROWTH_FILE,
    RECENT_PROMOTIONS_COUNT,
    RECENT_WINDOW_DAYS,
    TOP_RETRIEVALS_COUNT,
)
from tesseract.memory.librarian.distillation import DistillationMixin
from tesseract.memory.librarian.promotion import PromotionMixin
from tesseract.memory.librarian.summary import SummaryMixin
from tesseract.memory.librarian.utils import (
    _anchor_slug,
    _atomic_write,
    _clip_words,
    _extract_type_prefix,
    _is_bookkeeping_entry,
    _is_bookkeeping_title,
    _normalize,
    _parse_candidates,
    _parse_daily_sections,
)
from tesseract.memory.store import MemoryStore

logger = logging.getLogger(__name__)


class Librarian(PromotionMixin, DistillationMixin, SummaryMixin):
    """Consolidation agent — stateless; builds report from store on each run."""

    def __init__(
        self,
        store: MemoryStore,
        embeddings: EmbeddingIndex | None = None,
        adapter: ModelAdapter | None = None,
        adapter_options: AdapterOptions | None = None,
    ) -> None:
        self._store = store
        self._embeddings = embeddings
        self._adapter = adapter
        self._adapter_options = adapter_options

    async def run_pass(self) -> dict:
        """Full consolidation pass.

        Returns `{promoted, deduped, merged, skipped, counts, top, recent}`.

        - `promoted` — daily sections that passed dedupe + WNTS and were written
          to a canonical subdir.
        - `deduped` — daily sections blocked by title-exact/title-fuzzy/
          cosine-skip; new body discarded.
        - `merged` — cosine match in the merge band (0.88–0.92); existing
          entry's body replaced and `updated_at` bumped.
        - `skipped` — daily sections shorter than `_SECTION_MIN_CHARS` or
          rejected by WhatNotToSave inside `store.write()`.
        """
        promoted, deduped, merged, skipped = await self._promote_daily()

        # Three independent read-only scans of the store — each calls
        # `MemoryStore.list_all`, which already serialises through
        # `_fm_cache_lock`, so parallelising them is provably safe: no
        # shared mutable state outside that lock. Not wrapped with
        # `return_exceptions=True` — a scan failure here has always
        # aborted the whole pass (caught by `librarian_heartbeat`'s own
        # try/except), and swallowing one would silently ship a MEMORY.md
        # missing a section instead of surfacing the failure.
        counts, recent, (top, filtered) = await asyncio.gather(
            asyncio.to_thread(self._count_by_type),
            asyncio.to_thread(
                self._recent_entries, RECENT_WINDOW_DAYS, RECENT_PROMOTIONS_COUNT
            ),
            asyncio.to_thread(self._top_by_importance, TOP_RETRIEVALS_COUNT),
        )

        # `_write_memory_index` stays on the loop. It writes the same
        # `MEMORY.md` path `MemoryIndex._write()` owns under its own
        # `RLock` (`tesseract/memory/index.py`) — but this call bypasses
        # that lock entirely, a pre-existing gap between the two writers
        # that predates this pass. Moving this call to a thread would not
        # add a new race (the file already has no cross-writer
        # coordination, on or off the loop), but it would not close the
        # existing one either, so it stays here rather than being wrapped
        # under a false sense of having fixed it. Needs its own pass:
        # either `Librarian` writes through the shared `MemoryIndex`
        # instance, or the two summary formats are reconciled into one.
        self._write_memory_index(top=top, recent=recent, counts=counts)

        logger.info(
            "librarian pass: promoted=%d deduped=%d merged=%d skipped=%d counts=%s top=%d recent=%d filtered=%d",
            promoted, deduped, merged, skipped, counts, len(top), len(recent), filtered,
        )
        return {
            "promoted": promoted,
            "deduped": deduped,
            "merged": merged,
            "skipped": skipped,
            "counts": counts,
            "top": len(top),
            "recent": len(recent),
            "filtered": filtered,
        }


__all__ = [
    "Librarian",
    "MEMORY_INDEX_FILE",
    "PENDING_GROWTH_FILE",
    "RECENT_PROMOTIONS_COUNT",
    "RECENT_WINDOW_DAYS",
    "TOP_RETRIEVALS_COUNT",
    "_anchor_slug",
    "_atomic_write",
    "_clip_words",
    "_extract_type_prefix",
    "_is_bookkeeping_entry",
    "_is_bookkeeping_title",
    "_normalize",
    "_parse_candidates",
    "_parse_daily_sections",
]
