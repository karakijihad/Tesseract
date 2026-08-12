"""Auto memory retrieval — top-k relevant memories injected every turn.

Replaces the regex-based recall-intent nudge (the retired
``tesseract.brain.memory_trigger``): instead of pattern-matching the
operator's text for recall-shaped phrasing and hoping the model calls
``memory_search`` on its own, every turn is used as a retrieval query
up front. Relevant hits are injected as a synthetic context block;
irrelevant/empty results cost nothing.

REUSE, not a parallel path: :func:`auto_recall` calls
``RetrievalPipeline.retrieve`` — the exact same entry point
``MemorySearchTool`` (the ``memory_search`` tool) uses, reached via
``MemorySearchTool.pipeline``. There is exactly one embedding /
retrieval pipeline in the runtime.

Degrades per the project's memory rule (writes unconditional, embedding
best-effort): any exception from the retriever — embedder down, Ollama
unreachable, pipeline error — is logged and yields an empty list, so
the caller skips the injected block and the turn proceeds unaffected.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import yaml

from tesseract.memory.retrieval import MAX_FINAL_RESULTS
from tesseract.paths import config_dir

logger = logging.getLogger(__name__)

_RECALL_OPEN = "[recalled_memories]"
_RECALL_CLOSE = "[/recalled_memories]"


def _require(d: dict, key: str, where: str):
    if key not in d:
        raise RuntimeError(f"missing required key '{key}' in {where}")
    return d[key]


@dataclass(frozen=True)
class AutoRecallConfig:
    top_k: int
    char_cap: int
    min_similarity: float
    min_query_words: int
    dedup_window_turns: int


def load_auto_recall_config() -> AutoRecallConfig:
    """Read ``memory.yaml::auto_recall``; raise loudly on missing keys.

    The path resolves at call time. Frozen at import it captured whatever
    ``TESSERACT_HOME`` held when this module first loaded, so a caller that
    set the home afterwards — every test that isolates itself, and any process
    started before the home is known — read a different config directory than
    the one it had just written to.
    """
    raw = yaml.safe_load((config_dir() / "memory.yaml").read_text(encoding="utf-8"))
    section = _require(raw, "auto_recall", "memory.yaml")
    return AutoRecallConfig(
        top_k=int(_require(section, "top_k", "memory.yaml auto_recall")),
        char_cap=int(_require(section, "char_cap", "memory.yaml auto_recall")),
        min_similarity=float(_require(section, "min_similarity", "memory.yaml auto_recall")),
        min_query_words=int(_require(section, "min_query_words", "memory.yaml auto_recall")),
        dedup_window_turns=int(_require(section, "dedup_window_turns", "memory.yaml auto_recall")),
    )


@dataclass(frozen=True)
class RecallItem:
    memory_id: str
    text: str
    score: float


def _one_line(title: str, body: str, char_cap: int) -> str:
    text = f"{title}: {body}" if title else body
    text = " ".join(text.split())
    if len(text) > char_cap:
        text = text[: char_cap - 1].rstrip() + "…"
    return text


async def auto_recall(
    query: str,
    retriever,
    *,
    top_k: int = 5,
    char_cap: int = 300,
    min_similarity: float,
    min_query_words: int,
    exclude_ids: set[str] | None = None,
) -> list[RecallItem]:
    """Return up to ``top_k`` promoted memories relevant to ``query``.

    ``retriever`` is a :class:`tesseract.memory.retrieval.RetrievalPipeline`
    (typed loosely to keep this module import-light and easy to fake in
    tests). Hits scoring below ``min_similarity`` are dropped. Queries
    shorter than ``min_query_words`` short-circuit before the retriever is
    even called — one-word acks ("ok", "thanks") aren't worth a retrieval
    round-trip. ``exclude_ids`` (cross-turn dedup, see
    ``ChatSession._recall_dedup_window``) is filtered out BEFORE the
    ``top_k`` cap so a fresh candidate can backfill the skipped slot rather
    than shrinking the block for free.

    ``RetrievalPipeline.retrieve`` caps its OWN final result count at
    ``MAX_FINAL_RESULTS`` regardless of the ``top_k`` it's asked for, so
    requesting exactly ``top_k`` candidates leaves no surplus to backfill
    from once dedup excludes a hit. To give dedup real backfill room, we
    over-fetch up to ``MAX_FINAL_RESULTS`` candidates from the retriever,
    apply the floor + exclusion filters, and only THEN trim to ``top_k``
    locally. This bounds — but does not eliminate — the shrink: backfill
    draws from the retriever's surplus above ``top_k``
    (``MAX_FINAL_RESULTS - top_k``, e.g. 2 when top_k=5). A sustained
    same-topic conversation can still exclude more ids than that surplus,
    in which case the block shrinks below ``top_k`` (never crashes, never
    duplicates, never re-injects an excluded id) — which is intended:
    don't re-inject what was just shown. Any exception from the retriever
    degrades to an empty list — memory retrieval is best-effort and must
    never block the turn.
    """
    if not query or not query.strip():
        return []
    if len(query.split()) < min_query_words:
        return []
    fetch_k = max(top_k, MAX_FINAL_RESULTS)
    try:
        packet = await retriever.retrieve(query, top_k=fetch_k, include_work_history=False)
    except Exception:
        logger.warning("auto_recall: retrieval failed, skipping recall block", exc_info=True)
        return []

    excluded = exclude_ids or set()
    items: list[RecallItem] = []
    for r in packet.results:
        if r.score < min_similarity:
            continue
        if r.memory_id in excluded:
            continue
        items.append(RecallItem(
            memory_id=r.memory_id,
            text=_one_line(r.title, r.body, char_cap),
            score=r.score,
        ))
        if len(items) >= top_k:
            break
    return items


def format_recall_block(items: list[RecallItem]) -> str:
    """Render the ``[recalled_memories]``-delimited context block.

    Empty input returns ``""`` so the caller can skip injection
    entirely — zero tokens spent when nothing clears the floor.
    """
    if not items:
        return ""
    lines = [_RECALL_OPEN]
    for it in items:
        lines.append(f"- {it.text} ({it.memory_id}, {it.score:.2f})")
    lines.append(_RECALL_CLOSE)
    return "\n".join(lines)


__all__ = [
    "AutoRecallConfig",
    "RecallItem",
    "auto_recall",
    "format_recall_block",
    "load_auto_recall_config",
]
