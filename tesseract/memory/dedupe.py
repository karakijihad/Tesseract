"""Dedupe: title + cosine-similarity guards against restatements.

Two entry points:

* `check(body, embeddings, threshold=0.92)` — cosine-only, the pre-M4 API.
  Still called by `memory_save` (and its F1 regression tests). Fails open
  when embeddings are offline.

* `check_with_title(title, body, store, embeddings, ...)` — M4 layered
  guard used by the librarian. Runs title-exact → title-fuzzy → cosine
  against the memory store; returns a reason string so the caller can
  route between skip (discard) and merge (refresh existing body).

Both are fail-open: when embeddings are offline, cosine is skipped; when
the store is empty, title passes short-circuit to "proceed".
"""

from __future__ import annotations

import difflib
import logging

from tesseract.memory.embeddings import EmbeddingIndex
from tesseract.memory.store import MemoryStore
from tesseract.memory.types import MemoryFrontmatter, MemoryType

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD = 0.92
MERGE_THRESHOLD = 0.88
SKIP_THRESHOLD = 0.92
TITLE_FUZZY_THRESHOLD = 0.85


async def check(
    body: str,
    embeddings: EmbeddingIndex | None,
    threshold: float = DEFAULT_THRESHOLD,
) -> tuple[bool, str | None]:
    """Return (True, None) to proceed with the write, or (False, existing_id)
    when `body` is near-identical to an entry already in the index.

    Threshold defaults to 0.92 — calibrated empirically during F1.
    Adjust by reading `events/writes.jsonl` for `reason: "dedupe"` rates.
    """
    if embeddings is None:
        return True, None

    try:
        results = await embeddings.search(body, top_k=1, require_prefix="mem_")
    except Exception as e:
        logger.warning("dedupe search failed (%s) — failing open", e)
        return True, None

    if not results:
        return True, None

    top_id, score = results[0]
    if score >= threshold:
        logger.info("dedupe hit: body matches %s at score %.3f (threshold %.2f)", top_id, score, threshold)
        return False, top_id
    return True, None


def _strip_type_prefix(title: str) -> str:
    """Drop a leading `[token]` bracket from a section title, if present.

    Mirrors `librarian._extract_type_prefix`'s parser shape; kept local so
    the dedupe module does not import from the librarian.
    """
    t = (title or "").strip()
    if t.startswith("[") and "]" in t:
        t = t[t.index("]") + 1 :].strip()
    return t


def _normalize_title(title: str) -> str:
    return _strip_type_prefix(title).lower()


async def check_with_title(
    title: str,
    body: str,
    store: MemoryStore,
    embeddings: EmbeddingIndex | None,
    *,
    existing: list[MemoryFrontmatter] | None = None,
    new_type: MemoryType | None = None,
    merge_threshold: float = MERGE_THRESHOLD,
    skip_threshold: float = SKIP_THRESHOLD,
    title_fuzzy_threshold: float = TITLE_FUZZY_THRESHOLD,
) -> tuple[bool, str | None, str | None]:
    """Layered dedupe for librarian promotion.

    Returns `(proceed, existing_id, reason)`. `reason` is one of
    `"title_exact"`, `"title_fuzzy"`, `"cosine_merge"`, `"cosine_skip"`,
    or `None` on proceed.

    Short-circuits in order: title exact → title fuzzy → cosine. The
    cosine window `[merge_threshold, skip_threshold)` is the merge band;
    `>= skip_threshold` is the hard skip band (near-identical body).

    When `new_type` is given, title-exact / title-fuzzy only compare
    against entries of the same memory type. This stops `[user] Preferences`
    and `[project] Preferences` from colliding on the same-title check
    after the `[type]` prefix is stripped for normalization.
    """
    norm_new = _normalize_title(title)

    entries = existing if existing is not None else store.list_all()
    title_entries = (
        [fm for fm in entries if fm.type == new_type] if new_type is not None else entries
    )

    if norm_new:
        for fm in title_entries:
            if _normalize_title(fm.title) == norm_new:
                logger.info("dedupe hit: title_exact match %s for %r", fm.id, title)
                return False, fm.id, "title_exact"

        for fm in title_entries:
            norm_existing = _normalize_title(fm.title)
            if not norm_existing:
                continue
            ratio = difflib.SequenceMatcher(None, norm_new, norm_existing).ratio()
            if ratio > title_fuzzy_threshold:
                logger.info(
                    "dedupe hit: title_fuzzy match %s at ratio %.3f for %r",
                    fm.id, ratio, title,
                )
                return False, fm.id, "title_fuzzy"

    if embeddings is None:
        return True, None, None

    try:
        results = await embeddings.search(body, top_k=1, require_prefix="mem_")
    except Exception as e:
        logger.warning("dedupe cosine search failed (%s) — failing open", e)
        return True, None, None

    if not results:
        return True, None, None

    top_id, score = results[0]
    if score >= skip_threshold:
        logger.info("dedupe hit: cosine_skip %s at score %.3f", top_id, score)
        return False, top_id, "cosine_skip"
    if score >= merge_threshold:
        logger.info("dedupe hit: cosine_merge %s at score %.3f", top_id, score)
        return False, top_id, "cosine_merge"
    return True, None, None
