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

#: Kinds of connection the retrieval that produced these hits has already
#: found, so printing them back is pure cost.
#:
#: `similar_to` is an embedding neighbour — `auto_links`, written by the same
#: vector similarity `RetrievalPipeline` ranks with. Measured on the live
#: graph: with it, five hits carried 254 tokens of connections and most of
#: every line read "they read alike", which tells the assistant what the
#: search it did not have to run already told it.
#:
#: What the map has that search does not is the rest: what a record was drawn
#: FROM, what filed it, what wrote it, which subject it is filed under, which
#: part of the machine it came out of. Those are the ones that earn a place in
#: a block that arrives unasked.
_ALREADY_FOUND_BY_SEARCH = frozenset({"similar_to"})


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
    connections_per_memory: int = 0


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
        connections_per_memory=int(
            _require(section, "connections_per_memory", "memory.yaml auto_recall")
        ),
    )


@dataclass(frozen=True)
class RecallItem:
    memory_id: str
    text: str
    score: float
    #: What the map says this record connects to, as `(name, how)` pairs.
    #: Empty when the graph has not been drawn, cannot be read, or holds
    #: nothing beside this record. See :func:`with_connections`.
    connections: tuple[tuple[str, str], ...] = ()


def _is_calibrated(result) -> bool:
    """True when `result.score` means "how relevant is this", on a fixed scale.

    Only the cross-encoder produces such a number. Everything else in the
    pipeline emits a score on its own scale: an exact slug or entity match is a
    fixed constant, the importance prefilter is `importance / 10`, and hybrid
    fusion is a reciprocal-rank sum whose largest possible value is around
    0.033 — two hundredths of what a strong reranked hit scores.

    `min_similarity` is a relevance floor, so it may only be applied to the
    one score that measures relevance. Applied to the others it means nothing:
    it passes every exact match by construction, and rejects nearly every
    fused hit for being on a smaller scale rather than for being irrelevant.
    Which of the two happens depends on whether the reranker model finished
    loading, so the same threshold silently had two behaviours.

    Uncalibrated results are bounded by `top_k` instead, which is what a rank
    without a meaningful magnitude can honestly support.
    """
    return "reranked" in getattr(result, "provenance", ())


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
    tests). Hits scoring below ``min_similarity`` are dropped, but only when
    the score is the cross-encoder's — see :func:`_is_calibrated`. Queries
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
        if _is_calibrated(r) and r.score < min_similarity:
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


def with_connections(items: list[RecallItem], *, limit: int) -> list[RecallItem]:
    """The same hits, each carrying what the map says it connects to.

    **Why this is not a second block.** The map of how records connect had been
    asked a question zero times in 2,387 tool calls over fifteen days, while
    search was asked 48 times. The obvious explanation is wrong: the tool is
    not hidden — it is the first entry in the working set, so its description
    is in front of the assistant on every single turn, at `auto` posture. The
    real reason is in its own guidance, and the guidance is not wrong either:
    a turn almost never arrives as "how do these two connect". It arrives as
    "what do I know about X", and for that the assistant is correctly told to
    search instead. So the atlas was scoped to a question people do not ask
    out loud.

    Nothing about that is fixed by making the tool easier to find. What fixes
    it is the shape `auto_recall` already is: do not wait to be asked.

    And it rides the push that already happens rather than adding one. A
    second unasked block costs every turn whether or not it earns one, and the
    prompt's own `SECTIONS` note says a block that moves invalidates
    everything cached after it. The memories being pushed are already the
    answer to what the turn is about; this says how they relate.

    Degrades exactly the way memory does. A graph that has never been drawn,
    one that cannot be read, or one that does not hold a hit costs the turn
    nothing and the turn proceeds.
    """
    if limit < 1 or not items:
        return items
    try:
        from tesseract.orchestrator.atlas import store as atlas_store
        from tesseract.orchestrator.atlas.model import label_of_link

        atlas = atlas_store.load()
        if not atlas.nodes:
            return items
        # Read once for the whole block. Every hit is looked up in the same
        # edge list, and walking it per hit would be five passes over the
        # graph to answer one question about five records.
        beside: dict[str, list[tuple[str, str]]] = {}
        for edge in atlas.edges.values():
            if edge.type in _ALREADY_FOUND_BY_SEARCH:
                continue
            for near, far in ((edge.subject, edge.object), (edge.object, edge.subject)):
                other = atlas.nodes.get(far)
                if other is None:
                    continue
                beside.setdefault(near, []).append(
                    (other.title or other.id, label_of_link(edge.type))
                )
    except Exception:  # noqa: BLE001 — the map is not a reason to lose the turn
        logger.info("auto_recall: the map could not be read", exc_info=True)
        return items

    out: list[RecallItem] = []
    for item in items:
        near = beside.get(f"mem:{item.memory_id}", ())
        # Sorted, so the same graph gives the same block: an edge dict's order
        # is the order a build happened to write it in, and a prompt that
        # changes between two identical turns is a cache that never hits.
        out.append(
            RecallItem(
                memory_id=item.memory_id,
                text=item.text,
                score=item.score,
                connections=tuple(sorted(set(near))[:limit]),
            )
        )
    return out


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
        if it.connections:
            joined = "; ".join(f"{name} ({how})" for name, how in it.connections)
            lines.append(f"  connects to: {joined}")
    lines.append(_RECALL_CLOSE)
    return "\n".join(lines)


__all__ = [
    "AutoRecallConfig",
    "RecallItem",
    "auto_recall",
    "format_recall_block",
    "load_auto_recall_config",
    "with_connections",
]
